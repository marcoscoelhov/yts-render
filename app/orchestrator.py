from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import httpx
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import imageio_ffmpeg
from PIL import Image
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, session_scope
from app.models import (
    ErrorLog,
    FallbackEvent,
    Job,
    NarrationAsset,
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
from app.quality.subtitle_gate import SubtitleGate
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
        self.worker_thread = threading.Thread(target=self._worker_loop, name="yts-worker", daemon=True)
        self.worker_thread.start()

    def stop_worker(self) -> None:
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2)

    def create_job(self, payload: dict[str, Any], retry_of_job_id: str | None = None) -> str:
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
                return "failed"
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job
            job.status = "waiting_review"
            job.current_step = "publish_to_review_hub"
            job.lease_owner = None
            job.lease_expires_at = None
            self._upsert_topic_registry(session, job_id, approved=False)
        self._append_event(job_id, "render.completed", "succeeded", {"status": "waiting_review"})
        return "waiting_review"

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
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job_id))
        assets = session.scalars(select(SceneAsset).where(SceneAsset.job_id == job_id).order_by(SceneAsset.scene_id, SceneAsset.provider)).all()
        fallbacks = session.scalars(select(FallbackEvent).where(FallbackEvent.job_id == job_id).order_by(FallbackEvent.created_at)).all()
        errors = session.scalars(select(ErrorLog).where(ErrorLog.job_id == job_id).order_by(ErrorLog.created_at)).all()
        reviews = session.scalars(select(ReviewRecord).where(ReviewRecord.job_id == job_id).order_by(ReviewRecord.created_at)).all()
        return {
            "job": job,
            "topic_request": topic_request,
            "topic_plan": topic_plan,
            "script": script,
            "scene_plan": scene_plan,
            "assets": assets,
            "narration": narration,
            "subtitles": subtitles,
            "render": render,
            "fallbacks": fallbacks,
            "errors": errors,
            "reviews": reviews,
            "events": self._read_events(job_id),
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
                retry_step=payload.get("retry_step"),
            )
            session.add(review)
            if payload["action"] == "approve":
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
        self._append_event(job_id, "review.retry_requested", "succeeded", {"new_job_id": new_job_id})
        return new_job_id

    def publish_job(self, job_id: str, youtube_video_id: str | None = None, youtube_url: str | None = None) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            if job.status not in {"approved_for_publish", "published"}:
                raise FatalStepError("job must be approved_for_publish before publishing")
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

    def _steps(self) -> list[StepDefinition]:
        return [
            StepDefinition("input_gate", 0, self._step_input_gate),
            StepDefinition("topic_plan", 2, self._step_topic_plan),
            StepDefinition("script", 2, self._step_script),
            StepDefinition("scene_plan", 1, self._step_scene_plan),
            StepDefinition("asset_generation", 2, self._step_assets),
            StepDefinition("tts", 2, self._step_tts),
            StepDefinition("subtitle_alignment", 1, self._step_subtitles),
            StepDefinition("render", 1, self._step_render),
            StepDefinition("publish_to_review_hub", 0, self._step_publish),
        ]

    def _run_step(self, job_id: str, step: StepDefinition) -> bool:
        for attempt in range(1, step.retries + 2):
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
        }

    def _step_input_gate(self, session: Session, job: Job, attempt: int) -> list[str]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert request
        blocked = any(term in request.seed_theme.lower() for term in ["odio", "terrorismo", "explosivo"])
        if blocked:
            raise FatalStepError("input blocked by moderation")
        quality = {
            "schema_valid": True,
            "niche_supported": request.niche_id == "curiosidades",
            "language": request.language,
            "moderation_ok": True,
        }
        self._append_event(job.job_id, "input_gate.passed", "succeeded", quality)
        return ["request.json"]

    def _step_topic_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert request
        history_rows = session.scalars(
            select(TopicRegistry).where(
                TopicRegistry.approved.is_(True),
                TopicRegistry.created_at >= utcnow() - timedelta(days=90),
            )
        ).all()
        history = [
            {"canonical_topic": row.canonical_topic, "hook": row.hook, "title": row.title}
            for row in history_rows
        ]
        plan = self.providers.creative.plan_topic(
            request.seed_theme,
            attempt,
            history,
            request.requested_angle,
            tone=request.tone,
            notes=request.notes,
        )
        candidate_topic_surface = f"{plan['canonical_topic']} {plan['angle']}"
        topic_similarity = max(
            [cosineish_similarity(candidate_topic_surface, f"{row['canonical_topic']} {row['title']}") for row in history],
            default=0.0,
        )
        hook_similarity = max(
            [jaccard_bigrams(plan["hook_promise"], row["hook"]) for row in history],
            default=0.0,
        )
        if topic_similarity >= 0.82 or hook_similarity >= 0.88:
            raise RecoverableStepError("topic too similar to approved history")
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
                "topic_uniqueness_pass": True,
                "topic_similarity_max": round(topic_similarity, 3),
                "hook_similarity_max": round(hook_similarity, 3),
            },
        }
        session.execute(delete(TopicPlan).where(TopicPlan.job_id == job.job_id))
        session.add(TopicPlan(**model_payload(TopicPlan, payload)))
        self.storage.persist_json(job.job_id, "topic_plan.json", self._serialize_for_json(payload))
        job.topic_summary = f"{plan['canonical_topic']} | {plan['angle']}"
        self._append_event(job.job_id, "topic.generated", "succeeded", payload["quality_metrics"])
        return ["topic_plan.json"]

    def _step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        assert topic_plan and request
        plan_dict = {
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "title_candidates": topic_plan.title_candidates,
            "tone": request.tone or "intrigante_direto",
            "requested_angle": request.requested_angle,
            "hub_notes": request.notes,
            "original_input": request.seed_theme,
        }
        fact_pack = self._build_fact_pack(topic_plan, request)
        plan_dict["fact_pack"] = fact_pack
        self.storage.persist_json(job.job_id, "fact_pack.json", self._serialize_for_json(fact_pack))
        script = self.providers.creative.generate_script(plan_dict)
        script, metrics = self._validate_or_repair_script(script, plan_dict, job.target_duration_sec, request.cta_style or "none")
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
        quality_summary = dict(job.quality_summary or {})
        quality_summary["script"] = metrics
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "script.generated", "succeeded", metrics)
        return ["fact_pack.json", "script.json"]

    def _build_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest) -> dict[str, Any]:
        if self.settings.use_mock_providers:
            return {
                "status": "limited",
                "query_used": request.seed_theme,
                "facts": [],
                "sources": [],
                "editorial_rule": "Mock-provider test mode: no external fact retrieval.",
            }
        queries = [request.seed_theme, topic_plan.canonical_topic, *(topic_plan.title_candidates or [])]
        seen: set[str] = set()
        cleaned_queries = []
        for query in queries:
            normalized = " ".join(str(query or "").split())
            if normalized and normalized.lower() not in seen:
                cleaned_queries.append(normalized)
                seen.add(normalized.lower())
        for query in cleaned_queries[:4]:
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
        if not isinstance(fact_pack, dict) or fact_pack.get("status") != "verified":
            return []
        facts = fact_pack.get("facts") or []
        valid_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
        if not valid_ids:
            return []
        source_ids = script.get("source_fact_ids") or script.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
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
        enriched = dict(script)
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

    def _validate_or_repair_script(
        self,
        script: dict[str, Any],
        plan_dict: dict[str, Any],
        target_duration_sec: int,
        cta_style: str = "none",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        script = self._apply_cta_policy(dict(script), cta_style)
        script["qa_metrics"] = normalize_script_metrics(dict(script.get("qa_metrics") or {}))
        gate_result = self.script_gate.validate(script, target_duration_sec)
        consistency_reasons = self._fact_pack_consistency_reasons(script, plan_dict.get("fact_pack"))
        if gate_result.passed and not consistency_reasons:
            script["qa_metrics"] = {**gate_result.metrics, "fact_pack_consistency_pass": True}
            return script, script["qa_metrics"]

        repair_attempts = max(0, self.settings.llm_script_repair_attempts)
        last_reasons = [*gate_result.reasons, *consistency_reasons]
        for _ in range(repair_attempts):
            repaired = self.providers.creative.repair_script(script, last_reasons, plan_dict)
            repaired = self._apply_cta_policy(repaired, cta_style)
            repaired["qa_metrics"] = normalize_script_metrics(dict(repaired.get("qa_metrics") or {}))
            repaired_gate = self.script_gate.validate(repaired, target_duration_sec)
            repaired_consistency_reasons = self._fact_pack_consistency_reasons(repaired, plan_dict.get("fact_pack"))
            if repaired_gate.passed and not repaired_consistency_reasons:
                repaired["qa_metrics"] = {
                    **repaired_gate.metrics,
                    "fact_pack_consistency_pass": True,
                    "script_repair_used": True,
                    "script_repair_initial_reasons": [*gate_result.reasons, *consistency_reasons],
                }
                return repaired, repaired["qa_metrics"]
            script = repaired
            last_reasons = [*repaired_gate.reasons, *repaired_consistency_reasons]

        fallback_repaired = self.providers.creative.repair_script_with_fallback(script, last_reasons, plan_dict)
        if fallback_repaired is not None:
            fallback_repaired = self._apply_cta_policy(fallback_repaired, cta_style)
            fallback_repaired["qa_metrics"] = normalize_script_metrics(dict(fallback_repaired.get("qa_metrics") or {}))
            fallback_gate = self.script_gate.validate(fallback_repaired, target_duration_sec)
            fallback_consistency_reasons = self._fact_pack_consistency_reasons(fallback_repaired, plan_dict.get("fact_pack"))
            if fallback_gate.passed and not fallback_consistency_reasons:
                fallback_repaired["qa_metrics"] = {
                    **fallback_gate.metrics,
                    "fact_pack_consistency_pass": True,
                    "script_repair_used": True,
                    "script_repair_fallback_used": True,
                    "script_repair_initial_reasons": [*gate_result.reasons, *consistency_reasons],
                }
                return fallback_repaired, fallback_repaired["qa_metrics"]
            last_reasons = [*fallback_gate.reasons, *fallback_consistency_reasons]

        raise RecoverableStepError(f"script quality gate failed: {', '.join(last_reasons)}")

    def _step_scene_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "scene_plan_rejected.json")
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
        tokens = word_tokens(script.full_narration)
        if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
            fallback_planner = getattr(self.providers.creative, "fallback", None)
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
            if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
                raise RecoverableStepError("scene coverage invalid")
        scenes = [self._normalize_scene_semantics(scene, topic_plan.canonical_topic) for scene in scenes]
        scene_gate = self.scene_gate.validate(scenes, self.settings.scene_target_count)
        if not scene_gate.passed:
            fallback_planner = getattr(self.providers.creative, "fallback", None)
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
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
            for variant_index, variant_scene in enumerate(self._image_prompt_variants(scene), start=1):
                ai_path = scene_dir / ("ai.png" if variant_index == 1 else f"ai-{variant_index}.png")
                try:
                    ai_asset = self.providers.image.generate(variant_scene, ai_path)
                    ai_asset = self._normalize_asset_uri_extension(ai_asset)
                    ai_scores = self._score_asset(variant_scene, ai_asset)
                    candidates.append((ai_asset, ai_scores))
                    primary_provider = ai_asset["provider"]
                    if self._asset_scores_pass(ai_scores):
                        break
                except Exception as exc:  # noqa: BLE001
                    fallback_used = True
                    session.add(
                        FallbackEvent(
                            event_id=new_id(),
                            job_id=job.job_id,
                            schema_version=self.settings.schema_version,
                            content_hash=stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": f"provider_error_{variant_index}"}),
                            created_at=utcnow(),
                            step="asset_generation",
                            reason_code="ai_provider_error",
                            attempt=attempt,
                            scene_id=scene["scene_id"],
                            from_provider="minimax",
                            to_provider="local_semantic",
                            reason_detail=str(exc),
                        )
                    )
                    self._append_event(job.job_id, "asset.semantic_fallback", "succeeded", {"scene_id": scene["scene_id"], "variant": variant_index})
            needs_quality_fallback = not candidates or all(not self._asset_scores_pass(scores) for _, scores in candidates)
            if not candidates and not self.settings.use_mock_providers:
                fallback_used = True
            if needs_quality_fallback:
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

    def _image_prompt_variants(self, scene: dict[str, Any]) -> list[dict[str, Any]]:
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
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            variants.append({**scene, "image_prompt": normalized})
        return variants

    def _normalize_scene_semantics(self, scene: dict[str, Any], canonical_topic: str) -> dict[str, Any]:
        topic_text = canonical_topic.replace("_", " ").strip()
        normalized = dict(scene)
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

    def _split_subtitle_cue(self, cue: dict[str, Any], token_start: int, token_end: int) -> list[dict[str, Any]]:
        chunks = split_caption_chunks(str(cue["text"]), max_chars=42, max_lines=2) or [str(cue["text"])]
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

    def _avoid_weak_subtitle_endings(self, chunks: list[str]) -> list[str]:
        bad_endings = {"de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por", "para", "que", "e"}
        repaired = [chunk for chunk in chunks if chunk.strip()]
        for index in range(len(repaired) - 1):
            words = repaired[index].split()
            next_words = repaired[index + 1].split()
            if not words or len(next_words) <= 1:
                continue
            ending_tokens = word_tokens(words[-1])
            ending = ending_tokens[0] if ending_tokens else ""
            if ending in bad_endings:
                candidate = " ".join([*words, next_words[0]])
                if len(wrap_caption(candidate, max_chars=42, max_lines=2).splitlines()) <= 2:
                    repaired[index] = candidate
                    repaired[index + 1] = " ".join(next_words[1:])
        return [chunk for chunk in repaired if chunk.strip()]

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
        selected_assets = session.scalars(
            select(SceneAsset).where(SceneAsset.job_id == job.job_id, SceneAsset.selected.is_(True)).order_by(SceneAsset.scene_id)
        ).all()
        assert scene_plan and narration and subtitles and selected_assets
        final_video = self.storage.job_dir(job.job_id) / "render" / "final.mp4"
        poster = self.storage.job_dir(job.job_id) / "render" / "poster.jpg"
        ffmpeg_log = self.storage.job_dir(job.job_id) / "render" / "ffmpeg.log"
        ensure_dir(final_video.parent)
        total_duration = narration.duration_ms / 1000
        audio_path = path_from_uri(narration.audio_uri)
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
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        ffmpeg_log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RecoverableStepError("ffmpeg render failed")
        render_gate = self.render_gate.validate(final_video, narration.duration_ms)
        if not render_gate.passed:
            self.storage.persist_json(job.job_id, "render_quality_report.json", {"reasons": render_gate.reasons, "metrics": render_gate.metrics})
            raise RecoverableStepError(f"render quality gate failed: {', '.join(render_gate.reasons[:6])}")
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
        quality_summary = dict(job.quality_summary or {})
        quality_summary["render"] = {
            **render_gate.metrics,
            "render_gate_pass": True,
            "duration_ms": duration_ms,
            "resolution": "1080x1920",
            "audio_loudness_target_lufs": -16.0,
            "audio_true_peak_limit_db": -1.5,
        }
        job.quality_summary = quality_summary
        return ["render/final.mp4", "render/poster.jpg", "render/ffmpeg.log", "render_output.json"]

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

    def _step_publish(self, session: Session, job: Job, attempt: int) -> list[str]:
        publish_package = self._build_publish_package(session, job)
        self.storage.persist_json(job.job_id, "publish_package.json", self._serialize_for_json(publish_package))
        artifact_index = {
            "request": "request.json",
            "topic_plan": "topic_plan.json",
            "script": "script.json",
            "scene_plan": "scene_plan.json",
            "audio": "audio/narration.wav",
            "raw_subtitles": "audio/raw.srt",
            "subtitles": "audio/subtitles.ass",
            "render": "render/final.mp4",
            "events": "events.jsonl",
            "ffmpeg_log": "render/ffmpeg.log",
            "publish_package": "publish_package.json",
        }
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
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        title = script.title if script else (request.seed_theme if request else job.topic_summary or job.job_id)
        tags = ["#shorts", "#curiosidades", "#ciencia"]
        tag_stopwords = {"por", "que", "qual", "como", "porque", "para", "com", "uma", "um", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e"}
        if topic_plan:
            added = 0
            for token in word_tokens(topic_plan.canonical_topic):
                normalized = token.lower()
                if len(normalized) < 3 or normalized in tag_stopwords:
                    continue
                tags.append(f"#{normalized}")
                added += 1
                if added >= 3:
                    break
        description = "\n".join(
            [
                script.full_narration if script else title,
                "",
                " ".join(dict.fromkeys(tags)),
            ]
        )
        checklist = {
            "script_gate_pass": bool((job.quality_summary or {}).get("script", {}).get("script_quality_gate_pass")),
            "scene_plan_gate_pass": bool((job.quality_summary or {}).get("scene_plan", {}).get("scene_plan_gate_pass")),
            "asset_gate_pass": bool((job.quality_summary or {}).get("assets", {}).get("semantic_threshold_pass")),
            "subtitle_gate_pass": bool((job.quality_summary or {}).get("subtitles", {}).get("subtitle_gate_pass")),
            "render_gate_pass": bool((job.quality_summary or {}).get("render", {}).get("render_gate_pass")),
        }
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "created_at": iso_now(),
            "status": "ready_for_publish" if all(checklist.values()) else "needs_review",
            "title": title[:100],
            "description": description[:4900],
            "hashtags": list(dict.fromkeys(tags)),
            "category": "Education",
            "language": job.language,
            "video_uri": render.video_uri if render else None,
            "poster_uri": render.poster_uri if render else None,
            "subtitle_uri": subtitles.ass_uri if subtitles else None,
            "checklist": checklist,
            "quality_summary": job.quality_summary or {},
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
            claimed_job_id = None
            with session_scope() as session:
                now = utcnow()
                job = session.scalar(
                    select(Job)
                    .where(
                        or_(
                            Job.status == "queued",
                            (Job.status == "running") & (Job.lease_expires_at.is_(None) | (Job.lease_expires_at < now)),
                        )
                    )
                    .order_by(Job.created_at)
                    .limit(1)
                )
                if job:
                    claimed_job_id = job.job_id
                    job.status = "running"
                    job.lease_owner = self.worker_id
                    job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            if claimed_job_id:
                self.process_job(claimed_job_id)
            else:
                time.sleep(self.settings.worker_poll_seconds)


orchestrator = JobOrchestrator()
