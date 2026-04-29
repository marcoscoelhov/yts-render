from __future__ import annotations

import audioop
import math
import os
import shutil
import time
import wave
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient


os.environ.setdefault("YTS_DATA_DIR", str(Path("data-test").resolve()))
os.environ.setdefault("YTS_DATABASE_URL", f"sqlite:///{Path('data-test/test.db').resolve()}")
os.environ.setdefault("YTS_USE_MOCK_PROVIDERS", "true")

import app.main as main_module  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app, artifact_url  # noqa: E402
from app.models import Job, RenderOutput, SceneAsset, SubtitleTrack, TopicRegistry, TopicRequest  # noqa: E402
from app.orchestrator import normalize_script_metrics, orchestrator  # noqa: E402
from app.providers import LocalSpeechFallbackProvider, MinimaxCreativeProvider, MinimaxImageProvider, MockCreativeProvider  # noqa: E402
from app.utils import parse_srt, split_caption_chunks, wrap_caption  # noqa: E402


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


def test_artifact_url_maps_file_uri_to_static_route() -> None:
    artifact_path = Path(os.environ["YTS_DATA_DIR"]).resolve() / "artifacts" / "job-1" / "render" / "final.mp4"
    assert artifact_url(artifact_path.as_uri()) == "/artifacts/job-1/render/final.mp4"


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
    assert "ignore esse formato e mantenha exatamente o JSON estrito" in prompt


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


def test_hub_uses_curiosidades_random_theme_and_retention_duration_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_job(payload: dict[str, object]) -> str:
        captured.update(payload)
        return "job-defaults"

    monkeypatch.setattr(main_module, "_default_seed_theme", lambda: "abelhas")
    monkeypatch.setattr(main_module.orchestrator, "create_job", fake_create_job)
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert 'name="niche_id" value="curiosidades"' in page.text
    assert 'name="seed_theme" value="abelhas"' in page.text
    assert 'name="target_duration_sec" type="number" min="25" max="45" value="32"' in page.text

    response = client.post("/jobs", data={"seed_theme": "", "input_mode": "theme"}, follow_redirects=False)
    assert response.status_code == 303
    assert captured["seed_theme"] == "abelhas"
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
