from __future__ import annotations

import time
from typing import Any
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.editorial.retention import enrich_plan_for_script_generation
from app.manual_script import extract_ready_script_from_notes
from app.models import Job, Script, TopicPlan, TopicRequest
from app.pipelines.common import FatalStepError, RecoverableStepError, model_payload
from app.pipelines.base import BasePipeline
from app.pipelines.script_audit import ScriptAuditDomain
from app.pipelines.script_fact_pack import ScriptFactPackDomain
from app.pipelines.script_metrics import normalize_script_metrics
from app.pipelines.script_repair import ScriptRepairDomain
from app.utils import new_id, stable_hash, utcnow


class ScriptPipeline(BasePipeline):
    def step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
        step_started = time.monotonic()
        stage_timings_ms: dict[str, float] = {}
        self._remove_stale_quality_report(job.job_id, "script_rejected.json")
        self._remove_stale_quality_report(job.job_id, "script_generation_debug.json")
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert topic_plan and request
        editorial_mode = self._editorial_mode(topic_plan, request)
        simple_mode_fact_skip = self.settings.simple_shorts_mode and editorial_mode == "viral_curiosidades"
        research_brief = self._build_research_brief(topic_plan, request)
        plan_dict = {
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "hook_promise": topic_plan.hook_promise,
            "title_candidates": topic_plan.title_candidates,
            "tone": request.tone or "intrigante_direto",
            "requested_angle": request.requested_angle,
            "hub_notes": request.notes,
            "original_input": request.seed_theme,
            "simple_shorts_mode": simple_mode_fact_skip,
            "editorial_mode": editorial_mode,
            "research_brief": research_brief,
        }
        plan_dict = enrich_plan_for_script_generation(
            plan_dict,
            target_duration_sec=job.target_duration_sec,
            recent_history=self._recent_topic_history(session, request.niche_id),
        )
        plan_dict["channel_learning_brief"] = self._channel_learning_brief(session, request.niche_id)
        ready_script = extract_ready_script_from_notes(request.notes)
        if ready_script is not None:
            plan_dict["ready_script_mode"] = True
            plan_dict["ready_script_fact_check_confirmed"] = ready_script.fact_check_confirmed
            self.storage.persist_json(
                job.job_id,
                "ready_script_input.json",
                {
                    "schema_version": self.settings.schema_version,
                    "job_id": job.job_id,
                    "created_at": utcnow().isoformat(),
                    "fact_check_confirmed": ready_script.fact_check_confirmed,
                    "raw_text": ready_script.raw_text,
                    "hashtags": ready_script.hashtags,
                },
            )
        fact_started = time.monotonic()
        if ready_script is not None:
            fact_pack = ready_script.fact_pack
        else:
            fact_pack = self._simple_mode_fact_pack(request) if simple_mode_fact_skip else self._build_fact_pack(topic_plan, request, research_brief)
        stage_timings_ms["fact_pack_ms"] = round((time.monotonic() - fact_started) * 1000, 1)
        plan_dict["fact_pack"] = fact_pack
        self.storage.persist_json(job.job_id, "fact_pack.json", self._serialize_for_json(fact_pack))
        if self._requires_verified_fact_pack(topic_plan, request, fact_pack):
            error = FatalStepError("script quality gate failed: fact_pack_missing_for_factual_topic")
            self._persist_script_generation_debug(
                job_id=job.job_id,
                attempt=attempt,
                plan_dict=plan_dict,
                fact_pack=fact_pack,
                phase="fact_pack_failed",
                elapsed_ms=0.0,
                stage_timings_ms={
                    **stage_timings_ms,
                    "total_step_ms": round((time.monotonic() - step_started) * 1000, 1),
                },
                error=error,
            )
            raise error
        generation_started = time.monotonic()
        if ready_script is not None:
            script = ready_script.script
            generation_elapsed_ms = 0.0
        else:
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
                    stage_timings_ms={
                        **stage_timings_ms,
                        "generation_ms": round((time.monotonic() - generation_started) * 1000, 1),
                        "total_step_ms": round((time.monotonic() - step_started) * 1000, 1),
                    },
                    error=exc,
                )
                raise
            generation_elapsed_ms = round((time.monotonic() - generation_started) * 1000, 1)
        stage_timings_ms["generation_ms"] = generation_elapsed_ms
        validation_started = time.monotonic()
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
                stage_timings_ms={
                    **stage_timings_ms,
                    "validation_ms": round((time.monotonic() - validation_started) * 1000, 1),
                    "total_step_ms": round((time.monotonic() - step_started) * 1000, 1),
                },
                error=exc,
            )
            raise
        stage_timings_ms["validation_ms"] = round((time.monotonic() - validation_started) * 1000, 1)
        audit_started = time.monotonic()
        text_audit = self._text_publish_audit(job.job_id, script, fact_pack)
        stage_timings_ms["text_publish_audit_ms"] = round((time.monotonic() - audit_started) * 1000, 1)
        if text_audit.get("passed") is False:
            audit_reasons = [str(reason) for reason in text_audit.get("reasons") or ["text_publish_audit_failed"]]
            self._persist_script_generation_debug(
                job_id=job.job_id,
                attempt=attempt,
                plan_dict=plan_dict,
                fact_pack=fact_pack,
                phase="audit_failed",
                elapsed_ms=generation_elapsed_ms,
                script=script,
                metrics={**metrics, "text_publish_audit": text_audit},
                stage_timings_ms={
                    **stage_timings_ms,
                    "total_step_ms": round((time.monotonic() - step_started) * 1000, 1),
                },
            )
            self._persist_script_rejection(job.job_id, script, metrics, audit_reasons)
            raise RecoverableStepError(f"text publish audit failed: {', '.join(audit_reasons)}")
        self._persist_script_generation_debug(
            job_id=job.job_id,
            attempt=attempt,
            plan_dict=plan_dict,
            fact_pack=fact_pack,
            phase="completed",
            elapsed_ms=generation_elapsed_ms,
            script=script,
            metrics=metrics,
            stage_timings_ms={
                **stage_timings_ms,
                "total_step_ms": round((time.monotonic() - step_started) * 1000, 1),
            },
        )
        script = self._attach_editorial_source(script, plan_dict)
        editorial_source = "ready_script" if ready_script is not None else "hub_viral_prompt"
        metrics = {**metrics, "editorial_source": editorial_source, "downstream_source_of_truth": "script_full_narration"}
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
        artifacts = ["fact_pack.json", "script.json", "script_generation_debug.json", "text_publish_audit.json", script_telemetry_file]
        if ready_script is not None:
            artifacts.append("ready_script_input.json")
        return artifacts

    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.fact_pack_domain = ScriptFactPackDomain(self)
        self.audit_domain = ScriptAuditDomain(self)
        self.repair_domain = ScriptRepairDomain(self)

    def _requires_verified_fact_pack(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._requires_verified_fact_pack(*args, **kwargs)

    def _editorial_mode(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._editorial_mode(*args, **kwargs)

    def _topic_requires_verified_fact_pack(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._topic_requires_verified_fact_pack(*args, **kwargs)

    def _simple_mode_fact_pack(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._simple_mode_fact_pack(*args, **kwargs)

    def _build_research_brief(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._build_research_brief(*args, **kwargs)

    def _build_fact_pack(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._build_fact_pack(*args, **kwargs)

    def _query_supports_research_brief(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._query_supports_research_brief(*args, **kwargs)

    def _fact_topic_tokens(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_topic_tokens(*args, **kwargs)

    def _query_matches_primary_fact_topic(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._query_matches_primary_fact_topic(*args, **kwargs)

    def _fact_pack_matches_topic(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_pack_matches_topic(*args, **kwargs)

    def _fact_query_priority(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_query_priority(*args, **kwargs)

    def _is_weak_fact_query(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._is_weak_fact_query(*args, **kwargs)

    def _fact_pack_queries(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_pack_queries(*args, **kwargs)

    def _should_include_standalone_fact_concept(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._should_include_standalone_fact_concept(*args, **kwargs)

    def _fact_query_source_texts(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_query_source_texts(*args, **kwargs)

    def _clean_fact_query(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._clean_fact_query(*args, **kwargs)

    def _extract_fact_entity(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._extract_fact_entity(*args, **kwargs)

    def _fact_query_concepts(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_query_concepts(*args, **kwargs)

    def _normalize_fact_text(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._normalize_fact_text(*args, **kwargs)

    def _fact_result_is_relevant(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_result_is_relevant(*args, **kwargs)

    def _fact_sentence_is_useful(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._fact_sentence_is_useful(*args, **kwargs)

    def _scientific_article_fact_pack(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._scientific_article_fact_pack(*args, **kwargs)

    def _openalex_abstract_text(self, *args: Any, **kwargs: Any) -> Any:
        return self.fact_pack_domain._openalex_abstract_text(*args, **kwargs)

    def _text_publish_audit(self, *args: Any, **kwargs: Any) -> Any:
        return self.audit_domain._text_publish_audit(*args, **kwargs)

    def _normalize_text_publish_audit(self, *args: Any, **kwargs: Any) -> Any:
        return self.audit_domain._normalize_text_publish_audit(*args, **kwargs)

    def _call_with_timeout(self, *args: Any, **kwargs: Any) -> Any:
        return self.audit_domain._call_with_timeout(*args, **kwargs)

    def _fact_pack_consistency_reasons(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._fact_pack_consistency_reasons(*args, **kwargs)

    def _apply_cta_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._apply_cta_policy(*args, **kwargs)

    def _attach_editorial_source(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._attach_editorial_source(*args, **kwargs)

    def _postprocess_script_for_quality(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._postprocess_script_for_quality(*args, **kwargs)

    def _restore_script_from_retention_map(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._restore_script_from_retention_map(*args, **kwargs)

    def _repair_common_script_text_issues(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._repair_common_script_text_issues(*args, **kwargs)

    def _normalize_script_visible_text(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._normalize_script_visible_text(*args, **kwargs)

    def _normalize_script_narration_fields(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._normalize_script_narration_fields(*args, **kwargs)

    def _split_long_script_sentences(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._split_long_script_sentences(*args, **kwargs)

    def _should_force_conservative_fact_rewrite(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._should_force_conservative_fact_rewrite(*args, **kwargs)

    def _should_repair_loop(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._should_repair_loop(*args, **kwargs)

    def _rewrite_script_conservatively(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._rewrite_script_conservatively(*args, **kwargs)

    def _fact_backed_pt_br_sentence(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._fact_backed_pt_br_sentence(*args, **kwargs)

    def _soften_risky_sentence(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._soften_risky_sentence(*args, **kwargs)

    def _repair_script_loop_closure(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._repair_script_loop_closure(*args, **kwargs)

    def _loop_closure_sentence(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._loop_closure_sentence(*args, **kwargs)

    def _script_anchor_phrase(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._script_anchor_phrase(*args, **kwargs)

    def _attach_claim_trace(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._attach_claim_trace(*args, **kwargs)

    def _normalize_claim_trace(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._normalize_claim_trace(*args, **kwargs)

    def _validate_or_repair_script(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._validate_or_repair_script(*args, **kwargs)

    def _validate_ready_script_without_repair(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._validate_ready_script_without_repair(*args, **kwargs)

    def _ready_script_declared_fact_check_accepts(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._ready_script_declared_fact_check_accepts(*args, **kwargs)

    def _simple_mode_blocking_script_reasons(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._simple_mode_blocking_script_reasons(*args, **kwargs)

    def _simple_mode_lightweight_repair_reasons(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._simple_mode_lightweight_repair_reasons(*args, **kwargs)

    def _simple_mode_repair_improved(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._simple_mode_repair_improved(*args, **kwargs)

    def _claim_trace_metrics(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._claim_trace_metrics(*args, **kwargs)

    def _persist_script_rejection(self, *args: Any, **kwargs: Any) -> Any:
        return self.repair_domain._persist_script_rejection(*args, **kwargs)








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
        stage_timings_ms: dict[str, float] | None = None,
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
            "stage_timings_ms": stage_timings_ms or {},
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
