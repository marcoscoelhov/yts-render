# Sprint 1: Pipeline Audit

## Current Pipeline

The runtime pipeline is orchestrated by `JobOrchestrator._steps()`, but domain behavior now lives in pipeline and operation modules:

1. `input_gate`
2. `topic_plan`
3. `script`
4. `scene_plan`
5. `asset_generation`
6. `tts`
7. `subtitle_alignment`
8. `background_music`
9. `render`
10. `monetization_readiness_gate`
11. `publish_to_review_hub`

Artifacts are persisted under `data/artifacts/<job_id>/` through `StorageManager`.
Database state is stored in the SQLAlchemy models in `app/models.py`.

## Integration Points

- LLM generation currently happens through `ProviderRegistry.creative`.
- Topic planning, script generation, and scene planning are the LLM-owned stages.
- The script stage is the first high-leverage quality gate, because bad text later contaminates scenes, TTS, subtitles, and render.
- Asset selection has semantic scoring and production thresholds.
- Subtitle, background music and render have explicit quality gates or validation reports.
- Review, scheduling, publish tracking, retention and channel sync live in `app/publication_ops.py`.

## Failures Observed In Exported Jobs

- Mixed language in scripts and subtitles, including English words inside pt-BR narration.
- SSML/markup leaked into subtitles, such as `</prosody`.
- Suspicious glued words and malformed phrases.
- Script attempts can fail and later complete under the same job event stream, making audit history hard to read.
- Assets can have `semantic_threshold_pass: false` and still reach review.
- Some generated image prompts are too generic for the narration beat.
- Final renders are technically valid, but encoding quality is low for 1080x1920 YouTube Shorts.

## Sprint 1 Scope Closed

This sprint established the implementation map. The current codebase has now applied the modularization:

- Provider abstraction belongs in `app/providers/`, with `app.providers` kept as the compatibility facade.
- Script validation belongs in `app/quality/script_gate.py`, called by `app/pipelines/script_repair.py` through `ScriptPipeline`.
- `JobOrchestrator` should delegate domain behavior to pipeline/operation modules instead of owning step internals.
- Fallback should be provider-level, but quality validation must be deterministic app code.

For current ownership boundaries, use `docs/app.md` and `docs/modularization-plan.md`.
