from __future__ import annotations

import calendar
import json
import random
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import FallbackEvent, Job, PublicationSchedule, RenderOutput, SceneAsset, Script, TopicRequest
from app.orchestrator import FatalStepError, orchestrator
from pydantic import ValidationError

from app.schemas import PerformanceMetricPayload, PublicationSchedulePayload, ReviewActionPayload, TopicRequestCreate
from app.trends import TrendResearcher
from app.utils import path_from_uri


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))

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
- priorize "isso muda como você enxerga X" sobre lista de fatos soltos
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
HUB_JOBS_PER_PAGE = 20
MAX_VIRAL_PROMPT_TEMPLATE_CHARS = 12000
COMMON_SCHEDULE_TIMEZONES = [
    "UTC",
    "America/Sao_Paulo",
    "America/New_York",
    "Europe/London",
]


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

JOB_STATUS_LABELS = {
    "queued": "Na fila",
    "running": "Gerando vídeo",
    "script_quality_failed": "Falhou no roteiro",
    "scene_plan_quality_failed": "Falhou nas cenas",
    "asset_quality_failed": "Falhou nos assets",
    "subtitle_quality_failed": "Falhou nas legendas",
    "render_quality_failed": "Falhou no render",
    "monetization_review": "Precisa revisão",
    "blocked_for_monetization": "Bloqueado para publicar",
    "ready_for_upload": "Pronto para aprovar",
    "approved_for_publish": "Aprovado para publicar",
    "published": "Publicado",
    "approved": "Aprovado",
    "rejected": "Rejeitado",
    "failed": "Falhou",
}

SCHEDULE_STATUS_LABELS = {
    "scheduled": "Programado",
    "publishing": "Publicando",
    "publish_failed": "Falhou no upload",
    "published": "Publicado",
    "cancelled": "Programação limpa",
}


def _job_status_label(status: str | None) -> str:
    return JOB_STATUS_LABELS.get(str(status or ""), str(status or "-"))


def _schedule_status_label(status: str | None) -> str:
    return SCHEDULE_STATUS_LABELS.get(str(status or ""), str(status or "-"))


def _job_flow_stage(job_status: str | None, schedule_status: str | None = None) -> str:
    normalized = str(job_status or "")
    if schedule_status == "published" or normalized == "published":
        return "Publicado"
    if schedule_status in {"scheduled", "publishing", "publish_failed"} or normalized == "approved_for_publish":
        return "Programação"
    if normalized in {"monetization_review", "ready_for_upload"}:
        return "Aprovação"
    if normalized in {"queued", "running"}:
        return "Geração"
    if normalized in {"blocked_for_monetization", "rejected"}:
        return "Bloqueado"
    if normalized.endswith("_failed") or normalized == "failed":
        return "Falhou"
    return "Geração"


def _job_next_action(job_status: str | None, schedule_status: str | None = None) -> str:
    normalized = str(job_status or "")
    if schedule_status == "published" or normalized == "published":
        return "Registrar métricas do vídeo e seguir para o próximo."
    if schedule_status == "publish_failed":
        return "Abrir o job, revisar o erro e repetir a publicação."
    if schedule_status == "publishing":
        return "Aguardar o upload terminar e conferir o resultado."
    if schedule_status == "scheduled":
        return "Conferir data e hora; o worker publica quando o horário vencer."
    if normalized in {"monetization_review", "ready_for_upload"}:
        return "Abrir o job, revisar checklist e clicar em Aprovar."
    if normalized == "approved_for_publish":
        return "Definir data no bloco Agenda ou clicar em Publicar agora."
    if normalized == "blocked_for_monetization":
        return "Rejeitar ou recriar o job após corrigir os bloqueios."
    if normalized == "rejected":
        return "Criar novo job completo ou ajustar o tema."
    if normalized.endswith("_failed") or normalized == "failed":
        return "Abrir o job, ler o erro e tentar novamente."
    if normalized == "running":
        return "Aguardar a geração terminar."
    return "Aguardar a próxima etapa automática."


