from __future__ import annotations

import math
import queue
import re
import subprocess
import threading
import time
import wave
import concurrent.futures
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audio.music_mix import mix_background_music
from app.audio.sound_design import generate_sound_design_track, mix_sound_design_track
from app.db import session_scope
from app.models import BackgroundMusicAsset, FallbackEvent, Job, NarrationAsset, SceneAsset, ScenePlan, Script, SubtitleTrack, TopicPlan
from app.pipelines.common import RecoverableStepError, model_payload
from app.pipelines.base import BasePipeline
from app.pipelines.image_assets import ImageAssetDomain
from app.pipelines.music_assets import MusicDomain
from app.pipelines.subtitle_assets import SubtitleDomain
from app.pipelines.timeline import normalize_scene_timings
from app.pipelines.tts_assets import TTSDomain
from app.quality.subtitle_gate import BAD_ENDINGS
from app.utils import file_uri, ms_to_srt, new_id, parse_srt, path_from_uri, split_caption_chunks, stable_hash, utcnow, word_tokens, wrap_caption


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


class AssetPipeline(BasePipeline):
    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.image_assets = ImageAssetDomain(self)
        self.tts = TTSDomain(self)
        self.subtitles = SubtitleDomain(self)
        self.music = MusicDomain(self)
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
                    ai_asset = self.image_assets.generate_primary_asset(variant_scene, ai_path)
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
        assert script
        audio_path = self.storage.job_dir(job.job_id) / "audio" / "narration.wav"
        srt_path = self.storage.job_dir(job.job_id) / "audio" / "raw.srt"
        result = self.providers.tts.synthesize(script.full_narration, audio_path, srt_path)
        result = self.tts.fit_tts_duration(audio_path, srt_path, result)
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

    def step_subtitles(self, session: Session, job: Job, attempt: int) -> list[str]:
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
            items.extend(self.subtitles.split_subtitle_cue(cue, start, max(end, start)))
            cursor = end + 1
        items = self.subtitles.repair_subtitle_item_boundaries(items)
        coverage = round(min(cursor / max(len(script_words), 1), 1.0), 3)
        if coverage < 0.99:
            raise RecoverableStepError("subtitle coverage below threshold")
        subtitle_gate = self.subtitle_gate.validate(items, coverage)
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

    def step_background_music(self, session: Session, job: Job, attempt: int) -> list[str]:
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

    def _generate_primary_asset(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put(("ok", self.providers.image.generate(scene, output_path)), block=False)
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("error", exc), block=False)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=self.settings.asset_generation_timeout_sec)
        if thread.is_alive():
            raise RecoverableStepError(
                f"asset primary generation timed out after {self.settings.asset_generation_timeout_sec}s"
            )
        status, payload = result_queue.get_nowait()
        if status == "error":
            raise payload
        return payload

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
            prompt = f"{prompt}, {NO_TEXT_IMAGE_CONSTRAINT}".strip(", ")
            prompt_lower = prompt.lower()
        for constraint in extra_constraints:
            if constraint.lower() not in prompt_lower:
                prompt = f"{prompt}, {constraint}"
                prompt_lower = prompt.lower()
        return prompt

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
            self.tts.scale_srt_timings(srt_path, speed)
        finally:
            temp_audio.unlink(missing_ok=True)
        adjusted = dict(result)
        adjusted["duration_ms"] = self.tts.measure_audio_ms(audio_path)
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
        with wave.open(str(audio_path), "rb") as wav_file:
            return int(wav_file.getnframes() / wav_file.getframerate() * 1000)

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

    def _ms_to_ass(self, ms: int) -> str:
        hours, rem = divmod(ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        centis = round(millis / 10)
        return f"{hours}:{minutes:02}:{seconds:02}.{centis:02}"
