from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


TERMINAL_STATUSES = {
    "ready_for_upload",
    "monetization_review",
    "blocked_for_monetization",
    "failed",
    "script_quality_failed",
    "scene_plan_quality_failed",
    "asset_quality_failed",
    "subtitle_quality_failed",
    "render_quality_failed",
    "cancelled",
}


def _configure_environment(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["YTS_DATA_DIR"] = str(data_dir)
    os.environ["YTS_DATABASE_URL"] = f"sqlite:///{data_dir / 'yts_render.db'}"
    os.environ["YTS_USE_MOCK_PROVIDERS"] = "false"
    os.environ.setdefault("YTS_STRICT_MINIMAX_VALIDATION", "false")


def _job_row(db_path: Path, job_id: str) -> tuple[str, str | None, dict[str, Any] | None] | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "select status, failure_reason, quality_summary from jobs where job_id=?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    quality_summary = json.loads(row[2]) if row[2] else None
    return row[0], row[1], quality_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated end-to-end smoke job with real providers.")
    parser.add_argument("--seed-theme", default="curiosidades", help="Seed theme for the generated Short.")
    parser.add_argument("--target-duration-sec", type=int, default=35)
    parser.add_argument("--tone", default="intrigante_direto")
    parser.add_argument("--cta-style", default="none")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    data_dir = args.data_dir or Path(f"data-real-smoke-{timestamp}")
    _configure_environment(data_dir)

    from app.db import init_db
    from app.orchestrator import JobOrchestrator
    from app.schemas import TopicRequestCreate

    init_db()
    orchestrator = JobOrchestrator()
    job_id = orchestrator.create_job(
        TopicRequestCreate(
            seed_theme=args.seed_theme,
            target_duration_sec=args.target_duration_sec,
            tone=args.tone,
            cta_style=args.cta_style,
        )
    )
    print(json.dumps({"event": "job_created", "job_id": job_id, "data_dir": str(data_dir)}, ensure_ascii=False), flush=True)

    status = orchestrator.process_job(job_id)
    print(json.dumps({"event": "process_job_returned", "job_id": job_id, "status": status}, ensure_ascii=False), flush=True)

    db_path = data_dir / "yts_render.db"
    started_at = time.monotonic()
    row = _job_row(db_path, job_id)
    while time.monotonic() - started_at < args.timeout_sec:
        row = _job_row(db_path, job_id)
        if row and row[0] in TERMINAL_STATUSES:
            break
        time.sleep(2)

    if not row:
        print(json.dumps({"event": "job_missing", "job_id": job_id}, ensure_ascii=False), file=sys.stderr)
        return 2

    final_status, failure_reason, quality_summary = row
    artifacts_dir = data_dir / "artifacts" / job_id
    final_video = artifacts_dir / "render" / "final.mp4"
    result = {
        "event": "job_finished",
        "job_id": job_id,
        "status": final_status,
        "failure_reason": failure_reason,
        "data_dir": str(data_dir),
        "final_video": str(final_video) if final_video.exists() else None,
        "quality_summary": quality_summary,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if final_status in {"ready_for_upload", "monetization_review", "blocked_for_monetization"} and final_video.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