def _job_action_guide(
    job: Job,
    monetization_report: dict[str, object] | None,
    schedule_display: dict[str, str | None] | None,
    youtube_integration: dict[str, object],
) -> dict[str, str]:
    job_status = str(job.status or "")
    schedule_status = str((schedule_display or {}).get("status") or "")
    if schedule_status == "published" or job_status == "published":
        return {
            "step": "4. Publicado",
            "title": "Upload concluído",
            "body": "O vídeo já foi publicado. Use a seção de performance para registrar os números do YouTube Studio.",
        }
    if schedule_status == "publish_failed":
        return {
            "step": "4. Repetir publicação",
            "title": "Upload falhou",
            "body": "Revise a tentativa de publicação logo abaixo e dispare um novo upload quando o erro estiver claro.",
        }
    if schedule_status == "scheduled":
        return {
            "step": "3. Programado",
            "title": "O vídeo já está na agenda",
            "body": "Confira data, hora e visibilidade. Se quiser postar imediatamente, use o botão de publicar agora.",
        }
    if job_status == "approved_for_publish":
        publish_mode = str(youtube_integration.get("publish_mode") or "manual")
        helper = "No modo api, o worker publica no horário salvo." if publish_mode == "api" else "No modo manual, o hub só registra o que você publicou no Studio."
        return {
            "step": "3. Programar ou publicar",
            "title": "A aprovação terminou",
            "body": f"Agora o vídeo já pode entrar na agenda. Preencha data, hora e visibilidade no bloco Agenda. {helper}",
        }
    if job_status in {"monetization_review", "ready_for_upload"}:
        hard_blockers = list((monetization_report or {}).get("hard_blockers") or [])
        manual_required = list((monetization_report or {}).get("manual_required") or [])
        if hard_blockers:
            return {
                "step": "2. Corrigir antes de aprovar",
                "title": "Ainda não dá para aprovar",
                "body": "O relatório de monetização encontrou bloqueios. Revise os bloqueios e rejeite ou regenere o job.",
            }
        if manual_required:
            return {
                "step": "2. Aprovar vídeo",
                "title": "Falta revisão humana",
                "body": "Marque as confirmações exigidas na seção Review e clique em Aprovar. A agenda só libera depois disso.",
            }
        return {
            "step": "2. Aprovar vídeo",
            "title": "Pronto para aprovação",
            "body": "O vídeo já passou nos gates automáticos. Revise rápido o conteúdo e clique em Aprovar para liberar a agenda.",
        }
    if job_status in {"queued", "running"}:
        return {
            "step": "1. Gerando vídeo",
            "title": "A geração ainda está em andamento",
            "body": "Espere o pipeline terminar. Quando o status virar revisão, a ação principal passa a ser aprovar o vídeo.",
        }
    if job_status in {"blocked_for_monetization", "rejected"} or job_status.endswith("_failed") or job_status == "failed":
        return {
            "step": "1. Corrigir",
            "title": "Este job não chegou à publicação",
            "body": "Use Reject ou Criar novo job completo depois de revisar a causa principal na página.",
        }
    return {
        "step": "Fluxo",
        "title": "Acompanhe o próximo passo",
        "body": _job_next_action(job_status, schedule_status or None),
    }


templates.env.globals["job_status_label"] = _job_status_label
templates.env.globals["schedule_status_label"] = _schedule_status_label
templates.env.globals["job_flow_stage"] = _job_flow_stage
templates.env.globals["job_next_action"] = _job_next_action


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    orchestrator.start_worker()
    yield
    orchestrator.stop_worker()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
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


def _clamp_page(value: int | None) -> int:
    return max(1, int(value or 1))


def _clamp_per_page(value: int | None) -> int:
    return max(1, min(100, int(value or HUB_JOBS_PER_PAGE)))


