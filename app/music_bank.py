from __future__ import annotations

import hashlib
import json
import math
import shutil
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_TRACKS: list[dict[str, Any]] = [
    {
        "id": "local-science-calm-01",
        "title": "Local Science Calm 01",
        "moods": ["technology", "documentary", "cinematic"],
        "tags": ["ciencia", "curiosidades", "ambiente", "neuro"],
        "base_freq": 110.0,
        "pulse_hz": 0.32,
        "brightness": 0.42,
    },
    {
        "id": "local-discovery-pulse-01",
        "title": "Local Discovery Pulse 01",
        "moods": ["technology", "cinematic"],
        "tags": ["descoberta", "tecnologia", "universo"],
        "base_freq": 123.47,
        "pulse_hz": 0.46,
        "brightness": 0.55,
    },
    {
        "id": "local-documentary-warm-01",
        "title": "Local Documentary Warm 01",
        "moods": ["documentary", "cinematic"],
        "tags": ["natureza", "animal", "oceano", "historia"],
        "base_freq": 146.83,
        "pulse_hz": 0.28,
        "brightness": 0.35,
    },
    {
        "id": "local-suspense-low-01",
        "title": "Local Suspense Low 01",
        "moods": ["suspense", "cinematic"],
        "tags": ["misterio", "segredo", "sombra", "buraco negro"],
        "base_freq": 92.5,
        "pulse_hz": 0.22,
        "brightness": 0.25,
    },
    {
        "id": "local-curiosity-motion-01",
        "title": "Local Curiosity Motion 01",
        "moods": ["documentary", "technology"],
        "tags": ["curiosidades", "movimento", "cafeina"],
        "base_freq": 130.81,
        "pulse_hz": 0.52,
        "brightness": 0.50,
    },
    {
        "id": "local-space-ambient-01",
        "title": "Local Space Ambient 01",
        "moods": ["technology", "suspense", "cinematic"],
        "tags": ["espaco", "universo", "cosmos", "escala"],
        "base_freq": 98.0,
        "pulse_hz": 0.18,
        "brightness": 0.32,
    },
    {
        "id": "local-nature-soft-01",
        "title": "Local Nature Soft 01",
        "moods": ["documentary"],
        "tags": ["natureza", "animal", "polvo", "oceano"],
        "base_freq": 164.81,
        "pulse_hz": 0.30,
        "brightness": 0.38,
    },
    {
        "id": "local-cinematic-neutral-01",
        "title": "Local Cinematic Neutral 01",
        "moods": ["cinematic", "documentary", "technology", "suspense"],
        "tags": ["shorts", "retencao", "narracao"],
        "base_freq": 116.54,
        "pulse_hz": 0.36,
        "brightness": 0.45,
    },
]


def populate_builtin_music_bank(bank_dir: Path, *, force: bool = False, duration_seconds: int = 75) -> dict[str, Any]:
    bank_dir = Path(bank_dir)
    tracks_dir = bank_dir / "tracks"
    licenses_dir = bank_dir / "licenses"
    manifest_path = bank_dir / "manifest.json"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    licenses_dir.mkdir(parents=True, exist_ok=True)

    existing_tracks = _read_existing_tracks(manifest_path)
    by_id = {str(track.get("id")): track for track in existing_tracks if track.get("id")}
    written_ids: list[str] = []
    for preset in DEFAULT_TRACKS:
        track_id = str(preset["id"])
        audio_relative_path = f"tracks/{track_id}.wav"
        license_relative_path = f"licenses/{track_id}.txt"
        audio_path = bank_dir / audio_relative_path
        license_path = bank_dir / license_relative_path
        if force or not audio_path.exists():
            _write_synthetic_track(
                audio_path,
                duration_seconds=duration_seconds,
                base_freq=float(preset["base_freq"]),
                pulse_hz=float(preset["pulse_hz"]),
                brightness=float(preset["brightness"]),
            )
            written_ids.append(track_id)
        if force or not license_path.exists():
            license_path.write_text(_license_text(track_id), encoding="utf-8")
        by_id[track_id] = _manifest_track(preset, audio_relative_path, license_relative_path, duration_seconds)

    tracks = sorted(by_id.values(), key=lambda item: str(item.get("id") or ""))
    manifest = {
        "schema_version": "1.0",
        "source": "yts_render_builtin_synthetic_music_bank",
        "tracks": tracks,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "bank_dir": str(bank_dir),
        "manifest_path": str(manifest_path),
        "track_count": len(tracks),
        "written_track_ids": written_ids,
    }


