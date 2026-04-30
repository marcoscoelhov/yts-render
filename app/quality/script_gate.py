from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils import avg_words_per_sentence, max_words_single_sentence, word_tokens


ALLOWED_NON_PT_TERMS = {
    "gps",
    "usgs",
    "chaiten",
    "eyjafjallajokull",
    "einstein",
    "youtube",
    "shorts",
}

FOREIGN_LANGUAGE_MARKERS = {
    "right",
    "giving",
    "your",
    "cat",
    "second",
    "chance",
    "see",
    "heard",
    "mini-cerebro independiente",
    "independiente",
}

MARKUP_PATTERN = re.compile(r"</?[a-zA-Z][^>\s]*(?:\s[^>]*)?>?|&(?:lt|gt|amp|quot|apos);")
SUSPICIOUS_GLUED_PATTERN = re.compile(
    r"\b(?:um|uma|o|a|os|as|de|do|da|dos|das|no|na|nos|nas|e|que)(?:mini|micro|macro|super|ultra)[a-záàãâéêíóõôúç-]*\b",
    re.IGNORECASE,
)
GENERIC_HOOK_OPENING_PATTERN = re.compile(
    r"^\s*(?:você\s+sabia|voce\s+sabia|já\s+imaginou|ja\s+imaginou|nesse\s+v[ií]deo|neste\s+v[ií]deo)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScriptGateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class ScriptQualityGate:
    def validate(self, script: dict[str, Any], target_duration_sec: int) -> ScriptGateResult:
        reasons: list[str] = []
        full_narration = str(script.get("full_narration") or "")
        title = str(script.get("title") or "")
        text_fields = self._collect_text(script)
        combined_text = "\n".join(text_fields)

        if not full_narration.strip():
            reasons.append("missing_full_narration")
        if str(script.get("language") or "").lower() not in {"pt-br", "pt_br", "portuguese-br"}:
            reasons.append("language_field_not_pt_br")
        if MARKUP_PATTERN.search(combined_text):
            reasons.append("markup_or_ssml_leaked")
        if self._contains_foreign_language(combined_text):
            reasons.append("foreign_language_detected")
        if SUSPICIOUS_GLUED_PATTERN.search(combined_text.replace("-", "")):
            reasons.append("suspicious_glued_words")
        if GENERIC_HOOK_OPENING_PATTERN.search(str(script.get("hook") or "")) or GENERIC_HOOK_OPENING_PATTERN.search(full_narration):
            reasons.append("generic_hook_opening")

        word_count = len(word_tokens(full_narration))
        estimated_duration = float(script.get("estimated_duration_sec") or max(0, word_count / 2.55))
        avg_sentence = avg_words_per_sentence(full_narration)
        max_sentence = max_words_single_sentence(full_narration)
        words_per_second = round(word_count / estimated_duration, 2) if estimated_duration else 0.0
        target_min = max(24.5, target_duration_sec - 10)
        target_max = min(46.5, target_duration_sec + 10)

        if not 25 <= estimated_duration <= 45:
            reasons.append("estimated_duration_outside_absolute_range")
        if not target_min <= estimated_duration <= target_max:
            reasons.append("estimated_duration_outside_target_window")
        if avg_sentence > 14:
            reasons.append("avg_sentence_too_long")
        if max_sentence > 20:
            reasons.append("sentence_too_long")
        if not title.strip():
            reasons.append("missing_title")

        qa_metrics = dict(script.get("qa_metrics") or {})
        numeric_checks = {
            "hook_score": (0.80, None),
            "clarity_score": (0.75, None),
            "information_density_score": (0.75, None),
            "ending_strength_score": (0.75, None),
            "repetition_score": (None, 0.88),
        }
        for key, (minimum, maximum) in numeric_checks.items():
            value = qa_metrics.get(key)
            if not isinstance(value, int | float):
                reasons.append(f"missing_{key}")
                continue
            if minimum is not None and value < minimum:
                reasons.append(f"{key}_below_threshold")
            if maximum is not None and value >= maximum:
                reasons.append(f"{key}_above_threshold")

        metrics = {
            **qa_metrics,
            "word_count": word_count,
            "estimated_duration_sec": estimated_duration,
            "avg_words_per_sentence": round(avg_sentence, 2),
            "max_words_single_sentence": max_sentence,
            "words_per_second": words_per_second,
            "target_duration_sec": target_duration_sec,
            "script_quality_gate_pass": not reasons,
            "script_quality_gate_reasons": reasons,
        }
        return ScriptGateResult(passed=not reasons, reasons=reasons, metrics=metrics)

    def _collect_text(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            texts: list[str] = []
            for item in value:
                texts.extend(self._collect_text(item))
            return texts
        if isinstance(value, dict):
            texts = []
            for key, item in value.items():
                if key in {"image_prompt", "prompt_snapshot"}:
                    continue
                texts.extend(self._collect_text(item))
            return texts
        return []

    def _contains_foreign_language(self, text: str) -> bool:
        normalized = self._normalize(text)
        tokens = set(re.findall(r"\b[a-z]{2,}\b", normalized))
        tokens -= ALLOWED_NON_PT_TERMS
        if tokens & FOREIGN_LANGUAGE_MARKERS:
            return True
        for phrase in FOREIGN_LANGUAGE_MARKERS:
            if " " in phrase and phrase in normalized:
                return True
        return False

    def _normalize(self, text: str) -> str:
        text = text.lower()
        replacements = {
            "á": "a",
            "à": "a",
            "ã": "a",
            "â": "a",
            "é": "e",
            "ê": "e",
            "í": "i",
            "ó": "o",
            "õ": "o",
            "ô": "o",
            "ú": "u",
            "ç": "c",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text
