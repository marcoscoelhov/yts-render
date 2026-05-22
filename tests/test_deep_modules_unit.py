from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.hub_context import HubContext
from app.pipelines.script_audit import ScriptAuditDomain
from app.pipelines.script_metrics import normalize_script_metrics
from app.publication_ops import PublicationOperations
from app.utils import utcnow


class _StorageSpy:
    def __init__(self) -> None:
        self.persisted: list[tuple[str, str, dict]] = []

    def persist_json(self, job_id: str, relative_path: str, payload: dict) -> None:
        self.persisted.append((job_id, relative_path, payload))


def test_script_metrics_unit_normalizes_provider_score_shapes() -> None:
    metrics = normalize_script_metrics(
        {
            "hook_score": "8/10",
            "clarity_score": "90%",
            "information_density_score": "7",
            "repetition_score": "0.95",
            "ending_strength_score": "aprovado",
        }
    )

    assert metrics["hook_score"] == 0.8
    assert metrics["clarity_score"] == 0.9
    assert metrics["information_density_score"] == 0.7
    assert metrics["repetition_score"] == 0.05
    assert metrics["ending_strength_score"] is True


def test_script_audit_unit_skips_simple_mode_and_persists_artifact() -> None:
    storage = _StorageSpy()
    owner = SimpleNamespace(
        settings=SimpleNamespace(simple_shorts_mode=True, schema_version="1.0.0"),
        storage=storage,
        providers=SimpleNamespace(creative=SimpleNamespace()),
        _serialize_for_json=lambda payload: payload,
    )
    audit = ScriptAuditDomain(owner)._text_publish_audit("job-1", {"title": "x"}, {"status": "skipped"})

    assert audit == {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True}
    assert storage.persisted[0][1] == "text_publish_audit.json"
    assert storage.persisted[0][2]["audit"]["provider"] == "simple_shorts_mode"


def test_hub_context_unit_classifies_operational_status_for_scheduled_job() -> None:
    context = HubContext(SimpleNamespace(), SimpleNamespace(build_job_progress=lambda job: {}), SimpleNamespace())
    job = SimpleNamespace(status="approved_for_publish")
    schedule = SimpleNamespace(status="scheduled", scheduled_for_utc=utcnow() + timedelta(days=1))

    status = context._publication_operational_status(job, schedule)

    assert status["status"] == "scheduled_publication"
    assert status["stage"] == "Programação"
    assert status["label"] == "Programado"


def test_publication_ops_unit_retention_metadata_uses_publishable_ttl() -> None:
    settings = SimpleNamespace(
        artifact_ttl_hard_failure_hours=24,
        artifact_ttl_recoverable_hours=168,
        artifact_ttl_publishable_hours=504,
    )
    ops = PublicationOperations(SimpleNamespace(settings=settings))
    base_time = utcnow()
    job = SimpleNamespace(status="approved_for_publish", created_at=base_time, updated_at=base_time)

    metadata = ops._retention_metadata(job, None, now=base_time)

    assert metadata is not None
    assert metadata["classification"] == "publishable"
    assert metadata["expires_at"] == (base_time + timedelta(hours=504)).isoformat()
