from __future__ import annotations

import binascii
import json
import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from app.config import get_settings
from app.music_bank import populate_builtin_music_bank
from app.providers.errors import ProviderFailure
from app.utils import word_tokens


class MockBackgroundMusicProvider:
    provider_name = "mock_music"

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mood = self._infer_mood(topic_plan, script)
        query = self._query_hint(topic_plan, script, mood)
        self._write_mock_music(output_path, target_duration_ms, mood, query)
        return {
            "provider": self.provider_name,
            "query": query,
            "mood": mood,
            "source_url": None,
            "attribution": "Mock background bed generated locally for tests.",
            "license_note": "local_mock_background_music",
            "audio_uri": output_path.resolve().as_uri(),
            "duration_ms": target_duration_ms,
            "provider_metadata": {
                "fallback_used": True,
                "selection_mode": "generated",
            },
        }

    def _infer_mood(self, topic_plan: dict[str, Any], script: dict[str, Any]) -> str:
        surface = " ".join(
            [
                str(topic_plan.get("canonical_topic") or ""),
                str(topic_plan.get("angle") or ""),
                str(script.get("title") or ""),
                str(script.get("hook") or ""),
            ]
        ).lower()
        if any(term in surface for term in ["mist", "crime", "suspense", "mistério", "misterio", "segredo", "sombr"]):
            return "suspense"
        if any(term in surface for term in ["espaço", "espaco", "universo", "buraco negro", "tecnologia", "cafeína", "cafeina"]):
            return "technology"
        if any(term in surface for term in ["animal", "gato", "polvo", "oceano", "natureza", "flamingo"]):
            return "documentary"
        return "cinematic"

    def _query_hint(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str) -> str:
        topic = str(topic_plan.get("canonical_topic") or script.get("title") or "curiosidades").strip()
        return f"{topic} {mood}".strip()

    def _write_mock_music(self, output_path: Path, target_duration_ms: int, mood: str, seed_text: str) -> None:
        sample_rate = 24_000
        frame_count = max(1, round(sample_rate * target_duration_ms / 1000))
        base_freq = {
            "suspense": 92.5,
            "technology": 110.0,
            "documentary": 146.8,
            "cinematic": 130.8,
        }.get(mood, 123.5)
        phase_offset = (sum(ord(char) for char in seed_text) % 360) * math.pi / 180
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for idx in range(frame_count):
                t = idx / sample_rate
                bed = (
                    0.55 * math.sin(2 * math.pi * base_freq * t + phase_offset)
                    + 0.28 * math.sin(2 * math.pi * (base_freq * 1.5) * t)
                    + 0.17 * math.sin(2 * math.pi * (base_freq * 2.0) * t + phase_offset / 2)
                )
                pulse = 0.65 + 0.35 * math.sin(2 * math.pi * 0.22 * t)
                fade_in = min(1.0, t / 1.5)
                fade_out = min(1.0, max(0.0, (frame_count / sample_rate - t) / 1.2))
                envelope = pulse * fade_in * fade_out
                sample = int(1400 * envelope * bed)
                frames.extend(sample.to_bytes(2, "little", signed=True))
            wav_file.writeframes(frames)


