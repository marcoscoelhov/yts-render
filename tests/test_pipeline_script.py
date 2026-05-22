from tests.e2e_support import *  # noqa: F403


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
    pipeline = orchestrator.script_pipeline
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

    monkeypatch.setattr(pipeline.fact_pack_domain, "_scientific_article_fact_pack", fake_article_pack)
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
        orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 45, "none")
    except RecoverableStepError as exc:
        assert "placeholder_source_language" in str(exc)
    else:
        raise AssertionError("expected simple mode to block critical script warning")

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
    readiness = orchestrator.monetization_pipeline.publish_readiness_report(
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
    readiness = orchestrator.monetization_pipeline.publish_readiness_report(
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
    readiness = orchestrator.monetization_pipeline.publish_readiness_report(
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

    processed, metrics = orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 35, "none")

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
    monkeypatch.setattr(orchestrator.script_pipeline.repair_domain, "_postprocess_script_for_quality", fake_postprocess)
    monkeypatch.setattr(orchestrator.script_pipeline.repair_domain, "_fact_pack_consistency_reasons", lambda *_args, **_kwargs: [])

    processed, metrics = orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 45, "none")

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
    monkeypatch.setattr(orchestrator.script_pipeline.repair_domain, "_fact_pack_consistency_reasons", lambda *_args, **_kwargs: [])
    script = _base_script("Cafe muda seu estado de alerta. Na segunda olhada, a primeira frase vira pista.")
    plan_dict = {
        "canonical_topic": "cafe",
        "fact_pack": {
            "status": "verified",
            "facts": [{"fact_id": "F1", "claim": "Cafeina interage com receptores de adenosina."}],
        },
    }

    try:
        orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 35, "none")
    except RecoverableStepError as exc:
        assert "factual_claim_trace_missing" in str(exc)
    else:
        raise AssertionError("expected RecoverableStepError")

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
        repaired, metrics = orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 35, "none")
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

    repaired, metrics = orchestrator.script_pipeline._validate_or_repair_script(script, plan_dict, 35, "none")

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

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(script, {"canonical_topic": "polvo", "fact_pack": fact_pack}, [])

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

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(script, {"canonical_topic": "ilusão de ótica", "fact_pack": {"status": "limited", "facts": []}}, [])

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

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(
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

    repaired, metrics = orchestrator.script_pipeline._validate_or_repair_script(
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

def test_script_postprocess_removes_structured_body_beat_leak() -> None:
    script = _base_script("{'segment': 'visual_hook', 'narration': 'Texto vazado.'}")
    script["hook"] = "Este flamingo é rosa."
    script["body_beats"] = [
        {"segment": "visual_hook", "narration": "Mas ele nasceu branco."},
        {"segment": "payoff", "narration": "A cor vem dos carotenoides."},
    ]
    script["ending"] = "A dieta vira cor."

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(script, {"fact_pack": {"status": "limited", "facts": []}}, [])

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

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(script, {"fact_pack": {"status": "limited", "facts": []}}, [])
    combined = " ".join([processed["title"], processed["hook"], processed["ending"], processed["full_narration"]])

    assert "Flamengos" not in combined
    assert "roses" not in combined
    assert "deartemia" not in combined
    assert "supplementação" not in combined
    assert "alimentação" in processed["title"]

def test_weak_fact_query_rejects_generic_food_cause_terms() -> None:
    assert orchestrator.script_pipeline._is_weak_fact_query("causa comida")

def test_fact_query_concepts_include_octopus_camouflage_terms() -> None:
    concepts = orchestrator.script_pipeline._fact_query_concepts("camuflagem dos polvos usando cromatóforos")

    assert "chromatophores" in concepts
    assert "iridophores" in concepts

def test_fact_query_concepts_do_not_treat_coracoes_as_color() -> None:
    concepts = orchestrator.script_pipeline._fact_query_concepts("polvo com tres corações e sangue azul")

    assert "plumage pigmentation" not in concepts
    assert "carotenoid pigmentation" not in concepts

def test_fact_query_concepts_include_flamingo_pigment_terms() -> None:
    concepts = orchestrator.script_pipeline._fact_query_concepts("flamingos ficam rosas")

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

    monkeypatch.setattr(script_fact_pack_module.httpx, "Client", FakeClient)

    pack = orchestrator.script_pipeline._scientific_article_fact_pack("flamingo carotenoid")

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

    monkeypatch.setattr(script_fact_pack_module.httpx, "Client", FakeClient)

    pack = orchestrator.script_pipeline._scientific_article_fact_pack("formigas")

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

    reasons = orchestrator.script_pipeline._fact_pack_consistency_reasons(script, fact_pack)

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

    assert orchestrator.script_pipeline._fact_pack_consistency_reasons(script, fact_pack) == []

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

    assert orchestrator.script_pipeline._fact_pack_consistency_reasons(script, fact_pack) == []

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

    reasons = orchestrator.script_pipeline._fact_pack_consistency_reasons(script, fact_pack)

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

    reasons = orchestrator.script_pipeline._fact_pack_consistency_reasons(script, fact_pack)

    assert "invented_claim_trace_fact_ids" in reasons

def test_fact_pack_consistency_rejects_source_ids_when_fact_pack_limited() -> None:
    script = _base_script("Flamingos ficam rosas por pigmentos na alimentação.")
    script["source_fact_ids"] = ["fact_1"]

    reasons = orchestrator.script_pipeline._fact_pack_consistency_reasons(script, {"status": "limited", "facts": []})

    assert "invented_source_fact_ids" in reasons

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
            orchestrator.script_pipeline.step_script(session, job, 1)
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

def test_fact_pack_query_generation_extracts_entity_and_concepts() -> None:
    request = SimpleNamespace(seed_theme="Por que flamingos ficam cor-de-rosa?")
    topic_plan = SimpleNamespace(
        canonical_topic="Por que flamingos ficam cor-de-rosa",
        angle="A cor vem de pigmentos na alimentação",
        title_candidates=["A comida que pinta flamingos"],
    )

    queries = orchestrator.script_pipeline._fact_pack_queries(request, topic_plan)
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

    ordered = sorted(queries, key=orchestrator.script_pipeline._fact_query_priority)

    assert ordered[0] == "polvo corações pigmentos"
    assert ordered.index("polvos") > ordered.index("polvo corações pigmentos")

def test_fact_query_priority_keeps_exact_pisa_entity_before_derived_terms() -> None:
    queries = ["torre pisa solo", "torre pisa inclinação", "torre pisa"]

    ordered = sorted(queries, key=orchestrator.script_pipeline._fact_query_priority)

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
    for query in orchestrator.script_pipeline._fact_pack_queries(request, topic_plan):
        normalized = " ".join(str(query or "").split())
        if normalized and normalized.lower() not in seen and not orchestrator.script_pipeline._is_weak_fact_query(normalized):
            queries.append(normalized)
            seen.add(normalized.lower())

    top_queries = sorted(queries, key=orchestrator.script_pipeline._fact_query_priority)[:8]

    assert "name diet" not in top_queries
    assert "text diet" not in top_queries
    assert any("flamingo" in query.lower() for query in top_queries)

def test_fact_query_removes_generic_viral_opening() -> None:
    cleaned = orchestrator.script_pipeline._clean_fact_query("Você sabia? O cérebro humano tem um poder insano")

    assert "sabia" not in cleaned.lower()
    assert orchestrator.script_pipeline._extract_fact_entity(cleaned) == "cérebro humano"

def test_weak_fact_query_filters_generic_single_word_angle() -> None:
    assert orchestrator.script_pipeline._is_weak_fact_query("auto") is True
    assert orchestrator.script_pipeline._is_weak_fact_query("descubra dieta diet") is True
    assert orchestrator.script_pipeline._is_weak_fact_query("1325") is True
    assert orchestrator.script_pipeline._is_weak_fact_query("duas cidades") is True
    assert orchestrator.script_pipeline._is_weak_fact_query("polvos") is False

def test_script_postprocess_splits_long_sentences_before_gate() -> None:
    script = _base_script(
        "O espaço invisível parece distante mas atravessa sua vida todos os dias quando a luz viaja por regiões que ninguém consegue tocar diretamente. "
        "Esse efeito muda como você entende o céu."
    )

    processed = orchestrator.script_pipeline._postprocess_script_for_quality(script, {"canonical_topic": "espaço"}, [])
    result = ScriptQualityGate().validate(processed, target_duration_sec=35)

    assert result.metrics["max_words_single_sentence"] <= 20

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

    readiness = orchestrator.monetization_pipeline.publish_readiness_report(
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
