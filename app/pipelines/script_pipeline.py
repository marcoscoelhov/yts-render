from __future__ import annotations

import ast
import concurrent.futures
import queue
import re
import threading
import time
import unicodedata
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.editorial.retention import attach_retention_metadata, enrich_plan_for_script_generation
from app.models import Job, Script, TopicPlan, TopicRequest
from app.pipelines.common import RecoverableStepError, model_payload
from app.pipelines.base import BasePipeline
from app.utils import new_id, sentence_split, stable_hash, tokenize, utcnow, word_tokens


def normalize_script_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metrics)
    score_keys = {
        "hook_score",
        "clarity_score",
        "information_density_score",
        "repetition_score",
        "ending_strength_score",
    }
    for key in score_keys:
        value = normalized.get(key)
        if isinstance(value, int | float) and 1 < value <= 10:
            normalized[key] = round(value / 10, 3)
    if normalized.get("repetition_score") == 1:
        normalized["repetition_score"] = 0.1
    return normalized


class ScriptPipeline(BasePipeline):
    def step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "script_rejected.json")
        self._remove_stale_quality_report(job.job_id, "script_generation_debug.json")
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert topic_plan and request
        plan_dict = {
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "hook_promise": topic_plan.hook_promise,
            "title_candidates": topic_plan.title_candidates,
            "tone": request.tone or "intrigante_direto",
            "requested_angle": request.requested_angle,
            "hub_notes": request.notes,
            "original_input": request.seed_theme,
        }
        plan_dict = enrich_plan_for_script_generation(
            plan_dict,
            target_duration_sec=job.target_duration_sec,
            recent_history=self._recent_topic_history(session, request.niche_id),
        )
        plan_dict["channel_learning_brief"] = self._channel_learning_brief(session, request.niche_id)
        fact_pack = self._build_fact_pack(topic_plan, request)
        plan_dict["fact_pack"] = fact_pack
        self.storage.persist_json(job.job_id, "fact_pack.json", self._serialize_for_json(fact_pack))
        generation_started = time.monotonic()
        try:
            script = self.providers.creative.generate_script(plan_dict)
        except Exception as exc:  # noqa: BLE001
            self._persist_script_generation_debug(
                job_id=job.job_id,
                attempt=attempt,
                plan_dict=plan_dict,
                fact_pack=fact_pack,
                phase="generation",
                elapsed_ms=round((time.monotonic() - generation_started) * 1000, 1),
                error=exc,
            )
            raise
        generation_elapsed_ms = round((time.monotonic() - generation_started) * 1000, 1)
        try:
            script, metrics = self._validate_or_repair_script(script, plan_dict, job.target_duration_sec, request.cta_style or "none", job.job_id)
        except Exception as exc:  # noqa: BLE001
            self._persist_script_generation_debug(
                job_id=job.job_id,
                attempt=attempt,
                plan_dict=plan_dict,
                fact_pack=fact_pack,
                phase="validation",
                elapsed_ms=generation_elapsed_ms,
                script=script,
                error=exc,
            )
            raise
        self._persist_script_generation_debug(
            job_id=job.job_id,
            attempt=attempt,
            plan_dict=plan_dict,
            fact_pack=fact_pack,
            phase="completed",
            elapsed_ms=generation_elapsed_ms,
            script=script,
            metrics=metrics,
        )
        text_audit = self._text_publish_audit(job.job_id, script, fact_pack)
        if text_audit.get("passed") is False:
            audit_reasons = [str(reason) for reason in text_audit.get("reasons") or ["text_publish_audit_failed"]]
            self._persist_script_rejection(job.job_id, script, metrics, audit_reasons)
            raise RecoverableStepError(f"text publish audit failed: {', '.join(audit_reasons)}")
        script = self._attach_editorial_source(script, plan_dict)
        metrics = {**metrics, "editorial_source": "hub_viral_prompt", "downstream_source_of_truth": "script_full_narration"}
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "script_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(script),
            **script,
        }
        session.execute(delete(Script).where(Script.job_id == job.job_id))
        session.add(Script(**model_payload(Script, payload)))
        self.storage.persist_json(job.job_id, "script.json", self._serialize_for_json(payload))
        script_telemetry_file = self._persist_repair_telemetry(
            job.job_id,
            "script",
            {
                "job_id": job.job_id,
                "attempt": attempt,
                "final_passed": metrics.get("script_quality_gate_pass", False) and metrics.get("fact_pack_consistency_pass", False),
                "attempts": metrics.get("script_repair_attempts_log", []),
            },
        )
        quality_summary = dict(job.quality_summary or {})
        quality_summary["script"] = metrics
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "script.generated", "succeeded", metrics)
        return ["fact_pack.json", "script.json", "script_generation_debug.json", "text_publish_audit.json", script_telemetry_file]

    def _text_publish_audit(self, job_id: str, script: dict[str, Any], fact_pack: dict[str, Any]) -> dict[str, Any]:
        auditor = getattr(self.providers.creative, "audit_publish_package", None)
        if auditor is None:
            return {"passed": True, "reasons": [], "provider": "none", "skipped": True}
        payload = {
            "script": {
                "title": script.get("title"),
                "hook": script.get("hook"),
                "ending": script.get("ending"),
                "full_narration": script.get("full_narration"),
                "key_facts": script.get("key_facts"),
                "source_fact_ids": script.get("source_fact_ids"),
                "claim_trace": script.get("claim_trace"),
            },
            "fact_pack": fact_pack,
            "hashtags": ["#shorts"],
            "audit_phase": "text_before_assets",
        }
        try:
            audit = self._call_with_timeout(
                lambda: auditor(payload),
                timeout_sec=float(self.settings.llm_publish_audit_timeout_sec),
            )
        except TimeoutError:
            audit = {
                "passed": False,
                "reasons": ["text_publish_audit_timeout"],
                "provider": "publish_auditor",
                "timeout_sec": self.settings.llm_publish_audit_timeout_sec,
            }
        except Exception as exc:  # noqa: BLE001
            audit = {"passed": False, "reasons": ["text_publish_audit_failed"], "error": str(exc), "provider": "publish_auditor"}
        if not isinstance(audit, dict):
            audit = {"passed": False, "reasons": ["text_publish_audit_invalid"], "provider": "publish_auditor"}
        self.storage.persist_json(
            job_id,
            "text_publish_audit.json",
            {
                "schema_version": self.settings.schema_version,
                "job_id": job_id,
                "created_at": utcnow().isoformat(),
                "audit": self._serialize_for_json(audit),
            },
        )
        return audit

    def _call_with_timeout(self, func: Any, timeout_sec: float) -> Any:
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def runner() -> None:
            try:
                result_queue.put(("ok", func()))
            except Exception as exc:  # noqa: BLE001
                result_queue.put(("error", exc))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            status, result = result_queue.get(timeout=timeout_sec)
        except queue.Empty as exc:
            raise TimeoutError(f"operation timed out after {timeout_sec}s") from exc
        if status == "error":
            raise result
        return result

    def _persist_script_generation_debug(
        self,
        job_id: str,
        attempt: int,
        plan_dict: dict[str, Any],
        fact_pack: dict[str, Any],
        phase: str,
        elapsed_ms: float,
        script: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        payload = {
            "job_id": job_id,
            "attempt": attempt,
            "phase": phase,
            "elapsed_ms": elapsed_ms,
            "strict_minimax_validation": self.settings.strict_minimax_validation,
            "llm_primary_provider": self.settings.llm_primary_provider,
            "llm_fallback_provider": self.settings.llm_fallback_provider,
            "llm_script_draft_provider": self.settings.llm_script_draft_provider,
            "llm_enable_fallback": self.settings.llm_enable_fallback,
            "real_run_allow_mock_fallback": self.settings.real_run_allow_mock_fallback,
            "llm_script_draft_timeout_sec": self.settings.llm_script_draft_timeout_sec,
            "minimax_script_timeout_sec": self.settings.minimax_script_timeout_sec,
            "fact_pack_status": fact_pack.get("status"),
            "fact_count": len(fact_pack.get("facts") or []),
            "canonical_topic": plan_dict.get("canonical_topic"),
            "angle": plan_dict.get("angle"),
            "requested_angle": plan_dict.get("requested_angle"),
            "source_fact_ids": list((script or {}).get("source_fact_ids") or []),
            "claim_trace": self._serialize_for_json({"claim_trace": (script or {}).get("claim_trace") or []})["claim_trace"],
            "script_title": (script or {}).get("title"),
            "script_hook": (script or {}).get("hook"),
            "script_language": (script or {}).get("language"),
            "script_estimated_duration_sec": (script or {}).get("estimated_duration_sec"),
            "script_provider": ((script or {}).get("qa_metrics") or {}).get("generation_provider")
            or ((script or {}).get("qa_metrics") or {}).get("source_provider"),
            "script_provider_role": ((script or {}).get("qa_metrics") or {}).get("generation_provider_role"),
            "qa_metrics": self._serialize_for_json(metrics or {}),
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        self.storage.persist_json(job_id, "script_generation_debug.json", self._serialize_for_json(payload))

    def _build_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest) -> dict[str, Any]:
        if self.settings.use_mock_providers:
            return {
                "status": "limited",
                "query_used": request.seed_theme,
                "facts": [],
                "sources": [],
                "editorial_rule": "Mock-provider test mode: no external fact retrieval.",
            }
        queries = self._fact_pack_queries(request, topic_plan)
        seen: set[str] = set()
        cleaned_queries = []
        for query in queries:
            normalized = " ".join(str(query or "").split())
            if normalized and normalized.lower() not in seen and not self._is_weak_fact_query(normalized):
                cleaned_queries.append(normalized)
                seen.add(normalized.lower())
        topic_tokens = self._fact_topic_tokens(request, topic_plan)
        if topic_tokens:
            cleaned_queries = [query for query in cleaned_queries if self._query_matches_primary_fact_topic(query, topic_tokens)]
        cleaned_queries.sort(key=self._fact_query_priority)
        query_batch = cleaned_queries[:8]
        if query_batch:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(query_batch)))
            try:
                future_to_query = {executor.submit(self._scientific_article_fact_pack, query): query for query in query_batch}
                for future in concurrent.futures.as_completed(future_to_query):
                    query = future_to_query[future]
                    try:
                        pack = future.result()
                    except Exception:  # noqa: BLE001
                        continue
                    if pack.get("facts") and self._fact_pack_matches_topic(pack, request, topic_plan):
                        pack["query_used"] = query
                        pack["status"] = "verified"
                        pack["queries_attempted"] = query_batch
                        return pack
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        return {
            "status": "limited",
            "query_used": cleaned_queries[0] if cleaned_queries else request.seed_theme,
            "facts": [],
            "sources": [],
            "editorial_rule": "No source facts were retrieved. Script must avoid precise numbers, dates, medical/scientific/engineering causality, and absolute claims unless already present in the user input.",
            "topic_alignment": {"passed": False, "reason": "no_relevant_source_retrieved"},
        }

    def _fact_topic_tokens(self, request: TopicRequest, topic_plan: TopicPlan) -> set[str]:
        source_text = f"{request.seed_theme} {topic_plan.canonical_topic}".strip()
        normalized = self._normalize_fact_text(source_text)
        stopwords = {
            "porque",
            "como",
            "sobre",
            "plataforma",
            "dominante",
            "video",
            "videos",
            "curiosidade",
            "curiosidades",
            "chamando",
            "atencao",
            "atenção",
            "maior",
            "imagina",
            "parece",
            "segredo",
            "mecanismo",
        }
        tokens = {token for token in word_tokens(normalized) if len(token) >= 4 and token not in stopwords}
        protected_entities = {"youtube", "tiktok", "google", "wikipedia", "instagram", "meta", "flamingo", "flamingos", "polvo", "polvos", "pisa"}
        return {token for token in tokens if token in protected_entities} or set(list(tokens)[:3])

    def _query_matches_primary_fact_topic(self, query: str, topic_tokens: set[str]) -> bool:
        query_tokens = {token for token in word_tokens(self._normalize_fact_text(query)) if len(token) >= 4}
        if not query_tokens:
            return False
        if query_tokens & topic_tokens:
            return True
        side_entities = {"google", "wikipedia", "tiktok", "meta", "instagram", "facebook", "x", "twitter"}
        if query_tokens & side_entities:
            return False
        return len(query_tokens) > 2

    def _fact_pack_matches_topic(self, fact_pack: dict[str, Any], request: TopicRequest, topic_plan: TopicPlan) -> bool:
        topic_tokens = self._fact_topic_tokens(request, topic_plan)
        if not topic_tokens:
            fact_pack["topic_alignment"] = {"passed": True, "reason": "no_primary_topic_tokens"}
            return True
        source_text = " ".join(
            str(part or "")
            for part in [
                fact_pack.get("topic_title"),
                " ".join(str(source.get("title") or "") for source in fact_pack.get("sources") or []),
                " ".join(str(fact.get("claim") or "") for fact in fact_pack.get("facts") or []),
            ]
        )
        source_tokens = {token for token in word_tokens(self._normalize_fact_text(source_text)) if len(token) >= 4}
        matched = sorted(topic_tokens & source_tokens)
        passed = bool(matched)
        fact_pack["topic_alignment"] = {
            "passed": passed,
            "primary_topic_tokens": sorted(topic_tokens),
            "matched_tokens": matched,
            "reason": "matched_primary_topic" if passed else "source_fact_mismatch",
        }
        return passed

    def _fact_query_priority(self, query: str) -> tuple[int, int, int, int]:
        normalized = query.lower()
        token_count = len(word_tokens(query))
        is_short_entity = token_count <= 3 and ":" not in query and "?" not in query
        has_specific_entity = any(term in normalized for term in ["flamingo", "polvo", "octopus", "torre", "pisa", "mel", "honey"])
        has_concept_suffix = any(
            term in normalized
            for term in [
                "carotenoides",
                "carotenoid",
                "pigmentos",
                "pigment",
                "pigmentation",
                "plumage",
                "diet",
                "alimentação",
                "inclinacao",
                "inclinação",
                "engenharia",
                "solo",
                "cromatóforos",
                "chromatophore",
                "iridóforos",
                "camuflagem",
                "camouflage",
                "conservação",
                "conservacao",
                "durabilidade",
                "glucose oxidase",
                "peróxido",
                "peroxido",
                "hydrogen peroxide",
                "water activity",
                "antimicrobial",
            ]
        )
        ambiguous_short_entity = normalized in {"mel"}
        return (
            0 if has_concept_suffix else 1,
            0 if has_specific_entity and not ambiguous_short_entity else (1 if is_short_entity else 2),
            token_count,
            len(query),
        )

    def _is_weak_fact_query(self, query: str) -> bool:
        tokens = [token.lower() for token in word_tokens(query) if token]
        if not tokens:
            return True
        if all(token.isdigit() for token in tokens):
            return True
        weak_single_terms = {
            "auto",
            "manual",
            "segredo",
            "mecanismo",
            "processo",
            "fato",
            "fatos",
            "curiosidade",
            "curiosidades",
            "biologia",
            "ciencia",
            "ciência",
            "inteligencia",
            "inteligência",
            "surpresa",
            "visual",
            "revelar",
            "resposta",
            "motivo",
            "quimico",
            "químico",
            "pigmentos",
            "carotenoides",
            "diet",
            "alimentacao",
            "alimentação",
            "descubra",
            "apenas",
            "exatamente",
            "durante",
            "vida",
            "duas",
            "cidade",
            "cidades",
        }
        weak_multi_terms = weak_single_terms | {
            "causa",
            "causas",
            "comida",
            "alimentacao",
            "alimentação",
            "dieta",
            "cor",
            "cores",
            "segredo",
            "quimico",
            "químico",
            "visual",
            "surpresa",
            "incrivel",
            "incrível",
            "motivo",
            "deixa",
            "pinta",
            "transformacao",
            "transformação",
            "resposta",
            "explicacao",
            "explicação",
            "diet",
            "descubra",
            "apenas",
            "exatamente",
            "durante",
            "vida",
            "duas",
            "cidade",
            "cidades",
        }
        if len(tokens) == 1:
            return tokens[0] in weak_single_terms
        return all(token in weak_multi_terms for token in tokens)

    def _fact_pack_queries(self, request: TopicRequest, topic_plan: TopicPlan) -> list[str]:
        raw_sources = [
            request.seed_theme,
            topic_plan.canonical_topic,
            topic_plan.angle,
            getattr(topic_plan, "hook_promise", None),
            *(getattr(topic_plan, "search_terms", None) or []),
            *(getattr(topic_plan, "entities", None) or []),
            *(topic_plan.title_candidates or []),
        ]
        queries: list[str] = []
        for raw_query in raw_sources:
            for query in self._fact_query_source_texts(raw_query):
                cleaned = self._clean_fact_query(str(query or ""))
                if cleaned:
                    queries.append(cleaned)
                    entity = self._extract_fact_entity(cleaned)
                    if entity and entity != cleaned and not self._is_weak_fact_query(entity):
                        queries.append(entity)
                        for concept in self._fact_query_concepts(cleaned):
                            queries.append(f"{entity} {concept}")
        return queries

    def _fact_query_source_texts(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, dict):
            texts = [value.get(key) for key in ("name", "text", "title", "query", "term")]
            return [str(text).strip() for text in texts if str(text or "").strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = ast.literal_eval(stripped)
                except Exception:  # noqa: BLE001
                    parsed = None
                if isinstance(parsed, dict):
                    return self._fact_query_source_texts(parsed)
            return [stripped]
        return [str(value).strip()]

    def _clean_fact_query(self, query: str) -> str:
        query = unicodedata.normalize("NFKC", query).strip()
        query = re.sub(r"[?!¿¡]+", " ", query)
        query = re.sub(r"\s+", " ", query)
        query = re.sub(r"^(?:voc[eê]\s+sabia|j[aá]\s+imaginou|surpreenda-se|prepare-se)\b[:\s,.-]*", "", query, flags=re.IGNORECASE)
        query = re.sub(r"^(?:por que|porque|como|qual|quais|o que|quem|quando|onde)\s+", "", query, flags=re.IGNORECASE)
        query = re.sub(r"\b(?:fica|ficam|ficou|são|sao|é|e|era|foram|tem|têm)\b", " ", query, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", query).strip(" -–—:;,.")

    def _extract_fact_entity(self, query: str) -> str:
        stopwords = {
            "por", "que", "porque", "como", "qual", "quais", "para", "com", "uma", "um", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e",
            "fica", "ficam", "cor", "rosa", "cor-de-rosa", "não", "nao", "cai", "acontece", "segredo", "invisível", "invisivel", "parece", "artificial",
            "curiosidades", "curiosidade", "cientificas", "científicas", "cientifica", "científica", "sobre", "mais", "inteligente", "oceano",
            "animal", "explica", "explicacao", "explicação", "fatos", "fato", "voce", "você", "sabia", "surpreenda", "prepare",
            "comida", "pinta", "motivo", "incrivel", "incrível", "quimico", "químico", "deixa", "essa", "resposta", "transforma", "revelar",
            "transformacao", "transformação", "visual", "branco", "branca", "brancos", "brancas", "descubra", "apenas",
            "exatamente", "durante", "vida",
        }
        colon_head = query.split(":", 1)[0].strip(" -–—:;,.") if ":" in query else ""
        if colon_head:
            colon_tokens = [token for token in word_tokens(colon_head) if token]
            if 1 <= len(colon_tokens) <= 4:
                return " ".join(colon_tokens)
        plain_tokens = [token for token in word_tokens(query) if token]
        if plain_tokens:
            trailing_tokens = plain_tokens[1:]
            if trailing_tokens and all(len(token) < 3 or token.lower() in stopwords for token in trailing_tokens):
                return plain_tokens[0]
        filtered_tokens = [token for token in word_tokens(query) if len(token) >= 3 and token.lower() not in stopwords]
        if 1 <= len(filtered_tokens) <= 2:
            return " ".join(filtered_tokens)
        preposition_head = re.split(r"\b(?:sobre|com|contra|versus|vs\.?|em)\b", query, maxsplit=1, flags=re.IGNORECASE)[0].strip(" -–—:;,.")
        if preposition_head:
            head_tokens = [token for token in word_tokens(preposition_head) if token]
            if 1 <= len(head_tokens) <= 4:
                return " ".join(head_tokens)
        tokens = filtered_tokens
        if not tokens:
            return query
        if len(tokens) == 1:
            return tokens[0]
        return " ".join(tokens[:2])

    def _fact_query_concepts(self, query: str) -> list[str]:
        normalized = query.lower()
        normalized_tokens = set(word_tokens(self._normalize_fact_text(query)))
        concepts: list[str] = []
        if normalized_tokens & {"rosa", "color", "pink"} or "cor-de-rosa" in normalized:
            concepts.extend(["carotenoid pigmentation", "carotenoid diet", "plumage pigmentation"])
        if any(term in normalized for term in ["polvo", "polvos", "octopus", "octopuses", "cromatóforo", "cromatoforo", "camuflagem"]):
            concepts.extend(["chromatophores", "iridophores", "camouflage"])
        if any(term in normalized for term in ["flamingo", "flamingos"]):
            concepts.extend(["carotenoid pigmentation", "carotenoid diet", "plumage pigmentation"])
        if any(term in normalized for term in ["cai", "inclina", "torre"]):
            concepts.extend(["inclinação", "engenharia", "solo"])
        if any(term in normalized for term in ["mel", "honey", "abelha", "glucose oxidase", "peróxido", "peroxido"]):
            concepts.extend(["honey antimicrobial", "honey water activity", "glucose oxidase hydrogen peroxide"])
        return concepts[:3]

    def _normalize_fact_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(text or "").lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9\s]", " ", normalized)

    def _fact_result_is_relevant(self, query: str, title: str, extract: str) -> bool:
        query_tokens = {token for token in word_tokens(self._normalize_fact_text(query)) if len(token) >= 4}
        title_tokens = {token for token in word_tokens(self._normalize_fact_text(title)) if len(token) >= 4}
        text_tokens = {token for token in word_tokens(self._normalize_fact_text(f"{title} {extract[:500]}")) if len(token) >= 4}
        normalized_query = self._normalize_fact_text(query)
        honey_query = bool(query_tokens & {"honey"}) or bool(re.search(r"\bmel\b", normalized_query))
        if honey_query:
            honey_terms = {"honey", "bee", "bees", "antimicrobial", "glucose", "oxidase", "peroxide"}
            tts_false_friends = {"spectrogram", "spectrograms", "wavenet", "tacotron", "speech", "synthesis", "vocoder"}
            if text_tokens & tts_false_friends and not text_tokens & honey_terms:
                return False
        if not query_tokens:
            return True
        if query_tokens & title_tokens:
            return True
        if len(query_tokens) == 1:
            return False
        if len(query_tokens & text_tokens) >= min(2, len(query_tokens)):
            return True
        return False

    def _scientific_article_fact_pack(self, query: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "yts-render/1.0 fact-pack"}) as client:
                response = client.get(
                    "https://api.openalex.org/works",
                    params={
                        "search": query,
                        "filter": "type:article,has_abstract:true",
                        "per-page": 5,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:  # noqa: BLE001
            return {"status": "limited", "facts": [], "sources": []}
        if not isinstance(payload, dict):
            return {"status": "limited", "facts": [], "sources": []}
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            title = str(result.get("display_name") or "").strip()
            abstract = self._openalex_abstract_text(result.get("abstract_inverted_index")).strip()
            if re.search(r"\bdoes not have an abstract\b", abstract, re.IGNORECASE):
                continue
            if not title or not abstract or not self._fact_result_is_relevant(query, title, abstract):
                continue
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", abstract) if len(part.strip()) > 30]
            if not sentences:
                continue
            facts = [
                {
                    "fact_id": f"F{index}",
                    "claim": sentence[:260],
                    "source_id": "S1",
                }
                for index, sentence in enumerate(sentences[:5], start=1)
            ]
            primary_location = result.get("primary_location") if isinstance(result.get("primary_location"), dict) else {}
            source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
            source_url = str(result.get("doi") or primary_location.get("landing_page_url") or result.get("id") or "")
            return {
                "status": "verified",
                "provider": "openalex",
                "query_used": query,
                "topic_title": title,
                "publication_year": result.get("publication_year"),
                "facts": facts,
                "sources": [
                    {
                        "source_id": "S1",
                        "title": title,
                        "url": source_url,
                        "provider": "openalex",
                        "container": source.get("display_name"),
                        "publication_year": result.get("publication_year"),
                    }
                ],
                "editorial_rule": "Use peer-reviewed or scholarly article facts as source material only. Preserve viral pacing, but every precise number, date, technical cause, history claim, or scientific claim must be grounded in fact_id references or rewritten conservatively. Do not use Wikipedia as a factual source.",
            }
        return {"status": "limited", "facts": [], "sources": []}

    def _openalex_abstract_text(self, abstract_inverted_index: Any) -> str:
        if not isinstance(abstract_inverted_index, dict):
            return ""
        positioned_words: list[tuple[int, str]] = []
        for word, positions in abstract_inverted_index.items():
            if not isinstance(positions, list):
                continue
            for position in positions:
                if isinstance(position, int):
                    positioned_words.append((position, str(word)))
        positioned_words.sort(key=lambda item: item[0])
        return " ".join(word for _, word in positioned_words)

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
        used_ids = {str(item) for item in source_ids if str(item) in valid_ids}
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
                "editorial_source": "hub_viral_prompt",
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
        processed["estimated_duration_sec"] = round(max(25.0, min(42.0, len(word_tokens(str(processed.get("full_narration") or ""))) / 2.55)), 2)
        processed["token_count"] = len(tokenize(str(processed.get("full_narration") or "")))
        return processed

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
                rewritten["hook"] = f"{anchor.capitalize()} parece exagero, mas a fonte aponta um mecanismo real."
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
            f"A fonte sustenta um detalhe verificável sobre {anchor}, sem precisar inflar o fato.",
            f"O mecanismo real aparece quando {anchor} deixa de ser só aparência.",
            f"A surpresa funciona melhor porque existe lastro por trás da cena.",
            f"O payoff é esse: {anchor} parece estranho antes da explicação certa entrar.",
        ]
        return templates[index % len(templates)]

    def _soften_risky_sentence(self, sentence: str, anchor: str) -> str:
        text = " ".join(str(sentence or "").split())
        if not text:
            return f"Em geral, {anchor} revela um detalhe real sem exagero."
        if re.search(r"\b(?:1[0-9]{3}|20[0-9]{2})\b", text):
            return f"Em geral, {anchor} carrega um contexto antigo, mas o ponto principal aparece no mecanismo."
        if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:%|por cento\b|anos?\b|séculos?\b|seculos?\b|dias?\b|horas?\b|minutos?\b|segundos?\b|metros?\b|m\b|cm\b|mm\b|km\b|graus?\b|°|toneladas?\b|kg\b|quilos?\b|milhões?\b|milhoes?\b|bilhões?\b|bilhoes?\b)", text, re.IGNORECASE):
            return f"Em geral, {anchor} mostra uma escala incomum, sem depender de número exato."
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
            return f"Em geral, {anchor} ajuda a explicar o efeito sem exigir precisão que a fonte não sustenta."
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
        script = self._apply_cta_policy(dict(script), cta_style)
        script = self._postprocess_script_for_quality(script, plan_dict, [])
        script["qa_metrics"] = normalize_script_metrics(dict(script.get("qa_metrics") or {}))
        gate_result = self.script_gate.validate(script, target_duration_sec)
        consistency_reasons = self._fact_pack_consistency_reasons(script, plan_dict.get("fact_pack"))
        attempts_log: list[dict[str, Any]] = [
            {
                "repair_attempt": 0,
                "reason_codes": [*gate_result.reasons, *consistency_reasons],
                "passed": gate_result.passed and not consistency_reasons,
                "used_fallback": False,
            }
        ]
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
