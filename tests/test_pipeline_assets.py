from tests.e2e_support import *  # noqa: F403


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

def test_llm_registry_uses_deepseek_for_repair_and_scene_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.providers.llm.get_settings",
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

def test_scene_plan_gate_rejects_hook_intent_that_violates_visual_contract() -> None:
    visual_contract = {
        "hook_frame": {
            "recommended_visual_intent": "deceptive_establishing",
            "must_show": ["cidade", "rua"],
            "must_hide": ["mesa"],
        },
        "loop_policy": {"forbidden_early_reveal": ["cidade inteira cabe numa mesa"]},
        "payoff_frame": {"recommended_visual_intent": "loop_close_reframe"},
    }
    result = ScenePlanGate().validate(
        [
            {
                "scene_id": "scene-1",
                "order": 1,
                "retention_role": "visual_hook",
                "visual_intent": "subject_closeup",
                "narration_text": "Isso parece uma cidade abandonada de verdade.",
                "token_start": 0,
                "token_end": 7,
                "primary_subject": "cidade abandonada",
                "image_prompt": "miniature abandoned city street, no readable text anywhere",
            }
        ],
        expected_scene_count=1,
        visual_contract=visual_contract,
    )

    assert not result.passed
    assert "scene-1:visual_contract_hook_intent_mismatch" in result.reasons

def test_scene_plan_gate_matches_visual_contract_hook_terms_semantically() -> None:
    visual_contract = {
        "hook_frame": {
            "recommended_visual_intent": "deceptive establishing",
            "must_show": [
                "Quarteirões, edifícios, ruas, árvores em escala urbana",
                "Iluminação que sugere luz natural de drone",
            ],
            "must_hide": ["mãos", "régua"],
        },
        "loop_policy": {"forbidden_early_reveal": ["Mostrar a escala real"]},
        "payoff_frame": {"recommended_visual_intent": "scale reveal"},
    }
    result = ScenePlanGate().validate(
        [
            {
                "scene_id": "scene-1",
                "order": 1,
                "retention_role": "visual_hook",
                "visual_intent": "deceptive_establishing",
                "narration_text": "Isso não é uma foto aérea. É uma maquete.",
                "token_start": 0,
                "token_end": 7,
                "primary_subject": "Vista aérea de uma maquete urbana que parece real",
                "image_prompt": (
                    "Aerial view of a miniature city model with realistic buildings, streets, "
                    "and trees, shot from above like a drone photograph, no visible borders or hands, "
                    "no readable text anywhere"
                ),
            },
            {
                "scene_id": "scene-2",
                "order": 2,
                "retention_role": "loop_close",
                "visual_intent": "scale_reveal",
                "narration_text": "Agora a escala aparece.",
                "token_start": 8,
                "token_end": 12,
                "primary_subject": "maquete pequena",
                "image_prompt": "tiny model city in a hand, no readable text anywhere",
            },
        ],
        expected_scene_count=2,
        visual_contract=visual_contract,
    )

    assert result.passed

def test_visual_contract_normalization_drops_forbidden_reveal_that_conflicts_with_approved_beat() -> None:
    from app.editorial.visual_contract import normalize_visual_contract_payload

    contract = normalize_visual_contract_payload(
        {
            "loop_policy": {
                "forbidden_early_reveal": [
                    "diagramas de osmose",
                    "cidade inteira cabe numa mesa",
                ]
            },
            "beat_progression": [
                {
                    "source_text": "O açúcar concentrado puxa água das células por osmose.",
                    "visual_job": "Mostrar o processo de osmose em uma célula microbiana.",
                }
            ],
        },
        script={"title": "Mel", "hook": "O mel parece doce.", "ending": "A cidade cabe na mesa."},
        schema_version="1.0.0",
    )

    assert contract["loop_policy"]["forbidden_early_reveal"] == ["cidade inteira cabe numa mesa"]

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

def test_asset_visual_gate_accepts_hook_asset_aligned_with_visual_contract() -> None:
    scenes = [
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "visual_intent": "deceptive_establishing",
            "narration_text": "Isso parece uma cidade abandonada de verdade.",
            "primary_subject": "cidade abandonada com rua vazia",
            "image_prompt": "cidade abandonada com rua vazia em maquete realista, no readable text anywhere",
        },
        {
            "scene_id": "scene-2",
            "order": 2,
            "retention_role": "loop_close",
            "visual_intent": "loop_close_reframe",
            "narration_text": "No final, a cidade inteira cabe em uma mesa.",
            "primary_subject": "maquete urbana sobre uma mesa",
            "image_prompt": "maquete urbana sobre uma mesa revelando a escala, no readable text anywhere",
        },
    ]
    selected_assets = [
        {
            "scene_id": "scene-1",
            "provider": "minimax",
            "semantic_match": 0.91,
            "total_score": 0.88,
            "prompt_snapshot": "miniature abandoned city street, documentary realism, no readable text anywhere",
        },
        {
            "scene_id": "scene-2",
            "provider": "minimax",
            "semantic_match": 0.90,
            "total_score": 0.87,
            "prompt_snapshot": "miniature city on a table payoff reveal, no readable text anywhere",
        },
    ]
    visual_contract = {
        "hook_frame": {
            "recommended_visual_intent": "deceptive_establishing",
            "must_show": ["cidade", "rua"],
            "must_hide": ["mesa"],
            "negative_reads": ["imagem abstrata"],
        },
        "loop_policy": {"forbidden_early_reveal": ["cidade inteira cabe em uma mesa"]},
        "payoff_frame": {"recommended_visual_intent": "loop_close_reframe"},
    }

    result = AssetVisualGate().validate(selected_assets, scenes, visual_contract=visual_contract)

    assert result.passed
    assert result.metrics["asset_visual_gate_pass"] is True
    assert result.metrics["checked"] is True

