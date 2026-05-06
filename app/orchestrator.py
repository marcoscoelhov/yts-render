from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import unicodedata
import concurrent.futures
import wave
import httpx
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import imageio_ffmpeg
from PIL import Image
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.audio.music_mix import mix_background_music
from app.audio.sound_design import generate_sound_design_track, mix_sound_design_track
from app.compliance.review import build_human_review_checklist
from app.config import get_settings
from app.db import SessionLocal, session_scope
from app.editorial.retention import attach_retention_metadata, enrich_plan_for_script_generation
from app.editorial.repetition import build_channel_repetition_report
from app.models import (
    BackgroundMusicAsset,
    ErrorLog,
    FallbackEvent,
    Job,
    NarrationAsset,
    PerformanceMetric,
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
from app.providers import ProviderRegistry
from app.quality.asset_gate import AssetGate
from app.quality.render_gate import RenderGate
from app.quality.scene_gate import ScenePlanGate
from app.quality.script_gate import ScriptQualityGate
from app.quality.subtitle_gate import BAD_ENDINGS, SubtitleGate
from app.schemas import SUPPORTED_NICHES, TopicRequestCreate
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


class RecoverableStepError(RuntimeError):
    pass


class FatalStepError(RuntimeError):
    pass


def model_payload(model: type[Any], payload: dict[str, Any]) -> dict[str, Any]:
    columns = {column.key for column in model.__mapper__.columns}
    return {key: value for key, value in payload.items() if key in columns}


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
            job.lease_expires_at = utcnow() + timedelta(seconds=self.settings.job_lease_seconds)
        for step in self._steps():
            ok = self._run_step(job_id, step)
            if not ok:
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if not job:
                        raise KeyError(job_id)
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
        self._append_event(job_id, "render.completed", "succeeded", {"status": job.status})
        return job.status

    def get_job_details(self, session: Session, job_id: str) -> dict[str, Any]:
        job = session.get(Job, job_id)
        if not job:
            raise KeyError(job_id)
        topic_request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job_id))
        script = session.scalar(select(Script).where(Script.job_id == job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job_id))
        assets = session.scalars(select(SceneAsset).where(SceneAsset.job_id == job_id).order_by(SceneAsset.scene_id, SceneAsset.provider)).all()
        fallbacks = session.scalars(select(FallbackEvent).where(FallbackEvent.job_id == job_id).order_by(FallbackEvent.created_at)).all()
        errors = session.scalars(select(ErrorLog).where(ErrorLog.job_id == job_id).order_by(ErrorLog.created_at)).all()
        reviews = session.scalars(select(ReviewRecord).where(ReviewRecord.job_id == job_id).order_by(ReviewRecord.created_at)).all()
        repair_telemetry = {
            "topic_plan": self._read_job_json(job_id, "topic_plan_repair_telemetry.json"),
            "script": self._read_job_json(job_id, "script_repair_telemetry.json"),
            "background_music": self._read_job_json(job_id, "background_music_repair_telemetry.json"),
            "render": self._read_job_json(job_id, "render_repair_telemetry.json"),
        }
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
            "fallbacks": fallbacks,
            "errors": errors,
            "reviews": reviews,
            "performance_metrics": session.scalars(
                select(PerformanceMetric).where(PerformanceMetric.job_id == job_id).order_by(PerformanceMetric.created_at.desc())
            ).all(),
            "repair_telemetry": repair_telemetry,
            "events": self._read_events(job_id),
            "monetization_report": self._read_job_json(job_id, "monetization_report.json"),
            "publish_package": self._read_job_json(job_id, "publish_package.json"),
        }

    def review_job(self, payload: dict[str, Any], job_id: str) -> str | None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
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
                self._append_event(job_id, "review.approved", "succeeded", payload)
                return None
            if payload["action"] == "reject":
                job.status = "rejected"
                job.review_state = "rejected"
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

    def publish_job(self, job_id: str, youtube_video_id: str | None = None, youtube_url: str | None = None) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status not in {"approved_for_publish", "published"}:
                raise FatalStepError("job must be approved_for_publish before publishing")
            monetization_report = self._read_job_json(job.job_id, "monetization_report.json")
            if monetization_report and not monetization_report.get("passed"):
                raise FatalStepError("job has not passed monetization readiness gate")
            package = self._build_publish_package(session, job)
            package["youtube"] = {
                "mode": self.settings.youtube_publish_mode,
                "api_enabled": self.settings.youtube_api_enabled,
                "video_id": youtube_video_id,
                "url": youtube_url,
                "published_at": iso_now(),
            }
            self.storage.persist_json(job.job_id, "publish_result.json", self._serialize_for_json(package))
            job.status = "published"
            job.review_state = "published"
            quality_summary = dict(job.quality_summary or {})
            quality_summary["youtube"] = package["youtube"]
            job.quality_summary = quality_summary
        self._append_event(job_id, "youtube.published", "succeeded", {"video_id": youtube_video_id, "url": youtube_url})

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
            StepDefinition("script", 2, self._step_script),
            StepDefinition("scene_plan", 1, self._step_scene_plan),
            StepDefinition("asset_generation", 2, self._step_assets),
            StepDefinition("tts", 2, self._step_tts),
            StepDefinition("subtitle_alignment", 1, self._step_subtitles),
            StepDefinition("background_music", 1, self._step_background_music),
            StepDefinition("render", 1, self._step_render),
            StepDefinition("monetization_readiness_gate", 0, self._step_monetization_readiness),
            StepDefinition("publish_to_review_hub", 0, self._step_publish),
        ]

    def _run_step(self, job_id: str, step: StepDefinition) -> bool:
        for attempt in range(1, step.retries + 2):
            if self.stop_event.is_set():
                self._cancel_job(job_id, step.name, "worker shutdown requested before retry")
                return False
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
                job.lease_expires_at = utcnow() + timedelta(seconds=self.settings.job_lease_seconds)
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
                return True
            except RecoverableStepError as exc:
                self._record_step_failure(job_id, step.name, attempt, str(exc), recoverable=True)
                if attempt <= step.retries:
                    if self.stop_event.is_set():
                        self._cancel_job(job_id, step.name, "worker shutdown requested during recoverable retry")
                        return False
                    continue
                self._fail_job(job_id, step.name, str(exc))
                return False
            except Exception as exc:  # noqa: BLE001
                self._record_step_failure(job_id, step.name, attempt, str(exc), recoverable=False)
                self._fail_job(job_id, step.name, str(exc))
                return False
        return False

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
        self._append_event(job_id, f"{step_name}.failed", "failed", {"attempt": attempt, "message": message})

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
        blocked = any(term in request.seed_theme.lower() for term in ["odio", "terrorismo", "explosivo"])
        if blocked:
            raise FatalStepError("input blocked by moderation")
        quality = {
            "schema_valid": True,
            "niche_supported": True,
            "language": request.language,
            "moderation_ok": True,
        }
        self._append_event(job.job_id, "input_gate.passed", "succeeded", quality)
        return ["request.json"]

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
        return ["topic_plan.json", topic_telemetry_file]

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

        return {
            **plan,
            "canonical_topic": canonical_topic,
            "angle": angle,
            "hook_promise": hook_promise,
            "title_candidates": [str(title).strip() for title in title_candidates if str(title).strip()][:5],
            "entities": [str(entity).strip() for entity in entities if str(entity).strip()],
            "search_terms": [str(term).strip() for term in search_terms if str(term).strip()],
            "quality_metrics": quality_metrics,
        }

    def _step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
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
        return ["fact_pack.json", "script.json", "script_generation_debug.json", script_telemetry_file]

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
            "llm_enable_fallback": self.settings.llm_enable_fallback,
            "minimax_script_timeout_sec": self.settings.minimax_script_timeout_sec,
            "fact_pack_status": fact_pack.get("status"),
            "fact_count": len(fact_pack.get("facts") or []),
            "canonical_topic": plan_dict.get("canonical_topic"),
            "angle": plan_dict.get("angle"),
            "requested_angle": plan_dict.get("requested_angle"),
            "source_fact_ids": list((script or {}).get("source_fact_ids") or []),
            "script_title": (script or {}).get("title"),
            "script_hook": (script or {}).get("hook"),
            "script_language": (script or {}).get("language"),
            "script_estimated_duration_sec": (script or {}).get("estimated_duration_sec"),
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
        cleaned_queries.sort(key=self._fact_query_priority)
        for query in cleaned_queries[:8]:
            pack = self._wikipedia_fact_pack(query)
            if pack.get("facts"):
                pack["query_used"] = query
                pack["status"] = "verified"
                return pack
        return {
            "status": "limited",
            "query_used": cleaned_queries[0] if cleaned_queries else request.seed_theme,
            "facts": [],
            "sources": [],
            "editorial_rule": "No source facts were retrieved. Script must avoid precise numbers, dates, medical/scientific/engineering causality, and absolute claims unless already present in the user input.",
        }

    def _fact_query_priority(self, query: str) -> tuple[int, int, int, int]:
        normalized = query.lower()
        token_count = len(word_tokens(query))
        is_short_entity = token_count <= 3 and ":" not in query and "?" not in query
        has_concept_suffix = any(term in normalized for term in ["carotenoides", "pigmentos", "diet", "inclinação", "engenharia", "solo"])
        return (
            0 if is_short_entity else 1,
            1 if has_concept_suffix else 0,
            token_count,
            len(query),
        )

    def _is_weak_fact_query(self, query: str) -> bool:
        tokens = [token.lower() for token in word_tokens(query) if token]
        if not tokens:
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
        }
        return len(tokens) == 1 and tokens[0] in weak_single_terms


    def _fact_pack_queries(self, request: TopicRequest, topic_plan: TopicPlan) -> list[str]:
        raw_queries = [request.seed_theme, topic_plan.canonical_topic, topic_plan.angle, *(topic_plan.title_candidates or [])]
        queries: list[str] = []
        for query in raw_queries:
            cleaned = self._clean_fact_query(str(query or ""))
            if cleaned:
                queries.append(cleaned)
                entity = self._extract_fact_entity(cleaned)
                if entity and entity != cleaned:
                    queries.append(entity)
                    for concept in self._fact_query_concepts(cleaned):
                        queries.append(f"{entity} {concept}")
        return queries

    def _clean_fact_query(self, query: str) -> str:
        query = unicodedata.normalize("NFKC", query).strip()
        query = re.sub(r"[?!¿¡]+", " ", query)
        query = re.sub(r"\s+", " ", query)
        query = re.sub(r"^(?:voc[eê]\s+sabia|j[aá]\s+imaginou|surpreenda-se|prepare-se)\b[:\s,.-]*", "", query, flags=re.IGNORECASE)
        query = re.sub(r"^(?:por que|porque|como|qual|quais|o que|quem|quando|onde)\s+", "", query, flags=re.IGNORECASE)
        query = re.sub(r"\b(?:fica|ficam|ficou|são|sao|é|e|era|foram|tem|têm)\b", " ", query, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", query).strip(" -–—:;,." )

    def _extract_fact_entity(self, query: str) -> str:
        stopwords = {
            "por", "que", "porque", "como", "qual", "quais", "para", "com", "uma", "um", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e",
            "fica", "ficam", "cor", "rosa", "cor-de-rosa", "não", "nao", "cai", "acontece", "segredo", "invisível", "invisivel", "parece", "artificial",
            "curiosidades", "curiosidade", "cientificas", "científicas", "cientifica", "científica", "sobre", "mais", "inteligente", "oceano",
            "animal", "explica", "explicacao", "explicação", "fatos", "fato", "voce", "você", "sabia", "surpreenda", "prepare",
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
        concepts: list[str] = []
        if any(term in normalized for term in ["rosa", "cor", "color", "pink"]):
            concepts.extend(["carotenoides", "pigmentos", "diet"])
        if any(term in normalized for term in ["cai", "inclina", "torre"]):
            concepts.extend(["inclinação", "engenharia", "solo"])
        return concepts[:3]

    def _wikipedia_fact_pack(self, query: str) -> dict[str, Any]:
        for language in ["pt", "en"]:
            try:
                with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "yts-render/1.0 fact-pack"}) as client:
                    search = client.get(
                        f"https://{language}.wikipedia.org/w/api.php",
                        params={"action": "opensearch", "search": query, "limit": 1, "namespace": 0, "format": "json"},
                    )
                    search.raise_for_status()
                    payload = search.json()
                    titles = payload[1] if len(payload) > 1 else []
                    if not titles:
                        continue
                    title = str(titles[0])
                    summary = client.get(f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}")
                    summary.raise_for_status()
                    data = summary.json()
            except Exception:  # noqa: BLE001
                continue
            extract = str(data.get("extract") or "").strip()
            if not extract:
                continue
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", extract) if len(part.strip()) > 30]
            facts = [
                {
                    "fact_id": f"F{index}",
                    "claim": sentence[:260],
                    "source_id": "S1",
                }
                for index, sentence in enumerate(sentences[:5], start=1)
            ]
            source_url = str(data.get("content_urls", {}).get("desktop", {}).get("page") or data.get("content_urls", {}).get("mobile", {}).get("page") or "")
            return {
                "status": "verified",
                "language": language,
                "query_used": query,
                "topic_title": data.get("title") or title,
                "facts": facts,
                "sources": [
                    {
                        "source_id": "S1",
                        "title": data.get("title") or title,
                        "url": source_url,
                        "provider": f"wikipedia_{language}",
                    }
                ],
                "editorial_rule": "Use facts as source material only. Preserve viral pacing, but every precise number, date, technical cause, history claim, or scientific claim must be grounded in fact_id references or rewritten conservatively.",
            }
        return {"status": "limited", "facts": [], "sources": []}


    def _fact_pack_consistency_reasons(self, script: dict[str, Any], fact_pack: Any) -> list[str]:
        source_ids = script.get("source_fact_ids") or script.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        if not isinstance(fact_pack, dict) or fact_pack.get("status") != "verified":
            return ["invented_source_fact_ids"] if source_ids else []
        facts = fact_pack.get("facts") or []
        valid_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
        if not valid_ids:
            return []
        used_ids = {str(item) for item in source_ids if str(item) in valid_ids}
        minimum = min(2, len(valid_ids))
        reasons: list[str] = []
        if len(used_ids) < minimum:
            reasons.append("fact_pack_source_ids_missing")
        fact_risk = self.script_gate._fact_risk_report(script)  # noqa: SLF001
        if fact_risk.get("blocked") and len(used_ids) < len(valid_ids):
            reasons.append("high_risk_claims_need_fact_pack_grounding")
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
        processed = dict(script)
        fact_pack = plan_dict.get("fact_pack") if isinstance(plan_dict.get("fact_pack"), dict) else {}
        if self._should_force_conservative_fact_rewrite(processed, fact_pack, gate_reasons):
            processed = self._rewrite_script_conservatively(processed, fact_pack, plan_dict)
        if self._should_repair_loop(processed, gate_reasons):
            processed = self._repair_script_loop_closure(processed, plan_dict)
        processed = self._split_long_script_sentences(processed)
        processed["estimated_duration_sec"] = round(max(25.0, min(42.0, len(word_tokens(str(processed.get("full_narration") or ""))) / 2.55)), 2)
        processed["token_count"] = len(tokenize(str(processed.get("full_narration") or "")))
        return processed

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
            grounded_claims = [str(fact.get("claim") or "").strip() for fact in fact_pack.get("facts") or [] if str(fact.get("claim") or "").strip()]
            valid_ids = [str(fact.get("fact_id")) for fact in fact_pack.get("facts") or [] if fact.get("fact_id")]
            if grounded_claims:
                rewritten["hook"] = self._soften_risky_sentence(str(rewritten.get("hook") or ""), anchor)
                rewritten["body_beats"] = [claim.rstrip(".!?") + "." for claim in grounded_claims[: max(3, min(4, len(grounded_claims)))]]
                rewritten["key_facts"] = grounded_claims[:3]
                rewritten["source_fact_ids"] = valid_ids[: max(2, min(3, len(valid_ids)))]
                sentences = [rewritten["hook"], *rewritten["body_beats"]]
                ending = str(rewritten.get("ending") or "").strip()
                if ending:
                    sentences.append(self._soften_risky_sentence(ending, anchor))
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
        return rewritten

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
        opening_tokens = [token for token in word_tokens(hook) if len(token) >= 4]
        echoed = " ".join(opening_tokens[:2]) if opening_tokens else anchor
        repaired["ending"] = f"No fim, {anchor} fecha o ciclo, e {echoed} agora parece inevitável."
        body_beats = [str(item).rstrip(".!?") + "." for item in repaired.get("body_beats") or [] if str(item).strip()]
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
            script["qa_metrics"] = {**gate_result.metrics, "fact_pack_consistency_pass": True, "script_repair_attempts_log": attempts_log}
            return script, script["qa_metrics"]

        repair_attempts = max(0, self.settings.llm_script_repair_attempts)
        last_reasons = [*gate_result.reasons, *consistency_reasons]
        self._persist_script_rejection(job_id, script, gate_result.metrics, consistency_reasons)
        for repair_attempt in range(1, repair_attempts + 1):
            repaired = self.providers.creative.repair_script(script, last_reasons, plan_dict)
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
                }
                return repaired, repaired["qa_metrics"]
            self._persist_script_rejection(job_id, repaired, repaired_gate.metrics, repaired_consistency_reasons)
            script = repaired
            last_reasons = [*repaired_gate.reasons, *repaired_consistency_reasons]

        fallback_repaired = self.providers.creative.repair_script_with_fallback(script, last_reasons, plan_dict)
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
                }
                return fallback_repaired, fallback_repaired["qa_metrics"]
            self._persist_script_rejection(job_id, fallback_repaired, fallback_gate.metrics, fallback_consistency_reasons)
            last_reasons = [*fallback_gate.reasons, *fallback_consistency_reasons]

        raise RecoverableStepError(f"script quality gate failed: {', '.join(last_reasons)}")

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

    def _step_scene_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "scene_plan_rejected.json")
        self._remove_stale_quality_report(job.job_id, "scene_plan_raw.json")
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        assert script and topic_plan
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        script_dict = {
            "title": script.title,
            "hook": script.hook,
            "body_beats": script.body_beats,
            "ending": script.ending,
            "cta": script.cta,
            "full_narration": script.full_narration,
            "estimated_duration_sec": script.estimated_duration_sec,
            "key_facts": script.key_facts,
            "qa_metrics": script.qa_metrics,
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "hub_viral_prompt_source": request.notes if request else None,
            "downstream_rule": "Scenes, images, subtitles and TTS must derive from full_narration. Do not invent new beats or split tiny punchlines into standalone render scenes.",
        }
        scenes = self.providers.creative.plan_scenes(script_dict, self.settings.scene_target_count)
        self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
        tokens = word_tokens(script.full_narration)
        scenes = self._normalize_scene_token_coverage(scenes, script.full_narration)
        if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
            fallback_planner = None if self.settings.strict_minimax_validation else getattr(self.providers.creative, "fallback", None)
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
                self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
                scenes = self._normalize_scene_token_coverage(scenes, script.full_narration)
            if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
                raise RecoverableStepError("scene coverage invalid")
        scenes = [self._normalize_scene_semantics(scene, topic_plan.canonical_topic) for scene in scenes]
        scene_gate = self.scene_gate.validate(scenes, self.settings.scene_target_count)
        if not scene_gate.passed:
            fallback_planner = None if self.settings.strict_minimax_validation else getattr(self.providers.creative, "fallback", None)
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
                self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
                scenes = self._normalize_scene_token_coverage(scenes, script.full_narration)
                scenes = [self._normalize_scene_semantics(scene, topic_plan.canonical_topic) for scene in scenes]
                scene_gate = self.scene_gate.validate(scenes, self.settings.scene_target_count)
            if not scene_gate.passed:
                self.storage.persist_json(
                    job.job_id,
                    "scene_plan_rejected.json",
                    {"reasons": scene_gate.reasons, "metrics": scene_gate.metrics, "scenes": scenes},
                )
                raise RecoverableStepError(f"scene plan quality gate failed: {', '.join(scene_gate.reasons[:6])}")
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "scene_plan_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(scenes),
            "scene_count": len(scenes),
            "scenes": scenes,
        }
        session.execute(delete(ScenePlan).where(ScenePlan.job_id == job.job_id))
        session.add(ScenePlan(**model_payload(ScenePlan, payload)))
        self.storage.persist_json(job.job_id, "scene_plan.json", self._serialize_for_json(payload))
        quality_summary = dict(job.quality_summary or {})
        quality_summary["scene_plan"] = {**scene_gate.metrics, "scene_plan_gate_pass": True}
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "scene_plan.generated", "succeeded", quality_summary["scene_plan"])
        return ["scene_plan.json"]

    def _normalize_scene_token_coverage(self, scenes: list[dict[str, Any]], full_narration: str) -> list[dict[str, Any]]:
        if not scenes:
            return scenes
        tokens = word_tokens(full_narration)
        total_tokens = len(tokens)
        if total_tokens <= 0:
            return scenes
        ordered = [dict(scene) for scene in sorted(scenes, key=lambda scene: int(scene.get("order", 0) or 0))]
        weights = [max(1, len(word_tokens(str(scene.get("narration_text") or "")))) for scene in ordered]
        remaining_tokens = total_tokens
        remaining_weight = sum(weights)
        cursor = 0
        normalized: list[dict[str, Any]] = []
        for index, scene in enumerate(ordered):
            scene_id = str(scene.get("scene_id") or f"scene-{index + 1}")
            scenes_left = len(ordered) - index
            weight = weights[index]
            if index == len(ordered) - 1:
                count = remaining_tokens
            else:
                proportional = round(remaining_tokens * (weight / max(remaining_weight, 1)))
                count = max(1, min(proportional, remaining_tokens - (scenes_left - 1)))
            start = cursor
            end = start + count - 1
            exact_text = " ".join(tokens[start : end + 1]).strip()
            normalized.append(
                {
                    **scene,
                    "scene_id": scene_id,
                    "order": index + 1,
                    "token_start": start,
                    "token_end": end,
                    "narration_text": exact_text or str(scene.get("narration_text") or "").strip(),
                }
            )
            cursor = end + 1
            remaining_tokens -= count
            remaining_weight -= weight
        if normalized:
            normalized[0]["token_start"] = 0
            normalized[-1]["token_end"] = total_tokens - 1
            normalized[-1]["narration_text"] = " ".join(tokens[normalized[-1]["token_start"] : total_tokens]).strip()
        return normalized

    def _step_assets(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "asset_quality_report.json")
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        assert scene_plan
        session.execute(delete(SceneAsset).where(SceneAsset.job_id == job.job_id))
        asset_refs: list[str] = []
        selected_assets: list[dict[str, Any]] = []
        for scene in scene_plan.scenes:
            scene_dir = self.storage.job_dir(job.job_id) / "assets" / scene["scene_id"]
            candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            fallback_used = False
            primary_provider = "minimax"
            variant_cursor = 0
            for regeneration_round in range(1, max(1, self.settings.asset_generation_regeneration_rounds) + 1):
                for variant_index, variant_scene in enumerate(self._image_prompt_variants(scene, regeneration_round), start=1):
                    variant_cursor += 1
                    ai_path = scene_dir / ("ai.png" if variant_cursor == 1 else f"ai-{variant_cursor}.png")
                    try:
                        ai_asset = self._generate_primary_asset(variant_scene, ai_path)
                        ai_asset = self._normalize_asset_uri_extension(ai_asset)
                        ai_scores = self._score_asset(variant_scene, ai_asset)
                        candidates.append((ai_asset, ai_scores))
                        primary_provider = ai_asset["provider"]
                        self._append_event(
                            job.job_id,
                            "asset.primary_candidate_scored",
                            "succeeded",
                            {
                                "scene_id": scene["scene_id"],
                                "variant": variant_cursor,
                                "regeneration_round": regeneration_round,
                                "provider": ai_asset["provider"],
                                "semantic_match": ai_scores["semantic_match"],
                                "total_score": ai_scores["total_score"],
                            },
                        )
                        if self._asset_scores_pass(ai_scores):
                            break
                    except Exception as exc:  # noqa: BLE001
                        fallback_used = True
                        reason_code = "ai_provider_timeout" if "timed out" in str(exc).lower() else "ai_provider_error"
                        if not self.settings.strict_minimax_validation:
                            session.add(
                                FallbackEvent(
                                    event_id=new_id(),
                                    job_id=job.job_id,
                                    schema_version=self.settings.schema_version,
                                    content_hash=stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": f"{reason_code}_{variant_cursor}"}),
                                    created_at=utcnow(),
                                    step="asset_generation",
                                    reason_code=reason_code,
                                    attempt=attempt,
                                    scene_id=scene["scene_id"],
                                    from_provider="minimax",
                                    to_provider="local_semantic",
                                    reason_detail=str(exc),
                                )
                            )
                        self._append_event(
                            job.job_id,
                            "asset.primary_candidate_failed",
                            "failed",
                            {
                                "scene_id": scene["scene_id"],
                                "variant": variant_cursor,
                                "regeneration_round": regeneration_round,
                                "reason": str(exc),
                            },
                        )
                if any(self._asset_scores_pass(scores) for _, scores in candidates):
                    break
                self._append_event(
                    job.job_id,
                    "asset.regeneration_round_completed",
                    "succeeded",
                    {
                        "scene_id": scene["scene_id"],
                        "regeneration_round": regeneration_round,
                        "candidate_count": len(candidates),
                        "passing_candidate_count": sum(1 for _, scores in candidates if self._asset_scores_pass(scores)),
                    },
                )
            needs_quality_fallback = not candidates or all(not self._asset_scores_pass(scores) for _, scores in candidates)
            if not candidates and not self.settings.use_mock_providers:
                fallback_used = True
            if needs_quality_fallback and not self.settings.strict_minimax_validation:
                fallback_used = True
                fallback_reason_code = "low_semantic_score" if candidates else "no_primary_image_candidate"
                fallback_reason_detail = (
                    "Primary image candidates fell below semantic thresholds."
                    if candidates
                    else "Primary image provider returned no usable candidates."
                )
                session.add(
                    FallbackEvent(
                        event_id=new_id(),
                        job_id=job.job_id,
                        schema_version=self.settings.schema_version,
                        content_hash=stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": fallback_reason_code}),
                        created_at=utcnow(),
                        step="asset_generation",
                        reason_code=fallback_reason_code,
                        attempt=attempt,
                        scene_id=scene["scene_id"],
                        from_provider=primary_provider,
                        to_provider="local_semantic",
                        reason_detail=fallback_reason_detail,
                    )
                )
                self._append_event(job.job_id, "asset.semantic_fallback", "succeeded", {"scene_id": scene["scene_id"]})
                local_asset = self.providers.local_image.generate(scene, scene_dir / "local-semantic.png")
                local_asset = self._normalize_asset_uri_extension(local_asset)
                local_scores = self._score_asset(scene, local_asset)
                candidates.append((local_asset, local_scores))
            passing_candidates = [(asset, scores) for asset, scores in candidates if self._asset_scores_pass(scores)]
            if passing_candidates:
                winner_asset, winner_scores = sorted(passing_candidates, key=lambda item: item[1]["total_score"], reverse=True)[0]
            else:
                self.storage.persist_json(
                    job.job_id,
                    f"assets/{scene['scene_id']}/rejected_candidates.json",
                    {
                        "scene": scene,
                        "thresholds": {
                            "semantic_match": 0.80,
                            "total_score": 0.75,
                            "text_or_watermark_penalty": 0.15,
                            "artifact_penalty": 0.30,
                        },
                        "candidates": [
                            {"asset": asset, "scores": scores}
                            for asset, scores in sorted(candidates, key=lambda item: item[1]["total_score"], reverse=True)
                        ],
                    },
                )
                raise RecoverableStepError(f"asset quality gate failed for {scene['scene_id']}")
            for asset_payload, scores in candidates:
                selected = asset_payload["uri"] == winner_asset["uri"]
                rejection = None if selected else ("score_below_threshold" if not self._asset_scores_pass(scores) else "score_below_winner")
                asset_row = SceneAsset(
                    asset_id=new_id(),
                    job_id=job.job_id,
                    scene_id=scene["scene_id"],
                    schema_version=self.settings.schema_version,
                    content_hash=stable_hash({"asset": asset_payload["uri"], "scores": scores}),
                    created_at=utcnow(),
                    provider=asset_payload["provider"],
                    uri=asset_payload["uri"],
                    width=asset_payload["width"],
                    height=asset_payload["height"],
                    selected=selected,
                    scores=scores,
                    source_url=asset_payload.get("source_url"),
                    attribution=asset_payload.get("attribution"),
                    license_note=asset_payload.get("license_note"),
                    prompt_snapshot=asset_payload["prompt_snapshot"],
                    rejection_reason=rejection,
                    fallback_used=fallback_used and selected and asset_payload["provider"] != primary_provider,
                )
                session.add(asset_row)
            selected_assets.append({"scene_id": scene["scene_id"], "provider": winner_asset["provider"], **winner_scores})
            asset_refs.extend([path_from_uri(asset["uri"]).name for asset, _ in candidates])
        asset_gate = self.asset_gate.validate_selected(selected_assets)
        if not asset_gate.passed:
            self.storage.persist_json(job.job_id, "asset_quality_report.json", {"reasons": asset_gate.reasons, "metrics": asset_gate.metrics})
            raise RecoverableStepError(f"asset quality gate failed: {', '.join(asset_gate.reasons[:6])}")
        quality_summary = dict(job.quality_summary or {})
        quality_summary["assets"] = {**asset_gate.metrics, "semantic_threshold_pass": True}
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "asset.selected", "succeeded", quality_summary["assets"])
        return asset_refs

    def _generate_primary_asset(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        executor: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self.providers.image.generate, scene, output_path)
            return future.result(timeout=self.settings.asset_generation_timeout_sec)
        except concurrent.futures.TimeoutError as exc:
            raise RecoverableStepError(
                f"asset primary generation timed out after {self.settings.asset_generation_timeout_sec}s"
            ) from exc
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def _normalize_asset_uri_extension(self, asset: dict[str, Any]) -> dict[str, Any]:
        uri = str(asset.get("uri") or "")
        if not uri.startswith("file://"):
            return asset
        path = path_from_uri(uri)
        if not path.exists():
            return asset
        try:
            with Image.open(path) as image:
                fmt = (image.format or "").upper()
        except Exception:  # noqa: BLE001
            return asset
        suffix_by_format = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        expected_suffix = suffix_by_format.get(fmt)
        if not expected_suffix or path.suffix.lower() == expected_suffix:
            return asset
        target = path.with_suffix(expected_suffix)
        counter = 2
        while target.exists() and target.resolve() != path.resolve():
            target = path.with_name(f"{path.stem}-{counter}{expected_suffix}")
            counter += 1
        path.rename(target)
        updated = dict(asset)
        updated["uri"] = target.resolve().as_uri()
        updated["file_format"] = fmt.lower()
        updated["extension_normalized"] = True
        return updated

    def _score_asset(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        return self.providers.semantic.score(scene, asset)

    def _asset_scores_pass(self, scores: dict[str, Any]) -> bool:
        return (
            scores["semantic_match"] >= self.settings.asset_semantic_threshold
            and scores["total_score"] >= self.settings.asset_total_threshold
            and scores.get("text_or_watermark_penalty", 0.0) <= 0.15
            and scores.get("artifact_penalty", 0.0) <= 0.30
        )

    def _image_prompt_variants(self, scene: dict[str, Any], regeneration_round: int = 1) -> list[dict[str, Any]]:
        topic_text = str(scene.get("topic_hint") or scene.get("primary_subject") or "")
        primary_subject = str(scene.get("primary_subject") or scene.get("topic_hint") or "")
        base_prompt = self._semantic_english_image_prompt(scene, topic_text, primary_subject)
        english_subject = self._english_subject_hint(topic_text, primary_subject)
        narration = str(scene.get("narration_text") or "").strip()
        scene_hint = self._english_scene_visual_hint(scene, english_subject)
        variant_prompts = [
            base_prompt,
            self._with_no_text_image_constraints(
                f"vertical documentary close shot of {english_subject}, {scene_hint}, "
                f"visually illustrate this exact narration beat: {narration}, scientific documentary realism, "
                "natural lighting, one clear subject, no symbolic poster, no irrelevant props"
            ),
            self._with_no_text_image_constraints(
                f"realistic vertical YouTube Shorts visual, {english_subject} as the unmistakable central subject, "
                f"{narration}, cinematic science documentary frame, concrete factual detail, clean relevant background"
            ),
        ]
        variants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for prompt in variant_prompts:
            normalized = " ".join(prompt.split())
            if regeneration_round > 1:
                normalized = (
                    f"{normalized}, alternate composition, new camera framing, different background geometry, "
                    "keep the same factual subject and no text constraints"
                )
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            variants.append({**scene, "image_prompt": normalized, "regeneration_round": regeneration_round})
        return variants

    def _normalize_scene_semantics(self, scene: dict[str, Any], canonical_topic: str) -> dict[str, Any]:
        topic_text = canonical_topic.replace("_", " ").strip()
        normalized = dict(scene)
        normalized["scene_id"] = str(scene.get("scene_id") or normalized.get("scene_id") or "scene-1")
        primary_subject = str(scene.get("primary_subject") or topic_text).replace("_", " ").strip()
        normalized["primary_subject"] = primary_subject or topic_text
        normalized["topic_hint"] = str(scene.get("topic_hint") or topic_text).replace("_", " ").strip() or topic_text
        base_queries = [
            query.replace("_", " ").strip()
            for query in scene.get("fallback_queries", [topic_text, f"{topic_text} astronomia", f"{topic_text} espaco"])
        ]
        normalized["fallback_queries"] = self._fallback_query_variants(topic_text, base_queries)
        normalized["image_prompt"] = self._semantic_english_image_prompt(scene, topic_text, primary_subject)
        return normalized

    def _semantic_english_image_prompt(self, scene: dict[str, Any], topic_text: str, primary_subject: str) -> str:
        prompt = str(scene.get("image_prompt", "")).replace("_", " ")
        english_subject = self._english_subject_hint(topic_text, primary_subject)
        scene_hint = self._english_scene_visual_hint(scene, english_subject)
        semantic_directive = self._semantic_scene_directive(scene, scene_hint)
        if self._should_rebuild_image_prompt(prompt):
            visual_intent = str(scene.get("visual_intent") or "scientific documentary scene").replace("_", " ")
            prompt = scene_hint or f"vertical cinematic scientific image of {english_subject}, {visual_intent}"
        else:
            prompt = self._replace_subject_aliases(prompt)
        if semantic_directive.lower() not in prompt.lower():
            prompt = f"{prompt}, {semantic_directive}".strip(", ")
        if scene_hint and scene_hint.lower() not in prompt.lower():
            prompt = f"{scene_hint}, {prompt}".strip(", ")
        elif english_subject and english_subject.lower() not in prompt.lower():
            prompt = f"{prompt}, central subject: {english_subject}".strip(", ")
        if "no movie poster" not in prompt.lower():
            prompt += ", scientific visualization, documentary realism, no movie poster, no typography, no stock-photo generic scene"
        return self._with_no_text_image_constraints(prompt)

    def _english_subject_hint(self, topic_text: str, primary_subject: str) -> str:
        for value in [primary_subject, topic_text]:
            normalized = " ".join(str(value).replace("_", " ").lower().split())
            if normalized in ENGLISH_SUBJECT_ALIASES:
                return ENGLISH_SUBJECT_ALIASES[normalized]
            normalized_ascii = (
                normalized.replace("á", "a")
                .replace("à", "a")
                .replace("ã", "a")
                .replace("â", "a")
                .replace("é", "e")
                .replace("ê", "e")
                .replace("í", "i")
                .replace("ó", "o")
                .replace("õ", "o")
                .replace("ô", "o")
                .replace("ú", "u")
                .replace("ç", "c")
            )
            if "polvo" in normalized_ascii:
                return "octopus"
            if "gato" in normalized_ascii or "felino" in normalized_ascii:
                return "cat"
            if "buraco" in normalized_ascii and "negro" in normalized_ascii:
                return "black hole"
            if "vulcao" in normalized_ascii:
                return "volcano"
            if "cafeina" in normalized_ascii and "foco" in normalized_ascii:
                return "caffeine and focus"
            if "cafe" in normalized_ascii and "foco" in normalized_ascii:
                return "coffee and focus"
            if "cafeina" in normalized_ascii:
                return "caffeine"
            if "cafe" in normalized_ascii:
                return "coffee"
        return primary_subject or topic_text or "the subject"

    def _english_scene_visual_hint(self, scene: dict[str, Any], english_subject: str) -> str:
        narration = str(scene.get("narration_text") or "").lower()
        normalized = (
            narration.replace("á", "a")
            .replace("à", "a")
            .replace("ã", "a")
            .replace("â", "a")
            .replace("é", "e")
            .replace("ê", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("õ", "o")
            .replace("ô", "o")
            .replace("ú", "u")
            .replace("ç", "c")
        )
        for terms, hint in SCENE_VISUAL_HINTS:
            if all(term in narration or term in normalized for term in terms):
                return hint
        return f"vertical cinematic scientific image of {english_subject}"

    def _semantic_scene_directive(self, scene: dict[str, Any], scene_hint: str) -> str:
        narration = str(scene.get("narration_text") or "").strip()
        visual_intent = str(scene.get("visual_intent") or "documentary evidence").replace("_", " ")
        if narration:
            return (
                "depict the specific narration beat with concrete cause-and-effect visual evidence, "
                f"not a generic symbolic background, visual focus: {scene_hint}, scene role: {visual_intent}"
            )
        return "depict the specific narration beat with concrete cause-and-effect visual evidence, not a generic symbolic background"

    def _should_rebuild_image_prompt(self, prompt: str) -> bool:
        prompt_lower = prompt.lower()
        return any(
            phrase in prompt_lower
            for phrase in [
                "ilustracao",
                "mostrando",
                "foco no fenomeno",
                "sem texto",
                "sem watermark",
                "sem capa",
                "sem tipografia",
                "focused on the described phenomenon",
                "showing subject closeup",
                "showing subject in context",
                "showing process or mechanism",
                "showing comparison",
                "showing scale reference",
                "showing historical evocation",
            ]
        )

    def _replace_subject_aliases(self, prompt: str) -> str:
        updated = prompt
        for source, target in sorted(ENGLISH_SUBJECT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            updated = re.sub(rf"\b{re.escape(source)}\b", target, updated, flags=re.IGNORECASE)
        return updated

    def _with_no_text_image_constraints(self, prompt: str) -> str:
        prompt = " ".join(prompt.replace("_", " ").split())
        prompt_lower = prompt.lower()
        constraints = [NO_TEXT_IMAGE_CONSTRAINT]
        extra_constraints = [
            "no letters, no words, no numbers, no symbols",
            "no logo, no watermark, no captions, no subtitles",
            "every object must be completely blank and unbranded",
            "plain containers only, blank cups only, blank packages only",
            "no text on cups, no text on packages, no text on screens",
            "no labels or lettering on any object surface",
            "avoid screens, documents, books, newspapers, signs, dashboards, graphs, labels, and branded packaging",
            "no floating spheres, no random packages, no irrelevant lab props, no generic sci-fi objects",
            "the main subject must be unmistakable and relevant to the narration beat",
        ]
        if "no readable text anywhere" not in prompt_lower:
            prompt = f"{prompt}, {constraints[0]}".strip(", ")
            prompt_lower = prompt.lower()
        for constraint in extra_constraints:
            if constraint.lower() not in prompt_lower:
                prompt = f"{prompt}, {constraint}"
                prompt_lower = prompt.lower()
        return prompt

    def _fallback_query_variants(self, topic_text: str, base_queries: list[str]) -> list[str]:
        queries = [query for query in base_queries if query]
        normalized_topic = topic_text.lower()
        if "buraco" in normalized_topic and "negro" in normalized_topic:
            queries.extend(["black hole space", "black hole astronomy", "accretion disk space"])
        queries.extend([topic_text, f"{topic_text} ciencia", f"{topic_text} fotografia"])
        deduped: list[str] = []
        for query in queries:
            if query not in deduped:
                deduped.append(query)
        return deduped

    def _step_tts(self, session: Session, job: Job, attempt: int) -> list[str]:
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        assert script
        audio_path = self.storage.job_dir(job.job_id) / "audio" / "narration.wav"
        srt_path = self.storage.job_dir(job.job_id) / "audio" / "raw.srt"
        result = self.providers.tts.synthesize(script.full_narration, audio_path, srt_path)
        result = self._fit_tts_duration(audio_path, srt_path, result)
        if not 24_500 <= result["duration_ms"] <= 46_500:
            raise RecoverableStepError("tts duration outside allowed range")
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "narration_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(result),
            **result,
            "normalized_audio_uri": result["audio_uri"],
            "loudness_lufs": -15.0,
        }
        session.execute(delete(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        session.add(NarrationAsset(**model_payload(NarrationAsset, payload)))
        self.storage.persist_json(job.job_id, "narration_asset.json", self._serialize_for_json(payload))
        quality_summary = dict(job.quality_summary or {})
        quality_summary["tts"] = {
            "duration_ms": result["duration_ms"],
            "provider": result["provider"],
            "fallback_used": result.get("provider_metadata", {}).get("fallback_used", False),
            "loudness_normalized": result.get("provider_metadata", {}).get("loudness_normalized", False),
            "loudness_target_lufs": result.get("provider_metadata", {}).get("loudness_target_lufs", -16.0),
            "true_peak_limit_db": result.get("provider_metadata", {}).get("true_peak_limit_db", -1.5),
        }
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "tts.generated", "succeeded", quality_summary["tts"])
        return ["audio/narration.wav", "audio/raw.srt", "narration_asset.json"]

    def _fit_tts_duration(self, audio_path: Path, srt_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        duration_ms = int(result["duration_ms"])
        target_ms: int | None = None
        if duration_ms > 46_500:
            target_ms = 43_500
        elif duration_ms < 24_500:
            target_ms = 25_500
        if target_ms is None:
            return result
        speed = duration_ms / target_ms
        if not 0.5 <= speed <= 2.0:
            return result
        temp_audio = audio_path.with_suffix(".fit.wav")
        try:
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(audio_path),
                    "-filter:a",
                    f"atempo={speed:.6f},loudnorm=I=-16:LRA=11:TP=-1.5",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(temp_audio),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            temp_audio.replace(audio_path)
            self._scale_srt_timings(srt_path, speed)
        finally:
            temp_audio.unlink(missing_ok=True)
        adjusted = dict(result)
        adjusted["duration_ms"] = self._measure_audio_ms(audio_path)
        provider_metadata = dict(adjusted.get("provider_metadata") or {})
        provider_metadata.update(
            {
                "duration_fit_applied": True,
                "duration_fit_original_ms": duration_ms,
                "duration_fit_target_ms": target_ms,
                "duration_fit_speed": round(speed, 6),
            }
        )
        adjusted["provider_metadata"] = provider_metadata
        return adjusted

    def _scale_srt_timings(self, srt_path: Path, speed: float) -> None:
        cues = parse_srt(srt_path.read_text(encoding="utf-8"))
        blocks = []
        for cue in cues:
            start_ms = max(0, round(int(cue["start_ms"]) / speed))
            end_ms = max(start_ms + 1, round(int(cue["end_ms"]) / speed))
            blocks.append(f"{cue['idx']}\n{ms_to_srt(start_ms)} --> {ms_to_srt(end_ms)}\n{cue['text']}")
        srt_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    def _measure_audio_ms(self, audio_path: Path) -> int:
        import wave

        with wave.open(str(audio_path), "rb") as wav_file:
            return int(wav_file.getnframes() / wav_file.getframerate() * 1000)

    def _step_subtitles(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "subtitle_quality_report.json")
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        assert script and narration and scene_plan
        raw_srt_path = path_from_uri(narration.raw_subtitles_uri or "")
        cues = parse_srt(raw_srt_path.read_text(encoding="utf-8"))
        script_words = word_tokens(script.full_narration)
        cursor = 0
        items: list[dict[str, Any]] = []
        for cue in cues:
            cue_words = word_tokens(cue["text"])
            start = cursor
            end = min(len(script_words), start + len(cue_words)) - 1
            items.extend(self._split_subtitle_cue(cue, start, max(end, start)))
            cursor = end + 1
        items = self._repair_subtitle_item_boundaries(items)
        coverage = round(min(cursor / max(len(script_words), 1), 1.0), 3)
        if coverage < 0.99:
            raise RecoverableStepError("subtitle coverage below threshold")
        subtitle_gate = self.subtitle_gate.validate(items, coverage)
        if not subtitle_gate.passed:
            self.storage.persist_json(job.job_id, "subtitle_quality_report.json", {"reasons": subtitle_gate.reasons, "metrics": subtitle_gate.metrics})
            raise RecoverableStepError(f"subtitle quality gate failed: {', '.join(subtitle_gate.reasons[:6])}")
        scene_updates = self._normalize_scene_timings(scene_plan.scenes, narration.duration_ms)
        scene_plan.scenes = scene_updates
        scene_plan.content_hash = stable_hash(scene_updates)
        self.storage.persist_json(
            job.job_id,
            "scene_plan.json",
            self._serialize_for_json(
                {
                    "schema_version": scene_plan.schema_version,
                    "scene_plan_id": scene_plan.scene_plan_id,
                    "job_id": scene_plan.job_id,
                    "created_at": scene_plan.created_at,
                    "content_hash": scene_plan.content_hash,
                    "scene_count": scene_plan.scene_count,
                    "scenes": scene_updates,
                }
            ),
        )
        ass_path = self.storage.job_dir(job.job_id) / "audio" / "subtitles.ass"
        ass_path.write_text(self._render_ass(items), encoding="utf-8")
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "subtitle_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(items),
            "format": "internal",
            "items": items,
            "coverage_ratio": coverage,
            "p95_drift_ms": 0,
            "max_drift_ms": 0,
            "ass_uri": file_uri(ass_path),
            "raw_srt_uri": narration.raw_subtitles_uri,
        }
        session.execute(delete(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        session.add(SubtitleTrack(**model_payload(SubtitleTrack, payload)))
        self.storage.persist_json(job.job_id, "subtitle_track.json", self._serialize_for_json(payload))
        quality_summary = dict(job.quality_summary or {})
        quality_summary["subtitles"] = {**subtitle_gate.metrics, "subtitle_gate_pass": True}
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "subtitle.aligned", "succeeded", quality_summary["subtitles"])
        return ["subtitle_track.json", "audio/subtitles.ass"]

    def _step_background_music(self, session: Session, job: Job, attempt: int) -> list[str]:
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        assert script and topic_plan and narration
        self._remove_stale_quality_report(job.job_id, "background_music_debug.json")
        if not self.settings.background_music_enabled:
            quality_summary = dict(job.quality_summary or {})
            quality_summary["background_music"] = {"enabled": False, "skipped": True, "sound_design_enabled": self.settings.sound_design_enabled}
            job.quality_summary = quality_summary
            session.execute(delete(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
            return []

        music_dir = self.storage.job_dir(job.job_id) / "audio"
        raw_music_path = music_dir / "background_source.wav"
        mixed_audio_path = music_dir / "mixed.wav"
        topic_dict = {
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "title_candidates": topic_plan.title_candidates,
        }
        script_dict = {
            "title": script.title,
            "hook": script.hook,
            "ending": script.ending,
            "full_narration": script.full_narration,
        }
        music_started_at = time.perf_counter()
        try:
            result = self.providers.music.select_track(topic_dict, script_dict, raw_music_path, narration.duration_ms)
        except Exception as exc:
            self._persist_background_music_debug(
                job.job_id,
                attempt=attempt,
                topic_dict=topic_dict,
                script_dict=script_dict,
                target_duration_ms=narration.duration_ms,
                phase="provider_failure",
                elapsed_ms=(time.perf_counter() - music_started_at) * 1000,
                error=exc,
            )
            raise
        self._persist_background_music_debug(
            job.job_id,
            attempt=attempt,
            topic_dict=topic_dict,
            script_dict=script_dict,
            target_duration_ms=narration.duration_ms,
            phase="provider_completed",
            elapsed_ms=(time.perf_counter() - music_started_at) * 1000,
            result=result,
        )
        mixed_result = self._mix_background_music_with_repair(
            narration_path=path_from_uri(narration.audio_uri),
            music_path=raw_music_path,
            output_path=mixed_audio_path,
            target_duration_ms=narration.duration_ms,
            gain_db=self.settings.background_music_gain_db,
        )
        sound_design_metadata = None
        sound_design_file = None
        if self.settings.sound_design_enabled and subtitles and scene_plan:
            sound_design_file = self._generate_sound_design_track(
                job.job_id,
                self._normalize_scene_timings(scene_plan.scenes, narration.duration_ms),
                subtitles.items,
                narration.duration_ms,
            )
            sound_design_path = path_from_uri(sound_design_file["audio_uri"])
            mixed_result = {
                **mixed_result,
                **self._mix_sound_design_track(
                    base_audio_path=mixed_audio_path,
                    sound_design_path=sound_design_path,
                    output_path=mixed_audio_path,
                    gain_db=self.settings.sound_design_gain_db,
                ),
            }
            sound_design_metadata = {
                **sound_design_file,
                "gain_db": self.settings.sound_design_gain_db,
                "enabled": True,
            }
            self.storage.persist_json(job.job_id, "sound_design.json", self._serialize_for_json(sound_design_metadata))
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "music_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash({**result, **mixed_result}),
            "provider": result["provider"],
            "query": result.get("query"),
            "mood": result.get("mood"),
            "source_url": result.get("source_url"),
            "attribution": result.get("attribution"),
            "license_note": result.get("license_note"),
            "audio_uri": result["audio_uri"],
            "mixed_audio_uri": file_uri(mixed_audio_path),
            "duration_ms": narration.duration_ms,
            "gain_db": self.settings.background_music_gain_db,
            "provider_metadata": {
                **dict(result.get("provider_metadata") or {}),
                **mixed_result,
                "sound_design": sound_design_metadata or {"enabled": False},
            },
        }
        session.execute(delete(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        session.add(BackgroundMusicAsset(**model_payload(BackgroundMusicAsset, payload)))
        self.storage.persist_json(job.job_id, "background_music.json", self._serialize_for_json(payload))
        music_telemetry_file = self._persist_repair_telemetry(
            job.job_id,
            "background_music",
            {
                "job_id": job.job_id,
                "attempt": attempt,
                "final_passed": True,
                "attempts": mixed_result.get("mix_attempts_log", []),
            },
        )
        quality_summary = dict(job.quality_summary or {})
        quality_summary["background_music"] = {
            "enabled": True,
            "provider": result["provider"],
            "query": result.get("query"),
            "mood": result.get("mood"),
            "gain_db": self.settings.background_music_gain_db,
            "mixed_audio": "audio/mixed.wav",
            "fallback_used": bool((result.get("provider_metadata") or {}).get("fallback_used")),
            "mix_repair_used": bool(mixed_result.get("mix_repair_used")),
            "sound_design_enabled": bool(sound_design_metadata),
            "sound_design_event_count": int((sound_design_metadata or {}).get("event_count") or 0),
        }
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "background_music.mixed", "succeeded", quality_summary["background_music"])
        outputs = ["audio/background_source.wav", "audio/mixed.wav", "background_music.json", music_telemetry_file]
        if sound_design_metadata:
            outputs.extend(["audio/sound_design.wav", "sound_design.json"])
        return outputs

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
        provider_details = dict(getattr(error, "details", {}) or {})
        provider_metadata = dict((result or {}).get("provider_metadata") or {})
        payload = {
            "job_id": job_id,
            "attempt": attempt,
            "phase": phase,
            "elapsed_ms": elapsed_ms,
            "strict_minimax_validation": self.settings.strict_minimax_validation,
            "background_music_enabled": self.settings.background_music_enabled,
            "background_music_gain_db": self.settings.background_music_gain_db,
            "minimax_music_timeout_sec": self.settings.minimax_music_timeout_sec,
            "canonical_topic": topic_dict.get("canonical_topic"),
            "angle": topic_dict.get("angle"),
            "script_title": script_dict.get("title"),
            "script_hook": script_dict.get("hook"),
            "target_duration_ms": target_duration_ms,
            "provider": (result or {}).get("provider") or getattr(error, "provider", None),
            "query": (result or {}).get("query") or provider_details.get("query"),
            "mood": (result or {}).get("mood") or provider_details.get("mood"),
            "provider_metadata": self._serialize_for_json(provider_metadata),
            "provider_details": self._serialize_for_json(provider_details),
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        self.storage.persist_json(job_id, "background_music_debug.json", self._serialize_for_json(payload))

    def _mix_background_music_with_repair(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
    ) -> dict[str, Any]:
        strategies = ["sidechaincompress+amix+loudnorm", "simple_amix+loudnorm"]
        last_error: str | None = None
        attempts_log: list[dict[str, Any]] = []
        for strategy in strategies:
            try:
                result = self._mix_background_music(
                    narration_path=narration_path,
                    music_path=music_path,
                    output_path=output_path,
                    target_duration_ms=target_duration_ms,
                    gain_db=gain_db,
                    strategy=strategy,
                )
                attempts_log.append({"repair_attempt": len(attempts_log) + 1, "strategy": strategy, "passed": True, "reason_codes": []})
                if strategy != strategies[0]:
                    result["mix_repair_used"] = True
                result["mix_attempts_log"] = attempts_log
                return result
            except RecoverableStepError as exc:
                last_error = str(exc)
                attempts_log.append(
                    {
                        "repair_attempt": len(attempts_log) + 1,
                        "strategy": strategy,
                        "passed": False,
                        "reason_codes": [str(exc)],
                    }
                )
        raise RecoverableStepError(last_error or "background music mix failed")

    def _mix_background_music(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
        strategy: str = "sidechaincompress+amix+loudnorm",
    ) -> dict[str, Any]:
        try:
            return mix_background_music(
                narration_path=narration_path,
                music_path=music_path,
                output_path=output_path,
                target_duration_ms=target_duration_ms,
                gain_db=gain_db,
                strategy=strategy,
            )
        except RuntimeError as exc:
            raise RecoverableStepError(str(exc)) from exc

    def _generate_sound_design_track(
        self,
        job_id: str,
        scenes: list[dict[str, Any]],
        subtitle_items: list[dict[str, Any]],
        duration_ms: int,
    ) -> dict[str, Any]:
        output_path = self.storage.job_dir(job_id) / "audio" / "sound_design.wav"
        return generate_sound_design_track(output_path, scenes, subtitle_items, duration_ms)

    def _mix_sound_design_track(
        self,
        base_audio_path: Path,
        sound_design_path: Path,
        output_path: Path,
        gain_db: float,
    ) -> dict[str, Any]:
        try:
            return mix_sound_design_track(
                base_audio_path=base_audio_path,
                sound_design_path=sound_design_path,
                output_path=output_path,
                gain_db=gain_db,
            )
        except RuntimeError as exc:
            raise RecoverableStepError(str(exc)) from exc

    def _split_subtitle_cue(self, cue: dict[str, Any], token_start: int, token_end: int) -> list[dict[str, Any]]:
        chunks = self._split_caption_by_subtitle_limits(str(cue["text"])) or [str(cue["text"])]
        chunks = self._avoid_weak_subtitle_endings(chunks)
        if len(chunks) == 1:
            return [
                {
                    "idx": cue["idx"],
                    "start_ms": cue["start_ms"],
                    "end_ms": cue["end_ms"],
                    "text": chunks[0],
                    "token_start": token_start,
                    "token_end": token_end,
                }
            ]
        total_words = max(sum(len(word_tokens(chunk)) for chunk in chunks), 1)
        duration_ms = max(int(cue["end_ms"]) - int(cue["start_ms"]), len(chunks))
        split_items: list[dict[str, Any]] = []
        elapsed_words = 0
        token_cursor = token_start
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_word_count = max(len(word_tokens(chunk)), 1)
            start_ms = int(cue["start_ms"]) + round(elapsed_words / total_words * duration_ms)
            elapsed_words += chunk_word_count
            end_ms = int(cue["end_ms"]) if chunk_index == len(chunks) else int(cue["start_ms"]) + round(elapsed_words / total_words * duration_ms)
            chunk_token_end = min(token_end, token_cursor + chunk_word_count - 1)
            split_items.append(
                {
                    "idx": f"{cue['idx']}.{chunk_index}",
                    "start_ms": start_ms,
                    "end_ms": max(end_ms, start_ms + 1),
                    "text": chunk,
                    "token_start": token_cursor,
                    "token_end": chunk_token_end,
                }
            )
            token_cursor = chunk_token_end + 1
        split_items[-1]["end_ms"] = int(cue["end_ms"])
        split_items[-1]["token_end"] = token_end
        return split_items

    def _split_caption_by_subtitle_limits(self, text: str, max_words: int = 14, max_chars: int = 42, max_lines: int = 2) -> list[str]:
        initial_chunks = split_caption_chunks(text, max_chars=max_chars, max_lines=max_lines)
        chunks: list[str] = []
        for chunk in initial_chunks:
            words = chunk.split()
            if len(word_tokens(chunk)) <= max_words:
                chunks.append(chunk)
                continue
            group_count = math.ceil(len(words) / max_words)
            group_size = math.ceil(len(words) / group_count)
            for start in range(0, len(words), group_size):
                candidate = " ".join(words[start : start + group_size])
                if len(word_tokens(candidate)) <= max_words and self._subtitle_chunk_fits(candidate, max_chars=max_chars, max_lines=max_lines):
                    chunks.append(candidate)
                    continue
                chunks.extend(split_caption_chunks(candidate, max_chars=max_chars, max_lines=max_lines))
        return chunks

    def _avoid_weak_subtitle_endings(self, chunks: list[str]) -> list[str]:
        repaired = [chunk for chunk in chunks if chunk.strip()]
        for index in range(len(repaired) - 1):
            current_text, next_text, _ = self._rebalance_subtitle_boundary(repaired[index], repaired[index + 1])
            if current_text:
                repaired[index] = current_text
            if next_text:
                repaired[index + 1] = next_text
            else:
                repaired[index + 1] = ""
        return [chunk for chunk in repaired if chunk.strip()]

    def _subtitle_chunk_fits(self, text: str, max_chars: int = 42, max_lines: int = 2, max_words: int = 14) -> bool:
        normalized = str(text).strip()
        if not normalized:
            return False
        if len(word_tokens(normalized)) > max_words:
            return False
        return len(split_caption_chunks(normalized, max_chars=max_chars, max_lines=max_lines)) == 1

    def _rebalance_subtitle_boundary(
        self,
        current_text: str,
        next_text: str,
        max_chars: int = 42,
        max_lines: int = 2,
        max_words: int = 14,
    ) -> tuple[str, str, int]:
        current_words = str(current_text).split()
        next_words = str(next_text).split()
        if not current_words or not next_words:
            return str(current_text).strip(), str(next_text).strip(), 0
        ending_tokens = word_tokens(current_words[-1])
        ending = ending_tokens[0] if ending_tokens else ""
        if ending not in BAD_ENDINGS:
            return " ".join(current_words), " ".join(next_words), 0

        for moved_count in range(1, len(next_words) + 1):
            candidate_current = " ".join([*current_words, *next_words[:moved_count]])
            candidate_next = " ".join(next_words[moved_count:])
            candidate_tokens = word_tokens(candidate_current)
            candidate_ending = candidate_tokens[-1] if candidate_tokens else ""
            if candidate_ending in BAD_ENDINGS:
                continue
            if not self._subtitle_chunk_fits(candidate_current, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            if candidate_next and not self._subtitle_chunk_fits(candidate_next, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            return candidate_current, candidate_next, moved_count

        for moved_count in range(1, len(current_words)):
            candidate_current = " ".join(current_words[:-moved_count])
            candidate_next = " ".join([*current_words[-moved_count:], *next_words])
            if not candidate_current:
                continue
            candidate_tokens = word_tokens(candidate_current)
            candidate_ending = candidate_tokens[-1] if candidate_tokens else ""
            if candidate_ending in BAD_ENDINGS:
                continue
            if not self._subtitle_chunk_fits(candidate_current, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            if not self._subtitle_chunk_fits(candidate_next, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            return candidate_current, candidate_next, -moved_count

        return " ".join(current_words), " ".join(next_words), 0

    def _repair_subtitle_item_boundaries(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        repaired = [dict(item) for item in items if str(item.get("text") or "").strip()]
        for index in range(len(repaired) - 1):
            current = repaired[index]
            following = repaired[index + 1]
            original_current_word_count = len(word_tokens(str(current["text"])))
            original_following_word_count = len(word_tokens(str(following["text"])))
            if not original_current_word_count or not original_following_word_count:
                continue
            current_text, next_text, delta = self._rebalance_subtitle_boundary(str(current["text"]), str(following["text"]))
            if delta == 0:
                continue
            current["text"] = current_text
            following["text"] = next_text
            current["token_end"] = int(current.get("token_end", current.get("token_start", 0))) + delta
            following["token_start"] = int(following.get("token_start", following.get("token_end", 0))) + delta
            pair_start_ms = int(current["start_ms"])
            pair_end_ms = int(following["end_ms"])
            pair_duration_ms = max(pair_end_ms - pair_start_ms, 2)
            new_current_word_count = len(word_tokens(current_text))
            new_following_word_count = len(word_tokens(next_text))
            total_words = max(new_current_word_count + new_following_word_count, 1)
            boundary_ms = pair_start_ms + round(new_current_word_count / total_words * pair_duration_ms)
            boundary_ms = max(pair_start_ms + 1, min(pair_end_ms - 1, boundary_ms))
            current["end_ms"] = boundary_ms
            following["start_ms"] = boundary_ms
        return [item for item in repaired if str(item.get("text") or "").strip()]

    def _render_ass(self, items: list[dict[str, Any]]) -> str:
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H40000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        lines = [header]
        for item in items:
            start = self._ms_to_ass(item["start_ms"])
            end = self._ms_to_ass(item["end_ms"])
            text = wrap_caption(item["text"]).replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
        return "\n".join(lines) + "\n"

    def _remove_stale_quality_report(self, job_id: str, relative_path: str) -> None:
        try:
            (self.storage.job_dir(job_id) / relative_path).unlink(missing_ok=True)
        except OSError:
            pass

    def _ms_to_ass(self, ms: int) -> str:
        hours, rem = divmod(ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        centis = round(millis / 10)
        return f"{hours}:{minutes:02}:{seconds:02}.{centis:02}"

    def _step_render(self, session: Session, job: Job, attempt: int) -> list[str]:
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        selected_assets = session.scalars(
            select(SceneAsset).where(SceneAsset.job_id == job.job_id, SceneAsset.selected.is_(True)).order_by(SceneAsset.scene_id)
        ).all()
        assert scene_plan and narration and subtitles and selected_assets
        final_video = self.storage.job_dir(job.job_id) / "render" / "final.mp4"
        poster = self.storage.job_dir(job.job_id) / "render" / "poster.jpg"
        ffmpeg_log = self.storage.job_dir(job.job_id) / "render" / "ffmpeg.log"
        ensure_dir(final_video.parent)
        total_duration = narration.duration_ms / 1000
        audio_path = path_from_uri(background_music.mixed_audio_uri) if background_music and background_music.mixed_audio_uri else path_from_uri(narration.audio_uri)
        ass_path = path_from_uri(subtitles.ass_uri or "")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        command = [ffmpeg, "-y"]
        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        scene_segments = self._normalize_scene_timings(scene_plan.scenes, narration.duration_ms)
        if scene_plan.scenes != scene_segments:
            scene_plan.scenes = scene_segments
            scene_plan.content_hash = stable_hash(scene_segments)
            self.storage.persist_json(
                job.job_id,
                "scene_plan.json",
                self._serialize_for_json(
                    {
                        "schema_version": scene_plan.schema_version,
                        "scene_plan_id": scene_plan.scene_plan_id,
                        "job_id": scene_plan.job_id,
                        "created_at": scene_plan.created_at,
                        "content_hash": scene_plan.content_hash,
                        "scene_count": scene_plan.scene_count,
                        "scenes": scene_segments,
                    }
                ),
            )
        for index, scene in enumerate(scene_segments):
            asset = next(item for item in selected_assets if item.scene_id == scene["scene_id"])
            start = scene["actual_start_ms"] / 1000
            end = scene["actual_end_ms"] / 1000
            duration = max(0.5, end - start)
            command.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path_from_uri(asset.uri))])
            filter_parts.append(
                f"[{index}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,format=yuv420p[v{index}]"
            )
            concat_inputs.append(f"[v{index}]")
        command.extend(["-i", str(audio_path)])
        filter_parts.append(f"{''.join(concat_inputs)}concat=n={len(selected_assets)}:v=1:a=0[video]")
        ass_filter_path = ass_path.as_posix().replace("\\", "/").replace(":", "\\\\:")
        filter_parts.append(f"[video]ass={ass_filter_path}[vout]")
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-map",
                f"{len(selected_assets)}:a",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-b:v",
                "2500k",
                "-minrate",
                "800k",
                "-maxrate",
                "4500k",
                "-bufsize",
                "9000k",
                "-x264-params",
                "nal-hrd=cbr:force-cfr=1",
                "-pix_fmt",
                "yuv420p",
                "-af",
                "aresample=async=1:first_pts=0",
                "-ar",
                "48000",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(final_video),
            ]
        )
        render_gate, render_log = self._render_with_repair(job.job_id, command, final_video, ffmpeg_log, narration.duration_ms)
        ffmpeg_log.write_text(render_log, encoding="utf-8")
        Image.open(path_from_uri(selected_assets[0].uri)).resize((540, 960)).save(poster, format="JPEG")
        duration_ms = int(render_gate.metrics.get("duration_ms") or narration.duration_ms)
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "render_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(final_video.read_bytes()),
            "video_uri": file_uri(final_video),
            "poster_uri": file_uri(poster),
            "waveform_uri": None,
            "duration_ms": duration_ms,
            "resolution": "1080x1920",
            "video_codec": "H.264",
            "audio_codec": "AAC",
            "filesize_bytes": final_video.stat().st_size,
            "ffmpeg_log_uri": file_uri(ffmpeg_log),
        }
        session.execute(delete(RenderOutput).where(RenderOutput.job_id == job.job_id))
        session.add(RenderOutput(**model_payload(RenderOutput, payload)))
        self.storage.persist_json(job.job_id, "render_output.json", self._serialize_for_json(payload))
        render_telemetry_file = self._persist_repair_telemetry(
            job.job_id,
            "render",
            {
                "job_id": job.job_id,
                "attempt": attempt,
                "final_passed": True,
                "attempts": render_gate.metrics.get("render_attempts_log", []),
            },
        )
        quality_summary = dict(job.quality_summary or {})
        quality_summary["render"] = {
            **render_gate.metrics,
            "render_gate_pass": True,
            "duration_ms": duration_ms,
            "resolution": "1080x1920",
            "audio_loudness_target_lufs": -16.0,
            "audio_true_peak_limit_db": -1.5,
            "background_music_mixed": bool(background_music and background_music.mixed_audio_uri),
            "render_repair_used": len(render_gate.metrics.get("render_attempts_log", [])) > 1,
        }
        job.quality_summary = quality_summary
        return ["render/final.mp4", "render/poster.jpg", "render/ffmpeg.log", "render_output.json", render_telemetry_file]

    def _render_with_repair(
        self,
        job_id: str,
        base_command: list[str],
        final_video: Path,
        ffmpeg_log: Path,
        expected_duration_ms: int,
    ) -> tuple[Any, str]:
        attempts: list[list[str]] = [
            list(base_command),
            self._mutate_render_command_for_repair(base_command, repair_mode="quality_safe"),
        ]
        collected_logs: list[str] = []
        last_gate = None
        attempts_log: list[dict[str, Any]] = []
        for index, command in enumerate(attempts, start=1):
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            collected_logs.append(f"=== render attempt {index} ===\n{result.stdout}\n{result.stderr}")
            if result.returncode != 0:
                attempts_log.append(
                    {
                        "repair_attempt": index,
                        "strategy": "base" if index == 1 else "quality_safe",
                        "passed": False,
                        "reason_codes": ["ffmpeg_render_failed"],
                    }
                )
                continue
            render_gate = self.render_gate.validate(final_video, expected_duration_ms)
            last_gate = render_gate
            attempts_log.append(
                {
                    "repair_attempt": index,
                    "strategy": "base" if index == 1 else "quality_safe",
                    "passed": render_gate.passed,
                    "reason_codes": render_gate.reasons,
                }
            )
            if render_gate.passed:
                render_gate.metrics["render_attempts_log"] = attempts_log
                return render_gate, "\n".join(collected_logs)
            if index < len(attempts):
                continue
        if last_gate is not None:
            last_gate.metrics["render_attempts_log"] = attempts_log
            self.storage.persist_json(
                job_id,
                "render_quality_report.json",
                {"reasons": last_gate.reasons, "metrics": last_gate.metrics},
            )
            raise RecoverableStepError(f"render quality gate failed: {', '.join(last_gate.reasons[:6])}")
        raise RecoverableStepError("ffmpeg render failed")

    def _mutate_render_command_for_repair(self, command: list[str], repair_mode: str) -> list[str]:
        mutated = list(command)
        if repair_mode != "quality_safe":
            return mutated
        replacements = {
            "-preset": "faster",
            "-crf": "21",
            "-b:v": "3200k",
            "-minrate": "1200k",
            "-maxrate": "5200k",
            "-bufsize": "10400k",
            "-af": "aresample=async=1:first_pts=0,alimiter=limit=0.95",
            "-ar": "48000",
        }
        for flag, value in replacements.items():
            if flag in mutated:
                idx = mutated.index(flag)
                if idx + 1 < len(mutated):
                    mutated[idx + 1] = value
        return mutated

    def _normalize_scene_timings(self, scenes: list[dict[str, Any]], total_duration_ms: int) -> list[dict[str, Any]]:
        if not scenes:
            return []
        total_duration_ms = max(int(total_duration_ms), 1)
        total_tokens = max(max(int(scene.get("token_end", 0)) + 1 for scene in scenes), 1)
        normalized: list[dict[str, Any]] = []
        start_boundaries: list[int] = []
        for scene in scenes:
            fallback_start = round(int(scene.get("token_start", 0)) / total_tokens * total_duration_ms)
            start_ms = scene.get("actual_start_ms")
            if not isinstance(start_ms, int):
                start_ms = fallback_start
            start_boundaries.append(max(0, min(int(start_ms), total_duration_ms)))
        for index, scene in enumerate(scenes):
            start_ms = start_boundaries[index]
            next_boundary = total_duration_ms if index == len(scenes) - 1 else start_boundaries[index + 1]
            fallback_end = round((int(scene.get("token_end", scene.get("token_start", 0))) + 1) / total_tokens * total_duration_ms)
            end_ms = scene.get("actual_end_ms")
            if not isinstance(end_ms, int):
                end_ms = fallback_end
            if index < len(scenes) - 1:
                end_ms = min(int(end_ms), next_boundary)
            else:
                end_ms = total_duration_ms
            min_duration_ms = 500 if index == len(scenes) - 1 else 250
            if end_ms <= start_ms:
                end_ms = min(total_duration_ms, start_ms + min_duration_ms)
            normalized.append(
                {
                    **scene,
                    "actual_start_ms": start_ms,
                    "actual_end_ms": end_ms,
                }
            )
        normalized[-1]["actual_end_ms"] = total_duration_ms
        return normalized

    def _step_monetization_readiness(self, session: Session, job: Job, attempt: int) -> list[str]:
        report = self._build_monetization_report(session, job)
        self.storage.persist_json(job.job_id, "rights_registry.json", self._serialize_for_json(report["rights_registry"]))
        self.storage.persist_json(job.job_id, "ai_disclosure.json", self._serialize_for_json(report["ai_disclosure"]))
        self.storage.persist_json(job.job_id, "fact_claims_report.json", self._serialize_for_json(report["fact_claims_report"]))
        self.storage.persist_json(job.job_id, "channel_repetition_report.json", self._serialize_for_json(report["channel_repetition_report"]))
        self.storage.persist_json(job.job_id, "metadata_review.json", self._serialize_for_json(report["metadata_review"]))
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
        self._append_event(
            job.job_id,
            "monetization.checked",
            "succeeded",
            {
                "passed": report["passed"],
                "final_status": report["final_status"],
                "hard_blockers": report["hard_blockers"],
                "manual_required": report["manual_required"],
            },
        )
        return [
            "rights_registry.json",
            "ai_disclosure.json",
            "fact_claims_report.json",
            "channel_repetition_report.json",
            "metadata_review.json",
            "monetization_report.json",
        ]

    def _build_monetization_report(self, session: Session, job: Job, extra_confirmations: set[str] | None = None) -> dict[str, Any]:
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job.job_id))
        assets = session.scalars(select(SceneAsset).where(SceneAsset.job_id == job.job_id, SceneAsset.selected.is_(True)).order_by(SceneAsset.scene_id)).all()
        fact_pack = self._read_job_json(job.job_id, "fact_pack.json")
        script_artifact = self._read_job_json(job.job_id, "script.json")
        tags = self._build_publish_hashtags(topic_plan, script)
        checklist = {
            "script_gate_pass": bool((job.quality_summary or {}).get("script", {}).get("script_quality_gate_pass")),
            "scene_plan_gate_pass": bool((job.quality_summary or {}).get("scene_plan", {}).get("scene_plan_gate_pass")),
            "asset_gate_pass": bool((job.quality_summary or {}).get("assets", {}).get("semantic_threshold_pass")),
            "subtitle_gate_pass": bool((job.quality_summary or {}).get("subtitles", {}).get("subtitle_gate_pass")),
            "render_gate_pass": bool((job.quality_summary or {}).get("render", {}).get("render_gate_pass")),
        }
        confirmations = self._manual_monetization_confirmations(session, job.job_id)
        confirmations.update(extra_confirmations or set())

        rights_registry = self._build_rights_registry(job, assets, narration, background_music)
        ai_disclosure = self._build_ai_disclosure_report(assets)
        fact_claims_report = self._build_fact_claims_report(script, topic_plan, fact_pack, script_artifact)
        channel_repetition_report = self._build_channel_repetition_report(session, job, topic_plan, script)
        metadata_review = self._build_metadata_review(topic_plan, script, tags)
        publish_readiness = self._publish_readiness_report(
            script,
            topic_plan,
            fact_pack,
            tags,
            checklist,
            script_artifact,
            self._provider_publish_audit(script_artifact, fact_pack, tags),
        )

        hard_blockers: list[str] = []
        manual_required: list[str] = []
        warnings: list[str] = []
        if not all(checklist.values()):
            hard_blockers.append("quality_gate_not_passed")
        if not rights_registry["all_commercial_rights_confirmed"]:
            manual_required.append("rights_confirmation_required")
        if ai_disclosure["youtube_disclosure_required"] and "ai_disclosure_confirmed" not in confirmations:
            manual_required.append("youtube_ai_disclosure_toggle_required")
        if fact_claims_report["requires_fact_review"] and "fact_review_confirmed" not in confirmations:
            manual_required.append("fact_review_required")
        if metadata_review["requires_metadata_review"] and "metadata_confirmed" not in confirmations:
            manual_required.append("metadata_review_required")
        if channel_repetition_report["repetition_risk"] != "low" and "originality_confirmed" not in confirmations:
            manual_required.append("originality_review_required")
        if "rights_confirmed" in confirmations:
            manual_required = [item for item in manual_required if item != "rights_confirmation_required"]
        warnings.extend(publish_readiness["reasons"])
        if not self.settings.allow_synthetic_visuals_for_monetization and ai_disclosure["contains_synthetic_visuals"]:
            hard_blockers.append("synthetic_visuals_disabled_by_policy")
        if render and render.duration_ms > 60_000:
            hard_blockers.append("shorts_duration_over_60s")
        if channel_repetition_report["repetition_risk"] == "high" and "originality_confirmed" not in confirmations:
            hard_blockers.append("channel_repetition_high")

        hard_blockers = list(dict.fromkeys(hard_blockers))
        manual_required = list(dict.fromkeys(manual_required))
        warnings = list(dict.fromkeys(warnings))
        human_review_checklist = self._build_human_review_checklist(
            rights_registry=rights_registry,
            ai_disclosure=ai_disclosure,
            fact_claims_report=fact_claims_report,
            metadata_review=metadata_review,
            channel_repetition_report=channel_repetition_report,
            confirmations=confirmations,
        )
        passed = not hard_blockers and not manual_required
        final_status = "ready_for_upload" if passed else ("blocked_for_monetization" if hard_blockers else "monetization_review")
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "created_at": iso_now(),
            "passed": passed,
            "final_status": final_status,
            "hard_blockers": hard_blockers,
            "manual_required": manual_required,
            "warnings": warnings,
            "manual_confirmations": sorted(confirmations),
            "human_review_checklist": human_review_checklist,
            "quality_checklist": checklist,
            "rights_registry": rights_registry,
            "ai_disclosure": ai_disclosure,
            "fact_claims_report": fact_claims_report,
            "channel_repetition_report": channel_repetition_report,
            "metadata_review": metadata_review,
            "publish_readiness": publish_readiness,
        }

    def _build_human_review_checklist(
        self,
        rights_registry: dict[str, Any],
        ai_disclosure: dict[str, Any],
        fact_claims_report: dict[str, Any],
        metadata_review: dict[str, Any],
        channel_repetition_report: dict[str, Any],
        confirmations: set[str],
    ) -> dict[str, Any]:
        return build_human_review_checklist(
            rights_registry=rights_registry,
            ai_disclosure=ai_disclosure,
            fact_claims_report=fact_claims_report,
            metadata_review=metadata_review,
            channel_repetition_report=channel_repetition_report,
            confirmations=confirmations,
        )

    def _build_rights_registry(
        self,
        job: Job,
        assets: list[SceneAsset],
        narration: NarrationAsset | None,
        background_music: BackgroundMusicAsset | None,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for asset in assets:
            provider = asset.provider.lower()
            confirmed = bool(self.settings.minimax_commercial_rights_confirmed) if provider == "minimax" else bool(asset.license_note)
            evidence_url = self.settings.minimax_rights_evidence_url if provider == "minimax" else asset.license_note
            entries.append(
                {
                    "asset_type": asset.kind,
                    "scene_id": asset.scene_id,
                    "provider": asset.provider,
                    "uri": asset.uri,
                    "commercial_use_allowed": confirmed,
                    "license_source": asset.license_note or ("YTS_MINIMAX_COMMERCIAL_RIGHTS_CONFIRMED" if provider == "minimax" else None),
                    "rights_evidence_url": evidence_url,
                    "evidence_required": confirmed and provider == "minimax" and not bool(evidence_url),
                    "requires_attribution": bool(asset.attribution),
                    "attribution": asset.attribution,
                    "terms_checked_at": iso_now() if confirmed else None,
                    "review_required": not confirmed or (confirmed and provider == "minimax" and not bool(evidence_url)),
                }
            )
        if narration:
            provider = narration.provider.lower()
            confirmed = bool(self.settings.edge_tts_commercial_rights_confirmed) if provider == "edge_tts" else provider == "synthetic_wav"
            evidence_url = self.settings.edge_tts_rights_evidence_url if provider == "edge_tts" else "local_synthetic_test_audio"
            entries.append(
                {
                    "asset_type": "voice",
                    "provider": narration.provider,
                    "voice": narration.voice,
                    "uri": narration.audio_uri,
                    "commercial_use_allowed": confirmed,
                    "license_source": "YTS_EDGE_TTS_COMMERCIAL_RIGHTS_CONFIRMED" if provider == "edge_tts" else "local_synthetic_test_audio",
                    "rights_evidence_url": evidence_url,
                    "evidence_required": confirmed and provider == "edge_tts" and not bool(evidence_url),
                    "requires_attribution": False,
                    "terms_checked_at": iso_now() if confirmed else None,
                    "review_required": not confirmed or (confirmed and provider == "edge_tts" and not bool(evidence_url)),
                }
            )
        if background_music:
            provider = background_music.provider.lower()
            license_source = background_music.license_note or ("local_mock_background_music" if provider == "mock_music" else None)
            entries.append(
                {
                    "asset_type": "music",
                    "provider": background_music.provider,
                    "uri": background_music.audio_uri,
                    "commercial_use_allowed": bool(license_source),
                    "license_source": license_source,
                    "requires_attribution": bool(background_music.attribution),
                    "attribution": background_music.attribution,
                    "terms_checked_at": iso_now() if license_source else None,
                    "review_required": not bool(license_source),
                }
            )
            sound_design = dict(background_music.provider_metadata or {}).get("sound_design") or {}
            if sound_design.get("enabled"):
                entries.append(
                    {
                        "asset_type": "sound_design",
                        "provider": sound_design.get("provider") or "local_sfx",
                        "uri": sound_design.get("audio_uri"),
                        "commercial_use_allowed": True,
                        "license_source": sound_design.get("license_note") or "local_generated_sound_design",
                        "requires_attribution": False,
                        "terms_checked_at": iso_now(),
                        "review_required": False,
                    }
                )
        else:
            entries.append(
                {
                    "asset_type": "music",
                    "provider": "none",
                    "commercial_use_allowed": True,
                    "license_source": "no_music_used",
                    "requires_attribution": False,
                    "terms_checked_at": iso_now(),
                    "review_required": False,
                }
            )
        return {
            "entries": entries,
            "all_commercial_rights_confirmed": all(entry["commercial_use_allowed"] and not entry.get("evidence_required") for entry in entries),
            "review_required_count": sum(1 for entry in entries if entry["review_required"]),
            "evidence_required_count": sum(1 for entry in entries if entry.get("evidence_required")),
        }

    def _build_ai_disclosure_report(self, assets: list[SceneAsset]) -> dict[str, Any]:
        synthetic_assets = [asset for asset in assets if asset.provider.lower() in {"minimax", "mock"}]
        realistic_terms = re.compile(r"\b(?:human|person|face|eyes|brain|body|portrait|realistic|documentary|cinematic|figure)\b", re.IGNORECASE)
        realistic_assets = [
            {
                "scene_id": asset.scene_id,
                "provider": asset.provider,
                "prompt_snapshot": (asset.prompt_snapshot or "")[:260],
            }
            for asset in synthetic_assets
            if realistic_terms.search(asset.prompt_snapshot or "")
        ]
        contains_synthetic = bool(synthetic_assets)
        disclosure_required = contains_synthetic if self.settings.conservative_synthetic_disclosure else contains_synthetic and bool(realistic_assets)
        return {
            "contains_synthetic_visuals": contains_synthetic,
            "youtube_disclosure_required": disclosure_required,
            "description_notice": "Imagens ilustrativas geradas por IA." if contains_synthetic else None,
            "reason": (
                "Conservative synthetic disclosure mode: AI-generated visuals present."
                if disclosure_required and self.settings.conservative_synthetic_disclosure
                else "Realistic AI-generated illustrative visuals." if disclosure_required else "No realistic synthetic disclosure trigger detected."
            ),
            "policy_mode": "conservative" if self.settings.conservative_synthetic_disclosure else "realistic_only",
            "synthetic_asset_count": len(synthetic_assets),
            "realistic_synthetic_assets": realistic_assets,
        }

    def _build_fact_claims_report(
        self,
        script: Script | None,
        topic_plan: TopicPlan | None,
        fact_pack: dict[str, Any],
        script_artifact: dict[str, Any],
    ) -> dict[str, Any]:
        script_dict = {**(self._script_to_dict(script) if script else {}), **(script_artifact or {})}
        fact_risk = self.script_gate._fact_risk_report(script_dict) if script_dict else {"claims": [], "claim_count": 0, "blocked": False}  # noqa: SLF001
        source_ids = script_dict.get("source_fact_ids") or script_dict.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        facts = fact_pack.get("facts") or []
        sources = fact_pack.get("sources") or []
        valid_fact_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
        grounded_ids = [str(item) for item in source_ids if str(item) in valid_fact_ids]
        factual_topic = bool(
            topic_plan
            and re.search(
                r"\b(?:por que|porque|como|ci[eê]ncia|f[ií]sica|biologia|engenharia|hist[oó]ria|sa[uú]de|m[eé]dico|animal|animais|flamingo|torre|c[eé]rebro|neuro|estat[ií]stica)\b",
                f"{topic_plan.canonical_topic} {topic_plan.angle}",
                re.IGNORECASE,
            )
        )
        requires_review = (
            fact_pack.get("status") != "verified"
            and (factual_topic or fact_risk.get("claim_count", 0) > 0 or len(word_tokens(script_dict.get("full_narration", ""))) >= 45)
        )
        claim_sources = [
            {
                "claim": fact.get("claim"),
                "fact_id": fact.get("fact_id"),
                "source_id": fact.get("source_id"),
                "source_url": next((source.get("url") for source in sources if source.get("source_id") == fact.get("source_id")), None),
            }
            for fact in facts
        ]
        return {
            "fact_pack_status": fact_pack.get("status") or "missing",
            "requires_fact_review": requires_review,
            "source_fact_ids": list(source_ids),
            "grounded_source_fact_ids": grounded_ids,
            "claim_sources": claim_sources,
            "risk_report": fact_risk,
            "editorial_rule": fact_pack.get("editorial_rule"),
        }

    def _build_channel_repetition_report(self, session: Session, job: Job, topic_plan: TopicPlan | None, script: Script | None) -> dict[str, Any]:
        if not topic_plan or not script:
            return {"repetition_risk": "unknown", "max_similarity": 0.0, "matches": []}
        rows = session.execute(
            select(Job.job_id, Job.topic_summary, Script.title, Script.hook, Script.ending, Script.estimated_duration_sec, Script.body_beats)
            .join(Script, Script.job_id == Job.job_id)
            .where(Job.job_id != job.job_id)
            .order_by(Job.created_at.desc())
            .limit(30)
        ).all()
        recent_rows = []
        for other_job_id, topic_summary, title, hook, ending, estimated_duration_sec, body_beats in rows:
            recent_rows.append(
                {
                    "job_id": other_job_id,
                    "topic_summary": topic_summary,
                    "title": title,
                    "hook": hook,
                    "ending": ending,
                    "estimated_duration_sec": estimated_duration_sec,
                    "body_beats": body_beats,
                }
            )
        return build_channel_repetition_report(
            current={
                "canonical_topic": topic_plan.canonical_topic,
                "angle": topic_plan.angle,
                "script": self._script_to_dict(script),
            },
            recent_rows=recent_rows,
        )

    def _build_metadata_review(self, topic_plan: TopicPlan | None, script: Script | None, tags: list[str]) -> dict[str, Any]:
        weak_tags = [tag for tag in tags if tag.lower() not in {"#shorts"} and (tag.lstrip("#") in self._weak_hashtag_terms() or len(tag.lstrip("#")) < 4)]
        title = script.title if script else ""
        title_words = len(word_tokens(title))
        reasons = []
        if weak_tags:
            reasons.append("weak_hashtags")
        if not 5 <= title_words <= 14:
            reasons.append("title_length_outside_short_window")
        if len(tags) > 5:
            reasons.append("too_many_hashtags")
        suggested_tags = [tag for tag in tags if tag not in weak_tags][:5]
        if "#shorts" not in [tag.lower() for tag in suggested_tags]:
            suggested_tags.insert(0, "#shorts")
        return {
            "requires_metadata_review": bool(reasons),
            "reasons": reasons,
            "title": title[:100],
            "hashtag_count": len(tags),
            "weak_hashtags": weak_tags,
            "suggested_hashtags": list(dict.fromkeys(suggested_tags))[:5],
            "topic_keywords": word_tokens(f"{topic_plan.canonical_topic} {topic_plan.angle}")[:8] if topic_plan else [],
        }

    def _manual_monetization_confirmations(self, session: Session, job_id: str) -> set[str]:
        reviews = session.scalars(select(ReviewRecord).where(ReviewRecord.job_id == job_id).order_by(ReviewRecord.created_at)).all()
        confirmations: set[str] = set()
        for review in reviews:
            confirmations.update(str(item) for item in (review.reason_codes or []) if str(item).endswith("_confirmed"))
        return confirmations

    def _build_job_performance_report(self, metrics: list[PerformanceMetric]) -> dict[str, Any]:
        serialized = [
            {
                "metric_id": metric.metric_id,
                "job_id": metric.job_id,
                "created_at": metric.created_at.isoformat() if metric.created_at else None,
                "source": metric.source,
                "retention_percent": metric.retention_percent,
                "viewed_vs_swiped_away_percent": metric.viewed_vs_swiped_away_percent,
                "rewatch_rate": metric.rewatch_rate,
                "likes": metric.likes,
                "shares": metric.shares,
                "comments": metric.comments,
                "rpm_usd": metric.rpm_usd,
                "monetization_status": metric.monetization_status,
                "notes": metric.notes,
            }
            for metric in metrics
        ]
        latest = serialized[0] if serialized else None
        recommendations: list[str] = []
        if latest:
            if latest.get("retention_percent") is not None and latest["retention_percent"] < 55:
                recommendations.append("tighten_first_half_retention")
            if latest.get("viewed_vs_swiped_away_percent") is not None and latest["viewed_vs_swiped_away_percent"] < 50:
                recommendations.append("stronger_first_frame_hook")
            if latest.get("rewatch_rate") is not None and latest["rewatch_rate"] < 1.05:
                recommendations.append("improve_loop_close")
            if latest.get("monetization_status") and latest["monetization_status"] not in {"monetized", "eligible", "approved"}:
                recommendations.append("review_monetization_status")
        return {
            "schema_version": self.settings.schema_version,
            "latest": latest,
            "metrics": serialized,
            "recommendations": recommendations,
        }

    def _step_publish(self, session: Session, job: Job, attempt: int) -> list[str]:
        publish_package = self._build_publish_package(session, job)
        self.storage.persist_json(job.job_id, "publish_package.json", self._serialize_for_json(publish_package))
        artifact_index = {
            "request": "request.json",
            "topic_plan": "topic_plan.json",
            "script": "script.json",
            "scene_plan": "scene_plan.json",
            "audio": "audio/narration.wav",
            "background_music": "audio/background_source.wav",
            "mixed_audio": "audio/mixed.wav",
            "raw_subtitles": "audio/raw.srt",
            "subtitles": "audio/subtitles.ass",
            "render": "render/final.mp4",
            "events": "events.jsonl",
            "ffmpeg_log": "render/ffmpeg.log",
            "rights_registry": "rights_registry.json",
            "ai_disclosure": "ai_disclosure.json",
            "fact_claims_report": "fact_claims_report.json",
            "channel_repetition_report": "channel_repetition_report.json",
            "metadata_review": "metadata_review.json",
            "monetization_report": "monetization_report.json",
            "publish_package": "publish_package.json",
        }
        if (self.storage.job_dir(job.job_id) / "audio" / "sound_design.wav").exists():
            artifact_index["sound_design"] = "audio/sound_design.wav"
            artifact_index["sound_design_report"] = "sound_design.json"
        job.artifact_index = artifact_index
        self.storage.persist_json(
            job.job_id,
            "job_manifest.json",
            {
                "schema_version": self.settings.schema_version,
                "job_id": job.job_id,
                "created_at": iso_now(),
                "content_hash": stable_hash(artifact_index),
                "artifact_index": artifact_index,
                "quality_summary": job.quality_summary or {},
            },
        )
        return ["job_manifest.json", "publish_package.json"]

    def _build_publish_package(self, session: Session, job: Job) -> dict[str, Any]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        title = script.title if script else (request.seed_theme if request else job.topic_summary or job.job_id)
        fact_pack = self._read_job_json(job.job_id, "fact_pack.json")
        script_artifact = self._read_job_json(job.job_id, "script.json")
        monetization_report = self._read_job_json(job.job_id, "monetization_report.json")
        tags = self._build_publish_hashtags(topic_plan, script)
        if monetization_report.get("metadata_review", {}).get("suggested_hashtags"):
            tags = list(monetization_report["metadata_review"]["suggested_hashtags"])
        ai_notice = monetization_report.get("ai_disclosure", {}).get("description_notice")
        description = "\n".join(
            [part for part in [script.full_narration if script else title, ai_notice, "", " ".join(dict.fromkeys(tags))] if part is not None]
        )
        checklist = {
            "script_gate_pass": bool((job.quality_summary or {}).get("script", {}).get("script_quality_gate_pass")),
            "scene_plan_gate_pass": bool((job.quality_summary or {}).get("scene_plan", {}).get("scene_plan_gate_pass")),
            "asset_gate_pass": bool((job.quality_summary or {}).get("assets", {}).get("semantic_threshold_pass")),
            "subtitle_gate_pass": bool((job.quality_summary or {}).get("subtitles", {}).get("subtitle_gate_pass")),
            "render_gate_pass": bool((job.quality_summary or {}).get("render", {}).get("render_gate_pass")),
        }
        minimax_audit = self._provider_publish_audit(script_artifact, fact_pack, tags)
        readiness = self._publish_readiness_report(script, topic_plan, fact_pack, tags, checklist, script_artifact, minimax_audit)
        if monetization_report:
            readiness = {
                **readiness,
                "monetization_gate_pass": bool(monetization_report.get("passed")),
                "monetization_final_status": monetization_report.get("final_status"),
                "monetization_hard_blockers": monetization_report.get("hard_blockers", []),
                "monetization_manual_required": monetization_report.get("manual_required", []),
            }
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "created_at": iso_now(),
            "status": "ready_for_publish" if readiness["passed"] else "needs_manual_review",
            "title": title[:100],
            "description": description[:4900],
            "hashtags": list(dict.fromkeys(tags)),
            "category": "Education",
            "language": job.language,
            "video_uri": render.video_uri if render else None,
            "poster_uri": render.poster_uri if render else None,
            "subtitle_uri": subtitles.ass_uri if subtitles else None,
            "background_music_uri": background_music.audio_uri if background_music else None,
            "mixed_audio_uri": background_music.mixed_audio_uri if background_music else None,
            "sound_design_uri": dict(background_music.provider_metadata or {}).get("sound_design", {}).get("audio_uri") if background_music else None,
            "checklist": checklist,
            "publish_readiness": readiness,
            "monetization_report": monetization_report,
            "human_review_checklist": monetization_report.get("human_review_checklist", {}) if monetization_report else {},
            "altered_or_synthetic": bool(monetization_report.get("ai_disclosure", {}).get("youtube_disclosure_required")) if monetization_report else False,
            "ai_disclosure_reason": monetization_report.get("ai_disclosure", {}).get("reason") if monetization_report else None,
            "minimax_publish_audit": minimax_audit,
            "quality_summary": job.quality_summary or {},
        }

    def _provider_publish_audit(self, script_artifact: dict[str, Any], fact_pack: dict[str, Any], tags: list[str]) -> dict[str, Any]:
        auditor = getattr(self.providers.creative, "audit_publish_package", None)
        if auditor is None:
            return {"passed": True, "reasons": [], "provider": "none", "skipped": True}
        payload = {
            "script": {
                "title": script_artifact.get("title"),
                "hook": script_artifact.get("hook"),
                "ending": script_artifact.get("ending"),
                "full_narration": script_artifact.get("full_narration"),
                "key_facts": script_artifact.get("key_facts"),
                "source_fact_ids": script_artifact.get("source_fact_ids"),
            },
            "fact_pack": fact_pack,
            "hashtags": tags,
        }
        try:
            audit = auditor(payload)
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "reasons": ["minimax_audit_failed"], "error": str(exc), "provider": "minimax"}
        return audit if isinstance(audit, dict) else {"passed": False, "reasons": ["minimax_audit_invalid"], "provider": "minimax"}

    def _read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        path = self.storage.job_dir(job_id) / relative_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _build_publish_hashtags(self, topic_plan: TopicPlan | None, script: Script | None) -> list[str]:
        tags = ["#shorts", "#ciencia"]
        weak = self._weak_hashtag_terms()
        text = " ".join(
            str(part or "")
            for part in [
                topic_plan.canonical_topic if topic_plan else "",
                topic_plan.angle if topic_plan else "",
                script.title if script else "",
                " ".join(script.key_facts or []) if script else "",
            ]
        )
        niche_map = {
            "flamingo": ["#flamingos", "#animais", "#biologia"],
            "flamingos": ["#flamingos", "#animais", "#biologia"],
            "torre": ["#torredepisa", "#engenharia", "#historia"],
            "pisa": ["#torredepisa", "#engenharia", "#historia"],
            "cerebro": ["#cerebro", "#neurociencia", "#percepcao"],
            "cérebro": ["#cerebro", "#neurociencia", "#percepcao"],
            "neuro": ["#neurociencia", "#cerebro", "#percepcao"],
            "polvo": ["#polvo", "#biologia", "#animais"],
            "polvos": ["#polvo", "#biologia", "#animais"],
        }
        normalized_text = self._normalize_hashtag_text(text)
        for key, mapped_tags in niche_map.items():
            if key in normalized_text:
                tags.extend(mapped_tags)
        for token in word_tokens(text):
            normalized = self._normalize_hashtag_text(token)
            if len(normalized) < 4 or normalized in weak or normalized.isdigit():
                continue
            tags.append(f"#{normalized}")
            if len(dict.fromkeys(tags)) >= 5:
                break
        return list(dict.fromkeys(tags))[:5]

    def _weak_hashtag_terms(self) -> set[str]:
        return {
            "por", "que", "qual", "como", "porque", "para", "com", "uma", "uns", "umas", "tem", "têm", "fica", "ficam", "ficou", "ser", "sao", "são", "era",
            "foram", "esta", "está", "esse", "essa", "isso", "aquele", "aquela", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e", "cor", "cores",
            "video", "short", "shorts", "curiosidade", "curiosidades",
        }

    def _normalize_hashtag_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", normalized)

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
        reasons: list[str] = []
        if not all(checklist.values()):
            reasons.append("quality_checklist_failed")
        script_dict = {**(self._script_to_dict(script) if script else {}), **(script_artifact or {})}
        source_ids = script_dict.get("source_fact_ids") or script_dict.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        if fact_pack.get("status") != "verified" and source_ids:
            reasons.append("invented_source_fact_ids")
        fact_risk = self.script_gate._fact_risk_report(script_dict) if script_dict else {"blocked": False, "claim_count": 0}  # noqa: SLF001
        factual_topic = bool(topic_plan and re.search(r"\b(?:por que|porque|como|ci[eê]ncia|f[ií]sica|biologia|engenharia|hist[oó]ria|sa[uú]de|m[eé]dico|animal|animais|flamingo|torre|c[eé]rebro|neuro)\b", f"{topic_plan.canonical_topic} {topic_plan.angle}", re.IGNORECASE))
        if factual_topic and fact_pack.get("status") != "verified" and (fact_risk.get("claim_count", 0) > 0 or len(word_tokens(script_dict.get("full_narration", ""))) >= 45):
            reasons.append("fact_pack_missing_for_factual_topic")
        weak_tags = [tag for tag in tags if tag.lower() != "#shorts" and (tag.lstrip("#") in self._weak_hashtag_terms() or len(tag.lstrip("#")) < 4)]
        if weak_tags or len(tags) < 3:
            reasons.append("weak_hashtags")
        ending = str(script_dict.get("ending") or "").strip()
        narration = str(script_dict.get("full_narration") or "").strip()
        last_sentence = re.split(r"(?<=[.!?])\s+", narration)[-1] if narration else ""
        if len(word_tokens(ending or last_sentence)) < 6 or re.search(r"\b(?:que|de|da|do|para|com|e)$", (ending or last_sentence).lower().strip(" .!?")):
            reasons.append("weak_ending")
        if minimax_audit and minimax_audit.get("passed") is False:
            reasons.extend(str(reason) for reason in minimax_audit.get("reasons") or ["minimax_audit_failed"])
        if fact_pack.get("status") != "verified":
            reasons.append("manual_review_required")
        return {
            "passed": not reasons,
            "reasons": list(dict.fromkeys(reasons)),
            "fact_pack_status": fact_pack.get("status") or "missing",
            "hashtag_count": len(tags),
            "weak_hashtags": weak_tags,
            "fact_risk": fact_risk,
            "minimax_audit": minimax_audit or {"skipped": True},
        }

    def _script_to_dict(self, script: Script) -> dict[str, Any]:
        return {
            "title": script.title,
            "hook": script.hook,
            "body_beats": script.body_beats,
            "ending": script.ending,
            "cta": script.cta,
            "full_narration": script.full_narration,
            "estimated_duration_sec": script.estimated_duration_sec,
            "key_facts": script.key_facts,
            "language": script.language,
            "qa_metrics": script.qa_metrics or {},
            "source_fact_ids": (script.qa_metrics or {}).get("source_fact_ids", []),
        }

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
        event_path = self.storage.job_dir(job_id) / "events.jsonl"
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
            with session_scope() as session:
                claimed_job_id = self._claim_next_job(session)
            if claimed_job_id:
                self.process_job(claimed_job_id)
            else:
                time.sleep(self.settings.worker_poll_seconds)

    def _claim_next_job(self, session: Session) -> str | None:
        now = utcnow()
        lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
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


orchestrator = JobOrchestrator()
