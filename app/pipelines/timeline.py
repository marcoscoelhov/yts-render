from __future__ import annotations

from typing import Any


def normalize_scene_timings(scenes: list[dict[str, Any]], total_duration_ms: int) -> list[dict[str, Any]]:
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
