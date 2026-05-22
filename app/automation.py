from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.editorial.repetition import build_channel_repetition_report
from app.manual_script import build_ready_script_notes, parse_ready_script
from app.models import (
    AutomationAttempt,
    AutomationRun,
    AutomationSetting,
    Job,
    PublicationSchedule,
    ReadyScriptItem,
    Script,
    TopicRequest,
)
from app.schemas import TopicRequestCreate
from app.trends import TrendResearcher
from app.utils import new_id, stable_hash, utcnow


READY_SCRIPT_SPLIT_RE = re.compile(r"(?im)^\s*t[ií]tulo\s*:")
AUTOMATION_ENABLED_KEY = "automation_enabled"
AUTOMATION_SOURCE_READY_SCRIPT = "ready_script_bank"
AUTOMATION_SOURCE_AUTO_TOPIC = "automatic_topic"
AUTOMATION_SOURCE_RESUME = "resume_publish"
ACTIVE_SCHEDULE_STATUSES = {"scheduled", "publishing", "published"}
DEFAULT_AUTOMATION_TOPIC_POOL = [
    "curiosidades espaciais",
    "tecnologia que parece ficcao",
    "desastres historicos",
    "engenharia curiosa",
    "animais extremos",
    "fenomenos naturais",
    "historia pouco conhecida",
    "corpo humano",
    "misterios da ciencia",
    "lugares extremos da Terra",
]


@dataclass(frozen=True)
class ReadyScriptImportResult:
    imported: int
    errors: list[str]


