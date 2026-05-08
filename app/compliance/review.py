from __future__ import annotations

from typing import Any


def build_human_review_checklist(
    rights_registry: dict[str, Any],
    ai_disclosure: dict[str, Any],
    fact_claims_report: dict[str, Any],
    metadata_review: dict[str, Any],
    channel_repetition_report: dict[str, Any],
    confirmations: set[str],
) -> dict[str, Any]:
    items = [
        {
            "code": "rights_confirmation_required",
            "confirmation_code": "rights_confirmed",
            "label": "Direitos comerciais confirmados",
            "required": not rights_registry.get("all_commercial_rights_confirmed", False),
            "completed": "rights_confirmed" in confirmations,
            "source": "rights_registry",
        },
        {
            "code": "youtube_ai_disclosure_toggle_required",
            "confirmation_code": "ai_disclosure_confirmed",
            "label": "Disclosure de IA marcado no YouTube",
            "required": bool(ai_disclosure.get("youtube_disclosure_required")),
            "completed": bool(ai_disclosure.get("auto_confirmed")) or "ai_disclosure_confirmed" in confirmations,
            "auto_completed": bool(ai_disclosure.get("auto_confirmed")),
            "source": "ai_disclosure",
        },
        {
            "code": "fact_review_required",
            "confirmation_code": "fact_review_confirmed",
            "label": "Fatos e fontes revisados",
            "required": bool(fact_claims_report.get("requires_fact_review")),
            "completed": "fact_review_confirmed" in confirmations,
            "source": "fact_claims_report",
        },
        {
            "code": "metadata_review_required",
            "confirmation_code": "metadata_confirmed",
            "label": "Título, descrição e hashtags revisados",
            "required": bool(metadata_review.get("requires_metadata_review")),
            "completed": "metadata_confirmed" in confirmations,
            "source": "metadata_review",
        },
        {
            "code": "originality_review_required",
            "confirmation_code": "originality_confirmed",
            "label": "Originalidade em relação ao canal confirmada",
            "required": channel_repetition_report.get("repetition_risk") in {"medium", "high"},
            "completed": "originality_confirmed" in confirmations,
            "source": "channel_repetition_report",
        },
    ]
    required_codes = [item["code"] for item in items if item["required"]]
    completed_codes = [item["code"] for item in items if item["required"] and item["completed"]]
    pending_codes = [item["code"] for item in items if item["required"] and not item["completed"]]
    return {
        "items": items,
        "required_codes": required_codes,
        "completed_codes": completed_codes,
        "pending_codes": pending_codes,
        "all_required_completed": not pending_codes,
    }
