from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.editorial.research_brief import build_research_brief
from app.editorial.topic_mode import resolve_editorial_mode
from app.models import Job, PerformanceMetric, Script, TopicPlan, TopicRegistry, TopicRequest
from app.pipelines.base import BasePipeline
from app.pipelines.common import RecoverableStepError, model_payload
from app.utils import cosineish_similarity, jaccard_bigrams, new_id, stable_hash, utcnow


class TopicPipeline(BasePipeline):
    def step_topic_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert request
        history = self.recent_topic_history(session, request.niche_id)
        plan, topic_metrics = self.generate_topic_plan_with_repair(
            request=request,
            history=history,
            attempt=attempt,
        )
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "topic_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(plan),
            **plan,
            "quality_metrics": {
                **plan["quality_metrics"],
                **topic_metrics,
            },
        }
        session.execute(delete(TopicPlan).where(TopicPlan.job_id == job.job_id))
        session.add(TopicPlan(**model_payload(TopicPlan, payload)))
        self.storage.persist_json(job.job_id, "topic_plan.json", self._serialize_for_json(payload))
        self.storage.persist_json(job.job_id, "research_brief.json", self._serialize_for_json(payload.get("research_brief") or {}))
        topic_telemetry_file = self._persist_repair_telemetry(
            job.job_id,
            "topic_plan",
            {
                "job_id": job.job_id,
                "attempt": attempt,
                "final_passed": payload["quality_metrics"].get("topic_uniqueness_pass", False),
                "repair_attempts": payload["quality_metrics"].get("topic_repair_loop_attempt", 1),
                "attempts": payload["quality_metrics"].get("topic_repair_attempts_log", []),
            },
        )
        job.topic_summary = f"{plan['canonical_topic']} | {plan['angle']}"
        self._append_event(job.job_id, "topic.generated", "succeeded", payload["quality_metrics"])
        return ["topic_plan.json", "research_brief.json", topic_telemetry_file]

    def recent_topic_history(self, session: Session, niche_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(TopicRegistry)
            .where(
                TopicRegistry.approved.is_(True),
                TopicRegistry.created_at >= utcnow() - timedelta(days=90),
            )
            .order_by(TopicRegistry.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {"canonical_topic": row.canonical_topic, "hook": row.hook, "title": row.title}
            for row in rows
        ]

    def channel_learning_brief(self, session: Session, niche_id: str, limit: int = 30) -> dict[str, Any]:
        rows = session.execute(
            select(PerformanceMetric, TopicPlan, Script)
            .join(Job, Job.job_id == PerformanceMetric.job_id)
            .join(TopicPlan, TopicPlan.job_id == Job.job_id, isouter=True)
            .join(Script, Script.job_id == Job.job_id, isouter=True)
            .where(Job.niche_id == niche_id)
            .order_by(PerformanceMetric.created_at.desc())
            .limit(limit)
        ).all()
        samples = [
            {
                "job_id": metric.job_id,
                "retention_percent": metric.retention_percent,
                "viewed_vs_swiped_away_percent": metric.viewed_vs_swiped_away_percent,
                "rewatch_rate": metric.rewatch_rate,
                "rpm_usd": metric.rpm_usd,
                "monetization_status": metric.monetization_status,
                "canonical_topic": topic_plan.canonical_topic if topic_plan else None,
                "angle": topic_plan.angle if topic_plan else None,
                "hook": script.hook if script else None,
                "title": script.title if script else None,
            }
            for metric, topic_plan, script in rows
        ]
        if not samples:
            return {"sample_count": 0, "instruction": "No channel performance metrics recorded yet."}
        strong = [
            sample
            for sample in samples
            if (sample.get("retention_percent") or 0) >= 80
            or (sample.get("viewed_vs_swiped_away_percent") or 0) >= 70
            or (sample.get("rewatch_rate") or 0) >= 1.15
        ]
        weak = [
            sample
            for sample in samples
            if (sample.get("retention_percent") is not None and sample["retention_percent"] < 55)
            or (sample.get("viewed_vs_swiped_away_percent") is not None and sample["viewed_vs_swiped_away_percent"] < 50)
        ]
        return {
            "sample_count": len(samples),
            "strong_patterns": strong[:5],
            "weak_patterns": weak[:5],
            "instruction": "Prefer hooks, topics and pacing similar to strong_patterns; avoid weak_patterns unless the new angle is clearly different.",
        }

    def generate_topic_plan_with_repair(
        self,
        request: TopicRequest,
        history: list[dict[str, Any]],
        attempt: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        topic_attempts = max(1, self.settings.llm_topic_repair_attempts + 1)
        notes_suffix = ""
        last_metrics: dict[str, Any] | None = None
        last_plan: dict[str, Any] | None = None
        attempts_log: list[dict[str, Any]] = []
        for repair_attempt in range(1, topic_attempts + 1):
            plan = self.providers.creative.plan_topic(
                request.seed_theme,
                attempt,
                history,
                request.requested_angle,
                tone=request.tone,
                notes="\n\n".join(part for part in [request.notes, notes_suffix] if part),
            )
            plan = self.normalize_topic_plan_payload(plan, request)
            last_plan = plan
            candidate_topic_surface = f"{plan['canonical_topic']} {plan['angle']}"
            topic_similarity = max(
                [cosineish_similarity(candidate_topic_surface, f"{row['canonical_topic']} {row['title']}") for row in history],
                default=0.0,
            )
            hook_similarity = max(
                [jaccard_bigrams(plan["hook_promise"], row["hook"]) for row in history],
                default=0.0,
            )
            last_metrics = {
                "topic_uniqueness_pass": topic_similarity < 0.82 and hook_similarity < 0.88,
                "topic_similarity_max": round(topic_similarity, 3),
                "hook_similarity_max": round(hook_similarity, 3),
                "topic_repair_loop_attempt": repair_attempt,
            }
            attempts_log.append(
                {
                    "repair_attempt": repair_attempt,
                    "canonical_topic": plan["canonical_topic"],
                    "angle": plan["angle"],
                    "hook_promise": plan["hook_promise"],
                    "topic_similarity_max": round(topic_similarity, 3),
                    "hook_similarity_max": round(hook_similarity, 3),
                    "passed": last_metrics["topic_uniqueness_pass"],
                    "reason_codes": [] if last_metrics["topic_uniqueness_pass"] else ["topic_too_similar_to_history"],
                }
            )
            if last_metrics["topic_uniqueness_pass"]:
                if repair_attempt > 1:
                    last_metrics["topic_repair_used"] = True
                last_metrics["topic_repair_attempts_log"] = attempts_log
                return plan, last_metrics
            notes_suffix = (
                "REPAIR TOPIC FOR UNIQUENESS:\n"
                f"- previous canonical_topic: {plan['canonical_topic']}\n"
                f"- previous angle: {plan['angle']}\n"
                f"- previous hook_promise: {plan['hook_promise']}\n"
                f"- similarity thresholds exceeded: topic={topic_similarity:.3f}, hook={hook_similarity:.3f}\n"
                "- choose a distinctly different angle, hook promise and title set while preserving the seed theme.\n"
                "- avoid repeating recently approved topic surfaces or hooks."
            )
        assert last_plan is not None and last_metrics is not None
        last_metrics["topic_repair_attempts_log"] = attempts_log
        raise RecoverableStepError(
            f"topic too similar to approved history (topic_similarity={last_metrics['topic_similarity_max']}, hook_similarity={last_metrics['hook_similarity_max']})"
        )

    def normalize_topic_plan_payload(self, plan: dict[str, Any], request: TopicRequest) -> dict[str, Any]:
        aliases = {
            "canonical_topic": ("canonical_topic", "tema_canonico", "topico_canonico", "tema_principal", "topico_principal", "topic", "tema", "title"),
            "angle": ("angle", "angulo", "recorte", "abordagem", "requested_angle"),
            "hook_promise": ("hook_promise", "promessa_hook", "promessa_do_hook", "gancho", "hook"),
            "title_candidates": ("title_candidates", "titulos", "candidatos_titulo", "candidatos_de_titulo"),
            "entities": ("entities", "entidades", "elementos", "assuntos"),
            "search_terms": ("search_terms", "termos_busca", "termos_de_busca", "palavras_chave", "keywords"),
            "quality_metrics": ("quality_metrics", "metricas_qualidade", "metricas"),
        }
        normalized: dict[str, Any] = {}
        for target, names in aliases.items():
            for name in names:
                value = plan.get(name)
                if value not in (None, "", []):
                    normalized[target] = value
                    break

        canonical_topic = str(normalized.get("canonical_topic") or request.seed_theme).strip() or request.seed_theme
        angle = str(
            normalized.get("angle")
            or request.requested_angle
            or f"o detalhe mais contraintuitivo de {canonical_topic}"
        ).strip()
        hook_promise = str(
            normalized.get("hook_promise")
            or f"por que {canonical_topic} muda quando voce entende o mecanismo"
        ).strip()

        title_candidates = normalized.get("title_candidates")
        if isinstance(title_candidates, str):
            title_candidates = [title_candidates]
        if not isinstance(title_candidates, list) or not title_candidates:
            title_candidates = [f"{canonical_topic.capitalize()}: o detalhe que quase ninguem percebe"]

        entities = normalized.get("entities")
        if isinstance(entities, str):
            entities = [entities]
        if not isinstance(entities, list) or not entities:
            entities = [canonical_topic]

        search_terms = normalized.get("search_terms")
        if isinstance(search_terms, str):
            search_terms = [search_terms]
        if not isinstance(search_terms, list) or not search_terms:
            search_terms = [canonical_topic, f"{canonical_topic} curiosidades", f"{canonical_topic} explicacao"]

        quality_metrics = normalized.get("quality_metrics")
        if not isinstance(quality_metrics, dict):
            quality_metrics = {}
        if "topic_repair_used" not in quality_metrics:
            required = {"canonical_topic", "angle", "hook_promise", "title_candidates", "entities", "search_terms", "quality_metrics"}
            quality_metrics = {
                **quality_metrics,
                "topic_repair_used": any(key not in plan or plan.get(key) in (None, "", []) for key in required),
            }
        quality_metrics = {
            **quality_metrics,
            "editorial_mode": resolve_editorial_mode(
                {
                    "canonical_topic": canonical_topic,
                    "angle": angle,
                    "hook_promise": hook_promise,
                    "quality_metrics": quality_metrics,
                },
                request,
            ),
        }

        normalized_plan = {
            **plan,
            "canonical_topic": canonical_topic,
            "angle": angle,
            "hook_promise": hook_promise,
            "title_candidates": [str(title).strip() for title in title_candidates if str(title).strip()][:5],
            "entities": [str(entity).strip() for entity in entities if str(entity).strip()],
            "search_terms": [str(term).strip() for term in search_terms if str(term).strip()],
            "quality_metrics": quality_metrics,
        }
        return {
            **normalized_plan,
            "research_brief": build_research_brief(normalized_plan, request),
        }

    def upsert_topic_registry(self, session: Session, job_id: str, approved: bool) -> None:
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job_id))
        script = session.scalar(select(Script).where(Script.job_id == job_id))
        if not topic_plan or not script:
            return
        existing = session.scalar(select(TopicRegistry).where(TopicRegistry.job_id == job_id))
        if existing:
            existing.approved = approved
            existing.title = script.title
            existing.hook = script.hook
            existing.entities = topic_plan.entities
            return
        session.add(
            TopicRegistry(
                registry_id=new_id(),
                job_id=job_id,
                canonical_topic=topic_plan.canonical_topic,
                title=script.title,
                hook=script.hook,
                entities=topic_plan.entities,
                approved=approved,
                created_at=utcnow(),
            )
        )
