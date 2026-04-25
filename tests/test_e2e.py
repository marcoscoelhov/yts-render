from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient


os.environ.setdefault("YTS_DATA_DIR", str(Path("data-test").resolve()))
os.environ.setdefault("YTS_DATABASE_URL", f"sqlite:///{Path('data-test/test.db').resolve()}")
os.environ.setdefault("YTS_USE_MOCK_PROVIDERS", "true")

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Job, RenderOutput, SceneAsset, SubtitleTrack, TopicRegistry  # noqa: E402
from app.orchestrator import orchestrator  # noqa: E402
from app.providers import MockCreativeProvider  # noqa: E402


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


def setup_module() -> None:
    shutil.rmtree(Path(os.environ["YTS_DATA_DIR"]), ignore_errors=True)
    Path(os.environ["YTS_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    init_db()
    orchestrator.start_worker()


def teardown_module() -> None:
    orchestrator.stop_worker()


def test_full_pipeline_reaches_waiting_review() -> None:
    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"seed_theme": "polvos", "target_duration_sec": 35, "tone": "intrigante_direto", "cta_style": "none"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].split("/")[-1]
    wait_for_status(job_id, "waiting_review")
    with SessionLocal() as session:
        render = session.query(RenderOutput).filter_by(job_id=job_id).one()
        subtitles = session.query(SubtitleTrack).filter_by(job_id=job_id).one()
        selected_assets = session.query(SceneAsset).filter_by(job_id=job_id, selected=True).all()
        assert render.resolution == "1080x1920"
        assert 25_000 <= render.duration_ms <= 45_000
        assert subtitles.coverage_ratio >= 0.99
        assert len(selected_assets) >= 5
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Render" in detail.text
    assert "Audio &amp; Subtitles" in detail.text


def test_repetition_guard_generates_distinct_approved_topic() -> None:
    client = TestClient(app)
    first = client.post("/jobs", data={"seed_theme": "polvos", "target_duration_sec": 35}, follow_redirects=False)
    first_job_id = first.headers["location"].split("/")[-1]
    wait_for_status(first_job_id, "waiting_review")
    client.post(f"/jobs/{first_job_id}/review", data={"action": "approve", "reason_codes": ""}, follow_redirects=False)
    second = client.post("/jobs", data={"seed_theme": "polvos", "target_duration_sec": 35}, follow_redirects=False)
    second_job_id = second.headers["location"].split("/")[-1]
    wait_for_status(second_job_id, "waiting_review")
    with SessionLocal() as session:
        rows = session.query(TopicRegistry).filter(TopicRegistry.job_id.in_([first_job_id, second_job_id])).all()
        assert len(rows) == 2
        assert rows[0].hook != rows[1].hook or rows[0].canonical_topic == rows[1].canonical_topic


def test_retry_action_creates_new_job() -> None:
    client = TestClient(app)
    response = client.post("/jobs", data={"seed_theme": "vulcoes", "target_duration_sec": 35}, follow_redirects=False)
    job_id = response.headers["location"].split("/")[-1]
    wait_for_status(job_id, "waiting_review")
    retry = client.post(
        f"/jobs/{job_id}/review",
        data={"action": "retry_from_step", "retry_step": "render", "reason_codes": "render_issue"},
        follow_redirects=False,
    )
    assert retry.status_code == 303
    new_job_id = retry.headers["location"].split("/")[-1]
    assert new_job_id != job_id


def test_scene_timings_fall_back_to_token_boundaries() -> None:
    scenes = [
        {"scene_id": "scene-1", "token_start": 0, "token_end": 9},
        {"scene_id": "scene-2", "token_start": 10, "token_end": 19},
        {"scene_id": "scene-3", "token_start": 20, "token_end": 29},
    ]
    normalized = orchestrator._normalize_scene_timings(scenes, 30_000)
    assert [scene["actual_start_ms"] for scene in normalized] == [0, 10_000, 20_000]
    assert [scene["actual_end_ms"] for scene in normalized] == [10_000, 20_000, 30_000]


def test_mock_scene_planner_uses_canonical_topic_as_subject() -> None:
    provider = MockCreativeProvider()
    scenes = provider.plan_scenes(
        {
            "canonical_topic": "buracos negros",
            "title": "O que torna buracos negros tao estranhos pelo detalhe que quase ninguem nota",
            "full_narration": "Buracos negros distorcem luz e tempo quando o contexto certo entra em cena.",
            "estimated_duration_sec": 35,
        },
        6,
    )
    assert scenes[0]["primary_subject"] == "buracos negros"
    assert scenes[0]["topic_hint"] == "buracos negros"
    assert scenes[0]["fallback_queries"][0] == "buracos negros"
