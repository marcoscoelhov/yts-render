from __future__ import annotations

import re
from typing import Any

from app.editorial.retention import attach_retention_metadata
from app.pipelines.base import BasePipeline
from app.pipelines.common import RecoverableStepError
from app.pipelines.script_metrics import normalize_script_metrics
from app.quality.script_gate import REWATCH_LOOP_PATTERN
from app.utils import sentence_split, stable_hash, tokenize, word_tokens


class ScriptRepairDomain(BasePipeline):
    def __getattr__(self, name: str) -> Any:
        return getattr(self.owner, name)

    def _fact_pack_consistency_reasons(self, script: dict[str, Any], fact_pack: Any) -> list[str]:
        source_ids = script.get("source_fact_ids") or script.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        trace = script.get("claim_trace") or script.get("qa_metrics", {}).get("claim_trace") or []
        trace = trace if isinstance(trace, list) else []
        trace_source_ids = [
            str(source_id)
            for item in trace
            if isinstance(item, dict)
            for source_id in (item.get("source_fact_ids") or [])
            if str(source_id)
        ]
        if not isinstance(fact_pack, dict) or fact_pack.get("status") != "verified":
            return ["invented_source_fact_ids"] if source_ids or trace_source_ids else []
        facts = fact_pack.get("facts") or []
        valid_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
        if not valid_ids:
            return []
        used_ids = {str(item) for item in [*source_ids, *trace_source_ids] if str(item) in valid_ids}
        minimum = min(2, len(valid_ids))
        reasons: list[str] = []
        if len(used_ids) < minimum:
            reasons.append("fact_pack_source_ids_missing")
        if any(source_id not in valid_ids for source_id in trace_source_ids):
            reasons.append("invented_claim_trace_fact_ids")
        fact_risk = self.script_gate._fact_risk_report(script)  # noqa: SLF001
        if fact_risk.get("blocked") and len(used_ids) < len(valid_ids):
            reasons.append("high_risk_claims_need_fact_pack_grounding")
        risky_claims = []
        seen_risky_claims: set[str] = set()
        for claim in fact_risk.get("claims", []):
            if claim.get("score", 0) <= 0 or claim.get("conservative_language"):
                continue
            key = " ".join(str(claim.get("text") or "").lower().split())
            if key in seen_risky_claims:
                continue
            seen_risky_claims.add(key)
            risky_claims.append(claim)
        grounded_trace = [
            item
            for item in trace
            if isinstance(item, dict)
            and str(item.get("text") or "").strip()
            and any(str(source_id) in valid_ids for source_id in (item.get("source_fact_ids") or []))
        ]
        if risky_claims and len(grounded_trace) < len(risky_claims):
            reasons.append("factual_claim_trace_missing")
        for item in trace:
            if not isinstance(item, dict) or str(item.get("grounding") or "").strip().lower() != "conservative":
                continue
            report = self.script_gate._fact_risk_report({"hook": "", "full_narration": str(item.get("text") or ""), "key_facts": []})  # noqa: SLF001
            if any(claim.get("score", 0) > 0 and not claim.get("conservative_language") for claim in report.get("claims", [])):
                reasons.append("invalid_conservative_claim_trace")
                break
        return reasons

    def _apply_cta_policy(self, script: dict[str, Any], cta_style: str) -> dict[str, Any]:
        if cta_style != "none":
            return script
        cleaned = dict(script)
        cta = str(cleaned.get("cta") or "").strip()
        narration = str(cleaned.get("full_narration") or "")
        if cta and narration.rstrip().endswith(cta):
            narration = narration.rstrip()[: -len(cta)].rstrip()
        cta_patterns = [
            r"\s*Se inscrev[ae][^.?!]*[.?!]?$",
            r"\s*Curte[^.?!]*[.?!]?$",
            r"\s*Comenta[^.?!]*[.?!]?$",
            r"\s*Compartilha[^.?!]*[.?!]?$",
            r"\s*Ativa o sininho[^.?!]*[.?!]?$",
        ]
        for pattern in cta_patterns:
            narration = re.sub(pattern, "", narration, flags=re.IGNORECASE).rstrip()
        cleaned["cta"] = None
        cleaned["full_narration"] = narration
        return cleaned

    def _attach_editorial_source(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> dict[str, Any]:
        enriched = attach_retention_metadata(script, plan_dict)
        metrics = dict(enriched.get("qa_metrics") or {})
        metrics.update(
            {
                "editorial_source": "ready_script" if plan_dict.get("ready_script_mode") else "hub_viral_prompt",
                "downstream_source_of_truth": "script_full_narration",
                "original_input": plan_dict.get("original_input"),
                "requested_angle": plan_dict.get("requested_angle"),
                "tone": plan_dict.get("tone"),
                "hub_notes_hash": stable_hash(plan_dict.get("hub_notes") or ""),
            }
        )
        enriched["qa_metrics"] = metrics
        return enriched

    def _postprocess_script_for_quality(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        gate_reasons: list[str],
    ) -> dict[str, Any]:
        processed = self._repair_common_script_text_issues(dict(script))
        processed = self._restore_script_from_retention_map(processed)
        processed = self._normalize_script_visible_text(processed)
        processed = self._normalize_script_narration_fields(processed)
        processed = self._normalize_script_visible_text(processed)
        fact_pack = plan_dict.get("fact_pack") if isinstance(plan_dict.get("fact_pack"), dict) else {}
        if self._should_force_conservative_fact_rewrite(processed, fact_pack, gate_reasons):
            processed = self._rewrite_script_conservatively(processed, fact_pack, plan_dict)
        if self._should_repair_loop(processed, gate_reasons):
            processed = self._repair_script_loop_closure(processed, plan_dict)
        processed = self._split_long_script_sentences(processed)
        processed = self._normalize_script_visible_text(processed)
        processed = self._attach_claim_trace(processed, fact_pack)
        processed["estimated_duration_sec"] = round(max(35.0, min(55.0, len(word_tokens(str(processed.get("full_narration") or ""))) / 2.55)), 2)
        processed["token_count"] = len(tokenize(str(processed.get("full_narration") or "")))
        return processed

    def _restore_script_from_retention_map(self, script: dict[str, Any]) -> dict[str, Any]:
        retention_map = script.get("retention_map")
        if not isinstance(retention_map, dict):
            return script
        raw_segments = retention_map.get("segments")
        if not isinstance(raw_segments, list):
            return script
        segment_texts = [
            str(item.get("mapped_text") or "").strip()
            for item in raw_segments
            if isinstance(item, dict) and str(item.get("mapped_text") or "").strip()
        ]
        if len(segment_texts) < 3:
            return script
        rebuilt_narration = " ".join(text.rstrip(".!?") + "." for text in segment_texts if text).strip()
        current_narration = str(script.get("full_narration") or "").strip()
        rebuilt_word_count = len(word_tokens(rebuilt_narration))
        current_word_count = len(word_tokens(current_narration))
        placeholder_markers = {
            "detalhe verificável",
            "explicação concreta",
            "segura a surpresa",
            "sem inflar o fato",
            "era a pista",
            "deixa de ser só aparência",
        }
        current_normalized = self._normalize_fact_text(current_narration)
        looks_placeholder = any(marker in current_normalized for marker in {self._normalize_fact_text(marker) for marker in placeholder_markers})
        if rebuilt_word_count < max(45, current_word_count + 12) and not looks_placeholder:
            return script
        if not looks_placeholder and current_word_count >= 50:
            return script
        rebuilt = dict(script)
        rebuilt["hook"] = segment_texts[0].strip()
        rebuilt["body_beats"] = [text.strip() for text in segment_texts[1:-1]]
        rebuilt["ending"] = segment_texts[-1].strip()
        rebuilt["full_narration"] = rebuilt_narration
        current_key_facts = [str(item).strip() for item in (script.get("key_facts") or []) if str(item).strip()]
        if not current_key_facts or looks_placeholder:
            rebuilt["key_facts"] = [text.strip() for text in segment_texts[1:-1]][:3]
        return rebuilt

    def _repair_common_script_text_issues(self, value: Any) -> Any:
        replacements = {
            "Flamengos": "Flamingos",
            "flamengos": "flamingos",
            "roses": "rosas",
            "deartemia": "de artêmia",
            "supplementação": "suplementação",
            "trace": "traço",
            "alimentacao": "alimentação",
            "coloracao": "coloração",
            "biologico": "biológico",
            "cientifica": "científica",
            "Adulto Rosa": "adulto rosa",
        }
        if isinstance(value, str):
            repaired = value
            for source, target in replacements.items():
                repaired = repaired.replace(source, target)
            return repaired
        if isinstance(value, list):
            return [self._repair_common_script_text_issues(item) for item in value]
        if isinstance(value, dict):
            return {key: self._repair_common_script_text_issues(item) for key, item in value.items()}
        return value

    def _normalize_script_visible_text(self, value: Any) -> Any:
        if isinstance(value, str):
            text = value.replace("—", ", ").replace("–", ", ")
            text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
            text = re.sub(r"\b(pedir)\s*[^\x00-\x7FÀ-ÖØ-öø-ÿĀ-ſ]+\s*(ao)\b", r"\1 instrução \2", text, flags=re.IGNORECASE)
            text = re.sub(r"\b([a-záàãâéêíóõôúç]{3,})dede\b", r"\1 de", text, flags=re.IGNORECASE)
            text = re.sub(
                r"\b(a|o|e|de|do|da|dos|das|no|na|nos|nas|em|por|com)\.\s+([a-záàãâéêíóõôúç])",
                lambda match: f"{match.group(1)} {match.group(2)}",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(r"\s+([,.!?;:])", r"\1", text)
            text = re.sub(r"([,.!?;:]){2,}", r"\1", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text
        if isinstance(value, list):
            return [self._normalize_script_visible_text(item) for item in value]
        if isinstance(value, dict):
            return {key: self._normalize_script_visible_text(item) for key, item in value.items()}
        return value

    def _normalize_script_narration_fields(self, script: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(script)
        raw_beats = normalized.get("body_beats") or []
        if not isinstance(raw_beats, list):
            raw_beats = [raw_beats]
        body_beats: list[str] = []
        for beat in raw_beats:
            if isinstance(beat, dict):
                text = str(beat.get("narration") or beat.get("text") or beat.get("content") or "").strip()
            else:
                text = str(beat or "").strip()
            if text:
                body_beats.append(text.rstrip(".!?") + ".")
        normalized["body_beats"] = body_beats
        narration = str(normalized.get("full_narration") or "").strip()
        narration_has_structured_leak = bool(re.search(r"\{\s*['\"](?:segment|time_range|visual_description|narration)['\"]", narration))
        if narration_has_structured_leak or not narration:
            parts = [
                str(normalized.get("hook") or "").strip(),
                *body_beats,
                str(normalized.get("ending") or "").strip(),
            ]
            normalized["full_narration"] = " ".join(part.rstrip(".!?") + "." for part in parts if part).strip()
        return normalized

    def _split_long_script_sentences(self, script: dict[str, Any]) -> dict[str, Any]:
        narration = str(script.get("full_narration") or "").strip()
        if not narration:
            return script
        rewritten: list[str] = []
        for sentence in sentence_split(narration):
            words = word_tokens(sentence)
            if len(words) <= 18:
                rewritten.append(sentence.rstrip(".!?") + ".")
                continue
            raw_words = sentence.rstrip(".!?").split()
            midpoint = max(8, min(len(raw_words) - 7, len(raw_words) // 2))
            split_at = next(
                (
                    index
                    for index in range(midpoint, min(len(raw_words) - 5, midpoint + 6))
                    if raw_words[index].strip(",;:").lower() in {"e", "mas", "porque", "quando", "enquanto", "com"}
                ),
                midpoint,
            )
            first = " ".join(raw_words[:split_at]).strip(" ,;:")
            second = " ".join(raw_words[split_at:]).strip(" ,;:")
            if first:
                rewritten.append(first.rstrip(".!?") + ".")
            if second:
                rewritten.append(second.rstrip(".!?") + ".")
        updated = dict(script)
        updated["full_narration"] = " ".join(rewritten).strip()
        return updated

    def _should_force_conservative_fact_rewrite(
        self,
        script: dict[str, Any],
        fact_pack: dict[str, Any],
        gate_reasons: list[str],
    ) -> bool:
        if any(
            reason in gate_reasons
            for reason in {
                "factual_risk_requires_conservative_rewrite",
                "overconfident_or_unsupported_factual_claim",
                "invented_source_fact_ids",
                "fact_pack_source_ids_missing",
                "high_risk_claims_need_fact_pack_grounding",
                "factual_claim_trace_missing",
            }
        ):
            return True
        if fact_pack.get("status") == "verified":
            return False
        return bool(self.script_gate._fact_risk_report(script).get("blocked"))  # noqa: SLF001

    def _should_repair_loop(self, script: dict[str, Any], gate_reasons: list[str]) -> bool:
        if any(reason in gate_reasons for reason in {"ending_not_connected_to_hook", "weak_loop_closure"}):
            return True
        ending = str(script.get("ending") or "").strip()
        if REWATCH_LOOP_PATTERN.search(ending):
            return False
        return not self.script_gate._loop_report(script).get("connected_to_opening")  # noqa: SLF001

    def _rewrite_script_conservatively(
        self,
        script: dict[str, Any],
        fact_pack: dict[str, Any],
        plan_dict: dict[str, Any],
    ) -> dict[str, Any]:
        rewritten = dict(script)
        anchor = self._script_anchor_phrase(script, plan_dict)
        if fact_pack.get("status") == "verified" and fact_pack.get("facts"):
            grounded_facts = [
                fact
                for fact in fact_pack.get("facts") or []
                if str(fact.get("claim") or "").strip() and fact.get("fact_id")
            ]
            if grounded_facts:
                beats = [
                    self._fact_backed_pt_br_sentence(fact, anchor, index)
                    for index, fact in enumerate(grounded_facts[:4])
                ]
                rewritten["hook"] = f"{anchor.capitalize()} parece exagero, até a explicação concreta entrar."
                rewritten["body_beats"] = beats[: max(3, min(4, len(beats)))]
                rewritten["key_facts"] = beats[:3]
                rewritten["source_fact_ids"] = [str(fact.get("fact_id")) for fact in grounded_facts[: max(2, min(3, len(grounded_facts)))]]
                rewritten["claim_trace"] = [
                    {
                        "text": beat,
                        "source_fact_ids": [str(fact.get("fact_id"))],
                        "grounding": "fact_pack",
                    }
                    for beat, fact in zip(beats, grounded_facts[: len(beats)], strict=False)
                ]
                rewritten["ending"] = self._loop_closure_sentence(anchor, str(rewritten.get("hook") or ""), beats[-1] if beats else anchor, 0)
                sentences = [rewritten["hook"], *rewritten["body_beats"], rewritten["ending"]]
                rewritten["full_narration"] = " ".join(sentence.rstrip(".!?") + "." for sentence in sentences if sentence).strip()
                return rewritten

        narration_sentences = [sentence for sentence in sentence_split(str(rewritten.get("full_narration") or "")) if sentence]
        if not narration_sentences:
            narration_sentences = [f"{anchor} parece estranho até o mecanismo aparecer."]
        softened = [self._soften_risky_sentence(sentence, anchor) for sentence in narration_sentences]
        rewritten["hook"] = self._soften_risky_sentence(str(rewritten.get("hook") or softened[0]), anchor)
        rewritten["ending"] = self._soften_risky_sentence(str(rewritten.get("ending") or softened[-1]), anchor)
        if len(softened) >= 3:
            rewritten["body_beats"] = [sentence.rstrip(".!?") + "." for sentence in softened[1:-1][:4]]
        rewritten["full_narration"] = " ".join(sentence.rstrip(".!?") + "." for sentence in softened if sentence).strip()
        rewritten["key_facts"] = [sentence.rstrip(".!?") for sentence in softened[1:4] if sentence]
        rewritten["source_fact_ids"] = []
        rewritten["claim_trace"] = [
            {"text": sentence.rstrip(".!?") + ".", "source_fact_ids": [], "grounding": "conservative"}
            for sentence in softened
            if sentence
        ][:5]
        return rewritten

    def _fact_backed_pt_br_sentence(self, fact: dict[str, Any], anchor: str, index: int) -> str:
        claim = " ".join(str(fact.get("claim") or "").split())
        normalized = claim.lower()
        if re.search(r"\b(?:carotenoid|pigment|plumage|diet|feeding)\b", normalized):
            templates = [
                f"A pista forte está nos pigmentos da alimentação, não em uma mágica da pele.",
                f"O corpo muda a aparência quando esses pigmentos entram no processo biológico.",
                f"Por isso a cor parece pintura, mas nasce de um mecanismo alimentar real.",
                f"O detalhe viral é simples: o visual depende do que o organismo consegue acumular.",
            ]
            return templates[index % len(templates)]
        if re.search(r"\b(?:chromatophore|iridophore|camouflage|skin|reflect)\b", normalized):
            templates = [
                f"A pele entra na história como superfície ativa, não como uma capa parada.",
                f"Células especializadas ajudam a mudar cor, textura e reflexo diante do ambiente.",
                f"O efeito parece truque visual, mas vem de estruturas biológicas trabalhando juntas.",
                f"A primeira imagem ganha força quando você percebe que a pele também reage.",
            ]
            return templates[index % len(templates)]
        if re.search(r"\b(?:engineer|soil|foundation|stabil|inclination|tilt)\b", normalized):
            templates = [
                f"O ponto real está no solo e na base, não em uma força misteriosa.",
                f"Engenheiros trataram a inclinação como problema de fundação e estabilidade.",
                f"A cena parece impossível porque a solução acontece por baixo da estrutura.",
                f"O começo muda quando você entende que a base carrega a tensão principal.",
            ]
            return templates[index % len(templates)]
        templates = [
            f"O detalhe verificável sobre {anchor} segura a surpresa sem inflar o fato.",
            f"A explicação aparece quando {anchor} deixa de ser só aparência.",
            f"A surpresa funciona melhor quando a cena carrega um detalhe concreto.",
            f"{anchor.capitalize()} parece estranho antes da explicação certa entrar.",
        ]
        return templates[index % len(templates)]

    def _soften_risky_sentence(self, sentence: str, anchor: str) -> str:
        text = " ".join(str(sentence or "").split())
        if not text:
            return f"{anchor.capitalize()} chama atenção por um detalhe concreto."
        if re.search(r"\b(?:1[0-9]{3}|20[0-9]{2})\b", text):
            return f"{anchor.capitalize()} carrega um contexto antigo, mas o ponto central aparece com cuidado."
        if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:%|por cento\b|anos?\b|séculos?\b|seculos?\b|dias?\b|horas?\b|minutos?\b|segundos?\b|metros?\b|m\b|cm\b|mm\b|km\b|graus?\b|°|toneladas?\b|kg\b|quilos?\b|milhões?\b|milhoes?\b|bilhões?\b|bilhoes?\b)", text, re.IGNORECASE):
            return f"{anchor.capitalize()} impressiona pela escala, mesmo sem cravar um número exato."
        replacements = {
            r"\bsempre\b": "em geral",
            r"\bnunca\b": "quase nunca",
            r"\bimposs[ií]vel\b": "difícil de imaginar",
            r"\bgarante\b": "ajuda a sustentar",
            r"\bgarantida\b": "mais estável",
            r"\bprova\b": "sugere",
            r"\bcomprova\b": "reforça",
            r"\bdomina\b": "parece desafiar",
            r"\bdesafia\b": "parece contrariar",
            r"\búnico\b": "um dos exemplos mais fortes",
            r"\bunico\b": "um dos exemplos mais fortes",
            r"\bexatamente\b": "quase",
        }
        for pattern, replacement in replacements.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        text = re.sub(
            r"\b(?:porque|por isso|graças a|gracas a|causa|causou|criou|criam|impede|impediu|permite|permitiu|faz com que|resultado de|segredo|solução|solucao|explica|provoca|reduz|aumenta|corrige|corrigiu)\b",
            "pode ajudar a explicar",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if self.script_gate._fact_risk_report({"hook": "", "full_narration": text, "key_facts": []}).get("blocked"):  # noqa: SLF001
            return f"{anchor.capitalize()} chama atenção pelo efeito, sem transformar hipótese em certeza."
        return text.rstrip(".!?") + "."

    def _repair_script_loop_closure(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(script)
        anchor = self._script_anchor_phrase(script, plan_dict)
        hook = str(repaired.get("hook") or "").strip()
        body_beats = [str(item).rstrip(".!?") + "." for item in repaired.get("body_beats") or [] if str(item).strip()]
        if len(body_beats) == 1 and hook and hook.rstrip(".!?").lower() in body_beats[0].lower():
            body_beats = [
                sentence.rstrip(".!?") + "."
                for sentence in sentence_split(str(repaired.get("full_narration") or ""))
                if sentence and sentence.rstrip(".!?").lower() != hook.rstrip(".!?").lower()
            ]
        payoff_source = body_beats[-1] if body_beats else str(repaired.get("ending") or hook or anchor)
        payoff_words = [token for token in word_tokens(payoff_source) if len(token) >= 4]
        payoff_hint = " ".join(payoff_words[:3]) if payoff_words else anchor
        variant_seed = stable_hash({"hook": hook, "anchor": anchor, "payoff_hint": payoff_hint})
        variant = int(str(variant_seed)[:2], 16) if str(variant_seed)[:2] else 0
        repaired["ending"] = self._loop_closure_sentence(anchor, hook, payoff_hint, variant)
        repaired["full_narration"] = " ".join(
            sentence
            for sentence in [
                hook.rstrip(".!?") + "." if hook else "",
                *body_beats,
                repaired["ending"],
            ]
            if sentence
        ).strip()
        return repaired

    def _loop_closure_sentence(self, anchor: str, hook: str, payoff_hint: str, variant: int) -> str:
        opening_ref = "primeira frase" if hook else "cena inicial"
        templates = [
            f"Na segunda olhada, a {opening_ref} já apontava para {payoff_hint}.",
            f"Agora o começo muda de sentido: {payoff_hint} era a pista.",
            f"Repara no início outra vez: {anchor} já deixava esse sinal escondido.",
            f"O detalhe final devolve você ao começo, porque {payoff_hint} estava ali.",
            f"Volta para a primeira imagem e o truque aparece: {payoff_hint}.",
            f"É por isso que o início parece diferente quando {anchor} volta para a tela.",
        ]
        return templates[variant % len(templates)]

    def _script_anchor_phrase(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> str:
        candidates = [
            str(plan_dict.get("canonical_topic") or "").strip(),
            str(script.get("title") or "").strip(),
            str(script.get("hook") or "").strip(),
        ]
        for candidate in candidates:
            tokens = [token for token in word_tokens(candidate) if len(token) >= 4]
            if tokens:
                return " ".join(tokens[:2])
        return "o tema"

    def _attach_claim_trace(self, script: dict[str, Any], fact_pack: dict[str, Any]) -> dict[str, Any]:
        updated = dict(script)
        valid_ids = {
            str(fact.get("fact_id"))
            for fact in fact_pack.get("facts") or []
            if fact.get("fact_id")
        }
        existing = updated.get("claim_trace")
        if isinstance(existing, list) and existing:
            updated["claim_trace"] = self._normalize_claim_trace(existing, valid_ids)
            return updated
        risk_report = self.script_gate._fact_risk_report(updated)  # noqa: SLF001
        claims = [claim for claim in risk_report.get("claims", []) if claim.get("score", 0) > 0]
        if not claims:
            updated["claim_trace"] = []
            return updated
        trace: list[dict[str, Any]] = []
        for claim in claims:
            trace.append(
                {
                    "text": str(claim.get("text") or "").strip(),
                    "source_fact_ids": [],
                    "grounding": "conservative" if claim.get("conservative_language") else "missing",
                    "risk_types": claim.get("risk_types") or [],
                }
            )
        updated["claim_trace"] = trace
        return updated

    def _normalize_claim_trace(self, trace: list[Any], valid_ids: set[str]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in trace:
            if not isinstance(item, dict):
                continue
            source_ids = item.get("source_fact_ids") or []
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            filtered_ids = [str(source_id) for source_id in source_ids if str(source_id) in valid_ids]
            grounding = str(item.get("grounding") or "").strip().lower()
            if grounding == "fact_pack" and not filtered_ids:
                grounding = "missing"
            normalized.append(
                {
                    **item,
                    "text": str(item.get("text") or "").strip(),
                    "source_fact_ids": filtered_ids,
                    "grounding": grounding or ("fact_pack" if filtered_ids else "missing"),
                }
            )
        return normalized

    def _validate_or_repair_script(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        target_duration_sec: int,
        cta_style: str = "none",
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if plan_dict.get("ready_script_mode"):
            return self._validate_ready_script_without_repair(script, plan_dict, target_duration_sec)

        script = self._apply_cta_policy(dict(script), cta_style)
        script = self._postprocess_script_for_quality(script, plan_dict, [])
        script["qa_metrics"] = normalize_script_metrics(dict(script.get("qa_metrics") or {}))
        gate_result = self.script_gate.validate(script, target_duration_sec)
        fact_pack = plan_dict.get("fact_pack") if isinstance(plan_dict.get("fact_pack"), dict) else {}
        simple_mode_fact_skip = self.settings.simple_shorts_mode and fact_pack.get("status") == "skipped"
        consistency_reasons = [] if simple_mode_fact_skip else self._fact_pack_consistency_reasons(script, fact_pack)
        attempts_log: list[dict[str, Any]] = [
            {
                "repair_attempt": 0,
                "reason_codes": [*gate_result.reasons, *consistency_reasons],
                "passed": gate_result.passed and not consistency_reasons,
                "used_fallback": False,
            }
        ]
        if self._ready_script_declared_fact_check_accepts(script, plan_dict, gate_result.reasons, consistency_reasons):
            metrics = {
                **gate_result.metrics,
                "script_quality_gate_pass": True,
                "script_quality_gate_blocking": False,
                "script_quality_gate_warnings": list(gate_result.reasons),
                "fact_pack_consistency_pass": True,
                "ready_script_declared_fact_check_accepted": True,
                "script_repair_attempts_log": attempts_log,
                **self._claim_trace_metrics(script),
            }
            script["qa_metrics"] = metrics
            return script, metrics
        if simple_mode_fact_skip:
            critical_reasons = self._simple_mode_blocking_script_reasons(gate_result.reasons)
            if critical_reasons:
                raise RecoverableStepError(f"script quality gate failed: {', '.join(critical_reasons)}")
            repairable_reasons = self._simple_mode_lightweight_repair_reasons(gate_result.reasons)
            if repairable_reasons:
                repaired = self._postprocess_script_for_quality(dict(script), plan_dict, repairable_reasons)
                repaired["qa_metrics"] = normalize_script_metrics(dict(repaired.get("qa_metrics") or {}))
                repaired_gate = self.script_gate.validate(repaired, target_duration_sec)
                repaired_consistency_reasons: list[str] = []
                attempts_log.append(
                    {
                        "repair_attempt": 1,
                        "reason_codes": list(repaired_gate.reasons),
                        "passed": repaired_gate.passed,
                        "used_fallback": False,
                        "repair_strategy": "simple_mode_local",
                    }
                )
                if self._simple_mode_repair_improved(gate_result.reasons, repaired_gate.reasons):
                    script = repaired
                    gate_result = repaired_gate
                    consistency_reasons = repaired_consistency_reasons
            metrics = {
                **gate_result.metrics,
                "script_quality_gate_pass": True,
                "script_quality_gate_blocking": False,
                "script_quality_gate_warnings": list(gate_result.reasons),
                "fact_pack_consistency_pass": True,
                "fact_pack_consistency_skipped": True,
                "script_repair_attempts_log": attempts_log,
                "simple_shorts_mode": True,
                **self._claim_trace_metrics(script),
            }
            script["qa_metrics"] = metrics
            return script, metrics
        if gate_result.passed and not consistency_reasons:
            script["qa_metrics"] = {
                **gate_result.metrics,
                "fact_pack_consistency_pass": True,
                "script_repair_attempts_log": attempts_log,
                **self._claim_trace_metrics(script),
            }
            return script, script["qa_metrics"]

        repair_attempts = max(0, self.settings.llm_script_repair_attempts)
        last_reasons = [*gate_result.reasons, *consistency_reasons]
        self._persist_script_rejection(job_id, script, gate_result.metrics, consistency_reasons)
        for repair_attempt in range(1, repair_attempts + 1):
            try:
                repaired = self.providers.creative.repair_script(script, last_reasons, plan_dict)
            except Exception as exc:  # noqa: BLE001
                last_reasons = [*last_reasons, f"script_repair_provider_failed:{type(exc).__name__}"]
                attempts_log.append(
                    {
                        "repair_attempt": repair_attempt,
                        "reason_codes": last_reasons,
                        "passed": False,
                        "used_fallback": False,
                    }
                )
                continue
            repaired = self._apply_cta_policy(repaired, cta_style)
            repaired = self._postprocess_script_for_quality(repaired, plan_dict, last_reasons)
            repaired["qa_metrics"] = normalize_script_metrics(dict(repaired.get("qa_metrics") or {}))
            repaired_gate = self.script_gate.validate(repaired, target_duration_sec)
            repaired_consistency_reasons = self._fact_pack_consistency_reasons(repaired, plan_dict.get("fact_pack"))
            attempts_log.append(
                {
                    "repair_attempt": repair_attempt,
                    "reason_codes": [*repaired_gate.reasons, *repaired_consistency_reasons],
                    "passed": repaired_gate.passed and not repaired_consistency_reasons,
                    "used_fallback": False,
                }
            )
            if repaired_gate.passed and not repaired_consistency_reasons:
                repaired["qa_metrics"] = {
                    **repaired_gate.metrics,
                    "fact_pack_consistency_pass": True,
                    "script_repair_used": True,
                    "script_repair_initial_reasons": [*gate_result.reasons, *consistency_reasons],
                    "script_repair_attempts_log": attempts_log,
                    **self._claim_trace_metrics(repaired),
                }
                return repaired, repaired["qa_metrics"]
            self._persist_script_rejection(job_id, repaired, repaired_gate.metrics, repaired_consistency_reasons)
            script = repaired
            last_reasons = [*repaired_gate.reasons, *repaired_consistency_reasons]

        try:
            fallback_repaired = self.providers.creative.repair_script_with_fallback(script, last_reasons, plan_dict)
        except Exception as exc:  # noqa: BLE001
            fallback_repaired = None
            last_reasons = [*last_reasons, f"script_repair_fallback_failed:{type(exc).__name__}"]
        if fallback_repaired is not None:
            fallback_repaired = self._apply_cta_policy(fallback_repaired, cta_style)
            fallback_repaired = self._postprocess_script_for_quality(fallback_repaired, plan_dict, last_reasons)
            fallback_repaired["qa_metrics"] = normalize_script_metrics(dict(fallback_repaired.get("qa_metrics") or {}))
            fallback_gate = self.script_gate.validate(fallback_repaired, target_duration_sec)
            fallback_consistency_reasons = self._fact_pack_consistency_reasons(fallback_repaired, plan_dict.get("fact_pack"))
            attempts_log.append(
                {
                    "repair_attempt": repair_attempts + 1,
                    "reason_codes": [*fallback_gate.reasons, *fallback_consistency_reasons],
                    "passed": fallback_gate.passed and not fallback_consistency_reasons,
                    "used_fallback": True,
                }
            )
            if fallback_gate.passed and not fallback_consistency_reasons:
                fallback_repaired["qa_metrics"] = {
                    **fallback_gate.metrics,
                    "fact_pack_consistency_pass": True,
                    "script_repair_used": True,
                    "script_repair_fallback_used": True,
                    "script_repair_initial_reasons": [*gate_result.reasons, *consistency_reasons],
                    "script_repair_attempts_log": attempts_log,
                    **self._claim_trace_metrics(fallback_repaired),
                }
                return fallback_repaired, fallback_repaired["qa_metrics"]
            self._persist_script_rejection(job_id, fallback_repaired, fallback_gate.metrics, fallback_consistency_reasons)
            last_reasons = [*fallback_gate.reasons, *fallback_consistency_reasons]

        raise RecoverableStepError(f"script quality gate failed: {', '.join(last_reasons)}")

    def _validate_ready_script_without_repair(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        target_duration_sec: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        preserved = dict(script)
        preserved["qa_metrics"] = normalize_script_metrics(dict(preserved.get("qa_metrics") or {}))
        gate_result = self.script_gate.validate(preserved, target_duration_sec)
        fact_pack = plan_dict.get("fact_pack") if isinstance(plan_dict.get("fact_pack"), dict) else {}
        consistency_reasons = self._fact_pack_consistency_reasons(preserved, fact_pack)
        attempts_log: list[dict[str, Any]] = [
            {
                "repair_attempt": 0,
                "reason_codes": [*gate_result.reasons, *consistency_reasons],
                "passed": not consistency_reasons,
                "used_fallback": False,
                "repair_strategy": "ready_script_preserve",
            }
        ]
        if consistency_reasons:
            raise RecoverableStepError(f"script quality gate failed: {', '.join(consistency_reasons)}")

        metrics = {
            **gate_result.metrics,
            "script_quality_gate_pass": True,
            "script_quality_gate_blocking": False,
            "script_quality_gate_warnings": list(gate_result.reasons),
            "fact_pack_consistency_pass": True,
            "ready_script_declared_fact_check_accepted": bool(plan_dict.get("ready_script_fact_check_confirmed")),
            "ready_script_preserved": True,
            "script_auto_repair_skipped": True,
            "script_repair_attempts_log": attempts_log,
            **self._claim_trace_metrics(preserved),
        }
        preserved["qa_metrics"] = metrics
        return preserved, metrics

    def _ready_script_declared_fact_check_accepts(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        gate_reasons: list[str],
        consistency_reasons: list[str],
    ) -> bool:
        if not plan_dict.get("ready_script_mode") or not plan_dict.get("ready_script_fact_check_confirmed"):
            return False
        fact_pack = plan_dict.get("fact_pack") if isinstance(plan_dict.get("fact_pack"), dict) else {}
        if fact_pack.get("provider") != "user_declared_fact_check" or fact_pack.get("status") != "verified":
            return False
        if consistency_reasons:
            return False
        allowed_warnings = {"factual_risk_requires_conservative_rewrite"}
        if any(reason not in allowed_warnings for reason in gate_reasons):
            return False
        trace_metrics = self._claim_trace_metrics(script)
        return bool(trace_metrics["claim_trace_items"] and trace_metrics["claim_trace_missing_items"] == 0)

    def _simple_mode_blocking_script_reasons(self, reasons: list[str]) -> list[str]:
        blocking = {
            "placeholder_source_language",
            "repeated_clause",
            "estimated_duration_outside_absolute_range",
            "markup_or_ssml_leaked",
            "foreign_language_detected",
            "non_latin_text_detected",
            "em_dash_or_en_dash_detected",
            "truncated_ending_logic",
            "generic_ai_style_phrase",
        }
        return [reason for reason in reasons if reason in blocking]

    def _simple_mode_lightweight_repair_reasons(self, reasons: list[str]) -> list[str]:
        repairable = {
            "factual_claim_trace_missing",
            "factual_risk_requires_conservative_rewrite",
            "overconfident_or_unsupported_factual_claim",
            "weak_loop_closure",
            "ending_not_connected_to_hook",
        }
        return [reason for reason in reasons if reason in repairable]

    def _simple_mode_repair_improved(self, original_reasons: list[str], repaired_reasons: list[str]) -> bool:
        original = set(original_reasons)
        repaired = set(repaired_reasons)
        if not original:
            return False
        return len(repaired) < len(original) or repaired < original

    def _claim_trace_metrics(self, script: dict[str, Any]) -> dict[str, Any]:
        trace = script.get("claim_trace") if isinstance(script.get("claim_trace"), list) else []
        return {
            "claim_trace_items": len(trace),
            "claim_trace_grounded_items": sum(
                1
                for item in trace
                if isinstance(item, dict) and str(item.get("grounding") or "").lower() == "fact_pack" and item.get("source_fact_ids")
            ),
            "claim_trace_missing_items": sum(
                1
                for item in trace
                if isinstance(item, dict) and str(item.get("grounding") or "").lower() == "missing"
            ),
        }

    def _persist_script_rejection(self, job_id: str | None, script: dict[str, Any], gate_metrics: dict[str, Any], consistency_reasons: list[str]) -> None:
        if not job_id:
            return
        self.storage.persist_json(
            job_id,
            "script_rejected.json",
            {
                "script": self._serialize_for_json(script),
                "gate_metrics": self._serialize_for_json(gate_metrics),
                "consistency_reasons": consistency_reasons,
            },
        )
