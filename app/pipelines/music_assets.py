from __future__ import annotations

from pathlib import Path
from typing import Any

from app.audio.music_mix import mix_background_music
from app.audio.sound_design import generate_sound_design_track, mix_sound_design_track
from app.pipelines.common import RecoverableStepError


class MusicDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def persist_background_music_debug(
        self,
        job_id: str,
        attempt: int,
        phase: str,
        target_duration_ms: int,
        topic_dict: dict[str, Any],
        script_dict: dict[str, Any],
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
            "strict_minimax_validation": self.pipeline.settings.strict_minimax_validation,
            "background_music_enabled": self.pipeline.settings.background_music_enabled,
            "background_music_provider": self.pipeline.settings.background_music_provider,
            "background_music_gain_db": self.pipeline.settings.background_music_gain_db,
            "minimax_music_timeout_sec": self.pipeline.settings.minimax_music_timeout_sec,
            "canonical_topic": topic_dict.get("canonical_topic"),
            "angle": topic_dict.get("angle"),
            "script_title": script_dict.get("title"),
            "script_hook": script_dict.get("hook"),
            "target_duration_ms": target_duration_ms,
            "provider": (result or {}).get("provider") or getattr(error, "provider", None),
            "query": (result or {}).get("query") or provider_details.get("query"),
            "mood": (result or {}).get("mood") or provider_details.get("mood"),
            "provider_metadata": self.pipeline._serialize_for_json(provider_metadata),
            "provider_details": self.pipeline._serialize_for_json(provider_details),
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        self.pipeline.storage.persist_json(job_id, "background_music_debug.json", self.pipeline._serialize_for_json(payload))

    def mix_background_music_with_repair(
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
                result = self.mix_background_music(
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

    def mix_background_music(
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

    def generate_sound_design_track(
        self,
        job_id: str,
        scenes: list[dict[str, Any]],
        subtitle_items: list[dict[str, Any]],
        duration_ms: int,
    ) -> dict[str, Any]:
        output_path = self.pipeline.storage.job_dir(job_id) / "audio" / "sound_design.wav"
        return generate_sound_design_track(output_path, scenes, subtitle_items, duration_ms)

    def mix_sound_design_track(self, base_audio_path: Path, sound_design_path: Path, output_path: Path, gain_db: float) -> dict[str, Any]:
        try:
            return mix_sound_design_track(
                base_audio_path=base_audio_path,
                sound_design_path=sound_design_path,
                output_path=output_path,
                gain_db=gain_db,
            )
        except RuntimeError as exc:
            raise RecoverableStepError(str(exc)) from exc
