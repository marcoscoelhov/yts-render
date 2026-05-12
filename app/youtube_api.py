from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.utils import ensure_dir, new_id


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeIntegrationError(RuntimeError):
    pass


@dataclass
class YouTubeConnectionStatus:
    connected: bool
    client_configured: bool
    dependencies_available: bool
    missing_items: list[str]
    redirect_uri: str | None
    token_expires_at: str | None
    granted_scopes: list[str]
    connected_at: str | None


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.isoformat()


def _google_expiry_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


class YouTubePublisher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def connection_status(self, redirect_uri: str | None = None) -> YouTubeConnectionStatus:
        payload = self._read_json(self.settings.youtube_token_path)
        missing_items: list[str] = []
        client_configured = bool(self.settings.youtube_client_id and self.settings.youtube_client_secret)
        dependencies_available = self._google_dependencies_available()
        if not self.settings.youtube_api_enabled:
            missing_items.append("YTS_YOUTUBE_API_ENABLED=false")
        if self.settings.youtube_publish_mode != "api":
            missing_items.append("YTS_YOUTUBE_PUBLISH_MODE != api")
        if not client_configured:
            missing_items.append("YTS_YOUTUBE_CLIENT_ID/SECRET ausentes")
        if not dependencies_available:
            missing_items.append("Dependências Google OAuth/API ainda não instaladas no ambiente")
        if not payload:
            missing_items.append("Canal ainda não conectado por OAuth")
        return YouTubeConnectionStatus(
            connected=bool(payload),
            client_configured=client_configured,
            dependencies_available=dependencies_available,
            missing_items=missing_items,
            redirect_uri=redirect_uri or self.settings.youtube_oauth_redirect_uri,
            token_expires_at=payload.get("expiry") if payload else None,
            granted_scopes=list(payload.get("scopes") or []) if payload else [],
            connected_at=payload.get("connected_at") if payload else None,
        )

    def authorization_url(self, redirect_uri: str) -> str:
        flow = self._build_flow(redirect_uri, state=new_id())
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self._write_secret_json(
            self.settings.youtube_oauth_state_path,
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        return str(auth_url)

    def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        state_payload = self._read_json(self.settings.youtube_oauth_state_path)
        if not state_payload or state_payload.get("state") != state:
            raise YouTubeIntegrationError("OAuth state inválido ou expirado")
        redirect_uri = str(state_payload.get("redirect_uri") or "").strip()
        if not redirect_uri:
            raise YouTubeIntegrationError("redirect_uri do OAuth não encontrado")
        flow = self._build_flow(redirect_uri, state=state)
        flow.fetch_token(code=code)
        credentials = flow.credentials
        existing = self._read_json(self.settings.youtube_token_path)
        payload = self._credentials_to_payload(credentials)
        if not payload.get("refresh_token") and existing.get("refresh_token"):
            payload["refresh_token"] = existing["refresh_token"]
        payload["connected_at"] = existing.get("connected_at") or datetime.now(UTC).isoformat()
        payload["last_refreshed_at"] = datetime.now(UTC).isoformat()
        payload["redirect_uri"] = redirect_uri
        self._write_secret_json(self.settings.youtube_token_path, payload)
        self.settings.youtube_oauth_state_path.unlink(missing_ok=True)
        return payload

    def disconnect(self) -> None:
        self.settings.youtube_token_path.unlink(missing_ok=True)
        self.settings.youtube_oauth_state_path.unlink(missing_ok=True)

    def upload_video(
        self,
        *,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
        altered_or_synthetic: bool,
        category_id: str = "27",
    ) -> dict[str, Any]:
        if not video_path.exists():
            raise YouTubeIntegrationError(f"Arquivo de vídeo não encontrado: {video_path}")
        credentials = self._load_credentials(refresh=True)
        discovery, media_upload = self._google_upload_dependencies()
        service = discovery.build("youtube", "v3", credentials=credentials, cache_discovery=False)
        body: dict[str, Any] = {
            "snippet": {
                "title": title[:100],
                "description": description[:4900],
                "tags": list(dict.fromkeys(tags))[:15],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        if altered_or_synthetic:
            body["status"]["containsSyntheticMedia"] = True
        media = media_upload.MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
            notifySubscribers=self.settings.youtube_notify_subscribers,
        )
        response = None
        progress = 0
        while response is None:
            status, response = request.next_chunk()
            if status is not None:
                progress = int(status.progress() * 100)
        payload = self._serialize_response(response)
        payload["upload_progress_percent"] = progress or 100
        payload["youtube_url"] = self.watch_url(str(payload.get("id") or ""))
        return payload

    def watch_url(self, video_id: str) -> str | None:
        normalized = str(video_id or "").strip()
        if not normalized:
            return None
        return f"https://www.youtube.com/watch?v={normalized}"

    def _load_credentials(self, *, refresh: bool) -> Any:
        payload = self._read_json(self.settings.youtube_token_path)
        if not payload:
            raise YouTubeIntegrationError("Canal do YouTube ainda não conectado")
        credentials_module, transport_requests = self._google_auth_dependencies()
        credentials = credentials_module.Credentials(
            token=payload.get("token"),
            refresh_token=payload.get("refresh_token"),
            token_uri=payload.get("token_uri"),
            client_id=payload.get("client_id"),
            client_secret=payload.get("client_secret"),
            scopes=payload.get("scopes") or [YOUTUBE_UPLOAD_SCOPE],
        )
        expiry_raw = payload.get("expiry")
        parsed_expiry = _google_expiry_datetime(str(expiry_raw) if expiry_raw else None)
        if parsed_expiry is not None:
            credentials.expiry = parsed_expiry
        if refresh and credentials.expired and credentials.refresh_token:
            credentials.refresh(transport_requests.Request())
            updated = self._credentials_to_payload(credentials)
            updated["connected_at"] = payload.get("connected_at")
            updated["redirect_uri"] = payload.get("redirect_uri")
            updated["last_refreshed_at"] = datetime.now(UTC).isoformat()
            self._write_secret_json(self.settings.youtube_token_path, updated)
        return credentials

    def _build_flow(self, redirect_uri: str, state: str) -> Any:
        if not self.settings.youtube_client_id or not self.settings.youtube_client_secret:
            raise YouTubeIntegrationError("Credenciais OAuth do YouTube não configuradas no ambiente")
        flow_module = self._google_flow_dependency()
        client_config = {
            "web": {
                "client_id": self.settings.youtube_client_id,
                "client_secret": self.settings.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = flow_module.Flow.from_client_config(client_config, scopes=[YOUTUBE_UPLOAD_SCOPE], state=state)
        flow.redirect_uri = redirect_uri
        return flow

    def _credentials_to_payload(self, credentials: Any) -> dict[str, Any]:
        scopes = list(credentials.scopes or [YOUTUBE_UPLOAD_SCOPE])
        return {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": scopes,
            "expiry": _iso_or_none(credentials.expiry),
        }

    def _serialize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload))

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_secret_json(self, path: Path, payload: dict[str, Any]) -> None:
        ensure_dir(path.parent)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def _google_dependencies_available(self) -> bool:
        try:
            self._google_flow_dependency()
            self._google_auth_dependencies()
            self._google_upload_dependencies()
        except YouTubeIntegrationError:
            return False
        return True

    def _google_flow_dependency(self) -> Any:
        try:
            from google_auth_oauthlib import flow as google_flow  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise YouTubeIntegrationError("google-auth-oauthlib não está instalado") from exc
        return google_flow

    def _google_auth_dependencies(self) -> tuple[Any, Any]:
        try:
            from google.auth.transport import requests as google_requests  # type: ignore[import-not-found]
            from google.oauth2 import credentials as google_credentials  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise YouTubeIntegrationError("google-auth não está instalado") from exc
        return google_credentials, google_requests

    def _google_upload_dependencies(self) -> tuple[Any, Any]:
        try:
            from googleapiclient import discovery, http as media_upload  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise YouTubeIntegrationError("google-api-python-client não está instalado") from exc
        return discovery, media_upload
