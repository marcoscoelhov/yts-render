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

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/yts_render.db"
    schema_version: str = "1.0.0"

    niche_id: str = "curiosidades"
    language: str = "pt-BR"
    target_duration_sec: int = 35
    scene_target_count: int = 6

    use_mock_providers: bool = False
    minimax_api_key: str | None = None
    minimax_text_base_url: str = "https://api.minimax.io/v1"
    minimax_image_base_url: str = "https://api.minimax.io/v1/image_generation"
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

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "templates"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return settings