def test_asset_visual_gate_rejects_weak_hook_asset_against_visual_contract() -> None:
    scenes = [
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "visual_intent": "deceptive_establishing",
            "narration_text": "Isso parece uma cidade abandonada de verdade.",
            "primary_subject": "textura generica",
            "image_prompt": "imagem abstrata generica, no readable text anywhere",
        },
        {
            "scene_id": "scene-2",
            "order": 2,
            "retention_role": "loop_close",
            "visual_intent": "loop_close_reframe",
            "narration_text": "No final, a cidade inteira cabe em uma mesa.",
            "primary_subject": "maquete urbana sobre uma mesa",
            "image_prompt": "maquete urbana sobre uma mesa revelando a escala, no readable text anywhere",
        },
    ]
    selected_assets = [
        {
            "scene_id": "scene-1",
            "provider": "minimax",
            "semantic_match": 0.78,
            "total_score": 0.74,
            "prompt_snapshot": "imagem abstrata generica sem rua reconhecivel",
        },
        {
            "scene_id": "scene-2",
            "provider": "minimax",
            "semantic_match": 0.90,
            "total_score": 0.87,
            "prompt_snapshot": "maquete urbana sobre uma mesa revelando a escala",
        },
    ]
    visual_contract = {
        "hook_frame": {
            "recommended_visual_intent": "deceptive_establishing",
            "must_show": ["fachadas reconheciveis"],
            "must_hide": ["mesa"],
            "negative_reads": ["imagem abstrata"],
        },
        "loop_policy": {"forbidden_early_reveal": ["cidade inteira cabe em uma mesa"]},
        "payoff_frame": {"recommended_visual_intent": "loop_close_reframe"},
    }

    result = AssetVisualGate().validate(selected_assets, scenes, visual_contract=visual_contract)

    assert not result.passed
    assert "scene-1:hook_semantic_match_below_visual_threshold" in result.reasons
    assert "scene-1:hook_total_score_below_visual_threshold" in result.reasons
    assert "scene-1:hook_must_show_missing_from_asset_prompt" in result.reasons
    assert "scene-1:hook_negative_read_present_in_asset_prompt" in result.reasons

def test_subtitle_gate_blocks_markup_leakage() -> None:
    result = SubtitleGate().validate(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "Texto bom </prosody"}],
        coverage_ratio=1.0,
    )
    assert not result.passed
    assert "1:markup_or_ssml_leaked" in result.reasons

def test_subtitle_gate_blocks_visual_wraps() -> None:
    result = SubtitleGate().validate(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "Essa legenda antiga ainda quebra em duas linhas"}],
        coverage_ratio=1.0,
    )
    assert not result.passed
    assert "1:subtitle_wraps_multiple_lines" in result.reasons

def test_subtitle_gate_rejects_large_timing_drift() -> None:
    result = SubtitleGate().validate(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "Texto bom"}],
        coverage_ratio=1.0,
        p95_drift_ms=1300,
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

    report = orchestrator.asset_pipeline.subtitles.estimate_subtitle_timing_drift(cues, items)

    assert report["timing_basis"] == "raw_srt_token_timeline"
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

def test_minimax_scene_prompt_requires_first_scene_visual_hook(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_json_completion(self, prompt: str) -> list[dict[str, object]]:
        captured["prompt"] = prompt
        return [
            {
                "scene_id": "scene-1",
                "order": 1,
                "narration_text": "Cena em pt-BR com impacto visual.",
                "token_start": 0,
                "token_end": 5,
                "estimated_duration_sec": 5,
                "visual_intent": "subject_closeup",
                "primary_subject": "animal real",
                "image_prompt": "high-impact vertical first-frame hook of a real animal, no readable text anywhere",
                "fallback_queries": ["animal real"],
            }
        ]

    monkeypatch.setattr(MinimaxCreativeProvider, "_json_completion", fake_json_completion)
    provider = object.__new__(MinimaxCreativeProvider)
    provider.plan_scenes(
        {"title": "Teste", "hook": "O animal muda antes de você notar.", "full_narration": "Cena em pt-BR com impacto visual.", "estimated_duration_sec": 5},
        1,
    )

    prompt = captured["prompt"]
    assert "scene with order=1 is the visual hook frame" in prompt
    assert "instantly legible in under one second" in prompt
    assert "do not reveal a later payoff" in prompt
    assert "retention_role" in prompt
    assert 'scene order=1 deve ter retention_role="visual_hook"' in prompt
    assert "usar visual_opening como brief visual" in prompt

def test_minimax_scene_prompt_uses_visual_contract(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_json_completion(self, prompt: str) -> list[dict[str, object]]:
        captured["prompt"] = prompt
        return [
            {
                "scene_id": "scene-1",
                "order": 1,
                "narration_text": "Cena em pt-BR com cidade falsa.",
                "token_start": 0,
                "token_end": 5,
                "estimated_duration_sec": 5,
                "retention_role": "visual_hook",
                "visual_intent": "deceptive_establishing",
                "primary_subject": "cidade falsa",
                "image_prompt": "abandoned miniature city street, no readable text anywhere",
                "fallback_queries": ["cidade falsa"],
            }
        ]

    monkeypatch.setattr(MinimaxCreativeProvider, "_json_completion", fake_json_completion)
    provider = object.__new__(MinimaxCreativeProvider)
    provider.plan_scenes(
        {
            "title": "Teste",
            "hook": "Isso parece uma cidade real.",
            "full_narration": "Cena em pt-BR com cidade falsa.",
            "estimated_duration_sec": 5,
            "visual_contract": {
                "hook_frame": {
                    "recommended_visual_intent": "deceptive_establishing",
                    "must_show": ["rua", "fachadas"],
                    "must_hide": ["mesa", "mão humana"],
                },
                "loop_policy": {"forbidden_early_reveal": ["cabe numa mesa"]},
            },
        },
        1,
    )

    prompt = captured["prompt"]
    assert "fonte da verdade visual" in prompt
    assert "recommended_visual_intent" in prompt
    assert "forbidden_early_reveal" in prompt
    assert "nao force linguagem de scientific visualization" in prompt or "generic scientific styling" in prompt

def test_scientific_mechanism_image_prompt_uses_conservative_visual_directive() -> None:
    prompt = orchestrator.asset_pipeline.image_assets.semantic_english_image_prompt(
        {
            "narration_text": "dois corações empurram sangue para as brânquias",
            "primary_subject": "dois corações do polvo ligados às brânquias",
            "visual_intent": "process_or_mechanism",
            "image_prompt": "Vertical cinematic close-up with semi-transparent anatomical view revealing organs.",
        },
        "polvos biologia",
        "polvo",
    )

    assert "conservative science visual" in prompt
    assert "avoid invented organs" in prompt

def test_non_science_visual_domain_removes_scientific_prompt_style() -> None:
    prompt = orchestrator.asset_pipeline.image_assets.semantic_english_image_prompt(
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "visual_domain": "miniature urban diorama / craft documentary realism",
            "narration_text": "Isso parece uma cidade abandonada de verdade.",
            "primary_subject": "diorama de cidade abandonada",
            "visual_intent": "deceptive_establishing",
            "image_prompt": "vertical cinematic scientific image of a miniature abandoned city street, scientific visualization",
        },
        "diorama de cidade abandonada",
        "diorama de cidade abandonada",
    ).lower()

    assert "miniature craft documentary realism" in prompt
    assert "scientific visualization" not in prompt
    assert "scientific image" not in prompt
    assert "clean vertical cinematic scientific image" not in prompt

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

    monkeypatch.setattr("app.providers.llm.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.image.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.llm.OpenAI", fake_openai)

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

    monkeypatch.setattr("app.providers.image.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.music.httpx.post", fake_post)

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

    monkeypatch.setattr("app.providers.image.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.image.httpx.post", fake_post)
    monkeypatch.setattr("app.providers.image.time.sleep", lambda _: None)

    provider = MinimaxImageProvider()
    with pytest.raises(ProviderFailure, match="connection failed after 3 attempts"):
        provider.generate({"job_id": "job-timeout", "image_prompt": "vertical science image"}, tmp_path / "timeout.png")

    assert calls == ["Bearer text-key", "Bearer text-key", "Bearer text-key"]
    assert provider._primary_exhausted_for_job("job-timeout") is False

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

    monkeypatch.setattr("app.providers.music.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.music.MiniMaxBackgroundMusicProvider", lambda: FailingMusicProvider())

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
                        "instrumental": True,
                        "vocals_or_lyrics": "none",
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
                        "instrumental": True,
                        "vocals_or_lyrics": "none",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.providers.music.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir))

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

