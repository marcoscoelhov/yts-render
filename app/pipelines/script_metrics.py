from __future__ import annotations

import re
from typing import Any

def normalize_script_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metrics)
    score_keys = {
        "hook_score",
        "clarity_score",
        "information_density_score",
        "repetition_score",
        "ending_strength_score",
    }
    for key in score_keys:
        value = _coerce_script_metric_value(normalized.get(key))
        if isinstance(value, int | float) and 1 < value <= 10:
            normalized[key] = round(value / 10, 3)
            continue
        normalized[key] = value
    repetition_value = normalized.get("repetition_score")
    if repetition_value == 1:
        normalized["repetition_score"] = 0.1
        return normalized
    if isinstance(repetition_value, int | float) and 0.88 < repetition_value <= 1:
        normalized["repetition_score"] = round(max(0.0, 1 - repetition_value), 3)
    return normalized


def _coerce_script_metric_value(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "passed", "pass", "ok", "aprovado"}:
        return True
    if lowered in {"false", "failed", "fail", "reprovado"}:
        return False
    fraction_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", stripped)
    if fraction_match:
        numerator = float(fraction_match.group(1))
        denominator = float(fraction_match.group(2))
        if denominator > 0:
            return round(numerator / denominator, 3)
    percentage_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", stripped)
    if percentage_match:
        return round(float(percentage_match.group(1)) / 100, 3)
    normalized_number = stripped.replace(",", ".")
    try:
        return float(normalized_number)
    except ValueError:
        return value
