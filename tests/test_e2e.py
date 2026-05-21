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
import app.pipelines.script_pipeline as script_pipeline_module  # noqa: E402
from app.automation import AutomationService  # noqa: E402
from app.compliance.review import build_human_review_checklist  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.editorial.retention import EDITORIAL_PROMPT_VERSION, build_retention_map  # noqa: E402
from app.editorial.repetition import build_channel_repetition_report  # noqa: E402
from app.main import app, artifact_url  # noqa: E402
from app.models import AutomationAttempt, AutomationRun, ReadyScriptItem, BackgroundMusicAsset, ChannelPublication, Job, NarrationAsset, OperationalSetting, PerformanceMetric, PublicationSchedule, RenderOutput, SceneAsset, Script, SubtitleTrack, TopicPlan, TopicRegistry, TopicRequest  # noqa: E402
from app.music_bank import import_minimax_music_artifacts, populate_builtin_music_bank  # noqa: E402
from app.orchestrator import JobOrchestrator, RecoverableStepError, StepDefinition, normalize_script_metrics, orchestrator  # noqa: E402
from app.providers import DeepSeekCreativeProvider, LLMProviderRegistry, LocalMusicBankProvider, LocalSpeechFallbackProvider, MiniMaxBackgroundMusicProvider, MinimaxCreativeProvider, MinimaxImageProvider, MockCreativeProvider, OpenAICreativeProvider, ProviderFailure, ResilientCreativeProvider, ResilientMusicProvider  # noqa: E402
from app.quality.asset_gate import AssetGate  # noqa: E402
from app.quality.background_music_gate import BackgroundMusicGate  # noqa: E402
from app.quality.render_gate import RenderGate  # noqa: E402
from app.quality.scene_gate import ScenePlanGate  # noqa: E402
from app.quality.script_gate import ScriptQualityGate  # noqa: E402
from app.quality.subtitle_gate import SubtitleGate  # noqa: E402
from app.utils import parse_srt, split_caption_chunks, utcnow, word_tokens, wrap_caption  # noqa: E402
from app.youtube_api import YouTubeConnectionStatus, YouTubePublisher  # noqa: E402


@pytest.fixture(autouse=True)
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


def test_job_progress_is_exposed_in_detail_and_api() -> None:
    job_id = "job-progress-running"
    with SessionLocal() as session:
        _create_basic_job(session, job_id=job_id, status="running", current_step="script")
        session.flush()
        job = session.get(Job, job_id)
        job.lease_owner = "test-progress"
        job.lease_expires_at = utcnow() + timedelta(minutes=10)
        session.commit()
    _write_job_artifact(
        job_id,
        "performance_timeline.json",
        json.dumps(
            {
                "job_id": job_id,
                "steps": [
                    {"step_name": "input_gate", "attempt": 1, "status": "succeeded", "duration_ms": 120},
                    {"step_name": "topic_plan", "attempt": 1, "status": "succeeded", "duration_ms": 340},
                    {"step_name": "script", "attempt": 1, "status": "running", "duration_ms": None},
                ],
            }
        ),
    )
    _write_job_artifact(
        job_id,
        "events.jsonl",
        json.dumps({"event_name": "script.started", "status": "succeeded"}, ensure_ascii=False) + "\n",
    )

    client = TestClient(app)
    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["progress"]["state"] == "running"
    assert payload["progress"]["percent"] > 0
    assert payload["progress"]["current_label"] == "Roteiro"
    assert payload["progress"]["steps"][0]["status"] == "completed"
    assert payload["progress"]["steps"][2]["status"] == "running"

    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Progresso do job" in detail.text
    assert "Roteiro" in detail.text
    assert "script.started" in detail.text

    hub = client.get("/")
    assert hub.status_code == 200
    assert "Progresso do job" in hub.text


def test_full_pipeline_reaches_monetization_review() -> None:
    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"seed_theme": "polvos", "target_duration_sec": 35, "tone": "intrigante_direto", "cta_style": "none"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].split("/")[-1]
    wait_for_any_status(job_id, {"monetization_review", "blocked_for_monetization"})
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        render = session.query(RenderOutput).filter_by(job_id=job_id).one()
        subtitles = session.query(SubtitleTrack).filter_by(job_id=job_id).one()
        background_music = session.query(BackgroundMusicAsset).filter_by(job_id=job_id).one()
        selected_assets = session.query(SceneAsset).filter_by(job_id=job_id, selected=True).all()
        assert render.resolution == "1080x1920"
        assert 25_000 <= render.duration_ms <= 45_000
        assert subtitles.coverage_ratio >= 0.99
        assert subtitles.p95_drift_ms >= 0
        assert subtitles.max_drift_ms >= subtitles.p95_drift_ms
        assert background_music.gain_db == -17.0
        assert Path(background_music.audio_uri.removeprefix("file://")).exists()
        assert Path(background_music.mixed_audio_uri.removeprefix("file://")).exists()
        assert len(selected_assets) >= 5
        assert job and job.quality_summary["render"]["render_gate_pass"] is True
        assert job.quality_summary["background_music"]["enabled"] is True
        assert job.quality_summary["background_music"]["background_music_gate_pass"] is True
        assert job.quality_summary["monetization"]["final_status"] == "monetization_review"
        assert job.artifact_index["input_gate"] == "input_gate.json"
        assert job.artifact_index["publish_package"] == "publish_package.json"
        assert job.artifact_index["monetization_report"] == "monetization_report.json"
        assert job.artifact_index["background_music"] == "audio/background_source.wav"
        assert job.artifact_index["background_music_quality"] == "background_music_quality_report.json"
        assert job.artifact_index["mixed_audio"] == "audio/mixed.wav"
        assert job.artifact_index["performance_timeline"] == "performance_timeline.json"
        assert job.artifact_index["subtitle_timing"] == "subtitle_timing_report.json"
        timeline_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / job_id / "performance_timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        assert any(step["step_name"] == "render" and step["duration_ms"] is not None for step in timeline["steps"])
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Aprovar job" in detail.text
    assert "Agendar no YouTube" in detail.text


def test_artifact_url_maps_file_uri_to_static_route() -> None:
    artifact_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / "job-1" / "render" / "final.mp4"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"video")
    assert artifact_url(artifact_path.as_uri()) == "/artifacts/job-1/render/final.mp4"


def test_hub_auth_token_protects_pages_and_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "hub_auth_token", "secret-token")
    client = TestClient(app)
    main_module.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    assert client.get("/").status_code == 401
    assert client.get("/", headers={"x-yts-hub-token": "secret-token"}).status_code == 200
    assert client.get("/", cookies={"yts_hub_token": "secret-token"}).status_code == 200
    assert client.get("/artifacts/missing.mp4").status_code == 401
    assert client.get("/artifacts/missing.mp4", cookies={"yts_hub_token": "secret-token"}).status_code == 404
    assert client.get("/artifacts/missing.mp4?access_token=secret-token").status_code == 401
    assert client.post("/jobs", data={"seed_theme": "polvos"}, cookies={"yts_hub_token": "secret-token"}).status_code == 401


def test_artifact_url_does_not_embed_hub_auth_token(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "hub_auth_token", "secret-token")
    artifact_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / "job-1" / "render" / "final.mp4"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"video")

    assert artifact_url(artifact_path.as_uri()) == "/artifacts/job-1/render/final.mp4"


def test_sqlite_engine_uses_busy_timeout_and_wal_pragmas() -> None:
    with engine.connect() as connection:
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert busy_timeout >= 30_000
    assert str(journal_mode).lower() == "wal"


def test_hub_create_job_sends_title_mode_tone_angle_and_seo_notes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_job(payload: dict[str, object]) -> str:
        captured.update(payload)
        return "job-title-mode"

    monkeypatch.setattr(main_module.orchestrator, "create_job", fake_create_job)
    monkeypatch.setattr(main_module, "_viral_prompt_template", lambda: "Use curiosidade forte e payoff claro.")
    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={
            "input_mode": "title",
            "seed_theme": "Por que polvos parecem alienigenas dos oceanos?",
            "target_duration_sec": 35,
            "tone": "mito_vs_realidade",
            "requested_angle": "comparacao inesperada",
            "custom_angle": "inteligencia biologica impossivel",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/job-title-mode"
    assert captured["tone"] == "mito_vs_realidade"
    assert captured["requested_angle"] == "inteligencia biologica impossivel"
    assert "input_mode=title" in str(captured["notes"])
    assert "copywriting viral" in str(captured["notes"])
    assert "SEO otimizado" in str(captured["notes"])
    assert "retencao e viralizacao" in str(captured["notes"])
    assert "Use curiosidade forte e payoff claro." in str(captured["notes"])


def test_hub_create_job_accepts_ready_script_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}
    ready_script = """Título: Venus: o planeta onde um dia dura mais que um ano
Hook: 243 dias para girar uma vez, mas só 225 para orbitar o Sol.
Loop: Como um planeta pode envelhecer antes de terminar o próprio dia?
Beats: Em Venus, o relógio não acompanha o calendário.
O planeta gira tão devagar que o Sol parece quase travado.
Enquanto isso, ele completa uma volta inteira ao redor do Sol.
Payoff: O dia venusiano é maior que o ano venusiano.
Fechamento: Em Venus, aniversário chega antes do pôr do sol."""

    def fake_create_job(payload: dict[str, object]) -> str:
        captured.update(payload)
        return "job-ready-script"

    monkeypatch.setattr(main_module.orchestrator, "create_job", fake_create_job)
    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={
            "input_mode": "script",
            "ready_script_text": ready_script,
            "ready_script_fact_check_confirmed": "true",
            "target_duration_sec": 35,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/job-ready-script"
    assert captured["seed_theme"] == "Venus: o planeta onde um dia dura mais que um ano"
    assert "input_mode=script" in str(captured["notes"])
    assert "[[YTS_READY_SCRIPT_BEGIN]]" in str(captured["notes"])
    assert "ready_script_fact_check_confirmed=true" in str(captured["notes"])


def test_hub_ready_script_mode_hides_tone_control() -> None:
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert '<div class="field" data-mode-section="theme title">\n          <label for="tone">Tom</label>' in response.text


def test_hub_create_job_rejects_ready_script_without_fact_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(main_module.orchestrator, "create_job", lambda _payload: "should-not-run")
    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"input_mode": "script", "ready_script_text": "Título: X"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert "ready_script_fact_check_confirmed" in response.text


def test_ready_script_batch_import_persists_available_items() -> None:
    client = TestClient(app)
    batch = """Título: Terremoto teste lote A
Hook: O chão abriu e a cidade mudou de forma.
Loop: Como um abalo vira uma crise maior?
Beats: A primeira ruptura derrubou estruturas.
O dano atingiu sistemas vitais da cidade.
Payoff: O impacto principal veio da sequência de falhas.
Fechamento: O tremor começou. A cidade sentiu por dias.
Hashtags: #curiosidades #shorts

Título: Terremoto teste lote B
Hook: A manhã começou com poeira e sirenes.
Loop: Por que uma cidade preparada ainda pode colapsar?
Beats: O impacto inicial afetou ruas e prédios.
Depois vieram falhas em cadeia.
Payoff: O desastre cresceu porque a infraestrutura perdeu defesa.
Fechamento: O chão mexeu uma vez. O resto caiu em sequência.
Hashtags: #curiosidades #shorts"""

    response = client.post(
        "/automation/ready-scripts/import",
        data={"ready_script_batch": batch, "fact_check_confirmed": "true", "return_to": "/calendar"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar?imported=2"
    with SessionLocal() as session:
        rows = session.scalars(
            select(ReadyScriptItem).where(ReadyScriptItem.title.in_(["Terremoto teste lote A", "Terremoto teste lote B"]))
        ).all()

    assert len(rows) == 2
    assert {row.status for row in rows} == {"available"}
    assert all(row.fact_check_confirmed for row in rows)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="ready-script-bank-modal"' in page.text
    assert "Terremoto teste lote A" in page.text
    assert "Terremoto teste lote B" in page.text
    assert "2 disponíveis" in page.text


def test_operational_settings_route_saves_allowlisted_overrides() -> None:
    client = TestClient(app)
    response = client.post(
        "/operations/settings",
        data={
            "return_to": "/",
            "llm_primary_provider": "deepseek",
            "llm_fallback_provider": "openai",
            "llm_script_draft_provider": "deepseek",
            "llm_repair_provider": "openai",
            "llm_scene_provider": "deepseek",
            "llm_enable_fallback": "true",
            "background_music_provider": "local_bank",
            "background_music_enabled": "true",
            "music_bank_auto_populate": "true",
            "youtube_publish_mode": "api",
            "youtube_api_enabled": "true",
            "automation_daily_timezone": "UTC",
            "automation_daily_run_time": "03:30",
            "automation_publish_time": "12:45",
            "automation_fill_window_days": "7",
            "automation_max_generation_attempts": "2",
            "automation_max_publish_attempts_per_job": "2",
            "automation_score_threshold": "0.9",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?settings_saved=1"
    assert main_module.settings.llm_primary_provider == "deepseek"
    assert main_module.settings.youtube_publish_mode == "api"
    assert main_module.settings.youtube_api_enabled is True
    assert main_module.settings.allow_music_api_fallback is False
    with SessionLocal() as session:
        saved_keys = set(session.scalars(select(OperationalSetting.key)).all())

    assert "llm_primary_provider" in saved_keys
    assert "youtube_api_enabled" in saved_keys
    assert "openai_api_key" not in saved_keys

    reset = client.post("/operations/settings", data={"return_to": "/", "action": "reset"}, follow_redirects=False)
    assert reset.status_code == 303


def test_operational_settings_context_separates_scene_planner_from_image_generator(monkeypatch) -> None:
    from app.operational_settings import build_operational_settings_context

    monkeypatch.setattr(main_module.settings, "use_mock_providers", False)
    context = build_operational_settings_context(main_module.settings)
    fields = {field["key"]: field for group in context["groups"] for field in group["fields"]}
    group_names = {group["name"] for group in context["groups"]}

    assert fields["llm_scene_provider"]["label"] == "Planejador de cenas (LLM)"
    assert "nao gera imagens" in fields["llm_scene_provider"]["description"]
    assert "Imagem" in group_names
    assert fields["image_generation_provider"]["label"] == "Gerador de imagens"
    assert fields["image_generation_provider"]["input_type"] == "readonly"
    assert fields["image_generation_provider"]["value"] == "MiniMax"


def test_operational_settings_rejects_secret_fields() -> None:
    from app.operational_settings import validate_operational_update

    with pytest.raises(ValueError, match="desconhecida"):
        validate_operational_update(main_module.settings, {"openai_api_key": "secret"})


def test_automation_preflight_fails_before_consuming_ready_script() -> None:
    service = AutomationService(orchestrator)
    service.set_automation_enabled(True)
    result = service.import_ready_script_batch(
        """Título: Preflight YouTube roteiro unico
Hook: Uma agenda automatica sem OAuth vira armadilha.
Loop: Por que o sistema precisa parar antes de gerar?
Beats: Sem canal conectado, o upload não tem destino real.
O roteiro ainda deve ficar no banco.
Payoff: O preflight evita consumir material sem publicar.
Fechamento: Primeiro conecta. Depois automatiza.
Hashtags: #curiosidades #shorts""",
        fact_check_confirmed=True,
        source="test-preflight",
    )
    assert result.imported == 1

    run_result = service.run_daily_cycle(force=True)

    assert run_result["status"] == "failed"
    assert "YTS_YOUTUBE_API_ENABLED=false" in str(run_result["error"])
    with SessionLocal() as session:
        item = session.scalar(select(ReadyScriptItem).where(ReadyScriptItem.title == "Preflight YouTube roteiro unico"))
        run = session.scalar(select(AutomationRun).where(AutomationRun.run_id == run_result["run_id"]))

    assert item is not None
    assert item.status == "available"
    assert run is not None
    assert run.attempts_used == 0


def test_autoapproval_score_blocks_high_repetition() -> None:
    service = AutomationService(orchestrator)
    job_id = "auto-score-high-repetition"
    topic_request_id = f"{job_id}-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="auto-score",
                status="ready_for_upload",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
                quality_summary={
                    "assets": {"asset_semantic_score_avg": 0.9},
                    "monetization": {"passed": True, "final_status": "ready_for_upload", "hard_blockers": [], "manual_required": []},
                },
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="auto-score-request",
                niche_id="curiosidades",
                seed_theme="Score alto com repeticao",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            Script(
                script_id=f"{job_id}-script",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="auto-score-script",
                title="Score alto com repetição",
                hook="Um vídeo pode passar nos scores e ainda repetir demais.",
                body_beats=["O bloqueio principal vem da similaridade narrativa."],
                ending="Score bom não salva uma história repetida.",
                cta=None,
                full_narration="Um vídeo pode passar nos scores e ainda repetir demais. O bloqueio principal vem da similaridade narrativa. Score bom não salva uma história repetida.",
                estimated_duration_sec=40,
                key_facts=[],
                token_count=24,
                language="pt-BR",
                qa_metrics={"hook_score": 0.9, "information_density_score": 0.9},
            )
        )
        session.commit()
    orchestrator.storage.persist_json(
        job_id,
        "monetization_report.json",
        {
            "passed": True,
            "channel_repetition_report": {"repetition_risk": "high", "max_similarity": 0.91},
            "metadata_review": {"requires_metadata_review": False},
            "fact_claims_report": {"requires_fact_review": False},
            "publish_readiness": {
                "minimax_audit": {
                    "factual_score": 0.9,
                    "retention_score": 0.9,
                    "metadata_score": 0.9,
                }
            },
        },
    )

    report = service.evaluate_autoapproval(job_id)

    assert report["eligible"] is False
    assert "high_narrative_similarity" in report["reasons"]
    assert report["score"] >= 0.82


def test_automation_cycle_autoapproves_and_schedules_publishable_job(monkeypatch) -> None:
    service = AutomationService(orchestrator)
    service.set_automation_enabled(True)
    job_id = "automation-cycle-success-job"
    target_day = datetime(2099, 6, 10).date()

    def fake_create_job(payload):
        with SessionLocal() as session:
            _create_basic_job(session, job_id=job_id, status="queued", seed_theme="Automacao sucesso")
            session.commit()
        return job_id

    def fake_process_job(created_job_id: str) -> str:
        assert created_job_id == job_id
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.status = "ready_for_upload"
            job.quality_summary = {
                "assets": {"asset_semantic_score_avg": 0.93},
                "monetization": {"passed": True, "final_status": "ready_for_upload", "hard_blockers": [], "manual_required": []},
            }
            session.commit()
        return "ready_for_upload"

    def fake_review_job(payload: dict, reviewed_job_id: str) -> None:
        assert reviewed_job_id == job_id
        assert payload["action"] == "approve"
        assert "automation_score_confirmed" in payload["reason_codes"]
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.status = "approved_for_publish"
            job.review_state = "approved"
            session.commit()

    def fake_schedule_publication(scheduled_job_id: str, payload: dict) -> None:
        assert scheduled_job_id == job_id
        assert payload["scheduled_for_local"] == "2099-06-10T11:00"
        assert payload["timezone"] == "America/Sao_Paulo"
        assert payload["youtube_visibility"] == "public"
        with SessionLocal() as session:
            session.add(
                PublicationSchedule(
                    schedule_id=f"{job_id}-schedule",
                    job_id=job_id,
                    schema_version="1.0.0",
                    content_hash=f"{job_id}-schedule",
                    scheduled_for_utc=datetime(2099, 6, 10, 14, 0, tzinfo=UTC),
                    timezone="America/Sao_Paulo",
                    youtube_visibility="public",
                    status="scheduled",
                    youtube_video_id="yt-auto-scheduled",
                    youtube_url="https://www.youtube.com/watch?v=yt-auto-scheduled",
                    notes=payload["notes"],
                )
            )
            session.commit()

    monkeypatch.setattr(service, "_youtube_preflight", lambda: {"passed": True, "missing_items": [], "connected": True})
    monkeypatch.setattr(service, "_first_vacant_day", lambda: target_day)
    monkeypatch.setattr(service, "_select_ready_script_item", lambda: None)
    monkeypatch.setattr(service, "_automatic_topic_payload", lambda: {"seed_theme": "Automacao sucesso"})
    monkeypatch.setattr(orchestrator, "create_job", fake_create_job)
    monkeypatch.setattr(orchestrator, "process_job", fake_process_job)
    monkeypatch.setattr(service, "evaluate_autoapproval", lambda created_job_id: {"eligible": True, "score": 0.93, "threshold": 0.82, "reasons": [], "components": {}})
    monkeypatch.setattr(orchestrator, "review_job", fake_review_job)
    monkeypatch.setattr(orchestrator, "schedule_publication", fake_schedule_publication)

    run_result = service.run_daily_cycle(force=True)

    assert run_result["status"] == "succeeded"
    assert run_result["result_job_id"] == job_id
    assert run_result["attempts_used"] == 1
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        attempt = session.scalar(select(AutomationAttempt).where(AutomationAttempt.job_id == job_id))

    assert job is not None
    assert job.status == "approved_for_publish"
    assert schedule.status == "scheduled"
    assert schedule.youtube_visibility == "public"
    assert schedule.youtube_video_id == "yt-auto-scheduled"
    assert attempt is not None
    assert attempt.status == "scheduled"
    assert attempt.score == 0.93


def test_ready_script_selection_clears_stale_similarity_skip() -> None:
    service = AutomationService(orchestrator)
    item_id = "ready-script-stale-skip"
    with SessionLocal() as session:
        session.add(
            ReadyScriptItem(
                script_item_id=item_id,
                schema_version="1.0.0",
                content_hash="ready-script-stale-skip",
                status="available",
                source="test",
                title="Roteiro selecionavel sem repeticao",
                raw_text="""Título: Roteiro selecionavel sem repeticao
Hook: Um teste limpo precisa poder sair do banco.
Loop: O estado antigo ainda deveria bloquear?
Beats:
- O roteiro está disponível.
- A similaridade atual é baixa.
Payoff: O skip antigo é limpo ao selecionar.
Fechamento: Estado velho não deve travar o cron.
Hashtags: #curiosidades #shorts""",
                parsed_script={
                    "title": "Roteiro selecionavel sem repeticao",
                    "hook": "Um teste limpo precisa poder sair do banco.",
                    "loop": "O estado antigo ainda deveria bloquear?",
                    "body_beats": ["O roteiro está disponível.", "A similaridade atual é baixa."],
                    "ending": "Estado velho não deve travar o cron.",
                    "full_narration": "Um teste limpo precisa poder sair do banco. O roteiro está disponível. A similaridade atual é baixa. Estado velho não deve travar o cron.",
                    "estimated_duration_sec": 32,
                },
                hashtags=["#curiosidades", "#shorts"],
                fact_check_confirmed=True,
                last_skip_reason="high_narrative_similarity",
                last_similarity_score=0.9,
            )
        )
        session.commit()

    def fake_repetition_report(_session, item):
        if item.script_item_id == item_id:
            return {"repetition_risk": "low", "max_similarity": 0.1}
        return {"repetition_risk": "high", "max_similarity": 0.99}

    service._ready_script_repetition_report = fake_repetition_report

    selected = service._select_ready_script_item()

    assert selected is not None
    assert selected.script_item_id == item_id
    with SessionLocal() as session:
        item = session.get(ReadyScriptItem, item_id)

    assert item is not None
    assert item.last_skip_reason is None
    assert item.last_similarity_score is None


def test_hub_prompt_panel_saves_and_resets_safe_template(monkeypatch, tmp_path: Path) -> None:
    prompt_path = tmp_path / "hub_settings.json"
    monkeypatch.setattr(main_module, "_hub_settings_path", lambda: prompt_path)
    monkeypatch.setattr(main_module, "_default_seed_theme", lambda: "abelhas")
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert "Prompt viral avançado" in page.text
    assert "Banco de roteiros" in page.text
    assert 'data-open-ready-script-bank' in page.text
    assert "/automation/ready-scripts/import" in page.text
    assert main_module.DEFAULT_VIRAL_PROMPT_TEMPLATE.splitlines()[0] in page.text

    custom_prompt = "Priorize gancho contraintuitivo, titulo SEO e payoff visual."
    save = client.post(
        "/hub/prompt",
        data={"viral_prompt_template": custom_prompt, "action": "save", "return_to": "/calendar"},
        follow_redirects=False,
    )
    assert save.status_code == 303
    assert save.headers["location"] == "/calendar"
    assert main_module._viral_prompt_template() == custom_prompt

    reset = client.post("/hub/prompt", data={"action": "reset", "return_to": "/jobs?status=queued"}, follow_redirects=False)
    assert reset.status_code == 303
    assert reset.headers["location"] == "/jobs?status=queued"
    assert main_module._viral_prompt_template() == main_module.DEFAULT_VIRAL_PROMPT_TEMPLATE


def test_redirect_back_appends_params_before_fragment() -> None:
    response = main_module._redirect_back("/#publication-hub", {"automation_error": "failed"})

    assert response.status_code == 303
    assert response.headers["location"] == "/?automation_error=failed#publication-hub"


def test_create_job_rejects_unsupported_niche() -> None:
    client = TestClient(app)

    response = client.post(
        "/jobs",
        data={"seed_theme": "polvos", "niche_id": "esportes", "target_duration_sec": 35},
        follow_redirects=False,
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("unsupported niche_id" in item["msg"] for item in detail)


def test_orchestrator_create_job_rejects_blank_seed_theme_after_normalization() -> None:
    try:
        orchestrator.create_job(
            {
                "seed_theme": "   ",
                "niche_id": "curiosidades",
                "language": "pt-BR",
                "target_duration_sec": 35,
                "tone": "intrigante_direto",
                "cta_style": "none",
                "notes": None,
                "requested_angle": None,
            }
        )
    except ValidationError as exc:
        assert "seed_theme must have at least 3 non-space characters" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_create_job_rejects_unsupported_language() -> None:
    client = TestClient(app)

    response = client.post(
        "/jobs",
        data={"seed_theme": "polvos", "language": "en-US", "target_duration_sec": 35},
        follow_redirects=False,
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("unsupported language" in item["msg"] for item in detail)


def test_orchestrator_create_job_rejects_unsupported_niche() -> None:
    try:
        orchestrator.create_job(
            {
                "seed_theme": "polvos",
                "niche_id": "esportes",
                "language": "pt-BR",
                "target_duration_sec": 35,
                "tone": "intrigante_direto",
                "cta_style": "none",
                "notes": None,
                "requested_angle": None,
            }
        )
    except ValidationError as exc:
        assert "unsupported niche_id" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_minimax_script_prompt_requires_pt_br_for_all_text_fields(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_json_completion(self, prompt: str) -> dict[str, object]:
        captured["prompt"] = prompt
        return {
            "title": "Titulo teste",
            "hook": "Gancho teste.",
            "body_beats": ["Fato teste."],
            "ending": "Final teste.",
            "cta": None,
            "full_narration": "Gancho teste. Fato teste. Final teste.",
            "estimated_duration_sec": 30,
            "key_facts": ["Fato em pt-BR."],
            "token_count": 10,
            "language": "pt-BR",
            "qa_metrics": {
                "hook_score": 0.9,
                "clarity_score": 0.9,
                "information_density_score": 0.9,
                "repetition_score": 0.1,
                "ending_strength_score": 0.9,
                "estimated_duration_sec": 30,
                "avg_words_per_sentence": 6,
                "max_words_single_sentence": 8,
                "words_per_second": 2.5,
                "script_gate_pass": True,
            },
            "prompt_version": "teste",
        }

    monkeypatch.setattr(MinimaxCreativeProvider, "_json_completion", fake_json_completion)
    provider = object.__new__(MinimaxCreativeProvider)
    provider.generate_script(
        {
            "canonical_topic": "animais reais",
            "angle": "fatos verificados",
            "title_candidates": ["Animais reais que parecem mentira"],
            "hub_notes": "FORMATO DE SAIDA: gere blocos com timing.",
        }
    )
    prompt = captured["prompt"]
    assert "todos os campos textuais do JSON devem estar em portugues do Brasil" in prompt
    assert "nao use chines" in prompt
    assert "key_facts deve ser uma lista em pt-BR" in prompt
    assert "claim_trace" in prompt
    assert "proibido usar os caracteres" in prompt
    assert "ignore esse formato e mantenha exatamente o JSON estrito" in prompt


def test_script_quality_gate_blocks_generic_hook_opening() -> None:
    script = {
        "title": "Polvos pensam com os braços",
        "hook": "Você sabia que os braços do polvo pensam sozinhos?",
        "full_narration": "Você sabia que os braços do polvo pensam sozinhos? Cada braço processa sinais e reage ao ambiente de forma independente. Isso torna o polvo um animal muito diferente do nosso corpo centralizado.",
        "estimated_duration_sec": 35,
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.9,
            "repetition_score": 0.2,
            "ending_strength_score": 0.9,
        },
    }
    result = ScriptQualityGate().validate(script, target_duration_sec=35)
    assert not result.passed
    assert "generic_hook_opening" in result.reasons


def test_script_quality_gate_blocks_mixed_language_markup_and_glued_words() -> None:
    script = {
        "title": "Polvo pensa com os braços",
        "hook": "Cada braço do polvo é ummini-cérebro independiente.",
        "body_beats": ["Sim, você ouviu right.", "Isso muda tudo.</prosody"],
        "ending": "Comenta se isso te surpreendeu.",
        "cta": None,
        "full_narration": (
            "Cada braço do polvo é ummini-cérebro independiente. "
            "Sim, você ouviu right. Isso muda tudo.</prosody"
        ),
        "estimated_duration_sec": 30,
        "key_facts": ["Dois terços dos neurônios ficam nos braços."],
        "token_count": 20,
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.8,
            "repetition_score": 0.1,
            "ending_strength_score": 0.8,
        },
    }

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "foreign_language_detected" in result.reasons
    assert "markup_or_ssml_leaked" in result.reasons
    assert "suspicious_glued_words" in result.reasons


def test_script_quality_gate_blocks_ai_punctuation_and_non_latin_text() -> None:
    script = {
        "title": "Polvo muda de cor rápido",
        "hook": "A pele do polvo parece um painel vivo.",
        "body_beats": ["O sinal passa pela pele — e aparece no corpo."],
        "ending": "Quando você vê de novo, a pele entrega a pista.",
        "cta": None,
        "full_narration": (
            "A pele do polvo parece um painel vivo. "
            "A unidade muda no. centro da pele. "
            "O polvo faz isso sem pedir指令ao cérebro."
        ),
        "estimated_duration_sec": 30,
        "key_facts": ["A pele do polvo muda de cor."],
        "token_count": 24,
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.8,
            "repetition_score": 0.1,
            "ending_strength_score": 0.8,
        },
    }

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "em_dash_or_en_dash_detected" in result.reasons
    assert "non_latin_text_detected" in result.reasons
    assert "broken_sentence_punctuation" in result.reasons


def test_script_quality_gate_requires_trace_for_risky_factual_claims() -> None:
    full_narration = (
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Quando você vê de novo, a primeira frase vira alerta."
    )
    script = {
        "title": "O cérebro durante o sono",
        "hook": "O cérebro apaga memórias enquanto você dorme.",
        "body_beats": [full_narration],
        "ending": "Quando você vê de novo, a primeira frase vira alerta.",
        "cta": None,
        "full_narration": full_narration,
        "estimated_duration_sec": 30,
        "key_facts": ["O cérebro apaga memórias durante o sono."],
        "token_count": 18,
        "language": "pt-BR",
        "source_fact_ids": [],
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.8,
            "repetition_score": 0.1,
            "ending_strength_score": 0.8,
        },
    }

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "factual_claim_trace_missing" in result.reasons


def test_script_quality_gate_does_not_accept_global_source_ids_as_claim_trace() -> None:
    full_narration = (
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Na segunda olhada, a primeira frase vira alerta."
    )
    script = {
        "title": "O cérebro durante o sono",
        "hook": "O cérebro apaga memórias enquanto você dorme.",
        "body_beats": [full_narration],
        "ending": "Na segunda olhada, a primeira frase vira alerta.",
        "cta": None,
        "full_narration": full_narration,
        "estimated_duration_sec": 30,
        "key_facts": ["O cérebro apaga memórias durante o sono."],
        "token_count": 18,
        "language": "pt-BR",
        "source_fact_ids": ["F1"],
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.8,
            "repetition_score": 0.1,
            "ending_strength_score": 0.8,
        },
    }

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "factual_claim_trace_missing" in result.reasons


def test_llm_registry_uses_mock_when_mock_providers_enabled() -> None:
    registry = LLMProviderRegistry()
    assert registry.primary_provider().provider_name == "mock"
    assert registry.fallback_provider().provider_name == "mock"
    assert registry.repair_provider().provider_name == "mock"
    assert registry.scene_provider().provider_name == "mock"


def test_llm_registry_does_not_mock_fallback_in_real_runs(monkeypatch) -> None:
    settings = SimpleNamespace(
        use_mock_providers=False,
        llm_fallback_provider="deepseek",
        deepseek_api_key=None,
        real_run_allow_mock_fallback=False,
    )
    monkeypatch.setattr("app.providers.get_settings", lambda: settings)

    registry = LLMProviderRegistry()

    assert registry.fallback_provider() is None


def test_deepseek_provider_uses_v4_flash_openai_compatible_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    settings = SimpleNamespace(
        deepseek_api_key="deepseek-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_timeout_sec=90,
    )

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "title": "A comida que pinta flamingos",
                                    "hook": "A pena rosa começa no prato.",
                                    "body_beats": ["Pigmentos da dieta podem influenciar a cor."],
                                    "ending": "No replay, a primeira frase já mostrava a tinta.",
                                    "cta": None,
                                    "full_narration": "A pena rosa começa no prato. Pigmentos da dieta podem influenciar a cor. No replay, a primeira frase já mostrava a tinta.",
                                    "estimated_duration_sec": 30,
                                    "key_facts": ["Pigmentos da dieta podem influenciar a cor."],
                                    "source_fact_ids": [],
                                    "token_count": 24,
                                    "language": "pt-BR",
                                    "retention_map": {},
                                    "visual_opening": {},
                                    "qa_metrics": {},
                                    "prompt_version": EDITORIAL_PROMPT_VERSION,
                                }
                            )
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.OpenAI", FakeOpenAI)

    provider = DeepSeekCreativeProvider()
    result = provider.repair_script({"title": "x"}, ["weak_loop_closure"], {"canonical_topic": "flamingos"})

    assert captured["client_kwargs"]["api_key"] == "deepseek-key"
    assert captured["client_kwargs"]["base_url"] == "https://api.deepseek.com"
    assert captured["model"] == "deepseek-v4-flash"
    assert result["qa_metrics"]["repair_provider"] == "deepseek"


