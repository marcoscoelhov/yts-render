from tests.e2e_support import *  # noqa: F403


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
    monkeypatch.setattr("app.providers.llm.get_settings", lambda: settings)

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

    monkeypatch.setattr("app.providers.llm.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.llm.OpenAI", FakeOpenAI)

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
        "app.providers.llm.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_timeout_sec=120,
        ),
    )
    monkeypatch.setattr("app.providers.llm.OpenAI", FakeOpenAI)

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
        "app.providers.llm.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_timeout_sec=120,
        ),
    )
    monkeypatch.setattr("app.providers.llm.OpenAI", FakeOpenAI)

    provider = OpenAICreativeProvider()
    result = provider.plan_topic("Por que os flamingos ficam rosa?", 1, [], None)

    assert captured["client_kwargs"]["api_key"] == "openai-key"
    assert captured["text"] == {"format": {"type": "json_object"}}
    assert "Crie pautas de curiosidades globais para YouTube Shorts em pt-BR." in str(captured["input"])
    assert "Loop: pergunta mental de tensão que só fecha no payoff" in str(captured["input"])
    assert "exceto search_terms quando pesquisa factual em ingles ajudar" in str(captured["input"])
    assert "search_terms em ingles para pesquisa factual" in str(captured["input"])
    assert result["quality_metrics"]["source_provider"] == "openai"

def test_llm_registry_supports_openai_primary_provider(monkeypatch) -> None:
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=lambda **_kwargs: None)

    monkeypatch.setattr(
        "app.providers.llm.get_settings",
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
    monkeypatch.setattr("app.providers.llm.OpenAI", FakeOpenAI)

    registry = LLMProviderRegistry()

    assert registry.primary_provider().provider_name == "openai"

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

def test_resilient_creative_provider_disables_repair_fallback_in_strict_minimax_mode() -> None:
    provider = object.__new__(ResilientCreativeProvider)
    provider.settings = SimpleNamespace(minimax_script_timeout_sec=0.01, llm_enable_fallback=True, strict_minimax_validation=True)
    provider.strict_minimax_validation = True
    provider.primary = None
    provider.fallback = MockCreativeProvider()

    assert provider.repair_script_with_fallback({"title": "x"}, ["fact_pack_source_ids_missing"], {"canonical_topic": "polvos"}) is None

def test_job_lease_delta_has_floor_for_real_provider_steps(monkeypatch) -> None:
    test_orchestrator = JobOrchestrator()
    monkeypatch.setattr(test_orchestrator.settings, "job_lease_seconds", 60)

    assert test_orchestrator._lease_delta().total_seconds() == 300