def test_local_music_bank_provider_rejects_tracks_with_audible_vocals(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    vocal_path = bank_dir / "tracks" / "vocal.wav"
    instrumental_path = bank_dir / "tracks" / "instrumental.wav"
    _write_test_wave(vocal_path, duration_ms=700)
    _write_test_wave(instrumental_path, duration_ms=700)
    (bank_dir / "manifest.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "id": "vocal-track",
                        "path": "tracks/vocal.wav",
                        "moods": ["technology"],
                        "license": "Approved",
                        "source_url": "https://example.com/vocal",
                        "approved_for_youtube": True,
                        "content_id_registered": False,
                        "instrumental": True,
                        "vocals_or_lyrics": "audible_vocal",
                    },
                    {
                        "id": "instrumental-track",
                        "path": "tracks/instrumental.wav",
                        "moods": ["technology"],
                        "license": "Approved",
                        "source_url": "https://example.com/instrumental",
                        "approved_for_youtube": True,
                        "content_id_registered": False,
                        "instrumental": True,
                        "vocals_or_lyrics": "none",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.providers.music.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir))

    result = LocalMusicBankProvider().select_track(
        {"canonical_topic": "cafeína", "angle": "tecnologia do cérebro"},
        {"title": "Cafeína", "hook": "Café mexe com o alerta."},
        tmp_path / "out.wav",
        1500,
    )

    assert result["provider_metadata"]["track_id"] == "instrumental-track"

def test_background_music_mood_is_inferred_from_full_script() -> None:
    from app.providers.music import infer_background_music_mood

    cases = [
        (
            {"canonical_topic": "Erupção do Monte Tambora", "angle": "crise climática global"},
            {
                "title": "Vulcão Tambora roubou o verão do planeta",
                "hook": "1816 teve neve quando deveria ter colheita.",
                "ending": "Um vulcão gritou numa ilha, e o planeta inteiro sentiu frio.",
                "full_narration": (
                    "O Tambora explodiu na Indonésia em 1815. A luz solar foi parcialmente bloqueada. "
                    "Temperaturas caíram, plantações falharam e a fome se espalhou."
                ),
            },
            "cinematic",
        ),
        (
            {"canonical_topic": "Shanay-timpishka", "angle": "rio quase fervente sem vulcão visível"},
            {
                "title": "Rio fervente da Amazônia queima o que cai nele",
                "hook": "Água quase fervendo corre no meio da floresta.",
                "ending": "Também esconde água que queima.",
                "full_narration": "A água pode ficar quente o bastante para queimar pele. Animais pequenos podem não escapar.",
            },
            "suspense",
        ),
        (
            {"canonical_topic": "Peixe-pescador abissal", "angle": "bioluminescência no fundo do mar"},
            {
                "title": "Peixe-pescador usa luz viva para atrair vítimas",
                "hook": "Escuridão total vira isca quando esse peixe acende.",
                "ending": "A luz no fim do túnel pode ser uma boca.",
                "full_narration": "Presas se aproximam achando que encontraram comida. Quando chegam perto, o escuro abre dentes.",
            },
            "suspense",
        ),
        (
            {"canonical_topic": "Chuva vermelha", "angle": "fenômeno natural que parece presságio"},
            {
                "title": "Chuva de sangue já pintou cidades de vermelho",
                "hook": "O céu ficou vermelho e parecia sangrar sobre pessoas.",
                "ending": "O céu não estava sangrando. Estava carregando terra.",
                "full_narration": "Relatos antigos descrevem chuvas vermelhas como presságios terríveis. O susto parece sobrenatural.",
            },
            "suspense",
        ),
    ]

    for topic_plan, script, expected_mood in cases:
        assert infer_background_music_mood(topic_plan, script) == expected_mood

