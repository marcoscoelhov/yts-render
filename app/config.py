from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YTS_", env_file=".env", extra="ignore")

    app_name: str = "YTS Render"
    app_url: str = "http://127.0.0.1:8080"
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    hub_auth_token: str | None = None

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/yts_render.db"
    sqlite_busy_timeout_ms: int = 30_000
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"
    schema_version: str = "1.0.0"

    niche_id: str = "curiosidades"
    language: str = "pt-BR"
    target_duration_sec: int = 35
    scene_target_count: int = 6

    use_mock_providers: bool = False
    strict_minimax_validation: bool = False
    llm_primary_provider: str = "minimax"
    llm_fallback_provider: str = "deepseek"
    llm_repair_provider: str = "qwen"
    llm_scene_provider: str = "qwen"
    llm_enable_fallback: bool = True
    llm_script_draft_provider: str = "deepseek"
    llm_script_repair_attempts: int = 1
    llm_topic_repair_attempts: int = 2
    llm_topic_timeout_sec: float = 45.0
    llm_script_draft_timeout_sec: float = 45.0
    minimax_script_timeout_sec: float = 150.0
    llm_publish_audit_timeout_sec: float = 45.0
    real_run_allow_mock_fallback: bool = False
    scene_prompt_gate_enabled: bool = True
    asset_semantic_threshold: float = 0.80
    asset_total_threshold: float = 0.75
    render_min_bitrate: int = 250_000
    asset_generation_timeout_sec: float = 75.0
    asset_generation_regeneration_rounds: int = 2
    asset_generation_parallelism: int = 3
    background_music_enabled: bool = True
    background_music_gain_db: float = -20.0
    sound_design_enabled: bool = False
    sound_design_gain_db: float = -18.0
    youtube_publish_mode: str = "manual"
    youtube_api_enabled: bool = False
    youtube_channel_id: str | None = None
    minimax_commercial_rights_confirmed: bool = False
    edge_tts_commercial_rights_confirmed: bool = False
    minimax_rights_evidence_url: str | None = None
    edge_tts_rights_evidence_url: str | None = None
    allow_synthetic_visuals_for_monetization: bool = True
    conservative_synthetic_disclosure: bool = True
    channel_ai_generated_content: bool = True
    minimax_api_key: str | None = None
    minimax_text_api_key: str | None = None
    minimax_image_api_key: str | None = None
    minimax_music_api_key: str | None = None
    minimax_text_base_url: str = "https://api.minimax.io/v1"
    minimax_image_base_url: str = "https://api.minimax.io/v1/image_generation"
    minimax_music_base_url: str = "https://api.minimax.io/v1"
    minimax_text_timeout_sec: float = 150.0
    minimax_music_timeout_sec: float = 240.0
    minimax_scene_plan_timeout_sec: float = 90.0
    llm_scene_plan_timeout_sec: float = 45.0
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_sec: float = 90.0
    qwen_api_key: str | None = None
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.6-max-preview"
    qwen_timeout_sec: float = 90.0
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    tailscale_hostname: str = "shorts-hub"
    tailnet_domain: str = "example.ts.net"

    worker_poll_seconds: float = 1.0
    job_lease_seconds: int = 60

    @field_validator("target_duration_sec")
    @classmethod
    def validate_duration(cls, value: int) -> int:
        if not 25 <= value <= 45:
            raise ValueError("target_duration_sec must be between 25 and 45")
        return value

    @field_validator("sqlite_busy_timeout_ms")
    @classmethod
    def validate_sqlite_busy_timeout_ms(cls, value: int) -> int:
        if not 0 <= value <= 300_000:
            raise ValueError("sqlite_busy_timeout_ms must be between 0 and 300000")
        return value

    @field_validator("sqlite_journal_mode")
    @classmethod
    def validate_sqlite_journal_mode(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
        if normalized not in allowed:
            raise ValueError("sqlite_journal_mode must be a valid SQLite journal mode")
        return normalized

    @field_validator("sqlite_synchronous")
    @classmethod
    def validate_sqlite_synchronous(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"OFF", "NORMAL", "FULL", "EXTRA"}
        if normalized not in allowed:
            raise ValueError("sqlite_synchronous must be a valid SQLite synchronous mode")
        return normalized

    @field_validator(
        "llm_topic_timeout_sec",
        "llm_script_draft_timeout_sec",
        "minimax_script_timeout_sec",
        "llm_publish_audit_timeout_sec",
        "asset_generation_timeout_sec",
        "minimax_text_timeout_sec",
        "minimax_music_timeout_sec",
        "minimax_scene_plan_timeout_sec",
        "llm_scene_plan_timeout_sec",
        "deepseek_timeout_sec",
        "qwen_timeout_sec",
    )
    @classmethod
    def validate_positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout values must be positive")
        return value

    @field_validator("asset_generation_parallelism")
    @classmethod
    def validate_asset_generation_parallelism(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("asset_generation_parallelism must be between 1 and 8")
        return value

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "templates"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"

    @property
    def resolved_minimax_text_api_key(self) -> str | None:
        return self.minimax_text_api_key or self.minimax_api_key

    @property
    def resolved_minimax_image_api_key(self) -> str | None:
        return self.minimax_image_api_key or self.minimax_api_key

    @property
    def resolved_minimax_music_api_key(self) -> str | None:
        return self.minimax_music_api_key or self.resolved_minimax_text_api_key

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return settings
