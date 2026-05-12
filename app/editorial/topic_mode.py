from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


STRICT_OVERRIDE_PATTERN = re.compile(
    r"\b(?:factual[_\s-]*strict|strict[_\s-]*fact|research[_\s-]*strict|com\s+fontes|lastro\s+factual|grounded)\b",
    re.IGNORECASE,
)

HIGH_RISK_TOPIC_PATTERN = re.compile(
    r"\b(?:sa[uú]de|m[eé]dico|medicina|doen[cç]a|doen[cç]as|tratamento|tratamentos|rem[eé]dio|remedios|medicamento|"
    r"medicamentos|suplemento|suplementos|dosagem|gravidez|gesta[cç][aã]o|c[aâ]ncer|diabetes|ansiedade|depress[aã]o|"
    r"cirurgia|sintoma|sintomas|anatomia\s+humana|corpo\s+humano|investimento|investimentos|a[cç][oõ]es|cripto|"
    r"criptomoeda|criptomoedas|imposto|impostos|lei|leis|legal|jur[ií]dico|crime|crimes|pol[ií]tica|elei[cç][aã]o|"
    r"elei[cç][oõ]es|seguran[cç]a|risco|perigo)\b",
    re.IGNORECASE,
)


def _read_field(source: Any, field: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(field, default)
    return getattr(source, field, default)


def resolve_editorial_mode(topic_plan: Any | None = None, request: Any | None = None) -> str:
    notes = str(_read_field(request, "notes", "") or "")
    requested_angle = str(_read_field(request, "requested_angle", "") or "")
    seed_theme = str(_read_field(request, "seed_theme", "") or "")
    canonical_topic = str(_read_field(topic_plan, "canonical_topic", "") or "")
    angle = str(_read_field(topic_plan, "angle", "") or "")
    hook_promise = str(_read_field(topic_plan, "hook_promise", "") or "")
    quality_metrics = _read_field(topic_plan, "quality_metrics", {}) or {}
    if isinstance(quality_metrics, Mapping):
        existing_mode = str(quality_metrics.get("editorial_mode") or "").strip()
        if existing_mode in {"viral_curiosidades", "factual_strict"}:
            return existing_mode
    override_text = " ".join(part for part in [notes, requested_angle] if part).strip()
    if override_text and STRICT_OVERRIDE_PATTERN.search(override_text):
        return "factual_strict"
    source_text = " ".join(part for part in [seed_theme, canonical_topic, angle, hook_promise] if part).strip()
    if source_text and HIGH_RISK_TOPIC_PATTERN.search(source_text):
        return "factual_strict"
    return "viral_curiosidades"