def test_local_music_bank_provider_prefers_script_mood_over_generic_topic(monkeypatch, tmp_path: Path) -> None:
    bank_dir = tmp_path / "music_bank"
    suspense_path = bank_dir / "tracks" / "suspense.wav"
    cinematic_path = bank_dir / "tracks" / "cinematic.wav"
    _write_test_wave(suspense_path, duration_ms=700, amplitude=2600, freq_hz=92.5)
    _write_test_wave(cinematic_path, duration_ms=700, amplitude=2600, freq_hz=130.8)
    (bank_dir / "manifest.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "id": "cinematic-01",
                        "path": "tracks/cinematic.wav",
                        "moods": ["cinematic"],
                        "license": "Approved",
                        "source_url": "https://example.com/cinematic",
                        "approved_for_youtube": True,
                        "content_id_registered": False,
                        "instrumental": True,
                        "vocals_or_lyrics": "none",
                    },
                    {
                        "id": "suspense-01",
                        "path": "tracks/suspense.wav",
                        "moods": ["suspense"],
                        "license": "Approved",
                        "source_url": "https://example.com/suspense",
                        "approved_for_youtube": True,
                        "content_id_registered": False,
                        "instrumental": True,
                        "vocals_or_lyrics": "none",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.providers.music.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir))

    provider = LocalMusicBankProvider()
    result = provider.select_track(
        {"canonical_topic": "curiosidade natural", "angle": "explicação científica"},
        {
            "title": "Chuva de sangue já pintou cidades de vermelho",
            "hook": "O céu ficou vermelho e parecia sangrar sobre pessoas.",
            "ending": "O susto parecia sobrenatural, mas vinha da atmosfera.",
            "full_narration": "Relatos antigos descrevem chuvas vermelhas como presságios terríveis.",
        },
        tmp_path / "out" / "background.wav",
        1500,
    )

    assert result["mood"] == "suspense"
    assert result["provider_metadata"]["track_id"] == "suspense-01"

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
                    "instrumental": True,
                    "vocals_or_lyrics": "none",
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
    monkeypatch.setattr("app.providers.music.get_settings", lambda: settings)

    def fail_if_minimax_is_used():
        raise AssertionError("MiniMax should not be used when local bank succeeds")

    monkeypatch.setattr("app.providers.music.MiniMaxBackgroundMusicProvider", fail_if_minimax_is_used)

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
    monkeypatch.setattr("app.providers.music.get_settings", lambda: settings)

    def fast_populate(target_bank_dir: Path, *args, **kwargs):
        return populate_builtin_music_bank(target_bank_dir, duration_seconds=1)

    monkeypatch.setattr("app.providers.music.populate_builtin_music_bank", fast_populate)

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
                    "vocals_or_lyrics": "none",
                    "human_instrumental_review_confirmed": True,
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
    assert track["instrumental"] is True
    assert track["vocals_or_lyrics"] == "none"
    assert track["human_instrumental_review_confirmed"] is True
    assert track["source_url"] == "https://example.com/music.wav"
    assert "Signature" not in json.dumps(track)
    assert (bank_dir / track["path"]).exists()
    assert (bank_dir / track["license_file"]).read_text(encoding="utf-8").find("trace-123") >= 0

