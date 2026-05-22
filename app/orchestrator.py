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
from app.editorial.retention import attach_retention_metadata, enrich_plan_for_script_generation
from app.editorial.repetition import build_channel_repetition_report
from app.models import (
    BackgroundMusicAsset,
    ChannelPublication,
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
    ensure_dir,
    file_uri,
    iso_now,
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
from app.tiktok_api import TikTokIntegrationError, TikTokPublisher


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
PROGRESS_STEP_LABELS = {
    "input_gate": "Entrada",
    "topic_plan": "Pauta",
    "script": "Roteiro",
    "scene_plan": "Cenas",
    "asset_generation": "Imagens",
    "tts": "Narração",
    "subtitle_alignment": "Legendas",
    "background_music": "Trilha",
    "render": "Render",
    "monetization_readiness_gate": "Monetização",
    "publish_to_review_hub": "Revisão",
}
PROGRESS_COMPLETE_STATUSES = {
    "monetization_review",
    "blocked_for_monetization",
    "ready_for_upload",
    "approved_for_publish",
    "published",
    "approved",
    "rejected",
}
PROGRESS_FAILED_STATUSES = {
    "failed",
    "script_quality_failed",
    "scene_plan_quality_failed",
    "asset_quality_failed",
    "subtitle_quality_failed",
    "render_quality_failed",
    "cancelled",
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
        self.tiktok = TikTokPublisher(self.settings)
        from app.publication_ops import PublicationOperations

        self.publication_ops = PublicationOperations(self)
        self._last_retention_sweep_at = 0.0
        from app.pipelines.asset_pipeline import AssetPipeline
        from app.pipelines.monetization_pipeline import MonetizationPipeline
        from app.pipelines.render_pipeline import RenderPipeline
        from app.pipelines.scene_pipeline import ScenePipeline
        from app.pipelines.script_pipeline import ScriptPipeline
        from app.pipelines.topic_pipeline import TopicPipeline

        self.topic_pipeline = TopicPipeline(self)
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
            self.topic_pipeline.upsert_topic_registry(session, job_id, approved=False)
            self.publication_ops._refresh_retention_state(session, job)
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
        events = self._read_events(job_id)
        performance_timeline = self._read_job_json(job_id, "performance_timeline.json")
        progress = self.build_job_progress(job, performance_timeline, events)
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
            "events": events,
            "progress": progress,
            "monetization_report": self._read_job_json(job_id, "monetization_report.json") or cleanup_snapshots.get("monetization_report", {}),
            "publish_package": self._read_job_json(job_id, "publish_package.json") or cleanup_snapshots.get("publish_package", {}),
            "publish_result": self._read_job_json(job_id, "publish_result.json") or cleanup_snapshots.get("publish_result", {}),
            "publication_attempts": self._read_job_json(job_id, "youtube_publish_attempts.json").get("attempts", []) or cleanup_snapshots.get("publication_attempts", []),
            "retention_cleanup": retention_cleanup,
            "artifacts_cleaned": artifacts_cleaned,
        }

    def build_job_progress(
        self,
        job: Job,
        performance_timeline: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        step_names = [step.name for step in self._steps()]
        total_steps = len(step_names)
        timeline_steps = list((performance_timeline or {}).get("steps") or [])
        has_timeline_steps = bool(timeline_steps)
        latest_by_step: dict[str, dict[str, Any]] = {}
        for row in timeline_steps:
            if not isinstance(row, dict):
                continue
            step_name = str(row.get("step_name") or "")
            if step_name:
                latest_by_step[step_name] = row

        job_status = str(job.status or "")
        current_step = str(job.current_step or "")
        complete = job_status in PROGRESS_COMPLETE_STATUSES
        failed = job_status in PROGRESS_FAILED_STATUSES or job_status.endswith("_failed")
        running = job_status == "running"
        queued = job_status == "queued"
        current_index = step_names.index(current_step) if current_step in step_names else -1
        completed_count = 0
        progress_steps: list[dict[str, Any]] = []

        for index, step_name in enumerate(step_names, start=1):
            execution = latest_by_step.get(step_name, {})
            execution_status = str(execution.get("status") or "")
            step_status = "pending"
            if complete:
                step_status = "completed"
            elif execution_status == "succeeded":
                step_status = "completed"
            elif execution_status == "failed" or (failed and current_step == step_name):
                step_status = "failed"
            elif not has_timeline_steps and current_index >= 0 and index - 1 < current_index:
                step_status = "completed"
            elif running and current_step == step_name:
                step_status = "running"

            if step_status == "completed":
                completed_count += 1
            progress_steps.append(
                {
                    "name": step_name,
                    "label": PROGRESS_STEP_LABELS.get(step_name, step_name.replace("_", " ").title()),
                    "index": index,
                    "status": step_status,
                    "attempt": execution.get("attempt"),
                    "duration_ms": execution.get("duration_ms"),
                    "started_at": execution.get("started_at"),
                    "finished_at": execution.get("finished_at"),
                }
            )

        current_name = current_step if current_step in step_names else ""
        if not current_name:
            next_pending = next((step for step in progress_steps if step["status"] in {"running", "failed", "pending"}), None)
            current_name = str(next_pending["name"]) if next_pending else step_names[-1]
        current_label = PROGRESS_STEP_LABELS.get(current_name, current_name.replace("_", " ").title())

        if complete:
            percent = 100
            state = "completed"
            summary = "Pipeline concluído; o job está pronto para revisão, aprovação ou publicação."
        elif queued:
            percent = 0
            state = "queued"
            summary = "Aguardando o worker iniciar o pipeline."
            current_label = "Fila"
        elif failed:
            percent = round((completed_count / total_steps) * 100) if total_steps else 0
            state = "failed"
            summary = f"Falhou em {current_label}; abra os dados técnicos para ver o erro."
        elif running:
            partial = 0.35 if current_name else 0
            percent = min(99, round(((completed_count + partial) / total_steps) * 100)) if total_steps else 0
            state = "running"
            summary = f"Rodando {current_label}; a página atualiza automaticamente."
        else:
            percent = min(100, round((completed_count / total_steps) * 100)) if total_steps else 0
            state = "waiting"
            summary = "Sem execução ativa no momento."

        last_event = None
        if events:
            last_event = events[-1]
        return {
            "state": state,
            "percent": percent,
            "completed_steps": completed_count,
            "total_steps": total_steps,
            "current_step": current_name,
            "current_label": current_label,
            "summary": summary,
            "steps": progress_steps,
            "last_event": last_event,
        }

    def review_job(self, payload: dict[str, Any], job_id: str) -> str | None:
        return self.publication_ops.review_job(payload, job_id)

    def publish_job(
        self,
        job_id: str,
        youtube_video_id: str | None = None,
        youtube_url: str | None = None,
        *,
        trigger: str = "manual",
    ) -> None:
        return self.publication_ops.publish_job(
            job_id,
            youtube_video_id=youtube_video_id,
            youtube_url=youtube_url,
            trigger=trigger,
        )

    def update_publish_metadata(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.publication_ops.update_publish_metadata(job_id, payload)

    def schedule_publication(self, job_id: str, payload: dict[str, Any]) -> None:
        self.publication_ops.schedule_publication(job_id, payload)

    def clear_publication_schedule(self, job_id: str) -> None:
        self.publication_ops.clear_publication_schedule(job_id)

    def reopen_publication_for_republish(self, job_id: str) -> None:
        self.publication_ops.reopen_publication_for_republish(job_id)

    def record_performance_metrics(self, job_id: str, payload: dict[str, Any]) -> None:
        self.publication_ops.record_performance_metrics(job_id, payload)

    def _steps(self) -> list[StepDefinition]:
        return [
            StepDefinition("input_gate", 0, self._step_input_gate),
            StepDefinition("topic_plan", 2, self.topic_pipeline.step_topic_plan),
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

    def _remove_stale_quality_report(self, job_id: str, relative_path: str) -> None:
        try:
            (self.storage.job_dir(job_id) / relative_path).unlink(missing_ok=True)
        except OSError:
            pass

    def _read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        return self.monetization_pipeline.read_job_json(job_id, relative_path)

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

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.settings.artifact_retention_enabled:
                should_sweep = time.monotonic() - self._last_retention_sweep_at >= self.settings.artifact_retention_sweep_seconds
                if should_sweep:
                    self.publication_ops._run_retention_sweep()
            if self.publication_ops._youtube_api_mode_enabled():
                self.publication_ops._sync_native_scheduled_publications()
            if self.publication_ops._tiktok_auto_publish_enabled():
                self.publication_ops._sync_tiktok_publication_statuses()
                self.publication_ops._sync_tiktok_crosspost_queue()
            with session_scope() as session:
                claimed_job_id = self._claim_next_job(session)
            if claimed_job_id:
                self.process_job(claimed_job_id)
                continue
            claimed_publication_job_id = self.publication_ops._claim_due_publication_schedule()
            if claimed_publication_job_id:
                self.publish_job(claimed_publication_job_id, trigger="schedule_worker")
                continue
            claimed_tiktok_publication_id = self.publication_ops._claim_due_tiktok_publication()
            if claimed_tiktok_publication_id:
                self.publication_ops._publish_tiktok_channel_publication(claimed_tiktok_publication_id)
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

orchestrator = JobOrchestrator()
