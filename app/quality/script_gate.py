from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils import avg_words_per_sentence, max_words_single_sentence, word_tokens

LOOP_STOPWORDS = {
    "a",
    "as",
    "o",
    "os",
    "de",
    "da",
    "do",
    "das",
    "dos",
    "e",
    "em",
    "um",
    "uma",
    "uns",
    "umas",
    "no",
    "na",
    "nos",
    "nas",
    "para",
    "por",
    "com",
    "sem",
    "que",
    "isso",
    "esse",
    "essa",
    "essas",
    "esses",
    "como",
    "mais",
    "menos",
    "muito",
    "muita",
    "muitas",
    "muitos",
    "quando",
    "entao",
    "então",
    "sobre",
    "entre",
    "vira",
    "fica",
    "fim",
}


ALLOWED_NON_PT_TERMS = {
    "gps",
    "usgs",
    "chaiten",
    "eyjafjallajokull",
    "einstein",
    "youtube",
    "shorts",
}

FOREIGN_LANGUAGE_MARKERS = {
    "right",
    "giving",
    "your",
    "cat",
    "second",
    "chance",
    "see",
    "heard",
    "mini-cerebro independiente",
    "independiente",
}

MARKUP_PATTERN = re.compile(r"</?[a-zA-Z][^>\s]*(?:\s[^>]*)?>?|&(?:lt|gt|amp|quot|apos);")
SUSPICIOUS_GLUED_PATTERN = re.compile(
    r"\b(?:um|uma|o|a|os|as|de|do|da|dos|das|no|na|nos|nas|e|que)(?:mini|micro|macro|super|ultra)[a-záàãâéêíóõôúç-]*\b",
    re.IGNORECASE,
)
GENERIC_HOOK_OPENING_PATTERN = re.compile(
    r"^\s*(?:você\s+sabia|voce\s+sabia|já\s+imaginou|ja\s+imaginou|nesse\s+v[ií]deo|neste\s+v[ií]deo)\b",
    re.IGNORECASE,
)

OVERCONFIDENT_FACT_MARKERS = {
    "a torre está garantida",
    "a torre esta garantida",
    "a física prova",
    "a fisica prova",
    "domina a física",
    "domina a fisica",
    "desafia a física",
    "desafia a fisica",
    "ridiculamente simples",
}

SUSPICIOUS_PRECISION_PATTERN = re.compile(
    r"\b(?:apenas|s[oó]|somente)\s+\d+(?:[,.]\d+)?\s*(?:centímetros|centimetros|cm|milímetros|milimetros|mm)\b",
    re.IGNORECASE,
)

PISA_UNSUPPORTED_CLAIM_MARKERS = {
    "sapatas de concreto",
    "plano americano de congelar o solo",
    "congelar o solo foi recusado",
    "a inclinação a sustenta",
    "a inclinacao a sustenta",
}


