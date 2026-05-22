# Sprints 4-14 Task List

## Sprint 4: Scene Plan And Prompt Gate

- [x] Add deterministic scene plan gate.
- [x] Validate scene count, token bounds, narration text, image prompt presence, and no-text constraints.
- [x] Persist rejected scene plans to `scene_plan_rejected.json`.
- [x] Store scene gate metrics in `job.quality_summary.scene_plan`.

## Sprint 5: Per-Scene Asset Quality Gate

- [x] Enforce semantic thresholds per selected scene asset.
- [x] Stop selecting low-score assets in production instead of rendering anyway.
- [x] Persist rejected candidates and `asset_quality_report.json`.
- [x] Store asset gate metrics in `job.quality_summary.assets`.

## Sprint 6: Pluggable Fallback By Stage

- [x] Keep MiniMax as primary LLM provider.
- [x] Keep provider registry configurable through `YTS_LLM_*`.
- [x] Repair scripts with the primary provider first.
- [x] Use fallback provider for script repair when enabled.
- [x] Use scene fallback planner when generated scenes fail coverage or quality.

## Sprint 7: Subtitle Gate

- [x] Add deterministic subtitle gate.
- [x] Block markup/SSML leakage.
- [x] Block invalid timing, empty text, overly long blocks, and obvious truncation.
- [x] Persist `subtitle_quality_report.json` on failure.
- [x] Store subtitle gate metrics in `job.quality_summary.subtitles`.

## Sprint 8: TTS And Real Duration

- [x] Keep real audio measurement after TTS.
- [x] Fit audio duration when outside accepted range.
- [x] Store TTS duration and loudness metadata in quality summary.
- [x] Preserve subtitle timing scaling when audio speed is adjusted.

## Sprint 9: Render Gate

- [x] Add ffprobe validation.
- [x] Decode final video with ffmpeg before review.
- [x] Validate streams, resolution, duration drift, filesize, and minimum bitrate.
- [x] Render final AAC at 48 kHz.
- [x] Persist `render_quality_report.json` on failure.

## Sprint 10: Attempts And States

- [x] Add specific quality failure states:
  - `script_quality_failed`
  - `scene_plan_quality_failed`
  - `asset_quality_failed`
  - `subtitle_quality_failed`
  - `render_quality_failed`
- [x] Add event ids to event log entries.
- [x] Preserve step executions as attempt-level audit records.
- [x] Stop reprocessing terminal quality-failed states automatically.

## Sprint 11: Human Review Panel

- [x] Show quality summaries in job detail.
- [x] Show assets, scores, fallbacks, errors, events, and ffmpeg logs.
- [x] Add `approved_for_publish` flow after review approval.
- [x] Add manual publish form for approved jobs.

## Sprint 12: YouTube Package

- [x] Generate `publish_package.json`.
- [x] Include title, description, hashtags, language, media URIs, and checklist.
- [x] Include quality summary in publication package.
- [x] Add package to `job_manifest.json` artifact index.

## Sprint 13: YouTube Publication Tracking

- [x] Add manual publish endpoint.
- [x] Persist `publish_result.json`.
- [x] Track YouTube mode, video id, URL, and published timestamp.
- [x] Move approved jobs to `published` when publication is recorded.

## Sprint 14: Production And Operation

- [x] Add production-oriented env flags for gates, bitrate, and YouTube mode.
- [x] Keep `.env.example` aligned with new settings.
- [x] Keep secrets out of git through existing `.gitignore`.
- [x] Validate the full mock pipeline with all gates enabled.
- [x] Real YouTube API upload is implemented behind explicit API mode, OAuth connection and hub settings.
- [ ] PostgreSQL deployment and migration hardening remain a separate deployment task.