def test_local_music_bank_provider_ignores_unreviewed_imported_minimax(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr("app.providers.music.get_settings", lambda: SimpleNamespace(music_bank_dir=bank_dir, music_bank_auto_populate=False))

    provider = LocalMusicBankProvider()
    result = provider.select_track({"canonical_topic": "x"}, {"title": "x", "hook": "x"}, tmp_path / "selected.wav", 1000)

    assert result["provider_metadata"]["track_id"].startswith("local-")

def test_resilient_music_provider_allows_mock_only_in_mock_mode(monkeypatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(use_mock_providers=True, resolved_minimax_music_api_key=None, strict_minimax_validation=False)
    monkeypatch.setattr("app.providers.music.get_settings", lambda: settings)

    provider = ResilientMusicProvider()
    result = provider.select_track({"canonical_topic": "polvos"}, {"title": "Polvos", "hook": "Polvos somem."}, tmp_path / "music.wav", 10_000)

    assert result["provider"] == "mock_music"
    assert result["provider_metadata"]["fallback_used"] is True

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

def test_pipelines_use_explicit_base_dependencies_and_asset_helpers() -> None:
    test_orchestrator = JobOrchestrator()

    assert not hasattr(test_orchestrator, "_build_fact_pack")
    assert not hasattr(test_orchestrator, "_split_subtitle_cue")
    assert not hasattr(test_orchestrator, "_publish_readiness_report")
    assert not hasattr(test_orchestrator, "_step_topic_plan")
    assert not hasattr(test_orchestrator, "_normalize_topic_plan_payload")
    assert not hasattr(test_orchestrator, "_run_retention_sweep")
    assert not hasattr(test_orchestrator, "_sync_native_scheduled_publications")
    assert test_orchestrator.topic_pipeline.step_topic_plan.__self__ is test_orchestrator.topic_pipeline
    assert test_orchestrator.topic_pipeline.normalize_topic_plan_payload.__self__ is test_orchestrator.topic_pipeline
    assert test_orchestrator.publication_ops.schedule_publication.__self__ is test_orchestrator.publication_ops
    assert test_orchestrator.publication_ops._run_retention_sweep.__self__ is test_orchestrator.publication_ops
    assert "__getattr__" not in test_orchestrator.asset_pipeline.__class__.__mro__[1].__dict__
    assert "_build_fact_pack" not in test_orchestrator.script_pipeline.__class__.__mro__[1].__dict__
    assert "_normalize_scene_semantics" not in test_orchestrator.scene_pipeline.__class__.__mro__[1].__dict__
    assert test_orchestrator.script_pipeline._build_fact_pack.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.script_pipeline._validate_or_repair_script.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.script_pipeline._persist_script_generation_debug.__self__ is test_orchestrator.script_pipeline
    assert test_orchestrator.script_pipeline.fact_pack_domain._build_fact_pack.__self__ is test_orchestrator.script_pipeline.fact_pack_domain
    assert test_orchestrator.script_pipeline.audit_domain._text_publish_audit.__self__ is test_orchestrator.script_pipeline.audit_domain
    assert test_orchestrator.script_pipeline.repair_domain._validate_or_repair_script.__self__ is test_orchestrator.script_pipeline.repair_domain
    assert test_orchestrator.scene_pipeline.normalize_scene_token_coverage.__self__ is test_orchestrator.scene_pipeline
    assert test_orchestrator.scene_pipeline.normalize_scene_semantics.__self__ is test_orchestrator.scene_pipeline
    assert test_orchestrator.scene_pipeline.fallback_query_variants.__self__ is test_orchestrator.scene_pipeline
    assert not hasattr(test_orchestrator.asset_pipeline, "_fit_tts_duration")
    assert test_orchestrator.asset_pipeline.tts.fit_tts_duration.__self__ is test_orchestrator.asset_pipeline.tts
    assert not hasattr(test_orchestrator.asset_pipeline, "_mix_background_music_with_repair")
    assert not hasattr(test_orchestrator.asset_pipeline, "_persist_background_music_debug")
    assert not hasattr(test_orchestrator.asset_pipeline, "_generate_sound_design_track")
    assert test_orchestrator.asset_pipeline.music.mix_background_music_with_repair.__self__ is test_orchestrator.asset_pipeline.music
    assert not hasattr(test_orchestrator.asset_pipeline, "_split_subtitle_cue")
    assert not hasattr(test_orchestrator.asset_pipeline, "_estimate_subtitle_timing_drift")
    assert test_orchestrator.asset_pipeline.subtitles.split_subtitle_cue.__self__ is test_orchestrator.asset_pipeline.subtitles
    assert test_orchestrator.asset_pipeline.subtitles.estimate_subtitle_timing_drift.__self__ is test_orchestrator.asset_pipeline.subtitles
    assert not hasattr(test_orchestrator.asset_pipeline, "_generate_primary_asset")
    assert not hasattr(test_orchestrator.asset_pipeline, "_normalize_asset_uri_extension")
    assert not hasattr(test_orchestrator.asset_pipeline, "_image_prompt_variants")
    assert test_orchestrator.asset_pipeline.image_assets.generate_primary_asset.__self__ is test_orchestrator.asset_pipeline.image_assets
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

def test_scene_timings_fall_back_to_token_boundaries() -> None:
    scenes = [
        {"scene_id": "scene-1", "token_start": 0, "token_end": 9},
        {"scene_id": "scene-2", "token_start": 10, "token_end": 19},
        {"scene_id": "scene-3", "token_start": 20, "token_end": 29},
    ]
    normalized = normalize_scene_timings(scenes, 30_000)
    assert [scene["actual_start_ms"] for scene in normalized] == [0, 10_000, 20_000]
    assert [scene["actual_end_ms"] for scene in normalized] == [10_000, 20_000, 30_000]

def test_scene_token_coverage_normalizes_numeric_scene_ids_to_strings() -> None:
    narration = "polvos tem tres coracoes e sangue azul no oceano profundo"
    scenes = [
        {"scene_id": 1, "order": 1, "narration_text": "polvos tem tres coracoes"},
        {"scene_id": 2, "order": 2, "narration_text": "e sangue azul no oceano profundo"},
    ]

    normalized = orchestrator.scene_pipeline.normalize_scene_token_coverage(scenes, narration)

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
    items = orchestrator.asset_pipeline.subtitles.split_subtitle_cue(cue, token_start=10, token_end=22)
    assert items[0]["start_ms"] == 1000
    assert items[-1]["end_ms"] == 5000
    assert items[0]["token_start"] == 10
    assert items[-1]["token_end"] == 22
    assert " ".join(item["text"] for item in items) == cue["text"]
    for item in items:
        assert len(word_tokens(item["text"])) <= SUBTITLE_MAX_WORDS
        assert len(wrap_caption(item["text"], max_chars=SUBTITLE_MAX_CHARS, max_lines=SUBTITLE_MAX_LINES).splitlines()) == 1

def test_topic_plan_normalization_fills_missing_required_fields() -> None:
    request = SimpleNamespace(seed_theme="buracos negros", requested_angle=None)
    plan = {
        "tema": "Buracos Negros",
        "gancho": "o limite que muda tudo",
        "titulos": ["Buracos negros: o limite que muda tudo"],
    }

    normalized = orchestrator.topic_pipeline.normalize_topic_plan_payload(plan, request)

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
    from app.providers.llm import MinimaxCreativeProvider

    provider = MinimaxCreativeProvider.__new__(MinimaxCreativeProvider)
    normalized = provider._normalize_scene_plan_payload({"scenes": [{"scene_id": "scene-1"}]})

    assert isinstance(normalized, list)
    assert normalized[0]["scene_id"] == "scene-1"

def test_scene_plan_normalization_accepts_nested_scene_list() -> None:
    from app.providers.llm import MinimaxCreativeProvider

    provider = MinimaxCreativeProvider.__new__(MinimaxCreativeProvider)
    normalized = provider._normalize_scene_plan_payload({"data": {"plan": [{"scene_id": "scene-1", "narration_text": "abc"}]}})

    assert isinstance(normalized, list)
    assert normalized[0]["scene_id"] == "scene-1"

def test_plan_scenes_prefers_json_array_completion_when_available(monkeypatch) -> None:
    from app.providers.llm import MinimaxCreativeProvider

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

def test_subtitle_split_enforces_word_limit_for_long_cues() -> None:
    cue = {
        "idx": 5,
        "start_ms": 1000,
        "end_ms": 5000,
        "text": "Mesmo assim, o buraco negro age como um corpo negro ideal, absorvendo toda a luz.",
    }

    items = orchestrator.asset_pipeline.subtitles.split_subtitle_cue(cue, token_start=0, token_end=14)

    assert len(items) > 1
    assert " ".join(item["text"] for item in items) == cue["text"]
    for item in items:
        assert len(word_tokens(item["text"])) <= SUBTITLE_MAX_WORDS
        assert len(wrap_caption(item["text"], max_chars=SUBTITLE_MAX_CHARS, max_lines=SUBTITLE_MAX_LINES).splitlines()) == 1

def test_subtitle_ass_render_disables_automatic_wrapping() -> None:
    ass = orchestrator.asset_pipeline.subtitles.render_ass(
        [{"idx": 1, "start_ms": 0, "end_ms": 1000, "text": "legenda curta em uma linha"}]
    )

    assert "WrapStyle: 2" in ass
    assert "\\N" not in ass

def test_subtitle_boundary_repair_moves_words_across_cues() -> None:
    items = [
        {"idx": 9, "start_ms": 20_000, "end_ms": 22_500, "text": "deixa oceanos profundos mais concreto para", "token_start": 40, "token_end": 45},
        {"idx": 10, "start_ms": 22_500, "end_ms": 25_000, "text": "quem assiste. Assim cada cena sustenta a", "token_start": 46, "token_end": 52},
        {"idx": 11, "start_ms": 25_000, "end_ms": 27_500, "text": "ideia sem inventar elemento aleatorio. Por", "token_start": 53, "token_end": 58},
        {"idx": 12, "start_ms": 27_500, "end_ms": 30_000, "text": "isso oceanos profundos deixa de ser so", "token_start": 59, "token_end": 65},
    ]

    repaired = orchestrator.asset_pipeline.subtitles.repair_subtitle_item_boundaries(items)

    assert "concreto para quem" in [item["text"] for item in repaired]
    assert "aleatorio. Por isso" in [item["text"] for item in repaired]
    assert SubtitleGate().validate(repaired, 1.0).passed
    for item in repaired:
        assert len(wrap_caption(item["text"], max_chars=SUBTITLE_MAX_CHARS, max_lines=SUBTITLE_MAX_LINES).splitlines()) == 1

def test_subtitle_boundary_repair_can_push_weak_ending_into_next_chunk() -> None:
    items = [
        {"idx": "4.1", "start_ms": 8_002, "end_ms": 10_139, "text": "Isso significa que ele passa por qualquer fresta, se contorcendo ao máximo para", "token_start": 24, "token_end": 36},
        {"idx": "4.2", "start_ms": 10_139, "end_ms": 12_276, "text": "caber.", "token_start": 37, "token_end": 37},
    ]

    repaired = orchestrator.asset_pipeline.subtitles.repair_subtitle_item_boundaries(items)

    assert repaired[-2]["text"] == "contorcendo ao máximo"
    assert repaired[-1]["text"] == "para caber."
    assert SubtitleGate().validate(repaired, 1.0).passed

def test_subtitle_boundary_repair_can_pull_words_from_next_chunk() -> None:
    items = [
        {"idx": "6.2", "start_ms": 19_000, "end_ms": 20_500, "text": "predadores e muda de cor em", "token_start": 60, "token_end": 65},
        {"idx": "6.3", "start_ms": 20_500, "end_ms": 22_142, "text": "segundos.", "token_start": 66, "token_end": 66},
    ]

    repaired = orchestrator.asset_pipeline.subtitles.repair_subtitle_item_boundaries(items)

    assert repaired[0]["text"] == "predadores e muda de cor"
    assert repaired[1]["text"] == "em segundos."
    assert SubtitleGate().validate(repaired, 1.0).passed

def test_subtitle_split_avoids_semantic_orphan_fragments_from_audit_job() -> None:
    cue = {
        "idx": 7,
        "start_ms": 18_000,
        "end_ms": 25_200,
        "text": "Dois corações empurram sangue para as brânquias. O outro manda oxigênio para o resto do corpo.",
    }

    items = orchestrator.asset_pipeline.subtitles.split_subtitle_cue(cue, token_start=0, token_end=15)
    texts = [item["text"] for item in items]

    assert "para as" not in texts
    assert "oxigênio para o" not in texts
    assert all(not text.endswith((" para o", " para as", " olhada, a", " para outro")) for text in texts)
    assert SubtitleGate().validate(items, 1.0).passed

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

    result = orchestrator.asset_pipeline.tts.fit_tts_duration(
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

    result = orchestrator.asset_pipeline.tts.fit_tts_duration(
        audio_path,
        srt_path,
        {"duration_ms": 27_000, "provider_metadata": {"mode": "edge"}},
    )
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))

    assert 35_500 <= result["duration_ms"] <= 36_500
    assert result["provider_metadata"]["duration_fit_applied"] is True
    assert 35_500 <= cues[-1]["end_ms"] <= 36_500

def test_scene_semantics_keeps_image_prompt_in_english() -> None:
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
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

def test_scene_semantics_uses_visual_contract_domain_for_non_science_prompt() -> None:
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "primary_subject": "diorama de cidade abandonada",
            "narration_text": "Isso parece uma cidade abandonada de verdade.",
            "visual_intent": "deceptive_establishing",
            "image_prompt": "vertical cinematic scientific image of an abandoned miniature street, scientific visualization, no readable text anywhere",
            "fallback_queries": ["diorama cidade"],
        },
        "diorama de cidade abandonada",
        visual_contract={"visual_domain": "miniature urban diorama / craft documentary realism"},
    )
    prompt = normalized["image_prompt"].lower()
    assert normalized["visual_domain"] == "miniature urban diorama / craft documentary realism"
    assert "miniature craft documentary realism" in prompt
    assert "scientific visualization" not in prompt
    assert "scientific image" not in prompt

