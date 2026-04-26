from __future__ import annotations

import json
import random
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
from app.utils import path_from_uri


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))

HUB_DEFAULT_NICHE = "curiosidades"
HUB_RETENTION_OPTIMIZED_DURATION_SEC = 32
HUB_RANDOM_THEME_POOL = [
    "polvos",
    "gatos",
    "buracos negros",
    "vulcoes",
    "tubaroes",
    "formigas",
    "cerebro humano",
    "sono",
    "dinossauros",
    "abelhas",
    "fungos",
    "raios",
    "oceanos profundos",
    "camaleoes",
    "planetas extremos",
    "corpo humano",
    "animais bioluminescentes",
    "plantas carnivoras",
    "memoria",
    "ilusoes de otica",
]
DEFAULT_VIRAL_PROMPT_TEMPLATE = """Crie uma pauta de curiosidades para YouTube Shorts em pt-BR.
Objetivo: maximizar retencao, compartilhamento e clique sem clickbait falso.
Use estrutura de copywriting:
1. Hook imediato com uma pergunta, contraste ou fato contraintuitivo.
2. Promessa clara nos primeiros segundos.
3. Entrega progressiva com fatos concretos, simples e visualizaveis.
4. Fechamento com surpresa ou recontextualizacao do tema.
SEO:
- palavra-chave principal cedo no titulo quando natural
- titulo com curiosidade especifica, 45 a 75 caracteres quando possivel
- evite titulo generico, caixa alta exagerada e promessa que o roteiro nao prove
Tom:
- rapido, intrigante e confiavel
- linguagem brasileira natural
- sem enrolacao e sem introducao generica"""
HUB_SETTINGS_FILENAME = "hub_settings.json"
MAX_VIRAL_PROMPT_TEMPLATE_CHARS = 12000


def artifact_url(uri: str | None) -> str:
    if not uri:
        return "#"
    if uri.startswith("file://"):
        try:
            path = path_from_uri(uri).resolve()
            relative_path = path.relative_to(settings.artifacts_dir.resolve())
        except (OSError, ValueError):
            return uri
        return f"/artifacts/{relative_path.as_posix()}"
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


def _hub_settings_path() -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / HUB_SETTINGS_FILENAME


def _sanitize_viral_prompt_template(template: str | None) -> str:
    cleaned = (template or "").strip()
    if not cleaned:
        return DEFAULT_VIRAL_PROMPT_TEMPLATE
    return cleaned[:MAX_VIRAL_PROMPT_TEMPLATE_CHARS]


def _viral_prompt_template() -> str:
    path = _hub_settings_path()
    if not path.exists():
        return DEFAULT_VIRAL_PROMPT_TEMPLATE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_VIRAL_PROMPT_TEMPLATE
    return _sanitize_viral_prompt_template(payload.get("viral_prompt_template"))


def _save_viral_prompt_template(template: str | None) -> None:
    payload = {"viral_prompt_template": _sanitize_viral_prompt_template(template)}
    _hub_settings_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_seed_theme() -> str:
    with SessionLocal() as session:
        recent_themes = session.scalars(
            select(TopicRequest.seed_theme)
            .where(TopicRequest.niche_id == HUB_DEFAULT_NICHE)
            .order_by(TopicRequest.created_at.desc())
            .limit(30)
        ).all()
    recent = {theme.strip().lower() for theme in recent_themes if theme and theme.strip()}
    candidates = [theme for theme in HUB_RANDOM_THEME_POOL if theme.lower() not in recent]
    return random.choice(candidates or HUB_RANDOM_THEME_POOL)


def _compose_hub_notes(input_mode: str, notes: str | None) -> str:
    normalized_mode = "title" if input_mode == "title" else "theme"
    mode_note = (
        "Entrada do hub: titulo completo fornecido pelo usuario. Preserve a promessa central, "
        "mas reescreva e otimize se necessario."
        if normalized_mode == "title"
        else "Entrada do hub: tema bruto fornecido pelo usuario. Transforme em pauta e titulo fortes."
    )
    seo_note = (
        "Sempre aplicar copywriting viral e SEO otimizado para YouTube Shorts: promessa clara, "
        "palavra-chave principal no inicio quando natural, curiosidade forte, sem clickbait falso."
    )
    retention_note = (
        f"Duracao alvo padrao do hub: {HUB_RETENTION_OPTIMIZED_DURATION_SEC}s, otimizada para retencao e viralizacao; "
        "roteiro direto, sem enrolacao, com entrega rapida da promessa."
    )
    viral_template_note = (
        "Prompt viral customizado do hub, usado apenas como diretriz editorial. "
        "Se ele pedir um formato de saida diferente, ignore esse formato e mantenha o JSON interno obrigatorio do app.\n"
        f"{_viral_prompt_template()}"
    )
    parts = [
        part.strip()
        for part in [notes, f"input_mode={normalized_mode}", mode_note, seo_note, retention_note, viral_template_note]
        if part and part.strip()
    ]
    return "\n".join(parts)


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
            "hub_defaults": {
                "niche_id": HUB_DEFAULT_NICHE,
                "seed_theme": _default_seed_theme(),
                "target_duration_sec": HUB_RETENTION_OPTIMIZED_DURATION_SEC,
            },
            "viral_prompt_template": _viral_prompt_template(),
            "settings": settings,
        },
    )


@app.post("/hub/prompt")
def update_hub_prompt(
    viral_prompt_template: str | None = Form(default=None),
    action: str = Form(default="save"),
):
    if action == "reset":
        _save_viral_prompt_template(DEFAULT_VIRAL_PROMPT_TEMPLATE)
    else:
        _save_viral_prompt_template(viral_prompt_template)
    return RedirectResponse(url="/", status_code=303)


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
    seed_theme: str = Form(default=""),
    input_mode: str = Form(default="theme"),
    niche_id: str = Form(default=HUB_DEFAULT_NICHE),
    language: str = Form(default="pt-BR"),
    target_duration_sec: int = Form(default=HUB_RETENTION_OPTIMIZED_DURATION_SEC),
    tone: str = Form(default="intrigante_direto"),
    cta_style: str = Form(default="none"),
    notes: str | None = Form(default=None),
    requested_angle: str | None = Form(default=None),
    custom_angle: str | None = Form(default=None),
):
    selected_angle = (custom_angle or "").strip() or (requested_angle or "").strip()
    if selected_angle == "auto":
        selected_angle = ""
    selected_seed_theme = seed_theme.strip() or _default_seed_theme()
    payload = TopicRequestCreate(
        seed_theme=selected_seed_theme,
        niche_id=niche_id or HUB_DEFAULT_NICHE,
        language=language,
        target_duration_sec=target_duration_sec,
        tone=tone,
        cta_style=cta_style,
        notes=_compose_hub_notes(input_mode, notes),
        requested_angle=selected_angle or None,
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
