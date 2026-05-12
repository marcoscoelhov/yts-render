from __future__ import annotations

from typing import Any

from app.utils import stable_hash, word_tokens


EDITORIAL_PROMPT_VERSION = "shorts-retention-v3"

GOLDEN_SCRIPT_SAMPLES = {
    "good": [
        {
            "label": "visual_first_frame",
            "pattern": "abre com resultado visual concreto, segura a explicação e fecha voltando ao primeiro contraste com replay mental",
        },
        {
            "label": "single_idea_payoff",
            "pattern": "uma ideia central, beats em escalada e payoff só no último terço",
        },
    ],
    "bad": [
        {
            "label": "generic_ai_short",
            "pattern": "começa com você sabia, lista fatos soltos e termina sem recontextualizar o começo",
        },
        {
            "label": "unsupported_precision",
            "pattern": "usa clickbait não provado, precisão falsa ou causalidade forte sem base para parecer mais viral",
        },
    ],
}


def build_retention_map(target_duration_sec: int) -> dict[str, Any]:
    duration = max(35, min(55, int(target_duration_sec or 45)))
    return {
        "prompt_version": EDITORIAL_PROMPT_VERSION,
        "target_duration_sec": duration,
        "segments": [
            {
                "code": "visual_hook",
                "start_sec": 0,
                "end_sec": 2,
                "goal": "mostrar resultado, contraste ou tensão visual antes de explicar",
            },
            {
                "code": "proof_or_tension",
                "start_sec": 2,
                "end_sec": min(7, duration),
                "goal": "provar que a promessa é concreta e reduzir swipe",
            },
            {
                "code": "escalation",
                "start_sec": 7,
                "end_sec": min(20, duration),
                "goal": "entregar micro-recompensas com fatos visuais progressivos",
            },
            {
                "code": "turn_or_payoff",
                "start_sec": min(20, duration),
                "end_sec": max(min(duration - 3, 34), min(20, duration)),
                "goal": "revelar a virada principal sem inflar fato não verificado",
            },
            {
                "code": "loop_close",
                "start_sec": max(duration - 3, 0),
                "end_sec": duration,
                "goal": "fechar o loop conectando final ao primeiro hook",
            },
        ],
        "rules": [
            "uma ideia central por short",
            "primeiro frame precisa ser visualmente legível sem contexto",
            "cada beat deve subir estranheza, imagem mental ou impacto",
            "loop fica aberto até o payoff no último terço",
            "final deve recontextualizar o começo e provocar replay sem parecer template repetido",
        ],
    }


def build_visual_opening_brief(topic_plan: dict[str, Any]) -> dict[str, Any]:
    subject = str(topic_plan.get("canonical_topic") or topic_plan.get("original_input") or "tema").strip()
    angle = str(topic_plan.get("angle") or "").strip()
    hook_promise = str(topic_plan.get("hook_promise") or "").strip()
    return {
        "first_frame_goal": "começar com resultado, movimento ou contraste concreto",
        "subject": subject,
        "angle": angle,
        "hook_promise": hook_promise,
        "avoid": [
            "intro verbal genérica",
            "texto explicativo como primeira imagem",
            "imagem abstrata sem sujeito reconhecível",
            "slide ou infográfico com palavras",
        ],
    }


def build_recent_pattern_brief(history: list[dict[str, Any]]) -> dict[str, Any]:
    recent = history[-12:]
    hooks = [str(item.get("hook") or "").strip() for item in recent if str(item.get("hook") or "").strip()]
    titles = [str(item.get("title") or "").strip() for item in recent if str(item.get("title") or "").strip()]
    openings = [" ".join(word_tokens(hook)[:5]) for hook in hooks]
    return {
        "recent_count": len(recent),
        "avoid_hook_openings": list(dict.fromkeys(opening for opening in openings if opening))[:8],
        "avoid_title_openings": list(dict.fromkeys(" ".join(word_tokens(title)[:5]) for title in titles if title))[:8],
        "history_hash": stable_hash({"hooks": hooks[:12], "titles": titles[:12]}),
    }


def build_golden_sample_brief() -> dict[str, Any]:
    return {
        "prompt_version": EDITORIAL_PROMPT_VERSION,
        "use_good_patterns": GOLDEN_SCRIPT_SAMPLES["good"],
        "avoid_bad_patterns": GOLDEN_SCRIPT_SAMPLES["bad"],
    }


def enrich_plan_for_script_generation(
    plan_dict: dict[str, Any],
    target_duration_sec: int,
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    enriched = dict(plan_dict)
    enriched["editorial_prompt_version"] = EDITORIAL_PROMPT_VERSION
    enriched["retention_map"] = build_retention_map(target_duration_sec)
    enriched["visual_opening"] = build_visual_opening_brief(enriched)
    enriched["recent_pattern_brief"] = build_recent_pattern_brief(recent_history or [])
    enriched["golden_sample_brief"] = build_golden_sample_brief()
    return enriched


def attach_retention_metadata(script: dict[str, Any], plan_dict: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(script)
    metrics = dict(enriched.get("qa_metrics") or {})
    metrics["editorial_prompt_version"] = plan_dict.get("editorial_prompt_version") or EDITORIAL_PROMPT_VERSION
    metrics["retention_map_hash"] = stable_hash(plan_dict.get("retention_map") or {})
    metrics["visual_opening_hash"] = stable_hash(plan_dict.get("visual_opening") or {})
    metrics["recent_pattern_hash"] = stable_hash(plan_dict.get("recent_pattern_brief") or {})
    metrics["golden_sample_hash"] = stable_hash(plan_dict.get("golden_sample_brief") or {})
    enriched["qa_metrics"] = metrics
    enriched.setdefault("retention_map", plan_dict.get("retention_map") or build_retention_map(int(enriched.get("estimated_duration_sec") or 45)))
    enriched.setdefault("visual_opening", plan_dict.get("visual_opening") or build_visual_opening_brief(plan_dict))
    enriched["prompt_version"] = str(enriched.get("prompt_version") or plan_dict.get("editorial_prompt_version") or EDITORIAL_PROMPT_VERSION)
    return enriched