def test_scene_semantics_rebuilds_generic_portuguese_prompt_from_narration() -> None:
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "order": 2,
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

def test_first_scene_prompt_adds_visual_hook_contract() -> None:
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "primary_subject": "Polvos",
            "narration_text": "Polvos possuem três corações e sangue azul.",
            "visual_intent": "subject_closeup",
            "image_prompt": "ilustracao vertical cinematografica de Polvos, mostrando subject closeup, sem texto",
            "fallback_queries": ["Polvos"],
        },
        "Polvos",
    )
    prompt = normalized["image_prompt"].lower()
    assert "first-frame hook for shorts" in prompt
    assert "under one second" in prompt
    assert "strong concrete contrast or visible consequence" in prompt
    assert "do not reveal later payoff" in prompt
    assert "three subtle hearts" in prompt
    assert "blue copper-rich blood vessels" in prompt
    assert "polvos" not in prompt
    assert "sem texto" not in prompt

def test_image_prompt_variants_do_not_copy_portuguese_narration() -> None:
    variants = orchestrator.asset_pipeline.image_assets.image_prompt_variants(
        {
            "scene_id": "scene-1",
            "order": 1,
            "retention_role": "visual_hook",
            "primary_subject": "Polvos",
            "topic_hint": "Polvos",
            "narration_text": "Polvos possuem três corações e sangue azul.",
            "visual_intent": "subject_closeup",
            "image_prompt": "vertical cinematic image of octopus anatomy, no readable text anywhere",
            "fallback_queries": ["Polvos"],
        }
    )

    assert variants
    for variant in variants:
        prompt = variant["image_prompt"].lower()
        assert "possuem" not in prompt
        assert "três" not in prompt
        assert "tres" not in prompt
        assert "polvos" not in prompt
        assert "first-frame hook" in prompt or "stop-the-scroll" in prompt or "three subtle hearts" in prompt

