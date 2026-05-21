from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings


TIKTOK_VIDEO_PUBLISH_SCOPE = "video.publish"


class TikTokIntegrationError(RuntimeError):
    pass


@dataclass
class TikTokConnectionStatus:
    enabled: bool
    token_configured: bool
    ready: bool
    missing_items: list[str]


class TikTokPublisher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def connection_status(self) -> TikTokConnectionStatus:
        missing_items: list[str] = []
        if not self.settings.tiktok_auto_publish_enabled:
            missing_items.append("YTS_TIKTOK_AUTO_PUBLISH_ENABLED=false")
        if not self.settings.tiktok_access_token:
            missing_items.append("YTS_TIKTOK_ACCESS_TOKEN ausente")
        return TikTokConnectionStatus(
            enabled=bool(self.settings.tiktok_auto_publish_enabled),
            token_configured=bool(self.settings.tiktok_access_token),
            ready=not missing_items,
            missing_items=missing_items,
        )

    def query_creator_info(self) -> dict[str, Any]:
        return self._post_json("/v2/post/publish/creator_info/query/", {})

    def direct_post_video(
        self,
        *,
        video_path: Path,
        title: str,
        privacy_level: str,
        is_aigc: bool,
        disable_comment: bool,
        disable_duet: bool,
        disable_stitch: bool,
    ) -> dict[str, Any]:
        if not video_path.exists():
            raise TikTokIntegrationError(f"Arquivo de video nao encontrado: {video_path}")
        creator_info = self.query_creator_info()
        options = set(creator_info.get("privacy_level_options") or [])
        if privacy_level not in options:
            raise TikTokIntegrationError(f"privacy_level {privacy_level} nao esta disponivel para a conta TikTok conectada")
        size = video_path.stat().st_size
        init_payload = self._post_json(
            "/v2/post/publish/video/init/",
            {
                "post_info": {
                    "title": title[:2200],
                    "privacy_level": privacy_level,
                    "disable_duet": disable_duet,
                    "disable_comment": disable_comment,
                    "disable_stitch": disable_stitch,
                    "is_aigc": is_aigc,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                },
            },
        )
        upload_url = str(init_payload.get("upload_url") or "").strip()
        publish_id = str(init_payload.get("publish_id") or "").strip()
        if not upload_url or not publish_id:
            raise TikTokIntegrationError("TikTok nao retornou upload_url/publish_id")
        self._upload_file(upload_url, video_path, size)
        return {
            "publish_id": publish_id,
            "status": "processing",
            "creator": creator_info,
            "privacy_level": privacy_level,
        }

    def fetch_post_status(self, publish_id: str) -> dict[str, Any]:
        normalized = str(publish_id or "").strip()
        if not normalized:
            raise TikTokIntegrationError("publish_id do TikTok ausente")
        return self._post_json("/v2/post/publish/status/fetch/", {"publish_id": normalized})

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(self.settings.tiktok_access_token or "").strip()
        if not token:
            raise TikTokIntegrationError("Token do TikTok nao configurado")
        url = f"{self.settings.tiktok_base_url.rstrip('/')}{path}"
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        raw = response.json()
        error = dict(raw.get("error") or {})
        code = str(error.get("code") or "")
        if code and code != "ok":
            message = str(error.get("message") or code)
            raise TikTokIntegrationError(f"TikTok API {code}: {message}")
        return dict(raw.get("data") or {})

    def _upload_file(self, upload_url: str, video_path: Path, size: int) -> None:
        with video_path.open("rb") as fh:
            response = httpx.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                content=fh.read(),
                timeout=180,
            )
        response.raise_for_status()