class AutomationService:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator
        self.settings = orchestrator.settings

    def automation_enabled(self, session: Session) -> bool:
        row = session.get(AutomationSetting, AUTOMATION_ENABLED_KEY)
        if row is None:
            return bool(self.settings.automation_enabled)
        return bool((row.value or {}).get("enabled"))

    def set_automation_enabled(self, enabled: bool) -> None:
        with session_scope() as session:
            row = session.get(AutomationSetting, AUTOMATION_ENABLED_KEY)
            if row is None:
                row = AutomationSetting(key=AUTOMATION_ENABLED_KEY, value={"enabled": enabled})
                session.add(row)
            else:
                row.value = {"enabled": enabled}

    def import_ready_script_batch(self, raw_text: str, *, fact_check_confirmed: bool, source: str = "batch") -> ReadyScriptImportResult:
        blocks = split_ready_script_batch(raw_text)
        imported = 0
        errors: list[str] = []
        with session_scope() as session:
            for index, block in enumerate(blocks, start=1):
                try:
                    ready_script = parse_ready_script(block, fact_check_confirmed=fact_check_confirmed)
                except ValueError as exc:
                    errors.append(f"bloco {index}: {exc}")
                    continue
                content_hash = stable_hash({"raw_text": ready_script.raw_text, "fact_check_confirmed": fact_check_confirmed})
                existing = session.scalar(select(ReadyScriptItem).where(ReadyScriptItem.content_hash == content_hash))
                if existing:
                    errors.append(f"bloco {index}: roteiro duplicado ignorado")
                    continue
                session.add(
                    ReadyScriptItem(
                        script_item_id=new_id(),
                        schema_version=self.settings.schema_version,
                        content_hash=content_hash,
                        status="available" if fact_check_confirmed else "needs_review",
                        source=source,
                        title=str(ready_script.script["title"]),
                        raw_text=ready_script.raw_text,
                        parsed_script=ready_script.script,
                        hashtags=ready_script.hashtags,
                        fact_check_confirmed=fact_check_confirmed,
                    )
                )
                imported += 1
        return ReadyScriptImportResult(imported=imported, errors=errors)

    def dashboard_context(self) -> dict[str, Any]:
        with session_scope() as session:
            last_run = session.scalar(select(AutomationRun).order_by(AutomationRun.started_at.desc()).limit(1))
            attempts = []
            if last_run:
                attempts = session.scalars(
                    select(AutomationAttempt)
                    .where(AutomationAttempt.run_id == last_run.run_id)
                    .order_by(AutomationAttempt.attempt_number.asc(), AutomationAttempt.created_at.asc())
                ).all()
            metrics = {
                "enabled": self.automation_enabled(session),
                "available_ready_scripts": session.scalar(select(func.count()).select_from(ReadyScriptItem).where(ReadyScriptItem.status == "available")) or 0,
                "needs_review_ready_scripts": session.scalar(select(func.count()).select_from(ReadyScriptItem).where(ReadyScriptItem.status == "needs_review")) or 0,
                "scheduled_ready_scripts": session.scalar(select(func.count()).select_from(ReadyScriptItem).where(ReadyScriptItem.status == "scheduled")) or 0,
            }
            ready_scripts = session.scalars(
                select(ReadyScriptItem).order_by(ReadyScriptItem.created_at.desc(), ReadyScriptItem.title.asc()).limit(50)
            ).all()
            return {
                "metrics": metrics,
                "ready_scripts": [serialize_ready_script_item(item) for item in ready_scripts],
                "last_run": serialize_run(last_run),
                "last_attempts": [serialize_attempt(attempt) for attempt in attempts],
                "settings": {
                    "timezone": self.settings.automation_daily_timezone,
                    "run_time": self.settings.automation_daily_run_time,
                    "publish_time": self.settings.automation_publish_time,
                    "fill_window_days": self.settings.automation_fill_window_days,
                    "max_generation_attempts": self.settings.automation_max_generation_attempts,
                    "score_threshold": self.settings.automation_score_threshold,
                },
            }

    def run_daily_cycle(self, *, force: bool = False) -> dict[str, Any]:
        local_tz = ZoneInfo(self.settings.automation_daily_timezone)
        local_date = datetime.now(local_tz).date().isoformat()
        run = self._acquire_run(local_date, force=force)
        if run.status != "running":
            return serialize_run(run)
        try:
            with session_scope() as session:
                if not self.automation_enabled(session):
                    run = self._finish_run(run.run_id, status="skipped", skipped_reason="automation_disabled")
                    return serialize_run(run)

            preflight = self._youtube_preflight()
            if not preflight["passed"]:
                run = self._finish_run(run.run_id, status="failed", error="; ".join(preflight["missing_items"]))
                return serialize_run(run)

            target_day = self._first_vacant_day()
            if target_day is None:
                run = self._finish_run(run.run_id, status="skipped", skipped_reason="no_vacant_day")
                return serialize_run(run)
            target_local = datetime.fromisoformat(f"{target_day.isoformat()}T{self.settings.automation_publish_time}").replace(tzinfo=local_tz)
            target_utc = target_local.astimezone(UTC)
            self._set_run_target(run.run_id, target_day, target_utc)

            resumed = self._resume_publishable_job(run.run_id, target_day)
            if resumed:
                return serialize_run(self._get_run(run.run_id))

            for attempt_number in range(1, self.settings.automation_max_generation_attempts + 1):
                attempt_result = self._run_generation_attempt(run.run_id, attempt_number, target_day)
                if attempt_result.get("scheduled"):
                    return serialize_run(self._get_run(run.run_id))
                if attempt_result.get("provider_limit"):
                    return serialize_run(self._finish_run(run.run_id, status="failed", error=str(attempt_result.get("error") or "provider_limit")))
            return serialize_run(self._finish_run(run.run_id, status="failed", error="max_generation_attempts_exhausted"))
        except Exception as exc:  # noqa: BLE001
            return serialize_run(self._finish_run(run.run_id, status="failed", error=str(exc)))

    def _acquire_run(self, local_date: str, *, force: bool) -> AutomationRun:
        with session_scope() as session:
            existing = session.scalar(select(AutomationRun).where(AutomationRun.local_date == local_date))
            now = utcnow()
            if existing:
                stale = existing.status == "running" and existing.started_at < now - timedelta(hours=6)
                if not force and existing.status in {"running", "succeeded", "skipped"} and not stale:
                    return existing
                existing.status = "running"
                existing.started_at = now
                existing.finished_at = None
                existing.error = None
                existing.skipped_reason = None
                existing.attempts_used = 0
                existing.run_metadata = {"forced": force, "resumed_stale": stale}
                return existing
            run = AutomationRun(
                run_id=new_id(),
                schema_version=self.settings.schema_version,
                content_hash=stable_hash({"local_date": local_date, "created_at": now.isoformat()}),
                local_date=local_date,
                timezone=self.settings.automation_daily_timezone,
                status="running",
                started_at=now,
                run_metadata={"forced": force},
            )
            session.add(run)
            return run

    def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        skipped_reason: str | None = None,
        error: str | None = None,
        result_job_id: str | None = None,
        result_schedule_id: str | None = None,
    ) -> AutomationRun:
        with session_scope() as session:
            run = session.get(AutomationRun, run_id)
            if not run:
                raise KeyError(run_id)
            run.status = status
            run.finished_at = utcnow()
            run.skipped_reason = skipped_reason
            run.error = error
            if result_job_id:
                run.result_job_id = result_job_id
            if result_schedule_id:
                run.result_schedule_id = result_schedule_id
            return run

    def _get_run(self, run_id: str) -> AutomationRun:
        with session_scope() as session:
            run = session.get(AutomationRun, run_id)
            if not run:
                raise KeyError(run_id)
            return run

    def _set_run_target(self, run_id: str, target_day: date, target_utc: datetime) -> None:
        with session_scope() as session:
            run = session.get(AutomationRun, run_id)
            if not run:
                raise KeyError(run_id)
            run.target_publish_date = target_day.isoformat()
            run.target_publish_at_utc = target_utc

    def _youtube_preflight(self) -> dict[str, Any]:
        missing_items: list[str] = []
        if not self.settings.youtube_api_enabled:
            missing_items.append("YTS_YOUTUBE_API_ENABLED=false")
        if self.settings.youtube_publish_mode != "api":
            missing_items.append("YTS_YOUTUBE_PUBLISH_MODE != api")
        if not self.settings.youtube_channel_id:
            missing_items.append("YTS_YOUTUBE_CHANNEL_ID ausente")
        redirect_uri = self.settings.youtube_oauth_redirect_uri or f"{self.settings.app_url.rstrip('/')}/youtube/oauth/callback"
        status = self.orchestrator.youtube.connection_status(redirect_uri)
        missing_items.extend(item for item in status.missing_items if item not in missing_items)
        return {"passed": not missing_items and status.connected, "missing_items": missing_items, "connected": status.connected}

    def _first_vacant_day(self) -> date | None:
        local_tz = ZoneInfo(self.settings.automation_daily_timezone)
        today = datetime.now(local_tz).date()
        with session_scope() as session:
            rows = session.scalars(select(PublicationSchedule).where(PublicationSchedule.status.in_(ACTIVE_SCHEDULE_STATUSES))).all()
        occupied = set()
        for schedule in rows:
            scheduled_at = schedule.scheduled_for_utc if schedule.scheduled_for_utc.tzinfo else schedule.scheduled_for_utc.replace(tzinfo=UTC)
            occupied.add(scheduled_at.astimezone(local_tz).date())
        for offset in range(1, self.settings.automation_fill_window_days + 1):
            candidate = today + timedelta(days=offset)
            if candidate not in occupied:
                return candidate
        return None

    def _resume_publishable_job(self, run_id: str, target_day: date) -> bool:
        with session_scope() as session:
            rows = session.scalars(
                select(AutomationAttempt)
                .where(AutomationAttempt.status == "publish_failed")
                .where(AutomationAttempt.job_id.is_not(None))
                .order_by(AutomationAttempt.updated_at.desc())
                .limit(10)
            ).all()
            for row in rows:
                failures = session.scalar(
                    select(func.count())
                    .select_from(AutomationAttempt)
                    .where(AutomationAttempt.job_id == row.job_id)
                    .where(AutomationAttempt.status == "publish_failed")
                ) or 0
                if failures < self.settings.automation_max_publish_attempts_per_job:
                    job_id = str(row.job_id)
                    break
            else:
                return False
        attempt = self._create_attempt(run_id, 1, AUTOMATION_SOURCE_RESUME, job_id=job_id)
        try:
            schedule_id = self._approve_and_schedule(job_id, target_day)
        except Exception as exc:  # noqa: BLE001
            self._finish_attempt(attempt.attempt_id, status="publish_failed", error=str(exc))
            self._finish_run(run_id, status="failed", error=str(exc), result_job_id=job_id)
            return True
        self._finish_attempt(attempt.attempt_id, status="scheduled")
        self._finish_run(run_id, status="succeeded", result_job_id=job_id, result_schedule_id=schedule_id)
        return True

    def _run_generation_attempt(self, run_id: str, attempt_number: int, target_day: date) -> dict[str, Any]:
        selected_script = self._select_ready_script_item()
        source = AUTOMATION_SOURCE_READY_SCRIPT if selected_script else AUTOMATION_SOURCE_AUTO_TOPIC
        attempt = self._create_attempt(run_id, attempt_number, source, ready_script_item_id=selected_script.script_item_id if selected_script else None)
        try:
            payload = self._job_payload_from_ready_script(selected_script) if selected_script else self._automatic_topic_payload()
            job_id = self.orchestrator.create_job(payload)
            self._attach_job_to_attempt(attempt.attempt_id, job_id)
            if selected_script:
                self._consume_ready_script(selected_script.script_item_id, job_id)
            status = self.orchestrator.process_job(job_id)
            if status != "ready_for_upload":
                error = self._job_status_error(job_id, status)
                self._finish_attempt(attempt.attempt_id, status="not_eligible", error=error)
                if selected_script:
                    self._mark_ready_script_needs_review(selected_script.script_item_id)
                return {"scheduled": False, "provider_limit": self._is_provider_limit_error(error), "error": error}
            score_report = self.evaluate_autoapproval(job_id)
            self._set_attempt_score(attempt.attempt_id, score_report)
            if not score_report["eligible"]:
                self._finish_attempt(attempt.attempt_id, status="score_failed", error="; ".join(score_report["reasons"]))
                if selected_script:
                    self._mark_ready_script_needs_review(selected_script.script_item_id)
                return {"scheduled": False}
            schedule_id = self._approve_and_schedule(job_id, target_day)
        except Exception as exc:  # noqa: BLE001
            self._finish_attempt(attempt.attempt_id, status="failed", error=str(exc))
            if selected_script:
                self._mark_ready_script_needs_review(selected_script.script_item_id)
            return {"scheduled": False}
        self._finish_attempt(attempt.attempt_id, status="scheduled")
        if selected_script:
            self._mark_ready_script_scheduled(selected_script.script_item_id)
        self._finish_run(run_id, status="succeeded", result_job_id=job_id, result_schedule_id=schedule_id)
        return {"scheduled": True, "job_id": job_id, "schedule_id": schedule_id}

    def _job_status_error(self, job_id: str, status: str) -> str:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job and job.failure_reason:
                return f"job_status={status}; {job.failure_reason}"
        return f"job_status={status}"

    def _is_provider_limit_error(self, message: str) -> bool:
        normalized = message.lower()
        return any(
            marker in normalized
            for marker in [
                "provider limit",
                "usage limit",
                "quota",
                "rate limit",
                "too many requests",
                "insufficient",
                "balance",
                "credit",
            ]
        )

    def _create_attempt(
        self,
        run_id: str,
        attempt_number: int,
        source: str,
        *,
        ready_script_item_id: str | None = None,
        job_id: str | None = None,
    ) -> AutomationAttempt:
        with session_scope() as session:
            attempt = AutomationAttempt(
                attempt_id=new_id(),
                run_id=run_id,
                schema_version=self.settings.schema_version,
                content_hash=stable_hash({"run_id": run_id, "attempt_number": attempt_number, "source": source, "created_at": utcnow().isoformat()}),
                attempt_number=attempt_number,
                source=source,
                status="running",
                ready_script_item_id=ready_script_item_id,
                job_id=job_id,
            )
            session.add(attempt)
            run = session.get(AutomationRun, run_id)
            if run:
                run.attempts_used = max(run.attempts_used or 0, attempt_number)
            return attempt

    def _attach_job_to_attempt(self, attempt_id: str, job_id: str) -> None:
        with session_scope() as session:
            attempt = session.get(AutomationAttempt, attempt_id)
            if attempt:
                attempt.job_id = job_id

    def _set_attempt_score(self, attempt_id: str, score_report: dict[str, Any]) -> None:
        with session_scope() as session:
            attempt = session.get(AutomationAttempt, attempt_id)
            if attempt:
                attempt.score = float(score_report.get("score") or 0.0)
                attempt.score_report = score_report

    def _finish_attempt(self, attempt_id: str, *, status: str, error: str | None = None) -> AutomationAttempt:
        with session_scope() as session:
            attempt = session.get(AutomationAttempt, attempt_id)
            if not attempt:
                raise KeyError(attempt_id)
            attempt.status = status
            attempt.error = error
            attempt.finished_at = utcnow()
            return attempt

    def _select_ready_script_item(self) -> ReadyScriptItem | None:
        with session_scope() as session:
            items = session.scalars(select(ReadyScriptItem).where(ReadyScriptItem.status == "available")).all()
            random.shuffle(items)
            for item in items:
                report = self._ready_script_repetition_report(session, item)
                if report.get("repetition_risk") == "high":
                    item.last_skip_reason = "high_narrative_similarity"
                    item.last_similarity_score = float(report.get("max_similarity") or 0.0)
                    continue
                item.last_skip_reason = None
                item.last_similarity_score = None
                return item
        return None

    def _ready_script_repetition_report(self, session: Session, item: ReadyScriptItem) -> dict[str, Any]:
        rows = session.execute(
            select(Job.job_id, Job.topic_summary, Script.title, Script.hook, Script.ending, Script.estimated_duration_sec, Script.body_beats)
            .join(Script, Script.job_id == Job.job_id)
            .join(PublicationSchedule, PublicationSchedule.job_id == Job.job_id, isouter=True)
            .where(or_(PublicationSchedule.status.in_(ACTIVE_SCHEDULE_STATUSES), Job.status.in_(["approved_for_publish", "published"])))
            .order_by(Job.created_at.desc())
            .limit(30)
        ).all()
        recent_rows = [
            {
                "job_id": job_id,
                "topic_summary": topic_summary,
                "title": title,
                "hook": hook,
                "ending": ending,
                "estimated_duration_sec": estimated_duration_sec,
                "body_beats": body_beats,
            }
            for job_id, topic_summary, title, hook, ending, estimated_duration_sec, body_beats in rows
        ]
        return build_channel_repetition_report(
            current={
                "canonical_topic": item.title,
                "angle": "roteiro pronto",
                "script": item.parsed_script,
            },
            recent_rows=recent_rows,
        )

    def _consume_ready_script(self, script_item_id: str, job_id: str) -> None:
        with session_scope() as session:
            item = session.get(ReadyScriptItem, script_item_id)
            if item:
                item.status = "consumed"
                item.consumed_at = utcnow()
                item.consumed_job_id = job_id

    def _mark_ready_script_needs_review(self, script_item_id: str) -> None:
        with session_scope() as session:
            item = session.get(ReadyScriptItem, script_item_id)
            if item and item.status != "scheduled":
                item.status = "needs_review"

    def _mark_ready_script_scheduled(self, script_item_id: str) -> None:
        with session_scope() as session:
            item = session.get(ReadyScriptItem, script_item_id)
            if item:
                item.status = "scheduled"

    def _job_payload_from_ready_script(self, item: ReadyScriptItem) -> dict[str, Any]:
        return TopicRequestCreate(
            seed_theme=item.title,
            niche_id=self.settings.niche_id,
            language=self.settings.language,
            target_duration_sec=self.settings.target_duration_sec,
            tone="intrigante_direto",
            cta_style="none",
            notes=build_ready_script_notes(None, item.raw_text, item.fact_check_confirmed),
            requested_angle=None,
        ).model_dump()

    def _automatic_topic_payload(self) -> dict[str, Any]:
        trend = TrendResearcher().find_topic(self.settings.niche_id)
        if trend:
            seed_theme = trend.topic
            requested_angle = trend.requested_angle
            notes = "\n".join(["input_mode=theme", "automation_source=automatic_topic", trend.as_notes()])
        else:
            seed_theme = random.choice(DEFAULT_AUTOMATION_TOPIC_POOL)
            requested_angle = None
            notes = "input_mode=theme\nautomation_source=automatic_topic\ntrend_research=unavailable"
        return TopicRequestCreate(
            seed_theme=seed_theme,
            niche_id=self.settings.niche_id,
            language=self.settings.language,
            target_duration_sec=self.settings.target_duration_sec,
            tone="intrigante_direto",
            cta_style="none",
            notes=notes,
            requested_angle=requested_angle,
        ).model_dump()

    def evaluate_autoapproval(self, job_id: str) -> dict[str, Any]:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            script = session.scalar(select(Script).where(Script.job_id == job_id))
            monetization_report = self.orchestrator._read_job_json(job_id, "monetization_report.json")
            repetition_report = monetization_report.get("channel_repetition_report") or {}
            metadata_review = monetization_report.get("metadata_review") or {}
            fact_claims_report = monetization_report.get("fact_claims_report") or {}
            publish_readiness = monetization_report.get("publish_readiness") or {}
            audit = publish_readiness.get("minimax_audit") or {}
            quality_summary = dict(job.quality_summary or {})
            asset_summary = dict(quality_summary.get("assets") or {})
            qa_metrics = dict(script.qa_metrics or {}) if script else {}

        reasons: list[str] = []
        if job.status != "ready_for_upload":
            reasons.append("job_not_ready_for_upload")
        if not monetization_report.get("passed"):
            reasons.append("monetization_not_passed")
        repetition_risk = str(repetition_report.get("repetition_risk") or "unknown")
        if repetition_risk == "high":
            reasons.append("high_narrative_similarity")

        factual_score = as_score(audit.get("factual_score"))
        if factual_score is None:
            factual_score = 1.0 if not fact_claims_report.get("requires_fact_review") else 0.0
        retention_score = as_score(audit.get("retention_score"))
        if retention_score is None:
            candidates = [as_score(qa_metrics.get("hook_score")), as_score(qa_metrics.get("information_density_score"))]
            values = [value for value in candidates if value is not None]
            retention_score = sum(values) / len(values) if values else 0.85
        metadata_score = as_score(audit.get("metadata_score"))
        if metadata_score is None:
            metadata_score = 1.0 if not metadata_review.get("requires_metadata_review") else 0.7
        asset_score = as_score(asset_summary.get("asset_semantic_score_avg"))
        if asset_score is None:
            asset_score = 1.0

        if factual_score < 0.80:
            reasons.append("factual_score_below_threshold")
        if retention_score < 0.75:
            reasons.append("retention_score_below_threshold")
        if metadata_score < 0.75:
            reasons.append("metadata_score_below_threshold")
        if asset_score < 0.80:
            reasons.append("asset_semantic_score_below_threshold")

        component_scores = [factual_score, retention_score, metadata_score, asset_score]
        composite = sum(component_scores) / len(component_scores)
        penalty = 0.10 if repetition_risk == "medium" else 0.0
        score = max(0.0, round(composite - penalty, 3))
        if score < self.settings.automation_score_threshold:
            reasons.append("automation_score_below_threshold")
        report = {
            "eligible": not reasons,
            "score": score,
            "threshold": self.settings.automation_score_threshold,
            "reasons": list(dict.fromkeys(reasons)),
            "components": {
                "factual_score": round(factual_score, 3),
                "retention_score": round(retention_score, 3),
                "metadata_score": round(metadata_score, 3),
                "asset_semantic_score": round(asset_score, 3),
                "repetition_risk": repetition_risk,
                "repetition_penalty": penalty,
            },
        }
        self.orchestrator.storage.persist_json(job_id, "autoapproval_score.json", self.orchestrator._serialize_for_json(report))
        return report

    def _approve_and_schedule(self, job_id: str, target_day: date) -> str:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status == "ready_for_upload":
                pass
            elif job.status != "approved_for_publish":
                raise RuntimeError(f"job_status_not_publishable={job.status}")
        if self._job_status(job_id) == "ready_for_upload":
            self.orchestrator.review_job(
                {
                    "reviewer_identity": "automation:daily-cycle",
                    "action": "approve",
                    "reason_codes": ["automation_score_confirmed"],
                    "notes": "Aprovado automaticamente por Score de Autoaprovacao.",
                },
                job_id,
            )
        payload = {
            "scheduled_for_local": f"{target_day.isoformat()}T{self.settings.automation_publish_time}",
            "timezone": self.settings.automation_daily_timezone,
            "youtube_visibility": "public",
            "notes": "Agendado automaticamente pelo Ciclo Diario de Automacao.",
        }
        last_error: Exception | None = None
        for _ in range(self.settings.automation_max_publish_attempts_per_job):
            try:
                self.orchestrator.schedule_publication(job_id, payload)
                schedule_id = self._publication_schedule_id(job_id)
                if schedule_id:
                    return schedule_id
                raise RuntimeError("publication_schedule_missing_after_success")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("publication_schedule_failed")

    def _publication_schedule_id(self, job_id: str) -> str | None:
        with session_scope() as session:
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            return str(schedule.schedule_id) if schedule else None

    def _job_status(self, job_id: str) -> str:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            return str(job.status)


