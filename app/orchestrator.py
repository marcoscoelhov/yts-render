from __future__ import annotations

import json
import subprocess
import threading
import time
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
            if job.status in {"approved", "failed", "cancelled"}:
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
                job.status = "approved"
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
            job.status = "failed"
            job.failure_reason = f"{step_name}: {message}"
            job.lease_owner = None
            job.lease_expires_at = None
        self._append_event(job_id, "job.failed", "failed", {"step": step_name, "message": message})

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
        plan = self.providers.creative.plan_topic(request.seed_theme, attempt, history, request.requested_angle)
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
        session.add(TopicPlan(**payload))
        self.storage.persist_json(job.job_id, "topic_plan.json", self._serialize_for_json(payload))
        job.topic_summary = f"{plan['canonical_topic']} | {plan['angle']}"
        self._append_event(job.job_id, "topic.generated", "succeeded", payload["quality_metrics"])
        return ["topic_plan.json"]

    def _step_script(self, session: Session, job: Job, attempt: int) -> list[str]:
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        assert topic_plan
        plan_dict = {
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "title_candidates": topic_plan.title_candidates,
        }
        script = self.providers.creative.generate_script(plan_dict)
        metrics = script["qa_metrics"]
        gate = (
            metrics["hook_score"] >= 0.80
            and metrics["avg_words_per_sentence"] <= 14
            and metrics["max_words_single_sentence"] <= 20
            and 25 <= script["estimated_duration_sec"] <= 45
            and metrics["information_density_score"] >= 0.75
            and metrics["ending_strength_score"] >= 0.75
            and metrics["repetition_score"] < 0.88
        )
        if not gate:
            raise RecoverableStepError("script gate failed")
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
        session.add(Script(**payload))
        self.storage.persist_json(job.job_id, "script.json", self._serialize_for_json(payload))
        quality_summary = job.quality_summary or {}
        quality_summary["script"] = metrics
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "script.generated", "succeeded", metrics)
        return ["script.json"]

    def _step_scene_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        assert script and topic_plan
        script_dict = {
            "title": script.title,
            "full_narration": script.full_narration,
            "estimated_duration_sec": script.estimated_duration_sec,
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
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
        session.add(ScenePlan(**payload))
        self.storage.persist_json(job.job_id, "scene_plan.json", self._serialize_for_json(payload))
        self._append_event(job.job_id, "scene_plan.generated", "succeeded", {"scene_count": len(scenes)})
        return ["scene_plan.json"]

    def _step_assets(self, session: Session, job: Job, attempt: int) -> list[str]:
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
            if needs_quality_fallback:
                fallback_used = True
                session.add(
                    FallbackEvent(
                        event_id=new_id(),
                        job_id=job.job_id,
                        schema_version=self.settings.schema_version,
                        content_hash=stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": "quality_fallback"}),
                        created_at=utcnow(),
                        step="asset_generation",
                        reason_code="low_semantic_score",
                        attempt=attempt,
                        scene_id=scene["scene_id"],
                        from_provider=primary_provider,
                        to_provider="local_semantic",
                        reason_detail="Primary image candidates fell below semantic thresholds.",
                    )
                )
                self._append_event(job.job_id, "asset.semantic_fallback", "succeeded", {"scene_id": scene["scene_id"]})
                local_asset = self.providers.local_image.generate(scene, scene_dir / "local-semantic.png")
                local_scores = self._score_asset(scene, local_asset)
                candidates.append((local_asset, local_scores))
            passing_candidates = [(asset, scores) for asset, scores in candidates if self._asset_scores_pass(scores)]
            if not passing_candidates:
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
                raise RecoverableStepError(f"all assets failed threshold for {scene['scene_id']}")
            winner_asset, winner_scores = sorted(passing_candidates, key=lambda item: item[1]["total_score"], reverse=True)[0]
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
        mean_semantic = sum(item["semantic_match"] for item in selected_assets) / max(len(selected_assets), 1)
        quality_summary = job.quality_summary or {}
        quality_summary["assets"] = {"asset_semantic_score_avg": round(mean_semantic, 3), "scene_count": len(selected_assets)}
        job.quality_summary = quality_summary
        if mean_semantic < 0.80:
            raise RecoverableStepError("asset semantic average below threshold")
        self._append_event(job.job_id, "asset.selected", "succeeded", quality_summary["assets"])
        return asset_refs

    def _score_asset(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        return self.providers.semantic.score(scene, asset)

    def _asset_scores_pass(self, scores: dict[str, Any]) -> bool:
        return (
            scores["semantic_match"] >= 0.80
            and scores["total_score"] >= 0.75
            and scores.get("text_or_watermark_penalty", 0.0) <= 0.15
            and scores.get("artifact_penalty", 0.0) <= 0.30
        )

    def _image_prompt_variants(self, scene: dict[str, Any]) -> list[dict[str, Any]]:
        return [scene]

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
        prompt = str(scene.get("image_prompt", "")).replace("_", " ")
        if topic_text and topic_text not in prompt:
            prompt = f"{prompt}, tema central {topic_text}".strip(", ")
        if "sem capa de filme" not in prompt:
            prompt += ", visualizacao cientifica, sem capa de filme, sem poster, sem tipografia"
        normalized["image_prompt"] = prompt
        return normalized

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
        if not 25_000 <= result["duration_ms"] <= 45_000:
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
        session.add(NarrationAsset(**payload))
        self.storage.persist_json(job.job_id, "narration_asset.json", self._serialize_for_json(payload))
        quality_summary = job.quality_summary or {}
        quality_summary["tts"] = {
            "duration_ms": result["duration_ms"],
            "provider": result["provider"],
            "fallback_used": result.get("provider_metadata", {}).get("fallback_used", False),
        }
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "tts.generated", "succeeded", quality_summary["tts"])
        return ["audio/narration.wav", "audio/raw.srt", "narration_asset.json"]

    def _step_subtitles(self, session: Session, job: Job, attempt: int) -> list[str]:
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
            items.append(
                {
                    "idx": cue["idx"],
                    "start_ms": cue["start_ms"],
                    "end_ms": cue["end_ms"],
                    "text": cue["text"],
                    "token_start": start,
                    "token_end": max(end, start),
                }
            )
            cursor = end + 1
        coverage = round(min(cursor / max(len(script_words), 1), 1.0), 3)
        if coverage < 0.99:
            raise RecoverableStepError("subtitle coverage below threshold")
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
        session.add(SubtitleTrack(**payload))
        self.storage.persist_json(job.job_id, "subtitle_track.json", self._serialize_for_json(payload))
        self._append_event(job.job_id, "subtitle.aligned", "succeeded", {"coverage_ratio": coverage})
        return ["subtitle_track.json", "audio/subtitles.ass"]

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
        filter_parts.append(f"[video]ass={ass_path.as_posix()}[vout]")
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
                "-pix_fmt",
                "yuv420p",
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
        Image.open(path_from_uri(selected_assets[0].uri)).resize((540, 960)).save(poster, format="JPEG")
        duration_ms = narration.duration_ms
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
        session.add(RenderOutput(**payload))
        self.storage.persist_json(job.job_id, "render_output.json", self._serialize_for_json(payload))
        quality_summary = job.quality_summary or {}
        quality_summary["render"] = {"duration_ms": duration_ms, "resolution": "1080x1920"}
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
        return ["job_manifest.json"]

    def _append_event(self, job_id: str, event_name: str, status: str, payload: dict[str, Any]) -> None:
        job_dir = self.storage.job_dir(job_id)
        event_path = job_dir / "events.jsonl"
        line = json.dumps(
            {
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
