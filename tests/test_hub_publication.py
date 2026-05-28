from tests.e2e_support import *  # noqa: F403


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
    assert captured["job_origin"] == "manual_title"
    assert captured["creation_via"] == "hub"
    assert "input_mode=title" in str(captured["notes"])
    assert "copywriting viral" in str(captured["notes"])
    assert "SEO otimizado" in str(captured["notes"])
    assert "retencao e viralizacao" in str(captured["notes"])
    assert "Use curiosidade forte e payoff claro." in str(captured["notes"])

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
    assert run_result["result_schedule_id"] == f"{job_id}-schedule"
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

def test_hub_prompt_panel_saves_and_resets_safe_template(monkeypatch, tmp_path: Path) -> None:
    prompt_path = tmp_path / "hub_settings.json"
    monkeypatch.setattr(main_module, "_hub_settings_path", lambda: prompt_path)
    monkeypatch.setattr(main_module, "_default_seed_theme", lambda: "abelhas")
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert "Configurações do hub" in page.text
    assert "Banco de roteiros" in page.text
    assert 'data-open-ready-script-bank' in page.text
    assert "/automation/ready-scripts/import" in page.text
    assert 'data-open-operational-settings' in page.text

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

def test_default_viral_prompt_avoids_generic_changes_how_you_see_formula() -> None:
    prompt = main_module.DEFAULT_VIRAL_PROMPT_TEMPLATE

    assert "isso muda como você enxerga X" not in prompt
    assert "consequencia visual especifica" in prompt
    assert "virada verificavel" in prompt

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
    assert 'name="target_duration_sec" type="number" min="35" max="55" value="50"' in page.text

    response = client.post("/jobs", data={"seed_theme": "", "input_mode": "theme"}, follow_redirects=False)
    assert response.status_code == 303
    assert captured["seed_theme"] == "Por que flamingos estão em alta?"
    assert captured["requested_angle"] == "Transformar tendência real em curiosidade verificável."
    assert captured["job_origin"] == "automatic_topic"
    assert captured["creation_via"] == "hub"
    assert "trend_research=real_source" in str(captured["notes"])
    assert captured["niche_id"] == "curiosidades"
    assert captured["target_duration_sec"] == 50

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

def test_hub_filters_jobs_by_origin_and_shows_portuguese_labels() -> None:
    client = TestClient(app)
    with SessionLocal() as session:
        _create_basic_job(session, job_id="origin-filter-bank-job", status="monetization_review", seed_theme="Origem filtro banco")
        _create_basic_job(session, job_id="origin-filter-auto-job", status="monetization_review", seed_theme="Origem filtro automatico")
        session.flush()
        bank_job = session.get(Job, "origin-filter-bank-job")
        auto_job = session.get(Job, "origin-filter-auto-job")
        assert bank_job is not None
        assert auto_job is not None
        bank_job.job_origin = "ready_script_bank"
        bank_job.creation_via = "daily_cycle"
        auto_job.job_origin = "automatic_topic"
        auto_job.creation_via = "daily_cycle"
        session.commit()

    response = client.get("/?origin=ready_script_bank")

    assert response.status_code == 200
    assert "Origem filtro banco" in response.text
    assert "Origem filtro automatico" not in response.text
    assert "Origem: Banco" in response.text
    assert "Via: Ciclo diário" in response.text
    assert "Banco de Roteiros Prontos" in response.text


def test_create_job_persists_origin_and_audit_artifact() -> None:
    job_id = orchestrator.create_job(
        {
            "seed_theme": "Origem persistida por API",
            "target_duration_sec": 35,
            "job_origin": "manual_title",
            "creation_via": "api",
        }
    )

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.job_origin == "manual_title"
        assert job.creation_via == "api"
        assert (job.artifact_index or {}).get("job_origin") == "job_origin.json"

    artifact = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "job_origin.json").read_text(encoding="utf-8"))
    assert artifact["job_origin_label"] == "Título manual"
    assert artifact["creation_via_label"] == "API"
    assert artifact["inferred"] is False

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

