from __future__ import annotations

import audioop
import json
import math
import os
import shutil
import threading
import time
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError


os.environ.setdefault("YTS_DATA_DIR", str(Path("data-test").resolve()))
os.environ.setdefault("YTS_DATABASE_URL", f"sqlite:///{Path('data-test/test.db').resolve()}")
os.environ.setdefault("YTS_USE_MOCK_PROVIDERS", "true")

import app.main as main_module  # noqa: E402
import app.orchestrator as orchestrator_module  # noqa: E402
import app.pipelines.script_pipeline as script_pipeline_module  # noqa: E402
from app.compliance.review import build_human_review_checklist  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.editorial.retention import EDITORIAL_PROMPT_VERSION, build_retention_map  # noqa: E402
from app.editorial.repetition import build_channel_repetition_report  # noqa: E402
from app.main import app, artifact_url  # noqa: E402
from app.models import BackgroundMusicAsset, Job, NarrationAsset, PerformanceMetric, RenderOutput, SceneAsset, Script, SubtitleTrack, TopicPlan, TopicRegistry, TopicRequest  # noqa: E402
from app.orchestrator import JobOrchestrator, RecoverableStepError, StepDefinition, normalize_script_metrics, orchestrator  # noqa: E402
from app.providers import DeepSeekCreativeProvider, LLMProviderRegistry, LocalSpeechFallbackProvider, MiniMaxBackgroundMusicProvider, MinimaxCreativeProvider, MinimaxImageProvider, MockCreativeProvider, ProviderFailure, QwenCreativeProvider, ResilientCreativeProvider, ResilientMusicProvider  # noqa: E402
from app.quality.asset_gate import AssetGate  # noqa: E402
from app.quality.render_gate import RenderGate  # noqa: E402
from app.quality.scene_gate import ScenePlanGate  # noqa: E402
from app.quality.script_gate import ScriptQualityGate  # noqa: E402
from app.quality.subtitle_gate import SubtitleGate  # noqa: E402
from app.utils import parse_srt, split_caption_chunks, utcnow, word_tokens, wrap_caption  # noqa: E402


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
        assert background_music.gain_db == -20.0
        assert Path(background_music.audio_uri.removeprefix("file://")).exists()
        assert Path(background_music.mixed_audio_uri.removeprefix("file://")).exists()
        assert len(selected_assets) >= 5
        assert job and job.quality_summary["render"]["render_gate_pass"] is True
        assert job.quality_summary["background_music"]["enabled"] is True
        assert job.quality_summary["monetization"]["final_status"] == "monetization_review"
        assert job.artifact_index["publish_package"] == "publish_package.json"
        assert job.artifact_index["monetization_report"] == "monetization_report.json"
        assert job.artifact_index["background_music"] == "audio/background_source.wav"
        assert job.artifact_index["mixed_audio"] == "audio/mixed.wav"
        assert job.artifact_index["performance_timeline"] == "performance_timeline.json"
        timeline_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / job_id / "performance_timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        assert any(step["step_name"] == "render" and step["duration_ms"] is not None for step in timeline["steps"])
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Render" in detail.text
    assert "Audio &amp; Subtitles" in detail.text


def test_artifact_url_maps_file_uri_to_static_route() -> None:
    artifact_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / "job-1" / "render" / "final.mp4"
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


def test_hub_prompt_panel_saves_and_resets_safe_template(monkeypatch, tmp_path: Path) -> None:
    prompt_path = tmp_path / "hub_settings.json"
    monkeypatch.setattr(main_module, "_hub_settings_path", lambda: prompt_path)
    monkeypatch.setattr(main_module, "_default_seed_theme", lambda: "abelhas")
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert "⚙" in page.text
    assert "Prompt viral" in page.text
    assert main_module.DEFAULT_VIRAL_PROMPT_TEMPLATE.splitlines()[0] in page.text

    custom_prompt = "Priorize gancho contraintuitivo, titulo SEO e payoff visual."
    save = client.post("/hub/prompt", data={"viral_prompt_template": custom_prompt, "action": "save"}, follow_redirects=False)
    assert save.status_code == 303
    assert main_module._viral_prompt_template() == custom_prompt

    reset = client.post("/hub/prompt", data={"action": "reset"}, follow_redirects=False)
    assert reset.status_code == 303
    assert main_module._viral_prompt_template() == main_module.DEFAULT_VIRAL_PROMPT_TEMPLATE


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
        "estimated_duration_sec": 30,
        "language": "pt-BR",
        "qa_metrics": {
            "hook_score": 0.9,
            "clarity_score": 0.9,
            "information_density_score": 0.9,
            "repetition_score": 0.2,
            "ending_strength_score": 0.9,
        },
    }
    result = ScriptQualityGate().validate(script, target_duration_sec=32)
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


