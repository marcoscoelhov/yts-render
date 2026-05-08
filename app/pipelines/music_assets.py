from __future__ import annotations

from pathlib import Path
from typing import Any


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
        self.pipeline._persist_background_music_debug(job_id, attempt, topic_dict, script_dict, target_duration_ms, phase, elapsed_ms, result, error)

    def mix_background_music_with_repair(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
    ) -> dict[str, Any]:
        return self.pipeline._mix_background_music_with_repair(narration_path, music_path, output_path, target_duration_ms, gain_db)

    def mix_background_music(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        target_duration_ms: int,
        gain_db: float,
        strategy: str = "sidechaincompress+amix+loudnorm",
    ) -> dict[str, Any]:
        return self.pipeline._mix_background_music(narration_path, music_path, output_path, target_duration_ms, gain_db, strategy)

    def generate_sound_design_track(
        self,
        job_id: str,
        scenes: list[dict[str, Any]],
        subtitle_items: list[dict[str, Any]],
        duration_ms: int,
    ) -> dict[str, Any]:
        return self.pipeline._generate_sound_design_track(job_id, scenes, subtitle_items, duration_ms)

    def mix_sound_design_track(self, base_audio_path: Path, sound_design_path: Path, output_path: Path, gain_db: float) -> dict[str, Any]:
        return self.pipeline._mix_sound_design_track(base_audio_path, sound_design_path, output_path, gain_db)
