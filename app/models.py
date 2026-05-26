from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String, default="queued")
    current_step: Mapped[str | None] = mapped_column(String, nullable=True)
    niche_id: Mapped[str] = mapped_column(String, default="curiosidades")
    language: Mapped[str] = mapped_column(String, default="pt-BR")
    target_duration_sec: Mapped[int] = mapped_column(Integer, default=50)
    topic_request_id: Mapped[str] = mapped_column(String, unique=True)
    retry_of_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    review_state: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifact_index: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    topic_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TopicRequest(Base):
    __tablename__ = "topic_requests"

    topic_request_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    niche_id: Mapped[str] = mapped_column(String)
    seed_theme: Mapped[str] = mapped_column(String)
    language: Mapped[str] = mapped_column(String)
    target_duration_sec: Mapped[int] = mapped_column(Integer)
    tone: Mapped[str | None] = mapped_column(String, nullable=True)
    cta_style: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_angle: Mapped[str | None] = mapped_column(String, nullable=True)


class TopicPlan(Base):
    __tablename__ = "topic_plans"

    topic_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    canonical_topic: Mapped[str] = mapped_column(String)
    angle: Mapped[str] = mapped_column(String)
    hook_promise: Mapped[str] = mapped_column(String)
    entities: Mapped[list] = mapped_column(JSON)
    search_terms: Mapped[list] = mapped_column(JSON)
    title_candidates: Mapped[list] = mapped_column(JSON)
    quality_metrics: Mapped[dict] = mapped_column(JSON)


class Script(Base):
    __tablename__ = "scripts"

    script_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    title: Mapped[str] = mapped_column(String)
    hook: Mapped[str] = mapped_column(Text)
    body_beats: Mapped[list] = mapped_column(JSON)
    ending: Mapped[str] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(String, nullable=True)
    full_narration: Mapped[str] = mapped_column(Text)
    estimated_duration_sec: Mapped[float] = mapped_column(Float)
    key_facts: Mapped[list] = mapped_column(JSON)
    token_count: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String)
    qa_metrics: Mapped[dict] = mapped_column(JSON)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)


class ScenePlan(Base):
    __tablename__ = "scene_plans"

    scene_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    scene_count: Mapped[int] = mapped_column(Integer)
    scenes: Mapped[list] = mapped_column(JSON)


class SceneAsset(Base):
    __tablename__ = "scene_assets"

    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    scene_id: Mapped[str] = mapped_column(String, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    provider: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="image")
    uri: Mapped[str] = mapped_column(String)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    scores: Mapped[dict] = mapped_column(JSON)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    attribution: Mapped[str | None] = mapped_column(String, nullable=True)
    license_note: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)


class NarrationAsset(Base):
    __tablename__ = "narration_assets"

    narration_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    provider: Mapped[str] = mapped_column(String)
    voice: Mapped[str] = mapped_column(String)
    audio_uri: Mapped[str] = mapped_column(String)
    normalized_audio_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_subtitles_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    channels: Mapped[int] = mapped_column(Integer)
    loudness_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SubtitleTrack(Base):
    __tablename__ = "subtitle_tracks"

    subtitle_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    format: Mapped[str] = mapped_column(String)
    items: Mapped[list] = mapped_column(JSON)
    coverage_ratio: Mapped[float] = mapped_column(Float)
    p95_drift_ms: Mapped[int] = mapped_column(Integer)
    max_drift_ms: Mapped[int] = mapped_column(Integer)
    ass_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_srt_uri: Mapped[str | None] = mapped_column(String, nullable=True)


