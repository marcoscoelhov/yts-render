from __future__ import annotations

import threading
import time
import concurrent.futures
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import BackgroundMusicAsset, FallbackEvent, Job, NarrationAsset, SceneAsset, ScenePlan, Script, SubtitleTrack, TopicPlan
from app.pipelines.common import RecoverableStepError, model_payload
from app.pipelines.base import BasePipeline
from app.pipelines.image_assets import ImageAssetDomain
from app.pipelines.music_assets import MusicDomain
from app.pipelines.subtitle_assets import SubtitleDomain
from app.pipelines.timeline import normalize_scene_timings
from app.pipelines.tts_assets import TTSDomain
from app.quality.background_music_gate import BackgroundMusicGate
from app.utils import file_uri, new_id, parse_srt, path_from_uri, stable_hash, utcnow, word_tokens


class AssetPipeline(BasePipeline):
    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.image_assets = ImageAssetDomain(self)
        self.tts = TTSDomain(self)
        self.subtitles = SubtitleDomain(self)
        self.music = MusicDomain(self)
        self.background_music_gate = BackgroundMusicGate()
        self._music_prefetch_lock = threading.Lock()
        self._music_prefetch_futures: dict[str, concurrent.futures.Future] = {}

    def start_background_music_prefetch(self, job_id: str) -> None:
        if not self.settings.background_music_enabled:
            return
        with self._music_prefetch_lock:
            if job_id in self._music_prefetch_futures:
                return
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._music_prefetch_futures[job_id] = future

        thread = threading.Thread(target=self._run_background_music_prefetch, args=(job_id, future), daemon=True)
        thread.start()

    def _run_background_music_prefetch(self, job_id: str, future: concurrent.futures.Future) -> None:
        try:
            with session_scope() as session:
                script = session.scalar(select(Script).where(Script.job_id == job_id))
                topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job_id))
                if not script or not topic_plan:
                    raise RuntimeError("missing script or topic plan for music prefetch")
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
                estimated_ms = max(8_000, int(float(script.estimated_duration_sec or 35) * 1000))
            output_path = self.storage.job_dir(job_id) / "audio" / "background_source.prefetch.wav"
            started_at = time.perf_counter()
            result = self.providers.music.select_track(topic_dict, script_dict, output_path, estimated_ms)
            result.setdefault("provider_metadata", {})["prefetch_used"] = True
            result["provider_metadata"]["prefetch_elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            future.set_result({"result": result, "path": output_path, "target_duration_ms": estimated_ms})
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)

    def _consume_background_music_prefetch(self, job_id: str, raw_music_path: Path) -> tuple[dict[str, Any] | None, Exception | None]:
        with self._music_prefetch_lock:
            future = self._music_prefetch_futures.pop(job_id, None)
        if future is None:
            return None, None
        try:
            prefetched = future.result(timeout=max(float(self.settings.minimax_music_timeout_sec), 1.0))
            result = dict(prefetched["result"])
            source_path = Path(prefetched["path"])
            if source_path.exists() and source_path.resolve() != raw_music_path.resolve():
                raw_music_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.replace(raw_music_path)
            result["audio_uri"] = raw_music_path.resolve().as_uri()
            result["duration_ms"] = prefetched.get("target_duration_ms") or result.get("duration_ms")
            result.setdefault("provider_metadata", {})["prefetch_consumed"] = True
            return result, None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    def step_assets(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "asset_quality_report.json")
        if hasattr(self.providers.image, "begin_job"):
            self.providers.image.begin_job(job.job_id)
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        assert scene_plan
        session.execute(delete(SceneAsset).where(SceneAsset.job_id == job.job_id))
        asset_refs: list[str] = []
        selected_assets: list[dict[str, Any]] = []
        parallelism = max(1, min(int(self.settings.asset_generation_parallelism), len(scene_plan.scenes)))
        if parallelism == 1:
            scene_results = [self._generate_assets_for_scene(job.job_id, scene, attempt) for scene in scene_plan.scenes]
        else:
            scene_results_by_id: dict[str, dict[str, Any]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="asset-scene") as executor:
                futures = {
                    executor.submit(self._generate_assets_for_scene, job.job_id, scene, attempt): scene["scene_id"]
                    for scene in scene_plan.scenes
                }
                for future in concurrent.futures.as_completed(futures):
                    scene_id = futures[future]
                    scene_results_by_id[scene_id] = future.result()
            scene_results = [scene_results_by_id[scene["scene_id"]] for scene in scene_plan.scenes]
        for result in scene_results:
            for event_name, status, payload in result["events"]:
                self._append_event(job.job_id, event_name, status, payload)
            for fallback_payload in result["fallback_events"]:
                session.add(FallbackEvent(**model_payload(FallbackEvent, fallback_payload)))
            for asset_payload in result["asset_rows"]:
                session.add(SceneAsset(**model_payload(SceneAsset, asset_payload)))
            selected_assets.append(result["selected_asset"])
            asset_refs.extend(result["asset_refs"])
        asset_gate = self.asset_gate.validate_selected(selected_assets)
        if not asset_gate.passed:
            self.storage.persist_json(job.job_id, "asset_quality_report.json", {"reasons": asset_gate.reasons, "metrics": asset_gate.metrics})
            raise RecoverableStepError(f"asset quality gate failed: {', '.join(asset_gate.reasons[:6])}")
        quality_summary = dict(job.quality_summary or {})
        quality_summary["assets"] = {**asset_gate.metrics, "semantic_threshold_pass": True}
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "asset.selected", "succeeded", quality_summary["assets"])
        return asset_refs

    def _generate_assets_for_scene(self, job_id: str, scene: dict[str, Any], attempt: int) -> dict[str, Any]:
        scene_dir = self.storage.job_dir(job_id) / "assets" / scene["scene_id"]
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        events: list[tuple[str, str, dict[str, Any]]] = []
        fallback_events: list[dict[str, Any]] = []
        fallback_used = False
        primary_provider = "minimax"
        variant_cursor = 0
        for regeneration_round in range(1, max(1, self.settings.asset_generation_regeneration_rounds) + 1):
            for variant_scene in self.image_assets.image_prompt_variants(scene, regeneration_round):
                variant_cursor += 1
                ai_path = scene_dir / ("ai.png" if variant_cursor == 1 else f"ai-{variant_cursor}.png")
                try:
                    ai_asset = self.image_assets.generate_primary_asset(job_id, variant_scene, ai_path)
                    ai_asset = self.image_assets.normalize_asset_uri_extension(ai_asset)
                    ai_scores = self.image_assets.score_asset(variant_scene, ai_asset)
                    candidates.append((ai_asset, ai_scores))
                    primary_provider = ai_asset["provider"]
                    events.append(
                        (
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
                    )
                    if self.image_assets.asset_scores_pass(ai_scores):
                        break
                except Exception as exc:  # noqa: BLE001
                    fallback_used = True
                    reason_code = "ai_provider_timeout" if "timed out" in str(exc).lower() else "ai_provider_error"
                    if not self.settings.strict_minimax_validation:
                        fallback_events.append(
                            {
                                "event_id": new_id(),
                                "job_id": job_id,
                                "schema_version": self.settings.schema_version,
                                "content_hash": stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": f"{reason_code}_{variant_cursor}"}),
                                "created_at": utcnow(),
                                "step": "asset_generation",
                                "reason_code": reason_code,
                                "attempt": attempt,
                                "scene_id": scene["scene_id"],
                                "from_provider": "minimax",
                                "to_provider": "local_semantic",
                                "reason_detail": str(exc),
                            }
                        )
                    events.append(
                        (
                            "asset.primary_candidate_failed",
                            "failed",
                            {
                                "scene_id": scene["scene_id"],
                                "variant": variant_cursor,
                                "regeneration_round": regeneration_round,
                                "reason": str(exc),
                            },
                        )
                    )
            if any(self.image_assets.asset_scores_pass(scores) for _, scores in candidates):
                break
            events.append(
                (
                    "asset.regeneration_round_completed",
                    "succeeded",
                    {
                        "scene_id": scene["scene_id"],
                        "regeneration_round": regeneration_round,
                        "candidate_count": len(candidates),
                        "passing_candidate_count": sum(1 for _, scores in candidates if self.image_assets.asset_scores_pass(scores)),
                    },
                )
            )
        needs_quality_fallback = not candidates or all(not self.image_assets.asset_scores_pass(scores) for _, scores in candidates)
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
            fallback_events.append(
                {
                    "event_id": new_id(),
                    "job_id": job_id,
                    "schema_version": self.settings.schema_version,
                    "content_hash": stable_hash({"scene": scene["scene_id"], "attempt": attempt, "mode": fallback_reason_code}),
                    "created_at": utcnow(),
                    "step": "asset_generation",
                    "reason_code": fallback_reason_code,
                    "attempt": attempt,
                    "scene_id": scene["scene_id"],
                    "from_provider": primary_provider,
                    "to_provider": "local_semantic",
                    "reason_detail": fallback_reason_detail,
                }
            )
            events.append(("asset.semantic_fallback", "succeeded", {"scene_id": scene["scene_id"]}))
            local_asset = self.providers.local_image.generate(scene, scene_dir / "local-semantic.png")
            local_asset = self.image_assets.normalize_asset_uri_extension(local_asset)
            local_scores = self.image_assets.score_asset(scene, local_asset)
            candidates.append((local_asset, local_scores))
        passing_candidates = [(asset, scores) for asset, scores in candidates if self.image_assets.asset_scores_pass(scores)]
        if passing_candidates:
            winner_asset, winner_scores = sorted(passing_candidates, key=lambda item: item[1]["total_score"], reverse=True)[0]
        else:
            self.storage.persist_json(
                job_id,
                f"assets/{scene['scene_id']}/rejected_candidates.json",
                {
                    "scene": scene,
                    "thresholds": {
                        "semantic_match": self.settings.asset_semantic_threshold,
                        "total_score": self.settings.asset_total_threshold,
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
        asset_rows: list[dict[str, Any]] = []
        for asset_payload, scores in candidates:
            selected = asset_payload["uri"] == winner_asset["uri"]
            rejection = None if selected else ("score_below_threshold" if not self.image_assets.asset_scores_pass(scores) else "score_below_winner")
            asset_rows.append(
                {
                    "asset_id": new_id(),
                    "job_id": job_id,
                    "scene_id": scene["scene_id"],
                    "schema_version": self.settings.schema_version,
                    "content_hash": stable_hash({"asset": asset_payload["uri"], "scores": scores}),
                    "created_at": utcnow(),
                    "provider": asset_payload["provider"],
                    "uri": asset_payload["uri"],
                    "width": asset_payload["width"],
                    "height": asset_payload["height"],
                    "selected": selected,
                    "scores": scores,
                    "source_url": asset_payload.get("source_url"),
                    "attribution": asset_payload.get("attribution"),
                    "license_note": asset_payload.get("license_note"),
                    "prompt_snapshot": asset_payload["prompt_snapshot"],
                    "rejection_reason": rejection,
                    "fallback_used": fallback_used and selected and asset_payload["provider"] != primary_provider,
                }
            )
        return {
            "events": events,
            "fallback_events": fallback_events,
            "asset_rows": asset_rows,
            "selected_asset": {"scene_id": scene["scene_id"], "provider": winner_asset["provider"], **winner_scores},
            "asset_refs": [path_from_uri(asset["uri"]).name for asset, _ in candidates],
        }

    def step_tts(self, session: Session, job: Job, attempt: int) -> list[str]:
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        assert script
        audio_path = self.storage.job_dir(job.job_id) / "audio" / "narration.wav"
        srt_path = self.storage.job_dir(job.job_id) / "audio" / "raw.srt"
        script_artifact = self._read_job_json(job.job_id, "script.json")
        voice_direction = self._build_voice_direction(script, topic_plan, script_artifact)
        result = self.providers.tts.synthesize(script.full_narration, audio_path, srt_path, voice_direction)
        result = self.tts.fit_tts_duration(audio_path, srt_path, result)
        if not 35_000 <= result["duration_ms"] <= 55_000:
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

    def _build_voice_direction(self, script: Script, topic_plan: TopicPlan | None, script_artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        qa_metrics = dict(script.qa_metrics or {})
        retention_source = script_artifact if isinstance(script_artifact, dict) else {}
        retention_map = retention_source.get("retention_map") if isinstance(retention_source.get("retention_map"), dict) else {}
        if not retention_map:
            retention_map = qa_metrics.get("retention_map") if isinstance(qa_metrics.get("retention_map"), dict) else {}
        return {
            "canonical_topic": topic_plan.canonical_topic if topic_plan else None,
            "angle": topic_plan.angle if topic_plan else None,
            "hook_promise": topic_plan.hook_promise if topic_plan else None,
            "title": script.title,
            "hook": script.hook,
            "body_beats": script.body_beats,
            "ending": script.ending,
            "estimated_duration_sec": script.estimated_duration_sec,
            "retention_map": retention_map,
        }

    def _read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        path = self.storage.job_dir(job_id, create=False) / relative_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def step_subtitles(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "subtitle_quality_report.json")
        self._remove_stale_quality_report(job.job_id, "subtitle_timing_report.json")
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
            items.extend(self.subtitles.split_subtitle_cue(cue, start, max(end, start)))
            cursor = end + 1
        items = self.subtitles.repair_subtitle_item_boundaries(items)
        coverage = round(min(cursor / max(len(script_words), 1), 1.0), 3)
        if coverage < 0.99:
            raise RecoverableStepError("subtitle coverage below threshold")
        drift_report = self.subtitles.estimate_subtitle_timing_drift(cues, items)
        self.storage.persist_json(
            job.job_id,
            "subtitle_timing_report.json",
            {
                "job_id": job.job_id,
                "coverage_ratio": coverage,
                **self._serialize_for_json(drift_report),
            },
        )
        subtitle_gate = self.subtitle_gate.validate(
            items,
            coverage,
            p95_drift_ms=int(drift_report["p95_drift_ms"]),
            max_drift_ms=int(drift_report["max_drift_ms"]),
        )
        if not subtitle_gate.passed:
            self.storage.persist_json(job.job_id, "subtitle_quality_report.json", {"reasons": subtitle_gate.reasons, "metrics": subtitle_gate.metrics})
            raise RecoverableStepError(f"subtitle quality gate failed: {', '.join(subtitle_gate.reasons[:6])}")
        scene_updates = normalize_scene_timings(scene_plan.scenes, narration.duration_ms)
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
        ass_path.write_text(self.subtitles.render_ass(items), encoding="utf-8")
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
            "p95_drift_ms": int(drift_report["p95_drift_ms"]),
            "max_drift_ms": int(drift_report["max_drift_ms"]),
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
        return ["subtitle_track.json", "audio/subtitles.ass", "subtitle_timing_report.json"]

    def step_background_music(self, session: Session, job: Job, attempt: int) -> list[str]:
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job.job_id))
        assert script and topic_plan and narration
        self._remove_stale_quality_report(job.job_id, "background_music_debug.json")
        self._remove_stale_quality_report(job.job_id, "background_music_quality_report.json")
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
        result, prefetch_error = self._consume_background_music_prefetch(job.job_id, raw_music_path)
        if prefetch_error is not None:
            self.music.persist_background_music_debug(
                job.job_id,
                attempt=attempt,
                topic_dict=topic_dict,
                script_dict=script_dict,
                target_duration_ms=narration.duration_ms,
                phase="prefetch_failure",
                elapsed_ms=(time.perf_counter() - music_started_at) * 1000,
                error=prefetch_error,
            )
        if result is None:
            try:
                result = self.providers.music.select_track(topic_dict, script_dict, raw_music_path, narration.duration_ms)
            except Exception as exc:
                self.music.persist_background_music_debug(
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
        self.music.persist_background_music_debug(
            job.job_id,
            attempt=attempt,
            topic_dict=topic_dict,
            script_dict=script_dict,
            target_duration_ms=narration.duration_ms,
            phase="provider_completed_prefetch" if (result.get("provider_metadata") or {}).get("prefetch_consumed") else "provider_completed",
            elapsed_ms=(time.perf_counter() - music_started_at) * 1000,
            result=result,
        )
        mixed_result = self.music.mix_background_music_with_repair(
            narration_path=path_from_uri(narration.audio_uri),
            music_path=raw_music_path,
            output_path=mixed_audio_path,
            target_duration_ms=narration.duration_ms,
            gain_db=self.settings.background_music_gain_db,
        )
        sound_design_metadata = None
        sound_design_file = None
        if self.settings.sound_design_enabled and subtitles and scene_plan:
            sound_design_file = self.music.generate_sound_design_track(
                job.job_id,
                normalize_scene_timings(scene_plan.scenes, narration.duration_ms),
                subtitles.items,
                narration.duration_ms,
            )
            sound_design_path = path_from_uri(sound_design_file["audio_uri"])
            mixed_result = {
                **mixed_result,
                **self.music.mix_sound_design_track(
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
        gate_result = self.background_music_gate.validate(
            narration_path=path_from_uri(narration.audio_uri),
            music_path=raw_music_path,
            mixed_audio_path=mixed_audio_path,
            expected_duration_ms=narration.duration_ms,
            gain_db=self.settings.background_music_gain_db,
        )
        self.storage.persist_json(
            job.job_id,
            "background_music_quality_report.json",
            {
                "job_id": job.job_id,
                "passed": gate_result.passed,
                "reasons": gate_result.reasons,
                "metrics": self._serialize_for_json(gate_result.metrics),
            },
        )
        if not gate_result.passed:
            raise RecoverableStepError(f"background music quality gate failed: {', '.join(gate_result.reasons[:6])}")
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
            **gate_result.metrics,
            "enabled": True,
            "provider": result["provider"],
            "query": result.get("query"),
            "mood": result.get("mood"),
            "gain_db": self.settings.background_music_gain_db,
            "mixed_audio": "audio/mixed.wav",
            "background_music_gate_pass": True,
            "fallback_used": bool((result.get("provider_metadata") or {}).get("fallback_used")),
            "mix_repair_used": bool(mixed_result.get("mix_repair_used")),
            "sound_design_enabled": bool(sound_design_metadata),
            "sound_design_event_count": int((sound_design_metadata or {}).get("event_count") or 0),
        }
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "background_music.mixed", "succeeded", quality_summary["background_music"])
        outputs = ["audio/background_source.wav", "audio/mixed.wav", "background_music.json", "background_music_quality_report.json", music_telemetry_file]
        if sound_design_metadata:
            outputs.extend(["audio/sound_design.wav", "sound_design.json"])
        return outputs