def test_openai_provider_uses_responses_api_with_json_output(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "title": "Cafe mascara a fadiga",
                        "hook": "Cafe nao cria energia do nada.",
                        "body_beats": ["A cafeina atrasa a percepcao do cansaco."],
                        "ending": "Na segunda olhada, o primeiro aviso vira pista.",
                        "cta": None,
                        "full_narration": "Cafe nao cria energia do nada. A cafeina atrasa a percepcao do cansaco. Na segunda olhada, o primeiro aviso vira pista.",
                        "estimated_duration_sec": 35,
                        "key_facts": ["A cafeina atrasa a percepcao do cansaco."],
                        "source_fact_ids": ["F1"],
                        "claim_trace": [{"text": "A cafeina atrasa a percepcao do cansaco.", "source_fact_ids": ["F1"], "grounding": "fact_pack"}],
                        "token_count": 20,
                        "language": "pt-BR",
                        "retention_map": {},
                        "visual_opening": {},
                        "qa_metrics": {},
                        "prompt_version": EDITORIAL_PROMPT_VERSION,
                    }
                )
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setattr(
        "app.providers.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_timeout_sec=120,
        ),
    )
    monkeypatch.setattr("app.providers.OpenAI", FakeOpenAI)

    provider = OpenAICreativeProvider()
    result = provider.generate_script({"canonical_topic": "cafeina e sono", "title_candidates": ["Cafe mascara a fadiga"]})

    assert captured["client_kwargs"]["api_key"] == "openai-key"
    assert captured["model"] == "gpt-5.4"
    assert captured["text"] == {"format": {"type": "json_object"}}
    assert "meta editorial: retenção máxima, replay, compartilhamento orgânico e espanto genuíno" in str(captured["input"])
    assert "body_beats equivale aos Beats em escalada" in str(captured["input"])
    assert result["qa_metrics"]["source_provider"] == "openai"


def test_openai_provider_topic_prompt_uses_hub_viral_ruler(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "canonical_topic": "flamingos rosa",
                        "angle": "pigmento que muda a cor",
                        "hook_promise": "o prato muda a pena",
                        "title_candidates": ["Flamingos rosa: a comida muda a cor deles"],
                        "entities": ["flamingos", "pigmentos"],
                        "search_terms": ["flamingo carotenoids plumage"],
                        "quality_metrics": {},
                    }
                )
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setattr(
        "app.providers.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_timeout_sec=120,
        ),
    )
    monkeypatch.setattr("app.providers.OpenAI", FakeOpenAI)

    provider = OpenAICreativeProvider()
    result = provider.plan_topic("Por que os flamingos ficam rosa?", 1, [], None)

    assert captured["client_kwargs"]["api_key"] == "openai-key"
    assert captured["text"] == {"format": {"type": "json_object"}}
    assert "Crie pautas de curiosidades globais para YouTube Shorts em pt-BR." in str(captured["input"])
    assert "Loop: pergunta mental de tensão que só fecha no payoff" in str(captured["input"])
    assert result["quality_metrics"]["source_provider"] == "openai"


def test_llm_registry_uses_deepseek_for_repair_and_scene_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.providers.get_settings",
        lambda: SimpleNamespace(
            use_mock_providers=False,
            llm_primary_provider="minimax",
            llm_fallback_provider="deepseek",
            llm_script_draft_provider="deepseek",
            llm_repair_provider="deepseek",
            llm_scene_provider="deepseek",
            real_run_allow_mock_fallback=False,
            resolved_minimax_text_api_key="minimax-key",
            minimax_text_base_url="https://api.minimax.io/v1",
            minimax_text_timeout_sec=150,
            deepseek_api_key="deepseek-key",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-v4-flash",
            deepseek_timeout_sec=90,
        ),
    )

    registry = LLMProviderRegistry()

    assert registry.repair_provider().provider_name == "deepseek"
    assert registry.scene_provider().provider_name == "deepseek"


def test_llm_registry_supports_openai_primary_provider(monkeypatch) -> None:
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=lambda **_kwargs: None)

    monkeypatch.setattr(
        "app.providers.get_settings",
        lambda: SimpleNamespace(
            use_mock_providers=False,
            llm_primary_provider="openai",
            llm_fallback_provider="deepseek",
            llm_script_draft_provider="deepseek",
            llm_repair_provider="deepseek",
            llm_scene_provider="deepseek",
            real_run_allow_mock_fallback=False,
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_timeout_sec=120,
            deepseek_api_key="deepseek-key",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-v4-flash",
            deepseek_timeout_sec=90,
        ),
    )
    monkeypatch.setattr("app.providers.OpenAI", FakeOpenAI)

    registry = LLMProviderRegistry()

    assert registry.primary_provider().provider_name == "openai"


def test_scene_plan_gate_rejects_generic_prompt_without_no_text_constraint() -> None:
    result = ScenePlanGate().validate(
        [
            {
                "scene_id": "scene-1",
                "order": 1,
                "narration_text": "O polvo usa os braços para explorar o ambiente.",
                "token_start": 0,
                "token_end": 8,
                "primary_subject": "polvo",
                "image_prompt": "vertical cinematic scientific image",
            }
        ],
        expected_scene_count=1,
    )
    assert not result.passed
    assert any("missing_no_text_constraint" in reason for reason in result.reasons)


def test_asset_gate_rejects_low_semantic_scene_asset() -> None:
    result = AssetGate().validate_selected(
        [
            {
                "scene_id": "scene-1",
                "semantic_match": 0.45,
                "total_score": 0.5,
                "text_or_watermark_penalty": 0.0,
                "artifact_penalty": 0.0,
            }
        ]
    )
    assert not result.passed
    assert "scene-1:semantic_match_below_threshold" in result.reasons


def test_subtitle_gate_blocks_markup_leakage() -> None:
    result = SubtitleGate().validate(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "Texto bom </prosody"}],
        coverage_ratio=1.0,
    )
    assert not result.passed
    assert "1:markup_or_ssml_leaked" in result.reasons


def test_subtitle_gate_rejects_large_timing_drift() -> None:
    result = SubtitleGate().validate(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "Texto bom"}],
        coverage_ratio=1.0,
        p95_drift_ms=1200,
        max_drift_ms=2200,
    )
    assert not result.passed
    assert "p95_timing_drift_too_high" in result.reasons
    assert "max_timing_drift_too_high" in result.reasons


def test_estimate_subtitle_timing_drift_reports_boundary_changes() -> None:
    cues = [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "um dois tres quatro"}]
    items = [
        {"idx": "1.1", "start_ms": 0, "end_ms": 650, "text": "um dois tres", "token_start": 0, "token_end": 2},
        {"idx": "1.2", "start_ms": 650, "end_ms": 1000, "text": "quatro", "token_start": 3, "token_end": 3},
    ]

    report = orchestrator.asset_pipeline._estimate_subtitle_timing_drift(cues, items)

    assert report["timing_basis"] == "raw_srt_proportional_split"
    assert report["drift_item_count"] == 2
    assert report["p95_drift_ms"] > 0
    assert report["max_drift_ms"] >= report["p95_drift_ms"]


def test_background_music_gate_rejects_inaudible_bed(tmp_path: Path) -> None:
    narration_path = tmp_path / "narration.wav"
    music_path = tmp_path / "music.wav"
    mixed_path = tmp_path / "mixed.wav"

    def write_wave(path: Path, amplitude: int, freq_hz: float) -> None:
        sample_rate = 24_000
        frame_count = sample_rate
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for idx in range(frame_count):
                sample = int(amplitude * math.sin(2 * math.pi * freq_hz * (idx / sample_rate)))
                frames.extend(sample.to_bytes(2, "little", signed=True))
            wav_file.writeframes(frames)

    write_wave(narration_path, amplitude=4000, freq_hz=220.0)
    write_wave(music_path, amplitude=0, freq_hz=110.0)
    shutil.copyfile(narration_path, mixed_path)

    result = BackgroundMusicGate().validate(
        narration_path=narration_path,
        music_path=music_path,
        mixed_audio_path=mixed_path,
        expected_duration_ms=1000,
        gain_db=-17.0,
    )

    assert not result.passed
    assert "music_source_too_quiet" in result.reasons
    assert "music_bed_inaudible" in result.reasons


def test_render_gate_rejects_missing_file(tmp_path: Path) -> None:
    result = RenderGate().validate(tmp_path / "missing.mp4", expected_duration_ms=30_000)
    assert not result.passed
    assert "missing_render_file" in result.reasons


