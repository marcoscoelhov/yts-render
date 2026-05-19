from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.utils import tokenize, word_tokens


READY_SCRIPT_BEGIN = "[[YTS_READY_SCRIPT_BEGIN]]"
READY_SCRIPT_END = "[[YTS_READY_SCRIPT_END]]"
FACT_CHECK_CONFIRMED = "ready_script_fact_check_confirmed=true"

_LABELS = {
    "titulo": "title",
    "título": "title",
    "title": "title",
    "hook": "hook",
    "loop": "loop",
    "beats": "beats",
    "beat": "beats",
    "payoff": "payoff",
    "fechamento": "closing",
    "closing": "closing",
    "hashtags": "hashtags",
    "tags": "hashtags",
}

_LABEL_PATTERN = re.compile(
    r"^\s*(t[ií]tulo|title|hook|loop|beats?|payoff|fechamento|closing|hashtags?|tags)\s*:\s*(.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReadyScript:
    raw_text: str
    fact_check_confirmed: bool
    script: dict[str, Any]
    fact_pack: dict[str, Any]
    hashtags: list[str]


def build_ready_script_notes(notes: str | None, raw_script: str, fact_check_confirmed: bool) -> str:
    parts = [
        "input_mode=script",
        FACT_CHECK_CONFIRMED if fact_check_confirmed else "ready_script_fact_check_confirmed=false",
        READY_SCRIPT_BEGIN,
        raw_script.strip(),
        READY_SCRIPT_END,
    ]
    existing = (notes or "").strip()
    return "\n".join([existing, *parts] if existing else parts)


def extract_ready_script_from_notes(notes: str | None) -> ReadyScript | None:
    text = notes or ""
    if "input_mode=script" not in text or READY_SCRIPT_BEGIN not in text or READY_SCRIPT_END not in text:
        return None
    raw = text.split(READY_SCRIPT_BEGIN, 1)[1].split(READY_SCRIPT_END, 1)[0].strip()
    if not raw:
        return None
    return parse_ready_script(raw, fact_check_confirmed=FACT_CHECK_CONFIRMED in text)


def parse_ready_script(raw_text: str, *, fact_check_confirmed: bool) -> ReadyScript:
    fields = _parse_labeled_text(raw_text)
    missing = [label for label in ["title", "hook", "loop", "beats", "payoff", "closing"] if not fields.get(label)]
    if missing:
        raise ValueError(f"roteiro pronto sem campos obrigatorios: {', '.join(missing)}")

    beats = _split_beats(fields["beats"])
    if not beats:
        raise ValueError("roteiro pronto precisa ter pelo menos um beat")
    payoff = _clean_sentence(fields["payoff"])
    closing = _clean_sentence(fields["closing"])
    hook = _clean_sentence(fields["hook"])
    loop = _clean_sentence(fields["loop"])
    title = _clean_title(fields["title"])
    hashtags = _parse_hashtags(fields.get("hashtags", ""))

    narration_parts = [hook, loop, *beats, payoff, closing]
    full_narration = " ".join(_ensure_sentence(part) for part in narration_parts if part).strip()
    # Loop is an editorial tension question, not a factual claim to audit/ground.
    key_facts = [*beats, payoff]
    source_fact_ids = [f"D{index}" for index in range(1, len(key_facts) + 1)] if fact_check_confirmed else []
    claim_trace = [
        {"text": fact, "source_fact_ids": [source_id], "grounding": "fact_pack"}
        for fact, source_id in zip(key_facts, source_fact_ids, strict=False)
    ]
    estimated_duration_sec = round(max(35.0, min(55.0, len(word_tokens(full_narration)) / 2.55)), 2)
    script = {
        "title": title,
        "hook": hook,
        "body_beats": [*beats, payoff],
        "ending": closing,
        "cta": None,
        "full_narration": full_narration,
        "estimated_duration_sec": estimated_duration_sec,
        "key_facts": key_facts,
        "source_fact_ids": source_fact_ids,
        "claim_trace": claim_trace,
        "token_count": len(tokenize(full_narration)),
        "language": "pt-BR",
        "retention_map": {},
        "visual_opening": {"source": "ready_script", "text": hook},
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.85,
            "repetition_score": 0.1,
            "ending_strength_score": 0.88,
            "ready_script": True,
            "fact_check_confirmed": fact_check_confirmed,
            "declared_hashtags": hashtags,
        },
        "prompt_version": "ready-script-v1",
    }
    fact_pack = _build_declared_fact_pack(raw_text, key_facts, source_fact_ids, fact_check_confirmed)
    return ReadyScript(raw_text=raw_text, fact_check_confirmed=fact_check_confirmed, script=script, fact_pack=fact_pack, hashtags=hashtags)


def _parse_labeled_text(raw_text: str) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw_text.replace("\r\n", "\n").split("\n"):
        match = _LABEL_PATTERN.match(line)
        if match:
            current = _LABELS[match.group(1).strip().lower()]
            fields.setdefault(current, [])
            value = match.group(2).strip()
            if value:
                fields[current].append(value)
            continue
        if current and line.strip():
            fields[current].append(line.strip())
    return {key: "\n".join(value).strip() for key, value in fields.items() if value}


def _split_beats(value: str) -> list[str]:
    beats = []
    for line in value.splitlines():
        cleaned = re.sub(r"^\s*[-*•\d.)]+\s*", "", line).strip()
        if cleaned:
            beats.append(_clean_sentence(cleaned))
    if not beats:
        beats = [_clean_sentence(part) for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]
    return [beat for beat in beats if beat]


def _parse_hashtags(value: str) -> list[str]:
    tags = []
    for token in re.split(r"[\s,]+", value):
        cleaned = token.strip()
        if not cleaned:
            continue
        cleaned = cleaned if cleaned.startswith("#") else f"#{cleaned}"
        cleaned = re.sub(r"[^\w#À-ÖØ-öø-ÿ-]", "", cleaned).lower()
        if len(cleaned) > 1:
            tags.append(cleaned)
    return list(dict.fromkeys(tags))[:5]


def _clean_title(value: str) -> str:
    return _normalize_visible_text(value).rstrip(".!?")


def _clean_sentence(value: str) -> str:
    return _normalize_visible_text(value).strip()


def _ensure_sentence(value: str) -> str:
    cleaned = value.strip()
    return cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}."


def _normalize_visible_text(value: str) -> str:
    text = value.replace("—", ", ").replace("–", ", ")
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:]){2,}", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _build_declared_fact_pack(raw_text: str, key_facts: list[str], source_fact_ids: list[str], fact_check_confirmed: bool) -> dict[str, Any]:
    if not fact_check_confirmed:
        return {
            "status": "user_review_required",
            "provider": "ready_script",
            "facts": [],
            "sources": [],
            "raw_text_hash_source": "ready_script",
        }
    facts = [
        {"fact_id": source_id, "claim": fact, "source_id": "USER_DECLARED_FACT_CHECK"}
        for fact, source_id in zip(key_facts, source_fact_ids, strict=False)
    ]
    return {
        "status": "verified",
        "provider": "user_declared_fact_check",
        "facts": facts,
        "sources": [
            {
                "source_id": "USER_DECLARED_FACT_CHECK",
                "title": "Confirmacao de factualidade do Roteiro Pronto",
                "url": None,
                "kind": "human_confirmation",
            }
        ],
        "editorial_rule": "Fatos declarados pelo autor do Roteiro Pronto; o app preserva o texto como fonte de verdade editorial.",
        "raw_text_hash_source": "ready_script",
    }