def _read_existing_tracks(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tracks = payload.get("tracks") if isinstance(payload, dict) else payload
    if not isinstance(tracks, list):
        return []
    return [track for track in tracks if isinstance(track, dict)]


def _manifest_track(preset: dict[str, Any], audio_path: str, license_path: str, duration_seconds: int) -> dict[str, Any]:
    return {
        "id": preset["id"],
        "path": audio_path,
        "title": preset["title"],
        "artist": "YTS Render Local Generator",
        "moods": preset["moods"],
        "tags": preset["tags"],
        "license": "local_synthetic_project_owned",
        "license_note": "Locally generated instrumental bed by YTS Render; no external source material.",
        "license_file": license_path,
        "approved_for_youtube": True,
        "requires_attribution": False,
        "content_id_registered": False,
        "content_id_risk": "low",
        "quality_tier": "fallback",
        "bank_source": "builtin_synthetic",
        "instrumental": True,
        "vocals_or_lyrics": "none",
        "duration_seconds": duration_seconds,
    }


def _license_text(track_id: str) -> str:
    return (
        f"Track: {track_id}\n"
        "Source: Generated locally by YTS Render built-in synthetic music bank generator.\n"
        "External samples: none.\n"
        "Vocals or lyrics: none.\n"
        "License: project-owned local synthetic background bed for YTS Render jobs.\n"
        "Attribution required: no.\n"
        "Content ID registered: no.\n"
    )


def _write_synthetic_track(path: Path, *, duration_seconds: int, base_freq: float, pulse_hz: float, brightness: float) -> None:
    sample_rate = 44_100
    frame_count = max(1, int(sample_rate * duration_seconds))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        chord_steps = [0, 3, 7, 10, 5, 8, 12, 15]
        for idx in range(frame_count):
            t = idx / sample_rate
            beat = int(t * 2) % len(chord_steps)
            chord_root = base_freq * (2 ** (chord_steps[beat] / 12))
            phrase = 0.5 + 0.5 * math.sin(2 * math.pi * 0.035 * t)
            pulse = 0.68 + 0.32 * math.sin(2 * math.pi * pulse_hz * t)
            fade_in = min(1.0, t / 2.0)
            fade_out = min(1.0, max(0.0, (duration_seconds - t) / 2.5))
            envelope = fade_in * fade_out * pulse
            pad = (
                0.42 * math.sin(2 * math.pi * chord_root * 0.5 * t)
                + 0.30 * math.sin(2 * math.pi * chord_root * 0.75 * t + 0.5)
                + 0.22 * math.sin(2 * math.pi * chord_root * t + 1.1)
                + 0.16 * math.sin(2 * math.pi * chord_root * 1.5 * t + 1.8)
            )
            arp_gate = 1.0 if (int(t * 8) % 4) in {0, 2} else 0.35
            arp_freq = chord_root * (2 ** ([0, 7, 12, 15][int(t * 8) % 4] / 12))
            arp = math.sin(2 * math.pi * arp_freq * t + 0.25) * arp_gate
            low_pulse = math.sin(2 * math.pi * base_freq * 0.25 * t) * (0.4 + 0.6 * pulse)
            tone = (
                0.58 * pad
                + brightness * 0.20 * arp
                + 0.22 * low_pulse
                + brightness * 0.05 * math.sin(2 * math.pi * base_freq * 3.0 * t + 1.7)
            )
            slow_motion = 0.75 + 0.25 * phrase
            sample = int(3600 * envelope * slow_motion * tone)
            frames.extend(max(-32768, min(32767, sample)).to_bytes(2, "little", signed=True))
        wav_file.writeframes(frames)


def import_minimax_music_artifacts(
    artifacts_dir: Path,
    bank_dir: Path,
    *,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    artifacts_dir = Path(artifacts_dir)
    bank_dir = Path(bank_dir)
    tracks_dir = bank_dir / "tracks"
    licenses_dir = bank_dir / "licenses"
    manifest_path = bank_dir / "manifest.json"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    licenses_dir.mkdir(parents=True, exist_ok=True)

    existing_tracks = _read_existing_tracks(manifest_path)
    by_id = {str(track.get("id")): track for track in existing_tracks if track.get("id")}
    seen_hashes = {str(track.get("content_hash")) for track in existing_tracks if track.get("content_hash")}
    imported_ids: list[str] = []
    skipped: list[dict[str, str]] = []

    for music_json in sorted(artifacts_dir.glob("*/background_music.json")):
        if limit is not None and len(imported_ids) >= limit:
            break
        job_dir = music_json.parent
        job_id = job_dir.name
        try:
            payload = json.loads(music_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped.append({"job_id": job_id, "reason": "invalid_background_music_json"})
            continue
        if str(payload.get("provider") or "").lower() != "minimax_music":
            skipped.append({"job_id": job_id, "reason": "not_minimax_music"})
            continue
        quality = _read_quality_report(job_dir / "background_music_quality_report.json")
        if not quality.get("passed"):
            skipped.append({"job_id": job_id, "reason": "quality_report_not_passed"})
            continue
        source_path = _path_from_file_uri(str(payload.get("audio_uri") or ""))
        if not source_path or not source_path.exists():
            skipped.append({"job_id": job_id, "reason": "missing_audio_file"})
            continue
        content_hash = _file_sha256(source_path)
        if content_hash in seen_hashes and not force:
            skipped.append({"job_id": job_id, "reason": "duplicate_audio_hash"})
            continue
        track_id = f"minimax-{job_id[:8]}"
        target_audio = tracks_dir / f"{track_id}.wav"
        target_license = licenses_dir / f"{track_id}.txt"
        if force or not target_audio.exists():
            shutil.copy2(source_path, target_audio)
        target_license.write_text(_minimax_license_text(job_id, payload), encoding="utf-8")
        track = _minimax_manifest_track(
            track_id=track_id,
            audio_relative_path=f"tracks/{target_audio.name}",
            license_relative_path=f"licenses/{target_license.name}",
            job_id=job_id,
            payload=payload,
            quality=quality,
            content_hash=content_hash,
        )
        by_id[track_id] = track
        seen_hashes.add(content_hash)
        imported_ids.append(track_id)

    tracks = sorted(by_id.values(), key=lambda item: str(item.get("id") or ""))
    manifest = {
        "schema_version": "1.0",
        "source": "yts_render_music_bank",
        "updated_at": datetime.now(UTC).isoformat(),
        "tracks": tracks,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "artifacts_dir": str(artifacts_dir),
        "bank_dir": str(bank_dir),
        "manifest_path": str(manifest_path),
        "imported_count": len(imported_ids),
        "imported_track_ids": imported_ids,
        "skipped_count": len(skipped),
        "skipped": skipped[:20],
        "track_count": len(tracks),
    }


def _read_quality_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"passed": False, "missing": True}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"passed": False, "invalid": True}
    return payload if isinstance(payload, dict) else {"passed": False, "invalid": True}


def _path_from_file_uri(value: str) -> Path | None:
    if value.startswith("file://"):
        return Path(value.removeprefix("file://"))
    if value:
        return Path(value)
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _minimax_manifest_track(
    *,
    track_id: str,
    audio_relative_path: str,
    license_relative_path: str,
    job_id: str,
    payload: dict[str, Any],
    quality: dict[str, Any],
    content_hash: str,
) -> dict[str, Any]:
    metadata = payload.get("provider_metadata") if isinstance(payload.get("provider_metadata"), dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    source_metrics = metrics.get("music_source") if isinstance(metrics.get("music_source"), dict) else {}
    mood = str(payload.get("mood") or "cinematic")
    return {
        "id": track_id,
        "path": audio_relative_path,
        "title": f"MiniMax {mood} {job_id[:8]}",
        "artist": "MiniMax Music API",
        "moods": [mood, "cinematic"],
        "tags": [token for token in word_like_tokens(str(payload.get("query") or ""))[:12]],
        "license": "MiniMax music_generation API",
        "license_note": str(payload.get("license_note") or "Generated with MiniMax music_generation API."),
        "license_file": license_relative_path,
        "source_url": _sanitize_url(payload.get("source_url")),
        "approved_for_youtube": True,
        "requires_attribution": bool(payload.get("attribution")),
        "attribution": payload.get("attribution"),
        "content_id_registered": False,
        "content_id_risk": "low",
        "quality_tier": "primary",
        "bank_source": "minimax_artifact",
        "source_provider": "minimax_music",
        "source_job_id": job_id,
        "trace_id": metadata.get("trace_id"),
        "model": metadata.get("model"),
        "instrumental": metadata.get("instrumental") is True,
        "vocals_or_lyrics": str(metadata.get("vocals_or_lyrics") or "unknown").strip().lower(),
        "human_instrumental_review_confirmed": bool(metadata.get("human_instrumental_review_confirmed")),
        "content_hash": content_hash,
        "quality_report_passed": True,
        "duration_ms": source_metrics.get("duration_ms") or payload.get("duration_ms"),
        "rms_dbfs": source_metrics.get("rms_dbfs"),
        "peak_dbfs": source_metrics.get("peak_dbfs"),
    }


def _minimax_license_text(job_id: str, payload: dict[str, Any]) -> str:
    metadata = payload.get("provider_metadata") if isinstance(payload.get("provider_metadata"), dict) else {}
    return (
        f"Track source job: {job_id}\n"
        "Source provider: MiniMax Music API\n"
        f"License note: {payload.get('license_note') or 'Generated with MiniMax music_generation API.'}\n"
        f"Model: {metadata.get('model') or 'unknown'}\n"
        f"Trace ID: {metadata.get('trace_id') or 'unknown'}\n"
        f"Instrumental: {metadata.get('instrumental') is True}\n"
        f"Vocals or lyrics: {str(metadata.get('vocals_or_lyrics') or 'unknown').strip().lower()}\n"
        f"Human instrumental review confirmed: {bool(metadata.get('human_instrumental_review_confirmed'))}\n"
        "Imported from local YTS Render artifacts after background music quality gate passed.\n"
        "Original signed provider URL is intentionally not copied with query parameters.\n"
    )


def word_like_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    current = []
    for char in value.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            token = "".join(current)
            if len(token) >= 3 and token not in tokens:
                tokens.append(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 3 and token not in tokens:
            tokens.append(token)
    return tokens
