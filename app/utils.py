from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def stable_hash(data: Any) -> str:
    if isinstance(data, bytes):
        payload = data
    elif isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)


def word_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


def sentence_split(text: str) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text.strip()) if item.strip()]
    return sentences or [text.strip()]


def avg_words_per_sentence(text: str) -> float:
    sentences = sentence_split(text)
    word_counts = [len(word_tokens(sentence)) for sentence in sentences if sentence]
    return sum(word_counts) / max(len(word_counts), 1)


def max_words_single_sentence(text: str) -> int:
    return max((len(word_tokens(sentence)) for sentence in sentence_split(text)), default=0)


def words_per_second(text: str, duration_sec: float) -> float:
    return len(word_tokens(text)) / max(duration_sec, 0.001)


def jaccard_bigrams(a: str, b: str) -> float:
    def bigrams(text: str) -> set[str]:
        tokens = word_tokens(text)
        return {" ".join(pair) for pair in zip(tokens, tokens[1:])} if len(tokens) > 1 else set(tokens)

    set_a = bigrams(a)
    set_b = bigrams(b)
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / max(len(set_a | set_b), 1)


def cosineish_similarity(a: str, b: str) -> float:
    tokens_a = word_tokens(a)
    tokens_b = word_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    freq_a: dict[str, int] = {}
    freq_b: dict[str, int] = {}
    for token in tokens_a:
        freq_a[token] = freq_a.get(token, 0) + 1
    for token in tokens_b:
        freq_b[token] = freq_b.get(token, 0) + 1
    shared = set(freq_a) & set(freq_b)
    dot = sum(freq_a[token] * freq_b[token] for token in shared)
    mag_a = math.sqrt(sum(value * value for value in freq_a.values()))
    mag_b = math.sqrt(sum(value * value for value in freq_b.values()))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def path_from_uri(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[7:])
    return Path(uri)


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ms_to_srt(ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def srt_to_ms(timestamp: str) -> int:
    hours, minutes, rest = timestamp.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1000
        + int(millis)
    )


def parse_srt(content: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        idx = int(lines[0])
        start, end = [part.strip() for part in lines[1].split("-->")]
        text = " ".join(lines[2:])
        items.append(
            {
                "idx": idx,
                "start_ms": srt_to_ms(start),
                "end_ms": srt_to_ms(end),
                "text": text,
            }
        )
    return items


def wrap_caption(text: str, max_chars: int = 42) -> str:
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars and len(lines) < 2:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) == 1 and len(current) > 22:
            lines.append(current)
            current = ""
            break
    if current:
        lines.append(current)
    return "\n".join(lines[:2])