def _query_jobs(status: str | None, search: str | None, fallback: str | None, review: str | None, page: int = 1, per_page: int = HUB_JOBS_PER_PAGE):
    session = SessionLocal()
    try:
        normalized_page = _clamp_page(page)
        normalized_per_page = _clamp_per_page(per_page)
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
        all_rows = session.execute(stmt).all()
        total = len(all_rows)
        total_pages = max(1, (total + normalized_per_page - 1) // normalized_per_page)
        normalized_page = min(normalized_page, total_pages)
        offset = (normalized_page - 1) * normalized_per_page
        return {
            "rows": all_rows[offset : offset + normalized_per_page],
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total": total,
            "total_pages": total_pages,
            "has_previous": normalized_page > 1,
            "has_next": normalized_page < total_pages,
        }
    finally:
        session.close()


def _jobs_query_string(filters: dict[str, str], page: int, per_page: int) -> str:
    params = {
        "page": page,
        "per_page": per_page,
        **{key: value for key, value in filters.items() if value},
    }
    return urlencode(params)


def _job_list_context(
    *,
    status: str | None,
    search: str | None,
    fallback: str | None,
    review: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    filters = {"status": status or "", "search": search or "", "fallback": fallback or "", "review": review or ""}
    pagination = _query_jobs(status=status, search=search, fallback=fallback, review=review, page=page, per_page=per_page)
    pagination["previous_query"] = _jobs_query_string(filters, max(1, int(pagination["page"]) - 1), int(pagination["per_page"]))
    pagination["next_query"] = _jobs_query_string(filters, int(pagination["page"]) + 1, int(pagination["per_page"]))
    pagination["current_query"] = _jobs_query_string(filters, int(pagination["page"]), int(pagination["per_page"]))
    return {"rows": pagination["rows"], "pagination": pagination, "filters": filters}


def _schedule_display(schedule: PublicationSchedule | None) -> dict[str, str | None] | None:
    if schedule is None:
        return None
    scheduled_for_utc = schedule.scheduled_for_utc if schedule.scheduled_for_utc.tzinfo else schedule.scheduled_for_utc.replace(tzinfo=UTC)
    published_at = schedule.published_at if schedule.published_at and schedule.published_at.tzinfo else (
        schedule.published_at.replace(tzinfo=UTC) if schedule.published_at else None
    )
    local_dt = scheduled_for_utc.astimezone(ZoneInfo(schedule.timezone))
    published_local = published_at.astimezone(ZoneInfo(schedule.timezone)) if published_at else None
    return {
        "status": schedule.status,
        "scheduled_for_utc": scheduled_for_utc.isoformat(),
        "scheduled_for_local": local_dt.isoformat(),
        "scheduled_for_local_form": local_dt.strftime("%Y-%m-%dT%H:%M"),
        "local_date": local_dt.date().isoformat(),
        "local_time": local_dt.strftime("%H:%M"),
        "timezone": schedule.timezone,
        "youtube_visibility": schedule.youtube_visibility,
        "notes": schedule.notes,
        "published_at": published_local.isoformat() if published_local else None,
        "published_local_label": published_local.strftime("%d/%m/%Y %H:%M") if published_local else None,
        "youtube_video_id": schedule.youtube_video_id,
        "youtube_url": schedule.youtube_url,
    }


def _publication_title(job: Job, topic_request: TopicRequest | None, script: Script | None) -> str:
    return (
        (script.title if script else None)
        or job.topic_summary
        or (topic_request.seed_theme if topic_request else None)
        or job.job_id
    )


def _effective_youtube_redirect_uri(request: Request) -> str:
    return settings.youtube_oauth_redirect_uri or f"{str(request.base_url).rstrip('/')}/youtube/oauth/callback"


def _youtube_integration_context(request: Request) -> dict[str, object]:
    redirect_uri = _effective_youtube_redirect_uri(request)
    status = orchestrator.youtube.connection_status(redirect_uri)
    missing_items = list(status.missing_items)
    if not settings.youtube_channel_id:
        missing_items.append("YTS_YOUTUBE_CHANNEL_ID ainda não está configurado.")
    if settings.youtube_publish_mode == "manual":
        stage = "manual_only"
        headline = "Agenda local ativa. A publicação continua manual no YouTube Studio."
    elif settings.youtube_api_enabled and status.connected and not missing_items:
        stage = "api_ready"
        headline = "OAuth conectado e worker pronto para publicar automaticamente nos horários programados."
    else:
        stage = "config_partial"
        headline = "A integração real existe, mas ainda falta fechar configuração ou conexão OAuth."
    return {
        "stage": stage,
        "headline": headline,
        "publish_mode": settings.youtube_publish_mode,
        "api_enabled": settings.youtube_api_enabled,
        "channel_id": settings.youtube_channel_id,
        "connected": status.connected,
        "client_configured": status.client_configured,
        "dependencies_available": status.dependencies_available,
        "redirect_uri": redirect_uri,
        "granted_scopes": status.granted_scopes,
        "connected_at": status.connected_at,
        "token_expires_at": status.token_expires_at,
        "missing_items": missing_items,
    }


def _publication_dashboard_context(request: Request, limit: int = 6) -> dict[str, object]:
    with SessionLocal() as session:
        approved_rows = session.execute(
            select(Job, TopicRequest, Script, PublicationSchedule)
            .join(TopicRequest, TopicRequest.job_id == Job.job_id)
            .join(Script, Script.job_id == Job.job_id, isouter=True)
            .join(PublicationSchedule, PublicationSchedule.job_id == Job.job_id, isouter=True)
            .where(Job.status == "approved_for_publish")
            .where(or_(PublicationSchedule.schedule_id.is_(None), PublicationSchedule.status == "cancelled"))
            .order_by(Job.created_at.asc())
            .limit(limit)
        ).all()
        schedule_rows = session.execute(
            select(PublicationSchedule, Job, TopicRequest, Script)
            .join(Job, Job.job_id == PublicationSchedule.job_id)
            .join(TopicRequest, TopicRequest.job_id == PublicationSchedule.job_id)
            .join(Script, Script.job_id == PublicationSchedule.job_id, isouter=True)
            .where(PublicationSchedule.status.in_(["scheduled", "publishing", "publish_failed"]))
            .order_by(PublicationSchedule.scheduled_for_utc.asc())
            .limit(limit)
        ).all()
        published_rows = session.execute(
            select(PublicationSchedule, Job, TopicRequest, Script)
            .join(Job, Job.job_id == PublicationSchedule.job_id)
            .join(TopicRequest, TopicRequest.job_id == PublicationSchedule.job_id)
            .join(Script, Script.job_id == PublicationSchedule.job_id, isouter=True)
            .where(PublicationSchedule.status == "published")
            .order_by(PublicationSchedule.published_at.desc(), PublicationSchedule.updated_at.desc())
            .limit(limit)
        ).all()
        awaiting_approval_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status.in_(["monetization_review", "ready_for_upload"]))
        ) or 0
        unscheduled_approved_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .join(PublicationSchedule, PublicationSchedule.job_id == Job.job_id, isouter=True)
            .where(Job.status == "approved_for_publish")
            .where(or_(PublicationSchedule.schedule_id.is_(None), PublicationSchedule.status == "cancelled"))
        ) or 0
        scheduled_count = session.scalar(
            select(func.count()).select_from(PublicationSchedule).where(PublicationSchedule.status == "scheduled")
        ) or 0
        publishing_count = session.scalar(
            select(func.count()).select_from(PublicationSchedule).where(PublicationSchedule.status == "publishing")
        ) or 0
        failed_count = session.scalar(
            select(func.count()).select_from(PublicationSchedule).where(PublicationSchedule.status == "publish_failed")
        ) or 0
        published_count = session.scalar(
            select(func.count()).select_from(PublicationSchedule).where(PublicationSchedule.status == "published")
        ) or 0

    ready_to_schedule = []
    for job, topic_request, script, schedule in approved_rows:
        ready_to_schedule.append(
            {
                "job_id": job.job_id,
                "title": _publication_title(job, topic_request, script),
                "seed_theme": topic_request.seed_theme if topic_request else None,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "job_status": job.status,
                "schedule": _schedule_display(schedule) if schedule else None,
            }
        )

    upcoming_schedule = [
        {
            "job_id": job.job_id,
            "title": _publication_title(job, topic_request, script),
            "seed_theme": topic_request.seed_theme if topic_request else None,
            "job_status": job.status,
            "schedule": _schedule_display(schedule),
        }
        for schedule, job, topic_request, script in schedule_rows
    ]

    recent_publications = [
        {
            "job_id": job.job_id,
            "title": _publication_title(job, topic_request, script),
            "seed_theme": topic_request.seed_theme if topic_request else None,
            "job_status": job.status,
            "schedule": _schedule_display(schedule),
        }
        for schedule, job, topic_request, script in published_rows
    ]

    return {
        "integration": _youtube_integration_context(request),
        "ready_to_schedule": ready_to_schedule,
        "upcoming_schedule": upcoming_schedule,
        "recent_publications": recent_publications,
        "metrics": {
            "unscheduled_approved_count": unscheduled_approved_count,
            "scheduled_count": scheduled_count,
            "publishing_count": publishing_count,
            "failed_count": failed_count,
            "published_count": published_count,
            "awaiting_approval_count": awaiting_approval_count,
        },
    }


