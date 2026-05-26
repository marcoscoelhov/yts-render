from __future__ import annotations

from typing import Any

from app.utils import cosineish_similarity, jaccard_bigrams, word_tokens


def build_channel_repetition_report(
    current: dict[str, Any],
    recent_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not current.get("canonical_topic") or not current.get("script"):
        return {"repetition_risk": "unknown", "max_similarity": 0.0, "matches": []}
    script = current["script"]
    current_surface = f"{current['canonical_topic']} {current.get('angle') or ''} {script.get('hook') or ''} {script.get('title') or ''}"
    current_hook_opening = " ".join(word_tokens(script.get("hook") or "")[:5])
    current_title_opening = " ".join(word_tokens(script.get("title") or "")[:5])
    current_ending_pattern = " ".join(word_tokens(script.get("ending") or "")[:6])
    current_duration_bucket = f"{round(float(script.get('estimated_duration_sec') or 0) / 5) * 5}s"
    current_beat_count = len(script.get("body_beats") or [])
    matches = []
    exact_hook_opening_matches = 0
    exact_title_opening_matches = 0
    exact_ending_pattern_matches = 0
    exact_duration_bucket_matches = 0
    exact_beat_count_matches = 0
    for row in recent_rows:
        surface = f"{row.get('topic_summary') or ''} {row.get('title') or ''} {row.get('hook') or ''} {row.get('ending') or ''}"
        similarity = max(cosineish_similarity(current_surface, surface), jaccard_bigrams(current_surface, surface))
        hook_opening = " ".join(word_tokens(row.get("hook") or "")[:5])
        title_opening = " ".join(word_tokens(row.get("title") or "")[:5])
        ending_pattern = " ".join(word_tokens(row.get("ending") or "")[:6])
        duration_bucket = f"{round(float(row.get('estimated_duration_sec') or 0) / 5) * 5}s" if row.get("estimated_duration_sec") else None
        beat_count = len(row.get("body_beats") or [])
        signals: list[str] = []
        if current_hook_opening and hook_opening == current_hook_opening:
            exact_hook_opening_matches += 1
            signals.append("same_hook_opening")
        if current_title_opening and title_opening == current_title_opening:
            exact_title_opening_matches += 1
            signals.append("same_title_opening")
        if current_ending_pattern and ending_pattern == current_ending_pattern:
            exact_ending_pattern_matches += 1
            signals.append("same_ending_pattern")
        if current_duration_bucket and duration_bucket == current_duration_bucket:
            exact_duration_bucket_matches += 1
            signals.append("same_duration_bucket")
        if current_beat_count and beat_count == current_beat_count:
            exact_beat_count_matches += 1
            signals.append("same_beat_count")
        substantive_signals = {"same_hook_opening", "same_title_opening", "same_ending_pattern"} & set(signals)
        if similarity >= 0.45 or substantive_signals:
            matches.append(
                {
                    "job_id": row.get("job_id"),
                    "similarity": round(similarity, 3),
                    "title": row.get("title"),
                    "duration_bucket": duration_bucket,
                    "beat_count": beat_count,
                    "signals": signals,
                    "template_signal_count": len(signals),
                }
            )
    max_similarity = max((match["similarity"] for match in matches), default=0.0)
    repetitive_template_matches = sum(
        1
        for match in matches
        if match.get("template_signal_count", 0) >= 2
        and (
            match["similarity"] >= 0.45
            or {"same_hook_opening", "same_title_opening", "same_ending_pattern"} & set(match.get("signals") or [])
        )
    )
    exact_structural_signature_matches = sum(
        1 for match in matches if {"same_hook_opening", "same_duration_bucket", "same_beat_count"} <= set(match.get("signals") or [])
    )
    risk = "low"
    if (
        max_similarity >= 0.72
        or repetitive_template_matches >= 2
        or exact_hook_opening_matches >= 2
        or exact_structural_signature_matches >= 1
    ):
        risk = "high"
    elif (
        max_similarity >= 0.55
        or len(matches) >= 3
        or exact_hook_opening_matches >= 1
        or exact_title_opening_matches >= 1
        or exact_ending_pattern_matches >= 1
    ):
        risk = "medium"
    return {
        "repetition_risk": risk,
        "max_similarity": max_similarity,
        "matches": matches[:5],
        "signals": {
            "exact_hook_opening_matches": exact_hook_opening_matches,
            "exact_title_opening_matches": exact_title_opening_matches,
            "exact_ending_pattern_matches": exact_ending_pattern_matches,
            "exact_duration_bucket_matches": exact_duration_bucket_matches,
            "exact_beat_count_matches": exact_beat_count_matches,
            "exact_structural_signature_matches": exact_structural_signature_matches,
            "repetitive_template_matches": repetitive_template_matches,
        },
        "profile": {
            "hook_opening": current_hook_opening,
            "title_opening": current_title_opening,
            "ending_pattern": current_ending_pattern,
            "duration_bucket": current_duration_bucket,
            "beat_count": current_beat_count,
            "visual_style": "ai_science_short",
        },
    }
