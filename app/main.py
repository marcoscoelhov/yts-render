from __future__ import annotations

import json
import random
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.automation import AutomationService
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.manual_script import build_ready_script_notes, parse_ready_script
from app.models import Job, Script, TopicPlan, TopicRequest
from app.operational_settings import (
    apply_operational_settings,
    build_operational_settings_context,
    clear_operational_settings,
    parse_operational_form_values,
    save_operational_settings,
)
from app.orchestrator import FatalStepError, orchestrator
from app.hub_context import COMMON_SCHEDULE_TIMEZONES, HubContext
from app.routes.health import router as health_router
from pydantic import ValidationError

from app.schemas import PerformanceMetricPayload, PublicationSchedulePayload, ReviewActionPayload, TopicRequestCreate
from app.trends import TrendResearcher
from app.utils import path_from_uri


settings = get_settings()
automation_service = AutomationService(orchestrator)


def _request_path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _shared_template_context(request: Request) -> dict[str, object]:
    return {
        "settings": settings,
        "operational_settings": build_operational_settings_context(settings),
        "automation": automation_service.dashboard_context(),
        "viral_prompt_template": _viral_prompt_template(),
        "return_to": _request_path_with_query(request),
        "hub_defaults": {
            "niche_id": HUB_DEFAULT_NICHE,
            "seed_theme": "",
            "suggested_seed_theme": _default_seed_theme(),
            "target_duration_sec": HUB_RETENTION_OPTIMIZED_DURATION_SEC,
        },
    }


templates = Jinja2Templates(directory=str(settings.templates_dir), context_processors=[_shared_template_context])

