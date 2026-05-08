from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from app.utils import file_uri


def generate_sound_design_track(
    output_path: Path,
    scenes: list[dict[str, Any]],
    subtitle_items: list[dict[str, Any]],
    duration_ms: int,
) -> dict[str, Any]:
    sample_rate = 24_000
    total_frames = max(int(duration_ms * sample_rate / 1000), 1)
    samples = [0.0] * total_frames
    scene_events = [
        int(scene.get("actual_start_ms") or scene.get("start_ms") or 0)
        for scene in scenes[1:5]
        if int(scene.get("actual_start_ms") or scene.get("start_ms") or 0) > 0
    ]
    subtitle_events = [
        int(item.get("start_ms") or 0)
        for item in subtitle_items[1::2][:4]
        if int(item.get("start_ms") or 0) > 0
    ]
    event_times = sorted(set(scene_events + subtitle_events))
    if not event_times:
        event_times = [min(1200, max(duration_ms // 4, 250))]
    for index, start_ms in enumerate(event_times):
        frequency = 880.0 if index % 2 == 0 else 640.0
        duration_frames = int(sample_rate * (0.09 if index % 2 == 0 else 0.06))
        start_frame = min(total_frames - 1, int(start_ms * sample_rate / 1000))
        for offset in range(duration_frames):
            frame = start_frame + offset
            if frame >= total_frames:
                break
            envelope = math.exp(-4.8 * (offset / max(duration_frames, 1)))
            pulse = math.sin(2 * math.pi * frequency * (offset / sample_rate))
            samples[frame] += 0.12 * envelope * pulse
    pcm = bytearray()
    for sample in samples:
        clamped = max(-1.0, min(1.0, sample))
        pcm.extend(int(clamped * 32767).to_bytes(2, byteorder="little", signed=True))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(pcm))
    return {
        "audio_uri": file_uri(output_path),
        "provider": "local_sfx",
        "license_note": "local_generated_sound_design",
        "sample_rate": sample_rate,
        "event_count": len(event_times),
        "event_times_ms": event_times[:8],
    }


def mix_sound_design_track(
    base_audio_path: Path,
    sound_design_path: Path,
    output_path: Path,
    gain_db: float,
) -> dict[str, Any]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    temp_output = output_path.with_suffix(".sfx.tmp.wav")
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(base_audio_path),
                "-i",
                str(sound_design_path),
                "-filter_complex",
                (
                    "[0:a]aresample=24000[base];"
                    f"[1:a]aresample=24000,volume={gain_db}dB[sfx];"
                    "[base][sfx]amix=inputs=2:weights='1 0.85':normalize=0,"
                    "alimiter=limit=0.93,loudnorm=I=-16:LRA=11:TP=-1.5[out]"
                ),
                "-map",
                "[out]",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(temp_output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("sound design mix failed")
        temp_output.replace(output_path)
    finally:
        temp_output.unlink(missing_ok=True)
    return {
        "sound_design_mix_filter": "amix+loudnorm",
        "sound_design_gain_db": gain_db,
    }
