from __future__ import annotations

import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import concurrent.futures
import wave
import ast
import httpx
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import imageio_ffmpeg
from PIL import Image
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.audio.music_mix import mix_background_music
from app.audio.sound_design import generate_sound_design_track, mix_sound_design_track
from app.compliance.review import build_human_review_checklist
from app.config import get_settings
from app.db import SessionLocal, session_scope
from app.editorial.research_brief import audit_source_relevance, build_research_brief
from app.editorial.retention import attach_retention_metadata, enrich_plan_for_script_generation
from app.editorial.repetition import build_channel_repetition_report
from app.editorial.topic_mode import resolve_editorial_mode
from app.models import (
    BackgroundMusicAsset,
    ErrorLog,
    FallbackEvent,
    Job,
    NarrationAsset,
    PerformanceMetric,
    PublicationSchedule,
    RenderOutput,
    ReviewRecord,
    SceneAsset,
    ScenePlan,
    Script,
    StepExecution,
    SubtitleTrack,
    TopicPlan,
    TopicRegistry,
    TopicRequest,
)
from app.pipelines.common import FatalStepError, RecoverableStepError, model_payload
from app.providers import ProviderRegistry
from app.quality.asset_gate import AssetGate
from app.quality.render_gate import RenderGate
from app.quality.scene_gate import ScenePlanGate
from app.quality.script_gate import ScriptQualityGate
from app.quality.subtitle_gate import BAD_ENDINGS, SubtitleGate
from app.schemas import PublicationSchedulePayload, SUPPORTED_LANGUAGES, SUPPORTED_NICHES, TopicRequestCreate
from app.storage import StorageManager
from app.utils import (
    avg_words_per_sentence,
    cosineish_similarity,
    ensure_dir,
    file_uri,
    iso_now,
    jaccard_bigrams,
    ms_to_srt,
    new_id,
    parse_srt,
    path_from_uri,
    read_json,
    sentence_split,
    stable_hash,
    split_caption_chunks,
    tokenize,
    utcnow,
    word_tokens,
    wrap_caption,
    write_json,
)
from app.youtube_api import YouTubeIntegrationError, YouTubePublisher


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
        if isinstance(value, str):
            stripped = value.strip()
            lowered = stripped.lower()
            if lowered in {"true", "passed", "pass", "ok", "aprovado"}:
                value = True
            elif lowered in {"false", "failed", "fail", "reprovado"}:
                value = False
            else:
                fraction_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", stripped)
                percentage_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", stripped)
                if fraction_match:
                    denominator = float(fraction_match.group(2))
                    value = round(float(fraction_match.group(1)) / denominator, 3) if denominator > 0 else value
                elif percentage_match:
                    value = round(float(percentage_match.group(1)) / 100, 3)
                else:
                    try:
                        value = float(stripped.replace(",", "."))
                    except ValueError:
                        pass
        if isinstance(value, int | float) and 1 < value <= 10:
            normalized[key] = round(value / 10, 3)
            continue
        normalized[key] = value
    repetition_value = normalized.get("repetition_score")
    if repetition_value == 1:
        normalized["repetition_score"] = 0.1
        return normalized
    if isinstance(repetition_value, int | float) and 0.88 < repetition_value <= 1:
        normalized["repetition_score"] = round(max(0.0, 1 - repetition_value), 3)
    return normalized


NO_TEXT_IMAGE_CONSTRAINT = (
    "clean vertical cinematic scientific image, natural objects only, no readable text anywhere, "
    "no letters, no words, no numbers, no symbols, no logo, no watermark, no captions, "
    "no subtitles, no title card, no poster, no signs, no labels, no UI, no infographic, "
    "no typography, no diagrams with labels, no text printed on objects, no text on packages, "
    "no text on cups, no text on screens, no text on charts, no readable brand marks"
)

ENGLISH_SUBJECT_ALIASES = {
    "polvo": "octopus",
    "polvos": "octopuses",
    "buraco negro": "black hole",
    "buracos negros": "black holes",
    "vulcao": "volcano",
    "vulcoes": "volcanoes",
    "vulcão": "volcano",
    "vulcões": "volcanoes",
    "gato": "cat",
    "gatos": "cats",
    "felino": "cat",
    "felinos": "cats",
    "cafe": "coffee",
    "café": "coffee",
    "cafeina": "caffeine",
    "cafeína": "caffeine",
    "cafeina e foco": "caffeine and focus",
    "café e foco": "coffee and focus",
    "torre de pisa": "Leaning Tower of Pisa",
    "torre inclinada de pisa": "Leaning Tower of Pisa",
    "por que a torre de pisa não cai?": "Leaning Tower of Pisa",
    "por que a torre de pisa nao cai?": "Leaning Tower of Pisa",
}

