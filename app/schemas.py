from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

SUPPORTED_NICHES = {"curiosidades"}
SUPPORTED_LANGUAGES = {"pt-BR"}


class TopicRequestCreate(BaseModel):
    seed_theme: str = Field(min_length=3)
    niche_id: str = "curiosidades"
    language: str = "pt-BR"
    target_duration_sec: int = 45
    tone: str = "intrigante_direto"
    cta_style: Literal["none", "soft"] = "none"
    notes: str | None = None
    requested_angle: str | None = None

    @field_validator("seed_theme")
    @classmethod
    def validate_seed_theme(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("seed_theme must have at least 3 non-space characters")
        return normalized

    @field_validator("target_duration_sec")
    @classmethod
    def validate_duration(cls, value: int) -> int:
        if not 35 <= value <= 55:
            raise ValueError("target_duration_sec must be between 35 and 55")
        return value

    @field_validator("niche_id")
    @classmethod
    def validate_niche_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in SUPPORTED_NICHES:
            raise ValueError("unsupported niche_id: only 'curiosidades' is currently supported")
        return normalized

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-")
        alias_map = {
            "pt-br": "pt-BR",
            "portuguese-br": "pt-BR",
            "ptbr": "pt-BR",
        }
        resolved = alias_map.get(normalized)
        if resolved not in SUPPORTED_LANGUAGES:
            raise ValueError("unsupported language: only 'pt-BR' is currently supported")
        return resolved


class ReviewActionPayload(BaseModel):
    reviewer_identity: str = "tailscale:local-reviewer"
    action: Literal["approve", "reject", "retry"]
    reason_codes: list[str] = Field(default_factory=list)
    notes: str | None = None


class PerformanceMetricPayload(BaseModel):
    source: str = "youtube_studio_manual"
    retention_percent: float | None = None
    viewed_vs_swiped_away_percent: float | None = None
    rewatch_rate: float | None = None
    likes: int | None = None
    shares: int | None = None
    comments: int | None = None
    rpm_usd: float | None = None
    monetization_status: str | None = None
    notes: str | None = None

    @field_validator("retention_percent", "viewed_vs_swiped_away_percent")
    @classmethod
    def validate_percent(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100:
            raise ValueError("percent metrics must be between 0 and 100")
        return value

    @field_validator("rewatch_rate", "rpm_usd")
    @classmethod
    def validate_non_negative_float(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("metric must be non-negative")
        return value

    @field_validator("likes", "shares", "comments")
    @classmethod
    def validate_non_negative_int(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("metric must be non-negative")
        return value


class PublicationSchedulePayload(BaseModel):
    scheduled_for_local: str
    timezone: str = "UTC"
    youtube_visibility: Literal["private", "unlisted", "public"] = "private"
    notes: str | None = None

    @field_validator("scheduled_for_local")
    @classmethod
    def validate_scheduled_for_local(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("scheduled_for_local is required")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("scheduled_for_local must be a valid datetime-local value") from exc
        if parsed.tzinfo is not None:
            raise ValueError("scheduled_for_local must not include timezone offset")
        return normalized

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = str(value or "").strip() or "UTC"
        try:
            ZoneInfo(normalized)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return normalized