class LocalMusicBankProvider:
    provider_name = "local_music_bank"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.bank_dir = Path(self.settings.music_bank_dir)
        self.manifest_path = self.bank_dir / "manifest.json"

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        mood = self._infer_mood(topic_plan, script)
        query = self._query_hint(topic_plan, script, mood)
        tracks = self._load_manifest()
        track = self._select_approved_track(tracks, mood, query)
        source_path = self._resolve_bank_path(track.get("path"))
        if not source_path.exists():
            raise ProviderFailure(
                self.provider_name,
                f"approved music bank track is missing: {source_path}",
                details={"manifest_path": str(self.manifest_path), "track_id": track.get("id"), "source_path": str(source_path)},
            )
        self._prepare_track_audio(source_path, output_path, target_duration_ms)
        license_file = self._resolve_optional_bank_path(track.get("license_file"))
        requires_attribution = bool(track.get("requires_attribution"))
        attribution = str(track.get("attribution") or "").strip() or None
        if requires_attribution and not attribution:
            attribution = self._default_attribution(track)
        license_note = str(track.get("license_note") or track.get("license") or "approved_local_music_bank").strip()
        return {
            "provider": self.provider_name,
            "query": query,
            "mood": mood,
            "source_url": str(track.get("source_url") or "").strip() or None,
            "attribution": attribution,
            "license_note": license_note,
            "audio_uri": output_path.resolve().as_uri(),
            "duration_ms": target_duration_ms,
            "provider_metadata": {
                "selection_mode": "approved_bank",
                "track_id": track.get("id"),
                "track_title": track.get("title"),
                "artist": track.get("artist"),
                "source_path": str(source_path),
                "license_file": str(license_file) if license_file else None,
                "requires_attribution": requires_attribution,
                "content_id_registered": bool(track.get("content_id_registered")),
                "content_id_risk": str(track.get("content_id_risk") or "unknown"),
                "approved_for_youtube": bool(track.get("approved_for_youtube")),
                "requested_duration_ms": target_duration_ms,
                "source_looped_to_ms": target_duration_ms,
            },
        }

    def _load_manifest(self) -> list[dict[str, Any]]:
        if not self.manifest_path.exists():
            if bool(getattr(self.settings, "music_bank_auto_populate", True)):
                populate_builtin_music_bank(self.bank_dir)
            else:
                raise ProviderFailure(
                    self.provider_name,
                    "approved music bank manifest is missing",
                    details={"manifest_path": str(self.manifest_path)},
                )
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderFailure(
                self.provider_name,
                f"approved music bank manifest is invalid: {exc}",
                details={"manifest_path": str(self.manifest_path)},
            ) from exc
        tracks = payload.get("tracks") if isinstance(payload, dict) else payload
        if not isinstance(tracks, list):
            raise ProviderFailure(self.provider_name, "approved music bank manifest must contain a list of tracks")
        return [track for track in tracks if isinstance(track, dict)]

    def _select_approved_track(self, tracks: list[dict[str, Any]], mood: str, query: str) -> dict[str, Any]:
        candidates = [track for track in tracks if self._is_usable_track(track)]
        if not candidates and bool(getattr(self.settings, "music_bank_auto_populate", True)):
            populate_builtin_music_bank(self.bank_dir)
            tracks = self._load_manifest()
            candidates = [track for track in tracks if self._is_usable_track(track)]
        if not candidates:
            raise ProviderFailure(
                self.provider_name,
                "approved music bank has no usable YouTube-approved tracks",
                details={"manifest_path": str(self.manifest_path), "track_count": len(tracks)},
            )
        scored = sorted(
            ((self._score_track(track, mood, query), str(track.get("id") or track.get("path") or ""), track) for track in candidates),
            key=lambda item: (-item[0], item[1]),
        )
        best_score = scored[0][0]
        best_tracks = [item[2] for item in scored if item[0] == best_score]
        index = sum(ord(char) for char in query) % len(best_tracks)
        return best_tracks[index]

    def _is_usable_track(self, track: dict[str, Any]) -> bool:
        if not track.get("approved_for_youtube"):
            return False
        if track.get("content_id_registered"):
            return False
        if str(track.get("content_id_risk") or "").strip().lower() in {"high", "registered", "blocked"}:
            return False
        if not track.get("path"):
            return False
        if not (track.get("license") or track.get("license_note")):
            return False
        if not (track.get("source_url") or track.get("license_file")):
            return False
        return True

    def _score_track(self, track: dict[str, Any], mood: str, query: str) -> int:
        labels = self._track_labels(track)
        score = 0
        if str(track.get("quality_tier") or "").strip().lower() == "primary":
            score += 40
        if str(track.get("bank_source") or "").strip().lower() == "minimax_artifact":
            score += 15
        if mood in labels:
            score += 20
        for token in word_tokens(query):
            if token in labels:
                score += 2
        if str(track.get("content_id_risk") or "").strip().lower() == "low":
            score += 2
        if not bool(track.get("requires_attribution")):
            score += 1
        return score

    def _track_labels(self, track: dict[str, Any]) -> set[str]:
        values: list[str] = []
        for key in ("mood", "moods", "tags", "genre", "genres"):
            raw = track.get(key)
            if isinstance(raw, list):
                values.extend(str(item) for item in raw)
            elif raw:
                values.append(str(raw))
        return {token for value in values for token in word_tokens(value)}

    def _infer_mood(self, topic_plan: dict[str, Any], script: dict[str, Any]) -> str:
        surface = " ".join(
            [
                str(topic_plan.get("canonical_topic") or ""),
                str(topic_plan.get("angle") or ""),
                str(script.get("title") or ""),
                str(script.get("hook") or ""),
            ]
        ).lower()
        if any(term in surface for term in ["mistério", "misterio", "segredo", "sombra", "buraco negro", "crime"]):
            return "suspense"
        if any(term in surface for term in ["cafeína", "cafeina", "neuro", "tecnologia", "universo", "espaço", "espaco"]):
            return "technology"
        if any(term in surface for term in ["polvo", "gato", "animal", "oceano", "natureza", "história", "historia"]):
            return "documentary"
        return "cinematic"

    def _query_hint(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str) -> str:
        topic = str(topic_plan.get("canonical_topic") or "").strip()
        angle = str(topic_plan.get("angle") or "").strip()
        title = str(script.get("title") or "").strip()
        return " ".join(part for part in [topic, angle, title, mood] if part).strip()

    def _resolve_bank_path(self, value: Any) -> Path:
        raw_path = Path(str(value or ""))
        return raw_path if raw_path.is_absolute() else self.bank_dir / raw_path

    def _resolve_optional_bank_path(self, value: Any) -> Path | None:
        if not value:
            return None
        return self._resolve_bank_path(value)

    def _default_attribution(self, track: dict[str, Any]) -> str:
        title = str(track.get("title") or track.get("id") or "Unknown track").strip()
        artist = str(track.get("artist") or "Unknown artist").strip()
        source = str(track.get("source_url") or "").strip()
        parts = [f"{title} by {artist}"]
        if source:
            parts.append(source)
        return " - ".join(parts)

    def _prepare_track_audio(self, source_path: Path, output_path: Path, target_duration_ms: int) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_sec = max(target_duration_ms / 1000, 1.0)
        fade_duration = min(1.2, max(duration_sec / 2, 0.1))
        fade_out_start = max(duration_sec - fade_duration, 0.0)
        temp_path = output_path.with_suffix(".local-bank.wav")
        try:
            result = subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(source_path),
                    "-t",
                    f"{duration_sec:.3f}",
                    "-af",
                    f"aresample=24000,afade=t=in:st=0:d=0.25,afade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(temp_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise ProviderFailure(
                    self.provider_name,
                    "failed to prepare approved music bank track",
                    details={"source_path": str(source_path), "stderr": result.stderr[-1000:] if result.stderr else None},
                )
            temp_path.replace(output_path)
        finally:
            temp_path.unlink(missing_ok=True)


