from __future__ import annotations

import queue
import threading
from typing import Any

from app.pipelines.base import BasePipeline
from app.utils import utcnow


class ScriptAuditDomain(BasePipeline):
    def __getattr__(self, name: str) -> Any:
        return getattr(self.owner, name)

    def _text_publish_audit(self, job_id: str, script: dict[str, Any], fact_pack: dict[str, Any]) -> dict[str, Any]:
        if self.settings.simple_shorts_mode and fact_pack.get("status") == "skipped":
            audit = {"passed": True, "reasons": [], "provider": "simple_shorts_mode", "skipped": True}
            self.storage.persist_json(
                job_id,
                "text_publish_audit.json",
                {
                    "schema_version": self.settings.schema_version,
                    "job_id": job_id,
                    "created_at": utcnow().isoformat(),
                    "audit": self._serialize_for_json(audit),
                },
            )
            return audit
        if fact_pack.get("provider") == "user_declared_fact_check" and fact_pack.get("status") == "verified":
            audit = {
                "passed": True,
                "reasons": [],
                "provider": "user_declared_fact_check",
                "skipped": True,
                "scope": "ready_script_human_fact_confirmation",
            }
            self.storage.persist_json(
                job_id,
                "text_publish_audit.json",
                {
                    "schema_version": self.settings.schema_version,
                    "job_id": job_id,
                    "created_at": utcnow().isoformat(),
                    "audit": self._serialize_for_json(audit),
                },
            )
            return audit
        auditor = getattr(self.providers.creative, "audit_publish_package", None)
        if auditor is None:
            return {"passed": True, "reasons": [], "provider": "none", "skipped": True}
        payload = {
            "script": {
                "title": script.get("title"),
                "hook": script.get("hook"),
                "ending": script.get("ending"),
                "full_narration": script.get("full_narration"),
                "key_facts": script.get("key_facts"),
                "source_fact_ids": script.get("source_fact_ids"),
                "claim_trace": script.get("claim_trace"),
            },
            "fact_pack": fact_pack,
            "hashtags": ["#shorts"],
            "audit_phase": "text_before_assets",
        }
        timeout_sec = float(self.settings.llm_publish_audit_timeout_sec)
        bound_owner = getattr(auditor, "__self__", None)
        if (
            bound_owner is self.providers.creative
            and getattr(bound_owner, "fallback", None) is not None
            and not bool(getattr(bound_owner, "strict_minimax_validation", False))
        ):
            timeout_sec = max(timeout_sec, float(self.settings.llm_publish_audit_timeout_sec) * 2 + 10.0)
        try:
            audit = self._call_with_timeout(
                lambda: auditor(payload),
                timeout_sec=timeout_sec,
            )
        except TimeoutError:
            audit = {
                "passed": False,
                "reasons": ["text_publish_audit_timeout"],
                "provider": "publish_auditor",
                "timeout_sec": timeout_sec,
            }
        except Exception as exc:  # noqa: BLE001
            audit = {"passed": False, "reasons": ["text_publish_audit_failed"], "error": str(exc), "provider": "publish_auditor"}
        if not isinstance(audit, dict):
            audit = {"passed": False, "reasons": ["text_publish_audit_invalid"], "provider": "publish_auditor"}
        audit = self._normalize_text_publish_audit(audit)
        self.storage.persist_json(
            job_id,
            "text_publish_audit.json",
            {
                "schema_version": self.settings.schema_version,
                "job_id": job_id,
                "created_at": utcnow().isoformat(),
                "audit": self._serialize_for_json(audit),
            },
        )
        return audit

    def _normalize_text_publish_audit(self, audit: dict[str, Any]) -> dict[str, Any]:
        reasons = [str(reason) for reason in audit.get("reasons") or []]
        ignored_reasons = [reason for reason in reasons if reason == "weak_hashtags"]
        blocking_reasons = [reason for reason in reasons if reason != "weak_hashtags"]
        if ignored_reasons:
            audit = dict(audit)
            audit["reasons"] = blocking_reasons
            audit["ignored_reasons"] = list(dict.fromkeys([*(audit.get("ignored_reasons") or []), *ignored_reasons]))
            if not blocking_reasons and audit.get("passed") is False:
                audit["passed"] = True
        return audit

    def _call_with_timeout(self, func: Any, timeout_sec: float) -> Any:
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def runner() -> None:
            try:
                result_queue.put(("ok", func()))
            except Exception as exc:  # noqa: BLE001
                result_queue.put(("error", exc))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            status, result = result_queue.get(timeout=timeout_sec)
        except queue.Empty as exc:
            raise TimeoutError(f"operation timed out after {timeout_sec}s") from exc
        if status == "error":
            raise result
        return result