def test_qwen_provider_uses_max_openai_compatible_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    settings = SimpleNamespace(
        qwen_api_key="qwen-key",
        qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        qwen_model="qwen3.6-max-preview",
        qwen_timeout_sec=90,
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

    provider = QwenCreativeProvider()
    result = provider.generate_script({"canonical_topic": "flamingos", "angle": "cor pela alimentação"})

    assert captured["client_kwargs"]["api_key"] == "qwen-key"
    assert captured["client_kwargs"]["base_url"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert captured["model"] == "qwen3.6-max-preview"
    assert result["qa_metrics"]["source_provider"] == "qwen"


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


def test_minimax_text_and_image_providers_use_dedicated_keys(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_openai(**kwargs):
        captured["text_api_key"] = kwargs["api_key"]
        captured["text_base_url"] = kwargs["base_url"]
        return object()

    settings = SimpleNamespace(
        resolved_minimax_text_api_key="text-key",
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
    assert image.key == "image-key"
    assert image.url == "https://image.example/v1/image_generation"


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


def test_resilient_creative_provider_uses_fast_script_draft_first() -> None:
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
            return {
                "title": "Roteiro rapido",
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

    class Primary:
        provider_name = "minimax"

        def generate_script(self, topic_plan):
            raise AssertionError("primary should not be called when draft succeeds")

    provider.script_draft_provider = Draft()
    provider.primary = Primary()
    provider.fallback = None

    script = provider.generate_script({"canonical_topic": "polvos"})

    assert script["qa_metrics"]["generation_provider_role"] == "draft"
    assert script["qa_metrics"]["generation_provider"] == "deepseek"
    assert script["qa_metrics"]["script_generation_fallback_used"] is False


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


def test_resilient_music_provider_requires_minimax_success_in_strict_mode(monkeypatch) -> None:
    settings = SimpleNamespace(use_mock_providers=False, resolved_minimax_music_api_key="music-key", strict_minimax_validation=True)

    class FailingMusicProvider:
        def select_track(self, *args, **kwargs):
            raise RuntimeError("minimax music unavailable")

    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.MiniMaxBackgroundMusicProvider", lambda: FailingMusicProvider())

    provider = ResilientMusicProvider()

    try:
        provider.select_track({}, {}, Path("/tmp/out.wav"), 30_000)
    except ProviderFailure as exc:
        assert "strict minimax validation requires minimax music success" in str(exc)
    else:
        raise AssertionError("expected ProviderFailure")


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
            "target_duration_sec": 32,
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
            "trend_research=real_source\ntrend_source=wikipedia_pageviews_pt",
        ),
    )
    monkeypatch.setattr(main_module.orchestrator, "create_job", fake_create_job)
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert 'name="niche_id" value="curiosidades"' in page.text
    assert 'name="seed_theme" value=""' in page.text
    assert "Vazio = pesquisar tendências reais" in page.text
    assert 'name="target_duration_sec" type="number" min="25" max="45" value="32"' in page.text

    response = client.post("/jobs", data={"seed_theme": "", "input_mode": "theme"}, follow_redirects=False)
    assert response.status_code == 303
    assert captured["seed_theme"] == "Por que flamingos estão em alta?"
    assert captured["requested_angle"] == "Transformar tendência real em curiosidade verificável."
    assert "trend_research=real_source" in str(captured["notes"])
    assert captured["niche_id"] == "curiosidades"
    assert captured["target_duration_sec"] == 32


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
                    target_duration_sec=32,
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

    assert approve.status_code == 409
    assert "cannot be approved" in approve.text


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

    assert response.status_code == 409
    assert "manual publish requires" in response.text


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
            "target_duration_sec": 32,
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
            "target_duration_sec": 32,
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
    assert normalized["quality_metrics"]["topic_repair_used"] is True


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
    duration_sec = 48
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
        "1\n00:00:00,000 --> 00:00:24,000\nprimeira metade\n\n"
        "2\n00:00:24,000 --> 00:00:48,000\nsegunda metade\n",
        encoding="utf-8",
    )

    result = orchestrator._fit_tts_duration(
        audio_path,
        srt_path,
        {"duration_ms": 48_000, "provider_metadata": {"mode": "edge"}},
    )
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))

    assert 43_000 <= result["duration_ms"] <= 44_000
    assert result["provider_metadata"]["duration_fit_applied"] is True
    assert cues[-1]["end_ms"] <= 43_600


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


