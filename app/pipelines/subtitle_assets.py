from __future__ import annotations

from typing import Any


class SubtitleDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def split_subtitle_cue(self, cue: dict[str, Any], token_start: int, token_end: int) -> list[dict[str, Any]]:
        return self.pipeline._split_subtitle_cue(cue, token_start, token_end)

    def split_caption_by_subtitle_limits(self, text: str, max_words: int = 14, max_chars: int = 42, max_lines: int = 2) -> list[str]:
        return self.pipeline._split_caption_by_subtitle_limits(text, max_words, max_chars, max_lines)

    def avoid_weak_subtitle_endings(self, chunks: list[str]) -> list[str]:
        return self.pipeline._avoid_weak_subtitle_endings(chunks)

    def subtitle_chunk_fits(self, text: str, max_chars: int = 42, max_lines: int = 2, max_words: int = 14) -> bool:
        return self.pipeline._subtitle_chunk_fits(text, max_chars, max_lines, max_words)

    def rebalance_subtitle_boundary(
        self,
        left: str,
        right: str,
        max_chars: int = 42,
        max_lines: int = 2,
        max_words: int = 14,
    ) -> tuple[str, str]:
        return self.pipeline._rebalance_subtitle_boundary(left, right, max_chars, max_lines, max_words)

    def repair_subtitle_item_boundaries(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.pipeline._repair_subtitle_item_boundaries(items)

    def render_ass(self, items: list[dict[str, Any]]) -> str:
        return self.pipeline._render_ass(items)

    def ms_to_ass(self, ms: int) -> str:
        return self.pipeline._ms_to_ass(ms)
