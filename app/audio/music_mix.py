from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import imageio_ffmpeg


def mix_background_music(
    narration_path: Path,
    music_path: Path,
    output_path: Path,
    target_duration_ms: int,
    gain_db: float,
    strategy: str = "sidechaincompress+amix+loudnorm",
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = max(target_duration_ms / 1000, 1.0)
    fade_out_start = max(duration_sec - 1.2, 0.0)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    temp_output = output_path.with_suffix(".tmp.wav")
    if strategy == "sidechaincompress+amix+loudnorm":
        filter_graph = (
            f"[0:a]aresample=24000,volume={gain_db}dB,atrim=0:{duration_sec:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d=1.2[bg];"
            "[bg][1:a]sidechaincompress=threshold=0.025:ratio=10:attack=15:release=300[duck];"
            "[1:a][duck]amix=inputs=2:weights='1 0.8':normalize=0,"
            "loudnorm=I=-16:LRA=11:TP=-1.5[out]"
        )
    else:
        filter_graph = (
            f"[0:a]aresample=24000,volume={gain_db}dB,atrim=0:{duration_sec:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d=1.2[bg];"
            "[1:a]aresample=24000[voc];"
            "[voc][bg]amix=inputs=2:weights='1 0.55':normalize=0,"
            "alimiter=limit=0.93,loudnorm=I=-16:LRA=11:TP=-1.5[out]"
        )
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(music_path),
                "-i",
                str(narration_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[out]",
                "-t",
                f"{duration_sec:.3f}",
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
            raise RuntimeError(f"background music mix failed ({strategy})")
        temp_output.replace(output_path)
    finally:
        temp_output.unlink(missing_ok=True)
    return {
        "mix_filter": strategy,
        "mix_target_lufs": -16.0,
        "mix_true_peak_limit_db": -1.5,
    }