def test_minimax_scene_prompt_keeps_image_prompt_english_exception(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_json_completion(self, prompt: str) -> list[dict[str, object]]:
        captured["prompt"] = prompt
        return [
            {
                "scene_id": "scene-1",
                "order": 1,
                "narration_text": "Cena em pt-BR.",
                "token_start": 0,
                "token_end": 2,
                "estimated_duration_sec": 5,
                "visual_intent": "subject_closeup",
                "primary_subject": "animal real",
                "image_prompt": "vertical cinematic image of a real animal, no readable text anywhere",
                "fallback_queries": ["animal real"],
            }
        ]

    monkeypatch.setattr(MinimaxCreativeProvider, "_json_completion", fake_json_completion)
    provider = object.__new__(MinimaxCreativeProvider)
    provider.plan_scenes(
        {"title": "Teste", "full_narration": "Cena em pt-BR.", "estimated_duration_sec": 5},
        1,
    )
    prompt = captured["prompt"]
    assert "Todos os campos textuais devem estar em portugues do Brasil" in prompt
    assert "exceto image_prompt" in prompt
    assert "image_prompt MUST be written in English only" in prompt


def test_minimax_image_provider_prefers_text_key_before_dedicated_key(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_openai(**kwargs):
        captured["text_api_key"] = kwargs["api_key"]
        captured["text_base_url"] = kwargs["base_url"]
        return object()

    settings = SimpleNamespace(
        resolved_minimax_text_api_key="text-key",
        minimax_image_api_key="image-key",
        resolved_minimax_image_api_key="image-key",
        minimax_text_base_url="https://text.example/v1",
        minimax_image_base_url="https://image.example/v1/image_generation",
        minimax_text_timeout_sec=30,
    )

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.OpenAI", fake_openai)

    creative = MinimaxCreativeProvider()
    image = MinimaxImageProvider()

    assert creative.client is not None
    assert captured == {
        "text_api_key": "text-key",
        "text_base_url": "https://text.example/v1",
    }
    assert image.key == "text-key"
    assert image.primary_key == "text-key"
    assert image.dedicated_key == "image-key"
    assert image.url == "https://image.example/v1/image_generation"


def test_minimax_image_provider_falls_back_to_dedicated_key_on_quota(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(
        resolved_minimax_text_api_key="text-key",
        minimax_image_api_key="image-key",
        resolved_minimax_image_api_key="image-key",
        minimax_image_base_url="https://image.example/v1/image_generation",
    )
    request = httpx.Request("POST", settings.minimax_image_base_url)
    tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lCw3GQAAAABJRU5ErkJggg=="
    calls: list[str] = []

    def fake_post(url, headers, json, timeout):  # noqa: ANN001
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(429, request=request, text="quota exceeded")
        return httpx.Response(
            200,
            request=request,
            json={"base_resp": {"status_code": 0}, "data": {"image_base64": [tiny_png]}},
        )

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.httpx.post", fake_post)

    provider = MinimaxImageProvider()
    scene = {"job_id": "job-quota", "image_prompt": "vertical science image"}
    result = provider.generate(scene, tmp_path / "first.png")
    second = provider.generate(scene, tmp_path / "second.png")

    assert calls == [
        "Bearer text-key",
        "Bearer image-key",
        "Bearer image-key",
    ]
    assert result["provider_metadata"]["credential_role"] == "image_dedicated"
    assert result["provider_metadata"]["fallback_from_text_key"] is True
    assert result["provider_metadata"]["text_key_exhausted_for_job"] is True
    assert second["provider_metadata"]["credential_role"] == "image_dedicated"


def test_minimax_image_provider_does_not_use_dedicated_key_for_timeout(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(
        resolved_minimax_text_api_key="text-key",
        minimax_image_api_key="image-key",
        resolved_minimax_image_api_key="image-key",
        minimax_image_base_url="https://image.example/v1/image_generation",
    )
    calls: list[str] = []

    def fake_post(url, headers, json, timeout):  # noqa: ANN001
        calls.append(headers["Authorization"])
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.httpx.post", fake_post)
    monkeypatch.setattr("app.providers.time.sleep", lambda _: None)

    provider = MinimaxImageProvider()
    with pytest.raises(ProviderFailure, match="connection failed after 3 attempts"):
        provider.generate({"job_id": "job-timeout", "image_prompt": "vertical science image"}, tmp_path / "timeout.png")

    assert calls == ["Bearer text-key", "Bearer text-key", "Bearer text-key"]
    assert provider._primary_exhausted_for_job("job-timeout") is False


def test_script_metrics_normalize_zero_to_ten_provider_scores() -> None:
    metrics = normalize_script_metrics(
        {
            "hook_score": 9.2,
            "clarity_score": 8.8,
            "information_density_score": 8.5,
            "repetition_score": 2,
            "ending_strength_score": 8,
            "avg_words_per_sentence": 12.5,
        }
    )

    assert metrics["hook_score"] == 0.92
    assert metrics["information_density_score"] == 0.85
    assert metrics["repetition_score"] == 0.2
    assert metrics["ending_strength_score"] == 0.8
    assert metrics["avg_words_per_sentence"] == 12.5


def test_script_metrics_treat_repetition_one_as_low_provider_score() -> None:
    metrics = normalize_script_metrics({"repetition_score": 1})

    assert metrics["repetition_score"] == 0.1


def test_resilient_creative_provider_repair_script_falls_back_on_timeout() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(minimax_script_timeout_sec=0.01, llm_enable_fallback=True)
    provider.strict_minimax_validation = False

    class SlowPrimary:
        def repair_script(self, script, gate_reasons, topic_plan):
            time.sleep(0.05)
            return {"title": "nao deveria retornar"}

    class Fallback:
        def repair_script(self, script, gate_reasons, topic_plan):
            return {
                "title": "Roteiro conservador",
                "hook": "Polvos mudam de cor para confundir ameaças.",
                "body_beats": ["Eles usam camuflagem e tinta como defesa."],
                "ending": "Isso explica por que parecem alienígenas do mar.",
                "cta": None,
                "full_narration": "Polvos mudam de cor para confundir ameaças. Eles usam camuflagem e tinta como defesa. Isso explica por que parecem alienígenas do mar.",
                "estimated_duration_sec": 28,
                "key_facts": ["Polvos usam camuflagem e tinta como defesa."],
                "language": "pt-BR",
                "qa_metrics": {},
            }

    provider.primary = SlowPrimary()
    provider.fallback = Fallback()

    repaired = provider.repair_script({"title": "original"}, ["factual_risk_requires_conservative_rewrite"], {"canonical_topic": "polvos"})

    assert repaired["title"] == "Roteiro conservador"
    assert repaired["qa_metrics"]["fallback_used"] is True
    assert repaired["qa_metrics"]["fallback_stage"] == "script_repair_timeout"
    assert "timed out after 0.01s" in repaired["qa_metrics"]["fallback_reason"]


def test_resilient_creative_provider_uses_minimax_before_deepseek_fallback() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(
        minimax_script_timeout_sec=30,
        llm_script_draft_timeout_sec=0.5,
        llm_enable_fallback=True,
    )
    provider.strict_minimax_validation = False

    class Draft:
        provider_name = "deepseek"

        def generate_script(self, topic_plan):
            raise AssertionError("draft provider should not run before primary script generation")

    class Primary:
        provider_name = "minimax"

        def generate_script(self, topic_plan):
            return {
                "title": "Roteiro MiniMax",
                "hook": "O começo já entrega tensão.",
                "body_beats": ["A prova aparece sem enrolação."],
                "ending": "Na segunda olhada, o começo vira pista.",
                "cta": None,
                "full_narration": "O começo já entrega tensão. A prova aparece sem enrolação. Na segunda olhada, o começo vira pista.",
                "estimated_duration_sec": 28,
                "key_facts": [],
                "source_fact_ids": [],
                "token_count": 20,
                "language": "pt-BR",
                "qa_metrics": {"source_provider": "minimax"},
            }

    provider.script_draft_provider = Draft()
    provider.primary = Primary()
    provider.fallback = None

    script = provider.generate_script({"canonical_topic": "polvos"})

    assert script["qa_metrics"]["generation_provider_role"] == "primary"
    assert script["qa_metrics"]["generation_provider"] == "minimax"
    assert script["qa_metrics"]["script_generation_fallback_used"] is False


def test_resilient_creative_provider_falls_back_to_deepseek_after_minimax_failure() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(
        minimax_script_timeout_sec=30,
        llm_script_draft_timeout_sec=0.5,
        llm_enable_fallback=True,
    )
    provider.strict_minimax_validation = False

    class Primary:
        provider_name = "minimax"

        def generate_script(self, topic_plan):
            raise ProviderFailure("minimax_text", "minimax failed")

    class Fallback:
        provider_name = "deepseek"

        def generate_script(self, topic_plan):
            return {
                "title": "Roteiro fallback",
                "hook": "O começo já entrega tensão.",
                "body_beats": ["A prova aparece sem enrolação."],
                "ending": "Na segunda olhada, o começo vira pista.",
                "cta": None,
                "full_narration": "O começo já entrega tensão. A prova aparece sem enrolação. Na segunda olhada, o começo vira pista.",
                "estimated_duration_sec": 28,
                "key_facts": [],
                "source_fact_ids": [],
                "token_count": 20,
                "language": "pt-BR",
                "qa_metrics": {"source_provider": "deepseek"},
            }

    provider.script_draft_provider = None
    provider.primary = Primary()
    provider.fallback = Fallback()

    script = provider.generate_script({"canonical_topic": "polvos"})

    assert script["qa_metrics"]["generation_provider_role"] == "fallback"
    assert script["qa_metrics"]["generation_provider"] == "deepseek"
    assert script["qa_metrics"]["script_generation_fallback_used"] is True
    assert script["qa_metrics"]["script_generation_fallback_reasons"] == ["minimax failed"]


def test_resilient_creative_provider_topic_uses_role_timeout() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(llm_topic_timeout_sec=0.01, minimax_text_timeout_sec=30)
    provider.strict_minimax_validation = False

    class SlowPrimary:
        def plan_topic(self, *args, **kwargs):
            time.sleep(0.05)
            return {"quality_metrics": {}}

    class Fallback:
        def plan_topic(self, *args, **kwargs):
            return {
                "canonical_topic": "fallback",
                "angle": "rapido",
                "hook_promise": "gancho",
                "title_candidates": ["fallback"],
                "quality_metrics": {},
            }

    provider.primary = SlowPrimary()
    provider.fallback = Fallback()

    plan = provider.plan_topic("tema", 1, [], None)

    assert plan["canonical_topic"] == "fallback"
    assert plan["quality_metrics"]["fallback_used"] is True
    assert "timed out after 0.01s" in plan["quality_metrics"]["fallback_reason"]


def test_resilient_creative_provider_scene_uses_role_timeout() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(llm_scene_plan_timeout_sec=0.01, minimax_scene_plan_timeout_sec=30)
    provider.strict_minimax_validation = False

    class SlowPrimary:
        def plan_scenes(self, *args, **kwargs):
            time.sleep(0.05)
            return []

    class SceneFallback:
        def plan_scenes(self, *args, **kwargs):
            return [{"scene_id": "scene-1", "narration_text": "fallback"}]

    provider.primary = SlowPrimary()
    provider.fallback = SceneFallback()
    provider.scene_provider = SceneFallback()

    scenes = provider.plan_scenes({"full_narration": "x"}, 1)

    assert scenes[0]["scene_id"] == "scene-1"
    assert "timed out after 0.01s" in scenes[0]["provider_fallback_reason"]


def test_publish_audit_is_cached_by_input_hash(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", False)
    calls = {"count": 0}

    def audit_publish_package(payload):
        calls["count"] += 1
        return {"passed": True, "reasons": [], "provider": "test-auditor"}

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", audit_publish_package)
    job_id = "publish-audit-cache-test"
    script = {"title": "Titulo", "hook": "Hook", "ending": "Fim", "full_narration": "Texto", "key_facts": [], "source_fact_ids": []}
    fact_pack = {"status": "limited", "facts": []}
    tags = ["#shorts"]

    first = orchestrator.monetization_pipeline.provider_publish_audit(script, fact_pack, tags, job_id)
    second = orchestrator.monetization_pipeline.provider_publish_audit(script, fact_pack, tags, job_id)

    assert first["passed"] is True
    assert second["cache_hit"] is True
    assert calls["count"] == 1


def test_resilient_creative_provider_disables_repair_fallback_in_strict_minimax_mode() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(minimax_script_timeout_sec=0.01, llm_enable_fallback=True, strict_minimax_validation=True)
    provider.strict_minimax_validation = True
    provider.primary = None
    provider.fallback = MockCreativeProvider()

    assert provider.repair_script_with_fallback({"title": "x"}, ["fact_pack_source_ids_missing"], {"canonical_topic": "polvos"}) is None


def test_resilient_music_provider_requires_minimax_success_in_real_mode(monkeypatch) -> None:
    settings = SimpleNamespace(
        use_mock_providers=False,
        background_music_provider="minimax",
        allow_music_api_fallback=False,
        resolved_minimax_music_api_key="music-key",
        strict_minimax_validation=False,
    )

    class FailingMusicProvider:
        def select_track(self, *args, **kwargs):
            raise RuntimeError("minimax music unavailable")

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.MiniMaxBackgroundMusicProvider", lambda: FailingMusicProvider())

    provider = ResilientMusicProvider()

    try:
        provider.select_track({}, {}, Path("/tmp/out.wav"), 30_000)
    except ProviderFailure as exc:
        assert "background music selection failed" in str(exc)
        assert "minimax music unavailable" in str(exc)
    else:
        raise AssertionError("expected ProviderFailure")


def test_local_music_bank_provider_selects_approved_track_and_records_license(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    track_path = bank_dir / "tracks" / "science.wav"
    license_path = bank_dir / "licenses" / "science.txt"
    _write_test_wave(track_path, duration_ms=700, amplitude=2600, freq_hz=110.0)
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text("YouTube Audio Library license snapshot", encoding="utf-8")
    (bank_dir / "manifest.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "id": "blocked-track",
                        "path": "tracks/blocked.wav",
                        "title": "Blocked",
                        "moods": ["technology"],
                        "license": "Unknown",
                        "source_url": "https://example.com/blocked",
                        "approved_for_youtube": True,
                        "content_id_registered": True,
                    },
                    {
                        "id": "science-calm-01",
                        "path": "tracks/science.wav",
                        "title": "Science Calm",
                        "artist": "Audio Library",
                        "moods": ["technology", "documentary"],
                        "tags": ["cafeina", "curiosidades"],
                        "license": "YouTube Audio Library",
                        "source_url": "https://youtube.com/audiolibrary",
                        "license_file": "licenses/science.txt",
                        "approved_for_youtube": True,
                        "requires_attribution": False,
                        "content_id_registered": False,
                        "content_id_risk": "low",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.providers.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir))

    provider = LocalMusicBankProvider()
    output_path = tmp_path / "out" / "background.wav"
    result = provider.select_track(
        {"canonical_topic": "cafeína", "angle": "tecnologia do cérebro"},
        {"title": "Cafeína parece tecnologia", "hook": "Seu cérebro muda em minutos."},
        output_path,
        1500,
    )

    assert result["provider"] == "local_music_bank"
    assert result["license_note"] == "YouTube Audio Library"
    assert result["attribution"] is None
    assert result["provider_metadata"]["track_id"] == "science-calm-01"
    assert result["provider_metadata"]["license_file"] == str(license_path)
    assert result["provider_metadata"]["content_id_registered"] is False
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 24_000
        assert wav_file.getnchannels() == 1
        assert round(wav_file.getnframes() / wav_file.getframerate() * 1000) == 1500


def test_resilient_music_provider_uses_local_bank_before_minimax(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    track_path = bank_dir / "tracks" / "doc.wav"
    _write_test_wave(track_path)
    (bank_dir / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "id": "doc-01",
                    "path": "tracks/doc.wav",
                    "moods": ["documentary"],
                    "license": "YouTube Audio Library",
                    "source_url": "https://youtube.com/audiolibrary",
                    "approved_for_youtube": True,
                    "content_id_registered": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        use_mock_providers=False,
        background_music_provider="local_bank",
        allow_music_api_fallback=False,
        resolved_minimax_music_api_key="music-key",
        music_bank_dir=bank_dir,
    )
    monkeypatch.setattr("app.providers.get_settings", lambda: settings)

    def fail_if_minimax_is_used():
        raise AssertionError("MiniMax should not be used when local bank succeeds")

    monkeypatch.setattr("app.providers.MiniMaxBackgroundMusicProvider", fail_if_minimax_is_used)

    provider = ResilientMusicProvider()
    result = provider.select_track({"canonical_topic": "polvos"}, {"title": "Polvos", "hook": "Polvos somem."}, tmp_path / "music.wav", 1000)

    assert result["provider"] == "local_music_bank"
    assert result["provider_metadata"]["track_id"] == "doc-01"


def test_builtin_music_bank_population_creates_manifest_and_tracks(tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"

    result = populate_builtin_music_bank(bank_dir, duration_seconds=1)

    manifest_path = bank_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["track_count"] >= 8
    assert len(manifest["tracks"]) >= 8
    first_track = manifest["tracks"][0]
    assert first_track["approved_for_youtube"] is True
    assert first_track["content_id_registered"] is False
    assert first_track["license"] == "local_synthetic_project_owned"
    assert (bank_dir / first_track["path"]).exists()
    assert (bank_dir / first_track["license_file"]).exists()


def test_local_music_bank_provider_auto_populates_when_manifest_is_missing(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    settings = SimpleNamespace(music_bank_dir=bank_dir, music_bank_auto_populate=True)
    monkeypatch.setattr("app.providers.get_settings", lambda: settings)

    def fast_populate(target_bank_dir: Path, *args, **kwargs):
        return populate_builtin_music_bank(target_bank_dir, duration_seconds=1)

    monkeypatch.setattr("app.providers.populate_builtin_music_bank", fast_populate)

    provider = LocalMusicBankProvider()
    output_path = tmp_path / "music.wav"
    result = provider.select_track({"canonical_topic": "universo"}, {"title": "O universo", "hook": "Algo estranho acontece."}, output_path, 1000)

    assert result["provider"] == "local_music_bank"
    assert result["provider_metadata"]["track_id"].startswith("local-")
    assert (bank_dir / "manifest.json").exists()
    assert output_path.exists()


def test_import_minimax_music_artifacts_preserves_evidence_and_strips_signed_url(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    job_dir = artifacts_dir / "job-minimax-123456"
    audio_path = job_dir / "audio" / "background_source.wav"
    _write_test_wave(audio_path, duration_ms=1000)
    (job_dir / "background_music.json").write_text(
        json.dumps(
            {
                "provider": "minimax_music",
                "mood": "cinematic",
                "query": "universo curiosidade cinematic",
                "source_url": "https://example.com/music.wav?Signature=secret&OSSAccessKeyId=key",
                "audio_uri": audio_path.resolve().as_uri(),
                "license_note": "Generated with MiniMax music_generation API.",
                "provider_metadata": {
                    "trace_id": "trace-123",
                    "model": "music-2.6",
                    "instrumental": True,
                },
                "duration_ms": 1000,
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "background_music_quality_report.json").write_text(
        json.dumps(
            {
                "passed": True,
                "reasons": [],
                "metrics": {
                    "music_source": {
                        "duration_ms": 1000,
                        "rms_dbfs": -14.0,
                        "peak_dbfs": -3.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bank_dir = tmp_path / "music_bank"

    result = import_minimax_music_artifacts(artifacts_dir, bank_dir)

    assert result["imported_count"] == 1
    manifest = json.loads((bank_dir / "manifest.json").read_text(encoding="utf-8"))
    track = manifest["tracks"][0]
    assert track["bank_source"] == "minimax_artifact"
    assert track["quality_tier"] == "primary"
    assert track["source_job_id"] == "job-minimax-123456"
    assert track["trace_id"] == "trace-123"
    assert track["source_url"] == "https://example.com/music.wav"
    assert "Signature" not in json.dumps(track)
    assert (bank_dir / track["path"]).exists()
    assert (bank_dir / track["license_file"]).read_text(encoding="utf-8").find("trace-123") >= 0


def test_local_music_bank_provider_prefers_imported_minimax_over_synthetic(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    populate_builtin_music_bank(bank_dir, duration_seconds=1)
    artifacts_dir = tmp_path / "artifacts"
    job_dir = artifacts_dir / "job-minimax-preferred"
    audio_path = job_dir / "audio" / "background_source.wav"
    _write_test_wave(audio_path, duration_ms=1000, freq_hz=180.0)
    (job_dir / "background_music.json").write_text(
        json.dumps(
            {
                "provider": "minimax_music",
                "mood": "cinematic",
                "query": "tema sem palavra de mood",
                "audio_uri": audio_path.resolve().as_uri(),
                "license_note": "Generated with MiniMax music_generation API.",
                "provider_metadata": {"trace_id": "trace-preferred", "model": "music-2.6", "instrumental": True},
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "background_music_quality_report.json").write_text(json.dumps({"passed": True, "metrics": {"music_source": {"duration_ms": 1000}}}), encoding="utf-8")
    import_minimax_music_artifacts(artifacts_dir, bank_dir)
    monkeypatch.setattr("app.providers.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir, music_bank_auto_populate=False))

    provider = LocalMusicBankProvider()
    result = provider.select_track({"canonical_topic": "x"}, {"title": "x", "hook": "x"}, tmp_path / "selected.wav", 1000)

    assert result["provider_metadata"]["track_id"].startswith("minimax-")
    assert result["provider_metadata"]["track_id"] == "minimax-job-mini"


def test_resilient_music_provider_allows_mock_only_in_mock_mode(monkeypatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(use_mock_providers=True, resolved_minimax_music_api_key=None, strict_minimax_validation=False)
    monkeypatch.setattr("app.providers.get_settings", lambda: settings)

    provider = ResilientMusicProvider()
    result = provider.select_track({"canonical_topic": "polvos"}, {"title": "Polvos", "hook": "Polvos somem."}, tmp_path / "music.wav", 10_000)

    assert result["provider"] == "mock_music"
    assert result["provider_metadata"]["fallback_used"] is True


def test_mock_generate_script_grounds_source_fact_ids_when_fact_pack_is_verified() -> None:
    provider = MockCreativeProvider()

    script = provider.generate_script(
        {
            "canonical_topic": "polvos",
            "angle": "neuroanatomia real",
            "title_candidates": ["Polvos e seus cérebros distribuídos"],
            "fact_pack": {
                "status": "verified",
                "facts": [
                    {"fact_id": "F1", "claim": "Os polvos são moluscos marinhos da classe Cephalopoda.", "source_id": "S1"},
                    {"fact_id": "F2", "claim": "O polvo possui oito braços fortes com ventosas.", "source_id": "S1"},
                    {"fact_id": "F3", "claim": "O polvo pode mudar de cor e largar tinta como defesa.", "source_id": "S1"},
                ],
            },
        }
    )

    assert script["source_fact_ids"] == ["F1", "F2"]
    assert script["claim_trace"][0]["source_fact_ids"] == ["F1"]
    assert script["key_facts"][:2] == [
        "Os polvos são moluscos marinhos da classe Cephalopoda.",
        "O polvo possui oito braços fortes com ventosas.",
    ]


def test_fact_query_prioritizes_honey_concepts_over_ambiguous_mel() -> None:
    pipeline = object.__new__(script_pipeline_module.ScriptPipeline)
    request = SimpleNamespace(seed_theme="curiosidades")
    topic_plan = SimpleNamespace(
        canonical_topic="Durabilidade do mel",
        angle="Fato surpreendente",
        hook_promise="Descubra por que o mel nunca estraga",
        search_terms=["mel nunca estraga", "mel durabilidade", "mel conservação"],
        entities=["Mel", "Abelha", "Enzima glucose oxidase", "Peróxido de hidrogênio"],
        title_candidates=["Mel nunca estraga: o segredo da natureza"],
    )

    queries = []
    seen = set()
    for query in pipeline._fact_pack_queries(request, topic_plan):
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not pipeline._is_weak_fact_query(normalized):
            queries.append(normalized)
            seen.add(normalized.lower())
    queries.sort(key=pipeline._fact_query_priority)

    assert queries[0] != "mel"
    assert any("honey antimicrobial" in query for query in queries[:5])
    assert not pipeline._fact_result_is_relevant(
        "mel",
        "Natural TTS Synthesis by Conditioning Wavenet on MEL Spectrogram Predictions",
        "This paper describes Tacotron 2, a neural network architecture for speech synthesis.",
    )


def test_mock_generate_script_includes_retention_map_and_visual_opening() -> None:
    provider = MockCreativeProvider()

    script = provider.generate_script(
        {
            "canonical_topic": "polvos",
            "angle": "biologia curiosa",
            "title_candidates": ["Polvos parecem impossíveis pelo detalhe dos braços"],
            "retention_map": build_retention_map(32),
            "visual_opening": {"first_frame_goal": "mostrar braço do polvo reagindo antes da cabeça"},
            "editorial_prompt_version": EDITORIAL_PROMPT_VERSION,
        }
    )

    assert script["prompt_version"].endswith(EDITORIAL_PROMPT_VERSION)
    assert script["retention_map"]["segments"][0]["code"] == "visual_hook"
    assert script["visual_opening"]["first_frame_goal"]
    assert script["qa_metrics"]["editorial_prompt_version"] == EDITORIAL_PROMPT_VERSION


def test_mock_repair_script_uses_fact_pack_and_shortens_long_sentences() -> None:
    provider = MockCreativeProvider()
    repaired = provider.repair_script(
        {
            "title": "Polvos e seus cérebros",
            "hook": "Polvos escondem um detalhe biologico quase absurdo.",
            "full_narration": "Os polvos possuem uma organização neural muito distribuída e isso aparece de forma bem clara quando cada braço responde ao ambiente sem esperar uma ordem central.",
            "qa_metrics": {},
        },
        ["fact_pack_source_ids_missing", "avg_sentence_too_long", "sentence_too_long"],
        {
            "canonical_topic": "polvos",
            "fact_pack": {
                "status": "verified",
                "facts": [
                    {"fact_id": "F1", "claim": "Os polvos são moluscos marinhos da classe Cephalopoda.", "source_id": "S1"},
                    {"fact_id": "F2", "claim": "O polvo possui oito braços fortes com ventosas.", "source_id": "S1"},
                    {"fact_id": "F3", "claim": "O polvo pode mudar de cor e largar tinta como defesa.", "source_id": "S1"},
                ],
            },
        },
    )

    assert repaired["source_fact_ids"] == ["F1", "F2", "F3"]
    assert repaired["key_facts"] == [
        "Os polvos são moluscos marinhos da classe Cephalopoda.",
        "O polvo possui oito braços fortes com ventosas.",
        "O polvo pode mudar de cor e largar tinta como defesa.",
    ]
    assert max(len(word_tokens(sentence)) for sentence in repaired["full_narration"].split(". ") if sentence.strip()) <= 14


def test_run_step_cancels_job_when_shutdown_is_requested_during_retry(monkeypatch) -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    trigger = threading.Event()

    def failing_step(session, job, attempt):
        if attempt == 1:
            orchestrator.stop_event.set()
            trigger.set()
        raise RecoverableStepError("falha recuperavel")

    step = StepDefinition("script", 2, failing_step)
    monkeypatch.setattr(orchestrator, "_build_step_input", lambda session, job, step_name: {"job_id": job.job_id, "step": step_name, "attempt_marker": time.time_ns()})

    try:
        result = orchestrator._run_step(job_id, step)
    finally:
        orchestrator.stop_event.clear()

    assert trigger.is_set()
    assert result is False
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "cancelled"
        assert job.failure_reason == "script: worker shutdown requested during recoverable retry"


def test_hub_uses_trends_for_empty_theme_and_retention_duration_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_job(payload: dict[str, object]) -> str:
        captured.update(payload)
        return "job-defaults"

    monkeypatch.setattr(main_module, "_default_seed_theme", lambda: "abelhas")
    monkeypatch.setattr(
        main_module,
        "_trend_seed_theme",
        lambda niche_id: (
            "Por que flamingos estão em alta?",
            "Transformar tendência real em curiosidade verificável.",
            "trend_research=real_source\ntrend_source=google_trends_br",
            {"trend_research": "real_source", "source": "google_trends_br"},
        ),
    )
    monkeypatch.setattr(main_module.orchestrator, "create_job", fake_create_job)
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert 'name="niche_id" value="curiosidades"' in page.text
    assert 'name="seed_theme" value=""' in page.text
    assert "Vazio = pesquisar tendências reais" in page.text
    assert 'name="target_duration_sec" type="number" min="35" max="55" value="45"' in page.text

    response = client.post("/jobs", data={"seed_theme": "", "input_mode": "theme"}, follow_redirects=False)
    assert response.status_code == 303
    assert captured["seed_theme"] == "Por que flamingos estão em alta?"
    assert captured["requested_angle"] == "Transformar tendência real em curiosidade verificável."
    assert "trend_research=real_source" in str(captured["notes"])
    assert captured["niche_id"] == "curiosidades"
    assert captured["target_duration_sec"] == 45


def test_hub_jobs_table_supports_pagination_for_older_jobs() -> None:
    client = TestClient(app)
    base_time = utcnow() - timedelta(days=30)
    with SessionLocal() as session:
        for index in range(3):
            job_id = f"pagehub-job-{index}"
            topic_request_id = f"pagehub-topic-{index}"
            created_at = base_time + timedelta(minutes=index)
            session.add(
                Job(
                    job_id=job_id,
                    schema_version="1.0.0",
                    content_hash=f"hash-{job_id}",
                    created_at=created_at,
                    updated_at=created_at,
                    status="monetization_review",
                    current_step="publish_to_review_hub",
                    niche_id="curiosidades",
                    language="pt-BR",
                    target_duration_sec=35,
                    topic_request_id=topic_request_id,
                    artifact_index={},
                )
            )
            session.add(
                TopicRequest(
                    topic_request_id=topic_request_id,
                    job_id=job_id,
                    schema_version="1.0.0",
                    content_hash=f"hash-{topic_request_id}",
                    created_at=created_at,
                    niche_id="curiosidades",
                    seed_theme=f"pagehub tema {index}",
                    language="pt-BR",
                    target_duration_sec=35,
                )
            )
        session.commit()

    first_page = client.get("/jobs?search=pagehub&per_page=2&page=1")
    assert first_page.status_code == 200
    assert "pagehub-job-2" in first_page.text
    assert "pagehub-job-1" in first_page.text
    assert "pagehub-job-0" not in first_page.text
    assert "Página 1 de 2" in first_page.text
    assert "page=2&amp;per_page=2&amp;search=pagehub" in first_page.text

    second_page = client.get("/jobs?search=pagehub&per_page=2&page=2")
    assert second_page.status_code == 200
    assert "pagehub-job-0" in second_page.text
    assert "pagehub-job-2" not in second_page.text
    assert "Página 2 de 2" in second_page.text


def test_jobs_queue_uses_publication_schedule_for_operational_state() -> None:
    client = TestClient(app)
    unscheduled_job_id = "queue-unscheduled-approved"
    scheduled_job_id = "queue-scheduled-approved"
    with SessionLocal() as session:
        _create_basic_job(session, job_id=unscheduled_job_id, status="approved_for_publish", seed_theme="Job aprovado sem agenda")
        _create_basic_job(session, job_id=scheduled_job_id, status="approved_for_publish", seed_theme="Job programado real")
        session.add(
            PublicationSchedule(
                schedule_id=f"{scheduled_job_id}-schedule",
                job_id=scheduled_job_id,
                schema_version="1.0.0",
                content_hash=f"{scheduled_job_id}-schedule-hash",
                scheduled_for_utc=datetime(2099, 7, 1, 14, 0, tzinfo=UTC),
                timezone="America/Sao_Paulo",
                youtube_visibility="public",
                status="scheduled",
            )
        )
        session.commit()

    scheduled_response = client.get("/jobs?status=scheduled_publication")
    assert scheduled_response.status_code == 200
    assert "Job programado real" in scheduled_response.text
    assert "Job aprovado sem agenda" not in scheduled_response.text
    assert "Programado" in scheduled_response.text

    unscheduled_response = client.get("/jobs?status=unscheduled_approved")
    assert unscheduled_response.status_code == 200
    assert "Job aprovado sem agenda" in unscheduled_response.text
    assert "Job programado real" not in unscheduled_response.text
    assert "Aprovado sem agenda" in unscheduled_response.text


def test_job_detail_accepts_unique_short_job_prefix() -> None:
    client = TestClient(app)
    job_id = "prefix-open-job-123456789"
    topic_request_id = "prefix-open-topic-123456789"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="hash-prefix-open",
                status="script_quality_failed",
                current_step="script",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=35,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="hash-prefix-open-topic",
                niche_id="curiosidades",
                seed_theme="prefix open tema",
                language="pt-BR",
                target_duration_sec=35,
            )
        )
        session.commit()

    response = client.get("/jobs/prefix-open-job")

    assert response.status_code == 200
    assert job_id[:8] in response.text or "prefix open tema" in response.text


def test_default_seed_theme_avoids_recent_curiosidades_topics(monkeypatch) -> None:
    with SessionLocal() as session:
        for theme in main_module.HUB_RANDOM_THEME_POOL[:3]:
            session.add(
                TopicRequest(
                    topic_request_id=f"recent-{theme}",
                    job_id=f"job-recent-{theme}",
                    schema_version="1.0.0",
                    content_hash=theme,
                    niche_id="curiosidades",
                    seed_theme=theme,
                    language="pt-BR",
                    target_duration_sec=35,
                )
            )
        session.commit()

    monkeypatch.setattr(main_module.random, "choice", lambda candidates: candidates[0])
    assert main_module._default_seed_theme() == main_module.HUB_RANDOM_THEME_POOL[3]


def test_channel_repetition_report_flags_similar_recent_jobs() -> None:
    with SessionLocal() as session:
        previous = Job(
            job_id="job-repetition-previous",
            schema_version="1.0.0",
            content_hash="prev",
            status="approved_for_publish",
            niche_id="curiosidades",
            language="pt-BR",
            target_duration_sec=35,
            topic_request_id="topic-request-previous",
            topic_summary="Polvos e inteligência distribuída | surpresa científica",
        )
        current = Job(
            job_id="job-repetition-current",
            schema_version="1.0.0",
            content_hash="current",
            status="running",
            niche_id="curiosidades",
            language="pt-BR",
            target_duration_sec=35,
            topic_request_id="topic-request-current",
            topic_summary="Polvos e inteligência distribuída | surpresa científica",
        )
        session.add_all([previous, current])
        session.add(
            Script(
                    script_id="script-repetition-previous",
                    job_id=previous.job_id,
                    schema_version="1.0.0",
                    content_hash="script-prev",
                    title="Polvos pensam com os braços",
                    hook="O polvo não pensa só com a cabeça.",
                    body_beats=["Os braços processam sinais.", "O corpo reage antes da cabeça.", "Isso muda a leitura do animal."],
                ending="Isso muda como você olha para o animal.",
                cta=None,
                full_narration="O polvo não pensa só com a cabeça. Seus braços processam sinais.",
                estimated_duration_sec=30,
                key_facts=[],
                token_count=30,
                language="pt-BR",
                qa_metrics={},
            )
        )
        topic_plan = TopicPlan(
            topic_id="topic-repetition-current",
            job_id=current.job_id,
            schema_version="1.0.0",
            content_hash="topic-current",
            canonical_topic="Polvos e inteligência distribuída",
            angle="surpresa científica",
            hook_promise="o polvo não pensa só com a cabeça",
            entities=["polvo"],
            search_terms=["polvo inteligência"],
            title_candidates=["Polvos pensam com os braços"],
            quality_metrics={},
        )
        script = Script(
            script_id="script-repetition-current",
            job_id=current.job_id,
                schema_version="1.0.0",
                content_hash="script-current",
                title="Polvos pensam com os braços",
                hook="O polvo não pensa só com a cabeça.",
                body_beats=["Os braços processam sinais.", "O corpo reage antes da cabeça.", "Isso muda a leitura do animal."],
            ending="Isso muda como você olha para o animal.",
            cta=None,
            full_narration="O polvo não pensa só com a cabeça. Seus braços processam sinais.",
            estimated_duration_sec=30,
            key_facts=[],
            token_count=30,
            language="pt-BR",
            qa_metrics={},
        )
        session.add_all([topic_plan, script])
        session.commit()

    with SessionLocal() as session:
        current = session.get(Job, "job-repetition-current")
        topic_plan = session.query(TopicPlan).filter_by(job_id=current.job_id).one()
        script = session.query(Script).filter_by(job_id=current.job_id).one()
        report = orchestrator._build_channel_repetition_report(session, current, topic_plan, script)

    assert report["repetition_risk"] in {"medium", "high"}
    assert report["matches"]
    assert report["signals"]["exact_hook_opening_matches"] >= 1
    assert report["signals"]["exact_title_opening_matches"] >= 1
    assert report["signals"]["exact_duration_bucket_matches"] >= 1
    assert report["signals"]["exact_beat_count_matches"] >= 1
    assert any("same_hook_opening" in match["signals"] for match in report["matches"])
    assert any("same_duration_bucket" in match["signals"] for match in report["matches"])


def test_retry_action_creates_new_job() -> None:
    client = TestClient(app)
    response = client.post("/jobs", data={"seed_theme": "vulcoes", "target_duration_sec": 35}, follow_redirects=False)
    job_id = response.headers["location"].split("/")[-1]
    wait_for_any_status(job_id, {"monetization_review", "blocked_for_monetization"})
    retry = client.post(
        f"/jobs/{job_id}/review",
        data={"action": "retry", "reason_codes": "render_issue"},
        follow_redirects=False,
    )
    assert retry.status_code == 303
    new_job_id = retry.headers["location"].split("/")[-1]
    assert new_job_id != job_id


def test_review_action_rejects_non_reviewable_status() -> None:
    client = TestClient(app)
    response = client.post("/jobs", data={"seed_theme": "polvos", "target_duration_sec": 35}, follow_redirects=False)
    job_id = response.headers["location"].split("/")[-1]

    approve = client.post(
        f"/jobs/{job_id}/review",
        data={"action": "approve", "reason_codes": "rights_confirmed"},
        follow_redirects=False,
    )

    assert approve.status_code == 303
    assert "review_error=job+status+queued+cannot+be+approved" in approve.headers["location"]


def test_manual_publish_requires_youtube_reference() -> None:
    client = TestClient(app)
    job_id = "manual-publish-reference-required"
    topic_request_id = "manual-publish-reference-required-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="publish",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=35,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="request",
                niche_id="curiosidades",
                seed_theme="polvos",
                language="pt-BR",
                target_duration_sec=35,
            )
        )
        session.commit()

    response = client.post(f"/jobs/{job_id}/publish", data={}, follow_redirects=False)

    assert response.status_code == 303
    assert "publish_error=manual+publish+requires+youtube_video_id+or+youtube_url" in response.headers["location"]


def test_schedule_publication_persists_row_and_appears_in_calendar() -> None:
    client = TestClient(app)
    job_id = "scheduled-calendar-job"
    topic_request_id = "scheduled-calendar-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-request",
                niche_id="curiosidades",
                seed_theme="Lago Natron",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            Script(
                script_id="scheduled-calendar-script",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-calendar-script",
                title="Lago Natron parece impossível",
                hook="Parece pedra, mas era um animal.",
                body_beats=["A química da água muda o aspecto dos corpos."],
                ending="O choque é real, mas a explicação também é.",
                cta=None,
                full_narration="Parece pedra, mas era um animal. A química da água muda o aspecto dos corpos. O choque é real, mas a explicação também é.",
                estimated_duration_sec=40,
                key_facts=[],
                token_count=24,
                language="pt-BR",
                qa_metrics={},
                prompt_version="test",
            )
        )
        session.commit()

    response = client.post(
        f"/jobs/{job_id}/schedule",
        data={
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "America/Sao_Paulo",
            "youtube_visibility": "private",
            "notes": "slot da tarde",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "scheduled"
    assert schedule.timezone == "America/Sao_Paulo"
    assert schedule.youtube_visibility == "private"
    assert job and job.artifact_index["publication_schedule"] == "publication_schedule.json"
    artifact = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "publication_schedule.json").read_text(encoding="utf-8"))
    assert artifact["local_date"] == "2099-06-10"

    calendar_page = client.get("/calendar?month=2099-06")
    assert calendar_page.status_code == 200
    assert "Lago Natron parece impossível" in calendar_page.text
    assert "14:30" in calendar_page.text


def test_schedule_publication_queues_tiktok_crosspost_when_enabled(monkeypatch) -> None:
    job_id = "scheduled-tiktok-crosspost"
    monkeypatch.setattr(orchestrator.settings, "tiktok_auto_publish_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "tiktok_privacy_level", "PUBLIC_TO_EVERYONE")
    with SessionLocal() as session:
        _create_basic_job(session, job_id=job_id, status="approved_for_publish", seed_theme="TikTok agenda")
        session.commit()

    orchestrator.schedule_publication(
        job_id,
        {
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "America/Sao_Paulo",
            "youtube_visibility": "private",
            "notes": "",
        },
    )

    with SessionLocal() as session:
        publication = session.query(ChannelPublication).filter_by(job_id=job_id, channel="tiktok").one()

    assert publication.status == "scheduled"
    assert publication.source == "youtube_schedule"
    assert publication.privacy_level == "PUBLIC_TO_EVERYONE"
    scheduled_for_utc = publication.scheduled_for_utc if publication.scheduled_for_utc.tzinfo else publication.scheduled_for_utc.replace(tzinfo=UTC)
    assert scheduled_for_utc == datetime(2099, 6, 10, 17, 30, tzinfo=UTC)


def test_tiktok_retropost_queue_respects_daily_limit(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "tiktok_auto_publish_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "tiktok_retropost_daily_limit", 1)
    old_time = utcnow() - timedelta(days=5)
    with SessionLocal() as session:
        for index in range(2):
            job_id = f"tiktok-retropost-{index}"
            _create_basic_job(session, job_id=job_id, status="published", seed_theme=f"Publicado antigo {index}")
            session.add(
                PublicationSchedule(
                    schedule_id=f"{job_id}-schedule",
                    job_id=job_id,
                    schema_version="1.0.0",
                    content_hash=f"{job_id}-schedule-hash",
                    scheduled_for_utc=old_time,
                    timezone="America/Sao_Paulo",
                    youtube_visibility="public",
                    status="published",
                    published_at=old_time,
                )
            )
        session.commit()

    queued = orchestrator._sync_tiktok_crosspost_queue()

    with SessionLocal() as session:
        publications = session.query(ChannelPublication).filter_by(channel="tiktok", source="retropost").all()

    assert queued >= 1
    assert len(publications) == 1
    assert publications[0].status == "scheduled"


def test_due_tiktok_publication_uses_real_publisher_and_persists_processing(monkeypatch, tmp_path) -> None:
    job_id = "due-tiktok-publish"
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"fake mp4")
    monkeypatch.setattr(orchestrator.settings, "tiktok_auto_publish_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "tiktok_access_token", "token")
    monkeypatch.setattr(
        orchestrator,
        "_build_publish_package",
        lambda session, job: {
            "title": "Titulo TikTok",
            "hashtags": ["curiosidades"],
            "video_uri": str(video_path),
            "altered_or_synthetic": True,
        },
    )
    calls: list[dict] = []

    def fake_direct_post_video(**payload):
        calls.append(payload)
        return {"publish_id": "tt-publish-123", "status": "processing"}

    monkeypatch.setattr(orchestrator.tiktok, "direct_post_video", fake_direct_post_video)
    with SessionLocal() as session:
        _create_basic_job(session, job_id=job_id, status="approved_for_publish", seed_theme="TikTok devido")
        session.add(
            ChannelPublication(
                publication_id="due-tiktok-publication-row",
                job_id=job_id,
                channel="tiktok",
                schema_version="1.0.0",
                content_hash="due-tiktok-publication-row",
                scheduled_for_utc=utcnow() - timedelta(minutes=1),
                timezone="America/Sao_Paulo",
                status="scheduled",
                source="youtube_schedule",
                privacy_level="PUBLIC_TO_EVERYONE",
            )
        )
        session.commit()

    claimed = orchestrator._claim_due_tiktok_publication()
    assert claimed == "due-tiktok-publication-row"
    orchestrator._publish_tiktok_channel_publication(claimed)

    with SessionLocal() as session:
        publication = session.get(ChannelPublication, "due-tiktok-publication-row")

    assert calls
    assert calls[0]["video_path"] == video_path
    assert calls[0]["title"] == "Titulo TikTok #curiosidades"
    assert calls[0]["is_aigc"] is True
    assert publication.status == "processing"
    assert publication.external_id == "tt-publish-123"
    assert publication.attempt_count == 1


def test_calendar_quick_add_lists_only_unscheduled_approved_jobs() -> None:
    client = TestClient(app)
    ready_job_id = "calendar-quick-ready"
    published_job_id = "calendar-quick-published"
    scheduled_job_id = "calendar-quick-scheduled"
    with SessionLocal() as session:
        _create_basic_job(session, job_id=ready_job_id, status="approved_for_publish", seed_theme="Job pronto para o calendário")
        _create_basic_job(session, job_id=published_job_id, status="published", seed_theme="Job publicado fora da lista")
        _create_basic_job(session, job_id=scheduled_job_id, status="approved_for_publish", seed_theme="Job já agendado fora da lista")
        session.add(
            PublicationSchedule(
                schedule_id=f"{scheduled_job_id}-schedule",
                job_id=scheduled_job_id,
                schema_version="1.0.0",
                content_hash=f"{scheduled_job_id}-schedule-hash",
                scheduled_for_utc=datetime(2100, 1, 10, 18, 0, tzinfo=UTC),
                timezone="UTC",
                youtube_visibility="private",
                status="scheduled",
            )
        )
        session.commit()

    response = client.get("/calendar?month=2099-07")

    assert response.status_code == 200
    assert 'action="/calendar/schedule"' in response.text
    assert 'id="calendar-schedule-modal"' in response.text
    assert 'data-open-calendar-schedule' in response.text
    assert 'data-calendar-date="2099-07-01"' in response.text
    assert "Job pronto para o calendário" in response.text
    assert "Job publicado fora da lista" not in response.text
    assert "Job já agendado fora da lista" not in response.text


def test_calendar_quick_add_schedules_job_on_selected_day() -> None:
    client = TestClient(app)
    job_id = "calendar-quick-post"
    with SessionLocal() as session:
        _create_basic_job(session, job_id=job_id, status="approved_for_publish", seed_theme="Publicação rápida")
        session.commit()

    response = client.post(
        "/calendar/schedule",
        data={
            "job_id": job_id,
            "scheduled_date": "2099-08-15",
            "scheduled_time": "16:45",
            "timezone": "America/Sao_Paulo",
            "youtube_visibility": "private",
            "month": "2099-08",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar?month=2099-08"
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "scheduled"
    assert schedule.timezone == "America/Sao_Paulo"
    assert schedule.youtube_visibility == "private"
    assert job and job.artifact_index["publication_schedule"] == "publication_schedule.json"
    artifact = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "publication_schedule.json").read_text(encoding="utf-8"))
    assert artifact["local_date"] == "2099-08-15"
    assert artifact["local_time"] == "16:45"


def test_clear_publication_schedule_marks_entry_cancelled() -> None:
    client = TestClient(app)
    job_id = "scheduled-clear-job"
    topic_request_id = "scheduled-clear-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-clear",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-clear-request",
                niche_id="curiosidades",
                seed_theme="Polvos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="schedule-clear-row",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-clear-row",
                scheduled_for_utc=utcnow() + timedelta(days=30),
                timezone="UTC",
                youtube_visibility="unlisted",
                status="scheduled",
            )
        )
        session.commit()

    response = client.post(f"/jobs/{job_id}/schedule", data={"action": "clear"}, follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()

    assert schedule.status == "cancelled"


def test_schedule_publication_requires_approved_job() -> None:
    client = TestClient(app)
    job_id = "scheduled-unapproved-job"
    topic_request_id = "scheduled-unapproved-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-unapproved",
                status="ready_for_upload",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-unapproved-request",
                niche_id="curiosidades",
                seed_theme="Polvos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.commit()

    response = client.post(
        f"/jobs/{job_id}/schedule",
        data={
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "UTC",
            "youtube_visibility": "private",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "approved_for_publish" in response.text


def test_manual_publish_marks_publication_schedule_as_published() -> None:
    client = TestClient(app)
    job_id = "scheduled-publish-job"
    topic_request_id = "scheduled-publish-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-publish",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-publish-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="schedule-publish-row",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="schedule-publish-row",
                scheduled_for_utc=utcnow() + timedelta(days=2),
                timezone="UTC",
                youtube_visibility="private",
                status="scheduled",
            )
        )
        session.commit()

    response = client.post(
        f"/jobs/{job_id}/publish",
        data={"youtube_url": "https://youtube.com/shorts/abc123"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "published"
    assert schedule.youtube_url == "https://youtube.com/shorts/abc123"
    assert schedule.published_at is not None
    assert job and job.status == "published"


def test_job_detail_hides_immediate_publish_when_schedule_is_active() -> None:
    client = TestClient(app)
    job_id = "scheduled-detail-job"
    topic_request_id = "scheduled-detail-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-detail",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-detail-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="scheduled-detail-row",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-detail-row",
                scheduled_for_utc=datetime(2099, 6, 11, 17, 0, tzinfo=UTC),
                timezone="America/Sao_Paulo",
                youtube_visibility="public",
                status="scheduled",
            )
        )
        session.commit()

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Publicação automática ativada" in response.text
    assert "2099-06-11" in response.text
    assert "14:00" in response.text
    assert "Não precisa clicar em mais nada" in response.text
    assert "Reagendar publicação" in response.text
    assert 'id="schedule-picker-modal"' in response.text
    assert 'data-open-schedule-picker' in response.text
    assert 'name="scheduled_for_local" type="hidden" value="2099-06-11T14:00"' in response.text
    assert "Publicar imediatamente e ignorar agenda" not in response.text


def test_job_detail_schedule_uses_modal_picker_for_publication_time() -> None:
    client = TestClient(app)
    job_id = "scheduled-modal-picker-job"
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Vênus",
            review_state="approved",
        )
        session.commit()

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Horário de publicação" in response.text
    assert "Escolher data e hora" in response.text
    assert 'data-schedule-form' in response.text
    assert 'data-schedule-days' in response.text
    assert 'data-schedule-time-chips' in response.text
    assert 'name="scheduled_for_local" type="hidden" value=""' in response.text
    assert 'type="datetime-local"' not in response.text


def test_retention_sweep_cleans_expired_hard_failure_artifacts() -> None:
    job_id = "retention-hard-failure-job"
    expired_at = utcnow() - timedelta(days=2)
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="render_quality_failed",
            seed_theme="Falha dura",
            updated_at=expired_at,
            artifact_index={"render": "render/final.mp4"},
        )
        session.commit()

    artifact_path = _write_job_artifact(job_id, "render/final.mp4", "video")
    cleaned = orchestrator._run_retention_sweep()

    assert cleaned >= 1
    assert not artifact_path.exists()
    cleanup_path = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "retention_cleanup.json"
    assert cleanup_path.exists()
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    assert cleanup["classification"] == "hard_failure"
    assert cleanup["cleanup_reason"] == "ttl_expired"

    with SessionLocal() as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == "render_quality_failed"
    assert job.artifact_index == {"retention_cleanup": "retention_cleanup.json"}
    assert job.quality_summary["retention"]["cleaned"] is True
    assert job.quality_summary["retention"]["classification"] == "hard_failure"


def test_retention_sweep_cleans_recoverable_jobs_after_medium_ttl() -> None:
    job_id = "retention-recoverable-job"
    expired_at = utcnow() - timedelta(days=8)
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="rejected",
            seed_theme="Pode corrigir",
            updated_at=expired_at,
            artifact_index={"render": "render/final.mp4"},
        )
        session.commit()

    artifact_path = _write_job_artifact(job_id, "render/final.mp4", "video")
    cleaned = orchestrator._run_retention_sweep()

    assert cleaned >= 1
    assert not artifact_path.exists()
    with SessionLocal() as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == "rejected"
    assert job.quality_summary["retention"]["cleaned"] is True
    assert job.quality_summary["retention"]["classification"] == "recoverable"


def test_retention_sweep_keeps_publishable_jobs_longer_and_job_detail_handles_cleanup() -> None:
    client = TestClient(app)
    job_id = "retention-publishable-job"
    expired_job_id = "retention-publishable-expired-job"
    medium_age = utcnow() - timedelta(days=8)
    long_age = utcnow() - timedelta(days=30)
    video_path = _write_job_artifact(job_id, "render/final.mp4", "video")
    poster_path = _write_job_artifact(job_id, "render/poster.jpg", "poster")
    log_path = _write_job_artifact(job_id, "render/ffmpeg.log", "log")
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Danakil",
            updated_at=medium_age,
            artifact_index={"render": "render/final.mp4", "publish_package": "publish_package.json"},
            review_state="approved",
        )
        session.add(
            RenderOutput(
                render_id=f"{job_id}-render",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=f"{job_id}-render-hash",
                video_uri=video_path.as_uri(),
                poster_uri=poster_path.as_uri(),
                waveform_uri=None,
                duration_ms=36_000,
                resolution="1080x1920",
                video_codec="h264",
                audio_codec="aac",
                filesize_bytes=1024,
                ffmpeg_log_uri=log_path.as_uri(),
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id=f"{job_id}-schedule",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=f"{job_id}-schedule-hash",
                created_at=medium_age,
                updated_at=medium_age,
                scheduled_for_utc=utcnow() + timedelta(days=10),
                timezone="America/Sao_Paulo",
                youtube_visibility="private",
                status="scheduled",
            )
        )
        session.commit()
        _create_basic_job(
            session,
            job_id=expired_job_id,
            status="approved_for_publish",
            seed_theme="Danakil expirado",
            updated_at=long_age,
            artifact_index={"render": "render/final.mp4", "publish_package": "publish_package.json"},
            review_state="approved",
        )
        session.add(
            RenderOutput(
                render_id=f"{expired_job_id}-render",
                job_id=expired_job_id,
                schema_version="1.0.0",
                content_hash=f"{expired_job_id}-render-hash",
                video_uri=_write_job_artifact(expired_job_id, "render/final.mp4", "video").as_uri(),
                poster_uri=_write_job_artifact(expired_job_id, "render/poster.jpg", "poster").as_uri(),
                waveform_uri=None,
                duration_ms=36_000,
                resolution="1080x1920",
                video_codec="h264",
                audio_codec="aac",
                filesize_bytes=1024,
                ffmpeg_log_uri=_write_job_artifact(expired_job_id, "render/ffmpeg.log", "log").as_uri(),
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id=f"{expired_job_id}-schedule",
                job_id=expired_job_id,
                schema_version="1.0.0",
                content_hash=f"{expired_job_id}-schedule-hash",
                created_at=long_age,
                updated_at=long_age,
                scheduled_for_utc=utcnow() + timedelta(days=10),
                timezone="America/Sao_Paulo",
                youtube_visibility="private",
                status="scheduled",
            )
        )
        session.commit()

    orchestrator.storage.persist_json(
        job_id,
        "publish_package.json",
        {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "title": "Meta Danakil",
            "description": "Descrição de teste",
            "hashtags": ["#shorts", "#danakil"],
            "video_uri": video_path.as_uri(),
        },
    )
    orchestrator.storage.persist_json(
        expired_job_id,
        "publish_package.json",
        {
            "schema_version": "1.0.0",
            "job_id": expired_job_id,
            "title": "Meta Danakil",
            "description": "Descrição de teste",
            "hashtags": ["#shorts", "#danakil"],
            "video_uri": f"file://{expired_job_id}/render/final.mp4",
        },
    )

    cleaned = orchestrator._run_retention_sweep()

    assert cleaned >= 1
    assert video_path.exists()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.quality_summary["retention"]["cleaned"] is False
        assert job.quality_summary["retention"]["classification"] == "publishable"
    response = client.get(f"/jobs/{expired_job_id}")
    assert response.status_code == 200
    assert "Artefatos expirados e removidos automaticamente." in response.text
    assert "Os arquivos de mídia deste job já foram removidos." in response.text
    assert "Meta Danakil" in response.text

    cleanup_path = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / expired_job_id / "retention_cleanup.json"
    assert cleanup_path.exists()
    with SessionLocal() as session:
        job = session.get(Job, expired_job_id)

    assert job is not None
    assert job.artifact_index == {"retention_cleanup": "retention_cleanup.json"}
    assert job.quality_summary["retention"]["cleaned"] is True
    assert job.quality_summary["retention"]["classification"] == "publishable"


def test_retention_sweep_skips_published_jobs() -> None:
    job_id = "retention-published-job"
    old_timestamp = utcnow() - timedelta(days=60)
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="published",
            seed_theme="Publicado",
            updated_at=old_timestamp,
            artifact_index={"render": "render/final.mp4"},
            review_state="published",
        )
        session.commit()

    artifact_path = _write_job_artifact(job_id, "render/final.mp4", "video")
    cleaned = orchestrator._run_retention_sweep()

    assert cleaned == 0
    assert artifact_path.exists()
    assert not (Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "retention_cleanup.json").exists()
    with SessionLocal() as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert (job.quality_summary or {}).get("retention") is None


def test_manual_publish_syncs_stale_monetization_report_when_quality_summary_passed() -> None:
    client = TestClient(app)
    job_id = "stale-monetization-publish-job"
    topic_request_id = "stale-monetization-publish-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="stale-publish",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
                quality_summary={
                    "monetization": {
                        "passed": True,
                        "final_status": "ready_for_upload",
                        "hard_blockers": [],
                        "manual_required": [],
                        "warnings": [],
                    }
                },
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="stale-publish-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.commit()

    orchestrator.storage.persist_json(
        job_id,
        "monetization_report.json",
        {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "created_at": utcnow().isoformat(),
            "passed": False,
            "final_status": "blocked_for_monetization",
            "hard_blockers": ["old_blocker"],
            "manual_required": [],
            "warnings": [],
        },
    )

    response = client.post(
        f"/jobs/{job_id}/publish",
        data={"youtube_url": "https://youtube.com/shorts/abc123"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    report = orchestrator._read_job_json(job_id, "monetization_report.json")
    assert report["passed"] is True
    assert report["final_status"] == "ready_for_upload"


def test_reopen_publication_moves_published_job_back_to_approved_for_republish() -> None:
    client = TestClient(app)
    job_id = "reopen-published-job"
    topic_request_id = "reopen-published-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="reopen-publish",
                status="published",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                review_state="published",
                artifact_index={},
                quality_summary={"youtube_publish": {"status": "published"}},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="reopen-publish-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="reopen-publish-row",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="reopen-publish-row",
                scheduled_for_utc=utcnow() + timedelta(days=2),
                timezone="UTC",
                youtube_visibility="private",
                status="published",
                youtube_video_id="yt123",
                youtube_url="https://youtube.com/watch?v=yt123",
                published_at=utcnow(),
            )
        )
        session.commit()

    response = client.post(f"/jobs/{job_id}/reopen-publication", follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "cancelled"
    assert schedule.youtube_video_id is None
    assert schedule.youtube_url is None
    assert schedule.published_at is None
    assert job and job.status == "approved_for_publish"
    assert job.review_state == "approved"
    assert job.quality_summary["youtube_publish"]["status"] == "reopened_for_republish"


def test_reopen_publication_rejects_job_that_was_not_published() -> None:
    client = TestClient(app)
    job_id = "reopen-not-published-job"
    topic_request_id = "reopen-not-published-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="reopen-not-published",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="reopen-not-published-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.commit()

    response = client.post(f"/jobs/{job_id}/reopen-publication", follow_redirects=False)

    assert response.status_code == 409
    assert "only published jobs can be reopened for republication" in response.text


def test_publish_metadata_form_persists_overrides_into_publish_package() -> None:
    client = TestClient(app)
    job_id = "publish-metadata-job"
    topic_request_id = "publish-metadata-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="publish-metadata",
                status="ready_for_upload",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="publish-metadata-request",
                niche_id="curiosidades",
                seed_theme="Danakil",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            Script(
                script_id="publish-metadata-script",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="publish-metadata-script",
                title="Danakil parece outro planeta",
                hook="Danakil parece outro planeta.",
                body_beats=["O povo Afar vive ali há gerações."],
                ending="É um lugar real, não ficção.",
                cta=None,
                full_narration="Danakil parece outro planeta. O povo Afar vive ali há gerações. É um lugar real, não ficção.",
                estimated_duration_sec=38,
                key_facts=[],
                token_count=18,
                language="pt-BR",
                qa_metrics={},
                prompt_version="test",
            )
        )
        session.commit()

    response = client.post(
        f"/jobs/{job_id}/publish-metadata",
        data={
            "title": "Danakil: o lugar mais extremo da Terra onde pessoas ainda vivem",
            "description": "Na Depressão de Danakil, na Etiópia, calor extremo e salinas criam uma das paisagens mais hostis do planeta.",
            "hashtags": "#shorts #curiosidades #danakil #etiopia #geografia",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    overrides = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "publish_metadata_overrides.json").read_text(encoding="utf-8"))
    package = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "publish_package.json").read_text(encoding="utf-8"))

    assert overrides["title"] == "Danakil: o lugar mais extremo da Terra onde pessoas ainda vivem"
    assert package["title"] == overrides["title"]
    assert package["description"].startswith("Na Depressão de Danakil")
    assert package["hashtags"] == ["#shorts", "#curiosidades", "#danakil", "#etiopia", "#geografia"]


def test_schedule_publication_requires_connected_youtube_in_api_mode(monkeypatch) -> None:
    client = TestClient(app)
    job_id = "scheduled-api-needs-oauth"
    topic_request_id = "scheduled-api-needs-oauth-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-api-needs-oauth",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="scheduled-api-needs-oauth-request",
                niche_id="curiosidades",
                seed_theme="Axolotes",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")
    monkeypatch.setattr(
        orchestrator.youtube,
        "connection_status",
        lambda redirect_uri=None: YouTubeConnectionStatus(
            connected=False,
            client_configured=True,
            dependencies_available=True,
            missing_items=["Canal ainda não conectado por OAuth"],
            redirect_uri=redirect_uri,
            token_expires_at=None,
            granted_scopes=[],
            connected_at=None,
        ),
    )

    response = client.post(
        f"/jobs/{job_id}/schedule",
        data={
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "UTC",
            "youtube_visibility": "private",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "OAuth" in response.text


def test_schedule_publication_uploads_video_immediately_in_api_mode(monkeypatch) -> None:
    client = TestClient(app)
    job_id = "scheduled-native-youtube-job"
    topic_request_id = "scheduled-native-youtube-job-request"
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Atacama",
            review_state="approved",
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")
    monkeypatch.setattr(orchestrator, "_ensure_youtube_api_ready", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_publish_package",
        lambda session, job: {
            "video_uri": "file:///tmp/fake-short.mp4",
            "title": "Atacama",
            "description": "descricao",
            "hashtags": ["#shorts", "#deserto"],
            "altered_or_synthetic": False,
        },
    )
    monkeypatch.setattr(
        orchestrator.youtube,
        "upload_video",
        lambda **kwargs: {
            "id": "yt-scheduled-123",
            "youtube_url": "https://www.youtube.com/watch?v=yt-scheduled-123",
            "status": {"privacyStatus": "private"},
        },
    )

    response = client.post(
        f"/jobs/{job_id}/schedule",
        data={
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "America/Sao_Paulo",
            "youtube_visibility": "public",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "scheduled"
    assert schedule.youtube_video_id == "yt-scheduled-123"
    assert schedule.youtube_url == "https://www.youtube.com/watch?v=yt-scheduled-123"
    assert job is not None
    assert job.status == "approved_for_publish"
    assert job.quality_summary["youtube_publish"]["status"] == "scheduled"
    assert job.quality_summary["youtube_publish"]["native_youtube_schedule"] is True


def test_schedule_publication_rejects_non_public_visibility_in_api_mode(monkeypatch) -> None:
    client = TestClient(app)
    job_id = "scheduled-native-private-job"
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Salinas",
            review_state="approved",
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")
    monkeypatch.setattr(orchestrator, "_ensure_youtube_api_ready", lambda: None)

    response = client.post(
        f"/jobs/{job_id}/schedule",
        data={
            "scheduled_for_local": "2099-06-10T14:30",
            "timezone": "UTC",
            "youtube_visibility": "private",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "requires visibility public" in response.text


def test_api_publish_uploads_video_without_manual_url(monkeypatch) -> None:
    client = TestClient(app)
    job_id = "api-publish-job"
    topic_request_id = "api-publish-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="api-publish-job",
                status="approved_for_publish",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="api-publish-job-request",
                niche_id="curiosidades",
                seed_theme="Aves migratórias",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")
    monkeypatch.setattr(orchestrator, "_ensure_youtube_api_ready", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_upload_publish_package",
        lambda package, visibility: {
            "mode": "api",
            "api_enabled": True,
            "video_id": "yt123",
            "url": "https://www.youtube.com/watch?v=yt123",
            "published_at": "2099-06-10T14:30:00+00:00",
            "target_visibility": visibility,
            "actual_visibility": visibility,
            "response": {"id": "yt123", "status": {"privacyStatus": visibility}},
        },
    )

    response = client.post(f"/jobs/{job_id}/publish", data={}, follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "published"
    assert schedule.youtube_video_id == "yt123"
    assert schedule.youtube_url == "https://www.youtube.com/watch?v=yt123"
    assert job and job.status == "published"


def test_claim_due_publication_schedule_skips_video_already_scheduled_on_youtube(monkeypatch) -> None:
    job_id = "scheduled-native-due-job"
    due_at = utcnow() - timedelta(minutes=5)
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Danakil",
            review_state="approved",
        )
        session.add(
            PublicationSchedule(
                schedule_id=f"{job_id}-schedule",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=f"{job_id}-schedule",
                scheduled_for_utc=due_at,
                timezone="UTC",
                youtube_visibility="public",
                status="scheduled",
                youtube_video_id="yt-native-123",
                youtube_url="https://www.youtube.com/watch?v=yt-native-123",
            )
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")

    assert orchestrator._claim_due_publication_schedule() is None


def test_sync_native_scheduled_publication_marks_job_as_published(monkeypatch) -> None:
    job_id = "scheduled-native-sync-job"
    due_at = utcnow() - timedelta(minutes=5)
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=job_id,
            status="approved_for_publish",
            seed_theme="Atacama",
            review_state="approved",
        )
        session.add(
            PublicationSchedule(
                schedule_id=f"{job_id}-schedule",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=f"{job_id}-schedule",
                scheduled_for_utc=due_at,
                timezone="UTC",
                youtube_visibility="public",
                status="scheduled",
                youtube_video_id="yt-native-sync",
                youtube_url="https://www.youtube.com/watch?v=yt-native-sync",
            )
        )
        session.commit()

    monkeypatch.setattr(orchestrator.settings, "youtube_api_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "youtube_publish_mode", "api")
    monkeypatch.setattr(
        orchestrator.youtube,
        "fetch_video",
        lambda video_id: {
            "id": video_id,
            "status": {"privacyStatus": "public"},
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        },
    )

    synced = orchestrator._sync_native_scheduled_publications()

    assert synced >= 1
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert schedule.status == "published"
    assert schedule.published_at is not None
    assert job is not None
    assert job.status == "published"
    assert job.quality_summary["youtube_publish"]["status"] == "published"


def test_youtube_connect_redirects_to_google(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(orchestrator.youtube, "authorization_url", lambda redirect_uri: "https://accounts.google.com/o/oauth2/auth?state=test")

    response = client.get("/youtube/connect", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/o/oauth2/auth")


def test_youtube_build_flow_enables_pkce(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        youtube_api_enabled=True,
        youtube_publish_mode="api",
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
    )
    publisher = YouTubePublisher(settings)
    captured: dict[str, object] = {}

    class FakeFlow:
        def __init__(self) -> None:
            self.redirect_uri = None

    def fake_from_client_config(client_config, scopes, **kwargs):
        captured["client_config"] = client_config
        captured["scopes"] = scopes
        captured["kwargs"] = kwargs
        return FakeFlow()

    flow_module = SimpleNamespace(Flow=SimpleNamespace(from_client_config=fake_from_client_config))
    monkeypatch.setattr(publisher, "_google_flow_dependency", lambda: flow_module)

    flow = publisher._build_flow("https://example.test/youtube/oauth/callback", state="state-123")

    assert captured["scopes"] == [
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.upload",
    ]
    assert captured["kwargs"]["state"] == "state-123"
    assert captured["kwargs"]["autogenerate_code_verifier"] is True
    assert flow.redirect_uri == "https://example.test/youtube/oauth/callback"


def test_youtube_exchange_code_restores_saved_code_verifier(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        youtube_api_enabled=True,
        youtube_publish_mode="api",
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    publisher = YouTubePublisher(settings)
    built_flows: list[object] = []

    class FakeCredentials:
        token = "token-123"
        refresh_token = "refresh-123"
        token_uri = "https://oauth2.googleapis.com/token"
        client_id = "client-id"
        client_secret = "client-secret"
        scopes = [
            "https://www.googleapis.com/auth/youtube.force-ssl",
            "https://www.googleapis.com/auth/youtube.upload",
        ]
        expiry = datetime(2026, 5, 12, 15, 0, tzinfo=UTC)

    class FakeFlow:
        def __init__(self) -> None:
            self.redirect_uri = None
            self.code_verifier = None
            self.credentials = FakeCredentials()

        def authorization_url(self, **kwargs):
            self.code_verifier = "verifier-123"
            return "https://accounts.google.com/o/oauth2/auth?state=state-123", "state-123"

        def fetch_token(self, **kwargs) -> None:
            assert kwargs["code"] == "code-123"
            assert self.code_verifier == "verifier-123"

    def fake_build_flow(_redirect_uri: str, state: str | None = None):
        flow = FakeFlow()
        built_flows.append(flow)
        return flow

    monkeypatch.setattr(publisher, "_build_flow", fake_build_flow)

    publisher.authorization_url("https://example.test/youtube/oauth/callback")
    state_payload = json.loads(settings.youtube_oauth_state_path.read_text(encoding="utf-8"))

    assert state_payload["state"] == "state-123"
    assert state_payload["code_verifier"] == "verifier-123"

    payload = publisher.exchange_code(code="code-123", state="state-123")

    assert len(built_flows) == 2
    assert built_flows[1].code_verifier == "verifier-123"
    assert payload["scopes"] == [
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.upload",
    ]
    assert not settings.youtube_oauth_state_path.exists()


def test_youtube_load_credentials_normalizes_aware_expiry_for_google_auth(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        youtube_api_enabled=True,
        youtube_publish_mode="api",
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.youtube_token_path.write_text(
        json.dumps(
            {
                "token": "token-123",
                "refresh_token": "refresh-123",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": [
                    "https://www.googleapis.com/auth/youtube.force-ssl",
                    "https://www.googleapis.com/auth/youtube.upload",
                ],
                "expiry": "2026-05-12T14:50:58+00:00",
                "connected_at": "2026-05-12T14:50:59+00:00",
                "redirect_uri": "https://example.test/youtube/oauth/callback",
            }
        ),
        encoding="utf-8",
    )

    refresh_calls: list[str] = []

    class FakeCredentials:
        def __init__(self, **kwargs):
            self.token = kwargs["token"]
            self.refresh_token = kwargs["refresh_token"]
            self.token_uri = kwargs["token_uri"]
            self.client_id = kwargs["client_id"]
            self.client_secret = kwargs["client_secret"]
            self.scopes = kwargs["scopes"]
            self.expiry = None

        @property
        def expired(self) -> bool:
            # Mimic google-auth behavior, which compares against naive UTC now.
            return datetime.now(UTC).replace(tzinfo=None) >= self.expiry

        def refresh(self, _request) -> None:
            refresh_calls.append("called")
            self.token = "token-refreshed"
            self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(
        YouTubePublisher,
        "_google_auth_dependencies",
        lambda self: (SimpleNamespace(Credentials=FakeCredentials), SimpleNamespace(Request=lambda: object())),
    )

    publisher = YouTubePublisher(settings)
    credentials = publisher._load_credentials(refresh=True)

    assert refresh_calls == ["called"]
    assert credentials.token == "token-refreshed"
    assert credentials.expiry.tzinfo is None


def test_publication_dashboard_fragment_shows_ready_and_scheduled_items() -> None:
    client = TestClient(app)
    approved_job_id = "publication-dashboard-approved"
    scheduled_job_id = "publication-dashboard-scheduled"
    with SessionLocal() as session:
        session.add_all(
            [
                Job(
                    job_id=approved_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-approved",
                    status="approved_for_publish",
                    niche_id="curiosidades",
                    language="pt-BR",
                    target_duration_sec=45,
                    topic_request_id="publication-dashboard-approved-request",
                    artifact_index={},
                ),
                TopicRequest(
                    topic_request_id="publication-dashboard-approved-request",
                    job_id=approved_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-approved-request",
                    niche_id="curiosidades",
                    seed_theme="Lulas gigantes",
                    language="pt-BR",
                    target_duration_sec=45,
                ),
                Script(
                    script_id="publication-dashboard-approved-script",
                    job_id=approved_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-approved-script",
                    title="Lulas gigantes somem por um motivo",
                    hook="Elas aparecem e desaparecem do nada.",
                    body_beats=["O habitat profundo limita encontros humanos."],
                    ending="Por isso cada imagem delas parece impossível.",
                    cta=None,
                    full_narration="Elas aparecem e desaparecem do nada. O habitat profundo limita encontros humanos. Por isso cada imagem delas parece impossível.",
                    estimated_duration_sec=39,
                    key_facts=[],
                    token_count=20,
                    language="pt-BR",
                    qa_metrics={},
                    prompt_version="test",
                ),
                Job(
                    job_id=scheduled_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-scheduled",
                    status="approved_for_publish",
                    niche_id="curiosidades",
                    language="pt-BR",
                    target_duration_sec=45,
                    topic_request_id="publication-dashboard-scheduled-request",
                    artifact_index={},
                ),
                TopicRequest(
                    topic_request_id="publication-dashboard-scheduled-request",
                    job_id=scheduled_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-scheduled-request",
                    niche_id="curiosidades",
                    seed_theme="Morcegos",
                    language="pt-BR",
                    target_duration_sec=45,
                ),
                Script(
                    script_id="publication-dashboard-scheduled-script",
                    job_id=scheduled_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-scheduled-script",
                    title="Morcegos enxergam com o som",
                    hook="Eles mapeiam o ar no escuro.",
                    body_beats=["O eco vira distância e forma."],
                    ending="É quase uma visão construída em tempo real.",
                    cta=None,
                    full_narration="Eles mapeiam o ar no escuro. O eco vira distância e forma. É quase uma visão construída em tempo real.",
                    estimated_duration_sec=37,
                    key_facts=[],
                    token_count=18,
                    language="pt-BR",
                    qa_metrics={},
                    prompt_version="test",
                ),
                PublicationSchedule(
                    schedule_id="publication-dashboard-scheduled-row",
                    job_id=scheduled_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-scheduled-row",
                    scheduled_for_utc=datetime(2099, 1, 2, 17, 0, tzinfo=UTC),
                    timezone="America/Sao_Paulo",
                    youtube_visibility="private",
                    status="scheduled",
                ),
            ]
        )
        session.commit()

    response = client.get("/publication-hub")

    assert response.status_code == 200
    assert "Centro de publicação" in response.text
    assert "Morcegos enxergam com o som" in response.text
    assert "14:00" in response.text
    assert "Canal" in response.text
    assert "/automation/ready-scripts/import" not in response.text


def test_review_page_renders_dynamic_checklist_and_structured_reason_codes() -> None:
    client = TestClient(app)
    job_id = "review-page-dynamic-checklist"
    topic_request_id = "review-page-dynamic-checklist-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="review-page",
                status="monetization_review",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=35,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="request",
                niche_id="curiosidades",
                seed_theme="polvos",
                language="pt-BR",
                target_duration_sec=35,
            )
        )
        session.commit()
    orchestrator.storage.persist_json(
        job_id,
        "monetization_report.json",
        {
            "final_status": "monetization_review",
            "passed": False,
            "hard_blockers": [],
            "manual_required": ["rights_confirmation_required"],
            "human_review_checklist": {
                "items": [
                    {
                        "code": "rights_confirmation_required",
                        "confirmation_code": "rights_confirmed",
                        "label": "Direitos comerciais confirmados",
                        "required": True,
                        "completed": False,
                        "source": "rights_registry",
                    },
                    {
                        "code": "youtube_ai_disclosure_toggle_required",
                        "confirmation_code": "ai_disclosure_confirmed",
                        "label": "Disclosure de IA marcado no YouTube",
                        "required": True,
                        "completed": True,
                        "auto_completed": True,
                        "source": "ai_disclosure",
                    },
                ],
            },
            "ai_disclosure": {"youtube_disclosure_required": True, "auto_confirmed": True},
            "rights_registry": {
                "entries": [
                    {
                        "asset_type": "image",
                        "scene_id": "scene-1",
                        "provider": "minimax",
                        "commercial_use_allowed": False,
                        "license_source": None,
                        "evidence_required": False,
                    }
                ]
            },
            "fact_claims_report": {"claim_trace": [], "claim_sources": []},
            "metadata_review": {"title": "Polvos", "suggested_hashtags": ["#shorts"], "reasons": []},
            "channel_repetition_report": {"repetition_risk": "low"},
        },
    )

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert 'name="confirmation_codes" value="rights_confirmed"' in response.text
    assert 'name="ai_disclosure_confirmed"' not in response.text
    assert 'name="reason_codes" value="visual_incoherence"' in response.text
    assert "Disclosure de IA marcado no YouTube" in response.text
    assert "automático" in response.text


def test_record_performance_metrics_persists_artifact_and_learning_brief() -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
        }
    )
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        session.add(
            TopicPlan(
                topic_id="topic-performance",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic-performance",
                canonical_topic="polvos",
                angle="biologia curiosa",
                hook_promise="o polvo não pensa só com a cabeça",
                entities=["polvos"],
                search_terms=["polvos"],
                title_candidates=["Polvos pensam com os braços"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id="script-performance",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script-performance",
                title="Polvos pensam com os braços",
                hook="O polvo não pensa só com a cabeça.",
                body_beats=["Os braços processam sinais."],
                ending="Isso muda como você olha para o animal.",
                cta=None,
                full_narration="O polvo não pensa só com a cabeça. Os braços processam sinais.",
                estimated_duration_sec=35,
                key_facts=[],
                token_count=20,
                language="pt-BR",
                qa_metrics={},
                prompt_version="test",
            )
        )
        assert job
        job.status = "published"
        session.commit()

    orchestrator.record_performance_metrics(
        job_id,
        {
            "source": "youtube_studio_manual",
            "retention_percent": 82.0,
            "viewed_vs_swiped_away_percent": 71.0,
            "rewatch_rate": 1.2,
            "likes": 10,
            "shares": 2,
            "comments": 1,
            "rpm_usd": 0.08,
            "monetization_status": "monetized",
            "notes": "bom loop",
        },
    )

    with SessionLocal() as session:
        metric = session.query(PerformanceMetric).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)
        brief = orchestrator._channel_learning_brief(session, "curiosidades")

    assert metric.retention_percent == 82.0
    assert job and job.artifact_index["performance_metrics"] == "performance_metrics.json"
    assert job.quality_summary["performance"]["retention_percent"] == 82.0
    report = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "performance_metrics.json").read_text(encoding="utf-8"))
    assert report["latest"]["retention_percent"] == 82.0
    assert brief["sample_count"] >= 1
    assert brief["strong_patterns"]
    assert (Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "performance_metrics.json").exists()


def test_review_page_no_longer_promises_partial_retry() -> None:
    client = TestClient(app)
    response = client.post("/jobs", data={"seed_theme": "polvos", "target_duration_sec": 35}, follow_redirects=False)
    job_id = response.headers["location"].split("/")[-1]
    detail = client.get(f"/jobs/{job_id}")

    assert detail.status_code == 200
    assert 'name="retry_step"' not in detail.text
    assert 'value="retry_from_step"' not in detail.text
    assert 'value="retry"' not in detail.text
    assert "Nenhuma ação de review disponível para o status atual." in detail.text


def test_claim_next_job_is_atomic_under_concurrency() -> None:
    orchestrator.stop_worker()
    claim_orchestrator = JobOrchestrator()
    job_ids = [f"job-claim-{index}" for index in range(2)]
    topic_request_ids = [f"topic-claim-{index}" for index in range(2)]
    with SessionLocal() as session:
        base_created_at = utcnow() - timedelta(days=365)
        for index, (job_id, topic_request_id) in enumerate(zip(job_ids, topic_request_ids, strict=True)):
            session.add(
                Job(
                    job_id=job_id,
                    schema_version="1.0.0",
                    content_hash=job_id,
                    created_at=base_created_at + timedelta(seconds=index),
                    status="queued",
                    niche_id="curiosidades",
                    language="pt-BR",
                    target_duration_sec=35,
                    topic_request_id=topic_request_id,
                    artifact_index={},
                )
            )
        session.commit()

    barrier = threading.Barrier(2)
    claimed: list[str | None] = []
    errors: list[BaseException] = []

    def claimant() -> None:
        try:
            with SessionLocal() as session:
                barrier.wait(timeout=1.0)
                claimed.append(claim_orchestrator._claim_next_job(session))
                session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        assert not errors
        assert sorted(claimed) == job_ids
        with SessionLocal() as session:
            jobs = session.query(Job).filter(Job.job_id.in_(job_ids)).order_by(Job.job_id).all()
            assert [job.status for job in jobs] == ["running", "running"]
            assert {job.lease_owner for job in jobs} == {claim_orchestrator.worker_id}
    finally:
        with SessionLocal() as session:
            jobs = session.query(Job).filter(Job.job_id.in_(job_ids)).all()
            for job in jobs:
                session.delete(job)
            session.commit()
        orchestrator.start_worker()


def test_worker_can_restart_after_stop(monkeypatch) -> None:
    test_orchestrator = JobOrchestrator()
    loop_entered = threading.Event()
    loop_runs: list[float] = []

    def fake_worker_loop() -> None:
        loop_runs.append(time.time())
        loop_entered.set()
        while not test_orchestrator.stop_event.is_set():
            time.sleep(0.01)

    monkeypatch.setattr(test_orchestrator, "_worker_loop", fake_worker_loop)

    test_orchestrator.start_worker()
    assert loop_entered.wait(timeout=1.0)
    first_thread = test_orchestrator.worker_thread
    assert first_thread is not None
    test_orchestrator.stop_worker()
    assert not first_thread.is_alive()
    assert test_orchestrator.worker_thread is None

    loop_entered.clear()
    test_orchestrator.start_worker()
    assert loop_entered.wait(timeout=1.0)
    second_thread = test_orchestrator.worker_thread
    assert second_thread is not None
    assert second_thread is not first_thread
    test_orchestrator.stop_worker()
    assert len(loop_runs) == 2


def test_process_job_returns_persisted_cancelled_status_after_step_abort(monkeypatch) -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )

    def fake_steps() -> list[StepDefinition]:
        return [StepDefinition("script", 0, lambda session, job, attempt: (_ for _ in ()).throw(RecoverableStepError("gate failed")))]

    monkeypatch.setattr(orchestrator, "_steps", fake_steps)
    monkeypatch.setattr(orchestrator, "_build_step_input", lambda session, job, step_name: {"job_id": job.job_id, "step": step_name, "attempt_marker": time.time_ns()})

    status = orchestrator.process_job(job_id)

    assert status == "script_quality_failed"
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "script_quality_failed"


def test_orchestrator_routes_domain_steps_to_pipeline_modules() -> None:
    test_orchestrator = JobOrchestrator()
    handlers = {step.name: step.handler for step in test_orchestrator._steps()}

    assert handlers["script"].__self__ is test_orchestrator.script_pipeline
    assert handlers["scene_plan"].__self__ is test_orchestrator.scene_pipeline
    assert handlers["asset_generation"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["tts"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["subtitle_alignment"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["background_music"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["render"].__self__ is test_orchestrator.render_pipeline
    assert handlers["monetization_readiness_gate"].__self__ is test_orchestrator.monetization_pipeline
    assert handlers["publish_to_review_hub"].__self__ is test_orchestrator.monetization_pipeline


def test_pipelines_use_explicit_base_dependencies_and_asset_helpers() -> None:
    test_orchestrator = JobOrchestrator()

    assert "__getattr__" not in test_orchestrator.asset_pipeline.__class__.__mro__[1].__dict__
    assert "_build_fact_pack" not in test_orchestrator.script_pipeline.__class__.__mro__[1].__dict__
    assert "_normalize_scene_semantics" not in test_orchestrator.scene_pipeline.__class__.__mro__[1].__dict__
    assert test_orchestrator.script_pipeline._build_fact_pack.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.script_pipeline._validate_or_repair_script.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.script_pipeline._persist_script_generation_debug.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.scene_pipeline.normalize_scene_token_coverage.__self__ is test_orchestrator.scene_pipeline
    assert test_orchestrator.scene_pipeline.normalize_scene_semantics.__self__ is test_orchestrator.scene_pipeline
    assert test_orchestrator.scene_pipeline.fallback_query_variants.__self__ is test_orchestrator.scene_pipeline
    assert test_orchestrator.asset_pipeline._fit_tts_duration.__self__ is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline._mix_background_music_with_repair.__self__ is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline._split_subtitle_cue.__self__ is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline._generate_primary_asset.__self__ is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline.image_assets.pipeline is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline.tts.pipeline is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline.subtitles.pipeline is test_orchestrator.asset_pipeline
    assert test_orchestrator.asset_pipeline.music.pipeline is test_orchestrator.asset_pipeline
    assert test_orchestrator.render_pipeline.render_with_repair.__self__ is test_orchestrator.render_pipeline
    assert test_orchestrator.render_pipeline.mutate_render_command_for_repair.__self__ is test_orchestrator.render_pipeline
    assert test_orchestrator.monetization_pipeline.step_publish.__self__ is test_orchestrator.monetization_pipeline
    assert test_orchestrator.monetization_pipeline.build_monetization_report.__self__ is test_orchestrator.monetization_pipeline
    assert test_orchestrator.monetization_pipeline.build_rights_registry.__self__ is test_orchestrator.monetization_pipeline
    assert test_orchestrator.monetization_pipeline.build_fact_claims_report.__self__ is test_orchestrator.monetization_pipeline
    assert test_orchestrator.monetization_pipeline.build_publish_package.__self__ is test_orchestrator.monetization_pipeline
    assert test_orchestrator.monetization_pipeline.provider_publish_audit.__self__ is test_orchestrator.monetization_pipeline


def test_process_job_returns_persisted_cancelled_status_after_shutdown(monkeypatch) -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )

    def stopping_step(session, job, attempt):
        if attempt == 1:
            orchestrator.stop_event.set()
        raise RecoverableStepError("falha recuperavel")

    monkeypatch.setattr(orchestrator, "_steps", lambda: [StepDefinition("script", 1, stopping_step)])
    monkeypatch.setattr(orchestrator, "_build_step_input", lambda session, job, step_name: {"job_id": job.job_id, "step": step_name, "attempt_marker": time.time_ns()})

    try:
        status = orchestrator.process_job(job_id)
    finally:
        orchestrator.stop_event.clear()

    assert status == "cancelled"
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "cancelled"


def test_process_job_fails_explicitly_for_legacy_invalid_niche() -> None:
    job_id = "job-invalid-niche"
    topic_request_id = "topic-invalid-niche"
    with SessionLocal() as session:
        existing_job = session.get(Job, job_id)
        if existing_job is not None:
            session.delete(existing_job)
        existing_request = session.query(TopicRequest).filter_by(job_id=job_id).one_or_none()
        if existing_request is not None:
            session.delete(existing_request)
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="invalid-niche",
                status="queued",
                niche_id="esportes",
                language="pt-BR",
                target_duration_sec=35,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="invalid-niche-request",
                niche_id="esportes",
                seed_theme="polvos",
                language="pt-BR",
                target_duration_sec=35,
                tone="intrigante_direto",
                cta_style="none",
                notes=None,
                requested_angle=None,
            )
        )
        session.commit()

    status = orchestrator.process_job(job_id)

    assert status == "failed"
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.failure_reason == "input_gate: unsupported niche_id: esportes"


def test_process_job_fails_fast_for_invalid_language_in_persisted_request() -> None:
    job_id = "job-invalid-language"
    topic_request_id = "topic-invalid-language"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=job_id,
                created_at=utcnow(),
                status="queued",
                niche_id="curiosidades",
                language="en-US",
                target_duration_sec=35,
                topic_request_id=topic_request_id,
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash=topic_request_id,
                niche_id="curiosidades",
                seed_theme="polvos",
                language="en-US",
                target_duration_sec=35,
                tone="intrigante_direto",
                cta_style="none",
                notes=None,
                requested_angle=None,
            )
        )
        session.commit()

    status = orchestrator.process_job(job_id)

    assert status == "failed"
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.failure_reason == "input_gate: unsupported language: en-US"


def test_scene_timings_fall_back_to_token_boundaries() -> None:
    scenes = [
        {"scene_id": "scene-1", "token_start": 0, "token_end": 9},
        {"scene_id": "scene-2", "token_start": 10, "token_end": 19},
        {"scene_id": "scene-3", "token_start": 20, "token_end": 29},
    ]
    normalized = orchestrator._normalize_scene_timings(scenes, 30_000)
    assert [scene["actual_start_ms"] for scene in normalized] == [0, 10_000, 20_000]
    assert [scene["actual_end_ms"] for scene in normalized] == [10_000, 20_000, 30_000]


def test_scene_token_coverage_normalizes_numeric_scene_ids_to_strings() -> None:
    narration = "polvos tem tres coracoes e sangue azul no oceano profundo"
    scenes = [
        {"scene_id": 1, "order": 1, "narration_text": "polvos tem tres coracoes"},
        {"scene_id": 2, "order": 2, "narration_text": "e sangue azul no oceano profundo"},
    ]

    normalized = orchestrator._normalize_scene_token_coverage(scenes, narration)

    assert [scene["scene_id"] for scene in normalized] == ["1", "2"]
    assert normalized[0]["token_start"] == 0
    assert normalized[-1]["token_end"] == len(word_tokens(narration)) - 1


def test_subtitle_chunks_fit_two_lines_without_losing_words() -> None:
    text = "Cada coração bombeia hemocianina, o pigmento que colore seu sangue azul durante a circulação."
    chunks = split_caption_chunks(text, max_chars=28, max_lines=2)
    assert " ".join(chunks) == text
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(wrap_caption(chunk, max_chars=28).splitlines()) <= 2


def test_subtitle_cue_split_preserves_timing_and_token_coverage() -> None:
    cue = {
        "idx": 1,
        "start_ms": 1000,
        "end_ms": 5000,
        "text": "Cada coração bombeia hemocianina, o pigmento que colore seu sangue azul durante a circulação.",
    }
    items = orchestrator._split_subtitle_cue(cue, token_start=10, token_end=22)
    assert items[0]["start_ms"] == 1000
    assert items[-1]["end_ms"] == 5000
    assert items[0]["token_start"] == 10
    assert items[-1]["token_end"] == 22
    assert " ".join(item["text"] for item in items) == cue["text"]
    for item in items:
        assert len(wrap_caption(item["text"], max_chars=42).splitlines()) <= 2


def test_topic_plan_normalization_fills_missing_required_fields() -> None:
    request = SimpleNamespace(seed_theme="buracos negros", requested_angle=None)
    plan = {
        "tema": "Buracos Negros",
        "gancho": "o limite que muda tudo",
        "titulos": ["Buracos negros: o limite que muda tudo"],
    }

    normalized = orchestrator._normalize_topic_plan_payload(plan, request)

    assert normalized["canonical_topic"] == "Buracos Negros"
    assert normalized["angle"]
    assert normalized["hook_promise"] == "o limite que muda tudo"
    assert normalized["entities"] == ["Buracos Negros"]
    assert normalized["search_terms"]
    assert normalized["research_brief"]["focus_topic"] == "Buracos Negros"
    assert normalized["research_brief"]["primary_terms"]
    assert normalized["quality_metrics"]["editorial_mode"] == "viral_curiosidades"
    assert normalized["quality_metrics"]["topic_repair_used"] is True


def test_scene_plan_normalization_accepts_dict_wrapped_scene_list() -> None:
    from app.providers import MinimaxCreativeProvider

    provider = MinimaxCreativeProvider.__new__(MinimaxCreativeProvider)
    normalized = provider._normalize_scene_plan_payload({"scenes": [{"scene_id": "scene-1"}]})

    assert isinstance(normalized, list)
    assert normalized[0]["scene_id"] == "scene-1"


def test_scene_plan_normalization_accepts_nested_scene_list() -> None:
    from app.providers import MinimaxCreativeProvider

    provider = MinimaxCreativeProvider.__new__(MinimaxCreativeProvider)
    normalized = provider._normalize_scene_plan_payload({"data": {"plan": [{"scene_id": "scene-1", "narration_text": "abc"}]}})

    assert isinstance(normalized, list)
    assert normalized[0]["scene_id"] == "scene-1"


def test_plan_scenes_prefers_json_array_completion_when_available(monkeypatch) -> None:
    from app.providers import MinimaxCreativeProvider

    provider = MinimaxCreativeProvider.__new__(MinimaxCreativeProvider)
    calls: list[str] = []

    def fake_array_completion(_prompt: str):
        calls.append("array")
        return [{"scene_id": "scene-1", "narration_text": "abc"}]

    def fake_object_completion(_prompt: str):
        calls.append("object")
        return {"scenes": [{"scene_id": "scene-1", "narration_text": "abc"}]}

    provider._json_array_completion = fake_array_completion  # type: ignore[attr-defined]
    provider._json_completion = fake_object_completion  # type: ignore[method-assign]

    scenes = provider.plan_scenes({"full_narration": "abc", "estimated_duration_sec": 35}, 6)

    assert scenes[0]["scene_id"] == "scene-1"
    assert calls == ["array"]


def test_extract_fact_entity_prefers_subject_before_colon() -> None:
    entity = orchestrator._extract_fact_entity(
        "Polvos: curiosidades científicas sobre o cefalópode mais inteligente do oceano"
    )

    assert entity.lower() == "polvos"


def test_subtitle_split_enforces_word_limit_for_long_cues() -> None:
    cue = {
        "idx": 5,
        "start_ms": 1000,
        "end_ms": 5000,
        "text": "Mesmo assim, o buraco negro age como um corpo negro ideal, absorvendo toda a luz.",
    }

    items = orchestrator._split_subtitle_cue(cue, token_start=0, token_end=14)

    assert len(items) > 1
    assert " ".join(item["text"] for item in items) == cue["text"]
    for item in items:
        assert len(word_tokens(item["text"])) <= 14
        assert len(wrap_caption(item["text"], max_chars=42).splitlines()) <= 2


def test_subtitle_boundary_repair_moves_words_across_cues() -> None:
    items = [
        {"idx": 9, "start_ms": 20_000, "end_ms": 22_500, "text": "deixa oceanos profundos mais concreto para", "token_start": 40, "token_end": 45},
        {"idx": 10, "start_ms": 22_500, "end_ms": 25_000, "text": "quem assiste. Assim cada cena sustenta a", "token_start": 46, "token_end": 52},
        {"idx": 11, "start_ms": 25_000, "end_ms": 27_500, "text": "ideia sem inventar elemento aleatorio. Por", "token_start": 53, "token_end": 58},
        {"idx": 12, "start_ms": 27_500, "end_ms": 30_000, "text": "isso oceanos profundos deixa de ser so", "token_start": 59, "token_end": 65},
    ]

    repaired = orchestrator._repair_subtitle_item_boundaries(items)

    assert repaired[0]["text"].endswith("para quem")
    assert repaired[1]["text"].startswith("assiste.")
    assert repaired[2]["text"].endswith("Por isso")
    assert repaired[3]["text"].startswith("oceanos")
    assert SubtitleGate().validate(repaired, 1.0).passed


def test_subtitle_boundary_repair_can_push_weak_ending_into_next_chunk() -> None:
    items = [
        {"idx": "4.1", "start_ms": 8_002, "end_ms": 10_139, "text": "Isso significa que ele passa por qualquer fresta, se contorcendo ao máximo para", "token_start": 24, "token_end": 36},
        {"idx": "4.2", "start_ms": 10_139, "end_ms": 12_276, "text": "caber.", "token_start": 37, "token_end": 37},
    ]

    repaired = orchestrator._repair_subtitle_item_boundaries(items)

    assert repaired[0]["text"].endswith("máximo")
    assert repaired[1]["text"] == "para caber."
    assert SubtitleGate().validate(repaired, 1.0).passed


def test_subtitle_boundary_repair_can_pull_words_from_next_chunk() -> None:
    items = [
        {"idx": "6.2", "start_ms": 19_000, "end_ms": 20_500, "text": "predadores e muda de cor em", "token_start": 60, "token_end": 65},
        {"idx": "6.3", "start_ms": 20_500, "end_ms": 22_142, "text": "segundos.", "token_start": 66, "token_end": 66},
    ]

    repaired = orchestrator._repair_subtitle_item_boundaries(items)

    assert repaired[0]["text"] == "predadores e muda de cor em segundos."
    assert len(repaired) == 1
    assert SubtitleGate().validate(repaired, 1.0).passed


def test_speech_envelope_normalization_levels_caption_cues(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    srt_path = tmp_path / "voice.srt"
    sample_rate = 24_000
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for amp in [8000, 1200]:
            for idx in range(sample_rate):
                sample = int(amp * math.sin(2 * math.pi * 220 * idx / sample_rate))
                frames.extend(sample.to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nfrase alta\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nfrase baixa\n",
        encoding="utf-8",
    )
    LocalSpeechFallbackProvider()._normalize_speech_envelope(audio_path, srt_path, target_rms_db=-20.0)
    with wave.open(str(audio_path), "rb") as wav_file:
        width = wav_file.getsampwidth()
        first = wav_file.readframes(sample_rate)
        second = wav_file.readframes(sample_rate)
    first_rms = audioop.rms(first, width)
    second_rms = audioop.rms(second, width)
    ratio = max(first_rms, second_rms) / min(first_rms, second_rms)
    assert ratio < 1.15


def test_final_loudness_normalization_uses_ffmpeg_loudnorm(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "voice.wav"
    sample_rate = 24_000
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for idx in range(sample_rate):
            sample = int(1200 * math.sin(2 * math.pi * 220 * idx / sample_rate))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)

    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        shutil.copyfile(command[3], command[-1])
        return object()

    monkeypatch.setattr("app.providers.subprocess.run", fake_run)
    LocalSpeechFallbackProvider()._apply_final_loudness_normalization(audio_path)

    command_text = " ".join(captured["command"])
    assert "loudnorm=I=-16:LRA=11:TP=-1.5" in command_text
    assert audio_path.exists()


def test_tts_duration_fit_compresses_audio_and_subtitle_timings(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    srt_path = tmp_path / "voice.srt"
    sample_rate = 24_000
    duration_sec = 58
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for idx in range(sample_rate * duration_sec):
            sample = int(1200 * math.sin(2 * math.pi * 220 * idx / sample_rate))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:29,000\nprimeira metade\n\n"
        "2\n00:00:29,000 --> 00:00:58,000\nsegunda metade\n",
        encoding="utf-8",
    )

    result = orchestrator._fit_tts_duration(
        audio_path,
        srt_path,
        {"duration_ms": 58_000, "provider_metadata": {"mode": "edge"}},
    )
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))

    assert 53_500 <= result["duration_ms"] <= 54_500
    assert result["provider_metadata"]["duration_fit_applied"] is True
    assert cues[-1]["end_ms"] <= 54_600


def test_tts_duration_fit_expands_short_audio_and_subtitle_timings(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    srt_path = tmp_path / "voice.srt"
    sample_rate = 24_000
    duration_sec = 27
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for idx in range(sample_rate * duration_sec):
            sample = int(1200 * math.sin(2 * math.pi * 220 * idx / sample_rate))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:13,500\nprimeira metade\n\n"
        "2\n00:00:13,500 --> 00:00:27,000\nsegunda metade\n",
        encoding="utf-8",
    )

    result = orchestrator._fit_tts_duration(
        audio_path,
        srt_path,
        {"duration_ms": 27_000, "provider_metadata": {"mode": "edge"}},
    )
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))

    assert 35_500 <= result["duration_ms"] <= 36_500
    assert result["provider_metadata"]["duration_fit_applied"] is True
    assert 35_500 <= cues[-1]["end_ms"] <= 36_500


def test_scene_semantics_keeps_image_prompt_in_english() -> None:
    normalized = orchestrator._normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "primary_subject": "polvos",
            "image_prompt": "cinematic macro scene of an octopus changing skin texture",
            "fallback_queries": ["polvos"],
        },
        "polvos",
    )
    prompt = normalized["image_prompt"].lower()
    assert "octopuses" in prompt
    assert "polvos" not in prompt
    assert "no readable text anywhere" in prompt
    assert "no letters" in prompt
    assert "no logo" in prompt
    assert "no typography" in prompt
    assert "no text printed on objects" in prompt
    assert "blank packages" in prompt
    assert "sem texto" not in prompt


def test_scene_semantics_rebuilds_generic_portuguese_prompt_from_narration() -> None:
    normalized = orchestrator._normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "primary_subject": "Polvos",
            "narration_text": "Polvos possuem três corações e sangue azul.",
            "visual_intent": "subject_closeup",
            "image_prompt": "ilustracao vertical cinematografica de Polvos, mostrando subject closeup, sem texto",
            "fallback_queries": ["Polvos"],
        },
        "Polvos",
    )
    prompt = normalized["image_prompt"].lower()
    assert "three subtle hearts" in prompt
    assert "blue copper-rich blood vessels" in prompt
    assert "polvos" not in prompt
    assert "ilustracao" not in prompt
    assert "sem texto" not in prompt


def test_scene_semantics_adds_caffeine_specific_visuals_and_blank_objects() -> None:
    normalized = orchestrator._normalize_scene_semantics(
        {
            "scene_id": "scene-2",
            "primary_subject": "cafeina e foco",
            "narration_text": "A cafeina ocupa receptores de adenosina e reduz a sonolencia por alguns minutos.",
            "visual_intent": "process_or_mechanism",
            "image_prompt": "vertical cinematic image of coffee, no readable text anywhere",
            "fallback_queries": ["cafeina foco"],
        },
        "cafeina e foco",
    )
    prompt = normalized["image_prompt"].lower()
    assert "caffeine molecules" in prompt
    assert "adenosine receptors" in prompt
    assert "plain unbranded" in prompt or "blank cups" in prompt
    assert "no text on cups" in prompt
    assert "no labels or lettering on any object surface" in prompt
    assert "cafeina" not in prompt


def test_scene_semantics_translates_long_caffeine_topic_to_english_subject() -> None:
    normalized = orchestrator._normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "primary_subject": "Cafeína e foco: a ciência por trás do efeito do café na concentração matinal",
            "narration_text": "Cafeína e foco dependem da adenosina pela manhã.",
            "visual_intent": "subject_closeup",
            "image_prompt": "soft morning coffee scene, no readable text anywhere",
            "fallback_queries": ["cafeina foco"],
        },
        "Cafeína e foco",
    )
    prompt = normalized["image_prompt"].lower()
    assert "central subject: caffeine and focus" in prompt
    assert "cafeína" not in prompt
    assert "café" not in prompt


def test_scene_semantics_rebuilds_generic_cat_prompt_from_narration() -> None:
    normalized = orchestrator._normalize_scene_semantics(
        {
            "scene_id": "scene-3",
            "primary_subject": "gatos",
            "narration_text": "Gatos giram cada orelha até 180 graus para captar sons.",
            "visual_intent": "process_or_mechanism",
            "image_prompt": (
                "vertical cinematic scientific illustration of gatos, showing process or mechanism, "
                "focused on the described phenomenon"
            ),
            "fallback_queries": ["gatos"],
        },
        "gatos",
    )
    prompt = normalized["image_prompt"].lower()
    assert "cat ears rotating independently" in prompt
    assert "sound waves" in prompt
    assert "gatos" not in prompt
    assert "focused on the described phenomenon" not in prompt


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


def test_script_gate_rejects_overconfident_or_unsupported_pisa_claims() -> None:
    script = {
        "title": "Torre de Pisa não cai: o segredo revelado",
        "hook": "Ela deveria ter tombado há séculos.",
        "body_beats": [
            "Uma engenharia ridiculamente simples: sapatas de concreto compensaram a inclinação.",
            "Um túnel sob a base permitiu corrigir apenas 4 centímetros.",
        ],
        "ending": "A Torre de Pisa não cai porque a inclinação a sustenta.",
        "cta": None,
        "full_narration": (
            "Ela deveria ter tombado há séculos. Mas a Torre de Pisa não cai. "
            "Uma engenharia ridiculamente simples: sapatas de concreto compensaram a inclinação. "
            "Um túnel sob a base permitiu corrigir apenas 4 centímetros. "
            "A física prova: inclinação não é queda. A Torre de Pisa não cai porque a inclinação a sustenta."
        ),
        "estimated_duration_sec": 30,
        "key_facts": ["Sapatas de concreto compensaram a inclinação."],
        "token_count": 58,
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.95,
            "clarity_score": 0.95,
            "information_density_score": 0.9,
            "repetition_score": 0.1,
            "ending_strength_score": 0.9,
        },
    }

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "overconfident_or_unsupported_factual_claim" in result.reasons


def test_asset_extension_is_normalized_to_actual_file_format(tmp_path: Path) -> None:
    wrong_path = tmp_path / "ai.png"
    from PIL import Image

    Image.new("RGB", (32, 48), "white").save(wrong_path, format="JPEG")
    asset = {"uri": wrong_path.resolve().as_uri(), "provider": "test", "prompt_snapshot": "prompt"}

    normalized = orchestrator._normalize_asset_uri_extension(asset)

    normalized_path = Path(normalized["uri"].replace("file://", ""))
    assert normalized_path.suffix == ".jpg"
    assert normalized_path.exists()
    assert not wrong_path.exists()
    assert normalized["file_format"] == "jpeg"
    assert normalized["extension_normalized"] is True


def test_publish_package_skips_stopword_hashtags() -> None:
    tags = ["#shorts", "#curiosidades", "#ciencia"]
    tag_stopwords = {"por", "que", "qual", "como", "porque", "para", "com", "uma", "um", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e"}
    added = 0
    for token in ["Por", "que", "a", "Torre", "de", "Pisa", "não", "cai"]:
        normalized = token.lower()
        if len(normalized) < 3 or normalized in tag_stopwords:
            continue
        tags.append(f"#{normalized}")
        added += 1
        if added >= 3:
            break

    assert tags == ["#shorts", "#curiosidades", "#ciencia", "#torre", "#pisa", "#não"]
    assert "#por" not in tags
    assert "#que" not in tags


def test_build_publish_hashtags_prefers_specific_non_weak_tags() -> None:
    topic_plan = SimpleNamespace(
        canonical_topic="Dinossauros: por que o Tiranossauro rex tinha braços tão pequenos",
        angle="curiosidade visual sobre função real dos braços",
    )
    script = SimpleNamespace(
        title="Dinossauros: por que os braços do T. rex não eram inúteis",
        key_facts=["Os braços do T. rex eram pequenos, mas musculosos."],
    )

    tags = orchestrator.monetization_pipeline.build_publish_hashtags(topic_plan, script)

    assert tags[0] == "#shorts"
    assert "#fatos" in tags
    assert "#curiosidades" not in tags
    assert "#tinha" not in tags
    assert any(tag in tags for tag in {"#dinossauros", "#tiranossauro", "#trex", "#paleontologia"})


def test_human_review_checklist_marks_required_completed_and_pending_items() -> None:
    checklist = build_human_review_checklist(
        rights_registry={"all_commercial_rights_confirmed": False},
        ai_disclosure={"youtube_disclosure_required": True},
        fact_claims_report={"requires_fact_review": False},
        metadata_review={"requires_metadata_review": True},
        channel_repetition_report={"repetition_risk": "medium"},
        publish_audit_required=False,
        confirmations={"rights_confirmed", "originality_confirmed"},
    )

    assert checklist["all_required_completed"] is False
    assert "rights_confirmation_required" in checklist["completed_codes"]
    assert "youtube_ai_disclosure_toggle_required" in checklist["pending_codes"]
    assert "metadata_review_required" in checklist["pending_codes"]
    assert "originality_review_required" in checklist["completed_codes"]
    assert "fact_review_required" not in checklist["required_codes"]


def test_human_review_checklist_auto_completes_channel_ai_disclosure() -> None:
    checklist = build_human_review_checklist(
        rights_registry={"all_commercial_rights_confirmed": True},
        ai_disclosure={"youtube_disclosure_required": True, "auto_confirmed": True},
        fact_claims_report={"requires_fact_review": False},
        metadata_review={"requires_metadata_review": False},
        channel_repetition_report={"repetition_risk": "low"},
        publish_audit_required=False,
        confirmations=set(),
    )

    assert "youtube_ai_disclosure_toggle_required" in checklist["completed_codes"]
    assert "youtube_ai_disclosure_toggle_required" not in checklist["pending_codes"]
    disclosure_item = next(item for item in checklist["items"] if item["code"] == "youtube_ai_disclosure_toggle_required")
    assert disclosure_item["auto_completed"] is True


def test_human_review_checklist_includes_publish_audit_confirmation() -> None:
    checklist = build_human_review_checklist(
        rights_registry={"all_commercial_rights_confirmed": True},
        ai_disclosure={"youtube_disclosure_required": False},
        fact_claims_report={"requires_fact_review": False},
        metadata_review={"requires_metadata_review": False},
        channel_repetition_report={"repetition_risk": "low"},
        publish_audit_required=True,
        confirmations=set(),
    )

    assert "publish_audit_required" in checklist["pending_codes"]
    item = next(item for item in checklist["items"] if item["code"] == "publish_audit_required")
    assert item["confirmation_code"] == "publish_audit_confirmed"


def test_conservative_ai_disclosure_requires_toggle_for_any_synthetic_asset(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "conservative_synthetic_disclosure", True)
    monkeypatch.setattr(orchestrator.settings, "channel_ai_generated_content", False)
    report = orchestrator._build_ai_disclosure_report(
        [
            SimpleNamespace(
                provider="mock",
                scene_id="scene-1",
                prompt_snapshot="abstract underwater texture without people",
            )
        ]
    )

    assert report["youtube_disclosure_required"] is True
    assert report["auto_confirmed"] is False


def test_channel_ai_generated_content_auto_confirms_disclosure(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "conservative_synthetic_disclosure", True)
    monkeypatch.setattr(orchestrator.settings, "channel_ai_generated_content", True)
    report = orchestrator._build_ai_disclosure_report(
        [
            SceneAsset(
                asset_id="asset-ai-auto",
                job_id="job",
                scene_id="scene-1",
                content_hash="hash",
                kind="image",
                provider="minimax",
                uri="file:///tmp/a.png",
                width=1080,
                height=1920,
                prompt_snapshot="cinematic documentary image",
                scores={"semantic_match": 0.9, "aesthetic_score": 0.9, "technical_score": 0.9},
                selected=True,
            )
        ]
    )

    assert report["youtube_disclosure_required"] is True
    assert report["auto_confirmed"] is True
    assert report["confirmation_mode"] == "channel_policy"
    assert report["policy_mode"] == "conservative"


def test_fact_pack_rejects_verified_source_for_wrong_primary_topic(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "use_mock_providers", False)

    def fake_article_pack(query: str) -> dict:
        return {
            "status": "verified",
            "provider": "openalex",
            "query_used": query,
            "topic_title": "Google Earth Engine: Planetary-scale geospatial analysis for everyone",
            "facts": [
                {
                    "fact_id": "F1",
                    "claim": "Google Earth Engine is a cloud-based platform for planetary-scale geospatial analysis.",
                    "source_id": "S1",
                }
            ],
            "sources": [{"source_id": "S1", "title": "Google Earth Engine: Planetary-scale geospatial analysis for everyone"}],
        }

    monkeypatch.setattr(pipeline, "_scientific_article_fact_pack", fake_article_pack)
    topic_plan = SimpleNamespace(
        canonical_topic="YouTube como plataforma dominante em vídeo",
        angle="Comparar YouTube com Google e TikTok sem trocar a entidade principal.",
        hook_promise="Mostrar um dado verificável sobre YouTube.",
        search_terms=["Google", "YouTube crescimento"],
        entities=["YouTube", "Google", "TikTok"],
        title_candidates=["YouTube é maior do que você imagina"],
    )
    request = SimpleNamespace(seed_theme="Por que YouTube está chamando atenção?")

    report = pipeline._build_fact_pack(topic_plan, request)

    assert report["status"] == "limited"
    assert report["topic_alignment"]["passed"] is False


def test_script_pipeline_requires_verified_fact_pack_for_factual_real_topics(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "use_mock_providers", False)
    topic_plan = SimpleNamespace(
        canonical_topic="Suplemento para ansiedade muda o cérebro?",
        angle="como um suplemento pode afetar sintomas de ansiedade",
        hook_promise="explicar o mecanismo biologico com fontes",
    )
    request = SimpleNamespace(seed_theme="Suplemento para ansiedade funciona mesmo?", notes="[factual_strict]", requested_angle=None)

    assert pipeline._requires_verified_fact_pack(topic_plan, request, {"status": "limited", "facts": []}) is True
    assert pipeline._requires_verified_fact_pack(topic_plan, request, {"status": "verified", "facts": [{"fact_id": "F1"}]}) is False


def test_script_pipeline_defaults_curiosidades_to_viral_mode() -> None:
    pipeline = orchestrator.script_pipeline
    topic_plan = SimpleNamespace(
        canonical_topic="Por que os polvos mudam de cor",
        angle="o truque visual que parece impossível",
        hook_promise="o detalhe que faz a pele do polvo sumir no cenário",
        quality_metrics={},
    )
    request = SimpleNamespace(seed_theme="Como os polvos mudam de cor", notes=None, requested_angle=None)

    assert pipeline._editorial_mode(topic_plan, request) == "viral_curiosidades"
    assert pipeline._topic_requires_verified_fact_pack(topic_plan, request) is False


def test_step_script_disables_simple_prompt_rules_when_fact_pack_is_required(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", True)
    monkeypatch.setattr(pipeline.settings, "use_mock_providers", False)
    captured: dict[str, object] = {}
    job_id = orchestrator.create_job(
        {
            "seed_theme": "por que cafe tira o sono",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 45,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "[factual_strict] teste",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="cafeina e sono",
                angle="desconstrucao de crenca popular",
                hook_promise="revela o mecanismo biologico real",
                entities=["cafeina", "adenosina"],
                search_terms=["cafeina sono"],
                title_candidates=["Cafe nao te da energia"],
                quality_metrics={"editorial_mode": "factual_strict"},
            )
        )
        session.commit()
        job = session.get(Job, job_id)
        assert job is not None

        monkeypatch.setattr(
            pipeline,
            "_build_fact_pack",
            lambda *_args, **_kwargs: {"status": "verified", "facts": [{"fact_id": "F1", "claim": "Cafeina bloqueia receptores de adenosina."}]},
        )

        def fake_generate_script(topic_plan: dict[str, object]) -> dict[str, object]:
            captured.update(topic_plan)
            return {
                "title": "Cafe nao te da energia",
                "hook": "Cafe mascara o cansaco.",
                "body_beats": ["Cafeina bloqueia receptores de adenosina."],
                "ending": "Na segunda olhada, o começo vira pista.",
                "cta": None,
                "full_narration": "Cafe mascara o cansaco. Cafeina bloqueia receptores de adenosina. Na segunda olhada, o começo vira pista.",
                "estimated_duration_sec": 35,
                "key_facts": ["Cafeina bloqueia receptores de adenosina."],
                "source_fact_ids": ["F1"],
                "claim_trace": [{"text": "Cafeina bloqueia receptores de adenosina.", "source_fact_ids": ["F1"], "grounding": "fact_pack"}],
                "token_count": 15,
                "language": "pt-BR",
                "qa_metrics": {},
                "retention_map": {},
                "visual_opening": {},
                "prompt_version": "test",
            }

        monkeypatch.setattr(orchestrator.providers.creative, "generate_script", fake_generate_script)
        monkeypatch.setattr(
            pipeline,
            "_validate_or_repair_script",
            lambda script, *_args, **_kwargs: (script, {"script_quality_gate_pass": True, "fact_pack_consistency_pass": True}),
        )
        monkeypatch.setattr(pipeline, "_text_publish_audit", lambda *_args, **_kwargs: {"passed": True, "reasons": []})

        pipeline.step_script(session, job, 1)

    assert captured["simple_shorts_mode"] is False
    assert captured["editorial_mode"] == "factual_strict"


def test_step_script_uses_ready_script_without_llm_generation(monkeypatch) -> None:
    from app.manual_script import build_ready_script_notes

    pipeline = orchestrator.script_pipeline
    ready_script = """Título: Venus: o planeta onde um dia dura mais que um ano
Hook: 243 dias para girar uma vez, mas só 225 para orbitar o Sol.
Loop: Como um planeta pode envelhecer antes de terminar o próprio dia?
Beats: Em Venus, o relógio não acompanha o calendário.
O planeta gira tão devagar que o Sol parece quase travado.
Enquanto isso, ele completa uma volta inteira ao redor do Sol.
Payoff: O dia venusiano é maior que o ano venusiano.
Fechamento: Em Venus, aniversário chega antes do pôr do sol."""
    job_id = orchestrator.create_job(
        {
            "seed_theme": "Venus: o planeta onde um dia dura mais que um ano",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": build_ready_script_notes(None, ready_script, True),
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="Venus: dia maior que ano",
                angle="curiosidade astronomica contraintuitiva",
                hook_promise="explica por que o dia venusiano passa do ano venusiano",
                entities=["Venus"],
                search_terms=["Venus dia ano"],
                title_candidates=["Venus: o planeta onde um dia dura mais que um ano"],
                quality_metrics={"editorial_mode": "viral_curiosidades"},
            )
        )
        session.commit()
        job = session.get(Job, job_id)
        assert job is not None

        monkeypatch.setattr(orchestrator.providers.creative, "generate_script", lambda _plan: (_ for _ in ()).throw(AssertionError("should not generate script")))
        monkeypatch.setattr(
            pipeline,
            "_validate_or_repair_script",
            lambda script, *_args, **_kwargs: (
                script,
                {"script_quality_gate_pass": True, "fact_pack_consistency_pass": True, "ready_script": True},
            ),
        )
        monkeypatch.setattr(pipeline, "_text_publish_audit", lambda *_args, **_kwargs: {"passed": True, "reasons": []})

        artifacts = pipeline.step_script(session, job, 1)
        session.flush()
        script = session.scalar(select(Script).where(Script.job_id == job_id))

    assert script is not None
    assert script.title == "Venus: o planeta onde um dia dura mais que um ano"
    assert not script.full_narration.startswith(script.title)
    assert "243 dias para girar" in script.full_narration
    assert "ready_script_input.json" in artifacts


def test_ready_script_declared_fact_check_accepts_grounded_precise_numbers(monkeypatch) -> None:
    from app.manual_script import parse_ready_script

    ready_script = """Título: Venus: o planeta onde um dia dura mais que um ano
Hook: 243 dias para girar uma vez, mas só 225 para orbitar o Sol.
Loop: Como um planeta pode “envelhecer” antes de terminar o próprio dia?
Beats: Em Venus, o relógio não acompanha o calendário.
O planeta gira tão devagar que o Sol parece quase travado.
Enquanto isso, ele completa uma volta inteira ao redor do Sol.
Imagine esperar meses por um nascer do sol esmagado por nuvens ácidas.
A ideia de “dia” simplesmente quebra lá.
Payoff: O dia venusiano é maior que o ano venusiano. A rotação demora 243 dias terrestres; a órbita, 225.
Fechamento: Em Venus, aniversário chega antes do pôr do sol."""
    ready = parse_ready_script(ready_script, fact_check_confirmed=True)
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(
        orchestrator.providers.creative,
        "repair_script",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ready script should not call LLM repair")),
    )

    script, metrics = pipeline._validate_or_repair_script(
        ready.script,
        {
            "fact_pack": ready.fact_pack,
            "ready_script_mode": True,
            "ready_script_fact_check_confirmed": True,
            "canonical_topic": "Venus: dia maior que ano",
        },
        45,
        "none",
        "ready-script-test",
    )

    assert metrics["ready_script_declared_fact_check_accepted"] is True
    assert metrics["script_quality_gate_pass"] is True
    assert "script_quality_gate_warnings" in metrics
    assert "243 dias para girar" in script["full_narration"]
    assert "Como um planeta pode" in script["full_narration"]
    assert "“" not in script["full_narration"]
    assert script["claim_trace"]


def test_ready_script_preserves_author_closing_without_auto_repair(monkeypatch) -> None:
    from app.manual_script import parse_ready_script

    ready_script = """Título: Água-viva imortal: o animal que pode reiniciar a vida
Hook: Turritopsis não foge da velhice; ela aperta “voltar”.
Loop: O que acontece quando morrer deixa de ser a única saída?
Beats: Essa água-viva começa adulta, frágil e transparente.
Quando sofre estresse, ela pode inverter o próprio ciclo.
O corpo adulto volta a uma fase jovem, como uma planta marinha grudada.
É como ver uma borboleta virar lagarta de novo.
A natureza não criou imortalidade mística; criou um botão biológico.
Payoff: A Turritopsis dohrnii pode voltar de medusa para pólipo, reiniciando sua fase de vida.
Fechamento: Ela não vence o tempo; ela sai da partida.
Hashtags: #curiosidades #facts #natureza #didyouknow #shorts"""
    ready = parse_ready_script(ready_script, fact_check_confirmed=True)
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(
        pipeline,
        "_postprocess_script_for_quality",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ready script must not be postprocessed")),
    )
    monkeypatch.setattr(
        orchestrator.providers.creative,
        "repair_script",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ready script must not call LLM repair")),
    )

    script, metrics = pipeline._validate_or_repair_script(
        ready.script,
        {
            "fact_pack": ready.fact_pack,
            "ready_script_mode": True,
            "ready_script_fact_check_confirmed": True,
            "canonical_topic": "Turritopsis dohrnii",
        },
        45,
        "none",
        "ready-script-preserve-test",
    )

    assert script["ending"] == "Ela não vence o tempo; ela sai da partida."
    assert script["full_narration"].endswith("Ela não vence o tempo; ela sai da partida.")
    assert "Volta para a primeira imagem" not in script["full_narration"]
    assert metrics["ready_script_preserved"] is True
    assert metrics["script_auto_repair_skipped"] is True
    assert metrics["script_quality_gate_warnings"] == ["ending_not_connected_to_hook"]
    assert metrics["script_repair_attempts_log"] == [
        {
            "repair_attempt": 0,
            "reason_codes": ["ending_not_connected_to_hook"],
            "passed": True,
            "used_fallback": False,
            "repair_strategy": "ready_script_preserve",
        }
    ]


def test_job_lease_delta_has_floor_for_real_provider_steps(monkeypatch) -> None:
    test_orchestrator = JobOrchestrator()
    monkeypatch.setattr(test_orchestrator.settings, "job_lease_seconds", 60)

    assert test_orchestrator._lease_delta().total_seconds() == 300


def test_script_gate_blocks_placeholder_source_language() -> None:
    script = {
        "title": "YouTube é maior do que você imagina",
        "hook": "Youtube como parece exagero, mas a fonte aponta um mecanismo real.",
        "body_beats": ["A fonte sustenta um detalhe verificável sobre youtube como, sem precisar inflar o fato."],
        "ending": "Na segunda olhada, a primeira frase já apontava para O mecanismo real aparece.",
        "full_narration": (
            "Youtube como parece exagero, mas a fonte aponta um mecanismo real. "
            "A fonte sustenta um detalhe verificável sobre youtube como, sem precisar inflar o fato. "
            "Na segunda olhada, a primeira frase já apontava para O mecanismo real aparece. "
            "quando youtube como deixa de ser só aparência."
        ),
        "key_facts": [],
        "language": "pt-BR",
        "estimated_duration_sec": 25,
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.9,
            "ending_strength_score": 0.9,
            "repetition_score": 0.1,
        },
    }

    result = ScriptQualityGate().validate(script, 35)

    assert result.passed is False
    assert "placeholder_source_language" in result.reasons
    assert "truncated_ending_logic" in result.reasons


def test_script_gate_blocks_conservative_filler_visible_text() -> None:
    script = _base_script(
        "Em geral, depressão danakil mostra uma escala incomum, sem depender de número exato. "
        "O povo Afar vive em uma região extrema de sal, calor e atividade geotérmica. "
        "A cena inicial muda quando você percebe que esse lugar também é casa."
    )
    script["hook"] = "Em geral, depressão danakil mostra uma escala incomum, sem depender de número exato."

    result = ScriptQualityGate().validate(script, 45)

    assert result.passed is False
    assert "placeholder_source_language" in result.reasons


def test_simple_shorts_mode_blocks_critical_script_warnings(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", True)
    script = _base_script(
        "Em geral, depressão danakil mostra uma escala incomum, sem depender de número exato. "
        "O povo Afar vive em uma região extrema de sal, calor e atividade geotérmica. "
        "A cena inicial muda quando você percebe que esse lugar também é casa."
    )
    plan_dict = {"canonical_topic": "depressão de danakil", "fact_pack": {"status": "skipped", "facts": []}}

    try:
        orchestrator._validate_or_repair_script(script, plan_dict, 45, "none")
    except RecoverableStepError as exc:
        assert "placeholder_source_language" in str(exc)
    else:
        raise AssertionError("expected simple mode to block critical script warning")


def test_publish_audit_failures_become_automatic_hard_blockers() -> None:
    blockers = orchestrator.monetization_pipeline.automatic_publish_blockers(
        {
            "passed": False,
            "reasons": [
                "source_fact_mismatch",
                "unsupported_claim",
                "claim_trace_grounding_missing",
                "rights_confirmation_required",
                "low_retention_hook",
            ],
        }
    )

    assert blockers == ["source_fact_mismatch", "unsupported_claim", "claim_trace_grounding_missing", "low_retention_hook"]


def test_text_publish_audit_has_hard_timeout(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", False)
    monkeypatch.setattr(pipeline.settings, "llm_publish_audit_timeout_sec", 0.01)

    def slow_auditor(payload: dict) -> dict:
        time.sleep(0.2)
        return {"passed": True, "reasons": []}

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", slow_auditor)

    audit = pipeline._text_publish_audit(
        "job-timeout",
        {"title": "Teste", "hook": "Teste", "ending": "Teste", "full_narration": "Teste."},
        {"status": "limited", "facts": []},
    )

    assert audit["passed"] is False
    assert audit["reasons"] == ["text_publish_audit_timeout"]


def test_text_publish_audit_allows_resilient_fallback_window(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", False)
    monkeypatch.setattr(pipeline.settings, "llm_publish_audit_timeout_sec", 0.01)

    class ResilientLikeAuditor:
        fallback = object()
        strict_minimax_validation = False

        def audit_publish_package(self, payload: dict) -> dict:
            time.sleep(0.02)
            return {"passed": True, "reasons": [], "provider": "deepseek", "fallback_used": True}

    monkeypatch.setattr(orchestrator.providers, "creative", ResilientLikeAuditor())

    audit = pipeline._text_publish_audit(
        "job-fallback-window",
        {"title": "Teste", "hook": "Teste", "ending": "Teste forte agora", "full_narration": "Teste forte agora."},
        {"status": "verified", "facts": []},
    )

    assert audit["passed"] is True
    assert audit["fallback_used"] is True


def test_text_publish_audit_ignores_early_weak_hashtags(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", False)

    def auditor(payload: dict) -> dict:
        assert payload["audit_phase"] == "text_before_assets"
        return {"passed": False, "reasons": ["weak_hashtags"], "provider": "test"}

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", auditor)

    audit = pipeline._text_publish_audit(
        "job-weak-hashtags",
        {"title": "Mel", "hook": "Mel age contra bactérias", "ending": "Agora o pote parece diferente.", "full_narration": "Mel age contra bactérias."},
        {"status": "verified", "facts": [{"fact_id": "F1"}]},
    )

    assert audit["passed"] is True
    assert audit["reasons"] == []
    assert audit["ignored_reasons"] == ["weak_hashtags"]


def test_simple_shorts_mode_skips_text_publish_audit(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", True)

    def auditor(payload: dict) -> dict:
        raise AssertionError("auditor should be skipped in simple shorts mode")

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", auditor)

    audit = pipeline._text_publish_audit(
        "job-simple-audit",
        {"title": "Teste", "hook": "Teste", "ending": "Final forte", "full_narration": "Teste simples."},
        {"status": "skipped", "facts": []},
    )

    assert audit == {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True}


def test_ready_script_declared_fact_check_skips_text_publish_audit(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", False)

    def auditor(payload: dict) -> dict:
        raise AssertionError("ready script fact confirmation should not call publish auditor")

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", auditor)

    audit = pipeline._text_publish_audit(
        "job-ready-script-audit",
        {"title": "Venus", "hook": "243 dias", "ending": "Final forte", "full_narration": "243 dias."},
        {
            "status": "verified",
            "provider": "user_declared_fact_check",
            "facts": [{"fact_id": "D1", "claim": "243 dias.", "source_id": "USER_DECLARED_FACT_CHECK"}],
        },
    )

    assert audit == {
        "passed": True,
        "reasons": [],
        "provider": "user_declared_fact_check",
        "skipped": True,
        "scope": "ready_script_human_fact_confirmation",
    }


def test_simple_shorts_mode_runs_text_publish_audit_for_verified_fact_pack(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
    monkeypatch.setattr(pipeline.settings, "simple_shorts_mode", True)
    captured: dict[str, object] = {}

    def auditor(payload: dict) -> dict:
        captured.update(payload)
        return {"passed": True, "reasons": [], "provider": "test"}

    monkeypatch.setattr(orchestrator.providers.creative, "audit_publish_package", auditor)

    audit = pipeline._text_publish_audit(
        "job-simple-audit-verified",
        {"title": "Cafe", "hook": "Cafe muda seu alerta.", "ending": "Agora a ultima frase fecha o loop.", "full_narration": "Cafe muda seu alerta."},
        {"status": "verified", "facts": [{"fact_id": "F1", "claim": "Cafeina bloqueia receptores de adenosina."}]},
    )

    assert audit["passed"] is True
    assert audit["provider"] == "test"
    assert captured["audit_phase"] == "text_before_assets"


def test_simple_shorts_mode_publish_readiness_requires_manual_publish_audit(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    readiness = orchestrator._publish_readiness_report(
        None,
        SimpleNamespace(canonical_topic="paisagens extremas", angle="curiosidades visuais"),
        {"status": "skipped", "facts": []},
        ["#shorts", "#curiosidades", "#paisagens"],
        {
            "script_gate_pass": True,
            "scene_plan_gate_pass": True,
            "asset_gate_pass": True,
            "subtitle_gate_pass": True,
            "render_gate_pass": True,
        },
        {
            **_base_script("Paisagens extremas parecem de outro planeta."),
            "claim_trace": [{"text": "Paisagens extremas parecem de outro planeta.", "source_fact_ids": [], "grounding": "conservative"}],
        },
        {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True},
    )

    assert readiness["passed"] is False
    assert readiness["reasons"] == ["text_publish_audit_skipped"]


def test_simple_shorts_mode_publish_readiness_blocks_factual_topic_without_grounding(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    readiness = orchestrator._publish_readiness_report(
        None,
        SimpleNamespace(canonical_topic="Suplemento para ansiedade funciona mesmo", angle="saude", quality_metrics={"editorial_mode": "factual_strict"}),
        {"status": "skipped", "facts": []},
        ["#shorts", "#ciencia", "#cerebro"],
        {
            "script_gate_pass": True,
            "scene_plan_gate_pass": True,
            "asset_gate_pass": True,
            "subtitle_gate_pass": True,
            "render_gate_pass": True,
        },
        {
            **_base_script("A cafeina bloqueia os receptores de adenosina no cerebro."),
            "source_fact_ids": [],
            "claim_trace": [
                {
                    "text": "A cafeina bloqueia os receptores de adenosina no cerebro.",
                    "source_fact_ids": [],
                    "grounding": "missing",
                }
            ],
        },
        {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True},
    )

    assert readiness["passed"] is False
    assert "text_publish_audit_skipped" in readiness["reasons"]
    assert "fact_pack_missing_for_factual_topic" in readiness["reasons"]
    assert "claim_trace_grounding_missing" in readiness["reasons"]


def test_simple_shorts_mode_publish_readiness_keeps_viral_curiosidades_lightweight(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    readiness = orchestrator._publish_readiness_report(
        None,
        SimpleNamespace(canonical_topic="Por que os flamingos ficam rosa", angle="curiosidade visual", quality_metrics={"editorial_mode": "viral_curiosidades"}),
        {"status": "skipped", "facts": []},
        ["#shorts", "#curiosidades", "#flamingos"],
        {
            "script_gate_pass": True,
            "scene_plan_gate_pass": True,
            "asset_gate_pass": True,
            "subtitle_gate_pass": True,
            "render_gate_pass": True,
        },
        {
            **_base_script("Flamingos parecem pintados, mas o segredo começa no prato."),
            "source_fact_ids": [],
            "claim_trace": [{"text": "Flamingos parecem pintados.", "source_fact_ids": [], "grounding": "conservative"}],
        },
        {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True},
    )

    assert readiness["passed"] is False
    assert readiness["reasons"] == ["text_publish_audit_skipped"]


def test_simple_shorts_mode_makes_script_gate_non_blocking(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", True)
    script = _base_script(
        "Você sabia que café tira o sono? "
        "A cafeína muda sua energia. "
        "Agora tudo faz sentido."
    )
    plan_dict = {"canonical_topic": "café", "fact_pack": {"status": "skipped", "facts": []}}

    processed, metrics = orchestrator._validate_or_repair_script(script, plan_dict, 35, "none")

    assert processed["full_narration"]
    assert metrics["script_quality_gate_pass"] is True
    assert metrics["script_quality_gate_blocking"] is False
    assert metrics["simple_shorts_mode"] is True


def test_simple_shorts_mode_runs_local_repair_for_soft_warnings(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", True)
    script = _base_script("O tema parece simples. Depois a mesma pista fecha melhor no fim.")
    plan_dict = {"canonical_topic": "tiranossauro rex", "fact_pack": {"status": "skipped", "facts": []}}

    def fake_validate(candidate: dict[str, object], _target_duration_sec: int):
        if candidate.get("ending") == "Agora o começo fecha melhor no fim.":
            return SimpleNamespace(
                passed=True,
                reasons=[],
                metrics={"fact_risk": {"blocked": False, "claim_count": 0}},
            )
        return SimpleNamespace(
            passed=False,
            reasons=["weak_loop_closure", "factual_claim_trace_missing"],
            metrics={"fact_risk": {"blocked": False, "claim_count": 1}},
        )

    def fake_postprocess(candidate: dict[str, object], _plan_dict: dict[str, object], gate_reasons: list[str]):
        if not gate_reasons:
            return dict(candidate)
        assert "weak_loop_closure" in gate_reasons
        updated = dict(candidate)
        updated["ending"] = "Agora o começo fecha melhor no fim."
        return updated

    monkeypatch.setattr(orchestrator.script_pipeline.script_gate, "validate", fake_validate)
    monkeypatch.setattr(orchestrator.script_pipeline, "_postprocess_script_for_quality", fake_postprocess)
    monkeypatch.setattr(orchestrator.script_pipeline, "_fact_pack_consistency_reasons", lambda *_args, **_kwargs: [])

    processed, metrics = orchestrator._validate_or_repair_script(script, plan_dict, 45, "none")

    assert processed["ending"] == "Agora o começo fecha melhor no fim."
    assert metrics["script_quality_gate_warnings"] == []
    assert metrics["script_repair_attempts_log"][1]["repair_strategy"] == "simple_mode_local"


def test_simple_shorts_mode_verified_fact_pack_keeps_script_gate_blocking(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", True)
    monkeypatch.setattr(orchestrator.settings, "llm_script_repair_attempts", 0)
    monkeypatch.setattr(
        orchestrator.script_pipeline.script_gate,
        "validate",
        lambda *_args, **_kwargs: SimpleNamespace(
            passed=False,
            reasons=["factual_claim_trace_missing"],
            metrics={"fact_risk": {"blocked": False, "claim_count": 1}},
        ),
    )
    monkeypatch.setattr(orchestrator.script_pipeline, "_fact_pack_consistency_reasons", lambda *_args, **_kwargs: [])
    script = _base_script("Cafe muda seu estado de alerta. Na segunda olhada, a primeira frase vira pista.")
    plan_dict = {
        "canonical_topic": "cafe",
        "fact_pack": {
            "status": "verified",
            "facts": [{"fact_id": "F1", "claim": "Cafeina interage com receptores de adenosina."}],
        },
    }

    try:
        orchestrator._validate_or_repair_script(script, plan_dict, 35, "none")
    except RecoverableStepError as exc:
        assert "factual_claim_trace_missing" in str(exc)
    else:
        raise AssertionError("expected RecoverableStepError")


def test_build_monetization_report_turns_skipped_publish_audit_into_manual_review(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    job_id = orchestrator.create_job(
        {
            "seed_theme": "paisagens extremas",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 45,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="paisagens extremas",
                angle="curiosidades visuais",
                hook_promise="paisagens de outro planeta",
                entities=["paisagens"],
                search_terms=["paisagens extremas"],
                title_candidates=["Paisagens extremas parecem irreais"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script",
                title="Paisagens extremas parecem irreais",
                hook="Paisagens extremas parecem irreais.",
                body_beats=[],
                ending="No fim, parece outro planeta.",
                cta=None,
                full_narration="Paisagens extremas parecem irreais.",
                estimated_duration_sec=35,
                key_facts=[],
                token_count=5,
                language="pt-BR",
                qa_metrics={"script_quality_gate_pass": True},
            )
        )
        job = session.get(Job, job_id)
        assert job is not None
        job.quality_summary = {
            "script": {"script_quality_gate_pass": True},
            "scene_plan": {"scene_plan_gate_pass": True},
            "assets": {"semantic_threshold_pass": True},
            "subtitles": {"subtitle_gate_pass": True},
            "render": {"render_gate_pass": True},
        }
        session.commit()

    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "publish_readiness_report",
        lambda *args, **kwargs: {
            "passed": False,
            "reasons": ["text_publish_audit_skipped"],
            "fact_pack_status": "skipped",
            "hashtag_count": 3,
            "weak_hashtags": [],
            "fact_risk": {"blocked": False, "claim_count": 0, "simple_shorts_mode": True},
            "minimax_audit": {"passed": True, "skipped": True},
            "simple_shorts_mode": True,
        },
    )

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        report = orchestrator.monetization_pipeline.build_monetization_report(session, job)

    assert report["final_status"] == "monetization_review"
    assert report["passed"] is False
    assert report["hard_blockers"] == []
    assert "publish_audit_required" in report["manual_required"]


def test_build_monetization_report_keeps_fact_review_in_simple_mode_with_verified_fact_pack(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    job_id = orchestrator.create_job(
        {
            "seed_theme": "por que cafe tira o sono",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 45,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="Por que cafe tira o sono",
                angle="neurociencia",
                hook_promise="cafe muda sua percepcao de cansaco",
                entities=["cafe"],
                search_terms=["cafe adenosina"],
                title_candidates=["Por que cafe tira o sono"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script",
                title="Por que cafe tira o sono",
                hook="Cafe muda seu estado de alerta.",
                body_beats=[],
                ending="No fim, a primeira frase vira pista.",
                cta=None,
                full_narration="Cafe muda seu estado de alerta.",
                estimated_duration_sec=35,
                key_facts=["Cafeina interfere na sinalizacao de adenosina."],
                token_count=6,
                language="pt-BR",
                qa_metrics={"script_quality_gate_pass": True},
            )
        )
        job = session.get(Job, job_id)
        assert job is not None
        job.quality_summary = {
            "script": {"script_quality_gate_pass": True},
            "scene_plan": {"scene_plan_gate_pass": True},
            "assets": {"semantic_threshold_pass": True},
            "subtitles": {"subtitle_gate_pass": True},
            "render": {"render_gate_pass": True},
        }
        session.commit()

    fact_pack_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / job_id / "fact_pack.json"
    fact_pack_path.parent.mkdir(parents=True, exist_ok=True)
    fact_pack_path.write_text(
        json.dumps(
            {
                "status": "verified",
                "facts": [{"fact_id": "F1", "claim": "Cafeina bloqueia receptores de adenosina."}],
                "sources": [{"title": "Review on caffeine and adenosine receptors"}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_fact_claims_report",
        lambda *args, **kwargs: {
            "fact_pack_status": "verified",
            "requires_fact_review": True,
            "source_fact_ids": ["F1"],
            "grounded_source_fact_ids": ["F1"],
            "claim_trace": [{"text": "Cafe muda seu estado de alerta.", "source_fact_ids": ["F1"], "grounding": "fact_pack"}],
            "grounded_claim_trace": [{"text": "Cafe muda seu estado de alerta.", "source_fact_ids": ["F1"], "grounding": "fact_pack"}],
            "ungrounded_claim_trace": [],
            "claim_sources": [{"fact_id": "F1"}],
            "risk_report": {"score": 0, "blocked": False, "claim_count": 0, "high_risk_claim_count": 0, "claims": []},
        },
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "provider_publish_audit",
        lambda *args, **kwargs: {"passed": True, "reasons": [], "provider": "test", "skipped": False},
    )

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        report = orchestrator.monetization_pipeline.build_monetization_report(session, job)

    assert report["final_status"] == "monetization_review"
    assert report["passed"] is False
    assert "fact_review_required" in report["manual_required"]
    assert "publish_audit_required" not in report["manual_required"]


def test_build_monetization_report_allows_manual_publish_audit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", True)
    job_id = orchestrator.create_job(
        {
            "seed_theme": "paisagens extremas",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="Paisagens extremas",
                angle="geografia",
                hook_promise="lugares reais parecem outro planeta",
                entities=["paisagens"],
                search_terms=["paisagens extremas"],
                title_candidates=["Paisagens extremas parecem irreais"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script",
                title="Paisagens extremas parecem irreais",
                hook="Paisagens extremas parecem irreais.",
                body_beats=[],
                ending="No fim, parece outro planeta.",
                cta=None,
                full_narration="Paisagens extremas parecem irreais.",
                estimated_duration_sec=35,
                key_facts=[],
                token_count=5,
                language="pt-BR",
                qa_metrics={"script_quality_gate_pass": True},
            )
        )
        job = session.get(Job, job_id)
        assert job is not None
        job.quality_summary = {
            "script": {"script_quality_gate_pass": True},
            "scene_plan": {"scene_plan_gate_pass": True},
            "assets": {"semantic_threshold_pass": True},
            "subtitles": {"subtitle_gate_pass": True},
            "render": {"render_gate_pass": True},
        }
        session.commit()

    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "publish_readiness_report",
        lambda *args, **kwargs: {
            "passed": False,
            "reasons": ["text_publish_audit_skipped"],
            "fact_pack_status": "skipped",
            "hashtag_count": 3,
            "weak_hashtags": [],
            "fact_risk": {"blocked": False, "claim_count": 0, "simple_shorts_mode": True},
            "minimax_audit": {"passed": True, "skipped": True},
            "simple_shorts_mode": True,
        },
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_rights_registry",
        lambda *args, **kwargs: {"all_commercial_rights_confirmed": True, "entries": []},
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_ai_disclosure_report",
        lambda *args, **kwargs: {
            "youtube_disclosure_required": False,
            "auto_confirmed": False,
            "contains_synthetic_visuals": False,
        },
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_fact_claims_report",
        lambda *args, **kwargs: {
            "requires_fact_review": False,
            "claim_trace": [],
            "claim_sources": [],
        },
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_channel_repetition_report",
        lambda *args, **kwargs: {"repetition_risk": "low", "matches": [], "signals": {}},
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_metadata_review",
        lambda *args, **kwargs: {"requires_metadata_review": False, "title": "", "suggested_hashtags": [], "reasons": []},
    )

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        report = orchestrator.monetization_pipeline.build_monetization_report(session, job, {"publish_audit_confirmed"})

    assert report["passed"] is True
    assert report["final_status"] == "ready_for_upload"
    assert "publish_audit_required" not in report["manual_required"]


def test_ready_script_fact_check_confirmation_bypasses_external_publish_audit(monkeypatch) -> None:
    from app.manual_script import parse_ready_script

    ready = parse_ready_script(
        """Título: Peixe-pescador usa luz viva para atrair vítimas
Hook: Escuridão total vira isca quando esse peixe acende.
Loop: Por que brilhar no fundo do mar pode significar morte?
Beats: Nas profundezas, a luz do Sol quase nunca chega.
Mesmo assim, o peixe-pescador carrega um brilho próprio.
A luz balança na frente da boca como promessa.
Presas se aproximam achando que encontraram comida.
Quando chegam perto, encontram dentes.
Payoff: A lanterna é uma isca bioluminescente para atrair presas.
Fechamento: Lá embaixo, uma luz pequena pode ser uma armadilha.
Hashtags: #curiosidades #deepsea #biologia #shorts""",
        fact_check_confirmed=True,
    )
    job_id = orchestrator.create_job(
        {
            "seed_theme": ready.script["title"],
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 45,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "input_mode=script\nready_script_fact_check_confirmed=true",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic=ready.script["title"],
                angle="biologia marinha",
                hook_promise=ready.script["hook"],
                entities=["peixe-pescador"],
                search_terms=["peixe-pescador bioluminescencia"],
                title_candidates=[ready.script["title"]],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script",
                title=ready.script["title"],
                hook=ready.script["hook"],
                body_beats=ready.script["body_beats"],
                ending=ready.script["ending"],
                cta=None,
                full_narration=ready.script["full_narration"],
                estimated_duration_sec=ready.script["estimated_duration_sec"],
                key_facts=ready.script["key_facts"],
                token_count=ready.script["token_count"],
                language="pt-BR",
                qa_metrics=ready.script["qa_metrics"],
            )
        )
        job = session.get(Job, job_id)
        assert job is not None
        job.quality_summary = {
            "script": {"script_quality_gate_pass": True},
            "scene_plan": {"scene_plan_gate_pass": True},
            "assets": {"semantic_threshold_pass": True, "asset_semantic_score_avg": 0.95},
            "subtitles": {"subtitle_gate_pass": True},
            "render": {"render_gate_pass": True},
        }
        session.commit()

    artifact_dir = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "fact_pack.json").write_text(json.dumps(ready.fact_pack), encoding="utf-8")
    (artifact_dir / "script.json").write_text(json.dumps(ready.script), encoding="utf-8")
    monkeypatch.setattr(
        orchestrator.monetization_pipeline.providers.creative,
        "audit_publish_package",
        lambda *_args, **_kwargs: {
            "passed": False,
            "reasons": ["self_declared_source_only", "sensationalized_framing", "weak_hashtags"],
            "factual_score": 0.4,
            "metadata_score": 0.4,
        },
    )
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_channel_repetition_report",
        lambda *args, **kwargs: {"repetition_risk": "medium", "matches": [], "signals": {}},
    )

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        report = orchestrator.monetization_pipeline.build_monetization_report(session, job)

    assert report["passed"] is True
    assert report["final_status"] == "ready_for_upload"
    assert report["manual_required"] == []
    assert report["publish_readiness"]["minimax_audit"]["provider"] == "ready_script_manual_fact_check"
    assert "fact_review_confirmed" in report["manual_confirmations"]
    assert "publish_audit_confirmed" in report["manual_confirmations"]
    assert "originality_confirmed" in report["manual_confirmations"]


def test_rights_registry_requires_evidence_for_confirmed_minimax_assets(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "ai_generated_commercial_rights_confirmed", False)
    monkeypatch.setattr(orchestrator.settings, "minimax_commercial_rights_confirmed", True)
    monkeypatch.setattr(orchestrator.settings, "minimax_rights_evidence_url", None)
    report = orchestrator._build_rights_registry(
        SimpleNamespace(job_id="job-rights"),
        [
            SimpleNamespace(
                kind="image",
                scene_id="scene-1",
                provider="minimax",
                uri="file:///tmp/asset.jpg",
                license_note=None,
                attribution=None,
            )
        ],
        None,
        None,
    )

    assert report["all_commercial_rights_confirmed"] is False
    assert report["evidence_required_count"] == 1
    assert report["entries"][0]["review_required"] is True


def test_rights_registry_auto_confirms_ai_generated_assets(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "ai_generated_commercial_rights_confirmed", True)
    monkeypatch.setattr(orchestrator.settings, "minimax_commercial_rights_confirmed", False)
    monkeypatch.setattr(orchestrator.settings, "minimax_rights_evidence_url", None)
    report = orchestrator._build_rights_registry(
        SimpleNamespace(job_id="job-ai-rights"),
        [
            SimpleNamespace(
                kind="image",
                scene_id="scene-1",
                provider="minimax",
                uri="file:///tmp/asset.jpg",
                license_note=None,
                attribution=None,
            )
        ],
        SimpleNamespace(provider="edge_tts", voice="pt-BR", audio_uri="file:///tmp/voice.wav"),
        SimpleNamespace(provider="minimax_music", audio_uri="file:///tmp/music.wav", license_note=None, attribution=None, provider_metadata={}),
    )

    assert report["all_commercial_rights_confirmed"] is True
    assert report["evidence_required_count"] == 0
    assert report["review_required_count"] == 0
    assert {entry["license_source"] for entry in report["entries"]} == {"YTS_AI_GENERATED_COMMERCIAL_RIGHTS_CONFIRMED"}


def test_repetition_module_flags_structural_template_matches() -> None:
    report = build_channel_repetition_report(
        current={
            "canonical_topic": "polvos",
            "angle": "biologia curiosa",
            "script": {
                "title": "Polvos pensam com os braços",
                "hook": "O polvo não pensa só com a cabeça.",
                "ending": "Isso muda como você olha para o animal.",
                "estimated_duration_sec": 35,
                "body_beats": ["A", "B", "C"],
            },
        },
        recent_rows=[
            {
                "job_id": "previous",
                "topic_summary": "polvos biologia curiosa",
                "title": "Polvos pensam com os braços",
                "hook": "O polvo não pensa só com a cabeça.",
                "ending": "Isso muda como você olha para o animal.",
                "estimated_duration_sec": 35,
                "body_beats": ["A", "B", "C"],
            }
        ],
    )

    assert report["repetition_risk"] == "high"
    assert report["signals"]["exact_structural_signature_matches"] == 1


def test_repetition_module_does_not_block_only_same_duration_and_beat_count() -> None:
    report = build_channel_repetition_report(
        current={
            "canonical_topic": "chuva vermelha em cidades",
            "angle": "fenomeno natural",
            "script": {
                "title": "Chuva de sangue já pintou cidades de vermelho",
                "hook": "O céu ficou vermelho e muita gente pensou em sangue.",
                "ending": "O céu não estava sangrando. Estava carregando poeira.",
                "estimated_duration_sec": 35,
                "body_beats": ["A", "B", "C", "D", "E", "F"],
            },
        },
        recent_rows=[
            {
                "job_id": "previous",
                "topic_summary": "asteroide passou perto da Terra",
                "title": "Asteroide 2026 JH2 passou perto demais",
                "hook": "Uma rocha espacial passou perto e quase ninguém viu.",
                "ending": "O risco real era menor do que o susto.",
                "estimated_duration_sec": 35,
                "body_beats": ["A", "B", "C", "D", "E", "F"],
            }
        ],
    )

    assert report["repetition_risk"] == "low"
    assert report["signals"]["repetitive_template_matches"] == 0
    assert report["signals"]["exact_duration_bucket_matches"] == 1
    assert report["signals"]["exact_beat_count_matches"] == 1


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


def test_script_gate_blocks_generic_high_risk_precision_and_causality() -> None:
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios. "
        "Por isso você acorda mais inteligente no dia seguinte."
    )

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "factual_risk_requires_conservative_rewrite" in result.reasons
    assert result.metrics["fact_risk"]["high_risk_claim_count"] >= 1


def test_script_gate_allows_conservative_factual_language() -> None:
    script = _base_script(
        "O cérebro pode reorganizar algumas memórias durante o sono. "
        "Uma das explicações é que conexões usadas com frequência tendem a ficar mais fortes. "
        "Esse detalhe ajuda a entender por que dormir bem importa para aprender."
    )

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert result.passed
    assert result.metrics["fact_risk"]["blocked"] is False


def test_script_gate_rejects_ending_without_loop_connection() -> None:
    script = _base_script(
        "Polvos mudam de cor em segundos para confundir ameaças. "
        "Esse truque aparece quando o ambiente muda rápido. "
        "É assim que o corpo responde antes do predador chegar."
    )
    script["ending"] = "Por isso o oceano parece misterioso."

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "ending_not_connected_to_hook" in result.reasons
    assert result.metrics["loop_gate"]["connected_to_opening"] is False


def test_script_gate_accepts_rewatch_loop_without_exact_token_overlap() -> None:
    script = _base_script(
        "A pena rosa começa no prato. "
        "O pigmento entra pela comida antes de aparecer na cor. "
        "Quando você vê de novo, aquela refeição vira tinta."
    )
    script["title"] = "A comida que pinta flamingos de rosa"
    script["hook"] = "A pena rosa começa no prato."
    script["ending"] = "Quando você vê de novo, aquela refeição vira tinta."

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert result.passed
    assert result.metrics["loop_gate"]["rewatch_loop_signal"] is True


def test_script_gate_rejects_obvious_academic_title_tone() -> None:
    script = _base_script(
        "A pena rosa começa no prato. "
        "O pigmento entra pela comida antes de aparecer na cor. "
        "A pena rosa só parece mágica até você olhar para o prato."
    )
    script["title"] = "Metabolismo de carotenoides em aves aquáticas"

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "academic_title_tone" in result.reasons


def test_script_gate_rejects_generic_loop_ending_template() -> None:
    script = _base_script(
        "Polvos mudam de cor em segundos. "
        "A pele responde ao ambiente ao redor. "
        "No fim, polvos mudam de cor e agora tudo faz sentido."
    )
    script["ending"] = "No fim, polvos mudam de cor e agora tudo faz sentido."

    result = ScriptQualityGate().validate(script, target_duration_sec=35)

    assert not result.passed
    assert "generic_loop_ending" in result.reasons


def test_validate_or_repair_script_recovers_simple_loop_closure(monkeypatch) -> None:
    original_repair_attempts = orchestrator.settings.llm_script_repair_attempts
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", False)
    monkeypatch.setattr(orchestrator.settings, "llm_script_repair_attempts", 1)
    monkeypatch.setattr(orchestrator.providers.creative, "repair_script", lambda script, reasons, plan: dict(script))
    script = _base_script(
        "Polvos mudam de cor em segundos para confundir ameaças. "
        "A pele reage rápido quando a textura ao redor muda. "
        "Isso transforma fuga em camuflagem instantânea."
    )
    script["ending"] = "Por isso o mar parece estranho."
    plan_dict = {"canonical_topic": "polvos", "fact_pack": {"status": "limited", "facts": []}}

    try:
        repaired, metrics = orchestrator._validate_or_repair_script(script, plan_dict, 35, "none")
    finally:
        monkeypatch.setattr(orchestrator.settings, "llm_script_repair_attempts", original_repair_attempts)

    assert metrics["script_quality_gate_pass"] is True
    assert metrics["loop_gate"]["connected_to_opening"] is True
    assert metrics["loop_gate"]["rewatch_loop_signal"] is True
    assert "fecha o ciclo" not in repaired["ending"].lower()
    assert "no replay" not in repaired["ending"].lower()


def test_validate_or_repair_script_rewrites_weak_fact_pack_conservatively(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", False)
    monkeypatch.setattr(orchestrator.providers.creative, "repair_script", lambda script, reasons, plan: dict(script))
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios. "
        "Por isso você acorda mais inteligente no dia seguinte."
    )
    script["source_fact_ids"] = ["F9"]
    plan_dict = {"canonical_topic": "cérebro", "fact_pack": {"status": "limited", "facts": []}}

    repaired, metrics = orchestrator._validate_or_repair_script(script, plan_dict, 35, "none")

    assert metrics["script_quality_gate_pass"] is True
    assert metrics["fact_risk"]["blocked"] is False
    assert repaired["source_fact_ids"] == []
    assert "número exato" in repaired["full_narration"] or "Em geral" in repaired["full_narration"]
    assert repaired["claim_trace"]
    assert repaired["claim_trace"][0]["grounding"] == "conservative"


def test_postprocess_script_normalizes_visible_text_and_attaches_claim_trace() -> None:
    script = _base_script(
        "A pele do polvo reflete luz — e muda no. centro da unidade. "
        "A célula permite ajustar a refletância da pele."
    )
    script["source_fact_ids"] = ["F1", "F2"]
    script["ending"] = "Quando você vê de novo, a pele entrega a pista."
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "Leucóforos refletem luz em banda ampla.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "A unidade cromática contém iridóforos e cromatóforos.", "source_id": "S1"},
        ],
    }

    processed = orchestrator._postprocess_script_for_quality(script, {"canonical_topic": "polvo", "fact_pack": fact_pack}, [])

    assert "—" not in processed["full_narration"]
    assert "no. centro" not in processed["full_narration"]
    assert processed["claim_trace"]
    assert processed["claim_trace"][0]["grounding"] == "missing"
    assert processed["claim_trace"][0]["source_fact_ids"] == []


def test_postprocess_script_restores_richer_retention_map_narration() -> None:
    script = {
        "title": "Ilusão de ótica do movimento parado",
        "hook": "Ilusão ótica parece exagero, até a explicação concreta entrar.",
        "body_beats": ["O detalhe verificável segura a surpresa."],
        "ending": "Agora o começo muda de sentido: detalhe verificável era a pista.",
        "cta": None,
        "full_narration": "Ilusão ótica parece exagero, até a explicação concreta entrar. O detalhe verificável segura a surpresa. Agora o começo muda de sentido: detalhe verificável era a pista.",
        "estimated_duration_sec": 35.0,
        "key_facts": ["O detalhe verificável segura a surpresa."],
        "source_fact_ids": ["F1"],
        "claim_trace": [{"text": "O detalhe verificável segura a surpresa.", "source_fact_ids": ["F1"], "grounding": "fact_pack"}],
        "token_count": 35,
        "language": "pt-BR",
        "retention_map": {
            "segments": [
                {"code": "visual_hook", "mapped_text": "Está tudo parado. Mesmo assim, parece que gira."},
                {"code": "proof_or_tension", "mapped_text": "Quanto menos você fixa no centro, mais o efeito costuma crescer."},
                {"code": "escalation", "mapped_text": "Não é animação escondida. Seu olhar percorre contraste, curvas e repetição."},
                {"code": "turn_or_payoff", "mapped_text": "Pequenos movimentos dos olhos podem reforçar essa falsa sensação de deslocamento."},
                {"code": "loop_close", "mapped_text": "Então o giro do começo não estava na imagem. Ele apareceu no encontro entre o padrão e o jeito que seu olhar varre a cena."},
            ]
        },
        "visual_opening": {},
        "qa_metrics": {},
    }

    processed = orchestrator._postprocess_script_for_quality(script, {"canonical_topic": "ilusão de ótica", "fact_pack": {"status": "limited", "facts": []}}, [])

    assert processed["hook"] == "Está tudo parado. Mesmo assim, parece que gira."
    assert "pequenos movimentos dos olhos" in processed["full_narration"].lower()
    assert "pequenos movimentos olhos" in processed["ending"].lower() or processed["ending"].startswith("Então o giro do começo")
    assert processed["key_facts"][0].startswith("Quanto menos você fixa")


def test_postprocess_conservative_verified_fact_pack_keeps_narration_pt_br() -> None:
    script = _base_script(
        "Octopus skin can reflect broad-spectrum light because chromatophores control the surface. "
        "Na segunda olhada, a primeira frase entrega a pista."
    )
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "Chromatophores and iridophores contribute to octopus camouflage.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "Reflective cells alter the appearance of the octopus skin.", "source_id": "S1"},
        ],
    }

    processed = orchestrator._postprocess_script_for_quality(
        script,
        {"canonical_topic": "polvo", "fact_pack": fact_pack},
        ["factual_claim_trace_missing"],
    )

    assert "Chromatophores" not in processed["full_narration"]
    assert "octopus" not in processed["full_narration"].lower()
    assert "Células especializadas" in processed["full_narration"]
    assert processed["claim_trace"][0]["source_fact_ids"] == ["F1"]


def test_validate_or_repair_script_accepts_fractional_string_scores(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "simple_shorts_mode", False)
    monkeypatch.setattr(orchestrator.providers.creative, "repair_script", lambda script, reasons, plan: dict(script))
    script = _base_script(
        "A imagem está parada. Mesmo assim, parece girar. "
        "O contraste engana o olhar nas bordas. "
        "Pequenos movimentos dos olhos reforçam a sensação. "
        "Por isso o primeiro frame muda quando você olha outra vez."
    )
    script["qa_metrics"] = {
        "hook_score": "9/10",
        "clarity_score": "9/10",
        "information_density_score": "8/10",
        "repetition_score": "1/10",
        "ending_strength_score": "9/10",
        "script_gate_pass": "aprovado",
    }

    repaired, metrics = orchestrator._validate_or_repair_script(
        script,
        {"canonical_topic": "ilusão de ótica", "fact_pack": {"status": "limited", "facts": []}},
        35,
        "none",
    )

    assert metrics["script_quality_gate_pass"] is True
    assert repaired["qa_metrics"]["hook_score"] == 0.9
    assert repaired["qa_metrics"]["repetition_score"] == 0.1


def test_normalize_script_metrics_inverts_high_quality_repetition_score() -> None:
    normalized = normalize_script_metrics(
        {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.85,
            "repetition_score": 0.94,
            "ending_strength_score": 0.9,
        }
    )

    assert normalized["repetition_score"] == 0.06


def test_full_pipeline_with_sound_design_persists_rights_and_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "sound_design_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "sound_design_gain_db", -16.0)
    orchestrator.stop_worker()
    orchestrator.stop_event.clear()
    client = TestClient(app)
    try:
        response = client.post(
            "/jobs",
            data={"seed_theme": "polvos", "target_duration_sec": 35, "tone": "intrigante_direto", "cta_style": "none"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = response.headers["location"].split("/")[-1]
        assert orchestrator.process_job(job_id) in {"monetization_review", "blocked_for_monetization"}
    finally:
        orchestrator.start_worker()
    with SessionLocal() as session:
        background_music = session.query(BackgroundMusicAsset).filter_by(job_id=job_id).one()
        assert background_music.provider_metadata["sound_design"]["enabled"] is True
        assert Path(background_music.provider_metadata["sound_design"]["audio_uri"].removeprefix("file://")).exists()
        job = session.get(Job, job_id)
        assert job.artifact_index["sound_design"] == "audio/sound_design.wav"
    rights_registry = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "rights_registry.json").read_text(encoding="utf-8"))
    assert any(entry["asset_type"] == "sound_design" for entry in rights_registry["entries"])


def test_script_postprocess_removes_structured_body_beat_leak() -> None:
    script = _base_script("{'segment': 'visual_hook', 'narration': 'Texto vazado.'}")
    script["hook"] = "Este flamingo é rosa."
    script["body_beats"] = [
        {"segment": "visual_hook", "narration": "Mas ele nasceu branco."},
        {"segment": "payoff", "narration": "A cor vem dos carotenoides."},
    ]
    script["ending"] = "A dieta vira cor."

    processed = orchestrator._postprocess_script_for_quality(script, {"fact_pack": {"status": "limited", "facts": []}}, [])

    assert "{'segment'" not in processed["full_narration"]
    assert processed["body_beats"] == ["Mas ele nasceu branco.", "A cor vem dos carotenoides."]
    assert "A cor vem dos carotenoides." in processed["full_narration"]


def test_script_postprocess_repairs_common_provider_text_issues() -> None:
    script = _base_script(
        "Flamengos brancos viram roses. "
        "A cor vem deartemia e algas, sem nenhuma trace de rosa. "
        "Sem supplementação, o tom muda."
    )
    script["title"] = "Flamengos viram roses pela alimentacao"
    script["hook"] = "Flamengos brancos viram roses."
    script["ending"] = "Sem supplementação, o tom muda."

    processed = orchestrator._postprocess_script_for_quality(script, {"fact_pack": {"status": "limited", "facts": []}}, [])
    combined = " ".join([processed["title"], processed["hook"], processed["ending"], processed["full_narration"]])

    assert "Flamengos" not in combined
    assert "roses" not in combined
    assert "deartemia" not in combined
    assert "supplementação" not in combined
    assert "alimentação" in processed["title"]


def test_fact_result_relevance_rejects_fuzzy_wrong_search_hit() -> None:
    assert not orchestrator._fact_result_is_relevant(
        "polvo muda",
        "Povo munda",
        "O povo munda é um grupo étnico do subcontinente indiano.",
    )


def test_fact_result_relevance_rejects_single_token_only_in_abstract() -> None:
    assert not orchestrator._fact_result_is_relevant(
        "Modena",
        "Preferred Reporting Items for Systematic Reviews and Meta-Analyses",
        "The author affiliation mentions Università di Modena e Reggio Emilia.",
    )


def test_weak_fact_query_rejects_generic_food_cause_terms() -> None:
    assert orchestrator._is_weak_fact_query("causa comida")


def test_fact_query_concepts_include_octopus_camouflage_terms() -> None:
    concepts = orchestrator._fact_query_concepts("camuflagem dos polvos usando cromatóforos")

    assert "chromatophores" in concepts
    assert "iridophores" in concepts


def test_fact_query_concepts_do_not_treat_coracoes_as_color() -> None:
    concepts = orchestrator._fact_query_concepts("polvo com tres corações e sangue azul")

    assert "plumage pigmentation" not in concepts
    assert "carotenoid pigmentation" not in concepts


def test_fact_query_concepts_include_flamingo_pigment_terms() -> None:
    concepts = orchestrator._fact_query_concepts("flamingos ficam rosas")

    assert "carotenoid pigmentation" in concepts
    assert "plumage pigmentation" in concepts


def test_scientific_article_fact_pack_skips_result_without_abstract(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: object):
            self.payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> object:
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            assert url == "https://api.openalex.org/works"
            assert kwargs["params"]["filter"] == "type:article,has_abstract:true"
            return FakeResponse(
                {
                    "results": [
                        {"display_name": "Broken article", "abstract_inverted_index": None},
                        {
                            "display_name": "Carotenoid pigmentation in flamingos",
                            "abstract_inverted_index": {
                                "Flamingos": [0],
                                "show": [1],
                                "pink": [2],
                                "color": [3],
                                "because": [4],
                                "carotenoid": [5],
                                "pigments": [6],
                                "from": [7],
                                "food": [8],
                                "accumulate": [9],
                                "in": [10],
                                "their": [11],
                                "feathers.": [12],
                            },
                            "doi": "https://doi.org/10.0000/flamingo",
                            "publication_year": 2024,
                            "primary_location": {
                                "landing_page_url": "https://example.test/flamingo-paper",
                                "source": {"display_name": "Journal of Bird Color"},
                            },
                        },
                    ]
                }
            )
            raise AssertionError(url)

    monkeypatch.setattr(script_pipeline_module.httpx, "Client", FakeClient)

    pack = orchestrator._scientific_article_fact_pack("flamingo carotenoid")

    assert pack["status"] == "verified"
    assert pack["topic_title"] == "Carotenoid pigmentation in flamingos"
    assert pack["sources"][0]["url"] == "https://doi.org/10.0000/flamingo"
    assert pack["sources"][0]["provider"] == "openalex"


def test_scientific_article_fact_pack_skips_low_information_abstract(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> object:
            return {
                "results": [
                    {
                        "display_name": "Perspectivas no controle de formigas cortadeiras",
                        "abstract_inverted_index": {
                            "O": [0],
                            "conteudo": [1],
                            "e": [2],
                            "apresentado": [3],
                            "em:": [4],
                            "Introducao;": [5],
                            "Metodos": [6],
                            "de": [7],
                            "controle;": [8],
                            "Manejo": [9],
                            "do": [10],
                            "controle": [11],
                            "de": [12],
                            "formigas": [13],
                            "cortadeiras;": [14],
                            "Referencia": [15],
                            "bibliografica.": [16],
                        },
                    },
                    {
                        "display_name": "Formigas cortadeiras improve collective foraging decisions",
                        "abstract_inverted_index": {
                            "Formigas": [0],
                            "colonies": [1],
                            "coordinate": [2],
                            "foraging": [3],
                            "through": [4],
                            "local": [5],
                            "interactions": [6],
                            "that": [7],
                            "allow": [8],
                            "workers": [9],
                            "to": [10],
                            "adjust": [11],
                            "trail": [12],
                            "use": [13],
                            "as": [14],
                            "food": [15],
                            "conditions": [16],
                            "change.": [17],
                        },
                        "doi": "https://doi.org/10.0000/ants",
                        "publication_year": 2024,
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            assert url == "https://api.openalex.org/works"
            return FakeResponse()

    monkeypatch.setattr(script_pipeline_module.httpx, "Client", FakeClient)

    pack = orchestrator._scientific_article_fact_pack("formigas")

    assert pack["status"] == "verified"
    assert pack["topic_title"] == "Formigas cortadeiras improve collective foraging decisions"
    assert "conteudo e apresentado" not in pack["facts"][0]["claim"].lower()


def test_fact_pack_consistency_requires_source_fact_ids_when_verified() -> None:
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "O sono reorganiza memórias.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "Neurônios mudam conexões durante o sono.", "source_id": "S1"},
        ],
    }
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios."
    )

    reasons = orchestrator._fact_pack_consistency_reasons(script, fact_pack)

    assert "fact_pack_source_ids_missing" in reasons


def test_fact_pack_consistency_accepts_grounded_source_fact_ids() -> None:
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "O sono reorganiza memórias.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "Neurônios mudam conexões durante o sono.", "source_id": "S1"},
        ],
    }
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios."
    )
    script["source_fact_ids"] = ["F1", "F2"]
    script["claim_trace"] = [
        {"text": "A obra começou em 1173.", "source_fact_ids": ["F1"], "grounding": "fact_pack"},
        {
            "text": "Engenheiros estabilizaram a inclinação com uma intervenção moderna.",
            "source_fact_ids": ["F2"],
            "grounding": "fact_pack",
        },
    ]

    assert orchestrator._fact_pack_consistency_reasons(script, fact_pack) == []


def test_fact_pack_consistency_accepts_grounded_claim_trace_source_ids() -> None:
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "O sono reorganiza memórias.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "Neurônios mudam conexões durante o sono.", "source_id": "S1"},
        ],
    }
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios."
    )
    script["source_fact_ids"] = ["F1"]
    script["claim_trace"] = [
        {"text": "O cérebro apaga exatamente 73% das memórias durante o sono.", "source_fact_ids": ["F1"], "grounding": "fact_pack"},
        {
            "text": "Isso acontece porque a dopamina destrói conexões fracas nos neurônios.",
            "source_fact_ids": ["F2"],
            "grounding": "fact_pack",
        },
    ]

    assert orchestrator._fact_pack_consistency_reasons(script, fact_pack) == []


def test_fact_pack_consistency_requires_claim_trace_per_risky_claim() -> None:
    fact_pack = {
        "status": "verified",
        "facts": [
            {"fact_id": "F1", "claim": "O sono reorganiza memórias.", "source_id": "S1"},
            {"fact_id": "F2", "claim": "Neurônios mudam conexões durante o sono.", "source_id": "S1"},
        ],
    }
    script = _base_script(
        "O cérebro apaga exatamente 73% das memórias durante o sono. "
        "Isso acontece porque a dopamina destrói conexões fracas nos neurônios."
    )
    script["source_fact_ids"] = ["F1", "F2"]

    reasons = orchestrator._fact_pack_consistency_reasons(script, fact_pack)

    assert "factual_claim_trace_missing" in reasons


def test_fact_pack_consistency_rejects_invented_claim_trace_fact_ids() -> None:
    fact_pack = {
        "status": "verified",
        "facts": [{"fact_id": "F1", "claim": "O tema começou em 1173.", "source_id": "S1"}],
    }
    script = _base_script("A obra começou em 1173.")
    script["source_fact_ids"] = ["F1"]
    script["claim_trace"] = [
        {"text": "A obra começou em 1173.", "source_fact_ids": ["F9"], "grounding": "fact_pack"}
    ]

    reasons = orchestrator._fact_pack_consistency_reasons(script, fact_pack)

    assert "invented_claim_trace_fact_ids" in reasons


def test_fact_pack_consistency_rejects_source_ids_when_fact_pack_limited() -> None:
    script = _base_script("Flamingos ficam rosas por pigmentos na alimentação.")
    script["source_fact_ids"] = ["fact_1"]

    reasons = orchestrator._fact_pack_consistency_reasons(script, {"status": "limited", "facts": []})

    assert "invented_source_fact_ids" in reasons


def test_fact_claims_report_exposes_claim_trace() -> None:
    script_artifact = {
        **_base_script("A pele do polvo pode refletir luz em uma banda ampla."),
        "source_fact_ids": ["F1"],
        "claim_trace": [
            {
                "text": "A pele do polvo pode refletir luz em uma banda ampla.",
                "source_fact_ids": ["F1"],
                "grounding": "fact_pack",
            }
        ],
    }
    fact_pack = {
        "status": "verified",
        "facts": [{"fact_id": "F1", "claim": "A região central reflete luz em uma banda ampla.", "source_id": "S1"}],
        "sources": [{"source_id": "S1", "url": "https://doi.org/10.test/example"}],
    }

    report = orchestrator._build_fact_claims_report(None, None, fact_pack, script_artifact)

    assert report["claim_trace"] == script_artifact["claim_trace"]
    assert report["grounded_claim_trace"][0]["source_fact_ids"] == ["F1"]
    assert report["ungrounded_claim_trace"] == []


def test_scene_token_coverage_normalization_rebuilds_contiguous_spans() -> None:
    scenes = [
        {
            "scene_id": "scene-2",
            "order": 2,
            "narration_text": "muda de cor para fugir",
            "token_start": 99,
            "token_end": 120,
            "image_prompt": "ok no readable text anywhere",
        },
        {
            "scene_id": "scene-1",
            "order": 1,
            "narration_text": "polvos parecem alienigenas",
            "token_start": 5,
            "token_end": 7,
            "image_prompt": "ok no readable text anywhere",
        },
    ]

    normalized = orchestrator._normalize_scene_token_coverage(
        scenes,
        "Polvos parecem alienigenas e mudam de cor para fugir de predadores",
    )

    assert normalized[0]["order"] == 1
    assert normalized[0]["token_start"] == 0
    assert normalized[0]["token_end"] < normalized[1]["token_start"]
    assert normalized[1]["token_end"] == len(word_tokens("Polvos parecem alienigenas e mudam de cor para fugir de predadores")) - 1
    assert normalized[0]["narration_text"].startswith("polvos parecem alienigenas")


def test_step_script_persists_generation_debug_on_provider_failure(monkeypatch) -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="polvos",
                angle="curiosidades_inacreditaveis",
                hook_promise="fatos sobre polvos",
                entities=["polvos"],
                search_terms=["polvos curiosidades"],
                title_candidates=["Polvos parecem alienígenas reais"],
                quality_metrics={},
            )
        )
        session.commit()

    def fake_generate_script(_plan_dict):
        raise ProviderFailure("minimax_text", "script generation timed out after 90.0s")

    monkeypatch.setattr(orchestrator.providers.creative, "generate_script", fake_generate_script)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        try:
            orchestrator._step_script(session, job, 1)
        except ProviderFailure:
            pass
        else:
            raise AssertionError("expected ProviderFailure")

    debug_path = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "script_generation_debug.json"
    debug = json.loads(debug_path.read_text(encoding="utf-8"))

    assert debug["phase"] == "generation"
    assert debug["error_type"] == "ProviderFailure"
    assert debug["error_message"] == "script generation timed out after 90.0s"
    assert debug["canonical_topic"] == "polvos"
    assert debug["fact_pack_status"] in {"limited", "verified", "skipped"}


def test_step_background_music_persists_debug_on_provider_failure(monkeypatch) -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "polvos",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    audio_dir = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    narration_path = audio_dir / "narration.wav"
    narration_path.write_bytes(b"RIFFtest")

    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-music-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic-music",
                canonical_topic="polvos",
                angle="curiosidades_inacreditaveis",
                hook_promise="fatos sobre polvos",
                entities=["polvos"],
                search_terms=["polvos curiosidades"],
                title_candidates=["Polvos parecem alienígenas reais"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-music-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script-music",
                title="Polvos parecem alienígenas",
                hook="Cada braço do polvo parece pensar sozinho.",
                body_beats=[],
                ending="Isso muda como você olha para o oceano.",
                cta=None,
                full_narration="Cada braço do polvo parece pensar sozinho.",
                estimated_duration_sec=30,
                key_facts=[],
                token_count=12,
                language="pt-BR",
                qa_metrics={},
            )
        )
        session.add(
            NarrationAsset(
                narration_id=f"narration-music-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="narration-music",
                provider="edge_tts",
                voice="pt-BR-FranciscaNeural",
                audio_uri=narration_path.resolve().as_uri(),
                normalized_audio_uri=None,
                raw_subtitles_uri=None,
                duration_ms=32000,
                sample_rate_hz=24000,
                channels=1,
                loudness_lufs=-16.0,
                provider_metadata={},
            )
        )
        session.commit()

    def fake_select_track(_topic_dict, _script_dict, _output_path, _target_duration_ms):
        raise ProviderFailure(
            "background_music",
            "strict minimax validation requires minimax music success: minimax music request timed out after 120.0s",
            details={
                "provider": "minimax_music",
                "query": "polvos documentary",
                "mood": "documentary",
                "timeout_sec": 120.0,
                "request_payload": {"model": "music-2.6"},
            },
        )

    monkeypatch.setattr(orchestrator.providers.music, "select_track", fake_select_track)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        try:
            orchestrator._step_background_music(session, job, 1)
        except ProviderFailure:
            pass
        else:
            raise AssertionError("expected ProviderFailure")

    debug_path = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "background_music_debug.json"
    debug = json.loads(debug_path.read_text(encoding="utf-8"))

    assert debug["phase"] == "provider_failure"
    assert debug["error_type"] == "ProviderFailure"
    assert "minimax music request timed out" in debug["error_message"]
    assert debug["canonical_topic"] == "polvos"
    assert debug["provider_details"]["request_payload"]["model"] == "music-2.6"
    assert debug["provider_details"]["query"] == "polvos documentary"


def test_minimax_background_music_provider_uses_output_url(monkeypatch, tmp_path: Path) -> None:
    provider = MiniMaxBackgroundMusicProvider()

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload
            self.status_code = 200
            self.text = json.dumps(payload)

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    captured: dict[str, object] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse(
            {
                "data": {
                    "audio": "https://example.com/music.mp3",
                    "status": 2,
                },
                "trace_id": "trace-music",
                "extra_info": {
                    "music_duration": 31876,
                    "music_sample_rate": 44100,
                    "music_channel": 2,
                    "bitrate": 256000,
                },
            }
        )

    class FakeDownloadResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, timeout=None, follow_redirects=None):
        assert url == "https://example.com/music.mp3"
        return FakeDownloadResponse(b"fake-mp3")

    def fake_convert(input_path: Path, output_path: Path) -> None:
        assert input_path.exists()
        output_path.write_bytes(b"RIFFfake")

    trimmed: dict[str, object] = {}

    def fake_trim(output_path: Path, target_duration_ms: int) -> dict[str, object]:
        trimmed["path"] = output_path
        trimmed["target_duration_ms"] = target_duration_ms
        return {
            "source_trimmed_to_ms": target_duration_ms,
            "source_trim_applied": True,
        }

    monkeypatch.setattr("app.providers.httpx.post", fake_post)
    monkeypatch.setattr("app.providers.httpx.get", fake_get)
    monkeypatch.setattr(provider, "_convert_audio_file_to_wav", fake_convert)
    monkeypatch.setattr(provider, "_trim_wav_to_target_duration", fake_trim)

    result = provider.select_track(
        {"canonical_topic": "polvos", "angle": "inteligencia distribuida"},
        {"title": "Polvos parecem alienígenas", "hook": "Cada braço parece pensar sozinho.", "full_narration": "Texto curto."},
        tmp_path / "background.wav",
        32000,
    )

    assert captured["payload"]["output_format"] == "url"
    assert "exactly 32 seconds" in result["provider_metadata"]["prompt"]
    assert result["source_url"] == "https://example.com/music.mp3"
    assert result["provider_metadata"]["returned_duration_ms"] == 31876
    assert result["provider_metadata"]["source_trimmed_to_ms"] == 32000
    assert result["provider_metadata"]["source_trim_applied"] is True
    assert trimmed == {"path": tmp_path / "background.wav", "target_duration_ms": 32000}
    assert Path(result["audio_uri"].removeprefix("file://")).exists()


def test_minimax_background_music_provider_surfaces_usage_limit(monkeypatch, tmp_path: Path) -> None:
    provider = MiniMaxBackgroundMusicProvider()

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "data": {},
                "trace_id": "trace-limit",
                "base_resp": {"status_code": 2056, "status_msg": "usage limit exceeded"},
            }

    monkeypatch.setattr("app.providers.httpx.post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(ProviderFailure) as exc_info:
        provider.select_track({"canonical_topic": "polvos"}, {"title": "Polvos", "hook": "Polvos somem."}, tmp_path / "background.wav", 32000)

    assert "provider limit: usage limit exceeded" in str(exc_info.value)
    assert exc_info.value.details["base_resp"]["status_code"] == 2056


def test_minimax_background_music_prompt_is_compact_and_duration_aware() -> None:
    provider = MiniMaxBackgroundMusicProvider()

    prompt = provider._build_prompt(
        {"canonical_topic": "Polvos - curiosidades científicas sobre o cefalópode mais inteligente do oceano", "angle": "fatos_científicos_absurdos"},
        {
            "title": "O animal com 3 corações e sangue azul que impressiona até cientistas",
            "hook": "Este bicho marinho tem 3 corações e sangue azul.",
            "full_narration": "Texto longo que não deveria ser despejado inteiro no prompt de música.",
        },
        "documentary",
        25_476,
    )

    assert "exactly 25 seconds" in prompt
    assert "Video context:" in prompt
    assert "full_narration" not in prompt
    assert len(prompt) < 900


def test_fact_pack_query_generation_extracts_entity_and_concepts() -> None:
    request = SimpleNamespace(seed_theme="Por que flamingos ficam cor-de-rosa?")
    topic_plan = SimpleNamespace(
        canonical_topic="Por que flamingos ficam cor-de-rosa",
        angle="A cor vem de pigmentos na alimentação",
        title_candidates=["A comida que pinta flamingos"],
    )

    queries = orchestrator._fact_pack_queries(request, topic_plan)
    normalized = [query.lower() for query in queries]

    assert any(query == "flamingos" for query in normalized)
    assert any("flamingos carotenoid pigmentation" == query for query in normalized)


def test_fact_pack_query_generation_uses_honey_concepts_for_mel_topic() -> None:
    request = SimpleNamespace(seed_theme="Por que o mel quase não estraga?")
    topic_plan = SimpleNamespace(
        canonical_topic="Por que o mel quase não estraga",
        angle="auto",
        hook_promise="A ciência explica por que o mel resiste ao tempo.",
        search_terms=[],
        entities=[],
        title_candidates=[],
    )

    pipeline = orchestrator.script_pipeline
    queries = pipeline._fact_pack_queries(request, topic_plan)
    topic_tokens = pipeline._fact_topic_tokens(request, topic_plan)
    cleaned = []
    seen = set()
    for query in queries:
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not pipeline._is_weak_fact_query(normalized):
            cleaned.append(normalized)
            seen.add(normalized.lower())
    ordered = sorted([query for query in cleaned if pipeline._query_matches_primary_fact_topic(query, topic_tokens)], key=pipeline._fact_query_priority)

    assert {"mel", "honey", "honeys"} <= topic_tokens
    assert "honey water activity" in ordered[:4]
    assert ordered.index("honey water activity") < ordered.index("mel honey water activity")


def test_fact_pack_query_generation_uses_caffeine_concepts_for_cafe_topic() -> None:
    request = SimpleNamespace(seed_theme="Por que o café tira o sono?")
    topic_plan = SimpleNamespace(
        canonical_topic="Por que o café tira o sono",
        angle="cafeina bloqueia adenosina",
        hook_promise="A cafeína engana o cérebro ao bloquear a adenosina.",
        search_terms=[],
        entities=[],
        title_candidates=[],
    )

    pipeline = orchestrator.script_pipeline
    queries = pipeline._fact_pack_queries(request, topic_plan)
    topic_tokens = pipeline._fact_topic_tokens(request, topic_plan)
    cleaned = []
    seen = set()
    for query in queries:
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not pipeline._is_weak_fact_query(normalized):
            cleaned.append(normalized)
            seen.add(normalized.lower())
    ordered = sorted([query for query in cleaned if pipeline._query_matches_primary_fact_topic(query, topic_tokens)], key=pipeline._fact_query_priority)

    assert {"cafe", "cafeina", "caffeine", "adenosina", "adenosine"} <= topic_tokens
    assert "caffeine adenosine receptor" in ordered
    assert any("adenosina" in query or "caffeine adenosine" in query for query in ordered[:5])
    assert pipeline._fact_result_is_relevant(
        "café",
        "The World Café as a Participatory Method for Collecting Qualitative Data",
        "World Café is a participatory assessment tool used in community development.",
    ) is False
    assert pipeline._is_weak_fact_query("adenosina") is True


def test_fact_pack_query_generation_uses_optical_illusion_concepts() -> None:
    request = SimpleNamespace(seed_theme="Ilusão de ótica: por que imagem parada parece se mexer?")
    topic_plan = SimpleNamespace(
        canonical_topic="Ilusão de ótica de movimento parado",
        angle="Explicar por que certas imagens estáticas parecem se mover na visão periférica.",
        hook_promise="Seu cérebro inventa movimento onde nada está se movendo.",
        search_terms=["movimento ilusório explicação", "visão periférica ilusão de ótica"],
        entities=["percepção visual", "retina", "movimento ilusório"],
        title_candidates=["Por que essa ilusão parada parece viva?"],
    )

    pipeline = orchestrator.script_pipeline
    queries = pipeline._fact_pack_queries(request, topic_plan)
    topic_tokens = pipeline._fact_topic_tokens(request, topic_plan)
    cleaned = []
    seen = set()
    for query in queries:
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not pipeline._is_weak_fact_query(normalized):
            cleaned.append(normalized)
            seen.add(normalized.lower())
    ordered = sorted([query for query in cleaned if pipeline._query_matches_primary_fact_topic(query, topic_tokens)], key=pipeline._fact_query_priority)

    assert {"ilusao", "illusion", "motion"} <= topic_tokens
    assert "peripheral drift illusion" in ordered
    assert "illusory motion visual perception" in ordered


def test_fact_pack_rejects_adenosine_source_without_caffeine_for_cafe_topic() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Por que o café tira o sono?")
    topic_plan = SimpleNamespace(canonical_topic="Por que o café tira o sono", angle="cafeina bloqueia adenosina")
    fact_pack = {
        "topic_title": "Dosagem da atividade da adenosina deaminase no líquido pleural",
        "sources": [{"title": "Dosagem da atividade da adenosina deaminase no líquido pleural"}],
        "facts": [{"claim": "A dosagem da adenosina deaminase foi usada no diagnóstico da tuberculose pleural."}],
    }

    assert pipeline._fact_pack_matches_topic(fact_pack, request, topic_plan) is False


def test_research_brief_requires_promised_mechanism_not_just_entity_overlap() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Por que o café tira o sono?", requested_angle=None)
    topic_plan = SimpleNamespace(
        canonical_topic="Por que o café tira o sono",
        angle="cafeina bloqueia adenosina e atrasa o sono",
        hook_promise="A cafeína engana o cérebro ao bloquear a adenosina.",
        entities=["cafe", "cafeina"],
        search_terms=[
            "caffeine adenosine receptor antagonism sleep mechanism",
            "adenosine receptors caffeine wakefulness review",
        ],
    )
    research_brief = pipeline._build_research_brief(topic_plan, request)

    audit = orchestrator._audit_source_relevance(
        research_brief,
        "Teor de cafeina em cafés brasileiros comercializados em diferentes formas",
        "O estudo compara a variacao do teor de cafeina entre amostras de cafe e mede diferencas entre formas de preparo.",
    )

    assert audit["passed"] is False
    assert audit["reason"] == "missing_promised_mechanism_terms"


def test_fact_pack_accepts_source_when_research_brief_matches_primary_and_mechanism_terms() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Por que o café tira o sono?", requested_angle=None)
    topic_plan = SimpleNamespace(
        canonical_topic="Por que o café tira o sono",
        angle="cafeina bloqueia adenosina e atrasa o sono",
        hook_promise="A cafeína engana o cérebro ao bloquear a adenosina.",
        entities=["cafe", "cafeina"],
        search_terms=[
            "caffeine adenosine receptor antagonism sleep mechanism",
            "adenosine receptors caffeine wakefulness review",
        ],
    )
    fact_pack = {
        "topic_title": "Caffeine, adenosine receptors and sleep onset",
        "sources": [{"title": "Caffeine, adenosine receptors and sleep onset"}],
        "facts": [{"claim": "Caffeine blocks adenosine receptors and delays sleep onset in many people."}],
    }

    assert pipeline._fact_pack_matches_topic(fact_pack, request, topic_plan) is True
    assert fact_pack["topic_alignment"]["mechanism_overlap"]


def test_query_supports_research_brief_rejects_thin_mechanism_queries() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Por que o café tira o sono?", requested_angle=None)
    topic_plan = SimpleNamespace(
        canonical_topic="Como a cafeína do café bloqueia a adenosina no cérebro e atrasa o sono",
        angle="Explicar de forma direta e intrigante por que o café tira o sono, focando no mecanismo real: a cafeína ocupa receptores de adenosina e pode atrasar o sono.",
        hook_promise="O café engana o cérebro ao bloquear o sinal químico do sono.",
        entities=["cafeína", "café", "adenosina", "receptores de adenosina"],
        search_terms=[
            "cafeína bloqueio dos receptores de adenosina efeito no sono",
            "coffee caffeine adenosine receptor antagonism sleepiness",
            "caffeine circadian phase melatonin onset study",
        ],
    )
    research_brief = pipeline._build_research_brief(topic_plan, request)

    assert pipeline._query_supports_research_brief("meia cafeína", research_brief) is False
    assert pipeline._query_supports_research_brief("cafeína no cérebro", research_brief) is False
    assert pipeline._query_supports_research_brief("caffeine adenosine receptor antagonism sleepiness", research_brief) is True


def test_fact_pack_accepts_english_optical_illusion_source_for_pt_topic() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Ilusão de ótica: por que imagem parada parece se mexer?")
    topic_plan = SimpleNamespace(
        canonical_topic="Ilusão de ótica de movimento parado",
        angle="Explicar por que certas imagens estáticas parecem se mover na visão periférica.",
        search_terms=["peripheral drift illusion", "illusory motion visual perception"],
        entities=["ilusão de ótica", "movimento ilusório"],
    )
    fact_pack = {
        "topic_title": "The Peripheral Drift Illusion: A Motion Illusion in the Visual Periphery",
        "sources": [{"title": "The Peripheral Drift Illusion: A Motion Illusion in the Visual Periphery"}],
        "facts": [{"claim": "Peripheral drift illusions create apparent motion in static images through visual processing biases."}],
    }

    assert pipeline._fact_pack_matches_topic(fact_pack, request, topic_plan) is True
    assert fact_pack["topic_alignment"]["passed"] is True


def test_fact_pack_rejects_generic_visual_source_for_optical_illusion_topic() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Ilusão de ótica: por que imagem parada parece se mexer?")
    topic_plan = SimpleNamespace(
        canonical_topic="Ilusão de ótica de movimento parado",
        angle="Explicar por que certas imagens estáticas parecem se mover na visão periférica.",
        search_terms=["peripheral drift illusion", "illusory motion visual perception"],
        entities=["ilusão de ótica", "movimento ilusório"],
    )
    fact_pack = {
        "topic_title": "Função de Sensibilidade ao Contraste: Indicador da Percepção Visual da Forma e da Resolução Espacial",
        "sources": [{"title": "Função de Sensibilidade ao Contraste: Indicador da Percepção Visual da Forma e da Resolução Espacial"}],
        "facts": [{"claim": "A função de sensibilidade ao contraste é um indicador da percepção visual da forma e da resolução espacial."}],
    }

    assert pipeline._fact_pack_matches_topic(fact_pack, request, topic_plan) is False
    assert fact_pack["topic_alignment"]["passed"] is False


def test_fact_pack_rejects_optical_illusion_source_without_motion_for_motion_topic() -> None:
    pipeline = orchestrator.script_pipeline
    request = SimpleNamespace(seed_theme="Ilusão de ótica: por que imagem parada parece se mexer?")
    topic_plan = SimpleNamespace(
        canonical_topic="Ilusão de ótica de movimento parado",
        angle="Explicar por que certas imagens estáticas parecem se mover na visão periférica.",
        search_terms=["peripheral drift illusion", "illusory motion visual perception"],
        entities=["ilusão de ótica", "movimento ilusório"],
    )
    fact_pack = {
        "topic_title": "Ilusão transcendental e ilusão de ótica: a genealogia da ilusão nas obras kantianas",
        "sources": [{"title": "Ilusão transcendental e ilusão de ótica: a genealogia da ilusão nas obras kantianas"}],
        "facts": [{"claim": "O artigo discute a genealogia do termo ilusão em obras kantianas."}],
    }

    assert pipeline._fact_pack_matches_topic(fact_pack, request, topic_plan) is False
    assert fact_pack["topic_alignment"]["passed"] is False


def test_fact_pack_query_generation_protects_templarios_entity() -> None:
    request = SimpleNamespace(seed_theme="Templários: como monges-guerreiros viraram uma lenda poderosa")
    topic_plan = SimpleNamespace(
        canonical_topic="Templários - Ordem Militar e Religiosa",
        angle="A jornada real de monges-guerreiros que passaram de protetores de peregrinos a lenda.",
        title_candidates=["Templários: de monges-guerreiros a lenda"],
        search_terms=["grupo monges", "Ordem dos Templários", "Tomar templários"],
        entities=["Templários", "Ordem do Templo"],
    )

    pipeline = orchestrator.script_pipeline
    queries = pipeline._fact_pack_queries(request, topic_plan)
    topic_tokens = pipeline._fact_topic_tokens(request, topic_plan)
    cleaned = []
    seen = set()
    for query in queries:
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not pipeline._is_weak_fact_query(normalized):
            cleaned.append(normalized)
            seen.add(normalized.lower())
    filtered = [query for query in cleaned if pipeline._query_matches_primary_fact_topic(query, topic_tokens)]
    ordered = sorted(filtered, key=pipeline._fact_query_priority)

    assert topic_tokens == {"templarios"}
    assert "grupo monges" not in ordered
    assert ordered[0].lower() in {"templários", "templarios", "ordem dos templários", "tomar templários"}


def test_fact_query_priority_prefers_conceptual_entity_query_before_broad_entity() -> None:
    queries = [
        "Polvos: curiosidades científicas sobre o cefalópode mais inteligente do oceano",
        "polvos",
        "polvo corações pigmentos",
    ]

    ordered = sorted(queries, key=orchestrator._fact_query_priority)

    assert ordered[0] == "polvo corações pigmentos"
    assert ordered.index("polvos") > ordered.index("polvo corações pigmentos")


def test_fact_query_priority_keeps_exact_pisa_entity_before_derived_terms() -> None:
    queries = ["torre pisa solo", "torre pisa inclinação", "torre pisa"]

    ordered = sorted(queries, key=orchestrator._fact_query_priority)

    assert ordered[0] == "torre pisa"


def test_fact_pack_query_generation_uses_structured_topic_fields_without_dict_keys() -> None:
    request = SimpleNamespace(seed_theme="flamingos ficam rosas pela alimentação")
    topic_plan = SimpleNamespace(
        canonical_topic="Por que flamingos são cor-de-rosa pela alimentação",
        angle="surpresa científica visual",
        hook_promise="Descubra como flamingos nascem brancos e ficam cor-de-rosa apenas comendo",
        title_candidates=["{'text': 'Como flamingos ficam cor-de-rosa? A resposta está no cardápio', 'characters': 59}"],
        entities=["{'name': 'Flamingo', 'type': 'espécie_animal'}", "{'name': 'Astaxantina', 'type': 'pigmento_carotenoide'}"],
        search_terms=["flamingo cor rosa alimentação", "carotenoides flamingos"],
    )

    queries = []
    seen = set()
    for query in orchestrator._fact_pack_queries(request, topic_plan):
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not orchestrator._is_weak_fact_query(normalized):
            queries.append(normalized)
            seen.add(normalized.lower())

    top_queries = sorted(queries, key=orchestrator._fact_query_priority)[:8]

    assert "name diet" not in top_queries
    assert "text diet" not in top_queries
    assert any("flamingo" in query.lower() for query in top_queries)


def test_fact_query_removes_generic_viral_opening() -> None:
    cleaned = orchestrator._clean_fact_query("Você sabia? O cérebro humano tem um poder insano")

    assert "sabia" not in cleaned.lower()
    assert orchestrator._extract_fact_entity(cleaned) == "cérebro humano"


def test_weak_fact_query_filters_generic_single_word_angle() -> None:
    assert orchestrator._is_weak_fact_query("auto") is True
    assert orchestrator._is_weak_fact_query("descubra dieta diet") is True
    assert orchestrator._is_weak_fact_query("1325") is True
    assert orchestrator._is_weak_fact_query("duas cidades") is True
    assert orchestrator._is_weak_fact_query("polvos") is False


def test_script_postprocess_splits_long_sentences_before_gate() -> None:
    script = _base_script(
        "O espaço invisível parece distante mas atravessa sua vida todos os dias quando a luz viaja por regiões que ninguém consegue tocar diretamente. "
        "Esse efeito muda como você entende o céu."
    )

    processed = orchestrator._postprocess_script_for_quality(script, {"canonical_topic": "espaço"}, [])
    result = ScriptQualityGate().validate(processed, target_duration_sec=35)

    assert result.metrics["max_words_single_sentence"] <= 20


def test_publish_hashtags_use_entities_not_weak_words() -> None:
    topic_plan = SimpleNamespace(
        canonical_topic="Por que flamingos ficam cor-de-rosa",
        angle="A cor vem da cadeia alimentar invisível",
    )
    script = SimpleNamespace(
        title="A comida que pinta flamingos de rosa",
        key_facts=["Flamingos recebem pigmentos pela alimentação."],
    )

    tags = orchestrator._build_publish_hashtags(topic_plan, script)

    assert "#flamingos" in tags
    assert "#animais" in tags
    assert "#biologia" in tags
    assert "#ficam" not in tags
    assert "#cor" not in tags


def test_publish_hashtags_use_history_tags_for_templarios() -> None:
    topic_plan = SimpleNamespace(
        canonical_topic="Templários: de protetores de peregrinos a lenda medieval",
        angle="A ordem militar religiosa que virou lenda",
    )
    script = SimpleNamespace(
        title="Como os Templários viraram lenda medieval",
        key_facts=["A Ordem dos Templários nasceu para proteger peregrinos."],
    )

    tags = orchestrator._build_publish_hashtags(topic_plan, script)

    assert "#templarios" in tags
    assert "#historia" in tags
    assert "#medieval" in tags
    assert "#protetores" not in tags
    assert "#peregrinos" not in tags


def test_publish_hashtags_use_specific_geography_tags_for_danakil() -> None:
    topic_plan = SimpleNamespace(
        canonical_topic="Depressão de Danakil",
        angle="geografia extrema da Etiópia",
    )
    script = SimpleNamespace(
        title="Danakil: o lugar mais extremo da Terra onde pessoas ainda vivem",
        key_facts=["O povo Afar vive na região há gerações."],
    )

    tags = orchestrator._build_publish_hashtags(topic_plan, script)

    assert "#danakil" in tags
    assert "#etiopia" in tags
    assert "#geografia" in tags
    assert "#terra" not in tags
    assert "#lugar" not in tags


def test_build_publish_description_prefers_concise_summary_over_full_narration() -> None:
    description = orchestrator.monetization_pipeline.build_publish_description(
        SimpleNamespace(canonical_topic="Depressão de Danakil", angle="geografia extrema"),
        SimpleNamespace(
            hook="Na Depressão de Danakil, calor e sal parecem cenário de outro planeta.",
            body_beats=[
                "Mesmo assim, o povo Afar vive e trabalha ali há gerações.",
                "A paisagem mistura salinas, calor extremo e atividade geotérmica.",
            ],
            ending="É um dos ambientes habitados mais extremos do planeta.",
            full_narration="Texto mais longo que não deve virar a descrição inteira.",
        ),
        "Danakil: o lugar mais extremo da Terra onde pessoas ainda vivem",
        ["#shorts", "#curiosidades", "#danakil", "#etiopia", "#geografia"],
        "Imagens ilustrativas geradas por IA.",
    )

    assert "Texto mais longo que não deve virar a descrição inteira." not in description
    assert "Na Depressão de Danakil" in description
    assert "Imagens ilustrativas geradas por IA." in description
    assert "#shorts #curiosidades #danakil #etiopia #geografia" in description


def test_publish_readiness_blocks_limited_fact_pack_with_invented_source_ids(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.monetization_pipeline.settings, "simple_shorts_mode", False)
    script_artifact = {
        **_base_script(
            "Flamingos ficam rosas por pigmentos na alimentação. "
            "Essa cor pode sinalizar saúde para parceiros."
        ),
        "source_fact_ids": ["fact_1"],
    }
    topic_plan = SimpleNamespace(canonical_topic="Por que flamingos ficam cor-de-rosa", angle="biologia animal")
    checklist = {
        "script_gate_pass": True,
        "scene_plan_gate_pass": True,
        "asset_gate_pass": True,
        "subtitle_gate_pass": True,
        "render_gate_pass": True,
    }

    readiness = orchestrator._publish_readiness_report(
        None,
        topic_plan,
        {"status": "limited", "facts": []},
        ["#shorts", "#curiosidades", "#ciencia", "#flamingos", "#animais"],
        checklist,
        script_artifact,
    )

    assert readiness["passed"] is False
    assert "invented_source_fact_ids" in readiness["reasons"]
    assert "manual_review_required" in readiness["reasons"]


def test_trend_researcher_filters_curiosity_candidates() -> None:
    from app.trends import TrendResearcher

    researcher = TrendResearcher()

    assert researcher._is_curiosity_candidate("Flamingo") is True
    assert researcher._is_curiosity_candidate("Main_Page") is False
    assert researcher._is_curiosity_candidate("Lista de episódios") is False
    assert researcher._is_curiosity_candidate("acidente fatal na rodovia") is False
    assert researcher._is_curiosity_candidate("Darderi-Jodar: tempo, precedentes e onde assistir na TV aberta") is False


def test_trend_researcher_returns_none_without_google_candidates(monkeypatch) -> None:
    from app.trends import TrendResearcher

    monkeypatch.setattr(TrendResearcher, "_google_trends_candidates", lambda self: [])

    assert TrendResearcher().find_topic("curiosidades") is None


def test_google_trends_candidates_prioritize_familiar_topics(monkeypatch) -> None:
    from app.trends import TrendResearcher

    rss = """<?xml version='1.0' encoding='UTF-8'?>
    <rss xmlns:ht='https://trends.google.com/trending/rss' version='2.0'><channel>
      <item><title>Nahui Ollin</title><ht:approx_traffic>200K+</ht:approx_traffic></item>
      <item><title>chuva</title><ht:approx_traffic>50K+</ht:approx_traffic></item>
      <item><title>celular</title><ht:approx_traffic>20K+</ht:approx_traffic></item>
    </channel></rss>"""

    class FakeResponse:
        text = rss
        def raise_for_status(self) -> None: pass

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def get(self, url): return FakeResponse()

    monkeypatch.setattr("app.trends.httpx.Client", FakeClient)
    candidates = TrendResearcher()._google_trends_candidates()

    assert candidates
    assert max(candidates, key=lambda candidate: candidate.score).raw_title == "chuva"
    assert all(candidate.source == "google_trends_br" for candidate in candidates)


def test_google_trends_candidates_reject_news_title_without_fact_friendly_topic(monkeypatch) -> None:
    from app.trends import TrendResearcher

    rss = """<?xml version='1.0' encoding='UTF-8'?>
    <rss xmlns:ht='https://trends.google.com/trending/rss' version='2.0'><channel>
      <item>
        <title>br-040</title>
        <ht:approx_traffic>20K+</ht:approx_traffic>
        <ht:news_item><ht:news_item_title>Produtos químicos pegam fogo e levantam alerta sobre segurança nas estradas</ht:news_item_title></ht:news_item>
      </item>
    </channel></rss>"""

    class FakeResponse:
        text = rss

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("app.trends.httpx.Client", FakeClient)
    candidates = TrendResearcher()._google_trends_candidates()

    assert candidates == []


def test_google_trends_candidates_reject_live_sports_watch_topic(monkeypatch) -> None:
    from app.trends import TrendResearcher

    rss = """<?xml version='1.0' encoding='UTF-8'?>
    <rss xmlns:ht='https://trends.google.com/trending/rss' version='2.0'><channel>
      <item>
        <title>luciano darderi</title>
        <ht:approx_traffic>50K+</ht:approx_traffic>
        <ht:news_item><ht:news_item_title>Darderi-Jodar: tempo, precedentes e onde assistir na TV aberta</ht:news_item_title></ht:news_item>
      </item>
    </channel></rss>"""

    class FakeResponse:
        text = rss

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("app.trends.httpx.Client", FakeClient)
    candidates = TrendResearcher()._google_trends_candidates()

    assert candidates == []