FACT_NUMBER_PATTERN = re.compile(
    r"\b\d+(?:[,.]\d+)?\s*(?:%|por cento\b|anos?\b|séculos?\b|seculos?\b|dias?\b|horas?\b|minutos?\b|segundos?\b|metros?\b|m\b|cm\b|mm\b|km\b|graus?\b|°|toneladas?\b|kg\b|quilos?\b|milhões?\b|milhoes?\b|bilhões?\b|bilhoes?\b)",
    re.IGNORECASE,
)
FACT_YEAR_PATTERN = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")
FACT_CAUSAL_PATTERN = re.compile(
    r"\b(?:porque|por isso|graças a|gracas a|causa|causou|criou|criam|impede|impediu|permite|permitiu|faz com que|resultado de|segredo|solução|solucao|explica|provoca|reduz|aumenta|corrige|corrigiu)\b",
    re.IGNORECASE,
)
FACT_CONSERVATIVE_PATTERN = re.compile(
    r"\b(?:cerca de|aproximadamente|estimad[oa]|em geral|tende a|pode|podem|provavelmente|uma das explicações|uma das explicacoes|segundo|de acordo com)\b",
    re.IGNORECASE,
)
FACT_DOMAIN_PATTERN = re.compile(
    r"\b(?:cérebro|cerebro|neur[oô]nios?|dopamina|sangue|coração|coracao|dna|gene|bactéria|bacteria|vírus|virus|célula|celula|hormônio|hormonio|cura|doença|doenca|sono|memória|memoria|gravidade|física|fisica|solo|argila|fundação|fundacao|engenharia|terremoto|vulc[aã]o|planeta|estrela|buraco negro|oceano|espécie|especie|evolução|evolucao|temperatura|pressão|pressao|energia|radiação|radiacao)\b",
    re.IGNORECASE,
)
FACT_ABSOLUTE_PATTERN = re.compile(
    r"\b(?:sempre|nunca|impossível|impossivel|garante|garantida|prova|comprova|domina|desafia|único|unico|todos|nenhum|exatamente|sem exceção|sem excecao)\b",
    re.IGNORECASE,
)
FACT_HEALTH_PATTERN = re.compile(
    r"\b(?:cura|previne|trata|elimina|reduz o risco|causa câncer|causa cancer|emagrece|aumenta testosterona|dopamina|cortisol|insulina)\b",
    re.IGNORECASE,
)
FACT_HISTORY_TECH_PATTERN = re.compile(
    r"\b(?:construíd[oa]|construid[oa]|descobriram|inventaram|engenheiros|cientistas|pesquisadores|estudo|experimento|missão|missao|projeto|técnica|tecnica)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScriptGateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class ScriptQualityGate:
    def validate(self, script: dict[str, Any], target_duration_sec: int) -> ScriptGateResult:
        reasons: list[str] = []
        full_narration = str(script.get("full_narration") or "")
        title = str(script.get("title") or "")
        text_fields = self._collect_text(script)
        combined_text = "\n".join(text_fields)

        if not full_narration.strip():
            reasons.append("missing_full_narration")
        if str(script.get("language") or "").lower() not in {"pt-br", "pt_br", "portuguese-br"}:
            reasons.append("language_field_not_pt_br")
        if MARKUP_PATTERN.search(combined_text):
            reasons.append("markup_or_ssml_leaked")
        if self._contains_foreign_language(combined_text):
            reasons.append("foreign_language_detected")
        if SUSPICIOUS_GLUED_PATTERN.search(combined_text.replace("-", "")):
            reasons.append("suspicious_glued_words")
        if GENERIC_HOOK_OPENING_PATTERN.search(str(script.get("hook") or "")) or GENERIC_HOOK_OPENING_PATTERN.search(full_narration):
            reasons.append("generic_hook_opening")
        fact_risk = self._fact_risk_report(script)
        if self._has_overconfident_or_unsupported_factual_claims(combined_text):
            reasons.append("overconfident_or_unsupported_factual_claim")
        if fact_risk["blocked"]:
            reasons.append("factual_risk_requires_conservative_rewrite")
        loop_metrics = self._loop_report(script)
        if not loop_metrics["connected_to_opening"]:
            reasons.append("ending_not_connected_to_hook")
        elif loop_metrics["closure_strength"] < 0.25:
            reasons.append("weak_loop_closure")

        word_count = len(word_tokens(full_narration))
        estimated_duration = float(script.get("estimated_duration_sec") or max(0, word_count / 2.55))
        avg_sentence = avg_words_per_sentence(full_narration)
        max_sentence = max_words_single_sentence(full_narration)
        words_per_second = round(word_count / estimated_duration, 2) if estimated_duration else 0.0
        target_min = max(24.5, target_duration_sec - 10)
        target_max = min(46.5, target_duration_sec + 10)

        if not 25 <= estimated_duration <= 45:
            reasons.append("estimated_duration_outside_absolute_range")
        if not target_min <= estimated_duration <= target_max:
            reasons.append("estimated_duration_outside_target_window")
        if avg_sentence > 14:
            reasons.append("avg_sentence_too_long")
        if max_sentence > 20:
            reasons.append("sentence_too_long")
        if not title.strip():
            reasons.append("missing_title")

        qa_metrics = dict(script.get("qa_metrics") or {})
        numeric_checks = {
            "hook_score": (0.80, None),
            "clarity_score": (0.75, None),
            "information_density_score": (0.75, None),
            "ending_strength_score": (0.75, None),
            "repetition_score": (None, 0.88),
        }
        for key, (minimum, maximum) in numeric_checks.items():
            value = qa_metrics.get(key)
            if not isinstance(value, int | float):
                reasons.append(f"missing_{key}")
                continue
            if minimum is not None and value < minimum:
                reasons.append(f"{key}_below_threshold")
            if maximum is not None and value >= maximum:
                reasons.append(f"{key}_above_threshold")

        metrics = {
            **qa_metrics,
            "word_count": word_count,
            "estimated_duration_sec": estimated_duration,
            "avg_words_per_sentence": round(avg_sentence, 2),
            "max_words_single_sentence": max_sentence,
            "words_per_second": words_per_second,
            "target_duration_sec": target_duration_sec,
            "script_quality_gate_pass": not reasons,
            "script_quality_gate_reasons": reasons,
            "fact_risk": fact_risk,
            "loop_gate": loop_metrics,
        }
        return ScriptGateResult(passed=not reasons, reasons=reasons, metrics=metrics)

    def _collect_text(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            texts: list[str] = []
            for item in value:
                texts.extend(self._collect_text(item))
            return texts
        if isinstance(value, dict):
            texts = []
            for key, item in value.items():
                if key in {"image_prompt", "prompt_snapshot"}:
                    continue
                texts.extend(self._collect_text(item))
            return texts
        return []

    def _contains_foreign_language(self, text: str) -> bool:
        normalized = self._normalize(text)
        tokens = set(re.findall(r"\b[a-z]{2,}\b", normalized))
        tokens -= ALLOWED_NON_PT_TERMS
        if tokens & FOREIGN_LANGUAGE_MARKERS:
            return True
        for phrase in FOREIGN_LANGUAGE_MARKERS:
            if " " in phrase and phrase in normalized:
                return True
        return False

    def _fact_risk_report(self, script: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            str(part or "")
            for part in [
                script.get("hook"),
                script.get("full_narration"),
                " ".join(str(item) for item in script.get("key_facts") or []),
            ]
        )
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        claims: list[dict[str, Any]] = []
        total_score = 0
        for sentence in sentences:
            risk_types: list[str] = []
            normalized = self._normalize(sentence)
            conservative = bool(FACT_CONSERVATIVE_PATTERN.search(sentence))
            has_domain = bool(FACT_DOMAIN_PATTERN.search(sentence))
            if FACT_NUMBER_PATTERN.search(sentence):
                risk_types.append("precise_number_or_unit")
            if FACT_YEAR_PATTERN.search(sentence) and FACT_HISTORY_TECH_PATTERN.search(sentence):
                risk_types.append("dated_history_or_technical_event")
            if FACT_CAUSAL_PATTERN.search(sentence) and has_domain:
                risk_types.append("technical_causal_claim")
            if FACT_ABSOLUTE_PATTERN.search(sentence) and (has_domain or len(word_tokens(sentence)) >= 5):
                risk_types.append("absolute_claim")
            if FACT_HEALTH_PATTERN.search(sentence):
                risk_types.append("health_or_biology_claim")
            if "pisa" in normalized and any(marker in normalized for marker in {self._normalize(item) for item in PISA_UNSUPPORTED_CLAIM_MARKERS}):
                risk_types.append("known_topic_unsupported_claim")
            if not risk_types:
                continue
            score = len(risk_types)
            if conservative:
                score = max(0, score - 1)
            total_score += score
            claims.append(
                {
                    "text": sentence[:220],
                    "risk_types": risk_types,
                    "conservative_language": conservative,
                    "score": score,
                }
            )
        high_risk_claims = [claim for claim in claims if claim["score"] >= 2]
        blocked = (
            (bool(high_risk_claims) and total_score >= 3)
            or (total_score >= 3 and len(claims) >= 2)
            or any("known_topic_unsupported_claim" in claim["risk_types"] for claim in claims)
        )
        return {
            "score": total_score,
            "blocked": blocked,
            "claim_count": len(claims),
            "high_risk_claim_count": len(high_risk_claims),
            "claims": claims[:8],
        }

    def _loop_report(self, script: dict[str, Any]) -> dict[str, Any]:
        hook = str(script.get("hook") or "")
        ending = str(script.get("ending") or "")
        title = str(script.get("title") or "")
        full_narration = str(script.get("full_narration") or "")
        opening = hook or (full_narration.split(".", 1)[0] if full_narration else "")
        opening_tokens = self._salient_tokens(opening)
        ending_tokens = self._salient_tokens(ending)
        title_tokens = self._salient_tokens(title)
        shared_opening = sorted(opening_tokens & ending_tokens)
        shared_title = sorted(title_tokens & ending_tokens)
        opening_overlap = len(shared_opening) / max(len(opening_tokens), 1)
        title_overlap = len(shared_title) / max(len(title_tokens), 1)
        closure_strength = round(max(opening_overlap, title_overlap), 3)
        return {
            "connected_to_opening": bool(shared_opening or shared_title),
            "closure_strength": closure_strength,
            "shared_opening_tokens": shared_opening[:6],
            "shared_title_tokens": shared_title[:6],
            "opening_salient_token_count": len(opening_tokens),
            "ending_salient_token_count": len(ending_tokens),
        }

    def _salient_tokens(self, text: str) -> set[str]:
        return {
            token
            for token in word_tokens(text)
            if len(token) >= 4 and token not in LOOP_STOPWORDS
        }

    def _has_overconfident_or_unsupported_factual_claims(self, text: str) -> bool:
        normalized = self._normalize(text)
        if any(marker in normalized for marker in {self._normalize(item) for item in OVERCONFIDENT_FACT_MARKERS}):
            return True
        if SUSPICIOUS_PRECISION_PATTERN.search(text):
            return True
        if "pisa" in normalized and any(marker in normalized for marker in {self._normalize(item) for item in PISA_UNSUPPORTED_CLAIM_MARKERS}):
            return True
        return False

    def _normalize(self, text: str) -> str:
        text = text.lower()
        replacements = {
            "á": "a",
            "à": "a",
            "ã": "a",
            "â": "a",
            "é": "e",
            "ê": "e",
            "í": "i",
            "ó": "o",
            "õ": "o",
            "ô": "o",
            "ú": "u",
            "ç": "c",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text
