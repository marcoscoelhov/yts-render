from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.quality.script_gate import MARKUP_PATTERN
from app.utils import word_tokens


BAD_ENDINGS = {"de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por", "para", "que", "e"}
P95_DRIFT_THRESHOLD_MS = 900
MAX_DRIFT_THRESHOLD_MS = 1800


@dataclass(frozen=True)
class SubtitleGateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class SubtitleGate:
    def validate(self, items: list[dict[str, Any]], coverage_ratio: float, p95_drift_ms: int = 0, max_drift_ms: int = 0) -> SubtitleGateResult:
        reasons: list[str] = []
        item_results: list[dict[str, Any]] = []
        if coverage_ratio < 0.99:
            reasons.append("coverage_below_threshold")
        if p95_drift_ms > P95_DRIFT_THRESHOLD_MS:
            reasons.append("p95_timing_drift_too_high")
        if max_drift_ms > MAX_DRIFT_THRESHOLD_MS:
            reasons.append("max_timing_drift_too_high")
        if not items:
            reasons.append("missing_subtitle_items")
        for item in items:
            idx = str(item.get("idx"))
            text = str(item.get("text") or "").strip()
            item_reasons: list[str] = []
            if not text:
                item_reasons.append("empty_text")
            if MARKUP_PATTERN.search(text):
                item_reasons.append("markup_or_ssml_leaked")
            if re.search(r"\b[a-záàãâéêíóõôúç]$", text, re.IGNORECASE) and text.lower()[-1] not in {"a", "à", "á", "ã", "â", "e", "é", "ê", "o", "ó", "õ", "ô"}:
                item_reasons.append("possible_truncated_word")
            words = word_tokens(text)
            if len(words) > 14:
                item_reasons.append("subtitle_too_long")
            if words and words[-1].lower() in BAD_ENDINGS:
                item_reasons.append("weak_line_ending")
            start_ms = int(item.get("start_ms", 0))
            end_ms = int(item.get("end_ms", 0))
            if end_ms <= start_ms:
                item_reasons.append("invalid_timing")
            item_results.append({"idx": idx, "passed": not item_reasons, "reasons": item_reasons})
            reasons.extend(f"{idx}:{reason}" for reason in item_reasons)
        return SubtitleGateResult(
            passed=not reasons,
            reasons=reasons,
            metrics={
                "coverage_ratio": coverage_ratio,
                "item_count": len(items),
                "p95_drift_ms": int(p95_drift_ms),
                "max_drift_ms": int(max_drift_ms),
                "items": item_results,
            },
        )