def test_scene_semantics_adds_caffeine_specific_visuals_and_blank_objects() -> None:
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
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
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
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
    normalized = orchestrator.scene_pipeline.normalize_scene_semantics(
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

def test_scene_pipeline_passes_visual_contract_to_planner(monkeypatch) -> None:
    from app.models import ScenePlan

    job_id = orchestrator.create_job(
        {
            "seed_theme": "maquetes urbanas",
            "niche_id": "curiosidades",
            "language": "pt-BR",
            "target_duration_sec": 35,
            "tone": "intrigante_direto",
            "cta_style": "none",
            "notes": "teste",
            "requested_angle": None,
        }
    )
    script_payload = _base_script(
        "Isso parece uma cidade abandonada de verdade. A rua pequena engana o olho. "
        "A poeira vira neblina. Na segunda olhada, a cidade inteira cabe numa mesa."
    )
    visual_contract = {
        "contract_name": "Contrato Visual do Roteiro",
        "hook_frame": {
            "recommended_visual_intent": "deceptive_establishing",
            "must_show": ["cidade", "rua"],
            "must_hide": ["mesa", "mão humana"],
            "negative_reads": ["entulho abstrato"],
        },
        "loop_policy": {"forbidden_early_reveal": ["cidade inteira cabe numa mesa"]},
        "payoff_frame": {"recommended_visual_intent": "loop_close_reframe"},
    }
    artifact_dir = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "script.json").write_text(json.dumps(script_payload), encoding="utf-8")
    (artifact_dir / "visual_contract.json").write_text(json.dumps(visual_contract), encoding="utf-8")

    with SessionLocal() as session:
        session.add(
            TopicPlan(
                topic_id=f"topic-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="topic",
                canonical_topic="maquetes urbanas",
                angle="ilusao de cidade real",
                hook_promise="uma maquete parece cidade real antes da escala aparecer",
                entities=["maquetes"],
                search_terms=["maquetes urbanas"],
                title_candidates=["Maquetes urbanas parecem cidades reais"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id=f"script-{job_id}",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="script",
                title=str(script_payload["title"]),
                hook=str(script_payload["hook"]),
                body_beats=script_payload["body_beats"],
                ending=str(script_payload["ending"]),
                cta=None,
                full_narration=str(script_payload["full_narration"]),
                estimated_duration_sec=float(script_payload["estimated_duration_sec"]),
                key_facts=script_payload["key_facts"],
                token_count=int(script_payload["token_count"]),
                language="pt-BR",
                qa_metrics=script_payload["qa_metrics"],
            )
        )
        session.commit()

    captured: dict[str, object] = {}
    scene_count = orchestrator.settings.scene_target_count

    def fake_plan_scenes(script: dict[str, object], target_scene_count: int) -> list[dict[str, object]]:
        captured["visual_contract"] = script.get("visual_contract")
        scenes: list[dict[str, object]] = []
        for index in range(target_scene_count):
            is_first = index == 0
            is_last = index == target_scene_count - 1
            scenes.append(
                {
                    "scene_id": f"scene-{index + 1}",
                    "order": index + 1,
                    "retention_role": "visual_hook" if is_first else "loop_close" if is_last else "escalation",
                    "visual_intent": "deceptive_establishing" if is_first else "loop_close_reframe" if is_last else "visual_evidence",
                    "narration_text": f"Trecho visual {index + 1} com detalhe concreto.",
                    "token_start": index,
                    "token_end": index,
                    "estimated_duration_sec": 5,
                    "primary_subject": "cidade abandonada com rua" if is_first else "cidade em miniatura revelada" if is_last else "detalhe urbano",
                    "image_prompt": (
                        "abandoned miniature city street with facades, no readable text anywhere"
                        if is_first
                        else "miniature city on a table payoff reveal, no readable text anywhere"
                        if is_last
                        else "urban miniature detail, no readable text anywhere"
                    ),
                    "fallback_queries": ["cidade em miniatura"],
                }
            )
        assert target_scene_count == scene_count
        return scenes

    monkeypatch.setattr(orchestrator.providers.creative, "plan_scenes", fake_plan_scenes)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        artifacts = orchestrator.scene_pipeline.step_scene_plan(session, job, 1)
        session.flush()
        scene_plan = session.scalar(select(ScenePlan).where(ScenePlan.job_id == job_id))

    assert captured["visual_contract"] == visual_contract
    assert "scene_plan.json" in artifacts
    assert scene_plan is not None

def test_asset_extension_is_normalized_to_actual_file_format(tmp_path: Path) -> None:
    wrong_path = tmp_path / "ai.png"
    from PIL import Image

    Image.new("RGB", (32, 48), "white").save(wrong_path, format="JPEG")
    asset = {"uri": wrong_path.resolve().as_uri(), "provider": "test", "prompt_snapshot": "prompt"}

    normalized = orchestrator.asset_pipeline.image_assets.normalize_asset_uri_extension(asset)

    normalized_path = Path(normalized["uri"].replace("file://", ""))
    assert normalized_path.suffix == ".jpg"
    assert normalized_path.exists()
    assert not wrong_path.exists()
    assert normalized["file_format"] == "jpeg"
    assert normalized["extension_normalized"] is True

def test_conservative_ai_disclosure_requires_toggle_for_any_synthetic_asset(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "conservative_synthetic_disclosure", True)
    monkeypatch.setattr(orchestrator.settings, "channel_ai_generated_content", False)
    report = orchestrator.monetization_pipeline.build_ai_disclosure_report(
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

def test_rights_registry_requires_evidence_for_confirmed_minimax_assets(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "ai_generated_commercial_rights_confirmed", False)
    monkeypatch.setattr(orchestrator.settings, "minimax_commercial_rights_confirmed", True)
    monkeypatch.setattr(orchestrator.settings, "minimax_rights_evidence_url", None)
    report = orchestrator.monetization_pipeline.build_rights_registry(
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
    report = orchestrator.monetization_pipeline.build_rights_registry(
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

def test_narration_publishability_blocks_technical_tts_outside_mock(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "use_mock_providers", False)

    blockers = orchestrator.monetization_pipeline.narration_publishability_blockers(
        SimpleNamespace(provider="edge_tts", provider_metadata={"fallback_used": True})
    )

    assert blockers == ["technical_tts_provider_not_publishable"]

def test_narration_publishability_allows_gemini_tts(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "use_mock_providers", False)

    blockers = orchestrator.monetization_pipeline.narration_publishability_blockers(
        SimpleNamespace(provider="gemini_tts", provider_metadata={"fallback_used": False})
    )

    assert blockers == []

def test_voice_direction_uses_script_hook_and_retention_artifact(tmp_path: Path) -> None:
    job_id = "voice-direction-job"
    artifact_dir = Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "script.json").write_text(
        json.dumps(
            {
                "retention_map": {
                    "visual_hook": "Segurar o primeiro segundo.",
                    "turn_or_payoff": "A virada aparece tarde.",
                    "loop_close": "O final muda o começo.",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script = SimpleNamespace(
        job_id=job_id,
        title="Polvos parecem alienigenas",
        hook="Cada braço parece pensar sozinho.",
        body_beats=["O braço reage antes da cabeça."],
        ending="O começo muda quando o braço decide sozinho.",
        estimated_duration_sec=40,
        qa_metrics={},
    )
    topic_plan = SimpleNamespace(canonical_topic="polvos", angle="neurobiologia", hook_promise="o braço decide antes")

    direction = orchestrator.asset_pipeline._build_voice_direction(
        script,
        topic_plan,
        orchestrator.asset_pipeline._read_job_json(job_id, "script.json"),
    )

    assert direction["hook"] == "Cada braço parece pensar sozinho."
    assert direction["canonical_topic"] == "polvos"
    assert direction["retention_map"]["visual_hook"] == "Segurar o primeiro segundo."
    assert direction["retention_map"]["loop_close"] == "O final muda o começo."

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

    normalized = orchestrator.scene_pipeline.normalize_scene_token_coverage(
        scenes,
        "Polvos parecem alienigenas e mudam de cor para fugir de predadores",
    )

    assert normalized[0]["order"] == 1
    assert normalized[0]["token_start"] == 0
    assert normalized[0]["token_end"] < normalized[1]["token_start"]
    assert normalized[1]["token_end"] == len(word_tokens("Polvos parecem alienigenas e mudam de cor para fugir de predadores")) - 1
    assert normalized[0]["narration_text"].startswith("polvos parecem alienigenas")

def test_scene_retention_annotation_marks_first_scene_as_visual_hook() -> None:
    scenes = [
        {"scene_id": "scene-1", "order": 1, "narration_text": "Abertura forte"},
        {"scene_id": "scene-2", "order": 2, "narration_text": "Fechamento"},
    ]

    annotated = orchestrator.scene_pipeline.annotate_scene_retention_roles(
        scenes,
        {
            "hook": "A primeira imagem precisa segurar o swipe.",
            "visual_opening": {"first_frame_goal": "mostrar contraste imediato"},
        },
    )

    assert annotated[0]["retention_role"] == "visual_hook"
    assert annotated[0]["hook_text"] == "A primeira imagem precisa segurar o swipe."
    assert annotated[0]["visual_opening"]["first_frame_goal"] == "mostrar contraste imediato"
    assert annotated[-1]["retention_role"] == "loop_close"

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
            orchestrator.asset_pipeline.step_background_music(session, job, 1)
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

    monkeypatch.setattr("app.providers.image.httpx.post", fake_post)
    monkeypatch.setattr("app.providers.music.httpx.get", fake_get)
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

    monkeypatch.setattr("app.providers.music.httpx.post", lambda *args, **kwargs: FakeResponse())

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
