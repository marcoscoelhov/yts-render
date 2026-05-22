from __future__ import annotations

import audioop

import json

import math

import os

import shutil

import threading

import time

import wave

from datetime import UTC, datetime, timedelta

from pathlib import Path

from types import SimpleNamespace

import httpx

import pytest

from fastapi.testclient import TestClient

from pydantic import ValidationError

from sqlalchemy import select

os.environ.setdefault("YTS_DATA_DIR", str(Path("data-test").resolve()))

os.environ.setdefault("YTS_DATABASE_URL", f"sqlite:///{Path('data-test/test.db').resolve()}")

os.environ.setdefault("YTS_USE_MOCK_PROVIDERS", "true")

import app.main as main_module  # noqa: E402

import app.orchestrator as orchestrator_module  # noqa: E402

import app.pipelines.script_fact_pack as script_fact_pack_module  # noqa: E402

from app.automation import AutomationService  # noqa: E402

from app.compliance.review import build_human_review_checklist  # noqa: E402

from app.config import Settings  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402

from app.editorial.research_brief import audit_source_relevance  # noqa: E402

from app.editorial.retention import EDITORIAL_PROMPT_VERSION, build_retention_map  # noqa: E402

from app.editorial.repetition import build_channel_repetition_report  # noqa: E402

from app.main import app, artifact_url  # noqa: E402

from app.models import AutomationAttempt, AutomationRun, ReadyScriptItem, BackgroundMusicAsset, ChannelPublication, Job, NarrationAsset, OperationalSetting, PerformanceMetric, PublicationSchedule, RenderOutput, SceneAsset, Script, SubtitleTrack, TopicPlan, TopicRegistry, TopicRequest  # noqa: E402

from app.music_bank import import_minimax_music_artifacts, populate_builtin_music_bank  # noqa: E402

from app.orchestrator import JobOrchestrator, RecoverableStepError, StepDefinition, normalize_script_metrics, orchestrator  # noqa: E402

from app.pipelines.timeline import normalize_scene_timings  # noqa: E402

from app.providers import DeepSeekCreativeProvider, LLMProviderRegistry, LocalMusicBankProvider, LocalSpeechFallbackProvider, MiniMaxBackgroundMusicProvider, MinimaxCreativeProvider, MinimaxImageProvider, MockCreativeProvider, OpenAICreativeProvider, ProviderFailure, ResilientCreativeProvider, ResilientMusicProvider  # noqa: E402

from app.quality.asset_gate import AssetGate  # noqa: E402

from app.quality.background_music_gate import BackgroundMusicGate  # noqa: E402

from app.quality.render_gate import RenderGate  # noqa: E402

from app.quality.scene_gate import ScenePlanGate  # noqa: E402

from app.quality.script_gate import ScriptQualityGate  # noqa: E402

from app.quality.subtitle_gate import SubtitleGate  # noqa: E402

from app.utils import parse_srt, split_caption_chunks, utcnow, word_tokens, wrap_caption  # noqa: E402

from app.youtube_api import YouTubeConnectionStatus, YouTubePublisher  # noqa: E402

def isolate_youtube_settings(monkeypatch):
    monkeypatch.setattr(main_module.settings, "youtube_publish_mode", "manual")
    monkeypatch.setattr(main_module.settings, "youtube_api_enabled", False)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "manual")
    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", False)
    monkeypatch.setattr(main_module.settings, "tiktok_auto_publish_enabled", False)
    monkeypatch.setattr(orchestrator.settings, "tiktok_auto_publish_enabled", False)
    monkeypatch.setattr(main_module.settings, "tiktok_access_token", None)
    monkeypatch.setattr(orchestrator.settings, "tiktok_access_token", None)

def wait_for_status(job_id: str, expected: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job and job.status == expected:
                return
            if job and job.status == "failed":
                raise AssertionError(job.failure_reason)
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not reach {expected}")

def wait_for_any_status(job_id: str, expected: set[str], timeout: float = 90.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job and job.status in expected:
                return job.status
            if job and job.status == "failed":
                raise AssertionError(job.failure_reason)
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not reach any of {sorted(expected)}")

def setup_module() -> None:
    shutil.rmtree(Path(os.environ["YTS_DATA_DIR"]), ignore_errors=True)
    Path(os.environ["YTS_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    init_db()
    orchestrator.start_worker()

def teardown_module() -> None:
    orchestrator.stop_worker()

def _create_basic_job(
    session,
    *,
    job_id: str,
    status: str,
    current_step: str | None = None,
    seed_theme: str = "Polvos",
    updated_at: datetime | None = None,
    quality_summary: dict | None = None,
    artifact_index: dict | None = None,
    review_state: str | None = None,
) -> str:
    topic_request_id = f"{job_id}-request"
    timestamp = updated_at or utcnow()
    session.add(
        Job(
            job_id=job_id,
            schema_version="1.0.0",
            content_hash=f"{job_id}-hash",
            created_at=timestamp,
            updated_at=timestamp,
            status=status,
            current_step=current_step,
            niche_id="curiosidades",
            language="pt-BR",
            target_duration_sec=35,
            topic_request_id=topic_request_id,
            quality_summary=quality_summary or {},
            artifact_index=artifact_index or {},
            review_state=review_state,
        )
    )
    session.add(
        TopicRequest(
            topic_request_id=topic_request_id,
            job_id=job_id,
            schema_version="1.0.0",
            content_hash=f"{job_id}-request-hash",
            niche_id="curiosidades",
            seed_theme=seed_theme,
            language="pt-BR",
            target_duration_sec=35,
        )
    )
    return topic_request_id

def _write_job_artifact(job_id: str, relative_path: str, content: str = "artifact") -> Path:
    artifact_path = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(content, encoding="utf-8")
    return artifact_path

def _write_test_wave(path: Path, *, duration_ms: int = 1000, amplitude: int = 2400, freq_hz: float = 146.8) -> None:
    sample_rate = 24_000
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for idx in range(frame_count):
            sample = int(amplitude * math.sin(2 * math.pi * freq_hz * (idx / sample_rate)))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)

def _base_script(full_narration: str) -> dict[str, object]:
    return {
        "title": "Curiosidade científica em menos de um minuto",
        "hook": full_narration.split(".")[0] + ".",
        "body_beats": [full_narration],
        "ending": "No fim, essa curiosidade científica muda como você olha para o tema.",
        "cta": None,
        "full_narration": full_narration,
        "estimated_duration_sec": 45,
        "key_facts": [full_narration],
        "token_count": len(full_narration.split()),
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.92,
            "clarity_score": 0.9,
            "information_density_score": 0.85,
            "repetition_score": 0.1,
            "ending_strength_score": 0.85,
        },
    }

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name not in {"setup_module", "teardown_module", "isolate_youtube_settings"}
]