SCENE_VISUAL_HINTS = [
    (("torre", "pisa", "séculos"), "the Leaning Tower of Pisa in Piazza dei Miracoli at golden hour, visibly tilted but stable, documentary realism"),
    (("torre", "pisa", "seculos"), "the Leaning Tower of Pisa in Piazza dei Miracoli at golden hour, visibly tilted but stable, documentary realism"),
    (("solo", "argiloso"), "cutaway view of the Leaning Tower of Pisa foundation resting on soft clay soil layers, unlabeled scientific visualization"),
    (("solo", "mole"), "cutaway view of the Leaning Tower of Pisa foundation resting on soft clay soil layers, unlabeled scientific visualization"),
    (("fundação",), "close vertical cutaway of a shallow medieval tower foundation settling into soft ground, documentary engineering realism"),
    (("fundacao",), "close vertical cutaway of a shallow medieval tower foundation settling into soft ground, documentary engineering realism"),
    (("centro", "massa"), "unlabeled visual metaphor of the Leaning Tower of Pisa balancing with its mass still over the base, no diagrams or text"),
    (("inclinação", "reduz"), "engineers stabilizing the base of the Leaning Tower of Pisa with careful soil extraction, documentary realism"),
    (("inclinacao", "reduz"), "engineers stabilizing the base of the Leaning Tower of Pisa with careful soil extraction, documentary realism"),
    (("cafeina", "foco"), "caffeine molecules near alert neurons in warm morning light, a plain unbranded coffee cup nearby"),
    (("cafeína", "foco"), "caffeine molecules near alert neurons in warm morning light, a plain unbranded coffee cup nearby"),
    (("cafe", "foco"), "plain unbranded coffee cup beside a focused morning workspace, subtle neural energy glow"),
    (("café", "foco"), "plain unbranded coffee cup beside a focused morning workspace, subtle neural energy glow"),
    (("adenosina",), "caffeine molecules blocking adenosine receptors on neurons, cinematic scientific visualization"),
    (("receptores",), "caffeine molecules fitting into neural receptors, cinematic scientific visualization"),
    (("sonolencia",), "sleep pressure fading from a human silhouette after caffeine reaches the brain, morning light"),
    (("sonolência",), "sleep pressure fading from a human silhouette after caffeine reaches the brain, morning light"),
    (("alerta",), "alert brain activity represented by glowing neural pathways beside plain coffee steam"),
    (("manhã",), "soft morning kitchen light with plain unbranded coffee steam and a person becoming alert in silhouette"),
    (("manha",), "soft morning kitchen light with plain unbranded coffee steam and a person becoming alert in silhouette"),
    (("gatos", "veem", "mundo diferente"), "cat face close-up with reflective eyes perceiving an altered night world"),
    (("terceiro", "párpado"), "macro close-up of a cat eye showing the translucent third eyelid protecting the eye"),
    (("terceiro", "parpado"), "macro close-up of a cat eye showing the translucent third eyelid protecting the eye"),
    (("orelha", "180"), "cat ears rotating independently toward subtle sound waves in a quiet room"),
    (("visão noturna",), "cat moving through a dim night scene with bright reflective eyes and low light visibility"),
    (("visao noturna",), "cat moving through a dim night scene with bright reflective eyes and low light visibility"),
    (("memória episódica",), "cat remembering a hidden toy location in a realistic home environment"),
    (("memoria episodica",), "cat remembering a hidden toy location in a realistic home environment"),
    (("cabeça", "180"), "cat turning its head sharply to monitor a distant threat, natural posture"),
    (("cabeca", "180"), "cat turning its head sharply to monitor a distant threat, natural posture"),
    (("corações", "sangue azul"), "octopus anatomy close-up showing three subtle hearts and blue copper-rich blood vessels"),
    (("coracoes", "sangue azul"), "octopus anatomy close-up showing three subtle hearts and blue copper-rich blood vessels"),
    (("hemocianina",), "blue oxygen-carrying blood flowing through octopus anatomy"),
    (("dna",), "octopus adapting underwater beside clean molecular DNA strands made of light"),
    (("células nervosas",), "octopus arms exploring rocks independently with subtle neural glow inside the tentacles"),
    (("celulas nervosas",), "octopus arms exploring rocks independently with subtle neural glow inside the tentacles"),
    (("tentáculo", "cortado"), "detached octopus arm moving reflexively on the seabed, natural biology, non-graphic"),
    (("tentaculo", "cortado"), "detached octopus arm moving reflexively on the seabed, natural biology, non-graphic"),
    (("cor", "textura", "predadores"), "octopus rapidly changing skin color and texture while camouflaging from a predator"),
]

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


@dataclass
class StepDefinition:
    name: str
    retries: int
    handler: Callable[[Session, Job, int], list[str]]