def test_human_review_checklist_marks_required_completed_and_pending_items() -> None:
    checklist = build_human_review_checklist(
        rights_registry={"all_commercial_rights_confirmed": False},
        ai_disclosure={"youtube_disclosure_required": True},
        fact_claims_report={"requires_fact_review": False},
        metadata_review={"requires_metadata_review": True},
        channel_repetition_report={"repetition_risk": "medium"},
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
        confirmations=set(),
    )

    assert "youtube_ai_disclosure_toggle_required" in checklist["completed_codes"]
    assert "youtube_ai_disclosure_toggle_required" not in checklist["pending_codes"]
    disclosure_item = next(item for item in checklist["items"] if item["code"] == "youtube_ai_disclosure_toggle_required")
    assert disclosure_item["auto_completed"] is True


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


def test_publish_audit_failures_become_automatic_hard_blockers() -> None:
    blockers = orchestrator.monetization_pipeline.automatic_publish_blockers(
        {
            "passed": False,
            "reasons": [
                "source_fact_mismatch",
                "unsupported_claim",
                "rights_confirmation_required",
                "low_retention_hook",
            ],
        }
    )

    assert blockers == ["source_fact_mismatch", "unsupported_claim", "low_retention_hook"]


def test_text_publish_audit_has_hard_timeout(monkeypatch) -> None:
    pipeline = orchestrator.script_pipeline
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


def test_rights_registry_requires_evidence_for_confirmed_minimax_assets(monkeypatch) -> None:
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


def _base_script(full_narration: str) -> dict[str, object]:
    return {
        "title": "Curiosidade científica em menos de um minuto",
        "hook": full_narration.split(".")[0] + ".",
        "body_beats": [full_narration],
        "ending": "No fim, essa curiosidade científica muda como você olha para o tema.",
        "cta": None,
        "full_narration": full_narration,
        "estimated_duration_sec": 32,
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
    assert debug["fact_pack_status"] in {"limited", "verified"}


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


def test_fact_query_priority_prefers_conceptual_entity_query_before_broad_entity() -> None:
    queries = [
        "Polvos: curiosidades científicas sobre o cefalópode mais inteligente do oceano",
        "polvos",
        "polvo corações pigmentos",
    ]

    ordered = sorted(queries, key=orchestrator._fact_query_priority)

    assert ordered[0] == "polvo corações pigmentos"
    assert ordered.index("polvos") > ordered.index("polvo corações pigmentos")


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


def test_publish_readiness_blocks_limited_fact_pack_with_invented_source_ids() -> None:
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


def test_trend_researcher_filters_wikipedia_candidates() -> None:
    from app.trends import TrendResearcher

    researcher = TrendResearcher()

    assert researcher._clean_wikipedia_title("Flamingo") == "Flamingo"
    assert researcher._is_curiosity_candidate("Flamingo") is True
    assert researcher._is_curiosity_candidate("Main_Page") is False
    assert researcher._is_curiosity_candidate("Lista de episódios") is False


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
