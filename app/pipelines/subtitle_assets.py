from __future__ import annotations

import math
from typing import Any

from app.quality.subtitle_gate import BAD_ENDINGS
from app.utils import split_caption_chunks, word_tokens, wrap_caption


class SubtitleDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def split_subtitle_cue(self, cue: dict[str, Any], token_start: int, token_end: int) -> list[dict[str, Any]]:
        chunks = self.split_caption_by_subtitle_limits(str(cue["text"])) or [str(cue["text"])]
        chunks = self.avoid_weak_subtitle_endings(chunks)
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

    def split_caption_by_subtitle_limits(self, text: str, max_words: int = 14, max_chars: int = 42, max_lines: int = 2) -> list[str]:
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
                if len(word_tokens(candidate)) <= max_words and self.subtitle_chunk_fits(candidate, max_chars=max_chars, max_lines=max_lines):
                    chunks.append(candidate)
                    continue
                chunks.extend(split_caption_chunks(candidate, max_chars=max_chars, max_lines=max_lines))
        return chunks

    def avoid_weak_subtitle_endings(self, chunks: list[str]) -> list[str]:
        repaired = [chunk for chunk in chunks if chunk.strip()]
        for index in range(len(repaired) - 1):
            current_text, next_text, _ = self.rebalance_subtitle_boundary(repaired[index], repaired[index + 1])
            if current_text:
                repaired[index] = current_text
            if next_text:
                repaired[index + 1] = next_text
            else:
                repaired[index + 1] = ""
        return [chunk for chunk in repaired if chunk.strip()]

    def subtitle_chunk_fits(self, text: str, max_chars: int = 42, max_lines: int = 2, max_words: int = 14) -> bool:
        normalized = str(text).strip()
        if not normalized:
            return False
        if len(word_tokens(normalized)) > max_words:
            return False
        return len(split_caption_chunks(normalized, max_chars=max_chars, max_lines=max_lines)) == 1

    def rebalance_subtitle_boundary(
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
            if not self.subtitle_chunk_fits(candidate_current, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            if candidate_next and not self.subtitle_chunk_fits(candidate_next, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
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
            if not self.subtitle_chunk_fits(candidate_current, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            if not self.subtitle_chunk_fits(candidate_next, max_chars=max_chars, max_lines=max_lines, max_words=max_words):
                continue
            return candidate_current, candidate_next, -moved_count

        return " ".join(current_words), " ".join(next_words), 0

    def repair_subtitle_item_boundaries(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        repaired = [dict(item) for item in items if str(item.get("text") or "").strip()]
        for index in range(len(repaired) - 1):
            current = repaired[index]
            following = repaired[index + 1]
            original_current_word_count = len(word_tokens(str(current["text"])))
            original_following_word_count = len(word_tokens(str(following["text"])))
            if not original_current_word_count or not original_following_word_count:
                continue
            current_text, next_text, delta = self.rebalance_subtitle_boundary(str(current["text"]), str(following["text"]))
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

    def estimate_subtitle_timing_drift(self, cues: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
        cue_map = {int(cue["idx"]): cue for cue in cues if cue.get("idx") is not None}
        grouped_items: dict[int, list[dict[str, Any]]] = {}
        for item in items:
            idx_surface = str(item.get("idx") or "")
            parent_idx = idx_surface.split(".", 1)[0]
            if not parent_idx.isdigit():
                continue
            grouped_items.setdefault(int(parent_idx), []).append(item)
        drifts_ms: list[int] = []
        drift_items: list[dict[str, Any]] = []
        for parent_idx, grouped in grouped_items.items():
            cue = cue_map.get(parent_idx)
            if cue is None:
                continue
            total_words = max(sum(max(len(word_tokens(str(item.get("text") or ""))), 1) for item in grouped), 1)
            cue_start_ms = int(cue["start_ms"])
            cue_end_ms = int(cue["end_ms"])
            cue_duration_ms = max(cue_end_ms - cue_start_ms, len(grouped))
            elapsed_words = 0
            for item_index, item in enumerate(grouped, start=1):
                chunk_word_count = max(len(word_tokens(str(item.get("text") or ""))), 1)
                expected_start_ms = cue_start_ms + round(elapsed_words / total_words * cue_duration_ms)
                elapsed_words += chunk_word_count
                expected_end_ms = (
                    cue_end_ms
                    if item_index == len(grouped)
                    else cue_start_ms + round(elapsed_words / total_words * cue_duration_ms)
                )
                actual_start_ms = int(item["start_ms"])
                actual_end_ms = int(item["end_ms"])
                drift_ms = max(abs(actual_start_ms - expected_start_ms), abs(actual_end_ms - expected_end_ms))
                drifts_ms.append(drift_ms)
                drift_items.append(
                    {
                        "idx": item.get("idx"),
                        "parent_cue_idx": parent_idx,
                        "drift_ms": drift_ms,
                        "expected_start_ms": expected_start_ms,
                        "expected_end_ms": expected_end_ms,
                        "actual_start_ms": actual_start_ms,
                        "actual_end_ms": actual_end_ms,
                    }
                )
        if not drifts_ms:
            return {
                "timing_basis": "raw_srt_proportional_split",
                "drift_item_count": 0,
                "p95_drift_ms": 0,
                "max_drift_ms": 0,
                "worst_items": [],
            }
        sorted_drifts = sorted(drifts_ms)
        percentile_index = max(0, math.ceil(len(sorted_drifts) * 0.95) - 1)
        worst_items = sorted(drift_items, key=lambda item: int(item["drift_ms"]), reverse=True)[:5]
        return {
            "timing_basis": "raw_srt_proportional_split",
            "drift_item_count": len(drifts_ms),
            "p95_drift_ms": int(sorted_drifts[percentile_index]),
            "max_drift_ms": int(sorted_drifts[-1]),
            "worst_items": worst_items,
        }

    def render_ass(self, items: list[dict[str, Any]]) -> str:
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
            start = self.ms_to_ass(item["start_ms"])
            end = self.ms_to_ass(item["end_ms"])
            text = wrap_caption(item["text"]).replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
        return "\n".join(lines) + "\n"

    def ms_to_ass(self, ms: int) -> str:
        hours, rem = divmod(ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        centis = round(millis / 10)
        return f"{hours}:{minutes:02}:{seconds:02}.{centis:02}"
