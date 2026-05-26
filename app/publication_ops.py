from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.compliance.review import build_human_review_checklist
from app.db import session_scope
from app.models import ChannelPublication, Job, PerformanceMetric, PublicationSchedule, ReviewRecord, Script, TopicPlan, TopicRequest
from app.pipelines.common import FatalStepError, model_payload
from app.schemas import PublicationSchedulePayload
from app.tiktok_api import TikTokIntegrationError
from app.utils import file_uri, iso_now, new_id, path_from_uri, read_json, stable_hash, utcnow, write_json
from app.youtube_api import YouTubeIntegrationError


RETENTION_HARD_FAILURE_STATUSES = {
    "failed",
    "script_quality_failed",
    "scene_plan_quality_failed",
    "asset_quality_failed",
    "subtitle_quality_failed",
    "render_quality_failed",
}
RETENTION_RECOVERABLE_STATUSES = {
    "monetization_review",
    "blocked_for_monetization",
    "rejected",
}
RETENTION_EXCLUDED_JOB_STATUSES = {
    "queued",
    "running",
    "published",
    "cancelled",
}
RETENTION_EXCLUDED_SCHEDULE_STATUSES = {
    "publishing",
    "published",
}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class PublicationOperations:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    @property
    def settings(self) -> Any:
        return self.owner.settings

    @property
    def storage(self) -> Any:
        return self.owner.storage

    @property
    def youtube(self) -> Any:
        return self.owner.youtube

    @property
    def tiktok(self) -> Any:
        return self.owner.tiktok

    @property
    def monetization_pipeline(self) -> Any:
        return self.owner.monetization_pipeline

    @property
    def topic_pipeline(self) -> Any:
        return self.owner.topic_pipeline

    def _serialize_for_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.owner._serialize_for_json(payload)

    def _read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        return self.owner._read_job_json(job_id, relative_path)

    def _append_event(self, job_id: str, event_name: str, status: str, payload: dict[str, Any]) -> None:
        self.owner._append_event(job_id, event_name, status, payload)

    def _scheduled_local_to_utc(self, scheduled_for_local: str, timezone_name: str) -> datetime:
        local_naive = datetime.fromisoformat(scheduled_for_local)
        local_aware = local_naive.replace(tzinfo=ZoneInfo(timezone_name))
        return local_aware.astimezone(UTC)

    def _publication_schedule_payload(self, schedule: PublicationSchedule) -> dict[str, Any]:
        scheduled_for_utc = schedule.scheduled_for_utc if schedule.scheduled_for_utc.tzinfo else schedule.scheduled_for_utc.replace(tzinfo=UTC)
        published_at = schedule.published_at if schedule.published_at and schedule.published_at.tzinfo else (
            schedule.published_at.replace(tzinfo=UTC) if schedule.published_at else None
        )
        local_dt = scheduled_for_utc.astimezone(ZoneInfo(schedule.timezone))
        return {
            "schema_version": self.settings.schema_version,
            "job_id": schedule.job_id,
            "schedule_id": schedule.schedule_id,
            "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
            "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
            "status": schedule.status,
            "scheduled_for_utc": scheduled_for_utc.isoformat(),
            "scheduled_for_local": local_dt.isoformat(),
            "local_date": local_dt.date().isoformat(),
            "local_time": local_dt.strftime("%H:%M"),
            "timezone": schedule.timezone,
            "youtube_visibility": schedule.youtube_visibility,
            "notes": schedule.notes,
            "published_at": published_at.isoformat() if published_at else None,
            "youtube_video_id": schedule.youtube_video_id,
            "youtube_url": schedule.youtube_url,
        }

    def _persist_publication_schedule_artifact(self, job: Job, schedule: PublicationSchedule) -> None:
        payload = self._publication_schedule_payload(schedule)
        self.storage.persist_json(job.job_id, "publication_schedule.json", self._serialize_for_json(payload))
        artifact_index = dict(job.artifact_index or {})
        artifact_index["publication_schedule"] = "publication_schedule.json"
        job.artifact_index = artifact_index
        quality_summary = dict(job.quality_summary or {})
        quality_summary["publication_schedule"] = {
            "status": schedule.status,
            "scheduled_for_utc": payload["scheduled_for_utc"],
            "scheduled_for_local": payload["scheduled_for_local"],
            "timezone": schedule.timezone,
            "youtube_visibility": schedule.youtube_visibility,
        }
        job.quality_summary = quality_summary

    def _append_publication_attempt(self, job_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        attempts_payload = self._read_job_json(job_id, "youtube_publish_attempts.json")
        attempts = list(attempts_payload.get("attempts") or [])
        attempts.append(payload)
        persisted = {
            "schema_version": self.settings.schema_version,
            "job_id": job_id,
            "updated_at": iso_now(),
            "attempts": attempts[-20:],
        }
        self.storage.persist_json(job_id, "youtube_publish_attempts.json", self._serialize_for_json(persisted))
        return attempts[-20:]

    def _youtube_api_mode_enabled(self) -> bool:
        return bool(self.settings.youtube_api_enabled and self.settings.youtube_publish_mode == "api")

    def _tiktok_auto_publish_enabled(self) -> bool:
        return bool(self.settings.tiktok_auto_publish_enabled)

    def _ensure_youtube_api_ready(self) -> None:
        status = self.youtube.connection_status()
        blockers = [
            item
            for item in status.missing_items
            if item
            not in {"YTS_YOUTUBE_PUBLISH_MODE != api", "YTS_YOUTUBE_API_ENABLED=false"}
        ]
        if blockers:
            raise FatalStepError("integração YouTube indisponível: " + ", ".join(blockers))

    def _channel_publication_payload(self, publication: ChannelPublication) -> dict[str, Any]:
        scheduled_for_utc = publication.scheduled_for_utc if publication.scheduled_for_utc.tzinfo else publication.scheduled_for_utc.replace(tzinfo=UTC)
        published_at = publication.published_at if publication.published_at and publication.published_at.tzinfo else (
            publication.published_at.replace(tzinfo=UTC) if publication.published_at else None
        )
        local_dt = scheduled_for_utc.astimezone(ZoneInfo(publication.timezone))
        return {
            "schema_version": self.settings.schema_version,
            "publication_id": publication.publication_id,
            "job_id": publication.job_id,
            "channel": publication.channel,
            "status": publication.status,
            "source": publication.source,
            "scheduled_for_utc": scheduled_for_utc.isoformat(),
            "scheduled_for_local": local_dt.isoformat(),
            "local_date": local_dt.date().isoformat(),
            "local_time": local_dt.strftime("%H:%M"),
            "timezone": publication.timezone,
            "privacy_level": publication.privacy_level,
            "external_id": publication.external_id,
            "external_url": publication.external_url,
            "published_at": published_at.isoformat() if published_at else None,
            "attempt_count": publication.attempt_count,
            "last_attempt_at": publication.last_attempt_at.isoformat() if publication.last_attempt_at else None,
            "last_error": publication.last_error,
            "channel_metadata": publication.channel_metadata or {},
        }

    def _persist_channel_publication_artifact(self, job: Job, publication: ChannelPublication) -> None:
        payload = self._channel_publication_payload(publication)
        artifact_name = f"{publication.channel}_publication.json"
        self.storage.persist_json(job.job_id, artifact_name, self._serialize_for_json(payload))
        artifact_index = dict(job.artifact_index or {})
        artifact_index[f"{publication.channel}_publication"] = artifact_name
        job.artifact_index = artifact_index
        quality_summary = dict(job.quality_summary or {})
        channel_summary = dict(quality_summary.get("channel_publications") or {})
        channel_summary[publication.channel] = {
            "status": publication.status,
            "source": publication.source,
            "scheduled_for_utc": payload["scheduled_for_utc"],
            "external_id": publication.external_id,
            "external_url": publication.external_url,
            "last_error": publication.last_error,
        }
        quality_summary["channel_publications"] = channel_summary
        job.quality_summary = quality_summary

    def _refresh_channel_publication_hash(self, publication: ChannelPublication) -> None:
        publication.content_hash = stable_hash(
            {
                "job_id": publication.job_id,
                "channel": publication.channel,
                "status": publication.status,
                "source": publication.source,
                "scheduled_for_utc": publication.scheduled_for_utc.isoformat(),
                "timezone": publication.timezone,
                "privacy_level": publication.privacy_level,
                "external_id": publication.external_id,
                "external_url": publication.external_url,
                "published_at": publication.published_at.isoformat() if publication.published_at else None,
                "attempt_count": publication.attempt_count,
                "last_error": publication.last_error,
            }
        )

    def _tiktok_caption(self, package: dict[str, Any]) -> str:
        title = str(package.get("title") or "").strip()
        hashtags = [str(tag).strip() for tag in list(package.get("hashtags") or []) if str(tag).strip()]
        suffix = " ".join(tag if tag.startswith("#") else f"#{tag}" for tag in hashtags[:8])
        caption = " ".join(part for part in [title, suffix] if part)
        return caption[:2200]

    def _ensure_tiktok_publication_for_schedule(
        self,
        session: Session,
        job: Job,
        schedule: PublicationSchedule,
        *,
        source: str,
        scheduled_for_utc: datetime | None = None,
    ) -> ChannelPublication | None:
        if not self._tiktok_auto_publish_enabled():
            return None
        if schedule.status not in {"scheduled", "published"}:
            return None
        existing = session.scalar(
            select(ChannelPublication).where(
                ChannelPublication.job_id == job.job_id,
                ChannelPublication.channel == "tiktok",
            )
        )
        if existing:
            return existing
        target_utc = scheduled_for_utc or schedule.scheduled_for_utc
        target_utc = target_utc if target_utc.tzinfo else target_utc.replace(tzinfo=UTC)
        publication = ChannelPublication(
            publication_id=new_id(),
            job_id=job.job_id,
            channel="tiktok",
            schema_version=self.settings.schema_version,
            content_hash="",
            scheduled_for_utc=target_utc,
            timezone=schedule.timezone,
            status="scheduled",
            source=source,
            privacy_level=self.settings.tiktok_privacy_level,
        )
        self._refresh_channel_publication_hash(publication)
        session.add(publication)
        session.flush()
        self._persist_channel_publication_artifact(job, publication)
        self._append_event(
            job.job_id,
            "tiktok.publication_scheduled",
            "succeeded",
            {"publication_id": publication.publication_id, "source": source, "scheduled_for_utc": target_utc.isoformat()},
        )
        return publication

    def _retropost_day_bounds(self) -> tuple[datetime, datetime]:
        local_tz = ZoneInfo(self.settings.automation_daily_timezone)
        local_today = utcnow().astimezone(local_tz).date()
        start = datetime.combine(local_today, datetime.min.time(), tzinfo=local_tz).astimezone(UTC)
        end = start + timedelta(days=1)
        return start, end

    def _sync_tiktok_crosspost_queue(self) -> int:
        if not self._tiktok_auto_publish_enabled():
            return 0
        queued = 0
        now = utcnow()
        start, end = self._retropost_day_bounds()
        with session_scope() as session:
            scheduled_rows = session.execute(
                select(Job, PublicationSchedule)
                .join(PublicationSchedule, PublicationSchedule.job_id == Job.job_id)
                .where(PublicationSchedule.status == "scheduled")
                .where(
                    ~select(ChannelPublication.publication_id)
                    .where(ChannelPublication.job_id == Job.job_id)
                    .where(ChannelPublication.channel == "tiktok")
                    .exists()
                )
                .order_by(PublicationSchedule.scheduled_for_utc)
            ).all()
            for job, schedule in scheduled_rows:
                if self._ensure_tiktok_publication_for_schedule(session, job, schedule, source="youtube_schedule"):
                    queued += 1

            retropost_limit = max(0, int(self.settings.tiktok_retropost_daily_limit))
            retroposts_today = session.scalar(
                select(func.count())
                .select_from(ChannelPublication)
                .where(ChannelPublication.channel == "tiktok")
                .where(ChannelPublication.source == "retropost")
                .where(ChannelPublication.created_at >= start)
                .where(ChannelPublication.created_at < end)
            ) or 0
            remaining = max(0, retropost_limit - int(retroposts_today))
            if remaining:
                published_rows = session.execute(
                    select(Job, PublicationSchedule)
                    .join(PublicationSchedule, PublicationSchedule.job_id == Job.job_id)
                    .where(PublicationSchedule.status == "published")
                    .where(
                        ~select(ChannelPublication.publication_id)
                        .where(ChannelPublication.job_id == Job.job_id)
                        .where(ChannelPublication.channel == "tiktok")
                        .exists()
                    )
                    .order_by(PublicationSchedule.published_at.asc(), PublicationSchedule.updated_at.asc())
                    .limit(remaining)
                ).all()
                for job, schedule in published_rows:
                    if self._ensure_tiktok_publication_for_schedule(session, job, schedule, source="retropost", scheduled_for_utc=now):
                        queued += 1
        return queued

    def _claim_due_tiktok_publication(self) -> str | None:
        if not self._tiktok_auto_publish_enabled():
            return None
        now = utcnow()
        with session_scope() as session:
            claimable_id = (
                select(ChannelPublication.publication_id)
                .where(ChannelPublication.channel == "tiktok")
                .where(ChannelPublication.status == "scheduled")
                .where(ChannelPublication.scheduled_for_utc <= now)
                .order_by(ChannelPublication.scheduled_for_utc)
                .limit(1)
                .scalar_subquery()
            )
            claim = (
                update(ChannelPublication)
                .where(ChannelPublication.publication_id == claimable_id)
                .where(ChannelPublication.status == "scheduled")
                .values(status="publishing", updated_at=utcnow(), last_attempt_at=utcnow())
                .returning(ChannelPublication.publication_id)
            )
            publication_id = session.execute(claim).scalar_one_or_none()
            if not publication_id:
                return None
            publication = session.get(ChannelPublication, publication_id)
            job = session.get(Job, publication.job_id) if publication else None
            if job and publication:
                self._refresh_channel_publication_hash(publication)
                self._persist_channel_publication_artifact(job, publication)
            return publication_id

    def _publish_tiktok_channel_publication(self, publication_id: str) -> None:
        with session_scope() as session:
            publication = session.get(ChannelPublication, publication_id)
            if not publication:
                raise KeyError(publication_id)
            job = session.get(Job, publication.job_id)
            if not job:
                raise KeyError(publication.job_id)
            job_id = job.job_id
            package = self.monetization_pipeline.build_publish_package(session, job)
            video_uri = str(package.get("video_uri") or "")
            video_path = path_from_uri(video_uri)
            privacy_level = publication.privacy_level or self.settings.tiktok_privacy_level
            publication.attempt_count = int(publication.attempt_count or 0) + 1
            publication.last_attempt_at = utcnow()
        try:
            result = self.tiktok.direct_post_video(
                video_path=video_path,
                title=self._tiktok_caption(package),
                privacy_level=privacy_level,
                is_aigc=bool(package.get("altered_or_synthetic")),
                disable_comment=bool(self.settings.tiktok_disable_comment),
                disable_duet=bool(self.settings.tiktok_disable_duet),
                disable_stitch=bool(self.settings.tiktok_disable_stitch),
            )
        except (TikTokIntegrationError, httpx.HTTPError, OSError, FatalStepError) as exc:
            with session_scope() as session:
                publication = session.get(ChannelPublication, publication_id)
                job = session.get(Job, publication.job_id) if publication else None
                if publication:
                    publication.status = "publish_failed"
                    publication.last_error = str(exc)
                    publication.channel_metadata = {"error": str(exc)}
                    self._refresh_channel_publication_hash(publication)
                    if job:
                        self._persist_channel_publication_artifact(job, publication)
            self._append_event(job_id, "tiktok.publish_failed", "failed", {"publication_id": publication_id, "error": str(exc)})
            return
        with session_scope() as session:
            publication = session.get(ChannelPublication, publication_id)
            job = session.get(Job, publication.job_id) if publication else None
            if not publication:
                return
            publication.status = "processing"
            publication.external_id = str(result.get("publish_id") or "").strip() or None
            publication.last_error = None
            publication.channel_metadata = self._serialize_for_json(result)
            self._refresh_channel_publication_hash(publication)
            if job:
                self._persist_channel_publication_artifact(job, publication)
        self._append_event(job_id, "tiktok.publish_started", "succeeded", {"publication_id": publication_id, "publish_id": result.get("publish_id")})

    def _sync_tiktok_publication_statuses(self) -> int:
        if not self._tiktok_auto_publish_enabled() or not self.settings.tiktok_access_token:
            return 0
        with session_scope() as session:
            rows = session.execute(
                select(ChannelPublication.publication_id, ChannelPublication.external_id)
                .where(ChannelPublication.channel == "tiktok")
                .where(ChannelPublication.status == "processing")
                .where(ChannelPublication.external_id.is_not(None))
                .order_by(ChannelPublication.updated_at)
                .limit(10)
            ).all()
        synced = 0
        for publication_id, publish_id in rows:
            try:
                payload = self.tiktok.fetch_post_status(str(publish_id or ""))
            except (TikTokIntegrationError, httpx.HTTPError):
                continue
            status = str(payload.get("status") or "").upper()
            public_ids = payload.get("publicaly_available_post_id") or payload.get("publicly_available_post_id") or []
            final_status: str | None = None
            if status == "FAILED":
                final_status = "publish_failed"
            elif public_ids or status in {"PUBLISH_COMPLETE", "PUBLICLY_AVAILABLE", "SUCCESS"}:
                final_status = "published"
            if final_status is None:
                continue
            with session_scope() as session:
                publication = session.get(ChannelPublication, publication_id)
                job = session.get(Job, publication.job_id) if publication else None
                if not publication:
                    continue
                publication.status = final_status
                publication.channel_metadata = self._serialize_for_json(payload)
                if final_status == "published":
                    publication.published_at = utcnow()
                    publication.external_url = None
                    publication.last_error = None
                else:
                    publication.last_error = str(payload.get("fail_reason") or "TikTok publication failed")
                self._refresh_channel_publication_hash(publication)
                if job:
                    self._persist_channel_publication_artifact(job, publication)
            synced += 1
        return synced

    def _update_publication_artifact_index(self, job: Job) -> None:
        artifact_index = dict(job.artifact_index or {})
        artifact_index["youtube_publish_attempts"] = "youtube_publish_attempts.json"
        job_dir = self.storage.job_dir(job.job_id, create=False)
        if (job_dir / "publish_result.json").exists():
            artifact_index["publish_result"] = "publish_result.json"
        if (job_dir / "publish_package.json").exists():
            artifact_index["publish_package"] = "publish_package.json"
        if (job_dir / "publish_metadata_overrides.json").exists():
            artifact_index["publish_metadata_overrides"] = "publish_metadata_overrides.json"
        job.artifact_index = artifact_index

    def _retention_classification(self, job: Job, schedule: PublicationSchedule | None) -> str | None:
        schedule_status = str(schedule.status or "") if schedule else ""
        if job.status in RETENTION_EXCLUDED_JOB_STATUSES or schedule_status in RETENTION_EXCLUDED_SCHEDULE_STATUSES:
            return None
        if schedule_status == "scheduled":
            return "publishable"
        if schedule_status == "publish_failed":
            return "recoverable"
        if job.status in {"ready_for_upload", "approved_for_publish"}:
            return "publishable"
        if job.status in RETENTION_HARD_FAILURE_STATUSES:
            return "hard_failure"
        if job.status in RETENTION_RECOVERABLE_STATUSES:
            return "recoverable"
        return None

    def _retention_base_timestamp(self, job: Job, schedule: PublicationSchedule | None) -> datetime:
        timestamps = [_as_utc(job.updated_at) or _as_utc(job.created_at) or utcnow()]
        if schedule and schedule.updated_at:
            timestamps.append(_as_utc(schedule.updated_at) or utcnow())
        return max(timestamps)

    def _retention_ttl(self, classification: str) -> timedelta:
        if classification == "hard_failure":
            return timedelta(hours=self.settings.artifact_ttl_hard_failure_hours)
        if classification == "recoverable":
            return timedelta(hours=self.settings.artifact_ttl_recoverable_hours)
        return timedelta(hours=self.settings.artifact_ttl_publishable_hours)

    def _retention_metadata(
        self,
        job: Job,
        schedule: PublicationSchedule | None,
        *,
        now: datetime,
        cleaned: bool = False,
        cleaned_at: str | None = None,
        cleanup_reason: str | None = None,
    ) -> dict[str, Any] | None:
        classification = self._retention_classification(job, schedule)
        if not classification:
            return None
        base_timestamp = self._retention_base_timestamp(job, schedule)
        expires_at = base_timestamp + self._retention_ttl(classification)
        return {
            "classification": classification,
            "base_timestamp": base_timestamp.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_evaluated_at": now.isoformat(),
            "cleaned": cleaned,
            "cleaned_at": cleaned_at,
            "cleanup_reason": cleanup_reason,
        }

    def _set_retention_metadata(self, job: Job, metadata: dict[str, Any] | None) -> None:
        quality_summary = dict(job.quality_summary or {})
        if metadata is None:
            quality_summary.pop("retention", None)
        else:
            quality_summary["retention"] = metadata
        job.quality_summary = quality_summary

    def _retention_cleanup_snapshot(
        self,
        job: Job,
        schedule: PublicationSchedule | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        snapshots = {
            "monetization_report": self._read_job_json(job.job_id, "monetization_report.json"),
            "publish_package": self._read_job_json(job.job_id, "publish_package.json"),
            "publish_result": self._read_job_json(job.job_id, "publish_result.json"),
            "publication_attempts": self._read_job_json(job.job_id, "youtube_publish_attempts.json").get("attempts", []),
        }
        if schedule:
            snapshots["publication_schedule"] = self._publication_schedule_payload(schedule)
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "cleaned_at": metadata.get("cleaned_at"),
            "classification": metadata.get("classification"),
            "expires_at": metadata.get("expires_at"),
            "cleanup_reason": metadata.get("cleanup_reason"),
            "snapshots": snapshots,
        }

    def _cleanup_expired_job_artifacts(self, job_id: str) -> bool:
        if not self.settings.artifact_retention_enabled:
            return False
        now = utcnow()
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                return False
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            current_retention = dict((job.quality_summary or {}).get("retention") or {})
            if current_retention.get("cleaned"):
                return False
            metadata = current_retention or self._retention_metadata(job, schedule, now=now)
            self._set_retention_metadata(job, metadata)
            if not metadata:
                return False
            expires_at = _as_utc(datetime.fromisoformat(str(metadata["expires_at"]))) or now
            if expires_at > now:
                return False
            cleaned_at = iso_now()
            metadata = {
                **metadata,
                "cleaned": True,
                "cleaned_at": cleaned_at,
                "cleanup_reason": "ttl_expired",
                "last_evaluated_at": cleaned_at,
            }
            snapshot = self._retention_cleanup_snapshot(job, schedule, metadata)
            self.storage.remove_job_artifacts(job_id)
            self.storage.persist_json(job_id, "retention_cleanup.json", self._serialize_for_json(snapshot))
            self._set_retention_metadata(job, metadata)
            job.artifact_index = {"retention_cleanup": "retention_cleanup.json"}
        self._append_event(job_id, "job.artifacts.cleaned", "succeeded", {"reason": "ttl_expired"})
        return True

    def _refresh_retention_state(self, session: Session, job: Job, schedule: PublicationSchedule | None = None) -> None:
        if not self.settings.artifact_retention_enabled:
            return
        schedule = schedule if schedule is not None else session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job.job_id))
        current_retention = dict((job.quality_summary or {}).get("retention") or {})
        metadata = self._retention_metadata(job, schedule, now=utcnow())
        if current_retention.get("cleaned"):
            self._set_retention_metadata(
                job,
                {
                    **current_retention,
                    "last_evaluated_at": iso_now(),
                },
            )
            return
        self._set_retention_metadata(job, metadata)

    def _run_retention_sweep(self) -> int:
        if not self.settings.artifact_retention_enabled:
            return 0
        with session_scope() as session:
            rows = set(
                session.execute(
                select(Job.job_id).where(
                    Job.status.in_(
                        tuple(
                            RETENTION_HARD_FAILURE_STATUSES
                            | RETENTION_RECOVERABLE_STATUSES
                            | {"ready_for_upload", "approved_for_publish"}
                        )
                    )
                )
                ).scalars().all()
            )
            rows.update(
                session.execute(
                    select(PublicationSchedule.job_id).where(PublicationSchedule.status.in_(("scheduled", "publish_failed")))
                ).scalars().all()
            )
            for job_id in rows:
                job = session.get(Job, job_id)
                if job:
                    self._refresh_retention_state(session, job)
        cleaned = 0
        for job_id in rows:
            if self._cleanup_expired_job_artifacts(job_id):
                cleaned += 1
        self._last_retention_sweep_at = time.monotonic()
        return cleaned

    def _sync_monetization_report_from_quality_summary(self, job: Job) -> dict[str, Any] | None:
        summary = dict((job.quality_summary or {}).get("monetization") or {})
        if not summary:
            return None
        report = dict(self._read_job_json(job.job_id, "monetization_report.json") or {})
        report.update(
            {
                "schema_version": self.settings.schema_version,
                "job_id": job.job_id,
                "created_at": report.get("created_at") or iso_now(),
                "passed": bool(summary.get("passed")),
                "final_status": summary.get("final_status"),
                "hard_blockers": list(summary.get("hard_blockers") or []),
                "manual_required": list(summary.get("manual_required") or []),
                "warnings": list(summary.get("warnings") or []),
            }
        )
        self.storage.persist_json(job.job_id, "monetization_report.json", self._serialize_for_json(report))
        return report

    def _upload_publish_package(self, package: dict[str, Any], visibility: str) -> dict[str, Any]:
        video_uri = str(package.get("video_uri") or "").strip()
        if not video_uri:
            raise FatalStepError("publish package missing video_uri")
        try:
            upload = self.youtube.upload_video(
                video_path=path_from_uri(video_uri),
                title=str(package.get("title") or "") or "Short",
                description=str(package.get("description") or ""),
                tags=list(package.get("hashtags") or []),
                privacy_status=visibility,
                altered_or_synthetic=bool(package.get("altered_or_synthetic")),
            )
        except YouTubeIntegrationError as exc:
            raise FatalStepError(str(exc)) from exc
        return {
            "mode": "api",
            "api_enabled": True,
            "video_id": str(upload.get("id") or "").strip() or None,
            "url": upload.get("youtube_url"),
            "published_at": iso_now(),
            "response": upload,
            "target_visibility": visibility,
            "actual_visibility": ((upload.get("status") or {}).get("privacyStatus") if isinstance(upload.get("status"), dict) else None),
        }

    def _schedule_publish_package_on_youtube(self, package: dict[str, Any], scheduled_for_utc: datetime, visibility: str) -> dict[str, Any]:
        video_uri = str(package.get("video_uri") or "").strip()
        if not video_uri:
            raise FatalStepError("publish package missing video_uri")
        try:
            upload = self.youtube.upload_video(
                video_path=path_from_uri(video_uri),
                title=str(package.get("title") or "") or "Short",
                description=str(package.get("description") or ""),
                tags=list(package.get("hashtags") or []),
                privacy_status=visibility,
                altered_or_synthetic=bool(package.get("altered_or_synthetic")),
                publish_at=scheduled_for_utc,
            )
        except YouTubeIntegrationError as exc:
            raise FatalStepError(str(exc)) from exc
        return {
            "mode": "api",
            "api_enabled": True,
            "video_id": str(upload.get("id") or "").strip() or None,
            "url": upload.get("youtube_url"),
            "scheduled_for_utc": scheduled_for_utc.isoformat(),
            "response": upload,
            "target_visibility": visibility,
            "actual_visibility": ((upload.get("status") or {}).get("privacyStatus") if isinstance(upload.get("status"), dict) else None),
            "native_youtube_schedule": True,
        }

    def _reschedule_youtube_video(self, youtube_video_id: str, scheduled_for_utc: datetime) -> dict[str, Any]:
        try:
            response = self.youtube.schedule_published_video(video_id=youtube_video_id, publish_at=scheduled_for_utc)
        except YouTubeIntegrationError as exc:
            raise FatalStepError(str(exc)) from exc
        return {
            "mode": "api",
            "api_enabled": True,
            "video_id": youtube_video_id,
            "url": response.get("youtube_url"),
            "scheduled_for_utc": scheduled_for_utc.isoformat(),
            "response": response,
            "target_visibility": "public",
            "actual_visibility": ((response.get("status") or {}).get("privacyStatus") if isinstance(response.get("status"), dict) else None),
            "native_youtube_schedule": True,
        }

    def _clear_youtube_video_schedule(self, youtube_video_id: str) -> dict[str, Any]:
        try:
            response = self.youtube.clear_scheduled_publish(video_id=youtube_video_id)
        except YouTubeIntegrationError as exc:
            raise FatalStepError(str(exc)) from exc
        return {
            "mode": "api",
            "api_enabled": True,
            "video_id": youtube_video_id,
            "url": response.get("youtube_url"),
            "response": response,
            "native_youtube_schedule": True,
        }

    def _sync_native_scheduled_publications(self) -> int:
        if not self._youtube_api_mode_enabled():
            return 0
        now = utcnow()
        with session_scope() as session:
            rows = session.execute(
                select(PublicationSchedule.job_id, PublicationSchedule.youtube_video_id)
                .join(Job, Job.job_id == PublicationSchedule.job_id)
                .where(PublicationSchedule.status == "scheduled")
                .where(PublicationSchedule.youtube_video_id.is_not(None))
                .where(PublicationSchedule.scheduled_for_utc <= now)
                .where(Job.status == "approved_for_publish")
                .order_by(PublicationSchedule.scheduled_for_utc)
            ).all()
        synced = 0
        for job_id, youtube_video_id in rows:
            try:
                payload = self.youtube.fetch_video(str(youtube_video_id or ""))
            except YouTubeIntegrationError:
                continue
            status = dict(payload.get("status") or {})
            privacy_status = str(status.get("privacyStatus") or "").strip().lower()
            if privacy_status != "public":
                continue
            published_at = utcnow()
            with session_scope() as session:
                job = session.get(Job, job_id)
                schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
                if not job or not schedule or schedule.status != "scheduled":
                    continue
                schedule.status = "published"
                schedule.published_at = published_at
                schedule.youtube_video_id = str(youtube_video_id or "").strip() or None
                schedule.youtube_url = payload.get("youtube_url")
                schedule.content_hash = stable_hash(
                    {
                        "job_id": job_id,
                        "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                        "timezone": schedule.timezone,
                        "youtube_visibility": schedule.youtube_visibility,
                        "status": schedule.status,
                        "notes": schedule.notes,
                        "published_at": schedule.published_at.isoformat() if schedule.published_at else None,
                        "youtube_video_id": schedule.youtube_video_id,
                        "youtube_url": schedule.youtube_url,
                    }
                )
                self._persist_publication_schedule_artifact(job, schedule)
                job.status = "published"
                job.review_state = "published"
                quality_summary = dict(job.quality_summary or {})
                quality_summary["youtube_publish"] = {
                    "status": "published",
                    "last_attempt_at": iso_now(),
                    "mode": "api",
                    "video_id": schedule.youtube_video_id,
                    "youtube_url": schedule.youtube_url,
                }
                quality_summary["youtube"] = payload
                job.quality_summary = quality_summary
                self._update_publication_artifact_index(job)
                self._ensure_tiktok_publication_for_schedule(session, job, schedule, source="youtube_schedule")
                self._refresh_retention_state(session, job, schedule)
            self._append_publication_attempt(
                job_id,
                {
                    "attempt_id": new_id(),
                    "trigger": "youtube_schedule_sync",
                    "started_at": iso_now(),
                    "finished_at": iso_now(),
                    "status": "published",
                    "mode": "api",
                    "target_visibility": "public",
                    "youtube_video_id": youtube_video_id,
                    "youtube_url": payload.get("youtube_url"),
                },
            )
            self._append_event(job_id, "youtube.schedule.synced", "succeeded", {"video_id": youtube_video_id, "url": payload.get("youtube_url")})
            synced += 1
        return synced

    def _claim_due_publication_schedule(self) -> str | None:
        if not self._youtube_api_mode_enabled():
            return None
        now = utcnow()
        with session_scope() as session:
            claimable_job_id = (
                select(PublicationSchedule.job_id)
                .join(Job, Job.job_id == PublicationSchedule.job_id)
                .where(PublicationSchedule.status == "scheduled")
                .where(PublicationSchedule.youtube_video_id.is_(None))
                .where(PublicationSchedule.scheduled_for_utc <= now)
                .where(Job.status == "approved_for_publish")
                .order_by(PublicationSchedule.scheduled_for_utc)
                .limit(1)
                .scalar_subquery()
            )
            claim = (
                update(PublicationSchedule)
                .where(PublicationSchedule.job_id == claimable_job_id)
                .where(PublicationSchedule.status == "scheduled")
                .values(status="publishing", updated_at=utcnow())
                .returning(PublicationSchedule.job_id)
            )
            claimed_job_id = session.execute(claim).scalar_one_or_none()
            if not claimed_job_id:
                return None
            job = session.get(Job, claimed_job_id)
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == claimed_job_id))
            if job and schedule:
                self._persist_publication_schedule_artifact(job, schedule)
            return claimed_job_id

    def review_job(self, payload: dict[str, Any], job_id: str) -> str | None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            self._validate_review_action(job, payload["action"])
            review = ReviewRecord(
                review_id=new_id(),
                job_id=job_id,
                schema_version=self.settings.schema_version,
                content_hash=stable_hash(payload),
                created_at=utcnow(),
                reviewer_identity=payload["reviewer_identity"],
                action=payload["action"],
                reason_codes=payload.get("reason_codes", []),
                notes=payload.get("notes"),
                retry_step=None,
            )
            session.add(review)
            if payload["action"] == "approve":
                report = self.monetization_pipeline.build_monetization_report(session, job, set(payload.get("reason_codes") or []))
                if not report["passed"]:
                    self.storage.persist_json(job.job_id, "monetization_report.json", self._serialize_for_json(report))
                    quality_summary = dict(job.quality_summary or {})
                    quality_summary["monetization"] = {
                        "passed": report["passed"],
                        "final_status": report["final_status"],
                        "hard_blockers": report["hard_blockers"],
                        "manual_required": report["manual_required"],
                        "warnings": report["warnings"],
                        "content_hash": stable_hash(report),
                    }
                    job.quality_summary = quality_summary
                    job.status = report["final_status"]
                    self._refresh_retention_state(session, job)
                    raise FatalStepError(f"monetization readiness incomplete: {', '.join(report['hard_blockers'] + report['manual_required'])}")
                self.storage.persist_json(job.job_id, "monetization_report.json", self._serialize_for_json(report))
                quality_summary = dict(job.quality_summary or {})
                quality_summary["monetization"] = {
                    "passed": report["passed"],
                    "final_status": report["final_status"],
                    "hard_blockers": report["hard_blockers"],
                    "manual_required": report["manual_required"],
                    "warnings": report["warnings"],
                    "content_hash": stable_hash(report),
                }
                job.quality_summary = quality_summary
                job.status = "approved_for_publish"
                job.review_state = "approved"
                self.topic_pipeline.upsert_topic_registry(session, job_id, approved=True)
                self._refresh_retention_state(session, job)
                self._append_event(job_id, "review.approved", "succeeded", payload)
                return None
            if payload["action"] == "reject":
                job.status = "rejected"
                job.review_state = "rejected"
                self._refresh_retention_state(session, job)
                self._append_event(job_id, "review.rejected", "succeeded", payload)
                return None
            request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job_id))
            if not request:
                raise KeyError("missing topic request")
            clone_payload = {
                "seed_theme": request.seed_theme,
                "niche_id": request.niche_id,
                "language": request.language,
                "target_duration_sec": request.target_duration_sec,
                "tone": request.tone or "intrigante_direto",
                "cta_style": request.cta_style or "none",
                "notes": request.notes,
                "requested_angle": request.requested_angle,
            }
        new_job_id = self.owner.create_job(clone_payload, retry_of_job_id=job_id)
        self._append_event(
            job_id,
            "review.retry_requested",
            "succeeded",
            {"new_job_id": new_job_id, "retry_mode": "full_clone"},
        )
        return new_job_id

    def _validate_review_action(self, job: Job, action: str) -> None:
        reviewable_statuses = {"monetization_review", "blocked_for_monetization", "ready_for_upload"}
        retryable_statuses = {
            "monetization_review",
            "blocked_for_monetization",
            "rejected",
            "failed",
            "script_quality_failed",
            "scene_plan_quality_failed",
            "asset_quality_failed",
            "subtitle_quality_failed",
            "render_quality_failed",
        }
        rejectable_statuses = reviewable_statuses | retryable_statuses
        if action == "approve" and job.status not in reviewable_statuses:
            raise FatalStepError(f"job status {job.status} cannot be approved")
        if action == "reject" and job.status not in rejectable_statuses:
            raise FatalStepError(f"job status {job.status} cannot be rejected")
        if action == "retry" and job.status not in retryable_statuses:
            raise FatalStepError(f"job status {job.status} cannot be retried")

    def publish_job(
        self,
        job_id: str,
        youtube_video_id: str | None = None,
        youtube_url: str | None = None,
        *,
        trigger: str = "manual",
    ) -> None:
        attempt_id = new_id()
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status != "approved_for_publish":
                raise FatalStepError("job must be approved_for_publish before publishing")
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            monetization_report = self._read_job_json(job.job_id, "monetization_report.json")
            monetization_summary = dict((job.quality_summary or {}).get("monetization") or {})
            if monetization_summary.get("passed") is True and not monetization_report.get("passed"):
                monetization_report = self._sync_monetization_report_from_quality_summary(job) or monetization_report
            if monetization_report and not monetization_report.get("passed"):
                raise FatalStepError("job has not passed monetization readiness gate")
            if self._youtube_api_mode_enabled():
                self._ensure_youtube_api_ready()
            elif not (str(youtube_video_id or "").strip() or str(youtube_url or "").strip()):
                raise FatalStepError("manual publish requires youtube_video_id or youtube_url")
            package = self.monetization_pipeline.build_publish_package(session, job)
            published_at = utcnow()
            if schedule is None:
                schedule = PublicationSchedule(
                    schedule_id=new_id(),
                    job_id=job_id,
                    schema_version=self.settings.schema_version,
                    content_hash="",
                    created_at=published_at,
                    scheduled_for_utc=published_at,
                    timezone="UTC",
                    youtube_visibility="private",
                    status="scheduled" if self._youtube_api_mode_enabled() else "published",
                )
                session.add(schedule)
            if self._youtube_api_mode_enabled():
                schedule.status = "publishing"
                self._persist_publication_schedule_artifact(job, schedule)
            package_snapshot = self._serialize_for_json(package)
            visibility = schedule.youtube_visibility or "private"
            notes = schedule.notes

        started_at = iso_now()
        attempt_payload = {
            "attempt_id": attempt_id,
            "trigger": trigger,
            "started_at": started_at,
            "status": "started",
            "mode": "api" if self._youtube_api_mode_enabled() else "manual",
            "target_visibility": visibility,
            "notes": notes,
        }
        self._append_publication_attempt(job_id, attempt_payload)

        try:
            if self._youtube_api_mode_enabled():
                youtube_payload = self._upload_publish_package(package_snapshot, visibility)
                youtube_video_id = youtube_payload.get("video_id")
                youtube_url = youtube_payload.get("url")
            else:
                youtube_payload = {
                    "mode": self.settings.youtube_publish_mode,
                    "api_enabled": self.settings.youtube_api_enabled,
                    "video_id": str(youtube_video_id or "").strip() or None,
                    "url": str(youtube_url or "").strip() or None,
                    "published_at": iso_now(),
                }
        except Exception as exc:
            with session_scope() as session:
                job = session.get(Job, job_id)
                if not job:
                    raise
                schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
                if schedule is not None:
                    schedule.status = "publish_failed"
                    schedule.content_hash = stable_hash(
                        {
                            "job_id": job_id,
                            "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                            "timezone": schedule.timezone,
                            "youtube_visibility": schedule.youtube_visibility,
                            "status": schedule.status,
                            "notes": schedule.notes,
                        }
                    )
                    self._persist_publication_schedule_artifact(job, schedule)
                quality_summary = dict(job.quality_summary or {})
                quality_summary["youtube_publish"] = {
                    "status": "publish_failed",
                    "last_error": str(exc),
                    "last_attempt_at": iso_now(),
                }
                job.quality_summary = quality_summary
                self._update_publication_artifact_index(job)
                self._refresh_retention_state(session, job, schedule)
            self._append_publication_attempt(
                job_id,
                {
                    "attempt_id": attempt_id,
                    "trigger": trigger,
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "status": "failed",
                    "mode": "api" if self._youtube_api_mode_enabled() else "manual",
                    "target_visibility": visibility,
                    "error": str(exc),
                },
            )
            self._append_event(job_id, "youtube.publish_failed", "failed", {"error": str(exc), "trigger": trigger})
            if isinstance(exc, FatalStepError):
                raise
            raise FatalStepError(str(exc)) from exc

        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            published_at = utcnow()
            if schedule is None:
                schedule = PublicationSchedule(
                    schedule_id=new_id(),
                    job_id=job_id,
                    schema_version=self.settings.schema_version,
                    content_hash="",
                    created_at=published_at,
                    scheduled_for_utc=published_at,
                    timezone="UTC",
                    youtube_visibility=visibility,
                    status="published",
                )
                session.add(schedule)
            schedule.status = "published"
            schedule.published_at = published_at
            schedule.youtube_video_id = str(youtube_video_id or "").strip() or None
            schedule.youtube_url = str(youtube_url or "").strip() or None
            schedule.content_hash = stable_hash(
                {
                    "job_id": job_id,
                    "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                    "timezone": schedule.timezone,
                    "youtube_visibility": schedule.youtube_visibility,
                    "status": schedule.status,
                    "notes": schedule.notes,
                    "published_at": schedule.published_at.isoformat() if schedule.published_at else None,
                    "youtube_video_id": schedule.youtube_video_id,
                    "youtube_url": schedule.youtube_url,
                }
            )
            self._persist_publication_schedule_artifact(job, schedule)
            package_snapshot["status"] = "published"
            package_snapshot["published_at"] = published_at.isoformat()
            package_snapshot["youtube_video_id"] = schedule.youtube_video_id
            package_snapshot["youtube_url"] = schedule.youtube_url
            package_snapshot["publication_schedule"] = self._publication_schedule_payload(schedule)
            package_snapshot["youtube"] = youtube_payload
            self.storage.persist_json(job.job_id, "publish_result.json", self._serialize_for_json(package_snapshot))
            job.status = "published"
            job.review_state = "published"
            quality_summary = dict(job.quality_summary or {})
            quality_summary["youtube"] = youtube_payload
            quality_summary["youtube_publish"] = {
                "status": "published",
                "last_attempt_at": iso_now(),
                "mode": youtube_payload.get("mode"),
                "video_id": schedule.youtube_video_id,
                "youtube_url": schedule.youtube_url,
            }
            job.quality_summary = quality_summary
            self._update_publication_artifact_index(job)
            self._ensure_tiktok_publication_for_schedule(session, job, schedule, source="youtube_publish")
            self._refresh_retention_state(session, job, schedule)
        self._append_publication_attempt(
            job_id,
            {
                "attempt_id": attempt_id,
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": iso_now(),
                "status": "published",
                "mode": youtube_payload.get("mode"),
                "target_visibility": visibility,
                "youtube_video_id": youtube_video_id,
                "youtube_url": youtube_url,
            },
        )
        self._append_event(job_id, "youtube.published", "succeeded", {"video_id": youtube_video_id, "url": youtube_url, "trigger": trigger})

    def update_publish_metadata(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            overrides = self.monetization_pipeline.normalize_publish_metadata_overrides(
                payload.get("title"),
                payload.get("description"),
                payload.get("hashtags"),
            )
            persisted = {
                "schema_version": self.settings.schema_version,
                "job_id": job_id,
                "updated_at": iso_now(),
                **overrides,
            }
            self.storage.persist_json(job_id, "publish_metadata_overrides.json", self._serialize_for_json(persisted))
            package = self.monetization_pipeline.build_publish_package(session, job)
            self.storage.persist_json(job_id, "publish_package.json", self._serialize_for_json(package))
            self._refresh_retention_state(session, job)
            self._update_publication_artifact_index(job)
        self._append_event(
            job_id,
            "publish.metadata.updated",
            "succeeded",
            {
                "title": package.get("title"),
                "hashtags": package.get("hashtags"),
            },
        )
        return package

    def schedule_publication(self, job_id: str, payload: dict[str, Any]) -> None:
        validated = PublicationSchedulePayload(**payload)
        scheduled_for_utc = self._scheduled_local_to_utc(validated.scheduled_for_local, validated.timezone)
        if scheduled_for_utc <= utcnow():
            raise FatalStepError("scheduled publish time must be in the future")
        youtube_schedule_payload: dict[str, Any] | None = None
        youtube_video_id: str | None = None
        youtube_url: str | None = None
        had_existing_youtube_video = False
        if self._youtube_api_mode_enabled():
            self._ensure_youtube_api_ready()
            if validated.youtube_visibility != "public":
                raise FatalStepError("native YouTube scheduling currently requires visibility public")
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status != "approved_for_publish":
                raise FatalStepError("job must be approved_for_publish before entering the publication schedule")
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            package = self.monetization_pipeline.build_publish_package(session, job) if self._youtube_api_mode_enabled() and (schedule is None or not schedule.youtube_video_id) else None
            if schedule is not None:
                youtube_video_id = str(schedule.youtube_video_id or "").strip() or None
                youtube_url = str(schedule.youtube_url or "").strip() or None
                had_existing_youtube_video = youtube_video_id is not None
        if self._youtube_api_mode_enabled():
            if youtube_video_id:
                youtube_schedule_payload = self._reschedule_youtube_video(youtube_video_id, scheduled_for_utc)
            else:
                assert package is not None
                youtube_schedule_payload = self._schedule_publish_package_on_youtube(
                    package,
                    scheduled_for_utc,
                    validated.youtube_visibility,
                )
                youtube_video_id = str(youtube_schedule_payload.get("video_id") or "").strip() or None
                youtube_url = str(youtube_schedule_payload.get("url") or "").strip() or None
            attempt_started_at = iso_now()
            self._append_publication_attempt(
                job_id,
                {
                    "attempt_id": new_id(),
                    "trigger": "schedule_update" if had_existing_youtube_video else "schedule_upload",
                    "started_at": attempt_started_at,
                    "finished_at": attempt_started_at,
                    "status": "scheduled",
                    "mode": "api",
                    "target_visibility": validated.youtube_visibility,
                    "scheduled_for_utc": scheduled_for_utc.isoformat(),
                    "youtube_video_id": youtube_video_id,
                    "youtube_url": youtube_url,
                    "native_youtube_schedule": True,
                },
            )
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            if schedule is None:
                schedule = PublicationSchedule(
                    schedule_id=new_id(),
                    job_id=job_id,
                    schema_version=self.settings.schema_version,
                    content_hash="",
                    created_at=utcnow(),
                    scheduled_for_utc=scheduled_for_utc,
                    timezone=validated.timezone,
                    youtube_visibility=validated.youtube_visibility,
                    status="scheduled",
                    notes=validated.notes,
                )
                session.add(schedule)
            else:
                schedule.scheduled_for_utc = scheduled_for_utc
                schedule.timezone = validated.timezone
                schedule.youtube_visibility = validated.youtube_visibility
                schedule.status = "scheduled"
                schedule.notes = validated.notes
                schedule.published_at = None
            if self._youtube_api_mode_enabled():
                schedule.youtube_video_id = youtube_video_id
                schedule.youtube_url = youtube_url
            else:
                schedule.youtube_video_id = None
                schedule.youtube_url = None
            schedule.content_hash = stable_hash(
                {
                    "job_id": job_id,
                    "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                    "timezone": schedule.timezone,
                    "youtube_visibility": schedule.youtube_visibility,
                    "status": schedule.status,
                    "notes": schedule.notes,
                }
            )
            self._persist_publication_schedule_artifact(job, schedule)
            if self._youtube_api_mode_enabled():
                quality_summary = dict(job.quality_summary or {})
                quality_summary["youtube_publish"] = {
                    "status": "scheduled",
                    "last_attempt_at": iso_now(),
                    "mode": "api",
                    "video_id": schedule.youtube_video_id,
                    "youtube_url": schedule.youtube_url,
                    "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                    "native_youtube_schedule": True,
                }
                if youtube_schedule_payload is not None:
                    quality_summary["youtube"] = youtube_schedule_payload
                job.quality_summary = quality_summary
                self._update_publication_artifact_index(job)
            self._ensure_tiktok_publication_for_schedule(session, job, schedule, source="youtube_schedule")
            self._refresh_retention_state(session, job, schedule)
        self._append_event(
            job_id,
            "youtube.schedule.updated",
            "succeeded",
            {
                "scheduled_for_utc": scheduled_for_utc.isoformat(),
                "timezone": validated.timezone,
                "youtube_visibility": validated.youtube_visibility,
                "native_youtube_schedule": self._youtube_api_mode_enabled(),
                "youtube_video_id": youtube_video_id,
                "youtube_url": youtube_url,
            },
        )

    def clear_publication_schedule(self, job_id: str) -> None:
        youtube_video_id: str | None = None
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            if schedule is None:
                return
            if schedule.status == "published":
                raise FatalStepError("published job schedule cannot be cleared")
            if self._youtube_api_mode_enabled():
                youtube_video_id = str(schedule.youtube_video_id or "").strip() or None
        if self._youtube_api_mode_enabled() and youtube_video_id:
            self._clear_youtube_video_schedule(youtube_video_id)
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            if schedule is None:
                return
            schedule.status = "cancelled"
            schedule.content_hash = stable_hash(
                {
                    "job_id": job_id,
                    "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                    "timezone": schedule.timezone,
                    "youtube_visibility": schedule.youtube_visibility,
                    "status": schedule.status,
                    "notes": schedule.notes,
                }
            )
            self._persist_publication_schedule_artifact(job, schedule)
            channel_publication = session.scalar(
                select(ChannelPublication).where(ChannelPublication.job_id == job_id, ChannelPublication.channel == "tiktok")
            )
            if channel_publication and channel_publication.status in {"scheduled", "publishing", "processing", "publish_failed"}:
                channel_publication.status = "cancelled"
                channel_publication.last_error = None
                self._refresh_channel_publication_hash(channel_publication)
                self._persist_channel_publication_artifact(job, channel_publication)
            self._refresh_retention_state(session, job, schedule)
        self._append_event(job_id, "youtube.schedule.cleared", "succeeded", {"youtube_video_id": youtube_video_id})

    def reopen_publication_for_republish(self, job_id: str) -> None:
        reopened_at = iso_now()
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status != "published":
                raise FatalStepError("only published jobs can be reopened for republication")
            schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
            if schedule is None or schedule.status != "published":
                raise FatalStepError("published job is missing a published schedule record")
            previous_video_id = schedule.youtube_video_id
            previous_youtube_url = schedule.youtube_url
            schedule.status = "cancelled"
            schedule.published_at = None
            schedule.youtube_video_id = None
            schedule.youtube_url = None
            schedule.content_hash = stable_hash(
                {
                    "job_id": job_id,
                    "scheduled_for_utc": schedule.scheduled_for_utc.isoformat(),
                    "timezone": schedule.timezone,
                    "youtube_visibility": schedule.youtube_visibility,
                    "status": schedule.status,
                    "notes": schedule.notes,
                }
            )
            self._persist_publication_schedule_artifact(job, schedule)
            job.status = "approved_for_publish"
            job.review_state = "approved"
            quality_summary = dict(job.quality_summary or {})
            quality_summary["youtube_publish"] = {
                "status": "reopened_for_republish",
                "last_reopened_at": reopened_at,
                "previous_video_id": previous_video_id,
                "previous_youtube_url": previous_youtube_url,
            }
            job.quality_summary = quality_summary
            self._update_publication_artifact_index(job)
            self._refresh_retention_state(session, job, schedule)
        self._append_publication_attempt(
            job_id,
            {
                "attempt_id": new_id(),
                "trigger": "reopen_for_republish",
                "started_at": reopened_at,
                "finished_at": reopened_at,
                "status": "reopened_for_republish",
            },
        )
        self._append_event(job_id, "youtube.reopened_for_republish", "succeeded", {"status": "approved_for_publish"})

    def record_performance_metrics(self, job_id: str, payload: dict[str, Any]) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            metric_payload = {
                "metric_id": new_id(),
                "job_id": job_id,
                "schema_version": self.settings.schema_version,
                "created_at": utcnow(),
                **payload,
            }
            metric_payload["content_hash"] = stable_hash({key: value for key, value in metric_payload.items() if key != "created_at"})
            session.add(PerformanceMetric(**model_payload(PerformanceMetric, metric_payload)))
            session.flush()
            metrics = session.scalars(
                select(PerformanceMetric).where(PerformanceMetric.job_id == job_id).order_by(PerformanceMetric.created_at.desc())
            ).all()
            report = self.monetization_pipeline.build_job_performance_report(metrics)
            self.storage.persist_json(job_id, "performance_metrics.json", self._serialize_for_json(report))
            artifact_index = dict(job.artifact_index or {})
            artifact_index["performance_metrics"] = "performance_metrics.json"
            job.artifact_index = artifact_index
            quality_summary = dict(job.quality_summary or {})
            quality_summary["performance"] = report["latest"] or {}
            job.quality_summary = quality_summary
        self._append_event(job_id, "youtube.performance_recorded", "succeeded", payload)
