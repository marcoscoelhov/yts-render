from __future__ import annotations

import re
from typing import Any

from app.utils import stable_hash


VISUAL_CONTRACT_VERSION = "visual-contract-v1"


def build_mock_visual_contract(script: dict[str, Any], schema_version: str) -> dict[str, Any]:
    hook = _clean_text(script.get("hook") or _first_sentence(script.get("full_narration")))
    ending = _clean_text(script.get("ending") or "")
    beats = _text_list(script.get("body_beats"))
    visual_opening = script.get("visual_opening") if isinstance(script.get("visual_opening"), dict) else {}
    retention_map = script.get("retention_map") if isinstance(script.get("retention_map"), dict) else {}
    visual_subject = _clean_text(
        visual_opening.get("subject")
        or script.get("canonical_topic")
        or script.get("title")
        or "tema principal"
    )
    hook_promise = _clean_text(visual_opening.get("hook_promise") or hook)
    loop_text = _retention_text(retention_map, "proof_or_tension") or _clean_text(script.get("loop") or hook_promise)
    payoff_text = _retention_text(retention_map, "turn_or_payoff") or ending or (beats[-1] if beats else hook_promise)
    return normalize_visual_contract_payload(
        {
            "visual_thesis": f"Mostrar visualmente por que {visual_subject} muda de sentido ao final.",
            "visual_domain": "documentary realism",
            "visual_world": f"{visual_subject} em cenas concretas e legiveis para Shorts",
            "hook_frame": {
                "promise": hook_promise,
                "positive_read": hook_promise,
                "recommended_visual_intent": "deceptive_establishing",
                "must_show": [visual_subject],
                "must_hide": [],
                "negative_reads": ["imagem abstrata", "fundo generico", "texto explicativo"],
                "readability_test": "a primeira imagem deve ser compreensivel sem audio em menos de um segundo",
            },
            "loop_policy": {
                "open_question": loop_text,
                "forbidden_early_reveal": [payoff_text] if payoff_text else [],
            },
            "beat_progression": [
                {
                    "role": "escalation",
                    "source_text": beat,
                    "visual_job": f"Transformar este beat em uma evidencia visual concreta: {beat}",
                    "recommended_visual_intent": "visual_evidence",
                    "must_show": [visual_subject],
                    "must_hide": [],
                }
                for beat in beats
            ],
            "payoff_frame": {
                "reveal": payoff_text,
                "must_connect_to_hook": f"O final deve mudar a leitura do hook: {hook}",
            },
        },
        script=script,
        schema_version=schema_version,
        source_provider="mock",
    )


def normalize_visual_contract_payload(
    payload: Any,
    *,
    script: dict[str, Any],
    schema_version: str,
    source_provider: str | None = None,
) -> dict[str, Any]:
    raw = _unwrap_contract(payload)
    hook_frame = raw.get("hook_frame") if isinstance(raw.get("hook_frame"), dict) else {}
    loop_policy = raw.get("loop_policy") if isinstance(raw.get("loop_policy"), dict) else {}
    payoff_frame = raw.get("payoff_frame") if isinstance(raw.get("payoff_frame"), dict) else {}
    beat_progression = _normalize_beat_progression(raw.get("beat_progression") or raw.get("beats_visual"))
    forbidden_early_reveal = _filter_conflicting_forbidden_reveals(
        _text_list(
            loop_policy.get("forbidden_early_reveal")
            or loop_policy.get("must_hide_until_payoff")
            or loop_policy.get("revelacao_proibida_antes")
        ),
        beat_progression,
    )
    contract = {
        "schema_version": schema_version,
        "contract_version": VISUAL_CONTRACT_VERSION,
        "contract_name": "Contrato Visual do Roteiro",
        "source_provider": source_provider or _clean_text(raw.get("source_provider") or raw.get("provider")),
        "source_provider_role": _clean_text(raw.get("source_provider_role")),
        "fallback_used": bool(raw.get("visual_contract_fallback_used")),
        "fallback_reasons": _text_list(raw.get("visual_contract_fallback_reasons")),
        "source_script": {
            "content_hash": stable_hash(
                {
                    "title": script.get("title"),
                    "hook": script.get("hook"),
                    "body_beats": script.get("body_beats"),
                    "ending": script.get("ending"),
                    "full_narration": script.get("full_narration"),
                }
            ),
            "title": _clean_text(script.get("title")),
            "hook": _clean_text(script.get("hook")),
            "ending": _clean_text(script.get("ending")),
        },
        "visual_thesis": _clean_text(raw.get("visual_thesis") or raw.get("thesis") or raw.get("tese_visual")),
        "visual_domain": _clean_text(raw.get("visual_domain") or raw.get("domain") or raw.get("dominio_visual")),
        "visual_world": _clean_text(raw.get("visual_world") or raw.get("world") or raw.get("mundo_visual")),
        "hook_frame": {
            "promise": _clean_text(hook_frame.get("promise") or hook_frame.get("promessa") or hook_frame.get("visual_promise")),
            "positive_read": _clean_text(
                hook_frame.get("positive_read")
                or hook_frame.get("leitura_positiva")
                or hook_frame.get("expected_read")
            ),
            "recommended_visual_intent": _clean_text(
                hook_frame.get("recommended_visual_intent")
                or hook_frame.get("visual_intent")
                or hook_frame.get("intencao_visual_recomendada")
            ),
            "must_show": _text_list(
                hook_frame.get("must_show")
                or hook_frame.get("required_elements")
                or hook_frame.get("elementos_obrigatorios")
            ),
            "must_hide": _text_list(
                hook_frame.get("must_hide")
                or hook_frame.get("forbidden_elements")
                or hook_frame.get("elementos_proibidos")
            ),
            "negative_reads": _text_list(
                hook_frame.get("negative_reads")
                or hook_frame.get("must_not_look_like")
                or hook_frame.get("leituras_negativas")
            ),
            "readability_test": _clean_text(
                hook_frame.get("readability_test")
                or hook_frame.get("teste_de_leitura")
                or hook_frame.get("one_second_test")
            ),
        },
        "loop_policy": {
            "open_question": _clean_text(
                loop_policy.get("open_question")
                or loop_policy.get("pergunta_aberta")
                or loop_policy.get("visual_tension")
            ),
            "forbidden_early_reveal": forbidden_early_reveal,
        },
        "beat_progression": beat_progression,
        "payoff_frame": {
            "reveal": _clean_text(payoff_frame.get("reveal") or payoff_frame.get("revelacao") or payoff_frame.get("payoff")),
            "recommended_visual_intent": _clean_text(
                payoff_frame.get("recommended_visual_intent")
                or payoff_frame.get("visual_intent")
                or payoff_frame.get("intencao_visual_recomendada")
            ),
            "must_connect_to_hook": _clean_text(
                payoff_frame.get("must_connect_to_hook")
                or payoff_frame.get("connects_to_hook")
                or payoff_frame.get("conexao_com_hook")
            ),
        },
    }
    return contract


