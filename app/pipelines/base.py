from __future__ import annotations

from typing import Any


class BasePipeline:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    @property
    def settings(self) -> Any:
        return self.owner.settings

    @property
    def storage(self) -> Any:
        return self.owner.storage

    @property
    def providers(self) -> Any:
        return self.owner.providers

    @property
    def script_gate(self) -> Any:
        return self.owner.script_gate

    @property
    def scene_gate(self) -> Any:
        return self.owner.scene_gate

    @property
    def asset_gate(self) -> Any:
        return self.owner.asset_gate

    @property
    def subtitle_gate(self) -> Any:
        return self.owner.subtitle_gate

    @property
    def render_gate(self) -> Any:
        return self.owner.render_gate

    def _append_event(self, job_id: str, event_name: str, status: str, payload: dict[str, Any]) -> None:
        self.owner._append_event(job_id, event_name, status, payload)

    def _persist_repair_telemetry(self, job_id: str, stage: str, payload: dict[str, Any]) -> str:
        return self.owner._persist_repair_telemetry(job_id, stage, payload)

    def _remove_stale_quality_report(self, job_id: str, relative_path: str) -> None:
        self.owner._remove_stale_quality_report(job_id, relative_path)

    def _serialize_for_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.owner._serialize_for_json(payload)

    def _recent_topic_history(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner.topic_pipeline.recent_topic_history(*args, **kwargs)

    def _channel_learning_brief(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner.topic_pipeline.channel_learning_brief(*args, **kwargs)