def _parse_calendar_month(month: str | None) -> date:
    normalized = str(month or "").strip()
    if not normalized:
        now = datetime.now(UTC)
        return date(now.year, now.month, 1)
    try:
        parsed = datetime.strptime(normalized, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM") from exc
    return date(parsed.year, parsed.month, 1)


def _shift_month(month_start: date, delta: int) -> date:
    month_index = month_start.month - 1 + delta
    year = month_start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _calendar_context(month: str | None) -> dict[str, object]:
    month_start = _parse_calendar_month(month)
    previous_month = _shift_month(month_start, -1)
    next_month = _shift_month(month_start, 1)
    month_names_pt_br = [
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    ]
    month_weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)
    with SessionLocal() as session:
        schedule_rows = session.execute(
            select(PublicationSchedule, Job, TopicRequest, Script)
            .join(Job, Job.job_id == PublicationSchedule.job_id)
            .join(TopicRequest, TopicRequest.job_id == PublicationSchedule.job_id)
            .join(Script, Script.job_id == PublicationSchedule.job_id, isouter=True)
            .where(PublicationSchedule.status.in_(["scheduled", "publishing", "publish_failed", "published"]))
            .order_by(PublicationSchedule.scheduled_for_utc)
        ).all()

    entries_by_day: dict[date, list[dict[str, object]]] = {}
    scheduled_count = 0
    published_count = 0
    for schedule, job, topic_request, script in schedule_rows:
        scheduled_for_utc = schedule.scheduled_for_utc if schedule.scheduled_for_utc.tzinfo else schedule.scheduled_for_utc.replace(tzinfo=UTC)
        local_dt = scheduled_for_utc.astimezone(ZoneInfo(schedule.timezone))
        local_day = local_dt.date()
        if local_day.year != month_start.year or local_day.month != month_start.month:
            continue
        title = script.title if script else (job.topic_summary or topic_request.seed_theme)
        entry = {
            "job_id": job.job_id,
            "title": title,
            "seed_theme": topic_request.seed_theme,
            "job_status": job.status,
            "review_state": job.review_state,
            "schedule_status": schedule.status,
            "local_time": local_dt.strftime("%H:%M"),
            "timezone": schedule.timezone,
            "youtube_visibility": schedule.youtube_visibility,
            "youtube_url": schedule.youtube_url,
        }
        entries_by_day.setdefault(local_day, []).append(entry)
        if schedule.status == "scheduled":
            scheduled_count += 1
        if schedule.status == "published":
            published_count += 1

    weeks: list[list[dict[str, object]]] = []
    for week in month_weeks:
        week_cells = []
        for day in week:
            week_cells.append(
                {
                    "date": day,
                    "iso_date": day.isoformat(),
                    "day_number": day.day,
                    "is_current_month": day.month == month_start.month,
                    "entries": entries_by_day.get(day, []),
                }
            )
        weeks.append(week_cells)

    return {
        "month_value": month_start.strftime("%Y-%m"),
        "month_label": f"{month_names_pt_br[month_start.month - 1]} {month_start.year}",
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "weeks": weeks,
        "scheduled_count": scheduled_count,
        "published_count": published_count,
    }