def test_due_tiktok_publication_uses_real_publisher_and_persists_processing(monkeypatch, tmp_path) -> None:
    job_id = "due-tiktok-publish"
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"fake mp4")
    monkeypatch.setattr(orchestrator.settings, "tiktok_auto_publish_enabled", True)
    monkeypatch.setattr(orchestrator.settings, "tiktok_access_token", "token")
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_publish_package",
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

    claimed = orchestrator.publication_ops._claim_due_tiktok_publication()
    assert claimed == "due-tiktok-publication-row"
    orchestrator.publication_ops._publish_tiktok_channel_publication(claimed)

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
    publish_result = json.loads((orchestrator.storage.job_dir(job_id) / "publish_result.json").read_text())
    assert publish_result["status"] == "published"
    assert publish_result["published_at"] is not None
    assert publish_result["youtube_url"] == "https://youtube.com/shorts/abc123"
    assert publish_result["publication_schedule"]["status"] == "published"
    assert publish_result["publication_schedule"]["youtube_url"] == "https://youtube.com/shorts/abc123"
    assert publish_result["youtube"]["url"] == "https://youtube.com/shorts/abc123"

def test_manual_publish_rejects_already_published_job_until_reopened() -> None:
    client = TestClient(app)
    job_id = "already-published-job"
    topic_request_id = "already-published-job-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="already-published",
                status="published",
                niche_id="curiosidades",
                language="pt-BR",
                target_duration_sec=45,
                topic_request_id=topic_request_id,
                review_state="published",
                artifact_index={},
            )
        )
        session.add(
            TopicRequest(
                topic_request_id=topic_request_id,
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="already-published-request",
                niche_id="curiosidades",
                seed_theme="Flamingos",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="already-published-row",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="already-published-row",
                scheduled_for_utc=utcnow(),
                timezone="UTC",
                youtube_visibility="public",
                status="published",
                youtube_video_id="yt-published",
                youtube_url="https://youtube.com/watch?v=yt-published",
                published_at=utcnow(),
            )
        )
        session.commit()

    response = client.post(
        f"/jobs/{job_id}/publish",
        data={"youtube_url": "https://youtube.com/shorts/new"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "publish_error=job+must+be+approved_for_publish+before+publishing" in response.headers["location"]
    with SessionLocal() as session:
        schedule = session.query(PublicationSchedule).filter_by(job_id=job_id).one()
        job = session.get(Job, job_id)

    assert job and job.status == "published"
    assert schedule.youtube_url == "https://youtube.com/watch?v=yt-published"

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

    cleaned = orchestrator.publication_ops._run_retention_sweep()

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
    cleaned = orchestrator.publication_ops._run_retention_sweep()

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
    monkeypatch.setattr(orchestrator.publication_ops, "_ensure_youtube_api_ready", lambda: None)
    monkeypatch.setattr(
        orchestrator.monetization_pipeline,
        "build_publish_package",
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
    monkeypatch.setattr(orchestrator.publication_ops, "_ensure_youtube_api_ready", lambda: None)

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
    monkeypatch.setattr(orchestrator.publication_ops, "_ensure_youtube_api_ready", lambda: None)
    monkeypatch.setattr(
        orchestrator.publication_ops,
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

    assert orchestrator.publication_ops._claim_due_publication_schedule() is None

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

    synced = orchestrator.publication_ops._sync_native_scheduled_publications()

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
        "https://www.googleapis.com/auth/youtube.readonly",
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

def test_publication_dashboard_fragment_focuses_on_growth_analytics() -> None:
    client = TestClient(app)
    scheduled_job_id = "publication-dashboard-scheduled"
    published_job_id = "publication-dashboard-published"
    with SessionLocal() as session:
        _create_basic_job(
            session,
            job_id=scheduled_job_id,
            status="approved_for_publish",
            seed_theme="Morcegos",
        )
        session.add(
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
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="publication-dashboard-scheduled-row",
                job_id=scheduled_job_id,
                schema_version="1.0.0",
                content_hash="dashboard-scheduled-row",
                scheduled_for_utc=datetime(2099, 1, 2, 17, 0, tzinfo=UTC),
                timezone="America/Sao_Paulo",
                youtube_visibility="private",
                status="scheduled",
            )
        )
        _create_basic_job(
            session,
            job_id=published_job_id,
            status="published",
            seed_theme="Polvos",
        )
        session.add_all(
            [
                Script(
                    script_id="publication-dashboard-published-script",
                    job_id=published_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-published-script",
                    title="Polvos prendem atenção até o fim",
                    hook="O polvo muda de estratégia em segundos.",
                    body_beats=["A virada visual segura replay."],
                    ending="Esse padrão merece novas variações.",
                    cta=None,
                    full_narration="O polvo muda de estratégia em segundos. A virada visual segura replay. Esse padrão merece novas variações.",
                    estimated_duration_sec=35,
                    key_facts=[],
                    token_count=18,
                    language="pt-BR",
                    qa_metrics={},
                    prompt_version="test",
                ),
                PublicationSchedule(
                    schedule_id="publication-dashboard-published-row",
                    job_id=published_job_id,
                    schema_version="1.0.0",
                    content_hash="dashboard-published-row",
                    scheduled_for_utc=datetime(2099, 5, 20, 14, 0, tzinfo=UTC),
                    timezone="America/Sao_Paulo",
                    youtube_visibility="public",
                    status="published",
                    published_at=datetime(2099, 5, 20, 14, 15, tzinfo=UTC),
                    youtube_video_id="yt-growth-published",
                    youtube_url="https://www.youtube.com/watch?v=yt-growth-published",
                ),
            ]
        )
        session.commit()

    response = client.get("/publication-hub/fragment")

    assert response.status_code == 200
    assert "Centro de Crescimento do Canal" in response.text
    assert "Linhas editoriais por retenção" in response.text
    assert "Base de análise" in response.text
    assert "Polvos prendem atenção até o fim" in response.text
    assert "Sincronizar Analytics" in response.text
    assert "Morcegos enxergam com o som" not in response.text
    assert "Ciclo diário" not in response.text
    assert "Estado da integração" not in response.text
    assert "TikTok" not in response.text
    assert "Aprovados sem agenda" not in response.text
    assert "Agenda ativa" not in response.text
    assert "Para agendar" not in response.text
    assert "/automation/ready-scripts/import" not in response.text

def test_home_growth_menu_links_to_separate_growth_center() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/publication-hub"' in response.text
    assert 'id="publication-hub" class="publication-shell"' not in response.text
    assert "Fila de vídeos" in response.text
    assert "Linhas editoriais por retenção" not in response.text

    growth_response = client.get("/publication-hub")
    assert growth_response.status_code == 200
    assert 'id="publication-hub" class="publication-shell"' in growth_response.text
    assert "Centro de Crescimento do Canal" in growth_response.text
    assert "Linhas editoriais por retenção" in growth_response.text

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
        brief = orchestrator.topic_pipeline.channel_learning_brief(session, "curiosidades")

    assert metric.retention_percent == 82.0
    assert job and job.artifact_index["performance_metrics"] == "performance_metrics.json"
    assert job.quality_summary["performance"]["retention_percent"] == 82.0
    report = json.loads((Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "performance_metrics.json").read_text(encoding="utf-8"))
    assert report["latest"]["retention_percent"] == 82.0
    assert brief["sample_count"] >= 1
    assert brief["strong_patterns"]
    assert (Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "performance_metrics.json").exists()

def test_sync_youtube_analytics_snapshot_persists_snapshot_and_updates_growth_center(monkeypatch) -> None:
    job_id = "analytics-snapshot-job"
    with SessionLocal() as session:
        _create_basic_job(session, job_id=job_id, status="published", seed_theme="Polvos que resolvem labirintos")
        session.add(
            TopicPlan(
                topic_id="analytics-snapshot-topic",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="analytics-snapshot-topic",
                canonical_topic="polvos",
                angle="biologia curiosa",
                hook_promise="o polvo aprende rápido demais",
                entities=["polvos"],
                search_terms=["polvos"],
                title_candidates=["O polvo aprende rápido demais"],
                quality_metrics={},
            )
        )
        session.add(
            Script(
                script_id="analytics-snapshot-script",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="analytics-snapshot-script",
                title="O polvo aprende rápido demais",
                hook="O polvo aprende rápido demais.",
                body_beats=["Ele testa uma saída.", "Depois muda de estratégia.", "E guarda a pista."],
                ending="Esse é o tipo de loop que prende replay.",
                cta=None,
                full_narration="O polvo aprende rápido demais. Ele testa uma saída. Depois muda de estratégia.",
                estimated_duration_sec=35,
                key_facts=[],
                token_count=20,
                language="pt-BR",
                qa_metrics={},
                prompt_version="test",
            )
        )
        session.add(
            PublicationSchedule(
                schedule_id="analytics-snapshot-schedule",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="analytics-snapshot-schedule",
                scheduled_for_utc=datetime(2026, 5, 20, 14, 0, tzinfo=UTC),
                timezone="America/Sao_Paulo",
                youtube_visibility="public",
                status="published",
                published_at=datetime(2026, 5, 20, 14, 0, tzinfo=UTC),
                youtube_video_id="yt-analytics-123",
                youtube_url="https://www.youtube.com/watch?v=yt-analytics-123",
            )
        )
        session.commit()

    monkeypatch.setattr(
        orchestrator.youtube,
        "fetch_video_analytics_snapshot",
        lambda *, video_id, start_date, end_date: {
            "video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "summary_metrics": {
                "views": 240,
                "averageViewPercentage": 86.5,
                "averageViewDuration": 31.2,
                "likes": 18,
                "comments": 2,
                "shares": 7,
                "subscribersGained": 3,
            },
            "daily_rows": [{"day": end_date.isoformat(), "views": 240, "averageViewPercentage": 86.5}],
            "raw_response": {"summary": {}, "daily": {}},
            "fetched_at": datetime(2026, 5, 27, 12, 0, tzinfo=UTC).isoformat(),
        },
    )
    client = TestClient(app)

    response = client.post(f"/jobs/{job_id}/youtube-analytics/sync", data={"days": "28", "return_to": "/publication-hub"}, follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as session:
        snapshot = session.query(YouTubeAnalyticsSnapshot).filter_by(job_id=job_id).one()
        metric = session.query(PerformanceMetric).filter_by(job_id=job_id, source="youtube_analytics_api").one()
        job = session.get(Job, job_id)

    assert snapshot.summary_metrics["averageViewPercentage"] == 86.5
    assert metric.retention_percent == 86.5
    assert job and job.quality_summary["youtube_analytics"]["summary_metrics"]["views"] == 240
    assert (Path(os.environ["YTS_DATA_DIR"]) / "artifacts" / job_id / "youtube_analytics_snapshot.json").exists()
    dashboard = client.get("/publication-hub")
    assert "Linhas editoriais por retenção" in dashboard.text
    assert "86.5%" in dashboard.text

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

def test_publish_hashtags_use_entities_not_weak_words() -> None:
    topic_plan = SimpleNamespace(
        canonical_topic="Por que flamingos ficam cor-de-rosa",
        angle="A cor vem da cadeia alimentar invisível",
    )
    script = SimpleNamespace(
        title="A comida que pinta flamingos de rosa",
        key_facts=["Flamingos recebem pigmentos pela alimentação."],
    )

    tags = orchestrator.monetization_pipeline.build_publish_hashtags(topic_plan, script)

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

    tags = orchestrator.monetization_pipeline.build_publish_hashtags(topic_plan, script)

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

    tags = orchestrator.monetization_pipeline.build_publish_hashtags(topic_plan, script)

    assert "#danakil" in tags
    assert "#etiopia" in tags
    assert "#geografia" in tags
    assert "#terra" not in tags
    assert "#lugar" not in tags
