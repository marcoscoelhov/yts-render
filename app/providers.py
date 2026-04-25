from __future__ import annotations

import asyncio
import base64
import colorsys
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import anthropic
import httpx
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter

from app.config import get_settings
from app.utils import avg_words_per_sentence, max_words_single_sentence, sentence_split, tokenize, word_tokens, wrap_caption


VISUAL_INTENTS = [
    "subject_closeup",
    "subject_in_context",
    "process_or_mechanism",
    "comparison",
    "scale_reference",
    "historical_evocation",
    "symbolic_fallback",
]


class ProviderFailure(RuntimeError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


class MockCreativeProvider:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.angle_templates = [
            "o detalhe biologico que quase ninguem nota",
            "o mecanismo oculto que explica o fenomeno",
            "a comparacao inesperada que muda a perspectiva",
        ]

    def plan_topic(self, seed_theme: str, attempt: int, history: list[dict[str, Any]], requested_angle: str | None) -> dict[str, Any]:
        base_topic = seed_theme.strip().lower()
        angle = requested_angle or self.angle_templates[(attempt - 1) % len(self.angle_templates)]
        title_candidates = [
            f"O que torna {base_topic} tao estranho por {angle}",
            f"Por que {base_topic} desafia a intuicao quando entra em {angle}",
            f"O segredo escondido em {base_topic} e {angle}",
        ]
        return {
            "canonical_topic": base_topic,
            "angle": angle,
            "hook_promise": f"um detalhe de {base_topic} que reconfigura o jeito de olhar para o tema",
            "title_candidates": title_candidates,
            "entities": [base_topic],
            "search_terms": [base_topic, f"{base_topic} curiosidade", f"{base_topic} explicacao"],
            "quality_metrics": {
                "attempt": attempt,
                "history_checked": len(history),
                "source_provider": "mock",
            },
        }

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        subject = topic_plan["canonical_topic"]
        angle = topic_plan["angle"]
        hook = f"O detalhe menos intuitivo de {subject} muda toda a leitura."
        body = [
            f"{subject.capitalize()} parece vago so ate aparecer o efeito ao redor.",
            f"O ponto central entra em {angle} e muda a escala do tema.",
            f"Em vez do objeto isolado, o entorno entrega a pista principal.",
            f"Esse recorte deixa {subject} mais concreto para quem assiste.",
            f"Assim cada cena sustenta a ideia sem inventar elemento aleatorio.",
        ]
        ending = f"Por isso {subject} deixa de ser so um nome e vira um fenomeno legivel."
        narration_parts = [hook, *body, ending]
        full_narration = " ".join(narration_parts)
        token_count = len(tokenize(full_narration))
        estimated_duration_sec = round(max(28.5, min(41.5, len(word_tokens(full_narration)) / 2.55)), 2)
        qa_metrics = {
            "hook_score": 0.92,
            "clarity_score": 0.89,
            "information_density_score": 0.84,
            "repetition_score": 0.18,
            "ending_strength_score": 0.82,
            "estimated_duration_sec": estimated_duration_sec,
            "avg_words_per_sentence": round(avg_words_per_sentence(full_narration), 2),
            "max_words_single_sentence": max_words_single_sentence(full_narration),
            "words_per_second": round(len(word_tokens(full_narration)) / estimated_duration_sec, 2),
            "script_gate_pass": True,
            "source_provider": "mock",
        }
        return {
            "title": topic_plan["title_candidates"][0],
            "hook": hook,
            "body_beats": body,
            "ending": ending,
            "cta": None,
            "full_narration": full_narration,
            "estimated_duration_sec": estimated_duration_sec,
            "key_facts": [
                f"{subject.capitalize()} reage ao ambiente antes da maioria notar.",
                f"O tema fica mais claro quando se observa o contexto e a funcao.",
                f"O comportamento do sujeito economiza energia enquanto reduz risco.",
            ],
            "token_count": token_count,
            "language": "pt-BR",
            "qa_metrics": qa_metrics,
            "prompt_version": "mock-curiosidades-v1",
        }

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        words = word_tokens(script["full_narration"])
        total_words = len(words)
        scene_count = max(5, min(8, target_scene_count))
        chunk_size = math.ceil(total_words / scene_count)
        subject = (
            script.get("canonical_topic")
            or script.get("primary_subject")
            or script.get("topic_hint")
            or (word_tokens(script["title"])[-1] if word_tokens(script["title"]) else "tema")
        )
        scenes: list[dict[str, Any]] = []
        cursor = 0
        narration_sentences = sentence_split(script["full_narration"])
        for idx in range(scene_count):
            start = cursor
            end = min(total_words, start + chunk_size)
            if idx == scene_count - 1:
                end = total_words
            sentence = narration_sentences[min(idx, len(narration_sentences) - 1)]
            scenes.append(
                {
                    "scene_id": f"scene-{idx + 1}",
                    "order": idx + 1,
                    "narration_text": sentence,
                    "token_start": start,
                    "token_end": max(end - 1, start),
                    "estimated_duration_sec": round(script["estimated_duration_sec"] / scene_count, 2),
                    "visual_intent": VISUAL_INTENTS[idx % len(VISUAL_INTENTS)],
                    "primary_subject": subject,
                    "topic_hint": subject,
                    "image_prompt": (
                        f"ilustracao vertical cinematografica de {subject}, "
                        f"mostrando {VISUAL_INTENTS[idx % len(VISUAL_INTENTS)]}, "
                        "foco no fenomeno descrito, sem pessoas aleatorias, sem texto, sem watermark, sem colagem"
                    ),
                    "fallback_queries": [subject, f"{subject} fenomeno", f"{subject} espaco"],
                }
            )
            cursor = end
        return scenes


class MinimaxCreativeProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.minimax_api_key:
            raise ProviderFailure("minimax_text", "missing minimax api key")
        self.client = OpenAI(
            api_key=settings.minimax_api_key,
            base_url=settings.minimax_text_base_url,
        )

    def plan_topic(self, seed_theme: str, attempt: int, history: list[dict[str, Any]], requested_angle: str | None) -> dict[str, Any]:
        history_text = json.dumps(history[-8:], ensure_ascii=False)
        prompt = f"""
Você cria pautas de Shorts de curiosidades em pt-BR.
Tema base: {seed_theme}
Ângulo solicitado: {requested_angle or "auto"}
Tentativa: {attempt}
Histórico recente: {history_text}

Responda JSON estrito com:
canonical_topic, angle, hook_promise, title_candidates (3 a 5), entities, search_terms, quality_metrics.
Sem markdown.
"""
        payload = self._json_completion(prompt)
        payload["quality_metrics"] = {**payload.get("quality_metrics", {}), "source_provider": "minimax"}
        return payload

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Escreva um roteiro viral de curiosidades em pt-BR.
Entrada JSON: {json.dumps(topic_plan, ensure_ascii=False)}

Retorne JSON estrito com:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, token_count, language, qa_metrics, prompt_version

Regras:
- 25 a 45 segundos
- primeira frase com no maximo 12 palavras
- media por frase <= 14
- sem instruções de camera
- QA deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass
"""
        payload = self._json_completion(prompt)
        payload["qa_metrics"] = {**payload.get("qa_metrics", {}), "source_provider": "minimax"}
        return payload

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        prompt = f"""
Divida este roteiro em {target_scene_count} cenas renderizáveis.
Roteiro JSON: {json.dumps(script, ensure_ascii=False)}

Retorne apenas um JSON array.
Cada item precisa ter:
scene_id, order, narration_text, token_start, token_end, estimated_duration_sec, visual_intent, primary_subject, image_prompt, fallback_queries
Visual intents permitidos: {json.dumps(VISUAL_INTENTS)}
Cobertura total dos tokens.
"""
        payload = self._json_completion(prompt)
        if not isinstance(payload, list):
            raise ProviderFailure("minimax_text", "scene planner returned non-list json")
        return payload

    def _json_completion(self, prompt: str) -> Any:
        try:
            response = self.client.chat.completions.create(
                model="MiniMax-M2.7",
                messages=[
                    {"role": "system", "content": "Return valid JSON only. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure("minimax_text", str(exc)) from exc
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ProviderFailure("minimax_text", "empty text response")
        raw = self._strip_think(raw)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            extracted = self._extract_json(raw)
            if extracted is not None:
                try:
                    return json.loads(extracted)
                except Exception:
                    pass
            raise ProviderFailure("minimax_text", f"invalid json: {raw[:300]}") from exc

    def _extract_json(self, raw: str) -> str | None:
        candidates = []
        first_obj = raw.find("{")
        last_obj = raw.rfind("}")
        if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
            candidates.append(raw[first_obj : last_obj + 1])
        first_arr = raw.find("[")
        last_arr = raw.rfind("]")
        if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
            candidates.append(raw[first_arr : last_arr + 1])
        for candidate in candidates:
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                continue
        return None

    def _strip_think(self, raw: str) -> str:
        if "</think>" in raw:
            raw = raw.split("</think>", 1)[1].strip()
        return raw


class ResilientCreativeProvider:
    def __init__(self) -> None:
        self.primary = None if get_settings().use_mock_providers else MinimaxCreativeProvider()
        self.fallback = MockCreativeProvider()

    def plan_topic(self, seed_theme: str, attempt: int, history: list[dict[str, Any]], requested_angle: str | None) -> dict[str, Any]:
        if self.primary:
            try:
                return self.primary.plan_topic(seed_theme, attempt, history, requested_angle)
            except ProviderFailure as exc:
                payload = self.fallback.plan_topic(seed_theme, attempt, history, requested_angle)
                payload["quality_metrics"]["fallback_reason"] = str(exc)
                payload["quality_metrics"]["fallback_used"] = True
                return payload
        return self.fallback.plan_topic(seed_theme, attempt, history, requested_angle)

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        if self.primary:
            try:
                return self.primary.generate_script(topic_plan)
            except ProviderFailure as exc:
                payload = self.fallback.generate_script(topic_plan)
                payload["qa_metrics"]["fallback_reason"] = str(exc)
                payload["qa_metrics"]["fallback_used"] = True
                return payload
        return self.fallback.generate_script(topic_plan)

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        if self.primary:
            try:
                return self.primary.plan_scenes(script, target_scene_count)
            except ProviderFailure as exc:
                scenes = self.fallback.plan_scenes(script, target_scene_count)
                for scene in scenes:
                    scene["provider_fallback_reason"] = str(exc)
                return scenes
        return self.fallback.plan_scenes(script, target_scene_count)


class SemanticVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self.use_mock_providers = settings.use_mock_providers
        self.enabled = not settings.use_mock_providers and bool(settings.minimax_api_key)
        self.api_key = settings.minimax_api_key or ""
        self.mmx_path = shutil.which("mmx")
        self._cache: dict[str, dict[str, Any]] = {}
        self._vision_disabled_reason: str | None = None

    def score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        if self.use_mock_providers:
            return {
                "semantic_match": 0.9,
                "subject_salience": 0.88,
                "style_match": 0.84,
                "crop_fit": 0.93,
                "safety": 0.95,
                "artifact_penalty": 0.02,
                "text_or_watermark_penalty": 0.0,
                "total_score": 0.854,
            }
        heuristic = self._heuristic_score(scene, asset)
        if asset["provider"] == "local_semantic":
            return heuristic
        cache_key = f"{asset.get('uri')}::{scene.get('topic_hint') or scene.get('primary_subject')}"
        if cache_key in self._cache:
            return {**heuristic, **self._cache[cache_key]}
        if not self.enabled or not self.mmx_path:
            return self._verification_failed_score(heuristic, "vision verifier unavailable")
        if self._vision_disabled_reason:
            return self._verification_failed_score(heuristic, self._vision_disabled_reason)
        try:
            verified = self._vision_score(scene, asset)
            self._cache[cache_key] = verified
            return {**heuristic, **verified}
        except subprocess.TimeoutExpired as exc:
            self._vision_disabled_reason = f"vision verifier timed out after {exc.timeout}s"
            return self._verification_failed_score(heuristic, self._vision_disabled_reason)
        except Exception as exc:  # noqa: BLE001
            return self._verification_failed_score(heuristic, str(exc))

    def _heuristic_score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        expected_terms = self._keywords(
            " ".join(
                [
                    str(scene.get("topic_hint", "")),
                    str(scene.get("primary_subject", "")),
                    str(scene.get("narration_text", "")),
                    " ".join(scene.get("fallback_queries", [])),
                ]
            )
        )
        prompt_text = str(asset.get("prompt_snapshot", ""))
        source_text = " ".join(
            [
                str(asset.get("source_url", "")),
                str(asset.get("attribution", "")),
                str(asset.get("license_note", "")),
            ]
        )
        raw_asset_text = " ".join([prompt_text, source_text])
        asset_terms = self._keywords(raw_asset_text)
        prompt_terms = self._keywords(prompt_text)
        source_terms = self._keywords(source_text)
        overlap = len(expected_terms & asset_terms)
        coverage = overlap / max(len(expected_terms), 1)
        semantic = max(0.15, min(0.95, round(0.25 + coverage * 1.2, 3)))
        subject_salience = max(0.2, min(0.95, round(0.2 + coverage, 3)))
        if asset["provider"] == "local_semantic":
            semantic = max(semantic, 0.86)
            subject_salience = max(subject_salience, 0.84)
        style_match = 0.82 if asset["provider"] in {"ai", "minimax", "mock_ai"} else 0.74
        crop_fit = 0.93
        safety = 0.95
        artifact_penalty = 0.04 if asset["provider"] in {"ai", "minimax", "mock_ai"} else 0.02
        negative_text_constraint = any(
            phrase in prompt_text.lower()
            for phrase in [
                "sem texto",
                "sem watermark",
                "sem poster",
                "sem tipografia",
                "sem legenda",
                "no text",
                "without text",
                "no watermark",
                "no poster",
            ]
        )
        explicit_text_markers = {"poster", "post", "postit", "postits", "nota", "notas"}
        source_has_text_marker = any(token in source_terms for token in explicit_text_markers | {"texto"})
        prompt_has_text_marker = any(token in prompt_terms for token in explicit_text_markers | {"texto"})
        text_penalty = 0.2 if source_has_text_marker else 0.0
        if prompt_has_text_marker and not negative_text_constraint:
            text_penalty = 0.2
        total = (
            0.45 * semantic
            + 0.20 * subject_salience
            + 0.10 * style_match
            + 0.10 * crop_fit
            + 0.10 * safety
            - 0.15 * artifact_penalty
            - 0.20 * text_penalty
        )
        return {
            "semantic_match": round(semantic, 3),
            "subject_salience": round(subject_salience, 3),
            "style_match": round(style_match, 3),
            "crop_fit": round(crop_fit, 3),
            "safety": round(safety, 3),
            "artifact_penalty": round(artifact_penalty, 3),
            "text_or_watermark_penalty": round(text_penalty, 3),
            "total_score": round(total, 3),
        }

    def _verification_failed_score(self, heuristic: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            **heuristic,
            "semantic_match": min(heuristic["semantic_match"], 0.45),
            "subject_salience": min(heuristic["subject_salience"], 0.45),
            "total_score": min(heuristic["total_score"], 0.45),
            "verification_fallback_reason": reason,
        }

    def _vision_score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        asset_path = Path(asset["uri"][7:]) if str(asset.get("uri", "")).startswith("file://") else Path(asset["uri"])
        prompt = (
            "Avalie se esta imagem esta semanticamente alinhada com a cena de um video curto. "
            "Responda JSON estrito com keys description, aligned_boolean, alignment_score_0_to_1, "
            "subject_visibility_score_0_to_1, style_match_score_0_to_1, text_or_watermark_penalty_0_to_1, "
            "artifact_penalty_0_to_1, reasons. "
            f"Tema esperado: {scene.get('topic_hint') or scene.get('primary_subject')}. "
            f"Narracao da cena: {scene.get('narration_text')}. "
            f"Prompt de imagem esperado: {scene.get('image_prompt')}."
        )
        result = subprocess.run(
            [
                self.mmx_path,
                "--api-key",
                self.api_key,
                "--region",
                "global",
                "--output",
                "json",
                "--non-interactive",
                "vision",
                "describe",
                "--image",
                str(asset_path),
                "--prompt",
                prompt,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=25,
        )
        raw_stdout = result.stdout.strip()
        first_json = raw_stdout.find("{")
        if first_json == -1:
            raise ProviderFailure("minimax_vision", f"invalid vision output: {raw_stdout[:200]}")
        payload = json.loads(raw_stdout[first_json:])
        content = str(payload.get("content", "")).strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(content)
        semantic = float(data.get("alignment_score_0_to_1", 0.0))
        subject_salience = float(data.get("subject_visibility_score_0_to_1", semantic))
        style_match = float(data.get("style_match_score_0_to_1", 0.8))
        text_penalty = float(data.get("text_or_watermark_penalty_0_to_1", 0.0))
        artifact_penalty = float(data.get("artifact_penalty_0_to_1", 0.03))
        crop_fit = 0.93
        safety = 0.95
        total = (
            0.45 * semantic
            + 0.20 * subject_salience
            + 0.10 * style_match
            + 0.10 * crop_fit
            + 0.10 * safety
            - 0.15 * artifact_penalty
            - 0.20 * text_penalty
        )
        return {
            "semantic_match": round(semantic, 3),
            "subject_salience": round(subject_salience, 3),
            "style_match": round(style_match, 3),
            "crop_fit": round(crop_fit, 3),
            "safety": round(safety, 3),
            "artifact_penalty": round(artifact_penalty, 3),
            "text_or_watermark_penalty": round(text_penalty, 3),
            "total_score": round(total, 3),
            "vision_description": data.get("description"),
            "vision_reasons": data.get("reasons"),
            "vision_aligned": bool(data.get("aligned_boolean")),
        }

    def _keywords(self, text: str) -> set[str]:
        stopwords = {
            "a",
            "ao",
            "as",
            "com",
            "da",
            "de",
            "do",
            "dos",
            "e",
            "em",
            "no",
            "nos",
            "na",
            "nas",
            "o",
            "os",
            "ou",
            "para",
            "por",
            "sem",
            "um",
            "uma",
        }
        normalized = []
        for token in word_tokens(text.lower()):
            token = "".join(ch for ch in token if ch.isalnum())
            if len(token) >= 3 and token not in stopwords:
                normalized.append(token)
        return set(normalized)


class MockImageProvider:
    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        self._render_scene_image(scene, output_path, variant="ai")
        return {
            "provider": "mock_ai",
            "width": 1024,
            "height": 1536,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
        }

    def _render_scene_image(self, scene: dict[str, Any], output_path: Path, variant: str) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        hue_seed = ((scene["order"] * 53) % 360) / 360.0
        sat = 0.55 if variant == "ai" else 0.30
        val = 0.88 if variant == "ai" else 0.76
        rgb = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb(hue_seed, sat, val))
        rgb2 = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb((hue_seed + 0.12) % 1.0, sat * 0.8, val * 0.65))
        image = Image.new("RGB", (1024, 1536), rgb)
        draw = ImageDraw.Draw(image, "RGBA")
        for idx in range(6):
            width = 880 - idx * 90
            height = 1280 - idx * 120
            x0 = (1024 - width) // 2 + idx * 8
            y0 = 120 + idx * 25
            x1 = x0 + width
            y1 = y0 + height
            alpha = 45 + idx * 18
            fill = rgb2 + (alpha,)
            draw.rounded_rectangle((x0, y0, x1, y1), radius=90, fill=fill)
        draw.ellipse((250, 260, 774, 980), fill=(255, 255, 255, 36))
        draw.rounded_rectangle((180, 920, 844, 1270), radius=120, fill=(0, 0, 0, 32))
        image = image.filter(ImageFilter.GaussianBlur(radius=0.5))
        image.save(output_path, format="PNG")


class LocalSemanticImageProvider:
    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        subject = str(scene.get("topic_hint") or scene.get("primary_subject") or "fenomeno").lower()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if ("buraco" in subject and "negro" in subject) or "black hole" in subject:
            self._render_black_hole(output_path, int(scene.get("order", 1)))
        else:
            self._render_scientific_symbol(output_path, subject, int(scene.get("order", 1)))
        with Image.open(output_path) as image:
            width, height = image.size
        return {
            "provider": "local_semantic",
            "width": width,
            "height": height,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "license_note": "Local semantic fallback generated by the app.",
        }

    def _render_black_hole(self, output_path: Path, order: int) -> None:
        image = Image.new("RGB", (1024, 1536), (2, 4, 16))
        draw = ImageDraw.Draw(image, "RGBA")
        shift_x = ((order % 3) - 1) * 80
        shift_y = ((order % 4) - 1) * 45
        palette = [
            (255, 207, 103),
            (90, 196, 255),
            (255, 139, 60),
            (121, 204, 255),
        ]
        hot = palette[order % len(palette)]
        cool = palette[(order + 1) % len(palette)]
        for idx in range(220):
            x = (idx * 173 + order * 41) % 1024
            y = (idx * 97 + order * 67) % 1536
            alpha = 80 + (idx % 120)
            draw.ellipse((x, y, x + 2, y + 2), fill=(210, 230, 255, alpha))
        cx, cy = 512 + shift_x, 720 + shift_y
        disk_flattening = 2 + (order % 3)
        for radius, color in [
            (470, (26, 67, 125, 28)),
            (390, (*cool, 42)),
            (315, (*hot, 72)),
            (250, (255, 207, 103, 112)),
            (190, (121, 204, 255, 92)),
        ]:
            bbox = (cx - radius, cy - radius // disk_flattening, cx + radius, cy + radius // disk_flattening)
            draw.ellipse(bbox, outline=color, width=max(8, radius // 28))
        if order % 3 == 1:
            draw.polygon([(0, 980), (330, 880), (420, 1536), (0, 1536)], fill=(14, 25, 55, 170))
            draw.arc((cx - 430, cy - 330, cx + 430, cy + 330), 205, 340, fill=(*cool, 135), width=22)
        elif order % 3 == 2:
            draw.ellipse((730, 1010, 970, 1250), fill=(16, 31, 70, 210), outline=(*cool, 110), width=10)
            draw.arc((cx - 470, cy - 280, cx + 470, cy + 280), 0, 175, fill=(*hot, 135), width=20)
        else:
            for band in range(5):
                y = 1040 + band * 62
                draw.line((70, y, 954, y - 130), fill=(*cool, 34 + band * 18), width=5)
            draw.arc((cx - 360, cy - 250, cx + 360, cy + 250), 188, 352, fill=(*cool, 120), width=16)
        draw.ellipse((cx - 178, cy - 178, cx + 178, cy + 178), fill=(0, 0, 4, 255))
        draw.ellipse((cx - 220, cy - 220, cx + 220, cy + 220), outline=(*hot, 96), width=18)
        draw.arc((cx - 420, cy - 300, cx + 420, cy + 300), 10, 170, fill=(*hot, 115), width=18)
        image = image.filter(ImageFilter.GaussianBlur(radius=0.4))
        image.save(output_path, format="PNG")

    def _render_scientific_symbol(self, output_path: Path, subject: str, order: int) -> None:
        hue = ((len(subject) * 31 + order * 47) % 360) / 360
        bg = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.55, 0.16))
        accent = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.65, 0.92))
        image = Image.new("RGB", (1024, 1536), bg)
        draw = ImageDraw.Draw(image, "RGBA")
        for idx in range(9):
            pad = 110 + idx * 42
            alpha = 130 - idx * 10
            draw.ellipse((pad, 300 + idx * 24, 1024 - pad, 1220 - idx * 24), outline=accent + (alpha,), width=10)
        draw.ellipse((380, 620, 644, 884), fill=(255, 255, 255, 42), outline=accent + (180,), width=12)
        image = image.filter(ImageFilter.GaussianBlur(radius=0.35))
        image.save(output_path, format="PNG")


class MinimaxImageProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.minimax_api_key:
            raise ProviderFailure("minimax_image", "missing minimax api key")
        self.url = settings.minimax_image_base_url
        self.key = settings.minimax_api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        response = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": "image-01",
                "prompt": scene["image_prompt"],
                "aspect_ratio": "2:3",
                "response_format": "base64",
            },
            timeout=httpx.Timeout(35.0, connect=10.0),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("base_resp", {}).get("status_code", 0) not in (0, None):
            raise ProviderFailure("minimax_image", payload["base_resp"].get("status_msg", "minimax image error"))
        image_base64 = payload["data"]["image_base64"][0]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(image_base64))
        with Image.open(output_path) as image:
            width, height = image.size
        return {
            "provider": "minimax",
            "width": width,
            "height": height,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
        }


class PexelsStockProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.pexels_api_key:
            raise ProviderFailure("pexels", "missing pexels api key")
        self.api_key = settings.pexels_api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        query = scene["fallback_queries"][0]
        response = httpx.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": self.api_key},
            params={"query": query, "orientation": "portrait", "locale": "pt-BR", "per_page": 1},
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        response.raise_for_status()
        photos = response.json().get("photos", [])
        if not photos:
            raise ProviderFailure("pexels", f"no portrait result for query {query}")
        photo = photos[0]
        image_response = httpx.get(photo["src"]["large2x"], timeout=httpx.Timeout(30.0, connect=8.0))
        image_response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_response.content)
        return {
            "provider": "pexels",
            "width": photo["width"],
            "height": photo["height"],
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "source_url": photo["url"],
            "attribution": f"Pexels photo by {photo['photographer']}",
            "license_note": "Exibir link proeminente para o Pexels.",
        }


class PixabayStockProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.pixabay_api_key:
            raise ProviderFailure("pixabay", "missing pixabay api key")
        self.api_key = settings.pixabay_api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        query = scene["fallback_queries"][0]
        response = httpx.get(
            "https://pixabay.com/api/",
            params={"key": self.api_key, "q": query, "image_type": "photo", "orientation": "vertical", "per_page": 1},
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        response.raise_for_status()
        hits = response.json().get("hits", [])
        if not hits:
            raise ProviderFailure("pixabay", f"no vertical result for query {query}")
        hit = hits[0]
        image_response = httpx.get(hit["largeImageURL"], timeout=httpx.Timeout(30.0, connect=8.0))
        image_response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_response.content)
        return {
            "provider": "pixabay",
            "width": hit["imageWidth"],
            "height": hit["imageHeight"],
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "source_url": hit["pageURL"],
            "attribution": f"Pixabay photo by {hit['user']}",
            "license_note": "Fallback secundario via Pixabay.",
        }


class ResilientStockProvider:
    def __init__(self) -> None:
        self.providers: list[Any] = []
        settings = get_settings()
        if settings.pexels_api_key:
            self.providers.append(PexelsStockProvider())
        if settings.pixabay_api_key:
            self.providers.append(PixabayStockProvider())
        self.providers.append(MockImageProvider())

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        last_error = "stock failed"
        for provider in self.providers:
            try:
                return provider.generate(scene, output_path)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        raise ProviderFailure("stock", last_error)

    def generate_candidates(self, scene: dict[str, Any], output_dir: Path, max_candidates: int = 4) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        queries = [query for query in scene.get("fallback_queries", []) if query and query not in seen_queries]
        for query in queries:
            seen_queries.add(query)
            query_scene = {**scene, "fallback_queries": [query]}
            for provider in self.providers:
                if isinstance(provider, MockImageProvider):
                    continue
                if len(candidates) >= max_candidates:
                    return candidates
                provider_name = provider.__class__.__name__.replace("StockProvider", "").replace("ImageProvider", "").lower()
                output_path = output_dir / f"stock-{len(candidates) + 1}-{provider_name}.png"
                try:
                    candidates.append(provider.generate(query_scene, output_path))
                except Exception:
                    continue
        if candidates:
            return candidates
        return []


class LocalSpeechFallbackProvider:
    voice = "pt-BR-FranciscaNeural"

    def synthesize(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_speech_audio(text, audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        cues = self._build_cues(text, duration_ms)
        srt_path.write_text(self._render_srt(cues), encoding="utf-8")
        return {
            "provider": "espeak_ng",
            "voice": self.voice,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {"mode": "espeak_ng", "cue_count": len(cues), "fallback_used": True},
        }

    def _build_cues(self, text: str, duration_ms: int) -> list[dict[str, Any]]:
        words = text.split()
        chunks: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if len(candidate) > 42 or len(current) >= 7:
                chunks.append(" ".join(current))
                current = [word]
                continue
            current.append(word)
        if current:
            chunks.append(" ".join(current))
        if not chunks:
            chunks = [text]
        cue_duration = max(800, min(2500, duration_ms // len(chunks)))
        cues: list[dict[str, Any]] = []
        start = 0
        for idx, chunk in enumerate(chunks, start=1):
            end = duration_ms if idx == len(chunks) else min(duration_ms, start + cue_duration)
            cues.append({"idx": idx, "start_ms": start, "end_ms": end, "text": wrap_caption(chunk)})
            start = end
        return cues

    def _write_speech_audio(self, text: str, path: Path) -> None:
        raw_path = path.with_name(path.stem + ".raw.wav")
        subprocess.run(
            [
                "espeak-ng",
                "-v",
                "pt-br",
                "-s",
                "160",
                "-p",
                "40",
                "-w",
                str(raw_path),
                text,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_path),
                "-af",
                "highpass=f=120,lowpass=f=4300,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        raw_path.unlink(missing_ok=True)

    def _measure_audio_ms(self, path: Path) -> int:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        seconds = float(result.stdout.strip() or "0")
        return int(seconds * 1000)

    def _render_srt(self, cues: list[dict[str, Any]]) -> str:
        blocks = []
        for cue in cues:
            start = self._ms_to_srt(cue["start_ms"])
            end = self._ms_to_srt(cue["end_ms"])
            blocks.append(f"{cue['idx']}\n{start} --> {end}\n{cue['text']}")
        return "\n\n".join(blocks) + "\n"

    def _ms_to_srt(self, value: int) -> str:
        hours, rem = divmod(value, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


class EdgeTTSProvider(LocalSpeechFallbackProvider):
    async def _run(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        import edge_tts

        communicate = edge_tts.Communicate(text=text, voice=self.voice, connect_timeout=20, receive_timeout=120)
        submaker = edge_tts.SubMaker()
        temp_audio_path = Path(tempfile.mkstemp(suffix=".mp3")[1])
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_audio_path, "wb") as audio_file:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] in {"WordBoundary", "SentenceBoundary"}:
                    submaker.feed(chunk)
        self._normalize_edge_audio(temp_audio_path, audio_path)
        temp_audio_path.unlink(missing_ok=True)
        srt_path.write_text(submaker.get_srt(), encoding="utf-8")
        duration_ms = self._measure_audio_ms(audio_path)
        return {
            "provider": "edge_tts",
            "voice": self.voice,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {"mode": "edge", "fallback_used": False},
        }

    def synthesize(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        try:
            return asyncio.run(self._run(text, audio_path, srt_path))
        except Exception as exc:  # noqa: BLE001
            fallback = super().synthesize(text, audio_path, srt_path)
            fallback["provider_metadata"]["fallback_reason"] = str(exc)
            return fallback

    def _normalize_edge_audio(self, source_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-af",
                "highpass=f=80,lowpass=f=12000,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


class ProviderRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self.creative = ResilientCreativeProvider()
        self.image = MockImageProvider() if settings.use_mock_providers else MinimaxImageProvider()
        self.stock = ResilientStockProvider()
        self.tts = LocalSpeechFallbackProvider() if settings.use_mock_providers else EdgeTTSProvider()
        self.semantic = SemanticVerifier()
        self.local_image = LocalSemanticImageProvider()