def _resolve_job_id(session, job_id: str) -> str:
    if session.get(Job, job_id):
        return job_id
    matches = session.scalars(select(Job.job_id).where(Job.job_id.like(f"{job_id}%")).order_by(Job.created_at.desc()).limit(2)).all()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="job id prefix is ambiguous")
    raise KeyError(job_id)


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
    selected_niche = niche_id or HUB_DEFAULT_NICHE
    trend_notes = None
    if seed_theme.strip():
        selected_seed_theme = seed_theme.strip()
        trend_report = None
    else:
        selected_seed_theme, trend_angle, trend_notes, trend_report = _trend_seed_theme(selected_niche)
        selected_angle = selected_angle or trend_angle or ""
    combined_notes = "\n\n".join(part for part in [trend_notes, notes] if part)
    try:
        payload = TopicRequestCreate(
            seed_theme=selected_seed_theme,
            niche_id=selected_niche,
            language=language,
            target_duration_sec=target_duration_sec,
            tone=tone,
            cta_style=cta_style,
            notes=_compose_hub_notes(input_mode, combined_notes),
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


@app.get("/healthz")
def healthcheck():
    return {
        "status": "ok",
        "app": settings.app_name,
        "bind": f"{settings.app_host}:{settings.app_port}",
        "tailnet_url": f"https://{settings.tailscale_hostname}.{settings.tailnet_domain}",
    }