class BackgroundMusicAsset(Base):
    __tablename__ = "background_music_assets"

    music_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    provider: Mapped[str] = mapped_column(String)
    query: Mapped[str | None] = mapped_column(String, nullable=True)
    mood: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    attribution: Mapped[str | None] = mapped_column(String, nullable=True)
    license_note: Mapped[str | None] = mapped_column(String, nullable=True)
    audio_uri: Mapped[str] = mapped_column(String)
    mixed_audio_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    gain_db: Mapped[float] = mapped_column(Float)
    provider_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RenderOutput(Base):
    __tablename__ = "render_outputs"

    render_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    video_uri: Mapped[str] = mapped_column(String)
    poster_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    waveform_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    resolution: Mapped[str] = mapped_column(String)
    video_codec: Mapped[str] = mapped_column(String)
    audio_codec: Mapped[str] = mapped_column(String)
    filesize_bytes: Mapped[int] = mapped_column(Integer)
    ffmpeg_log_uri: Mapped[str] = mapped_column(String)


class ReviewRecord(Base):
    __tablename__ = "review_records"

    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewer_identity: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    reason_codes: Mapped[list] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_step: Mapped[str | None] = mapped_column(String, nullable=True)


class PublicationSchedule(Base):
    __tablename__ = "publication_schedules"

    schedule_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    scheduled_for_utc: Mapped[datetime] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    youtube_visibility: Mapped[str] = mapped_column(String, default="private")
    status: Mapped[str] = mapped_column(String, default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String, nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(String, nullable=True)


class ChannelPublication(Base):
    __tablename__ = "channel_publications"
    __table_args__ = (UniqueConstraint("job_id", "channel", name="uq_channel_publication_job_channel"),)

    publication_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    scheduled_for_utc: Mapped[datetime] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    status: Mapped[str] = mapped_column(String, default="scheduled", index=True)
    source: Mapped[str] = mapped_column(String, default="crosspost")
    privacy_level: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_url: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PerformanceMetric(Base):
    __tablename__ = "performance_metrics"

    metric_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String, default="youtube_studio_manual")
    retention_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    viewed_vs_swiped_away_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    rewatch_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpm_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monetization_status: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReadyScriptItem(Base):
    __tablename__ = "ready_script_items"

    script_item_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String, default="available", index=True)
    source: Mapped[str] = mapped_column(String, default="batch")
    title: Mapped[str] = mapped_column(String)
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_script: Mapped[dict] = mapped_column(JSON)
    hashtags: Mapped[list] = mapped_column(JSON)
    fact_check_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_job_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    last_skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class AutomationSetting(Base):
    __tablename__ = "automation_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OperationalSetting(Base):
    __tablename__ = "operational_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AutomationRun(Base):
    __tablename__ = "automation_runs"
    __table_args__ = (UniqueConstraint("local_date", name="uq_automation_run_local_date"),)

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    local_date: Mapped[str] = mapped_column(String, index=True)
    timezone: Mapped[str] = mapped_column(String, default="America/Sao_Paulo")
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_publish_date: Mapped[str | None] = mapped_column(String, nullable=True)
    target_publish_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    result_job_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    result_schedule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)


class AutomationAttempt(Base):
    __tablename__ = "automation_attempts"

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("automation_runs.run_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    attempt_number: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ready_script_item_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FallbackEvent(Base):
    __tablename__ = "fallback_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    step: Mapped[str] = mapped_column(String)
    reason_code: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    from_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    to_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class ErrorLog(Base):
    __tablename__ = "error_logs"

    error_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0")
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    step: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    error_code: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    recoverable: Mapped[bool] = mapped_column(Boolean)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stacktrace_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_snapshot_uri: Mapped[str | None] = mapped_column(String, nullable=True)


class StepExecution(Base):
    __tablename__ = "step_executions"
    __table_args__ = (
        UniqueConstraint("job_id", "step_name", "attempt", "input_hash", name="uq_step_execution"),
    )

    execution_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    step_name: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    input_hash: Mapped[str] = mapped_column(String, index=True)
    output_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TopicRegistry(Base):
    __tablename__ = "topic_registry"

    registry_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    canonical_topic: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    hook: Mapped[str] = mapped_column(Text)
    entities: Mapped[list] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
