from __future__ import annotations

import base64
import colorsys
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter

from app.config import get_settings
from app.providers.errors import ProviderFailure
from app.utils import word_tokens


class SemanticVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self.use_mock_providers = settings.use_mock_providers
        text_api_key = settings.resolved_minimax_text_api_key
        self.enabled = not settings.use_mock_providers and bool(text_api_key)
        self.api_key = text_api_key or ""
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
                    str(scene.get("image_prompt", "")),
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
            "verification_fallback_reason": reason,
            "verification_mode": "prompt_heuristic",
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
        primary_key = settings.resolved_minimax_text_api_key
        dedicated_key = getattr(settings, "minimax_image_api_key", None)
        if not primary_key and settings.resolved_minimax_image_api_key:
            dedicated_key = settings.resolved_minimax_image_api_key
        if not primary_key and not dedicated_key:
            raise ProviderFailure("minimax_image", "missing minimax text or image api key")
        self.url = settings.minimax_image_base_url
        self.primary_key = primary_key
        self.dedicated_key = dedicated_key if dedicated_key != primary_key else None
        self.key = primary_key or dedicated_key
        self._exhausted_primary_jobs: set[str] = set()
        self._key_lock = threading.Lock()

    def begin_job(self, job_id: str) -> None:
        with self._key_lock:
            self._exhausted_primary_jobs.discard(job_id)

    def _primary_exhausted_for_job(self, job_id: str | None) -> bool:
        return bool(job_id and job_id in self._exhausted_primary_jobs)

    def _mark_primary_exhausted(self, job_id: str | None) -> None:
        if not job_id:
            return
        with self._key_lock:
            self._exhausted_primary_jobs.add(job_id)

    def _key_attempts(self, job_id: str | None, *, skip_primary: bool = False) -> list[tuple[str, str]]:
        attempts: list[tuple[str, str]] = []
        if self.primary_key and not skip_primary and not self._primary_exhausted_for_job(job_id):
            attempts.append(("text_primary", self.primary_key))
        if self.dedicated_key:
            attempts.append(("image_dedicated", self.dedicated_key))
        return attempts

    def _is_provider_limit_error(self, response: httpx.Response | None, message: str) -> bool:
        if response is not None and response.status_code == 429:
            return True
        normalized = message.lower()
        return any(
            marker in normalized
            for marker in [
                "quota",
                "rate limit",
                "rate_limit",
                "too many requests",
                "insufficient",
                "balance",
                "credit",
                "credits",
                "limit exceeded",
                "exceed limit",
                "exceeded limit",
            ]
        )

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        job_id = str(scene.get("job_id") or "").strip() or None
        key_attempts = self._key_attempts(job_id, skip_primary=bool(scene.get("_skip_text_primary")))
        if not key_attempts:
            raise ProviderFailure("minimax_image", "missing available minimax image credential")
        last_error: Exception | None = None
        credential_role = key_attempts[-1][0]
        fallback_from_text_key = False
        exhausted_text_key = self._primary_exhausted_for_job(job_id)
        for credential_role, api_key in key_attempts:
            response: httpx.Response | None = None
            try:
                for attempt in range(1, 4):
                    try:
                        response = httpx.post(
                            self.url,
                            headers={"Authorization": f"Bearer {api_key}"},
                            json={
                                "model": "image-01",
                                "prompt": scene["image_prompt"],
                                "aspect_ratio": "2:3",
                                "response_format": "base64",
                            },
                            timeout=httpx.Timeout(45.0, connect=15.0),
                        )
                        response.raise_for_status()
                        break
                    except httpx.HTTPStatusError as exc:
                        response = exc.response
                        message = exc.response.text or str(exc)
                        if credential_role == "text_primary" and self._is_provider_limit_error(exc.response, message) and self.dedicated_key:
                            self._mark_primary_exhausted(job_id)
                            fallback_from_text_key = True
                            exhausted_text_key = True
                            last_error = ProviderFailure(
                                "minimax_image",
                                "minimax text key reached provider limit for image generation",
                                {"credential_role": credential_role, "status_code": exc.response.status_code},
                            )
                            break
                        raise
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                        last_error = exc
                        if attempt == 3:
                            raise ProviderFailure("minimax_image", f"connection failed after {attempt} attempts: {exc}") from exc
                        time.sleep(1.5 * attempt)
                else:
                    raise ProviderFailure("minimax_image", f"connection failed: {last_error}")
                if response is None or response.is_error:
                    continue
                break
            except httpx.HTTPStatusError:
                raise
        else:
            raise ProviderFailure("minimax_image", f"connection failed: {last_error}")
        payload = response.json()
        if payload.get("base_resp", {}).get("status_code", 0) not in (0, None):
            status_msg = payload["base_resp"].get("status_msg", "minimax image error")
            if credential_role == "text_primary" and self._is_provider_limit_error(response, status_msg) and self.dedicated_key:
                self._mark_primary_exhausted(job_id)
                fallback_scene = dict(scene)
                fallback_scene["job_id"] = job_id
                fallback_scene["_skip_text_primary"] = True
                fallback_result = self.generate(fallback_scene, output_path)
                fallback_metadata = dict(fallback_result.get("provider_metadata") or {})
                fallback_metadata.update(
                    {
                        "credential_role": "image_dedicated",
                        "fallback_from_text_key": True,
                        "text_key_exhausted_for_job": True,
                        "fallback_reason": "provider_limit",
                    }
                )
                fallback_result["provider_metadata"] = fallback_metadata
                return fallback_result
            raise ProviderFailure("minimax_image", status_msg)
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
            "provider_metadata": {
                "credential_role": credential_role,
                "fallback_from_text_key": fallback_from_text_key,
                "text_key_exhausted_for_job": exhausted_text_key,
            },
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
