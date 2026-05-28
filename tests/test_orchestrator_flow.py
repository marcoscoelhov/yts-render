from tests.e2e_support import *  # noqa: F403


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
    assert 'role="progressbar"' in detail.text
    assert 'aria-valuenow="' in detail.text
    assert 'aria-live="polite"' in detail.text
    assert "Roteiro" in detail.text
    assert "script.started" in detail.text

    hub = client.get("/")
    assert hub.status_code == 200
    assert "Progresso do job" in hub.text
    assert 'role="progressbar"' in hub.text
    assert "Fila atualizada." in hub.text

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

def test_sqlite_engine_uses_busy_timeout_and_wal_pragmas() -> None:
    with engine.connect() as connection:
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert busy_timeout >= 30_000
    assert str(journal_mode).lower() == "wal"

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

def test_operational_settings_rejects_secret_fields() -> None:
    from app.operational_settings import validate_operational_update

    with pytest.raises(ValueError, match="desconhecida"):
        validate_operational_update(main_module.settings, {"openai_api_key": "secret"})

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

def test_autoapproval_score_accepts_high_repetition_with_manual_originality_confirmation() -> None:
    service = AutomationService(orchestrator)
    job_id = "auto-score-manual-originality"
    topic_request_id = f"{job_id}-request"
    with SessionLocal() as session:
        session.add(
            Job(
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="auto-score-manual-originality",
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
                content_hash="auto-score-manual-originality-request",
                niche_id="curiosidades",
                seed_theme="Score alto com repeticao validada",
                language="pt-BR",
                target_duration_sec=45,
            )
        )
        session.add(
            Script(
                script_id=f"{job_id}-script",
                job_id=job_id,
                schema_version="1.0.0",
                content_hash="auto-score-manual-originality-script",
                title="Score alto com repeticao validada",
                hook="Um roteiro manual pode repetir tema sem reprovar automaticamente.",
                body_beats=["A confirmacao humana decide a originalidade editorial."],
                ending="A fila automatica respeita a validacao manual.",
                cta=None,
                full_narration="Um roteiro manual pode repetir tema sem reprovar automaticamente. A confirmacao humana decide a originalidade editorial. A fila automatica respeita a validacao manual.",
                estimated_duration_sec=40,
                key_facts=[],
                token_count=23,
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
            "manual_confirmations": ["originality_confirmed"],
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

    assert report["eligible"] is True
    assert "high_narrative_similarity" not in report["reasons"]
    assert report["components"]["originality_confirmed"] is True
    assert report["score"] >= 0.82

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
            topic_summary="Cristal heliotropo zafiro | fenômeno inventado de teste",
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
            topic_summary="Cristal heliotropo zafiro | fenômeno inventado de teste",
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
        report = orchestrator.monetization_pipeline.build_channel_repetition_report(session, current, topic_plan, script)

    assert report["repetition_risk"] in {"medium", "high"}
    assert report["matches"]
    assert report["signals"]["exact_hook_opening_matches"] >= 1
    assert report["signals"]["exact_title_opening_matches"] >= 1
    assert report["signals"]["exact_duration_bucket_matches"] >= 1
    assert report["signals"]["exact_beat_count_matches"] >= 1
    assert any("same_hook_opening" in match["signals"] for match in report["matches"])
    assert any("same_duration_bucket" in match["signals"] for match in report["matches"])

def test_channel_repetition_report_ignores_failed_regeneration_source() -> None:
    with SessionLocal() as session:
        failed = Job(
            job_id="job-repetition-failed-source",
            schema_version="1.0.0",
            content_hash="failed-source",
            status="failed",
            niche_id="curiosidades",
            language="pt-BR",
            target_duration_sec=35,
            topic_request_id="topic-request-failed-source",
            topic_summary="Polvos e inteligência distribuída | surpresa científica",
        )
        current = Job(
            job_id="job-repetition-regenerated-current",
            schema_version="1.0.0",
            content_hash="regenerated-current",
            status="ready_for_upload",
            niche_id="curiosidades",
            language="pt-BR",
            target_duration_sec=35,
            topic_request_id="topic-request-regenerated-current",
            topic_summary="Polvos e inteligência distribuída | surpresa científica",
        )
        session.add_all([failed, current])
        session.add(
            Script(
                script_id="script-repetition-failed-source",
                job_id=failed.job_id,
                schema_version="1.0.0",
                content_hash="script-failed-source",
                title="Cristal zafiro vibra sem tocar na mesa",
                hook="O cristal zafiro parece parado, mas vibra sozinho.",
                body_beats=["A borda muda de brilho.", "A mesa não encosta no centro.", "O reflexo entrega o truque."],
                ending="O objeto parecia imóvel, mas já estava reagindo.",
                cta=None,
                full_narration="O cristal zafiro parece parado, mas vibra sozinho. A borda muda de brilho.",
                estimated_duration_sec=35,
                key_facts=[],
                token_count=30,
                language="pt-BR",
                qa_metrics={},
            )
        )
        topic_plan = TopicPlan(
            topic_id="topic-repetition-regenerated-current",
            job_id=current.job_id,
            schema_version="1.0.0",
            content_hash="topic-regenerated-current",
            canonical_topic="Cristal heliotropo zafiro",
            angle="fenômeno inventado de teste",
            hook_promise="o cristal parece parado, mas vibra sozinho",
            entities=["cristal zafiro"],
            search_terms=["cristal zafiro teste"],
            title_candidates=["Cristal zafiro vibra sem tocar na mesa"],
            quality_metrics={},
        )
        script = Script(
            script_id="script-repetition-regenerated-current",
            job_id=current.job_id,
            schema_version="1.0.0",
            content_hash="script-regenerated-current",
            title="Cristal zafiro vibra sem tocar na mesa",
            hook="O cristal zafiro parece parado, mas vibra sozinho.",
            body_beats=["A borda muda de brilho.", "A mesa não encosta no centro.", "O reflexo entrega o truque."],
            ending="O objeto parecia imóvel, mas já estava reagindo.",
            cta=None,
            full_narration="O cristal zafiro parece parado, mas vibra sozinho. A borda muda de brilho.",
            estimated_duration_sec=35,
            key_facts=[],
            token_count=30,
            language="pt-BR",
            qa_metrics={},
        )
        session.add_all([topic_plan, script])
        session.commit()

    with SessionLocal() as session:
        current = session.get(Job, "job-repetition-regenerated-current")
        topic_plan = session.query(TopicPlan).filter_by(job_id=current.job_id).one()
        script = session.query(Script).filter_by(job_id=current.job_id).one()
        report = orchestrator.monetization_pipeline.build_channel_repetition_report(session, current, topic_plan, script)

    assert report["repetition_risk"] == "low"
    assert report["matches"] == []

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

    queued = orchestrator.publication_ops._sync_tiktok_crosspost_queue()

    with SessionLocal() as session:
        publications = session.query(ChannelPublication).filter_by(channel="tiktok", source="retropost").all()

    assert queued >= 1
    assert len(publications) == 1
    assert publications[0].status == "scheduled"

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
    cleaned = orchestrator.publication_ops._run_retention_sweep()

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
    cleaned = orchestrator.publication_ops._run_retention_sweep()

    assert cleaned >= 1
    assert not artifact_path.exists()
    with SessionLocal() as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == "rejected"
    assert job.quality_summary["retention"]["cleaned"] is True
    assert job.quality_summary["retention"]["classification"] == "recoverable"

def test_review_page_no_longer_promises_partial_retry() -> None:
    client = TestClient(app)
    response = client.post("/jobs", data={"seed_theme": "polvos", "target_duration_sec": 35}, follow_redirects=False)
    job_id = response.headers["location"].split("/")[-1]
    detail = client.get(f"/jobs/{job_id}")

    assert detail.status_code == 200
    assert 'name="retry_step"' not in detail.text
    assert 'value="retry_from_step"' not in detail.text
    assert 'value="retry"' not in detail.text
    assert "Nenhuma ação de revisão disponível para o status atual." in detail.text

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

    assert handlers["topic_plan"].__self__ is test_orchestrator.topic_pipeline
    assert handlers["script"].__self__ is test_orchestrator.script_pipeline
    assert handlers["scene_plan"].__self__ is test_orchestrator.scene_pipeline
    assert handlers["asset_generation"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["tts"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["subtitle_alignment"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["background_music"].__self__ is test_orchestrator.asset_pipeline
    assert handlers["render"].__self__ is test_orchestrator.render_pipeline
    assert handlers["monetization_readiness_gate"].__self__ is test_orchestrator.monetization_pipeline
    assert handlers["publish_to_review_hub"].__self__ is test_orchestrator.monetization_pipeline

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
    orchestrator.stop_event.clear()
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

def test_extract_fact_entity_prefers_subject_before_colon() -> None:
    entity = orchestrator.script_pipeline._extract_fact_entity(
        "Polvos: curiosidades científicas sobre o cefalópode mais inteligente do oceano"
    )

    assert entity.lower() == "polvos"

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

    monkeypatch.setattr("app.providers.tts.subprocess.run", fake_run)
    LocalSpeechFallbackProvider()._apply_final_loudness_normalization(audio_path)

    command_text = " ".join(captured["command"])
    assert "loudnorm=I=-16:LRA=11:TP=-1.5" in command_text
    assert audio_path.exists()

def test_elevenlabs_tts_generates_normalized_wav_and_local_srt(tmp_path: Path, monkeypatch) -> None:
    from app.providers.tts import ElevenLabsTTSProvider

    settings = SimpleNamespace(
        elevenlabs_api_key="test-key",
        elevenlabs_base_url="https://api.elevenlabs.io",
        elevenlabs_voice_id="voice-ptbr",
        elevenlabs_model_id="eleven_multilingual_v2",
        elevenlabs_output_format="mp3_44100_128",
        elevenlabs_timeout_sec=120.0,
        elevenlabs_voice_stability=0.5,
        elevenlabs_voice_similarity_boost=0.75,
        elevenlabs_voice_style=0.0,
        elevenlabs_voice_use_speaker_boost=True,
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, *, params: dict[str, str], headers: dict[str, str], json: dict[str, object]):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(200, content=b"audio")

    def fake_normalize(source_path: Path, output_path: Path) -> None:
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24_000)
            wav_file.writeframes(b"\0\0" * 24_000)

    monkeypatch.setattr("app.providers.tts.get_settings", lambda: settings)
    monkeypatch.setattr("app.providers.tts.httpx.Client", FakeClient)
    monkeypatch.setattr(ElevenLabsTTSProvider, "_normalize_elevenlabs_audio", lambda self, source, output: fake_normalize(source, output))

    audio_path = tmp_path / "voice.wav"
    srt_path = tmp_path / "voice.srt"
    result = ElevenLabsTTSProvider().synthesize("Texto curto em portugues brasileiro.", audio_path, srt_path)

    assert result["provider"] == "elevenlabs"
    assert result["voice"] == "voice-ptbr"
    assert result["duration_ms"] == 1000
    assert result["provider_metadata"]["fallback_used"] is False
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice-ptbr"
    assert captured["params"] == {"output_format": "mp3_44100_128"}
    assert captured["json"]["model_id"] == "eleven_multilingual_v2"
    assert "Texto curto" in srt_path.read_text(encoding="utf-8")

def test_gemini_tts_generates_wav_and_local_srt(tmp_path: Path, monkeypatch) -> None:
    from app.providers.tts import GeminiTTSProvider

    settings = SimpleNamespace(
        gemini_api_key="gemini-key",
        gemini_tts_api_key=None,
        gemini_tts_model="gemini-3.1-flash-tts-preview",
        gemini_tts_voice_name="Kore",
        gemini_tts_style_prompt="Narre em portugues brasileiro natural.",
        gemini_tts_timeout_sec=120.0,
    )
    pcm = b"\0\0" * 24_000

    monkeypatch.setattr("app.providers.tts.get_settings", lambda: settings)
    monkeypatch.setattr(GeminiTTSProvider, "_generate_gemini_audio_bytes", lambda self, text, configured, api_key, context: (pcm, "audio/L16;rate=24000"))
    monkeypatch.setattr(GeminiTTSProvider, "_apply_final_loudness_normalization", lambda self, audio_path: None)

    audio_path = tmp_path / "voice.wav"
    srt_path = tmp_path / "voice.srt"
    result = GeminiTTSProvider().synthesize("Texto curto em portugues brasileiro.", audio_path, srt_path)

    assert result["provider"] == "gemini_tts"
    assert result["voice"] == "Kore"
    assert result["duration_ms"] == 1000
    assert result["provider_metadata"]["model_id"] == "gemini-3.1-flash-tts-preview"
    assert result["provider_metadata"]["fallback_used"] is False
    assert "Texto curto" in srt_path.read_text(encoding="utf-8")
    with wave.open(str(audio_path), "rb") as wav_file:
        assert wav_file.getframerate() == 24000
        assert wav_file.getnchannels() == 1

def test_gemini_tts_prompt_prioritizes_hook_and_retention() -> None:
    from app.providers.tts import GeminiTTSProvider

    settings = SimpleNamespace(
        gemini_tts_style_prompt="Narre em portugues brasileiro natural.",
    )
    prompt = GeminiTTSProvider()._build_gemini_prompt(
        "O polvo não pensa só com a cabeça.",
        settings,
        {
            "canonical_topic": "polvos",
            "angle": "neurobiologia curiosa",
            "title": "Polvos parecem alienigenas",
            "hook": "O polvo não pensa só com a cabeça.",
            "ending": "O começo vira outra coisa quando o braço decide sozinho.",
            "estimated_duration_sec": 40,
            "retention_map": {
                "visual_hook": "O hook precisa parecer impossível no primeiro segundo.",
                "turn_or_payoff": "A virada mostra o braço processando sinais.",
                "loop_close": "O fechamento aponta de volta para o começo.",
            },
        },
    )

    assert "O hook deve segurar atenção" in prompt
    assert "A retenção vem antes de dramatização" in prompt
    assert "O payoff deve ganhar ênfase" in prompt
    assert "O fechamento deve recontextualizar" in prompt
    assert "visual_hook: O hook precisa parecer impossível" in prompt
    assert "Preserve exatamente o texto aprovado" in prompt

def test_gemini_tts_falls_back_to_elevenlabs_when_primary_is_not_configured(tmp_path: Path, monkeypatch) -> None:
    from app.providers.tts import ElevenLabsTTSProvider, GeminiTTSProvider

    settings = SimpleNamespace(gemini_api_key=None, gemini_tts_api_key=None)

    def fake_elevenlabs_synthesize(self, text: str, audio_path: Path, srt_path: Path, context: dict[str, object] | None = None) -> dict[str, object]:
        audio_path.write_bytes(b"elevenlabs")
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nelevenlabs\n", encoding="utf-8")
        return {
            "provider": "elevenlabs",
            "voice": "voice-ptbr",
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": 1000,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {"fallback_used": False},
        }

    monkeypatch.setattr("app.providers.tts.get_settings", lambda: settings)
    monkeypatch.setattr(ElevenLabsTTSProvider, "synthesize", fake_elevenlabs_synthesize)

    result = GeminiTTSProvider().synthesize("Texto", tmp_path / "voice.wav", tmp_path / "voice.srt")

    assert result["provider"] == "elevenlabs"
    assert result["provider_metadata"]["fallback_used"] is True
    assert result["provider_metadata"]["fallback_from_provider"] == "gemini_tts"
    assert result["provider_metadata"]["fallback_provider"] == "elevenlabs"

def test_elevenlabs_tts_falls_back_to_edge_tts_when_primary_fails(tmp_path: Path, monkeypatch) -> None:
    from app.providers.tts import EdgeTTSProvider, ElevenLabsTTSProvider

    settings = SimpleNamespace(elevenlabs_api_key=None)

    def fake_edge_synthesize(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, object]:
        audio_path.write_bytes(b"edge")
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nedge\n", encoding="utf-8")
        return {
            "provider": "edge_tts",
            "voice": "pt-BR-FranciscaNeural",
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": 1000,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {"fallback_used": False},
        }

    monkeypatch.setattr("app.providers.tts.get_settings", lambda: settings)
    monkeypatch.setattr(EdgeTTSProvider, "synthesize", fake_edge_synthesize)
    monkeypatch.setattr("app.providers.tts.time.sleep", lambda _: None)

    result = ElevenLabsTTSProvider().synthesize("Texto", tmp_path / "voice.wav", tmp_path / "voice.srt")

    assert result["provider"] == "edge_tts"
    assert result["provider_metadata"]["fallback_used"] is True
    assert result["provider_metadata"]["fallback_from_provider"] == "elevenlabs"
    assert result["provider_metadata"]["fallback_provider"] == "edge_tts"

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

def test_channel_ai_generated_content_auto_confirms_disclosure(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.settings, "conservative_synthetic_disclosure", True)
    monkeypatch.setattr(orchestrator.settings, "channel_ai_generated_content", True)
    report = orchestrator.monetization_pipeline.build_ai_disclosure_report(
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

def test_fact_result_relevance_rejects_fuzzy_wrong_search_hit() -> None:
    assert not orchestrator.script_pipeline._fact_result_is_relevant(
        "polvo muda",
        "Povo munda",
        "O povo munda é um grupo étnico do subcontinente indiano.",
    )

def test_fact_result_relevance_rejects_single_token_only_in_abstract() -> None:
    assert not orchestrator.script_pipeline._fact_result_is_relevant(
        "Modena",
        "Preferred Reporting Items for Systematic Reviews and Meta-Analyses",
        "The author affiliation mentions Università di Modena e Reggio Emilia.",
    )

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

    report = orchestrator.monetization_pipeline.build_fact_claims_report(None, None, fact_pack, script_artifact)

    assert report["claim_trace"] == script_artifact["claim_trace"]
    assert report["grounded_claim_trace"][0]["source_fact_ids"] == ["F1"]
    assert report["ungrounded_claim_trace"] == []

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

    audit = audit_source_relevance(
        research_brief,
        "Teor de cafeina em cafés brasileiros comercializados em diferentes formas",
        "O estudo compara a variacao do teor de cafeina entre amostras de cafe e mede diferencas entre formas de preparo.",
    )

    assert audit["passed"] is False
    assert audit["reason"] == "missing_promised_mechanism_terms"

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
