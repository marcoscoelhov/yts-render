from __future__ import annotations

from pathlib import Path

import pytest

from tests import e2e_support as support


@pytest.fixture(scope="session", autouse=True)
def e2e_environment():
    support.shutil.rmtree(Path(support.os.environ["YTS_DATA_DIR"]), ignore_errors=True)
    Path(support.os.environ["YTS_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    support.init_db()
    support.orchestrator.start_worker()
    yield
    support.orchestrator.stop_worker()


@pytest.fixture(autouse=True)
def isolate_youtube_settings(monkeypatch):
    support.orchestrator.stop_event.clear()
    monkeypatch.setattr(support.main_module.settings, "youtube_publish_mode", "manual")
    monkeypatch.setattr(support.main_module.settings, "youtube_api_enabled", False)
    monkeypatch.setattr(support.orchestrator.settings, "youtube_publish_mode", "manual")
    monkeypatch.setattr(support.orchestrator.settings, "youtube_api_enabled", False)
    monkeypatch.setattr(support.main_module.settings, "tiktok_auto_publish_enabled", False)
    monkeypatch.setattr(support.orchestrator.settings, "tiktok_auto_publish_enabled", False)
    monkeypatch.setattr(support.main_module.settings, "tiktok_access_token", None)
    monkeypatch.setattr(support.orchestrator.settings, "tiktok_access_token", None)
    yield
    support.orchestrator.stop_event.clear()