HUB_DEFAULT_NICHE = "curiosidades"
HUB_RETENTION_OPTIMIZED_DURATION_SEC = 45
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
Objetivo: maximizar retencao, compartilhamento, comentarios e replay mental sem clickbait falso.
Use estrutura de copywriting agressiva para retenção:
1. Hook de choque nos primeiros 1-2 segundos: contraste, ameaça cognitiva, paradoxo ou fato que pareça impossivel mas seja verdadeiro.
2. Loop aberto imediato: plante uma pergunta mental que so sera fechada no final.
3. Promessa clara e especifica: diga/implique por que a pessoa precisa continuar assistindo agora.
4. Escalada em 3 a 5 beats: cada frase deve revelar algo mais forte, mais estranho ou mais visual que a anterior.
5. Payoff atrasado: guarde a explicacao mais surpreendente para o ultimo terco.
6. Fechamento com recontextualizacao forte ou loop: termine fazendo o espectador repensar o primeiro hook, com frase memoravel.
Retenção:
- cada frase deve criar motivo para assistir a proxima
- evite frase neutra, didatica ou enciclopedica quando puder virar tensão, contraste ou consequência
- use curiosidade concreta, causalidade e imagens mentais fortes
- priorize consequencia visual especifica, tensão concreta ou virada verificavel sobre lista de fatos soltos
SEO:
- palavra-chave principal cedo no titulo quando natural
- titulo com curiosidade especifica, 45 a 75 caracteres quando possivel
- evite titulo generico, caixa alta exagerada e promessa que o roteiro nao prove
Tom:
- rapido, intrigante, confiante e mais agressivo em retenção
- linguagem brasileira natural, com tensão e ritmo de Shorts
- sem enrolacao, sem aula morna, sem introducao generica
Proibido:
- nao comece com "voce sabia", "você sabia", "ja imaginou", "já imaginou", "nesse video" ou aberturas genericas equivalentes
- o hook deve abrir direto com contraste, consequencia, conflito ou fato especifico
- nao entregue a explicacao completa no primeiro beat; abra um loop e feche depois
- nao use clickbait falso: todo choque precisa ser provado no roteiro"""
HUB_SETTINGS_FILENAME = "hub_settings.json"
HUB_JOBS_PER_PAGE = 4
MAX_VIRAL_PROMPT_TEMPLATE_CHARS = 12000


hub_context = HubContext(settings, orchestrator, automation_service)
_job_status_label = hub_context._job_status_label
_schedule_status_label = hub_context._schedule_status_label
_job_flow_stage = hub_context._job_flow_stage
_job_next_action = hub_context._job_next_action
_publication_operational_status = hub_context._publication_operational_status
_job_progress_snapshot = hub_context._job_progress_snapshot
_job_action_guide = hub_context._job_action_guide
_job_list_context = hub_context._job_list_context
_schedule_display = hub_context._schedule_display
_ready_to_schedule_entries = hub_context._ready_to_schedule_entries
_effective_youtube_redirect_uri = hub_context._effective_youtube_redirect_uri
_youtube_integration_context = hub_context._youtube_integration_context
_tiktok_integration_context = hub_context._tiktok_integration_context
_publication_dashboard_context = hub_context._publication_dashboard_context
_calendar_context = hub_context._calendar_context
_resolve_job_id = hub_context._resolve_job_id















def artifact_url(uri: str | None) -> str:
    if not uri:
        return "#"
    if uri.startswith("file://"):
        try:
            path = path_from_uri(uri).resolve()
            relative_path = path.relative_to(settings.artifacts_dir.resolve())
        except (OSError, ValueError):
            return uri
        if not path.exists():
            return "#"
        return f"/artifacts/{relative_path.as_posix()}"
    return uri


templates.env.globals["artifact_url"] = artifact_url
templates.env.globals["job_status_label"] = _job_status_label
templates.env.globals["schedule_status_label"] = _schedule_status_label
templates.env.globals["job_flow_stage"] = _job_flow_stage
templates.env.globals["job_next_action"] = _job_next_action
templates.env.globals["publication_operational_status"] = _publication_operational_status
templates.env.globals["job_progress_snapshot"] = _job_progress_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    apply_operational_settings(settings)
    orchestrator.start_worker()
    yield
    orchestrator.stop_worker()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(health_router)
app.mount("/artifacts", StaticFiles(directory=str(settings.artifacts_dir)), name="artifacts")
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def _authorized_request(request: Request) -> bool:
    if not settings.hub_auth_token:
        return True
    supplied = request.headers.get("x-yts-hub-token")
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    if not supplied and request.method in {"GET", "HEAD"}:
        supplied = request.cookies.get("yts_hub_token")
    return supplied == settings.hub_auth_token


def _optional_float(value: str | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _optional_int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


@app.middleware("http")
async def require_hub_auth(request: Request, call_next):
    if request.url.path.startswith("/healthz") or request.url.path.startswith("/static"):
        return await call_next(request)
    if request.method == "OPTIONS" or _authorized_request(request):
        return await call_next(request)
    return PlainTextResponse("unauthorized", status_code=401)


































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


def _safe_return_to(return_to: str | None, default: str = "/") -> str:
    target = (return_to or "").strip()
    if not target or not target.startswith("/") or target.startswith("//") or any(char in target for char in "\r\n"):
        return default
    return target


def _redirect_back(return_to: str | None, params: dict[str, str] | None = None, default: str = "/") -> RedirectResponse:
    target = _safe_return_to(return_to, default=default)
    if params:
        path, fragment_separator, fragment = target.partition("#")
        separator = "&" if "?" in path else "?"
        target = f"{path}{separator}{urlencode(params)}"
        if fragment_separator:
            target = f"{target}#{fragment}"
    return RedirectResponse(url=target, status_code=303)


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


def _trend_seed_theme(niche_id: str) -> tuple[str, str | None, str | None, dict[str, object] | None]:
    trend = TrendResearcher().find_topic(niche_id)
    if trend is None:
        fallback_theme = _default_seed_theme()
        return (
            fallback_theme,
            None,
            "trend_research=unavailable\ntrend_source=fallback_pool\ntrend_status=no_live_trend_candidate",
            {
                "trend_research": "unavailable",
                "source": "fallback_pool",
                "status": "no_live_trend_candidate",
                "fallback_seed_theme": fallback_theme,
            },
        )
    return trend.topic, trend.requested_angle, trend.as_notes(), trend.as_report()


def _compose_hub_notes(input_mode: str, notes: str | None) -> str:
    normalized_mode = "script" if input_mode == "script" else ("title" if input_mode == "title" else "theme")
    if normalized_mode == "title":
        mode_note = "Entrada do hub: titulo completo fornecido pelo usuario. Preserve a promessa central, mas reescreva e otimize se necessario."
    elif normalized_mode == "script":
        mode_note = "Entrada do hub: roteiro pronto fornecido pelo usuario. Preserve como fonte de verdade editorial; nao gere outro roteiro."
    else:
        mode_note = "Entrada do hub: tema bruto fornecido pelo usuario. Transforme em pauta e titulo fortes."
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
    page: int = Query(default=1),
    per_page: int = Query(default=HUB_JOBS_PER_PAGE),
):
    list_context = _job_list_context(status=status, search=search, fallback=fallback, review=review, page=page, per_page=per_page)
    publication_context = _publication_dashboard_context(request, limit=4)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            **list_context,
            "workflow_summary": publication_context["metrics"],
            "youtube_integration": publication_context["integration"],
            "automation": publication_context["automation"],
            "hub_defaults": {
                "niche_id": HUB_DEFAULT_NICHE,
                "seed_theme": "",
                "suggested_seed_theme": _default_seed_theme(),
                "target_duration_sec": HUB_RETENTION_OPTIMIZED_DURATION_SEC,
            },
            "viral_prompt_template": _viral_prompt_template(),
            "calendar_url": "/calendar",
            "settings": settings,
        },
    )


@app.post("/hub/prompt")
def update_hub_prompt(
    viral_prompt_template: str | None = Form(default=None),
    action: str = Form(default="save"),
    return_to: str | None = Form(default=None),
):
    if action == "reset":
        _save_viral_prompt_template(DEFAULT_VIRAL_PROMPT_TEMPLATE)
    else:
        _save_viral_prompt_template(viral_prompt_template)
    return _redirect_back(return_to)


@app.post("/operations/settings")
async def update_operational_settings(request: Request):
    form = await request.form()
    return_to = str(form.get("return_to") or "")
    action = str(form.get("action") or "save")
    try:
        if action == "reset":
            clear_operational_settings(settings)
        else:
            save_operational_settings(settings, parse_operational_form_values(dict(form)))
    except (ValueError, ValidationError) as exc:
        return _redirect_back(return_to, {"settings_error": str(exc)}, default="/#publication-hub")
    return _redirect_back(return_to, {"settings_saved": "1"}, default="/#publication-hub")


@app.get("/jobs", response_class=HTMLResponse)
def jobs_fragment(
    request: Request,
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    fallback: str | None = Query(default=None),
    review: str | None = Query(default=None),
    page: int = Query(default=1),
    per_page: int = Query(default=HUB_JOBS_PER_PAGE),
):
    list_context = _job_list_context(status=status, search=search, fallback=fallback, review=review, page=page, per_page=per_page)
    return templates.TemplateResponse(
        request,
        "jobs_table.html",
        list_context,
    )


@app.get("/publication-hub", response_class=HTMLResponse)
def publication_dashboard_fragment(request: Request):
    return templates.TemplateResponse(
        request,
        "publication_dashboard.html",
        {
            **_publication_dashboard_context(request),
            "settings": settings,
        },
    )


@app.post("/automation/toggle")
def toggle_automation(enabled: bool = Form(default=False), return_to: str | None = Form(default=None)):
    automation_service.set_automation_enabled(enabled)
    return _redirect_back(return_to, default="/#publication-hub")


@app.post("/automation/run")
def run_automation_now(force: bool = Form(default=False), return_to: str | None = Form(default=None)):
    result = automation_service.run_daily_cycle(force=force)
    if result and result.get("status") == "failed":
        return _redirect_back(return_to, {"automation_error": result.get("error") or "failed"}, default="/#publication-hub")
    return _redirect_back(return_to, default="/#publication-hub")


@app.post("/automation/ready-scripts/import")
async def import_ready_scripts(
    ready_script_batch: str = Form(default=""),
    ready_script_file: UploadFile | None = File(default=None),
    fact_check_confirmed: bool = Form(default=False),
    return_to: str | None = Form(default=None),
):
    if not fact_check_confirmed:
        raise HTTPException(status_code=422, detail="fact_check_confirmed is required for automation-ready script batches")
    file_text = ""
    if ready_script_file and ready_script_file.filename:
        file_text = (await ready_script_file.read()).decode("utf-8")
    raw_text = "\n\n".join(part for part in [ready_script_batch, file_text] if part and part.strip())
    result = automation_service.import_ready_script_batch(raw_text, fact_check_confirmed=fact_check_confirmed)
    params = {"imported": str(result.imported)}
    if result.errors:
        params["errors"] = str(len(result.errors))
    return _redirect_back(return_to, params=params)


@app.get("/youtube/connect")
def connect_youtube(request: Request):
    try:
        authorization_url = orchestrator.youtube.authorization_url(_effective_youtube_redirect_uri(request))
    except FatalStepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=authorization_url, status_code=303)


@app.get("/youtube/oauth/callback")
def youtube_oauth_callback(request: Request, code: str | None = Query(default=None), state: str | None = Query(default=None), error: str | None = Query(default=None)):
    if error:
        raise HTTPException(status_code=400, detail=f"youtube oauth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="youtube oauth callback missing code/state")
    try:
        orchestrator.youtube.exchange_code(code=code, state=state)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/youtube/disconnect")
def disconnect_youtube():
    orchestrator.youtube.disconnect()
    return RedirectResponse(url="/", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def publication_calendar(request: Request, month: str | None = Query(default=None)):
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            **_calendar_context(month),
            "settings": settings,
        },
    )


@app.post("/calendar/schedule")
def schedule_publication_from_calendar(
    job_id: str = Form(...),
    scheduled_date: str = Form(...),
    scheduled_time: str = Form(default="15:00"),
    timezone: str = Form(default="America/Sao_Paulo"),
    youtube_visibility: str = Form(default="private"),
    notes: str | None = Form(default=None),
    month: str | None = Form(default=None),
):
    try:
        scheduled_day = date.fromisoformat(scheduled_date)
        payload = PublicationSchedulePayload(
            scheduled_for_local=f"{scheduled_day.isoformat()}T{scheduled_time}",
            timezone=timezone,
            youtube_visibility=youtube_visibility,
            notes=notes,
        )
        orchestrator.schedule_publication(job_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="scheduled_date must use YYYY-MM-DD") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except FatalStepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target_month = month or scheduled_day.strftime("%Y-%m")
    return RedirectResponse(url=f"/calendar?{urlencode({'month': target_month})}", status_code=303)


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
    ready_script_text: str | None = Form(default=None),
    ready_script_fact_check_confirmed: bool = Form(default=False),
):
    selected_angle = (custom_angle or "").strip() or (requested_angle or "").strip()
    if selected_angle == "auto":
        selected_angle = ""
    selected_niche = niche_id or HUB_DEFAULT_NICHE
    trend_notes = None
    normalized_mode = "script" if input_mode == "script" else ("title" if input_mode == "title" else "theme")
    if normalized_mode == "script":
        if not ready_script_fact_check_confirmed:
            raise HTTPException(status_code=422, detail="ready_script_fact_check_confirmed is required for Roteiro Pronto")
        try:
            ready_script = parse_ready_script(ready_script_text or "", fact_check_confirmed=ready_script_fact_check_confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        selected_seed_theme = str(ready_script.script["title"]).strip()
        combined_notes = build_ready_script_notes(notes, ready_script.raw_text, ready_script_fact_check_confirmed)
        trend_report = None
    elif seed_theme.strip():
        selected_seed_theme = seed_theme.strip()
        trend_report = None
    else:
        selected_seed_theme, trend_angle, trend_notes, trend_report = _trend_seed_theme(selected_niche)
        selected_angle = selected_angle or trend_angle or ""
        combined_notes = "\n\n".join(part for part in [trend_notes, notes] if part)
    if normalized_mode != "script" and seed_theme.strip():
        combined_notes = "\n\n".join(part for part in [trend_notes, notes] if part)
    try:
        payload = TopicRequestCreate(
            seed_theme=selected_seed_theme,
            niche_id=selected_niche,
            language=language,
            target_duration_sec=target_duration_sec,
            tone=tone,
            cta_style=cta_style,
            notes=_compose_hub_notes(normalized_mode, combined_notes),
            requested_angle=selected_angle or None,
        )
        job_id = orchestrator.create_job(payload.model_dump())
        if trend_report is not None:
            orchestrator.storage.persist_json(job_id, "trend_research.json", trend_report)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": error["loc"],
                    "msg": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors()
            ],
        ) from exc
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/api/jobs/{job_id}")
def job_json(job_id: str):
    with SessionLocal() as session:
        resolved_job_id = _resolve_job_id(session, job_id)
        details = orchestrator.get_job_details(session, resolved_job_id)
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
            "progress": details["progress"],
        }


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    with SessionLocal() as session:
        try:
            resolved_job_id = _resolve_job_id(session, job_id)
            details = orchestrator.get_job_details(session, resolved_job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
    youtube_integration = _youtube_integration_context(request)
    publication_schedule_display = _schedule_display(details.get("publication_schedule"))
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "details": details,
            "settings": settings,
            "publication_schedule_display": publication_schedule_display,
            "common_schedule_timezones": COMMON_SCHEDULE_TIMEZONES,
            "youtube_integration": youtube_integration,
            "review_error": request.query_params.get("review_error"),
            "publish_error": request.query_params.get("publish_error"),
            "action_guide": _job_action_guide(
                details["job"],
                details.get("monetization_report"),
                publication_schedule_display,
                youtube_integration,
            ),
        },
    )


@app.post("/jobs/{job_id}/review")
def review_job(
    request: Request,
    job_id: str,
    reviewer_identity: str = Form(default="tailscale:local-reviewer"),
    action: str = Form(...),
    reason_codes: list[str] | None = Form(default=None),
    confirmation_codes: list[str] | None = Form(default=None),
    rights_confirmed: bool = Form(default=False),
    ai_disclosure_confirmed: bool = Form(default=False),
    fact_review_confirmed: bool = Form(default=False),
    metadata_confirmed: bool = Form(default=False),
    originality_confirmed: bool = Form(default=False),
    notes: str | None = Form(default=None),
):
    parsed_reason_codes = []
    for raw_reason in reason_codes or []:
        parsed_reason_codes.extend(item.strip() for item in str(raw_reason).split(",") if item.strip())
    for confirmation_code in confirmation_codes or []:
        code = str(confirmation_code).strip()
        if code and code not in parsed_reason_codes:
            parsed_reason_codes.append(code)
    for enabled, code in [
        (rights_confirmed, "rights_confirmed"),
        (ai_disclosure_confirmed, "ai_disclosure_confirmed"),
        (fact_review_confirmed, "fact_review_confirmed"),
        (metadata_confirmed, "metadata_confirmed"),
        (originality_confirmed, "originality_confirmed"),
    ]:
        if enabled and code not in parsed_reason_codes:
            parsed_reason_codes.append(code)
    payload = ReviewActionPayload(
        reviewer_identity=reviewer_identity,
        action=action,
        reason_codes=parsed_reason_codes,
        notes=notes,
    )
    try:
        new_job_id = orchestrator.review_job(payload.model_dump(), job_id)
    except FatalStepError as exc:
        redirect_to = f"/jobs/{job_id}?{urlencode({'review_error': str(exc)})}"
        return RedirectResponse(url=redirect_to, status_code=303)
    redirect_to = f"/jobs/{new_job_id}" if new_job_id else f"/jobs/{job_id}"
    return RedirectResponse(url=redirect_to, status_code=303)


@app.post("/jobs/{job_id}/publish-metadata")
def update_publish_metadata(
    job_id: str,
    title: str = Form(default=""),
    description: str = Form(default=""),
    hashtags: str = Form(default=""),
):
    try:
        orchestrator.update_publish_metadata(
            job_id,
            {
                "title": title,
                "description": description,
                "hashtags": hashtags,
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except FatalStepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/publish")
def publish_job(
    request: Request,
    job_id: str,
    youtube_video_id: str | None = Form(default=None),
    youtube_url: str | None = Form(default=None),
):
    try:
        orchestrator.publish_job(job_id, youtube_video_id=youtube_video_id, youtube_url=youtube_url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except FatalStepError as exc:
        redirect_to = f"/jobs/{job_id}?{urlencode({'publish_error': str(exc)})}"
        return RedirectResponse(url=redirect_to, status_code=303)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/schedule")
def schedule_job_publication(
    job_id: str,
    action: str = Form(default="schedule"),
    scheduled_for_local: str | None = Form(default=None),
    timezone: str = Form(default="UTC"),
    youtube_visibility: str = Form(default="private"),
    notes: str | None = Form(default=None),
):
    try:
        if action == "clear":
            orchestrator.clear_publication_schedule(job_id)
        else:
            payload = PublicationSchedulePayload(
                scheduled_for_local=scheduled_for_local or "",
                timezone=timezone,
                youtube_visibility=youtube_visibility,
                notes=notes,
            )
            orchestrator.schedule_publication(job_id, payload.model_dump())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except FatalStepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/reopen-publication")
def reopen_job_publication(job_id: str):
    try:
        orchestrator.reopen_publication_for_republish(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except FatalStepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/performance")
def record_performance(
    job_id: str,
    source: str = Form(default="youtube_studio_manual"),
    retention_percent: str | None = Form(default=None),
    viewed_vs_swiped_away_percent: str | None = Form(default=None),
    rewatch_rate: str | None = Form(default=None),
    likes: str | None = Form(default=None),
    shares: str | None = Form(default=None),
    comments: str | None = Form(default=None),
    rpm_usd: str | None = Form(default=None),
    monetization_status: str | None = Form(default=None),
    notes: str | None = Form(default=None),
):
    try:
        payload = PerformanceMetricPayload(
            source=source,
            retention_percent=_optional_float(retention_percent),
            viewed_vs_swiped_away_percent=_optional_float(viewed_vs_swiped_away_percent),
            rewatch_rate=_optional_float(rewatch_rate),
            likes=_optional_int(likes),
            shares=_optional_int(shares),
            comments=_optional_int(comments),
            rpm_usd=_optional_float(rpm_usd),
            monetization_status=monetization_status,
            notes=notes,
        )
        orchestrator.record_performance_metrics(job_id, payload.model_dump())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
