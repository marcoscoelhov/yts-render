from __future__ import annotations

import asyncio
import audioop
import base64
import math
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from app.quality.subtitle_gate import BAD_ENDINGS
from app.config import get_settings
from app.utils import parse_srt, word_tokens, wrap_caption


class LocalSpeechFallbackProvider:
    voice = "pt-BR-FranciscaNeural"

    def synthesize(self, text: str, audio_path: Path, srt_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        mode = self._write_speech_audio(text, audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        cues = self._build_cues(text, duration_ms)
        srt_path.write_text(self._render_srt(cues), encoding="utf-8")
        self._normalize_speech_envelope(audio_path, srt_path)
        self._apply_final_loudness_normalization(audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        provider = "espeak_ng" if mode == "espeak_ng" else "synthetic_wav"
        return {
            "provider": provider,
            "voice": self.voice,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {
                "mode": mode,
                "cue_count": len(cues),
                "fallback_used": True,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
            },
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
        chunks = self._avoid_weak_cue_endings(chunks)
        cues: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks, start=1):
            start = round((idx - 1) / len(chunks) * duration_ms)
            end = duration_ms if idx == len(chunks) else round(idx / len(chunks) * duration_ms)
            cues.append({"idx": idx, "start_ms": start, "end_ms": end, "text": wrap_caption(chunk)})
        return cues

    def _avoid_weak_cue_endings(self, chunks: list[str]) -> list[str]:
        repaired = [chunk for chunk in chunks if str(chunk).strip()]
        for index in range(len(repaired) - 1):
            current_words = repaired[index].split()
            next_words = repaired[index + 1].split()
            if not current_words or not next_words:
                continue
            ending_tokens = word_tokens(current_words[-1])
            ending = ending_tokens[0] if ending_tokens else ""
            if ending not in BAD_ENDINGS:
                continue
            candidate_current = " ".join([*current_words, next_words[0]])
            candidate_next = " ".join(next_words[1:])
            if candidate_next and len(candidate_current) <= 42:
                repaired[index] = candidate_current
                repaired[index + 1] = candidate_next
        return repaired

    def _write_speech_audio(self, text: str, path: Path) -> str:
        if not shutil.which("espeak-ng"):
            self._write_synthetic_audio(text, path)
            return "synthetic_wav"
        raw_path = path.with_name(path.stem + ".raw.wav")
        try:
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
                    imageio_ffmpeg.get_ffmpeg_exe(),
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
            return "espeak_ng"
        except Exception:
            self._write_synthetic_audio(text, path)
            return "synthetic_wav"
        finally:
            raw_path.unlink(missing_ok=True)

    def _write_synthetic_audio(self, text: str, path: Path) -> None:
        sample_rate = 24_000
        duration_sec = max(25.0, min(45.0, len(word_tokens(text)) / 2.0))
        frame_count = int(sample_rate * duration_sec)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for idx in range(frame_count):
                t = idx / sample_rate
                envelope = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * 1.8 * t))
                sample = int(2600 * envelope * math.sin(2 * math.pi * 185 * t))
                frames.extend(sample.to_bytes(2, "little", signed=True))
            wav_file.writeframes(frames)

    def _measure_audio_ms(self, path: Path) -> int:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
        return int(frames / sample_rate * 1000)

    def _normalize_speech_envelope(self, audio_path: Path, srt_path: Path, target_rms_db: float = -20.0) -> None:
        cues = parse_srt(srt_path.read_text(encoding="utf-8")) if srt_path.exists() else []
        if not cues:
            return
        with wave.open(str(audio_path), "rb") as source:
            params = source.getparams()
            frame_rate = source.getframerate()
            sample_width = source.getsampwidth()
            channels = source.getnchannels()
            audio = bytearray(source.readframes(source.getnframes()))
        if sample_width != 2:
            return
        full_scale = float(2 ** (8 * sample_width - 1))
        target_rms = full_scale * (10 ** (target_rms_db / 20))
        peak_ceiling = full_scale * (10 ** (-3.0 / 20))
        frame_size = sample_width * channels
        for cue in cues:
            start_frame = max(0, round(int(cue["start_ms"]) * frame_rate / 1000))
            end_frame = max(start_frame + 1, round(int(cue["end_ms"]) * frame_rate / 1000))
            start = start_frame * frame_size
            end = min(len(audio), end_frame * frame_size)
            segment = bytes(audio[start:end])
            if not segment:
                continue
            rms = audioop.rms(segment, sample_width)
            peak = audioop.max(segment, sample_width)
            if rms <= 0 or peak <= 0:
                continue
            gain = target_rms / rms
            gain = min(gain, peak_ceiling / peak)
            gain = max(0.45, min(gain, 4.0))
            audio[start:end] = audioop.mul(segment, sample_width, gain)
        temp_path = audio_path.with_suffix(".leveled.wav")
        with wave.open(str(temp_path), "wb") as target:
            target.setparams(params)
            target.writeframes(bytes(audio))
        temp_path.replace(audio_path)

    def _apply_final_loudness_normalization(self, audio_path: Path) -> None:
        temp_path = audio_path.with_suffix(".loudnorm.wav")
        try:
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(audio_path),
                    "-af",
                    "highpass=f=80,lowpass=f=12000,loudnorm=I=-16:LRA=11:TP=-1.5",
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
            temp_path.replace(audio_path)
        finally:
            temp_path.unlink(missing_ok=True)

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
    rate = "+12%"

    async def _run(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        import edge_tts

        communicate = edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            connect_timeout=20,
            receive_timeout=120,
        )
        submaker = edge_tts.SubMaker()
        temp_audio = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_audio_path = Path(temp_audio.name)
        temp_audio.close()
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(temp_audio_path, "wb") as audio_file:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_file.write(chunk["data"])
                    elif chunk["type"] in {"WordBoundary", "SentenceBoundary"}:
                        submaker.feed(chunk)
            self._normalize_edge_audio(temp_audio_path, audio_path)
        finally:
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
            "provider_metadata": {
                "mode": "edge",
                "rate": self.rate,
                "fallback_used": False,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
                "envelope_normalized": False,
                "denoise_applied": True,
            },
        }

    def synthesize(self, text: str, audio_path: Path, srt_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return asyncio.run(self._run(text, audio_path, srt_path))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
        fallback = super().synthesize(text, audio_path, srt_path)
        fallback["provider_metadata"]["fallback_reason"] = f"edge_tts failed after 3 attempts: {last_error}"
        return fallback

    def _normalize_edge_audio(self, source_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(source_path),
                "-af",
                "highpass=f=70,lowpass=f=9500,afftdn=nf=-25,loudnorm=I=-16:LRA=11:TP=-1.5",
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


class ElevenLabsTTSProvider(EdgeTTSProvider):
    def synthesize(self, text: str, audio_path: Path, srt_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = get_settings()
        if not settings.elevenlabs_api_key:
            fallback = super().synthesize(text, audio_path, srt_path, context)
            metadata = fallback.setdefault("provider_metadata", {})
            metadata["fallback_used"] = True
            metadata["fallback_from_provider"] = "elevenlabs"
            metadata["fallback_provider"] = fallback.get("provider")
            metadata["fallback_reason"] = "missing YTS_ELEVENLABS_API_KEY"
            return fallback
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                return self._run_elevenlabs(text, audio_path, srt_path, settings)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * attempt)
                    continue
        fallback = super().synthesize(text, audio_path, srt_path, context)
        metadata = fallback.setdefault("provider_metadata", {})
        metadata["fallback_used"] = True
        metadata["fallback_from_provider"] = "elevenlabs"
        metadata["fallback_provider"] = fallback.get("provider")
        metadata["fallback_reason"] = f"elevenlabs failed after 2 attempts: {last_error}"
        return fallback

    def _run_elevenlabs(self, text: str, audio_path: Path, srt_path: Path, settings: Any) -> dict[str, Any]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        temp_audio = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_audio_path = Path(temp_audio.name)
        temp_audio.close()
        try:
            url = f"{settings.elevenlabs_base_url.rstrip('/')}/v1/text-to-speech/{settings.elevenlabs_voice_id}"
            payload = {
                "text": text,
                "model_id": settings.elevenlabs_model_id,
                "voice_settings": {
                    "stability": settings.elevenlabs_voice_stability,
                    "similarity_boost": settings.elevenlabs_voice_similarity_boost,
                    "style": settings.elevenlabs_voice_style,
                    "use_speaker_boost": settings.elevenlabs_voice_use_speaker_boost,
                },
            }
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": settings.elevenlabs_api_key,
            }
            with httpx.Client(timeout=settings.elevenlabs_timeout_sec) as client:
                response = client.post(
                    url,
                    params={"output_format": settings.elevenlabs_output_format},
                    headers=headers,
                    json=payload,
                )
            if response.status_code >= 400:
                detail = response.text[:500].replace(settings.elevenlabs_api_key, "[redacted]")
                raise RuntimeError(f"elevenlabs status={response.status_code}: {detail}")
            temp_audio_path.write_bytes(response.content)
            self._normalize_elevenlabs_audio(temp_audio_path, audio_path)
        finally:
            temp_audio_path.unlink(missing_ok=True)

        duration_ms = self._measure_audio_ms(audio_path)
        cues = self._build_cues(text, duration_ms)
        srt_path.write_text(self._render_srt(cues), encoding="utf-8")
        return {
            "provider": "elevenlabs",
            "voice": settings.elevenlabs_voice_id,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {
                "mode": "elevenlabs",
                "model_id": settings.elevenlabs_model_id,
                "output_format": settings.elevenlabs_output_format,
                "voice_id": settings.elevenlabs_voice_id,
                "fallback_used": False,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
                "envelope_normalized": False,
            },
        }

    def _normalize_elevenlabs_audio(self, source_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(source_path),
                "-af",
                "highpass=f=70,lowpass=f=12000,loudnorm=I=-16:LRA=11:TP=-1.5",
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


class GeminiTTSProvider(ElevenLabsTTSProvider):
    def synthesize(self, text: str, audio_path: Path, srt_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = get_settings()
        api_key = settings.gemini_tts_api_key or settings.gemini_api_key
        if not api_key:
            fallback = super().synthesize(text, audio_path, srt_path, context)
            self._mark_gemini_fallback(fallback, "missing YTS_GEMINI_TTS_API_KEY or YTS_GEMINI_API_KEY")
            return fallback
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                return self._run_gemini(text, audio_path, srt_path, settings, api_key, context)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * attempt)
                    continue
        fallback = super().synthesize(text, audio_path, srt_path, context)
        self._mark_gemini_fallback(fallback, f"gemini_tts failed after 2 attempts: {last_error}")
        return fallback

    def _mark_gemini_fallback(self, fallback: dict[str, Any], reason: str) -> None:
        metadata = fallback.setdefault("provider_metadata", {})
        metadata["fallback_used"] = True
        metadata["fallback_from_provider"] = "gemini_tts"
        metadata["fallback_provider"] = fallback.get("provider")
        metadata["fallback_reason"] = reason

    def _run_gemini(self, text: str, audio_path: Path, srt_path: Path, settings: Any, api_key: str, context: dict[str, Any] | None) -> dict[str, Any]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes, mime_type = self._generate_gemini_audio_bytes(text, settings, api_key, context)
        self._write_gemini_audio(audio_bytes, mime_type, audio_path)
        self._apply_final_loudness_normalization(audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        cues = self._build_cues(text, duration_ms)
        srt_path.write_text(self._render_srt(cues), encoding="utf-8")
        return {
            "provider": "gemini_tts",
            "voice": settings.gemini_tts_voice_name,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {
                "mode": "gemini_tts",
                "model_id": settings.gemini_tts_model,
                "voice_name": settings.gemini_tts_voice_name,
                "mime_type": mime_type,
                "voice_direction_used": bool(context),
                "voice_direction": self._metadata_voice_direction(context),
                "fallback_used": False,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
                "envelope_normalized": False,
            },
        }

    def _generate_gemini_audio_bytes(self, text: str, settings: Any, api_key: str, context: dict[str, Any] | None) -> tuple[bytes, str]:
        from google import genai
        from google.genai import types

        prompt = self._build_gemini_prompt(text, settings, context)
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(float(settings.gemini_tts_timeout_sec) * 1000)))
        response = client.models.generate_content(
            model=settings.gemini_tts_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_tts_voice_name)
                    )
                )
            ),
        )
        for candidate in response.candidates or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                data = getattr(inline_data, "data", None)
                if data:
                    audio_bytes = base64.b64decode(data) if isinstance(data, str) else bytes(data)
                    return audio_bytes, str(getattr(inline_data, "mime_type", "") or "audio/L16;rate=24000")
        raise RuntimeError("gemini_tts returned no audio data")

    def _build_gemini_prompt(self, text: str, settings: Any, context: dict[str, Any] | None) -> str:
        direction = context or {}
        retention_map = direction.get("retention_map") if isinstance(direction.get("retention_map"), dict) else {}
        retention_lines = []
        for key in ("visual_hook", "proof_or_tension", "escalation", "turn_or_payoff", "loop_close"):
            value = retention_map.get(key)
            if value:
                retention_lines.append(f"- {key}: {value}")
        blocks = [
            "### PERFIL DA VOZ",
            str(settings.gemini_tts_style_prompt),
            "A narração deve soar humana, brasileira e editorialmente intencional, sem tom de propaganda ou leitura robotica.",
            "",
            "### PRIORIDADE EDITORIAL",
            "1. O hook deve segurar atenção nos primeiros segundos com urgência controlada.",
            "2. A retenção vem antes de dramatização: mantenha tensão crescente sem exagerar.",
            "3. O payoff deve ganhar ênfase clara quando a virada aparecer.",
            "4. O fechamento deve recontextualizar o começo e provocar replay mental.",
            "5. Preserve exatamente o texto aprovado; não adicione, remova ou reescreva palavras.",
        ]
        if direction:
            blocks.extend(
                [
                    "",
                    "### CONTEXTO DO ROTEIRO",
                    f"Tema: {direction.get('canonical_topic') or 'nao informado'}",
                    f"Angulo: {direction.get('angle') or 'nao informado'}",
                    f"Titulo: {direction.get('title') or 'nao informado'}",
                    f"Hook: {direction.get('hook') or 'nao informado'}",
                    f"Payoff ou fechamento: {direction.get('ending') or 'nao informado'}",
                    f"Duracao alvo: {direction.get('estimated_duration_sec') or 'nao informada'} segundos",
                ]
            )
        if retention_lines:
            blocks.extend(["", "### MAPA DE RETENCAO", *retention_lines])
        blocks.extend(["", "### TEXTO EXATO DA NARRACAO", text])
        return "\n".join(blocks)

    def _metadata_voice_direction(self, context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not context:
            return None
        retention_map = context.get("retention_map") if isinstance(context.get("retention_map"), dict) else {}
        return {
            "title": context.get("title"),
            "hook": context.get("hook"),
            "ending": context.get("ending"),
            "canonical_topic": context.get("canonical_topic"),
            "retention_roles": [key for key in ("visual_hook", "proof_or_tension", "escalation", "turn_or_payoff", "loop_close") if retention_map.get(key)],
        }

    def _write_gemini_audio(self, audio_bytes: bytes, mime_type: str, output_path: Path) -> None:
        normalized_mime = mime_type.lower()
        if "wav" in normalized_mime:
            temp_path = output_path.with_suffix(".gemini-source.wav")
            try:
                temp_path.write_bytes(audio_bytes)
                self._normalize_elevenlabs_audio(temp_path, output_path)
            finally:
                temp_path.unlink(missing_ok=True)
            return
        sample_rate = self._sample_rate_from_mime(mime_type)
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)

    def _sample_rate_from_mime(self, mime_type: str) -> int:
        match = re.search(r"(?:rate|sample_rate)=(\d+)", mime_type, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 24000