class JobOrchestrator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.storage = StorageManager()
        self.providers = ProviderRegistry()
        self.youtube = YouTubePublisher(self.settings)
        self._last_retention_sweep_at = 0.0
        from app.pipelines.asset_pipeline import AssetPipeline
        from app.pipelines.monetization_pipeline import MonetizationPipeline
        from app.pipelines.render_pipeline import RenderPipeline
        from app.pipelines.scene_pipeline import ScenePipeline
        from app.pipelines.script_pipeline import ScriptPipeline

        self.script_pipeline = ScriptPipeline(self)
        self.scene_pipeline = ScenePipeline(self)
        self.asset_pipeline = AssetPipeline(self)
        self.render_pipeline = RenderPipeline(self)
        self.monetization_pipeline = MonetizationPipeline(self)
        self.script_gate = ScriptQualityGate()
        self.scene_gate = ScenePlanGate()
        self.asset_gate = AssetGate()
        self.subtitle_gate = SubtitleGate()
        self.render_gate = RenderGate(min_bitrate=self.settings.render_min_bitrate)
        self.worker_id = f"worker-{new_id()[:8]}"
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

    def start_worker(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.stop_event = threading.Event()
        self.worker_thread = threading.Thread(target=self._worker_loop, name="yts-worker", daemon=True)
        self.worker_thread.start()

    def stop_worker(self) -> None:
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2)
        if self.worker_thread and not self.worker_thread.is_alive():
            self.worker_thread = None

    def _lease_delta(self) -> timedelta:
        return timedelta(seconds=max(300, self.settings.job_lease_seconds))

    def _start_lease_heartbeat(self, job_id: str) -> threading.Event:
        stop_heartbeat = threading.Event()
        interval = max(5.0, min(30.0, max(300, self.settings.job_lease_seconds) / 3))

        def heartbeat() -> None:
            while not stop_heartbeat.wait(interval):
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if not job or job.status != "running" or job.lease_owner != self.worker_id:
                        return
                    job.lease_expires_at = utcnow() + self._lease_delta()

        threading.Thread(target=heartbeat, name=f"yts-lease-{job_id[:8]}", daemon=True).start()
        return stop_heartbeat

    def _persist_repair_telemetry(self, job_id: str, stage: str, payload: dict[str, Any]) -> str:
        filename = f"{stage}_repair_telemetry.json"
        self.storage.persist_json(job_id, filename, self._serialize_for_json(payload))
        return filename

    def create_job(self, payload: dict[str, Any], retry_of_job_id: str | None = None) -> str:
        payload = TopicRequestCreate.model_validate(payload).model_dump()
        now = utcnow()
        job_id = new_id()
        topic_request_id = new_id()
        request_data = {
            "schema_version": self.settings.schema_version,
            "topic_request_id": topic_request_id,
            "job_id": job_id,
            "content_hash": stable_hash(payload),
            "created_at": now,
            **payload,
        }
        with session_scope() as session:
            job = Job(
                job_id=job_id,
                schema_version=self.settings.schema_version,
                content_hash=stable_hash(
                    {
                        "seed_theme": payload["seed_theme"],
                        "target_duration_sec": payload["target_duration_sec"],
                        "language": payload["language"],
                    }
                ),
                status="queued",
                current_step=None,
                niche_id=payload["niche_id"],
                language=payload["language"],
                target_duration_sec=payload["target_duration_sec"],
                topic_request_id=topic_request_id,
                retry_of_job_id=retry_of_job_id,
                artifact_index={},
            )
            topic_request = TopicRequest(**request_data)
            session.add(job)
            session.add(topic_request)
            self._append_event(job_id, "job.created", "succeeded", {"seed_theme": payload["seed_theme"]})
            self.storage.persist_json(job_id, "request.json", self._serialize_for_json(request_data))
        return job_id

    def process_job(self, job_id: str) -> str:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status in {
                "approved",
                "approved_for_publish",
                "ready_for_upload",
                "monetization_review",
                "blocked_for_monetization",
                "published",
                "failed",
                "script_quality_failed",
                "scene_plan_quality_failed",
                "asset_quality_failed",
                "subtitle_quality_failed",
                "render_quality_failed",
                "cancelled",
            }:
                return job.status
            job.status = "running"
            job.lease_owner = self.worker_id
            job.lease_expires_at = utcnow() + self._lease_delta()
        steps = self._steps()
        self._cli_progress(job_id, "job", "started", f"{len(steps)} steps")
        for step_index, step in enumerate(steps, start=1):
            ok = self._run_step(job_id, step, step_index=step_index, total_steps=len(steps))
            if not ok:
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if not job:
                        raise KeyError(job_id)
                    self._cli_progress(job_id, "job", "stopped", f"status={job.status}")
                    return job.status
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job
            monetization = (job.quality_summary or {}).get("monetization", {})
            job.status = str(monetization.get("final_status") or "monetization_review")
            job.current_step = "publish_to_review_hub"
            job.lease_owner = None
            job.lease_expires_at = None
            self._upsert_topic_registry(session, job_id, approved=False)
            self._refresh_retention_state(session, job)
        self._append_event(job_id, "render.completed", "succeeded", {"status": job.status})
        self._cli_progress(job_id, "job", "finished", f"status={job.status}")
        return job.status

    def get_job_details(self, session: Session, job_id: str) -> dict[str, Any]:
        job = session.get(Job, job_id)
        if not job:
            raise KeyError(job_id)
        retention = dict((job.quality_summary or {}).get("retention") or {})
        retention_cleanup = self._read_job_json(job_id, "retention_cleanup.json")
        artifacts_cleaned = bool(retention.get("cleaned") or retention_cleanup)
        topic_request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job_id))
        script = session.scalar(select(Script).where(Script.job_id == job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job_id))
        publication_schedule = session.scalar(select(PublicationSchedule).where(PublicationSchedule.job_id == job_id))
        assets = session.scalars(select(SceneAsset).where(SceneAsset.job_id == job_id).order_by(SceneAsset.scene_id, SceneAsset.provider)).all()
        fallbacks = session.scalars(select(FallbackEvent).where(FallbackEvent.job_id == job_id).order_by(FallbackEvent.created_at)).all()
        errors = session.scalars(select(ErrorLog).where(ErrorLog.job_id == job_id).order_by(ErrorLog.created_at)).all()
        reviews = session.scalars(select(ReviewRecord).where(ReviewRecord.job_id == job_id).order_by(ReviewRecord.created_at)).all()
        cleanup_snapshots = dict(retention_cleanup.get("snapshots") or {})
        if artifacts_cleaned:
            render = None
            narration = None
            subtitles = None
            background_music = None
            assets = []
        repair_telemetry = {
            "topic_plan": self._read_job_json(job_id, "topic_plan_repair_telemetry.json"),
            "script": self._read_job_json(job_id, "script_repair_telemetry.json"),
            "background_music": self._read_job_json(job_id, "background_music_repair_telemetry.json"),
            "render": self._read_job_json(job_id, "render_repair_telemetry.json"),
        }
        if artifacts_cleaned:
            repair_telemetry = {}
        return {
            "job": job,
            "topic_request": topic_request,
            "topic_plan": topic_plan,
            "script": script,
            "scene_plan": scene_plan,
            "assets": assets,
            "narration": narration,
            "subtitles": subtitles,
            "background_music": background_music,
            "render": render,
            "publication_schedule": publication_schedule,
            "fallbacks": fallbacks,
            "errors": errors,
            "reviews": reviews,
            "performance_metrics": session.scalars(
                select(PerformanceMetric).where(PerformanceMetric.job_id == job_id).order_by(PerformanceMetric.created_at.desc())
            ).all(),
            "repair_telemetry": repair_telemetry,
            "events": self._read_events(job_id),
            "monetization_report": self._read_job_json(job_id, "monetization_report.json") or cleanup_snapshots.get("monetization_report", {}),
            "publish_package": self._read_job_json(job_id, "publish_package.json") or cleanup_snapshots.get("publish_package", {}),
            "publish_result": self._read_job_json(job_id, "publish_result.json") or cleanup_snapshots.get("publish_result", {}),
            "publication_attempts": self._read_job_json(job_id, "youtube_publish_attempts.json").get("attempts", []) or cleanup_snapshots.get("publication_attempts", []),
            "retention_cleanup": retention_cleanup,
            "artifacts_cleaned": artifacts_cleaned,
        }

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
                report = self._build_monetization_report(session, job, set(payload.get("reason_codes") or []))
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
                self._upsert_topic_registry(session, job_id, approved=True)
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
        new_job_id = self.create_job(clone_payload, retry_of_job_id=job_id)
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
            if job.status not in {"approved_for_publish", "published"}:
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
            package = self._build_publish_package(session, job)
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
            package_snapshot["youtube"] = youtube_payload
            self.storage.persist_json(job.job_id, "publish_result.json", self._serialize_for_json(package_snapshot))
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
            package = self._build_publish_package(session, job)
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
            package = self._build_publish_package(session, job) if self._youtube_api_mode_enabled() and (schedule is None or not schedule.youtube_video_id) else None
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
            report = self._build_job_performance_report(metrics)
            self.storage.persist_json(job_id, "performance_metrics.json", self._serialize_for_json(report))
            artifact_index = dict(job.artifact_index or {})
            artifact_index["performance_metrics"] = "performance_metrics.json"
            job.artifact_index = artifact_index
            quality_summary = dict(job.quality_summary or {})
            quality_summary["performance"] = report["latest"] or {}
            job.quality_summary = quality_summary
        self._append_event(job_id, "youtube.performance_recorded", "succeeded", payload)

    def _steps(self) -> list[StepDefinition]:
        return [
            StepDefinition("input_gate", 0, self._step_input_gate),
            StepDefinition("topic_plan", 2, self._step_topic_plan),
            StepDefinition("script", 2, self.script_pipeline.step_script),
            StepDefinition("scene_plan", 1, self.scene_pipeline.step_scene_plan),
            StepDefinition("asset_generation", 2, self.asset_pipeline.step_assets),
            StepDefinition("tts", 2, self.asset_pipeline.step_tts),
            StepDefinition("subtitle_alignment", 1, self.asset_pipeline.step_subtitles),
            StepDefinition("background_music", 1, self.asset_pipeline.step_background_music),
            StepDefinition("render", 1, self.render_pipeline.step_render),
            StepDefinition("monetization_readiness_gate", 0, self.monetization_pipeline.step_monetization_readiness),
            StepDefinition("publish_to_review_hub", 0, self.monetization_pipeline.step_publish),
        ]

    def _run_step(self, job_id: str, step: StepDefinition, step_index: int | None = None, total_steps: int | None = None) -> bool:
        for attempt in range(1, step.retries + 2):
            if self.stop_event.is_set():
                self._cancel_job(job_id, step.name, "worker shutdown requested before retry")
                return False
            started = time.monotonic()
            step_label = f"{step_index}/{total_steps} " if step_index and total_steps else ""
            with session_scope() as session:
                job = session.get(Job, job_id)
                assert job
                input_hash = stable_hash(self._build_step_input(session, job, step.name))
                cached = session.scalar(
                    select(StepExecution).where(
                        StepExecution.job_id == job_id,
                        StepExecution.step_name == step.name,
                        StepExecution.input_hash == input_hash,
                        StepExecution.status == "succeeded",
                    )
                )
                if cached:
                    job.current_step = step.name
                    self._cli_progress(job_id, step.name, "cached", f"{step_label}attempt={attempt}")
                    return True
                execution = StepExecution(
                    execution_id=new_id(),
                    job_id=job_id,
                    step_name=step.name,
                    attempt=attempt,
                    status="running",
                    input_hash=input_hash,
                    output_refs=[],
                    started_at=utcnow(),
                )
                session.add(execution)
                job.current_step = step.name
                job.lease_owner = self.worker_id
                job.lease_expires_at = utcnow() + self._lease_delta()
            self._cli_progress(job_id, step.name, "started", f"{step_label}attempt={attempt}/{step.retries + 1}")
            heartbeat_stop = self._start_lease_heartbeat(job_id)
            try:
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    assert job
                    refs = step.handler(session, job, attempt)
                    execution = session.scalar(
                        select(StepExecution).where(
                            StepExecution.job_id == job_id,
                            StepExecution.step_name == step.name,
                            StepExecution.attempt == attempt,
                        )
                    )
                    assert execution
                    execution.status = "succeeded"
                    execution.output_refs = refs
                    execution.finished_at = utcnow()
                    job.current_step = step.name
                self._persist_performance_timeline(job_id)
                if step.name == "script":
                    self.asset_pipeline.start_background_music_prefetch(job_id)
                elapsed = time.monotonic() - started
                self._cli_progress(job_id, step.name, "done", f"{step_label}{elapsed:.1f}s")
                return True
            except RecoverableStepError as exc:
                elapsed = time.monotonic() - started
                self._cli_progress(job_id, step.name, "retry" if attempt <= step.retries else "failed", f"{step_label}{elapsed:.1f}s {exc}")
                self._record_step_failure(job_id, step.name, attempt, str(exc), recoverable=True)
                if attempt <= step.retries:
                    if self.stop_event.is_set():
                        self._cancel_job(job_id, step.name, "worker shutdown requested during recoverable retry")
                        return False
                    continue
                self._fail_job(job_id, step.name, str(exc))
                return False
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - started
                self._cli_progress(job_id, step.name, "failed", f"{step_label}{elapsed:.1f}s {type(exc).__name__}: {exc}")
                self._record_step_failure(job_id, step.name, attempt, str(exc), recoverable=False)
                self._fail_job(job_id, step.name, str(exc))
                return False
            finally:
                heartbeat_stop.set()
        return False

    def _cli_progress(self, job_id: str, stage: str, state: str, detail: str = "") -> None:
        timestamp = utcnow().strftime("%H:%M:%S")
        suffix = f" {detail}" if detail else ""
        print(f"[yts {timestamp}] job={job_id[:8]} stage={stage} {state}{suffix}", flush=True)

    def _record_step_failure(self, job_id: str, step_name: str, attempt: int, message: str, recoverable: bool) -> None:
        with session_scope() as session:
            execution = session.scalar(
                select(StepExecution).where(
                    StepExecution.job_id == job_id,
                    StepExecution.step_name == step_name,
                    StepExecution.attempt == attempt,
                )
            )
            if execution:
                execution.status = "failed"
                execution.finished_at = utcnow()
            session.add(
                ErrorLog(
                    error_id=new_id(),
                    job_id=job_id,
                    schema_version=self.settings.schema_version,
                    content_hash=stable_hash(message),
                    created_at=utcnow(),
                    step=step_name,
                    severity="warn" if recoverable else "fatal",
                    error_code=f"{step_name}_error",
                    message=message,
                    recoverable=recoverable,
                    attempt=attempt,
                )
            )
        self._persist_performance_timeline(job_id)
        self._append_event(job_id, f"{step_name}.failed", "failed", {"attempt": attempt, "message": message})

    def _persist_performance_timeline(self, job_id: str) -> None:
        with session_scope() as session:
            rows = session.scalars(
                select(StepExecution)
                .where(StepExecution.job_id == job_id)
                .order_by(StepExecution.started_at, StepExecution.step_name, StepExecution.attempt)
            ).all()
        steps: list[dict[str, Any]] = []
        total_ms = 0
        for row in rows:
            duration_ms = None
            if row.finished_at and row.started_at:
                duration_ms = max(0, round((row.finished_at - row.started_at).total_seconds() * 1000))
                if row.status == "succeeded":
                    total_ms += duration_ms
            steps.append(
                {
                    "step_name": row.step_name,
                    "attempt": row.attempt,
                    "status": row.status,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "duration_ms": duration_ms,
                    "output_refs": row.output_refs or [],
                }
            )
        self.storage.persist_json(
            job_id,
            "performance_timeline.json",
            {
                "job_id": job_id,
                "created_at": iso_now(),
                "total_succeeded_step_duration_ms": total_ms,
                "steps": steps,
            },
        )

    def _fail_job(self, job_id: str, step_name: str, message: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job
            job.status = self._failure_status_for_step(step_name, message)
            job.failure_reason = f"{step_name}: {message}"
            job.lease_owner = None
            job.lease_expires_at = None
        self._append_event(job_id, "job.failed", "failed", {"step": step_name, "message": message})

    def _cancel_job(self, job_id: str, step_name: str, message: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job
            job.status = "cancelled"
            job.failure_reason = f"{step_name}: {message}"
            job.lease_owner = None
            job.lease_expires_at = None
        self._append_event(job_id, "job.cancelled", "cancelled", {"step": step_name, "message": message})

    def _failure_status_for_step(self, step_name: str, message: str) -> str:
        if "quality gate" in message or "gate failed" in message:
            return {
                "script": "script_quality_failed",
                "scene_plan": "scene_plan_quality_failed",
                "asset_generation": "asset_quality_failed",
                "subtitle_alignment": "subtitle_quality_failed",
                "render": "render_quality_failed",
            }.get(step_name, "failed")
        return "failed"

    def _build_step_input(self, session: Session, job: Job, step_name: str) -> dict[str, Any]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job.job_id))
        return {
            "step": step_name,
            "job_id": job.job_id,
            "request": request.seed_theme if request else None,
            "request_notes_hash": stable_hash(request.notes or "") if request else None,
            "topic_plan": topic_plan.content_hash if topic_plan else None,
            "script": script.content_hash if script else None,
            "scene_plan": scene_plan.content_hash if scene_plan else None,
            "narration": narration.content_hash if narration else None,
            "subtitles": subtitles.content_hash if subtitles else None,
            "background_music": background_music.content_hash if background_music else None,
            "render": render.content_hash if render else None,
            "monetization": (job.quality_summary or {}).get("monetization", {}).get("content_hash"),
        }

    def _step_input_gate(self, session: Session, job: Job, attempt: int) -> list[str]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert request
        if request.niche_id not in SUPPORTED_NICHES:
            raise FatalStepError(f"unsupported niche_id: {request.niche_id}")
        normalized_theme = str(request.seed_theme or "").strip()
        if len(normalized_theme) < 3:
            raise FatalStepError("seed_theme too short after normalization")
        if request.target_duration_sec < 35 or request.target_duration_sec > 55:
            raise FatalStepError(f"target_duration_sec outside supported range: {request.target_duration_sec}")
        normalized_language = str(request.language or "").strip().lower().replace("_", "-")
        resolved_language = {
            "pt-br": "pt-BR",
            "portuguese-br": "pt-BR",
            "ptbr": "pt-BR",
        }.get(normalized_language)
        if resolved_language not in SUPPORTED_LANGUAGES:
            raise FatalStepError(f"unsupported language: {request.language}")
        moderation_match = self._input_moderation_block_match(
            " ".join(
                part
                for part in [
                    normalized_theme,
                    str(request.requested_angle or "").strip(),
                    str(request.notes or "").strip(),
                ]
                if part
            )
        )
        if moderation_match:
            raise FatalStepError(f"input blocked by moderation: {moderation_match}")
        quality = {
            "schema_valid": True,
            "niche_supported": True,
            "language": resolved_language,
            "target_duration_sec": request.target_duration_sec,
            "seed_theme_length": len(normalized_theme),
            "moderation_ok": True,
        }
        self.storage.persist_json(job.job_id, "input_gate.json", self._serialize_for_json(quality))
        self._append_event(job.job_id, "input_gate.passed", "succeeded", quality)
        return ["request.json", "input_gate.json"]

    def _input_moderation_block_match(self, surface: str) -> str | None:
        normalized = unicodedata.normalize("NFKD", surface).encode("ascii", "ignore").decode("ascii").lower()
        patterns = {
            "terrorism": r"\bterroris(?:mo|t)\b",
            "explosive_instructions": r"\b(?:bomba caseira|fabricar bomba|explosiv(?:o|a|os|as))\b",
            "mass_harm": r"\b(?:massacre|atirar em escola|explodir escola)\b",
            "self_harm": r"\b(?:suicid(?:io|a)|autoagress)\b",
            "child_abuse": r"\b(?:abuso infantil|exploracao infantil)\b",
            "hate_targeting": r"\b(?:odio contra|matar (?:gays|negros|judeus|mulheres))\b",
        }
        for reason, pattern in patterns.items():
            if re.search(pattern, normalized):
                return reason
        return None

    def _step_topic_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert request
        history = self._recent_topic_history(session, request.niche_id)
        plan, topic_metrics = self._generate_topic_plan_with_repair(
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

    def _recent_topic_history(self, session: Session, niche_id: str, limit: int = 30) -> list[dict[str, Any]]:
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

    def _channel_learning_brief(self, session: Session, niche_id: str, limit: int = 30) -> dict[str, Any]:
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

    def _generate_topic_plan_with_repair(
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
            plan = self._normalize_topic_plan_payload(plan, request)
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

    def _normalize_topic_plan_payload(self, plan: dict[str, Any], request: TopicRequest) -> dict[str, Any]:
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

    def _build_research_brief(self, topic_plan: Any, request: Any) -> dict[str, Any]:
        return build_research_brief(topic_plan, request)

    def _audit_source_relevance(self, research_brief: dict[str, Any], title: str, extract: str) -> dict[str, Any]:
        return audit_source_relevance(research_brief, title, extract)

    def _step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.script_pipeline.step_script(session, job, attempt)

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
        self.script_pipeline._persist_script_generation_debug(
            job_id=job_id,
            attempt=attempt,
            plan_dict=plan_dict,
            fact_pack=fact_pack,
            phase=phase,
            elapsed_ms=elapsed_ms,
            script=script,
            metrics=metrics,
            error=error,
        )

    def _build_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest) -> dict[str, Any]:
        return self.script_pipeline._build_fact_pack(topic_plan, request)

    def _fact_query_priority(self, query: str) -> tuple[int, int, int, int]:
        return self.script_pipeline._fact_query_priority(query)

    def _is_weak_fact_query(self, query: str) -> bool:
        return self.script_pipeline._is_weak_fact_query(query)


    def _fact_pack_queries(self, request: TopicRequest, topic_plan: TopicPlan) -> list[str]:
        return self.script_pipeline._fact_pack_queries(request, topic_plan)

    def _fact_query_source_texts(self, value: Any) -> list[str]:
        return self.script_pipeline._fact_query_source_texts(value)

    def _clean_fact_query(self, query: str) -> str:
        return self.script_pipeline._clean_fact_query(query)

    def _extract_fact_entity(self, query: str) -> str:
        return self.script_pipeline._extract_fact_entity(query)

    def _fact_query_concepts(self, query: str) -> list[str]:
        return self.script_pipeline._fact_query_concepts(query)

    def _normalize_fact_text(self, text: str) -> str:
        return self.script_pipeline._normalize_fact_text(text)

    def _fact_result_is_relevant(
        self,
        query: str,
        title: str,
        extract: str,
        research_brief: dict[str, Any] | None = None,
    ) -> bool:
        return self.script_pipeline._fact_result_is_relevant(query, title, extract, research_brief)

    def _scientific_article_fact_pack(self, query: str, research_brief: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.script_pipeline._scientific_article_fact_pack(query, research_brief)

    def _openalex_abstract_text(self, abstract_inverted_index: Any) -> str:
        return self.script_pipeline._openalex_abstract_text(abstract_inverted_index)


    def _fact_pack_consistency_reasons(self, script: dict[str, Any], fact_pack: Any) -> list[str]:
        return self.script_pipeline._fact_pack_consistency_reasons(script, fact_pack)

    def _apply_cta_policy(self, script: dict[str, Any], cta_style: str) -> dict[str, Any]:
        return self.script_pipeline._apply_cta_policy(script, cta_style)

    def _attach_editorial_source(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> dict[str, Any]:
        return self.script_pipeline._attach_editorial_source(script, plan_dict)

    def _postprocess_script_for_quality(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        gate_reasons: list[str],
    ) -> dict[str, Any]:
        return self.script_pipeline._postprocess_script_for_quality(script, plan_dict, gate_reasons)

    def _repair_common_script_text_issues(self, value: Any) -> Any:
        return self.script_pipeline._repair_common_script_text_issues(value)

    def _normalize_script_narration_fields(self, script: dict[str, Any]) -> dict[str, Any]:
        return self.script_pipeline._normalize_script_narration_fields(script)

    def _split_long_script_sentences(self, script: dict[str, Any]) -> dict[str, Any]:
        return self.script_pipeline._split_long_script_sentences(script)

    def _should_force_conservative_fact_rewrite(
        self,
        script: dict[str, Any],
        fact_pack: dict[str, Any],
        gate_reasons: list[str],
    ) -> bool:
        return self.script_pipeline._should_force_conservative_fact_rewrite(script, fact_pack, gate_reasons)  # noqa: SLF001

    def _should_repair_loop(self, script: dict[str, Any], gate_reasons: list[str]) -> bool:
        return self.script_pipeline._should_repair_loop(script, gate_reasons)  # noqa: SLF001

    def _rewrite_script_conservatively(
        self,
        script: dict[str, Any],
        fact_pack: dict[str, Any],
        plan_dict: dict[str, Any],
    ) -> dict[str, Any]:
        return self.script_pipeline._rewrite_script_conservatively(script, fact_pack, plan_dict)

    def _soften_risky_sentence(self, sentence: str, anchor: str) -> str:
        return self.script_pipeline._soften_risky_sentence(sentence, anchor)

    def _repair_script_loop_closure(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> dict[str, Any]:
        return self.script_pipeline._repair_script_loop_closure(script, plan_dict)

    def _script_anchor_phrase(self, script: dict[str, Any], plan_dict: dict[str, Any]) -> str:
        return self.script_pipeline._script_anchor_phrase(script, plan_dict)

    def _validate_or_repair_script(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        target_duration_sec: int,
        cta_style: str = "none",
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.script_pipeline._validate_or_repair_script(script, plan_dict, target_duration_sec, cta_style, job_id)

    def _persist_script_rejection(self, job_id: str | None, script: dict[str, Any], gate_metrics: dict[str, Any], consistency_reasons: list[str]) -> None:
        self.script_pipeline._persist_script_rejection(job_id, script, gate_metrics, consistency_reasons)

    def _step_scene_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.scene_pipeline.step_scene_plan(session, job, attempt)

    def _scene_fallback_planner(self) -> Any:
        return self.scene_pipeline.scene_fallback_planner()

    def _normalize_scene_token_coverage(self, scenes: list[dict[str, Any]], full_narration: str) -> list[dict[str, Any]]:
        return self.scene_pipeline.normalize_scene_token_coverage(scenes, full_narration)

    def _step_assets(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.asset_pipeline.step_assets(session, job, attempt)

    def _generate_primary_asset(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        return self.asset_pipeline._generate_primary_asset(scene, output_path)

    def _normalize_asset_uri_extension(self, asset: dict[str, Any]) -> dict[str, Any]:
        return self.asset_pipeline._normalize_asset_uri_extension(asset)

    def _score_asset(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        return self.asset_pipeline._score_asset(scene, asset)

    def _asset_scores_pass(self, scores: dict[str, Any]) -> bool:
        return self.asset_pipeline._asset_scores_pass(scores)

    def _image_prompt_variants(self, scene: dict[str, Any], regeneration_round: int = 1) -> list[dict[str, Any]]:
        return self.asset_pipeline._image_prompt_variants(scene, regeneration_round)

    def _normalize_scene_semantics(self, scene: dict[str, Any], canonical_topic: str) -> dict[str, Any]:
        return self.scene_pipeline.normalize_scene_semantics(scene, canonical_topic)

    def _semantic_english_image_prompt(self, scene: dict[str, Any], topic_text: str, primary_subject: str) -> str:
        return self.asset_pipeline._semantic_english_image_prompt(scene, topic_text, primary_subject)

    def _english_subject_hint(self, topic_text: str, primary_subject: str) -> str:
        return self.asset_pipeline._english_subject_hint(topic_text, primary_subject)

    def _english_scene_visual_hint(self, scene: dict[str, Any], english_subject: str) -> str:
        return self.asset_pipeline._english_scene_visual_hint(scene, english_subject)

    def _semantic_scene_directive(self, scene: dict[str, Any], scene_hint: str) -> str:
        return self.asset_pipeline._semantic_scene_directive(scene, scene_hint)

    def _should_rebuild_image_prompt(self, prompt: str) -> bool:
        return self.asset_pipeline._should_rebuild_image_prompt(prompt)

    def _replace_subject_aliases(self, prompt: str) -> str:
        return self.asset_pipeline._replace_subject_aliases(prompt)

    def _with_no_text_image_constraints(self, prompt: str) -> str:
        return self.asset_pipeline._with_no_text_image_constraints(prompt)

    def _fallback_query_variants(self, topic_text: str, base_queries: list[str]) -> list[str]:
        return self.scene_pipeline.fallback_query_variants(topic_text, base_queries)

    def _step_tts(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.asset_pipeline.step_tts(session, job, attempt)

    def _fit_tts_duration(self, audio_path: Path, srt_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        return self.asset_pipeline._fit_tts_duration(audio_path, srt_path, result)

    def _scale_srt_timings(self, srt_path: Path, speed: float) -> None:
        self.asset_pipeline._scale_srt_timings(srt_path, speed)

    def _measure_audio_ms(self, audio_path: Path) -> int:
        return self.asset_pipeline._measure_audio_ms(audio_path)

    def _step_subtitles(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.asset_pipeline.step_subtitles(session, job, attempt)

    def _step_background_music(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.asset_pipeline.step_background_music(session, job, attempt)

    def _persist_background_music_debug(
        self,
        job_id: str,
        attempt: int,
        topic_dict: dict[str, Any],
        script_dict: dict[str, Any],
        target_duration_ms: int,
        phase: str,
        elapsed_ms: float,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.asset_pipeline._persist_background_music_debug(
            job_id=job_id,
            attempt=attempt,
            topic_dict=topic_dict,
            script_dict=script_dict,
            target_duration_ms=target_duration_ms,
            phase=phase,
            elapsed_ms=elapsed_ms,
            result=result,
            error=error,
        )

    def _mix_background_music_with_repair(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
    ) -> dict[str, Any]:
        return self.asset_pipeline._mix_background_music_with_repair(
            narration_path=narration_path,
            music_path=music_path,
            output_path=output_path,
            target_duration_ms=target_duration_ms,
            gain_db=gain_db,
        )

    def _mix_background_music(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
        strategy: str = "sidechaincompress+amix+loudnorm",
    ) -> dict[str, Any]:
        return self.asset_pipeline._mix_background_music(
            narration_path=narration_path,
            music_path=music_path,
            output_path=output_path,
            target_duration_ms=target_duration_ms,
            gain_db=gain_db,
            strategy=strategy,
        )

    def _generate_sound_design_track(
        self,
        job_id: str,
        scenes: list[dict[str, Any]],
        subtitle_items: list[dict[str, Any]],
        duration_ms: int,
    ) -> dict[str, Any]:
        return self.asset_pipeline._generate_sound_design_track(job_id, scenes, subtitle_items, duration_ms)

    def _mix_sound_design_track(
        self,
        base_audio_path: Path,
        sound_design_path: Path,
        output_path: Path,
        gain_db: float,
    ) -> dict[str, Any]:
        return self.asset_pipeline._mix_sound_design_track(
            base_audio_path=base_audio_path,
            sound_design_path=sound_design_path,
            output_path=output_path,
            gain_db=gain_db,
        )

    def _split_subtitle_cue(self, cue: dict[str, Any], token_start: int, token_end: int) -> list[dict[str, Any]]:
        return self.asset_pipeline._split_subtitle_cue(cue, token_start, token_end)

    def _split_caption_by_subtitle_limits(self, text: str, max_words: int = 14, max_chars: int = 42, max_lines: int = 2) -> list[str]:
        return self.asset_pipeline._split_caption_by_subtitle_limits(text, max_words=max_words, max_chars=max_chars, max_lines=max_lines)

    def _avoid_weak_subtitle_endings(self, chunks: list[str]) -> list[str]:
        return self.asset_pipeline._avoid_weak_subtitle_endings(chunks)

    def _subtitle_chunk_fits(self, text: str, max_chars: int = 42, max_lines: int = 2, max_words: int = 14) -> bool:
        return self.asset_pipeline._subtitle_chunk_fits(text, max_chars=max_chars, max_lines=max_lines, max_words=max_words)

    def _rebalance_subtitle_boundary(
        self,
        current_text: str,
        next_text: str,
        max_chars: int = 42,
        max_lines: int = 2,
        max_words: int = 14,
    ) -> tuple[str, str, int]:
        return self.asset_pipeline._rebalance_subtitle_boundary(
            current_text,
            next_text,
            max_chars=max_chars,
            max_lines=max_lines,
            max_words=max_words,
        )

    def _repair_subtitle_item_boundaries(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.asset_pipeline._repair_subtitle_item_boundaries(items)

    def _render_ass(self, items: list[dict[str, Any]]) -> str:
        return self.asset_pipeline._render_ass(items)

    def _remove_stale_quality_report(self, job_id: str, relative_path: str) -> None:
        try:
            (self.storage.job_dir(job_id) / relative_path).unlink(missing_ok=True)
        except OSError:
            pass

    def _ms_to_ass(self, ms: int) -> str:
        return self.asset_pipeline._ms_to_ass(ms)

    def _step_render(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.render_pipeline.step_render(session, job, attempt)

    def _render_with_repair(
        self,
        job_id: str,
        base_command: list[str],
        final_video: Path,
        ffmpeg_log: Path,
        expected_duration_ms: int,
    ) -> tuple[Any, str]:
        return self.render_pipeline.render_with_repair(job_id, base_command, final_video, ffmpeg_log, expected_duration_ms)

    def _mutate_render_command_for_repair(self, command: list[str], repair_mode: str) -> list[str]:
        return self.render_pipeline.mutate_render_command_for_repair(command, repair_mode)

    def _normalize_scene_timings(self, scenes: list[dict[str, Any]], total_duration_ms: int) -> list[dict[str, Any]]:
        from app.pipelines.timeline import normalize_scene_timings

        return normalize_scene_timings(scenes, total_duration_ms)

    def _step_monetization_readiness(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.monetization_pipeline.step_monetization_readiness(session, job, attempt)

    def _build_monetization_report(self, session: Session, job: Job, extra_confirmations: set[str] | None = None) -> dict[str, Any]:
        return self.monetization_pipeline.build_monetization_report(session, job, extra_confirmations)

    def _build_human_review_checklist(
        self,
        rights_registry: dict[str, Any],
        ai_disclosure: dict[str, Any],
        fact_claims_report: dict[str, Any],
        metadata_review: dict[str, Any],
        channel_repetition_report: dict[str, Any],
        publish_audit_required: bool,
        confirmations: set[str],
    ) -> dict[str, Any]:
        return self.monetization_pipeline.build_human_review_checklist(
            rights_registry=rights_registry,
            ai_disclosure=ai_disclosure,
            fact_claims_report=fact_claims_report,
            metadata_review=metadata_review,
            channel_repetition_report=channel_repetition_report,
            publish_audit_required=publish_audit_required,
            confirmations=confirmations,
        )

    def _build_rights_registry(
        self,
        job: Job,
        assets: list[SceneAsset],
        narration: NarrationAsset | None,
        background_music: BackgroundMusicAsset | None,
    ) -> dict[str, Any]:
        return self.monetization_pipeline.build_rights_registry(job, assets, narration, background_music)

    def _build_ai_disclosure_report(self, assets: list[SceneAsset]) -> dict[str, Any]:
        return self.monetization_pipeline.build_ai_disclosure_report(assets)

    def _build_fact_claims_report(
        self,
        script: Script | None,
        topic_plan: TopicPlan | None,
        fact_pack: dict[str, Any],
        script_artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return self.monetization_pipeline.build_fact_claims_report(script, topic_plan, fact_pack, script_artifact)

    def _build_channel_repetition_report(self, session: Session, job: Job, topic_plan: TopicPlan | None, script: Script | None) -> dict[str, Any]:
        return self.monetization_pipeline.build_channel_repetition_report(session, job, topic_plan, script)

    def _build_metadata_review(self, topic_plan: TopicPlan | None, script: Script | None, tags: list[str]) -> dict[str, Any]:
        return self.monetization_pipeline.build_metadata_review(topic_plan, script, tags)

    def _manual_monetization_confirmations(self, session: Session, job_id: str) -> set[str]:
        return self.monetization_pipeline.manual_monetization_confirmations(session, job_id)

    def _build_job_performance_report(self, metrics: list[PerformanceMetric]) -> dict[str, Any]:
        return self.monetization_pipeline.build_job_performance_report(metrics)

    def _step_publish(self, session: Session, job: Job, attempt: int) -> list[str]:
        return self.monetization_pipeline.step_publish(session, job, attempt)

    def _build_publish_package(self, session: Session, job: Job) -> dict[str, Any]:
        return self.monetization_pipeline.build_publish_package(session, job)

    def _provider_publish_audit(self, script_artifact: dict[str, Any], fact_pack: dict[str, Any], tags: list[str]) -> dict[str, Any]:
        return self.monetization_pipeline.provider_publish_audit(script_artifact, fact_pack, tags)

    def _read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        return self.monetization_pipeline.read_job_json(job_id, relative_path)

    def _build_publish_hashtags(self, topic_plan: TopicPlan | None, script: Script | None) -> list[str]:
        return self.monetization_pipeline.build_publish_hashtags(topic_plan, script)

    def _weak_hashtag_terms(self) -> set[str]:
        return self.monetization_pipeline.weak_hashtag_terms()

    def _normalize_hashtag_text(self, text: str) -> str:
        return self.monetization_pipeline.normalize_hashtag_text(text)

    def _publish_readiness_report(
        self,
        script: Script | None,
        topic_plan: TopicPlan | None,
        fact_pack: dict[str, Any],
        tags: list[str],
        checklist: dict[str, bool],
        script_artifact: dict[str, Any] | None = None,
        minimax_audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.monetization_pipeline.publish_readiness_report(script, topic_plan, fact_pack, tags, checklist, script_artifact, minimax_audit)

    def _script_to_dict(self, script: Script) -> dict[str, Any]:
        return self.monetization_pipeline.script_to_dict(script)

    def _append_event(self, job_id: str, event_name: str, status: str, payload: dict[str, Any]) -> None:
        job_dir = self.storage.job_dir(job_id)
        event_path = job_dir / "events.jsonl"
        line = json.dumps(
            {
                "event_id": new_id(),
                "timestamp": iso_now(),
                "level": "info" if status == "succeeded" else "error",
                "job_id": job_id,
                "event_name": event_name,
                "status": status,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _read_events(self, job_id: str) -> list[dict[str, Any]]:
        event_path = self.storage.job_dir(job_id, create=False) / "events.jsonl"
        if not event_path.exists():
            return []
        return [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _serialize_for_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = {}
        for key, value in payload.items():
            if hasattr(value, "isoformat"):
                data[key] = value.isoformat()
            else:
                data[key] = value
        return data

    def _upsert_topic_registry(self, session: Session, job_id: str, approved: bool) -> None:
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

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.settings.artifact_retention_enabled:
                should_sweep = time.monotonic() - self._last_retention_sweep_at >= self.settings.artifact_retention_sweep_seconds
                if should_sweep:
                    self._run_retention_sweep()
            if self._youtube_api_mode_enabled():
                self._sync_native_scheduled_publications()
            with session_scope() as session:
                claimed_job_id = self._claim_next_job(session)
            if claimed_job_id:
                self.process_job(claimed_job_id)
                continue
            claimed_publication_job_id = self._claim_due_publication_schedule()
            if claimed_publication_job_id:
                self.publish_job(claimed_publication_job_id, trigger="schedule_worker")
                continue
            time.sleep(self.settings.worker_poll_seconds)

    def _claim_next_job(self, session: Session) -> str | None:
        now = utcnow()
        lease_expires_at = now + self._lease_delta()
        claimable_job_id = (
            select(Job.job_id)
            .where(
                or_(
                    Job.status == "queued",
                    (Job.status == "running") & (Job.lease_expires_at.is_(None) | (Job.lease_expires_at < now)),
                )
            )
            .order_by(Job.created_at)
            .limit(1)
            .scalar_subquery()
        )
        claim = (
            update(Job)
            .where(Job.job_id == claimable_job_id)
            .values(
                status="running",
                lease_owner=self.worker_id,
                lease_expires_at=lease_expires_at,
            )
            .returning(Job.job_id)
        )
        return session.execute(claim).scalar_one_or_none()

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


orchestrator = JobOrchestrator()