def split_ready_script_batch(raw_text: str) -> list[str]:
    text = (raw_text or "").strip()
    if not text:
        return []
    starts = [match.start() for match in READY_SCRIPT_SPLIT_RE.finditer(text)]
    if not starts:
        return [text]
    blocks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def as_score(value: Any) -> float | None:
    try:
        if value is None:
            return None
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))


def serialize_run(run: AutomationRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "local_date": run.local_date,
        "timezone": run.timezone,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "target_publish_date": run.target_publish_date,
        "target_publish_at_utc": run.target_publish_at_utc.isoformat() if run.target_publish_at_utc else None,
        "attempts_used": run.attempts_used,
        "result_job_id": run.result_job_id,
        "result_schedule_id": run.result_schedule_id,
        "skipped_reason": run.skipped_reason,
        "error": run.error,
        "metadata": run.run_metadata or {},
    }


def serialize_ready_script_item(item: ReadyScriptItem) -> dict[str, Any]:
    return {
        "script_item_id": item.script_item_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "status": item.status,
        "source": item.source,
        "title": item.title,
        "fact_check_confirmed": item.fact_check_confirmed,
        "consumed_job_id": item.consumed_job_id,
        "last_skip_reason": item.last_skip_reason,
        "last_similarity_score": item.last_similarity_score,
    }


def serialize_attempt(attempt: AutomationAttempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "run_id": attempt.run_id,
        "attempt_number": attempt.attempt_number,
        "source": attempt.source,
        "status": attempt.status,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
        "ready_script_item_id": attempt.ready_script_item_id,
        "job_id": attempt.job_id,
        "score": attempt.score,
        "score_report": attempt.score_report or {},
        "error": attempt.error,
    }
