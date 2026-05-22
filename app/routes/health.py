from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings


router = APIRouter()


@router.get("/healthz")
def healthcheck() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "bind": f"{settings.app_host}:{settings.app_port}",
        "tailnet_url": f"https://{settings.tailscale_hostname}.{settings.tailnet_domain}",
    }