class MiniMaxBackgroundMusicProvider:
    provider_name = "minimax_music"

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.resolved_minimax_music_api_key
        if not api_key:
            raise ProviderFailure("minimax_music", "missing minimax music api key")
        self.settings = settings
        self.api_key = api_key
        self.url = f"{settings.minimax_music_base_url.rstrip('/')}/music_generation"
        self.timeout = httpx.Timeout(settings.minimax_music_timeout_sec, connect=15.0)
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        mood = self._infer_mood(topic_plan, script)
        query = self._query_hint(topic_plan, script, mood)
        prompt = self._build_prompt(topic_plan, script, mood, target_duration_ms)
        payload = {
            "model": "music-2.6",
            "prompt": prompt,
            "lyrics": "",
            "is_instrumental": True,
            "lyrics_optimizer": False,
            "output_format": "url",
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": 256000,
                "format": "mp3",
            },
        }
        debug_payload = {
            "provider": self.provider_name,
            "url": self.url,
            "query": query,
            "mood": mood,
            "target_duration_ms": target_duration_ms,
            "timeout_sec": self.settings.minimax_music_timeout_sec,
            "request_payload": payload,
        }
        try:
            response = httpx.post(self.url, json=payload, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "minimax_music",
                f"minimax music request timed out after {self.settings.minimax_music_timeout_sec}s",
                details={**debug_payload, "error_type": type(exc).__name__},
            ) from exc
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text[:500] if exc.response is not None else None
            raise ProviderFailure(
                "minimax_music",
                f"minimax music http {exc.response.status_code}: {response_text or exc}",
                details={
                    **debug_payload,
                    "error_type": type(exc).__name__,
                    "status_code": exc.response.status_code if exc.response is not None else None,
                    "response_text": response_text,
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure(
                "minimax_music",
                str(exc),
                details={**debug_payload, "error_type": type(exc).__name__},
            ) from exc
        data = body.get("data", {}) if isinstance(body.get("data"), dict) else {}
        base_resp = body.get("base_resp", {}) if isinstance(body.get("base_resp"), dict) else {}
        base_status_code = base_resp.get("status_code")
        base_status_msg = str(base_resp.get("status_msg") or "").strip()
        if base_status_code not in (None, 0, "0"):
            raise ProviderFailure(
                "minimax_music",
                f"provider limit: {base_status_msg or f'minimax status {base_status_code}'}",
                details={
                    **debug_payload,
                    "response_trace_id": body.get("trace_id"),
                    "base_resp": base_resp,
                    "data_keys": sorted(data.keys()),
                    "extra_info": body.get("extra_info"),
                    "analysis_info": body.get("analysis_info"),
                    "response_keys": sorted(body.keys()),
                },
            )
        audio_payload = str(data.get("audio") or "")
        if not audio_payload:
            raise ProviderFailure(
                "minimax_music",
                "missing audio payload from minimax music generation",
                details={
                    **debug_payload,
                    "response_trace_id": body.get("trace_id"),
                    "base_resp": body.get("base_resp"),
                    "provider_status": data.get("status"),
                    "data_keys": sorted(data.keys()),
                    "extra_info": body.get("extra_info"),
                    "analysis_info": body.get("analysis_info"),
                    "response_keys": sorted(body.keys()),
                },
            )
        source_url = audio_payload if self._looks_like_url(audio_payload) else None
        if source_url:
            self._download_audio_to_wav(source_url, output_path)
        else:
            self._decode_audio_to_wav(audio_payload, output_path)
        trim_metadata = self._trim_wav_to_target_duration(output_path, target_duration_ms)
        extra_info = body.get("extra_info", {}) if isinstance(body.get("extra_info"), dict) else {}
        return {
            "provider": self.provider_name,
            "query": query,
            "mood": mood,
            "source_url": source_url,
            "attribution": "AI-generated instrumental background music via MiniMax.",
            "license_note": "Generated with MiniMax music_generation API.",
            "audio_uri": output_path.resolve().as_uri(),
            "duration_ms": target_duration_ms,
            "provider_metadata": {
                "selection_mode": "generated",
                "model": "music-2.6",
                "instrumental": True,
                "output_format": "url",
                "prompt": prompt,
                "trace_id": body.get("trace_id"),
                "provider_status": data.get("status"),
                "requested_duration_ms": target_duration_ms,
                "returned_duration_ms": extra_info.get("music_duration"),
                "returned_sample_rate": extra_info.get("music_sample_rate"),
                "returned_channels": extra_info.get("music_channel"),
                "returned_bitrate": extra_info.get("bitrate"),
                **trim_metadata,
            },
        }

    def _infer_mood(self, topic_plan: dict[str, Any], script: dict[str, Any]) -> str:
        surface = " ".join(
            [
                str(topic_plan.get("canonical_topic") or ""),
                str(topic_plan.get("angle") or ""),
                str(script.get("title") or ""),
                str(script.get("hook") or ""),
            ]
        ).lower()
        if any(term in surface for term in ["mistério", "misterio", "segredo", "sombra", "buraco negro", "crime"]):
            return "suspense"
        if any(term in surface for term in ["cafeína", "cafeina", "neuro", "tecnologia", "universo", "espaço", "espaco"]):
            return "technology"
        if any(term in surface for term in ["polvo", "gato", "animal", "oceano", "natureza", "história", "historia"]):
            return "documentary"
        return "cinematic"

    def _query_hint(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str) -> str:
        topic = str(topic_plan.get("canonical_topic") or "").strip()
        angle = str(topic_plan.get("angle") or "").strip()
        title = str(script.get("title") or "").strip()
        parts = [topic, angle, title, mood]
        return " ".join(part for part in parts if part).strip()

    def _build_prompt(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str, target_duration_ms: int) -> str:
        duration_sec = max(8, round(target_duration_ms / 1000))
        topic = str(topic_plan.get("canonical_topic") or "").strip()
        angle = str(topic_plan.get("angle") or "").strip()
        title = str(script.get("title") or "").strip()
        hook = str(script.get("hook") or "").strip()
        mood_map = {
            "suspense": "tense documentary underscore, restrained, mysterious, no jump scares, no vocals",
            "technology": "modern science documentary underscore, pulsing but controlled, curious, precise, no vocals",
            "documentary": "curiosity-driven documentary underscore, warm and intelligent, organic percussion, no vocals",
            "cinematic": "cinematic short-form background score, engaging and polished, no vocals",
        }
        brief_context = ". ".join(part for part in [title, hook] if part)
        if len(brief_context) > 120:
            brief_context = brief_context[:117].rstrip(" ,.;:") + "..."
        prompt = (
            f"Instrumental only. {mood_map.get(mood, mood_map['cinematic'])}. "
            f"Designed as background music for a vertical educational short about {topic or 'a curiosity topic'}. "
            f"Angle: {angle or 'counterintuitive reveal'}. "
            f"Target duration exactly {duration_sec} seconds, matching the narration length as closely as possible. "
            "End naturally at that runtime, no long tail, intro, or outro. "
            "Fast hook in the first 2 seconds, steady mid-section, clean ending for narration ducking. "
            "No vocals, spoken words, lyrics, or stingers that overpower voice-over. "
            "Avoid pop-song structure; this should feel like underscore, not a standalone single. "
            f"Video context: {brief_context or topic or 'scientific curiosity short'}"
        )
        return " ".join(prompt.split())

    def _looks_like_url(self, audio_payload: str) -> bool:
        return audio_payload.startswith("http://") or audio_payload.startswith("https://")

    def _download_audio_to_wav(self, audio_url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".minimax.mp3")
        try:
            response = httpx.get(audio_url, timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            temp_path.write_bytes(response.content)
            self._convert_audio_file_to_wav(temp_path, output_path)
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "minimax_music",
                f"minimax music download timed out after {self.settings.minimax_music_timeout_sec}s",
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def _decode_audio_to_wav(self, audio_hex: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".minimax.mp3")
        try:
            temp_path.write_bytes(binascii.unhexlify(audio_hex))
            self._convert_audio_file_to_wav(temp_path, output_path)
        except (binascii.Error, ValueError) as exc:
            raise ProviderFailure("minimax_music", f"invalid audio payload from minimax: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def _convert_audio_file_to_wav(self, input_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(input_path),
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

    def _trim_wav_to_target_duration(self, output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        target_sec = max(target_duration_ms / 1000, 1.0)
        temp_path = output_path.with_suffix(".trimmed.wav")
        try:
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(output_path),
                    "-t",
                    f"{target_sec:.3f}",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(temp_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            temp_path.replace(output_path)
        except subprocess.CalledProcessError as exc:
            raise ProviderFailure(
                "minimax_music",
                "failed to trim minimax music to target duration",
                details={
                    "target_duration_ms": target_duration_ms,
                    "stderr": exc.stderr[-1000:] if exc.stderr else None,
                },
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)
        return {
            "source_trimmed_to_ms": round(target_sec * 1000),
            "source_trim_applied": True,
        }


class ResilientMusicProvider:
    def __init__(self) -> None:
        settings = get_settings()
        self.providers: list[Any] = []
        provider_mode = str(getattr(settings, "background_music_provider", "local_bank") or "local_bank").strip().lower()
        allow_api_fallback = bool(getattr(settings, "allow_music_api_fallback", False))
        if settings.use_mock_providers:
            self.providers.append(MockBackgroundMusicProvider())
        elif provider_mode == "local_bank":
            self.providers.append(LocalMusicBankProvider())
            if allow_api_fallback and settings.resolved_minimax_music_api_key:
                self.providers.append(MiniMaxBackgroundMusicProvider())
        elif provider_mode == "auto":
            self.providers.append(LocalMusicBankProvider())
            if settings.resolved_minimax_music_api_key:
                self.providers.append(MiniMaxBackgroundMusicProvider())
        elif provider_mode == "minimax" and settings.resolved_minimax_music_api_key:
            self.providers.append(MiniMaxBackgroundMusicProvider())
        elif provider_mode == "minimax":
            self.providers = []
        else:
            raise ProviderFailure("background_music", f"unknown background music provider: {provider_mode}")

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        last_error = "music selection failed"
        last_details: dict[str, Any] = {}
        if not self.providers:
            raise ProviderFailure("background_music", "background music provider is not configured")
        for provider in self.providers:
            try:
                return provider.select_track(topic_plan, script, output_path, target_duration_ms)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if isinstance(exc, ProviderFailure):
                    last_details = dict(exc.details or {})
        raise ProviderFailure("background_music", f"background music selection failed: {last_error}", details=last_details)
