from __future__ import annotations

from typing import Any


class ProviderFailure(RuntimeError):
    def __init__(self, provider: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.details = details or {}
