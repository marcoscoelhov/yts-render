from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Job, ScenePlan, Script, TopicPlan, TopicRequest
from app.pipelines.common import RecoverableStepError, model_payload
from app.pipelines.base import BasePipeline
from app.utils import new_id, stable_hash, utcnow, word_tokens


class ScenePipeline(BasePipeline):
    def step_scene_plan(self, session: Session, job: Job, attempt: int) -> list[str]:
        self._remove_stale_quality_report(job.job_id, "scene_plan_rejected.json")
        self._remove_stale_quality_report(job.job_id, "scene_plan_raw.json")
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        assert script and topic_plan
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        script_dict = {
            "title": script.title,
            "hook": script.hook,
            "body_beats": script.body_beats,
            "ending": script.ending,
            "cta": script.cta,
            "full_narration": script.full_narration,
            "estimated_duration_sec": script.estimated_duration_sec,
            "key_facts": script.key_facts,
            "qa_metrics": script.qa_metrics,
            "canonical_topic": topic_plan.canonical_topic,
            "angle": topic_plan.angle,
            "hub_viral_prompt_source": request.notes if request else None,
            "downstream_rule": "Scenes, images, subtitles and TTS must derive from full_narration. Do not invent new beats or split tiny punchlines into standalone render scenes.",
        }
        scenes = self.providers.creative.plan_scenes(script_dict, self.settings.scene_target_count)
        self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
        tokens = word_tokens(script.full_narration)
        scenes = self.normalize_scene_token_coverage(scenes, script.full_narration)
        if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
            fallback_planner = self.scene_fallback_planner()
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
                self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
                scenes = self.normalize_scene_token_coverage(scenes, script.full_narration)
            if not scenes or scenes[0]["token_start"] != 0 or scenes[-1]["token_end"] != len(tokens) - 1:
                raise RecoverableStepError("scene coverage invalid")
        scenes = [self.normalize_scene_semantics(scene, topic_plan.canonical_topic) for scene in scenes]
        scene_gate = self.scene_gate.validate(scenes, self.settings.scene_target_count)
        if not scene_gate.passed:
            fallback_planner = self.scene_fallback_planner()
            if fallback_planner is not None:
                scenes = fallback_planner.plan_scenes(script_dict, self.settings.scene_target_count)
                self.storage.persist_json(job.job_id, "scene_plan_raw.json", self._serialize_for_json({"scenes": scenes}))
                scenes = self.normalize_scene_token_coverage(scenes, script.full_narration)
                scenes = [self.normalize_scene_semantics(scene, topic_plan.canonical_topic) for scene in scenes]
                scene_gate = self.scene_gate.validate(scenes, self.settings.scene_target_count)
            if not scene_gate.passed:
                self.storage.persist_json(
                    job.job_id,
                    "scene_plan_rejected.json",
                    {"reasons": scene_gate.reasons, "metrics": scene_gate.metrics, "scenes": scenes},
                )
                raise RecoverableStepError(f"scene plan quality gate failed: {', '.join(scene_gate.reasons[:6])}")
        created_at = utcnow()
        payload = {
            "schema_version": self.settings.schema_version,
            "scene_plan_id": new_id(),
            "job_id": job.job_id,
            "created_at": created_at,
            "content_hash": stable_hash(scenes),
            "scene_count": len(scenes),
            "scenes": scenes,
        }
        session.execute(delete(ScenePlan).where(ScenePlan.job_id == job.job_id))
        session.add(ScenePlan(**model_payload(ScenePlan, payload)))
        self.storage.persist_json(job.job_id, "scene_plan.json", self._serialize_for_json(payload))
        quality_summary = dict(job.quality_summary or {})
        quality_summary["scene_plan"] = {**scene_gate.metrics, "scene_plan_gate_pass": True}
        job.quality_summary = quality_summary
        self._append_event(job.job_id, "scene_plan.generated", "succeeded", quality_summary["scene_plan"])
        return ["scene_plan.json"]

    def scene_fallback_planner(self) -> Any:
        if self.settings.strict_minimax_validation:
            return None
        return getattr(self.providers.creative, "scene_provider", None) or getattr(self.providers.creative, "fallback", None)

    def normalize_scene_token_coverage(self, scenes: list[dict[str, Any]], full_narration: str) -> list[dict[str, Any]]:
        if not scenes:
            return scenes
        tokens = word_tokens(full_narration)
        total_tokens = len(tokens)
        if total_tokens <= 0:
            return scenes
        ordered = [dict(scene) for scene in sorted(scenes, key=lambda scene: int(scene.get("order", 0) or 0))]
        weights = [max(1, len(word_tokens(str(scene.get("narration_text") or "")))) for scene in ordered]
        remaining_tokens = total_tokens
        remaining_weight = sum(weights)
        cursor = 0
        normalized: list[dict[str, Any]] = []
        for index, scene in enumerate(ordered):
            scene_id = str(scene.get("scene_id") or f"scene-{index + 1}")
            scenes_left = len(ordered) - index
            weight = weights[index]
            if index == len(ordered) - 1:
                count = remaining_tokens
            else:
                proportional = round(remaining_tokens * (weight / max(remaining_weight, 1)))
                count = max(1, min(proportional, remaining_tokens - (scenes_left - 1)))
            start = cursor
            end = start + count - 1
            exact_text = " ".join(tokens[start : end + 1]).strip()
            normalized.append(
                {
                    **scene,
                    "scene_id": scene_id,
                    "order": index + 1,
                    "token_start": start,
                    "token_end": end,
                    "narration_text": exact_text or str(scene.get("narration_text") or "").strip(),
                }
            )
            cursor = end + 1
            remaining_tokens -= count
            remaining_weight -= weight
        if normalized:
            normalized[0]["token_start"] = 0
            normalized[-1]["token_end"] = total_tokens - 1
            normalized[-1]["narration_text"] = " ".join(tokens[normalized[-1]["token_start"] : total_tokens]).strip()
        return normalized

    def normalize_scene_semantics(self, scene: dict[str, Any], canonical_topic: str) -> dict[str, Any]:
        topic_text = canonical_topic.replace("_", " ").strip()
        normalized = dict(scene)
        normalized["scene_id"] = str(scene.get("scene_id") or normalized.get("scene_id") or "scene-1")
        primary_subject = str(scene.get("primary_subject") or topic_text).replace("_", " ").strip()
        normalized["primary_subject"] = primary_subject or topic_text
        normalized["topic_hint"] = str(scene.get("topic_hint") or topic_text).replace("_", " ").strip() or topic_text
        base_queries = [
            query.replace("_", " ").strip()
            for query in scene.get("fallback_queries", [topic_text, f"{topic_text} astronomia", f"{topic_text} espaco"])
        ]
        normalized["fallback_queries"] = self.fallback_query_variants(topic_text, base_queries)
        normalized["image_prompt"] = self.owner.asset_pipeline.image_assets.semantic_english_image_prompt(scene, topic_text, primary_subject)
        return normalized

    def fallback_query_variants(self, topic_text: str, base_queries: list[str]) -> list[str]:
        queries = [query for query in base_queries if query]
        normalized_topic = topic_text.lower()
        if "buraco" in normalized_topic and "negro" in normalized_topic:
            queries.extend(["black hole space", "black hole astronomy", "accretion disk space"])
        queries.extend([topic_text, f"{topic_text} ciencia", f"{topic_text} fotografia"])
        deduped: list[str] = []
        for query in queries:
            if query not in deduped:
                deduped.append(query)
        return deduped
