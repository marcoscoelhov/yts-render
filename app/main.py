from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import FallbackEvent, Job, RenderOutput, SceneAsset, TopicRequest
from app.orchestrator import orchestrator
from app.schemas import ReviewActionPayload, TopicRequestCreate


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))


def artifact_url(uri: str | None) -> str:
    if not uri:
        return "#"
    base = settings.artifacts_dir.resolve().as_posix()
    if uri.startswith("file://"):
        path = uri[7:]
        if path.startswith(base):
            return "/artifacts" + path[len(base):]
    return uri


templates.env.globals["artifact_url"] = artifact_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    orchestrator.start_worker()
    yield
    orchestrator.stop_worker()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/artifacts", StaticFiles(directory=str(settings.artifacts_dir)), name="artifacts")
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def _query_jobs(status: str | None, search: str | None, fallback: str | None, review: str | None):
    session = SessionLocal()
    try:
        fallback_count = (
            select(FallbackEvent.job_id, func.count(FallbackEvent.event_id).label("fallback_count"))
            .group_by(FallbackEvent.job_id)
            .subquery()
        )
        final_asset = (
            select(SceneAsset.job_id, func.sum(case((SceneAsset.selected.is_(True), 1), else_=0)).label("asset_count"))
            .group_by(SceneAsset.job_id)
            .subquery()
        )
        stmt = (
            select(
                Job,
                TopicRequest.seed_theme,
                RenderOutput.duration_ms,
                func.coalesce(fallback_count.c.fallback_count, 0),
                func.coalesce(final_asset.c.asset_count, 0),
            )
            .join(TopicRequest, TopicRequest.job_id == Job.job_id)
            .join(RenderOutput, RenderOutput.job_id == Job.job_id, isouter=True)
            .join(fallback_count, fallback_count.c.job_id == Job.job_id, isouter=True)
            .join(final_asset, final_asset.c.job_id == Job.job_id, isouter=True)
            .order_by(Job.created_at.desc())
        )
        if status:
            stmt = stmt.where(Job.status == status)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(or_(Job.job_id.like(pattern), TopicRequest.seed_theme.like(pattern), Job.topic_summary.like(pattern)))
        if fallback == "yes":
            stmt = stmt.where(func.coalesce(fallback_count.c.fallback_count, 0) > 0)
        if review:
            stmt = stmt.where(Job.review_state == review)
        return session.execute(stmt).all()
    finally:
        session.close()


@app.get("/", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    fallback: str | None = Query(default=None),
    review: str | None = Query(default=None),
):
    rows = _query_jobs(status=status, search=search, fallback=fallback, review=review)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": rows,
            "filters": {"status": status or "", "search": search or "", "fallback": fallback or "", "review": review or ""},
            "settings": settings,
        },
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_fragment(
    request: Request,
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    fallback: str | None = Query(default=None),
    review: str | None = Query(default=None),
):
    rows = _query_jobs(status=status, search=search, fallback=fallback, review=review)
    return templates.TemplateResponse(
        request,
        "jobs_table.html",
        {"rows": rows},
    )


@app.post("/jobs")
def create_job(
    seed_theme: str = Form(...),
    niche_id: str = Form(default="curiosidades"),
    language: str = Form(default="pt-BR"),
    target_duration_sec: int = Form(default=35),
    tone: str = Form(default="intrigante_direto"),
    cta_style: str = Form(default="none"),
    notes: str | None = Form(default=None),
    requested_angle: str | None = Form(default=None),
):
    payload = TopicRequestCreate(
        seed_theme=seed_theme,
        niche_id=niche_id,
        language=language,
        target_duration_sec=target_duration_sec,
        tone=tone,
        cta_style=cta_style,
        notes=notes,
        requested_angle=requested_angle,
    )
    job_id = orchestrator.create_job(payload.model_dump())
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/api/jobs/{job_id}")
def job_json(job_id: str):
    with SessionLocal() as session:
        details = orchestrator.get_job_details(session, job_id)
        return {
            "job": {
                "job_id": details["job"].job_id,
                "status": details["job"].status,
                "current_step": details["job"].current_step,
                "quality_summary": details["job"].quality_summary,
            },
            "topic_request": {
                "seed_theme": details["topic_request"].seed_theme if details["topic_request"] else None,
            },
            "render": {
                "video_uri": details["render"].video_uri if details["render"] else None,
                "duration_ms": details["render"].duration_ms if details["render"] else None,
            },
        }


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    with SessionLocal() as session:
        try:
            details = orchestrator.get_job_details(session, job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
    return templates.TemplateResponse(request, "job_detail.html", {"details": details, "settings": settings})


@app.post("/jobs/{job_id}/review")
def review_job(
    job_id: str,
    reviewer_identity: str = Form(default="tailscale:local-reviewer"),
    action: str = Form(...),
    reason_codes: str = Form(default=""),
    notes: str | None = Form(default=None),
    retry_step: str | None = Form(default=None),
):
    payload = ReviewActionPayload(
        reviewer_identity=reviewer_identity,
        action=action,
        reason_codes=[item.strip() for item in reason_codes.split(",") if item.strip()],
        notes=notes,
        retry_step=retry_step,
    )
    new_job_id = orchestrator.review_job(payload.model_dump(), job_id)
    redirect_to = f"/jobs/{new_job_id}" if new_job_id else f"/jobs/{job_id}"
    return RedirectResponse(url=redirect_to, status_code=303)


@app.get("/healthz")
def healthcheck():
    return {
        "status": "ok",
        "app": settings.app_name,
        "bind": f"{settings.app_host}:{settings.app_port}",
        "tailnet_url": f"https://{settings.tailscale_hostname}.{settings.tailnet_domain}",
    }