def _unwrap_contract(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("visual_contract", "contract", "contrato_visual"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def _normalize_beat_progression(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    beats: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            beats.append(
                {
                    "role": _clean_text(item.get("role") or item.get("retention_role") or f"beat_{index + 1}"),
                    "source_text": _clean_text(item.get("source_text") or item.get("text") or item.get("trecho")),
                    "visual_job": _clean_text(item.get("visual_job") or item.get("job") or item.get("funcao_visual")),
                    "recommended_visual_intent": _clean_text(
                        item.get("recommended_visual_intent")
                        or item.get("visual_intent")
                        or item.get("intencao_visual_recomendada")
                    ),
                    "must_show": _text_list(item.get("must_show") or item.get("required_elements")),
                    "must_hide": _text_list(item.get("must_hide") or item.get("forbidden_elements")),
                }
            )
        elif str(item).strip():
            text = _clean_text(item)
            beats.append(
                {
                    "role": f"beat_{index + 1}",
                    "source_text": text,
                    "visual_job": text,
                    "recommended_visual_intent": "",
                    "must_show": [],
                    "must_hide": [],
                }
            )
    return beats


def _filter_conflicting_forbidden_reveals(
    forbidden_items: list[str],
    beat_progression: list[dict[str, Any]],
) -> list[str]:
    approved_visual_text = " ".join(
        " ".join(
            [
                beat.get("source_text", ""),
                beat.get("visual_job", ""),
                beat.get("recommended_visual_intent", ""),
                " ".join(beat.get("must_show", [])),
            ]
        )
        for beat in beat_progression
    )
    approved_terms = _normalized_tokens(approved_visual_text)
    if not approved_terms:
        return forbidden_items
    filtered: list[str] = []
    for item in forbidden_items:
        item_terms = _normalized_tokens(item)
        if not item_terms:
            continue
        matches = approved_terms & item_terms
        if not _has_material_overlap(matches, item_terms):
            filtered.append(item)
    return filtered


def _normalized_tokens(value: Any) -> set[str]:
    text = _clean_text(value).lower()
    replacements = str.maketrans(
        {
            "á": "a",
            "à": "a",
            "ã": "a",
            "â": "a",
            "é": "e",
            "ê": "e",
            "í": "i",
            "ó": "o",
            "ô": "o",
            "õ": "o",
            "ú": "u",
            "ç": "c",
        }
    )
    text = text.translate(replacements)
    return {token for token in re.findall(r"[a-z0-9]+", text.replace("_", " ")) if len(token) >= 4}


def _has_material_overlap(matches: set[str], item_terms: set[str]) -> bool:
    if not matches:
        return False
    if any(len(term) >= 6 for term in matches):
        return True
    if len(item_terms) <= 2:
        return len(matches) == len(item_terms)
    return len(matches) >= 2


def _retention_text(retention_map: dict[str, Any], code: str) -> str:
    raw_segments = retention_map.get("segments")
    if isinstance(raw_segments, list):
        for segment in raw_segments:
            if isinstance(segment, dict) and segment.get("code") == code:
                return _clean_text(segment.get("mapped_text") or segment.get("text") or segment.get("goal"))
    item = retention_map.get(code)
    if isinstance(item, dict):
        return _clean_text(item.get("mapped_text") or item.get("text") or item.get("goal"))
    return _clean_text(item)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _clean_text(item))]
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
        return [part for part in parts if part]
    return []


def _first_sentence(value: Any) -> str:
    text = _clean_text(value)
    for marker in (".", "?", "!"):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    return text


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("_", " ").split())
