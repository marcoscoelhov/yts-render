from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.editorial.repetition import build_channel_repetition_report
from app.compliance.review import build_human_review_checklist
from app.models import BackgroundMusicAsset, Job, NarrationAsset, PerformanceMetric, RenderOutput, ReviewRecord, SceneAsset, Script, SubtitleTrack, TopicPlan, TopicRequest
from app.pipelines.base import BasePipeline
from app.utils import iso_now, stable_hash, word_tokens


class MonetizationPipeline(BasePipeline):
    def step_monetization_readiness(self, session: Session, job: Job, attempt: int) -> list[str]:
        report = self.build_monetization_report(session, job)
        self.storage.persist_json(job.job_id, "rights_registry.json", self._serialize_for_json(report["rights_registry"]))
        self.storage.persist_json(job.job_id, "ai_disclosure.json", self._serialize_for_json(report["ai_disclosure"]))
        self.storage.persist_json(job.job_id, "fact_claims_report.json", self._serialize_for_json(report["fact_claims_report"]))
        self.storage.persist_json(job.job_id, "channel_repetition_report.json", self._serialize_for_json(report["channel_repetition_report"]))
        self.storage.persist_json(job.job_id, "metadata_review.json", self._serialize_for_json(report["metadata_review"]))
        self.storage.persist_json(job.job_id, "monetization_report.json", self._serialize_for_json(report))
        quality_summary = dict(job.quality_summary or {})
        quality_summary["monetization"] = {
            "passed": report["passed"],
            "final_status": report["final_status"],
            "hard_blockers": report["hard_blockers"],
            "manual_required": report["manual_required"],
            "warnings": report["warnings"],
            "content_hash": stable_hash(report),
        }
        job.quality_summary = quality_summary
        self._append_event(
            job.job_id,
            "monetization.checked",
            "succeeded",
            {
                "passed": report["passed"],
                "final_status": report["final_status"],
                "hard_blockers": report["hard_blockers"],
                "manual_required": report["manual_required"],
            },
        )
        return [
            "rights_registry.json",
            "ai_disclosure.json",
            "fact_claims_report.json",
            "channel_repetition_report.json",
            "metadata_review.json",
            "monetization_report.json",
        ]

    def build_monetization_report(self, session: Session, job: Job, extra_confirmations: set[str] | None = None) -> dict[str, Any]:
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        narration = session.scalar(select(NarrationAsset).where(NarrationAsset.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job.job_id))
        assets = session.scalars(select(SceneAsset).where(SceneAsset.job_id == job.job_id, SceneAsset.selected.is_(True)).order_by(SceneAsset.scene_id)).all()
        fact_pack = self.read_job_json(job.job_id, "fact_pack.json")
        script_artifact = self.read_job_json(job.job_id, "script.json")
        tags = self.build_publish_hashtags(topic_plan, script)
        checklist = {
            "script_gate_pass": bool((job.quality_summary or {}).get("script", {}).get("script_quality_gate_pass")),
            "scene_plan_gate_pass": bool((job.quality_summary or {}).get("scene_plan", {}).get("scene_plan_gate_pass")),
            "asset_gate_pass": bool((job.quality_summary or {}).get("assets", {}).get("semantic_threshold_pass")),
            "subtitle_gate_pass": bool((job.quality_summary or {}).get("subtitles", {}).get("subtitle_gate_pass")),
            "render_gate_pass": bool((job.quality_summary or {}).get("render", {}).get("render_gate_pass")),
        }
        confirmations = self.manual_monetization_confirmations(session, job.job_id)
        confirmations.update(extra_confirmations or set())

        rights_registry = self.build_rights_registry(job, assets, narration, background_music)
        ai_disclosure = self.build_ai_disclosure_report(assets)
        fact_claims_report = self.build_fact_claims_report(script, topic_plan, fact_pack, script_artifact)
        channel_repetition_report = self.build_channel_repetition_report(session, job, topic_plan, script)
        metadata_review = self.build_metadata_review(topic_plan, script, tags)
        publish_readiness = self.publish_readiness_report(
            script,
            topic_plan,
            fact_pack,
            tags,
            checklist,
            script_artifact,
            self.provider_publish_audit(script_artifact, fact_pack, tags, job.job_id),
        )

        hard_blockers: list[str] = []
        manual_required: list[str] = []
        warnings: list[str] = []
        if not all(checklist.values()):
            hard_blockers.append("quality_gate_not_passed")
        if not rights_registry["all_commercial_rights_confirmed"]:
            manual_required.append("rights_confirmation_required")
        if (
            ai_disclosure["youtube_disclosure_required"]
            and not ai_disclosure.get("auto_confirmed")
            and "ai_disclosure_confirmed" not in confirmations
        ):
            manual_required.append("youtube_ai_disclosure_toggle_required")
        if fact_claims_report["requires_fact_review"] and "fact_review_confirmed" not in confirmations:
            manual_required.append("fact_review_required")
        if metadata_review["requires_metadata_review"] and "metadata_confirmed" not in confirmations:
            manual_required.append("metadata_review_required")
        if channel_repetition_report["repetition_risk"] != "low" and "originality_confirmed" not in confirmations:
            manual_required.append("originality_review_required")
        if "rights_confirmed" in confirmations:
            manual_required = [item for item in manual_required if item != "rights_confirmation_required"]
        warnings.extend(publish_readiness["reasons"])
        if not self.settings.allow_synthetic_visuals_for_monetization and ai_disclosure["contains_synthetic_visuals"]:
            hard_blockers.append("synthetic_visuals_disabled_by_policy")
        if render and render.duration_ms > 60_000:
            hard_blockers.append("shorts_duration_over_60s")
        if channel_repetition_report["repetition_risk"] == "high" and "originality_confirmed" not in confirmations:
            hard_blockers.append("channel_repetition_high")
        hard_blockers.extend(self.automatic_publish_blockers(publish_readiness))

        hard_blockers = list(dict.fromkeys(hard_blockers))
        manual_required = list(dict.fromkeys(manual_required))
        warnings = list(dict.fromkeys(warnings))
        human_review_checklist = self.build_human_review_checklist(
            rights_registry=rights_registry,
            ai_disclosure=ai_disclosure,
            fact_claims_report=fact_claims_report,
            metadata_review=metadata_review,
            channel_repetition_report=channel_repetition_report,
            confirmations=confirmations,
        )
        passed = not hard_blockers and not manual_required
        final_status = "ready_for_upload" if passed else ("blocked_for_monetization" if hard_blockers else "monetization_review")
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "created_at": iso_now(),
            "passed": passed,
            "final_status": final_status,
            "hard_blockers": hard_blockers,
            "manual_required": manual_required,
            "warnings": warnings,
            "manual_confirmations": sorted(confirmations),
            "human_review_checklist": human_review_checklist,
            "quality_checklist": checklist,
            "rights_registry": rights_registry,
            "ai_disclosure": ai_disclosure,
            "fact_claims_report": fact_claims_report,
            "channel_repetition_report": channel_repetition_report,
            "metadata_review": metadata_review,
            "publish_readiness": publish_readiness,
        }

    def automatic_publish_blockers(self, publish_readiness: dict[str, Any]) -> list[str]:
        automatic_reasons = {
            "source_fact_mismatch",
            "unsupported_claim",
            "weak_ending",
            "truncated_ending_logic",
            "low_retention_hook",
            "minimax_audit_failed",
            "minimax_audit_invalid",
            "text_publish_audit_timeout",
            "invented_source_fact_ids",
            "fact_pack_missing_for_factual_topic",
            "quality_checklist_failed",
            "placeholder_source_language",
        }
        return [reason for reason in publish_readiness.get("reasons") or [] if reason in automatic_reasons]

    def build_human_review_checklist(
        self,
        rights_registry: dict[str, Any],
        ai_disclosure: dict[str, Any],
        fact_claims_report: dict[str, Any],
        metadata_review: dict[str, Any],
        channel_repetition_report: dict[str, Any],
        confirmations: set[str],
    ) -> dict[str, Any]:
        return build_human_review_checklist(
            rights_registry=rights_registry,
            ai_disclosure=ai_disclosure,
            fact_claims_report=fact_claims_report,
            metadata_review=metadata_review,
            channel_repetition_report=channel_repetition_report,
            confirmations=confirmations,
        )

    def build_rights_registry(
        self,
        job: Job,
        assets: list[SceneAsset],
        narration: NarrationAsset | None,
        background_music: BackgroundMusicAsset | None,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for asset in assets:
            provider = asset.provider.lower()
            confirmed = bool(self.settings.minimax_commercial_rights_confirmed) if provider == "minimax" else bool(asset.license_note)
            evidence_url = self.settings.minimax_rights_evidence_url if provider == "minimax" else asset.license_note
            entries.append(
                {
                    "asset_type": asset.kind,
                    "scene_id": asset.scene_id,
                    "provider": asset.provider,
                    "uri": asset.uri,
                    "commercial_use_allowed": confirmed,
                    "license_source": asset.license_note or ("YTS_MINIMAX_COMMERCIAL_RIGHTS_CONFIRMED" if provider == "minimax" else None),
                    "rights_evidence_url": evidence_url,
                    "evidence_required": confirmed and provider == "minimax" and not bool(evidence_url),
                    "requires_attribution": bool(asset.attribution),
                    "attribution": asset.attribution,
                    "terms_checked_at": iso_now() if confirmed else None,
                    "review_required": not confirmed or (confirmed and provider == "minimax" and not bool(evidence_url)),
                }
            )
        if narration:
            provider = narration.provider.lower()
            confirmed = bool(self.settings.edge_tts_commercial_rights_confirmed) if provider == "edge_tts" else provider == "synthetic_wav"
            evidence_url = self.settings.edge_tts_rights_evidence_url if provider == "edge_tts" else "local_synthetic_test_audio"
            entries.append(
                {
                    "asset_type": "voice",
                    "provider": narration.provider,
                    "voice": narration.voice,
                    "uri": narration.audio_uri,
                    "commercial_use_allowed": confirmed,
                    "license_source": "YTS_EDGE_TTS_COMMERCIAL_RIGHTS_CONFIRMED" if provider == "edge_tts" else "local_synthetic_test_audio",
                    "rights_evidence_url": evidence_url,
                    "evidence_required": confirmed and provider == "edge_tts" and not bool(evidence_url),
                    "requires_attribution": False,
                    "terms_checked_at": iso_now() if confirmed else None,
                    "review_required": not confirmed or (confirmed and provider == "edge_tts" and not bool(evidence_url)),
                }
            )
        if background_music:
            provider = background_music.provider.lower()
            license_source = background_music.license_note or ("local_mock_background_music" if provider == "mock_music" else None)
            entries.append(
                {
                    "asset_type": "music",
                    "provider": background_music.provider,
                    "uri": background_music.audio_uri,
                    "commercial_use_allowed": bool(license_source),
                    "license_source": license_source,
                    "requires_attribution": bool(background_music.attribution),
                    "attribution": background_music.attribution,
                    "terms_checked_at": iso_now() if license_source else None,
                    "review_required": not bool(license_source),
                }
            )
            sound_design = dict(background_music.provider_metadata or {}).get("sound_design") or {}
            if sound_design.get("enabled"):
                entries.append(
                    {
                        "asset_type": "sound_design",
                        "provider": sound_design.get("provider") or "local_sfx",
                        "uri": sound_design.get("audio_uri"),
                        "commercial_use_allowed": True,
                        "license_source": sound_design.get("license_note") or "local_generated_sound_design",
                        "requires_attribution": False,
                        "terms_checked_at": iso_now(),
                        "review_required": False,
                    }
                )
        else:
            entries.append(
                {
                    "asset_type": "music",
                    "provider": "none",
                    "commercial_use_allowed": True,
                    "license_source": "no_music_used",
                    "requires_attribution": False,
                    "terms_checked_at": iso_now(),
                    "review_required": False,
                }
            )
        return {
            "entries": entries,
            "all_commercial_rights_confirmed": all(entry["commercial_use_allowed"] and not entry.get("evidence_required") for entry in entries),
            "review_required_count": sum(1 for entry in entries if entry["review_required"]),
            "evidence_required_count": sum(1 for entry in entries if entry.get("evidence_required")),
        }

    def build_ai_disclosure_report(self, assets: list[SceneAsset]) -> dict[str, Any]:
        synthetic_assets = [asset for asset in assets if asset.provider.lower() in {"minimax", "mock"}]
        realistic_terms = re.compile(r"\b(?:human|person|face|eyes|brain|body|portrait|realistic|documentary|cinematic|figure)\b", re.IGNORECASE)
        realistic_assets = [
            {
                "scene_id": asset.scene_id,
                "provider": asset.provider,
                "prompt_snapshot": (asset.prompt_snapshot or "")[:260],
            }
            for asset in synthetic_assets
            if realistic_terms.search(asset.prompt_snapshot or "")
        ]
        contains_synthetic = bool(synthetic_assets)
        disclosure_required = contains_synthetic if self.settings.conservative_synthetic_disclosure else contains_synthetic and bool(realistic_assets)
        auto_confirmed = bool(disclosure_required and self.settings.channel_ai_generated_content)
        return {
            "contains_synthetic_visuals": contains_synthetic,
            "youtube_disclosure_required": disclosure_required,
            "auto_confirmed": auto_confirmed,
            "confirmation_mode": "channel_policy" if auto_confirmed else "manual_review",
            "description_notice": "Imagens ilustrativas geradas por IA." if contains_synthetic else None,
            "reason": (
                "Channel policy marks AI disclosure automatically because generated visuals are present."
                if auto_confirmed
                else "Conservative synthetic disclosure mode: AI-generated visuals present."
                if disclosure_required and self.settings.conservative_synthetic_disclosure
                else "Realistic AI-generated illustrative visuals."
                if disclosure_required
                else "No realistic synthetic disclosure trigger detected."
            ),
            "policy_mode": "conservative" if self.settings.conservative_synthetic_disclosure else "realistic_only",
            "synthetic_asset_count": len(synthetic_assets),
            "realistic_synthetic_assets": realistic_assets,
        }

    def build_fact_claims_report(
        self,
        script: Script | None,
        topic_plan: TopicPlan | None,
        fact_pack: dict[str, Any],
        script_artifact: dict[str, Any],
    ) -> dict[str, Any]:
        script_dict = {**(self.script_to_dict(script) if script else {}), **(script_artifact or {})}
        fact_risk = self.script_gate._fact_risk_report(script_dict) if script_dict else {"claims": [], "claim_count": 0, "blocked": False}  # noqa: SLF001
        source_ids = script_dict.get("source_fact_ids") or script_dict.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        claim_trace = script_dict.get("claim_trace") or script_dict.get("qa_metrics", {}).get("claim_trace") or []
        if not isinstance(claim_trace, list):
            claim_trace = []
        facts = fact_pack.get("facts") or []
        sources = fact_pack.get("sources") or []
        valid_fact_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
        grounded_ids = [str(item) for item in source_ids if str(item) in valid_fact_ids]
        grounded_trace = []
        ungrounded_trace = []
        for item in claim_trace:
            if not isinstance(item, dict):
                continue
            item_ids = item.get("source_fact_ids") or []
            if isinstance(item_ids, str):
                item_ids = [item_ids]
            valid_item_ids = [str(source_id) for source_id in item_ids if str(source_id) in valid_fact_ids]
            normalized_item = {**item, "source_fact_ids": valid_item_ids}
            if valid_item_ids or str(item.get("grounding") or "").lower() in {"conservative", "common_knowledge", "user_input"}:
                grounded_trace.append(normalized_item)
            else:
                ungrounded_trace.append(normalized_item)
        factual_topic = bool(
            topic_plan
            and re.search(
                r"\b(?:por que|porque|como|ci[eê]ncia|f[ií]sica|biologia|engenharia|hist[oó]ria|sa[uú]de|m[eé]dico|animal|animais|flamingo|torre|c[eé]rebro|neuro|estat[ií]stica)\b",
                f"{topic_plan.canonical_topic} {topic_plan.angle}",
                re.IGNORECASE,
            )
        )
        requires_review = (
            fact_pack.get("status") != "verified"
            and (factual_topic or fact_risk.get("claim_count", 0) > 0 or len(word_tokens(script_dict.get("full_narration", ""))) >= 45)
        )
        claim_sources = [
            {
                "claim": fact.get("claim"),
                "fact_id": fact.get("fact_id"),
                "source_id": fact.get("source_id"),
                "source_url": next((source.get("url") for source in sources if source.get("source_id") == fact.get("source_id")), None),
            }
            for fact in facts
        ]
        return {
            "fact_pack_status": fact_pack.get("status") or "missing",
            "requires_fact_review": requires_review,
            "source_fact_ids": list(source_ids),
            "grounded_source_fact_ids": grounded_ids,
            "claim_trace": claim_trace,
            "grounded_claim_trace": grounded_trace,
            "ungrounded_claim_trace": ungrounded_trace,
            "claim_sources": claim_sources,
            "risk_report": fact_risk,
            "editorial_rule": fact_pack.get("editorial_rule"),
        }

    def build_channel_repetition_report(self, session: Session, job: Job, topic_plan: TopicPlan | None, script: Script | None) -> dict[str, Any]:
        if not topic_plan or not script:
            return {"repetition_risk": "unknown", "max_similarity": 0.0, "matches": []}
        rows = session.execute(
            select(Job.job_id, Job.topic_summary, Script.title, Script.hook, Script.ending, Script.estimated_duration_sec, Script.body_beats)
            .join(Script, Script.job_id == Job.job_id)
            .where(Job.job_id != job.job_id)
            .order_by(Job.created_at.desc())
            .limit(30)
        ).all()
        recent_rows = []
        for other_job_id, topic_summary, title, hook, ending, estimated_duration_sec, body_beats in rows:
            recent_rows.append(
                {
                    "job_id": other_job_id,
                    "topic_summary": topic_summary,
                    "title": title,
                    "hook": hook,
                    "ending": ending,
                    "estimated_duration_sec": estimated_duration_sec,
                    "body_beats": body_beats,
                }
            )
        return build_channel_repetition_report(
            current={
                "canonical_topic": topic_plan.canonical_topic,
                "angle": topic_plan.angle,
                "script": self.script_to_dict(script),
            },
            recent_rows=recent_rows,
        )

    def build_metadata_review(self, topic_plan: TopicPlan | None, script: Script | None, tags: list[str]) -> dict[str, Any]:
        weak_tags = [tag for tag in tags if tag.lower() not in {"#shorts"} and (tag.lstrip("#") in self.weak_hashtag_terms() or len(tag.lstrip("#")) < 4)]
        title = script.title if script else ""
        title_words = len(word_tokens(title))
        reasons = []
        if weak_tags:
            reasons.append("weak_hashtags")
        if not 5 <= title_words <= 14:
            reasons.append("title_length_outside_short_window")
        if len(tags) > 5:
            reasons.append("too_many_hashtags")
        suggested_tags = [tag for tag in tags if tag not in weak_tags][:5]
        if "#shorts" not in [tag.lower() for tag in suggested_tags]:
            suggested_tags.insert(0, "#shorts")
        return {
            "requires_metadata_review": bool(reasons),
            "reasons": reasons,
            "title": title[:100],
            "hashtag_count": len(tags),
            "weak_hashtags": weak_tags,
            "suggested_hashtags": list(dict.fromkeys(suggested_tags))[:5],
            "topic_keywords": word_tokens(f"{topic_plan.canonical_topic} {topic_plan.angle}")[:8] if topic_plan else [],
        }

    def manual_monetization_confirmations(self, session: Session, job_id: str) -> set[str]:
        reviews = session.scalars(select(ReviewRecord).where(ReviewRecord.job_id == job_id).order_by(ReviewRecord.created_at)).all()
        confirmations: set[str] = set()
        for review in reviews:
            confirmations.update(str(item) for item in (review.reason_codes or []) if str(item).endswith("_confirmed"))
        return confirmations

    def build_job_performance_report(self, metrics: list[PerformanceMetric]) -> dict[str, Any]:
        serialized = [
            {
                "metric_id": metric.metric_id,
                "job_id": metric.job_id,
                "created_at": metric.created_at.isoformat() if metric.created_at else None,
                "source": metric.source,
                "retention_percent": metric.retention_percent,
                "viewed_vs_swiped_away_percent": metric.viewed_vs_swiped_away_percent,
                "rewatch_rate": metric.rewatch_rate,
                "likes": metric.likes,
                "shares": metric.shares,
                "comments": metric.comments,
                "rpm_usd": metric.rpm_usd,
                "monetization_status": metric.monetization_status,
                "notes": metric.notes,
            }
            for metric in metrics
        ]
        latest = serialized[0] if serialized else None
        recommendations: list[str] = []
        if latest:
            if latest.get("retention_percent") is not None and latest["retention_percent"] < 55:
                recommendations.append("tighten_first_half_retention")
            if latest.get("viewed_vs_swiped_away_percent") is not None and latest["viewed_vs_swiped_away_percent"] < 50:
                recommendations.append("stronger_first_frame_hook")
            if latest.get("rewatch_rate") is not None and latest["rewatch_rate"] < 1.05:
                recommendations.append("improve_loop_close")
            if latest.get("monetization_status") and latest["monetization_status"] not in {"monetized", "eligible", "approved"}:
                recommendations.append("review_monetization_status")
        return {
            "schema_version": self.settings.schema_version,
            "latest": latest,
            "metrics": serialized,
            "recommendations": recommendations,
        }

    def step_publish(self, session: Session, job: Job, attempt: int) -> list[str]:
        publish_package = self.build_publish_package(session, job)
        self.storage.persist_json(job.job_id, "publish_package.json", self._serialize_for_json(publish_package))
        artifact_index = {
            "request": "request.json",
            "topic_plan": "topic_plan.json",
            "script": "script.json",
            "text_publish_audit": "text_publish_audit.json",
            "scene_plan": "scene_plan.json",
            "audio": "audio/narration.wav",
            "background_music": "audio/background_source.wav",
            "mixed_audio": "audio/mixed.wav",
            "raw_subtitles": "audio/raw.srt",
            "subtitles": "audio/subtitles.ass",
            "render": "render/final.mp4",
            "events": "events.jsonl",
            "ffmpeg_log": "render/ffmpeg.log",
            "rights_registry": "rights_registry.json",
            "ai_disclosure": "ai_disclosure.json",
            "fact_claims_report": "fact_claims_report.json",
            "channel_repetition_report": "channel_repetition_report.json",
            "metadata_review": "metadata_review.json",
            "monetization_report": "monetization_report.json",
            "publish_audit": "publish_audit.json",
            "publish_package": "publish_package.json",
            "performance_timeline": "performance_timeline.json",
        }
        if (self.storage.job_dir(job.job_id) / "audio" / "sound_design.wav").exists():
            artifact_index["sound_design"] = "audio/sound_design.wav"
            artifact_index["sound_design_report"] = "sound_design.json"
        job.artifact_index = artifact_index
        self.storage.persist_json(
            job.job_id,
            "job_manifest.json",
            {
                "schema_version": self.settings.schema_version,
                "job_id": job.job_id,
                "created_at": iso_now(),
                "content_hash": stable_hash(artifact_index),
                "artifact_index": artifact_index,
                "quality_summary": job.quality_summary or {},
            },
        )
        return ["job_manifest.json", "publish_package.json"]

    def build_publish_package(self, session: Session, job: Job) -> dict[str, Any]:
        request = session.scalar(select(TopicRequest).where(TopicRequest.job_id == job.job_id))
        script = session.scalar(select(Script).where(Script.job_id == job.job_id))
        render = session.scalar(select(RenderOutput).where(RenderOutput.job_id == job.job_id))
        subtitles = session.scalar(select(SubtitleTrack).where(SubtitleTrack.job_id == job.job_id))
        background_music = session.scalar(select(BackgroundMusicAsset).where(BackgroundMusicAsset.job_id == job.job_id))
        topic_plan = session.scalar(select(TopicPlan).where(TopicPlan.job_id == job.job_id))
        title = script.title if script else (request.seed_theme if request else job.topic_summary or job.job_id)
        fact_pack = self.read_job_json(job.job_id, "fact_pack.json")
        script_artifact = self.read_job_json(job.job_id, "script.json")
        monetization_report = self.read_job_json(job.job_id, "monetization_report.json")
        tags = self.build_publish_hashtags(topic_plan, script)
        if monetization_report.get("metadata_review", {}).get("suggested_hashtags"):
            tags = list(monetization_report["metadata_review"]["suggested_hashtags"])
        ai_notice = monetization_report.get("ai_disclosure", {}).get("description_notice")
        description = "\n".join(
            [part for part in [script.full_narration if script else title, ai_notice, "", " ".join(dict.fromkeys(tags))] if part is not None]
        )
        checklist = {
            "script_gate_pass": bool((job.quality_summary or {}).get("script", {}).get("script_quality_gate_pass")),
            "scene_plan_gate_pass": bool((job.quality_summary or {}).get("scene_plan", {}).get("scene_plan_gate_pass")),
            "asset_gate_pass": bool((job.quality_summary or {}).get("assets", {}).get("semantic_threshold_pass")),
            "subtitle_gate_pass": bool((job.quality_summary or {}).get("subtitles", {}).get("subtitle_gate_pass")),
            "render_gate_pass": bool((job.quality_summary or {}).get("render", {}).get("render_gate_pass")),
        }
        minimax_audit = self.provider_publish_audit(script_artifact, fact_pack, tags, job.job_id)
        readiness = self.publish_readiness_report(script, topic_plan, fact_pack, tags, checklist, script_artifact, minimax_audit)
        if monetization_report:
            readiness = {
                **readiness,
                "monetization_gate_pass": bool(monetization_report.get("passed")),
                "monetization_final_status": monetization_report.get("final_status"),
                "monetization_hard_blockers": monetization_report.get("hard_blockers", []),
                "monetization_manual_required": monetization_report.get("manual_required", []),
            }
        return {
            "schema_version": self.settings.schema_version,
            "job_id": job.job_id,
            "created_at": iso_now(),
            "status": "ready_for_publish" if readiness["passed"] else "needs_manual_review",
            "title": title[:100],
            "description": description[:4900],
            "hashtags": list(dict.fromkeys(tags)),
            "category": "Education",
            "language": job.language,
            "video_uri": render.video_uri if render else None,
            "poster_uri": render.poster_uri if render else None,
            "subtitle_uri": subtitles.ass_uri if subtitles else None,
            "background_music_uri": background_music.audio_uri if background_music else None,
            "mixed_audio_uri": background_music.mixed_audio_uri if background_music else None,
            "sound_design_uri": dict(background_music.provider_metadata or {}).get("sound_design", {}).get("audio_uri") if background_music else None,
            "checklist": checklist,
            "publish_readiness": readiness,
            "monetization_report": monetization_report,
            "human_review_checklist": monetization_report.get("human_review_checklist", {}) if monetization_report else {},
            "altered_or_synthetic": bool(monetization_report.get("ai_disclosure", {}).get("youtube_disclosure_required")) if monetization_report else False,
            "ai_disclosure_reason": monetization_report.get("ai_disclosure", {}).get("reason") if monetization_report else None,
            "minimax_publish_audit": minimax_audit,
            "quality_summary": job.quality_summary or {},
        }

    def provider_publish_audit(self, script_artifact: dict[str, Any], fact_pack: dict[str, Any], tags: list[str], job_id: str | None = None) -> dict[str, Any]:
        auditor = getattr(self.providers.creative, "audit_publish_package", None)
        if auditor is None:
            return {"passed": True, "reasons": [], "provider": "none", "skipped": True}
        payload = {
            "script": {
                "title": script_artifact.get("title"),
                "hook": script_artifact.get("hook"),
                "ending": script_artifact.get("ending"),
                "full_narration": script_artifact.get("full_narration"),
                "key_facts": script_artifact.get("key_facts"),
                "source_fact_ids": script_artifact.get("source_fact_ids"),
                "claim_trace": script_artifact.get("claim_trace"),
            },
            "fact_pack": fact_pack,
            "hashtags": tags,
        }
        input_hash = stable_hash(payload)
        if job_id:
            cached = self.read_job_json(job_id, "publish_audit.json")
            if cached.get("input_hash") == input_hash and isinstance(cached.get("audit"), dict):
                audit = dict(cached["audit"])
                audit["cache_hit"] = True
                return audit
        try:
            audit = auditor(payload)
        except Exception as exc:  # noqa: BLE001
            audit = {"passed": False, "reasons": ["minimax_audit_failed"], "error": str(exc), "provider": "minimax"}
        if not isinstance(audit, dict):
            audit = {"passed": False, "reasons": ["minimax_audit_invalid"], "provider": "minimax"}
        if job_id:
            self.storage.persist_json(
                job_id,
                "publish_audit.json",
                {
                    "schema_version": self.settings.schema_version,
                    "job_id": job_id,
                    "created_at": iso_now(),
                    "input_hash": input_hash,
                    "audit": self._serialize_for_json(audit),
                },
            )
        return audit

    def read_job_json(self, job_id: str, relative_path: str) -> dict[str, Any]:
        path = self.storage.job_dir(job_id) / relative_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def build_publish_hashtags(self, topic_plan: TopicPlan | None, script: Script | None) -> list[str]:
        tags = ["#shorts", "#ciencia"]
        weak = self.weak_hashtag_terms()
        text = " ".join(
            str(part or "")
            for part in [
                topic_plan.canonical_topic if topic_plan else "",
                topic_plan.angle if topic_plan else "",
                script.title if script else "",
                " ".join(script.key_facts or []) if script else "",
            ]
        )
        niche_map = {
            "flamingo": ["#flamingos", "#animais", "#biologia"],
            "flamingos": ["#flamingos", "#animais", "#biologia"],
            "torre": ["#torredepisa", "#engenharia", "#historia"],
            "pisa": ["#torredepisa", "#engenharia", "#historia"],
            "cerebro": ["#cerebro", "#neurociencia", "#percepcao"],
            "cérebro": ["#cerebro", "#neurociencia", "#percepcao"],
            "neuro": ["#neurociencia", "#cerebro", "#percepcao"],
            "polvo": ["#polvo", "#biologia", "#animais"],
            "polvos": ["#polvo", "#biologia", "#animais"],
        }
        normalized_text = self.normalize_hashtag_text(text)
        for key, mapped_tags in niche_map.items():
            if key in normalized_text:
                tags.extend(mapped_tags)
        for token in word_tokens(text):
            normalized = self.normalize_hashtag_text(token)
            if len(normalized) < 4 or normalized in weak or normalized.isdigit():
                continue
            tags.append(f"#{normalized}")
            if len(dict.fromkeys(tags)) >= 5:
                break
        return list(dict.fromkeys(tags))[:5]

    def weak_hashtag_terms(self) -> set[str]:
        return {
            "por", "que", "qual", "como", "porque", "para", "com", "uma", "uns", "umas", "tem", "têm", "fica", "ficam", "ficou", "ser", "sao", "são", "era",
            "foram", "esta", "está", "esse", "essa", "isso", "aquele", "aquela", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e", "cor", "cores",
            "video", "short", "shorts", "curiosidade", "curiosidades",
        }

    def normalize_hashtag_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", normalized)

    def publish_readiness_report(
        self,
        script: Script | None,
        topic_plan: TopicPlan | None,
        fact_pack: dict[str, Any],
        tags: list[str],
        checklist: dict[str, bool],
        script_artifact: dict[str, Any] | None = None,
        minimax_audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if not all(checklist.values()):
            reasons.append("quality_checklist_failed")
        script_dict = {**(self.script_to_dict(script) if script else {}), **(script_artifact or {})}
        source_ids = script_dict.get("source_fact_ids") or script_dict.get("qa_metrics", {}).get("source_fact_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        if fact_pack.get("status") != "verified" and source_ids:
            reasons.append("invented_source_fact_ids")
        fact_risk = self.script_gate._fact_risk_report(script_dict) if script_dict else {"blocked": False, "claim_count": 0}  # noqa: SLF001
        factual_topic = bool(topic_plan and re.search(r"\b(?:por que|porque|como|ci[eê]ncia|f[ií]sica|biologia|engenharia|hist[oó]ria|sa[uú]de|m[eé]dico|animal|animais|flamingo|torre|c[eé]rebro|neuro)\b", f"{topic_plan.canonical_topic} {topic_plan.angle}", re.IGNORECASE))
        if factual_topic and fact_pack.get("status") != "verified" and (fact_risk.get("claim_count", 0) > 0 or len(word_tokens(script_dict.get("full_narration", ""))) >= 45):
            reasons.append("fact_pack_missing_for_factual_topic")
        weak_tags = [tag for tag in tags if tag.lower() != "#shorts" and (tag.lstrip("#") in self.weak_hashtag_terms() or len(tag.lstrip("#")) < 4)]
        if weak_tags or len(tags) < 3:
            reasons.append("weak_hashtags")
        ending = str(script_dict.get("ending") or "").strip()
        narration = str(script_dict.get("full_narration") or "").strip()
        last_sentence = re.split(r"(?<=[.!?])\s+", narration)[-1] if narration else ""
        if len(word_tokens(ending or last_sentence)) < 6 or re.search(r"\b(?:que|de|da|do|para|com|e)$", (ending or last_sentence).lower().strip(" .!?")):
            reasons.append("weak_ending")
        if minimax_audit and minimax_audit.get("passed") is False:
            reasons.extend(str(reason) for reason in minimax_audit.get("reasons") or ["minimax_audit_failed"])
        if fact_pack.get("status") != "verified":
            reasons.append("manual_review_required")
        return {
            "passed": not reasons,
            "reasons": list(dict.fromkeys(reasons)),
            "fact_pack_status": fact_pack.get("status") or "missing",
            "hashtag_count": len(tags),
            "weak_hashtags": weak_tags,
            "fact_risk": fact_risk,
            "minimax_audit": minimax_audit or {"skipped": True},
        }

    def script_to_dict(self, script: Script) -> dict[str, Any]:
        return {
            "title": script.title,
            "hook": script.hook,
            "body_beats": script.body_beats,
            "ending": script.ending,
            "cta": script.cta,
            "full_narration": script.full_narration,
            "estimated_duration_sec": script.estimated_duration_sec,
            "key_facts": script.key_facts,
            "language": script.language,
            "qa_metrics": script.qa_metrics or {},
            "source_fact_ids": (script.qa_metrics or {}).get("source_fact_ids", []),
        }
