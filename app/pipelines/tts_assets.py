from __future__ import annotations

from pathlib import Path
from typing import Any


class TTSDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def fit_tts_duration(self, audio_path: Path, srt_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline._fit_tts_duration(audio_path, srt_path, result)

    def scale_srt_timings(self, srt_path: Path, speed: float) -> None:
        self.pipeline._scale_srt_timings(srt_path, speed)

    def measure_audio_ms(self, audio_path: Path) -> int:
        return self.pipeline._measure_audio_ms(audio_path)
