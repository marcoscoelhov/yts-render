from __future__ import annotations

import math
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackgroundMusicGateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class BackgroundMusicGate:
    def validate(
        self,
        narration_path: Path,
        music_path: Path,
        mixed_audio_path: Path,
        expected_duration_ms: int,
        gain_db: float,
    ) -> BackgroundMusicGateResult:
        reasons: list[str] = []
        metrics: dict[str, Any] = {
            "gain_db": gain_db,
            "expected_duration_ms": int(expected_duration_ms),
            "narration_path": str(narration_path),
            "music_path": str(music_path),
            "mixed_audio_path": str(mixed_audio_path),
        }
        if gain_db > -8.0 or gain_db < -30.0:
            reasons.append("gain_db_outside_safe_range")

        narration = self._read_wave_stats(narration_path)
        source = self._read_wave_stats(music_path)
        mixed = self._read_wave_stats(mixed_audio_path)
        metrics.update(
            {
                "narration": narration["metrics"],
                "music_source": source["metrics"],
                "mixed": mixed["metrics"],
            }
        )
        if narration["error"]:
            reasons.append(f"narration_{narration['error']}")
        if source["error"]:
            reasons.append(f"music_source_{source['error']}")
        if mixed["error"]:
            reasons.append(f"mixed_{mixed['error']}")
        if reasons:
            return BackgroundMusicGateResult(False, reasons, metrics)

        mixed_metrics = mixed["metrics"]
        source_metrics = source["metrics"]
        if abs(int(mixed_metrics["duration_ms"]) - int(expected_duration_ms)) > 1200:
            reasons.append("mixed_duration_drift_too_high")
        if int(mixed_metrics["sample_rate_hz"]) != 24_000:
            reasons.append("mixed_sample_rate_unexpected")
        if int(mixed_metrics["channels"]) != 1:
            reasons.append("mixed_channels_unexpected")
        if float(mixed_metrics["peak_dbfs"]) > -0.2:
            reasons.append("mixed_peak_too_hot")
        source_rms_dbfs = float(source_metrics["rms_dbfs"])
        if source_rms_dbfs < -55.0:
            reasons.append("music_source_too_quiet")

        bed_ratio = self._bed_relative_rms_ratio(narration["samples"], mixed["samples"])
        metrics["bed_relative_rms_ratio"] = round(bed_ratio, 4)
        if bed_ratio < 0.015 and source_rms_dbfs < -45.0:
            reasons.append("music_bed_inaudible")
        return BackgroundMusicGateResult(not reasons, reasons, metrics)

    def _read_wave_stats(self, path: Path) -> dict[str, Any]:
        metrics: dict[str, Any] = {"path": str(path)}
        if not path.exists():
            return {"error": "missing_file", "metrics": metrics, "samples": array("h")}
        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
                raw_frames = wav_file.readframes(frame_count)
        except wave.Error:
            return {"error": "invalid_wave_file", "metrics": metrics, "samples": array("h")}
        if sample_width != 2:
            metrics.update(
                {
                    "sample_width_bytes": sample_width,
                    "sample_rate_hz": sample_rate,
                    "channels": channels,
                }
            )
            return {"error": "unsupported_sample_width", "metrics": metrics, "samples": array("h")}

        samples = array("h")
        samples.frombytes(raw_frames)
        duration_ms = round(frame_count / sample_rate * 1000) if sample_rate else 0
        peak = max((abs(sample) for sample in samples), default=0)
        rms = math.sqrt(sum(sample * sample for sample in samples) / max(len(samples), 1)) if samples else 0.0
        metrics.update(
            {
                "duration_ms": duration_ms,
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "sample_width_bytes": sample_width,
                "frame_count": frame_count,
                "peak_dbfs": self._dbfs(peak),
                "rms_dbfs": self._dbfs(rms),
            }
        )
        return {"error": None, "metrics": metrics, "samples": samples}

    def _bed_relative_rms_ratio(self, narration_samples: array, mixed_samples: array) -> float:
        length = min(len(narration_samples), len(mixed_samples))
        if length <= 0:
            return 0.0
        residual_sum = 0.0
        narration_sum = 0.0
        for idx in range(length):
            narration_sample = int(narration_samples[idx])
            mixed_sample = int(mixed_samples[idx])
            residual = mixed_sample - narration_sample
            residual_sum += residual * residual
            narration_sum += narration_sample * narration_sample
        if narration_sum <= 0:
            return 0.0
        residual_rms = math.sqrt(residual_sum / length)
        narration_rms = math.sqrt(narration_sum / length)
        return residual_rms / narration_rms if narration_rms else 0.0

    def _dbfs(self, amplitude: float) -> float:
        if amplitude <= 0:
            return -120.0
        return round(20 * math.log10(amplitude / 32767.0), 2)
