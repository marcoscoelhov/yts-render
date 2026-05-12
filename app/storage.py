from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any

from app.config import get_settings
from app.utils import ensure_dir, file_uri, stable_hash, write_json, write_text


@dataclass
class StoredArtifact:
    uri: str
    content_hash: str
    size_bytes: int


class StorageManager:
    def __init__(self) -> None:
        self.settings = get_settings()

    def job_dir(self, job_id: str, *, create: bool = True) -> Path:
        path = self.settings.artifacts_dir / job_id
        if create:
            return ensure_dir(path)
        return path

    def persist_json(self, job_id: str, relative_path: str, payload: dict[str, Any]) -> StoredArtifact:
        path = self.job_dir(job_id) / relative_path
        write_json(path, payload)
        raw = path.read_bytes()
        return StoredArtifact(uri=file_uri(path), content_hash=stable_hash(raw), size_bytes=len(raw))

    def persist_text(self, job_id: str, relative_path: str, content: str) -> StoredArtifact:
        path = self.job_dir(job_id) / relative_path
        write_text(path, content)
        raw = path.read_bytes()
        return StoredArtifact(uri=file_uri(path), content_hash=stable_hash(raw), size_bytes=len(raw))

    def persist_bytes(self, job_id: str, relative_path: str, data: bytes) -> StoredArtifact:
        path = self.job_dir(job_id) / relative_path
        ensure_dir(path.parent)
        path.write_bytes(data)
        return StoredArtifact(uri=file_uri(path), content_hash=stable_hash(data), size_bytes=len(data))

    def remove_job_artifacts(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id, create=False), ignore_errors=True)
