# Documentacao do App

YTS Render e um app FastAPI para gerar Shorts verticais em pt-BR, revisar o resultado em um hub web e publicar no YouTube em fluxo manual ou via API.

## Visao geral

Blocos principais:

- `app/main.py`: rotas FastAPI, SSR com Jinja2, formularios do hub, dashboard de publicacao, calendario e OAuth do YouTube.
- `app/orchestrator.py`: worker, maquina de estados do job, retries, publicacao, agenda e sweep de retencao de artefatos.
- `app/pipelines/`: etapas especializadas do pipeline.
- `app/providers.py`: providers de texto, imagem, TTS, musica e fallback.
- `app/youtube_api.py`: integracao OAuth e upload real via YouTube Data API.
- `app/models.py`: persistencia SQLAlchemy de jobs, agenda, review, erros, retries, telemetria e artefatos logicos.

Persistencia local padrao:

- banco: `data/yts_render.db`
- artefatos: `data/artifacts/<job_id>/`
- token OAuth do YouTube: `data/youtube_oauth_token.json`
- state temporario do OAuth: `data/youtube_oauth_state.json`

## Ciclo de vida do job

1. O usuario cria um job pela home ou por `POST /jobs`.
2. `main.py` valida o payload com `TopicRequestCreate` e chama `orchestrator.create_job`.
3. O job entra como `queued`.
4. O worker reivindica jobs pendentes e executa o pipeline.
5. Ao fim, o status do job vira `monetization_review`, `blocked_for_monetization` ou `ready_for_upload`.
6. O revisor abre `/jobs/{job_id}`, assiste ao video, revisa checklist e aprova ou rejeita.
7. Ao aprovar, o job vira `approved_for_publish`.
8. A partir disso, o operador pode salvar metadados de upload, agendar pela pagina do job ou pelo calendario, publicar imediatamente ou reabrir para republicacao.
9. Em `YTS_YOUTUBE_PUBLISH_MODE=api` com OAuth conectado, o worker processa slots vencidos e sobe o video no YouTube automaticamente.
10. Em `manual`, o hub continua servindo para aprovacao, agenda local e registro de publish manual.

## Modos de entrada

`POST /jobs` recebe tres modos operacionais pelo campo `input_mode`:

- `theme`: assunto bruto. Quando `seed_theme` vem vazio, o hub tenta resolver um tema automatico por tendencias e registra fallback quando nao encontra candidato vivo.
- `title`: titulo completo fornecido pelo operador. O app preserva a promessa central e ainda passa pelo fluxo normal de pauta, roteiro e gates.
- `script`: **Roteiro Pronto** em texto rotulado. O app preserva o texto como fonte editorial e nao chama LLM para gerar outro roteiro.

O `Roteiro Pronto` exige estes rotulos:

```text
Titulo: ...
Hook: ...
Loop: ...
Beats:
- ...
Payoff: ...
Fechamento: ...
Hashtags: #opcional
```

Regras importantes desse modo:

- `ready_script_fact_check_confirmed=true` e obrigatorio no hub/API.
- `Titulo` vira metadado, nao narracao.
- a narracao e montada com `Hook`, `Loop`, `Beats`, `Payoff` e `Fechamento`.
- `Loop` e tratado como tensao narrativa, nao como fato declarado.
- fatos declarados entram a partir de `Beats` e `Payoff` sob responsabilidade da confirmacao humana.
- desvios grandes de formato ou duracao bloqueiam antes de midia; o app nao reescreve automaticamente hook, beats, payoff ou fechamento.

## Estados

### Job

- `queued`: criado e aguardando worker.
- `running`: worker executando o pipeline.
- `monetization_review`: falta revisao humana antes da aprovacao.
- `blocked_for_monetization`: houve bloqueio hard de compliance, factualidade, direitos ou qualidade.
- `ready_for_upload`: passou no gate final e esta pronto para aprovacao humana.
- `approved_for_publish`: aprovado e liberado para agenda/publicacao.
- `published`: publicado e registrado.
- `rejected`: rejeitado na revisao.
- `failed`: falha geral no pipeline.

Falhas especificas por etapa tambem sao estados finais validos:

- `script_quality_failed`
- `scene_plan_quality_failed`
- `asset_quality_failed`
- `subtitle_quality_failed`
- `render_quality_failed`

### Agenda de publicacao

- `scheduled`: slot salvo.
- `publishing`: upload em andamento.
- `publish_failed`: tentativa de upload falhou.
- `published`: upload concluido.
- `cancelled`: agenda limpa ou reaberta para republicacao.

## Pipeline

Etapas atuais de `JobOrchestrator._steps()`:

| Etapa | Retry | Responsabilidade |
| --- | ---: | --- |
| `input_gate` | 0 | Valida entrada basica do job. |
| `topic_plan` | 2 | Gera pauta, angulo, entidades, promessa e candidatos de titulo. |
| `script` | 2 | Gera roteiro e passa pelo `ScriptQualityGate`, com repair quando cabivel. |
| `scene_plan` | 1 | Divide o roteiro em cenas e valida estrutura visual. |
| `asset_generation` | 2 | Gera ou seleciona imagens e aplica score semantico. |
| `tts` | 2 | Gera narracao e metadados basicos de audio. |
| `subtitle_alignment` | 1 | Normaliza legenda e arquivos de render. |
| `background_music` | 1 | Gera trilha, faz mix e valida audio final. |
| `render` | 1 | Gera `render/final.mp4` vertical via FFmpeg. |
| `monetization_readiness_gate` | 0 | Consolida direitos, disclosure, factualidade, repeticao e publish readiness. |
| `publish_to_review_hub` | 0 | Persiste o pacote de publicacao e leva o job ao hub. |

Cada etapa grava `StepExecution`, eventos em `events.jsonl` e artefatos JSON ou midia no diretorio do job.

## Publicacao e YouTube

Comportamento atual:

- `manual`: o formulario de publish exige `youtube_video_id` ou `youtube_url` e apenas registra a publicacao no hub.
- `api`: o hub pode subir o video direto pela YouTube Data API.
- agenda automatica: so e consumida pelo worker quando o modo efetivo e `api`.

Fluxo OAuth:

- `GET /youtube/connect` cria a URL de autorizacao e persiste `youtube_oauth_state.json`.
- `GET /youtube/oauth/callback` troca `code` por token e salva `youtube_oauth_token.json`.
- `POST /youtube/disconnect` remove token e state locais.

O contexto de integracao exposto no hub usa:

- `publish_mode`
- `api_enabled`
- `connected`
- `channel_id`
- `missing_items`
- `connected_at`
- `token_expires_at`

## Rotas principais

| Metodo | Rota | Uso |
| --- | --- | --- |
| `GET` | `/` | Home do hub com formulario, jobs e resumo operacional. |
| `POST` | `/hub/prompt` | Salva ou reseta o template viral do hub. |
| `GET` | `/jobs` | Fragmento HTML da tabela paginada de jobs. |
| `GET` | `/publication-hub` | Dashboard de publicacao, integracao YouTube e fila. |
| `GET` | `/youtube/connect` | Inicia OAuth do YouTube. |
| `GET` | `/youtube/oauth/callback` | Conclui OAuth do YouTube. |
| `POST` | `/youtube/disconnect` | Remove token OAuth local. |
| `GET` | `/calendar` | Calendario mensal de programados, publicados e jobs aprovados livres para agendar. |
| `POST` | `/calendar/schedule` | Agenda um job aprovado a partir do dia escolhido no calendario. |
| `POST` | `/jobs` | Cria novo job. |
| `GET` | `/api/jobs/{job_id}` | JSON compacto com status e render. |
| `GET` | `/jobs/{job_id}` | Detalhe do job com revisao, agenda e metadados. |
| `POST` | `/jobs/{job_id}/review` | Aprova, rejeita ou cria retry integral. |
| `POST` | `/jobs/{job_id}/publish-metadata` | Salva titulo, descricao e hashtags de upload. |
| `POST` | `/jobs/{job_id}/publish` | Publica agora ou registra publicacao manual. |
| `POST` | `/jobs/{job_id}/schedule` | Salva ou limpa agenda local. |
| `POST` | `/jobs/{job_id}/reopen-publication` | Reabre um publish para republicacao. |
| `POST` | `/jobs/{job_id}/performance` | Registra metricas manuais do YouTube Studio. |
| `GET` | `/healthz` | Healthcheck do app. |

Arquivos sob `data/artifacts/` sao servidos por `/artifacts/...` quando ainda existem.

## Configuracao

`app/config.py` e a fonte de verdade para `Settings`.

Defaults importantes:

- `app_url=http://127.0.0.1:8080`
- `niche_id=curiosidades`
- `language=pt-BR`
- `target_duration_sec=45`
- `simple_shorts_mode=true`
- `llm_primary_provider=minimax`
- `llm_fallback_provider=deepseek`
- `youtube_publish_mode=manual`
- `youtube_api_enabled=false`
- `artifact_retention_enabled=true`

Blocos relevantes de configuracao:

- hub e auth: `YTS_APP_URL`, `YTS_HUB_AUTH_TOKEN`
- banco: `YTS_DATABASE_URL`, `YTS_SQLITE_*`
- editorial: `YTS_NICHE_ID`, `YTS_LANGUAGE`, `YTS_SIMPLE_SHORTS_MODE`
- providers e timeouts: `YTS_LLM_*`, `YTS_MINIMAX_*`, `YTS_OPENAI_*`, `YTS_DEEPSEEK_*`, `YTS_QWEN_*`
- audio: `YTS_BACKGROUND_MUSIC_*`, `YTS_SOUND_DESIGN_*`
- YouTube: `YTS_YOUTUBE_*`
- direitos: `YTS_*COMMERCIAL_RIGHTS*`, `YTS_CONSERVATIVE_SYNTHETIC_DISCLOSURE`, `YTS_ALLOW_SYNTHETIC_VISUALS_FOR_MONETIZATION`
- worker e retencao: `YTS_WORKER_POLL_SECONDS`, `YTS_JOB_LEASE_SECONDS`, `YTS_ARTIFACT_RETENTION_*`

Credenciais MiniMax por midia:

- texto usa `YTS_MINIMAX_TEXT_API_KEY` ou `YTS_MINIMAX_API_KEY`
- imagem tenta primeiro a chave resolvida de texto
- imagem usa `YTS_MINIMAX_IMAGE_API_KEY` so depois de limite ou quota na chave de texto, e marca essa chave como esgotada para o job atual
- se nao houver chave de texto, imagem usa diretamente `YTS_MINIMAX_IMAGE_API_KEY`
- musica usa `YTS_MINIMAX_MUSIC_API_KEY` ou a chave resolvida de texto

Limite de provedor para troca de chave de imagem significa quota, saldo, credito ou rate limit. Timeout, erro de conexao, resposta invalida e `5xx` continuam sendo falhas transientes da chamada atual.

## Persistencia e artefatos

Modelos principais:

- `Job`
- `TopicRequest`
- `TopicPlan`
- `Script`
- `ScenePlan`
- `SceneAsset`
- `NarrationAsset`
- `SubtitleTrack`
- `BackgroundMusicAsset`
- `RenderOutput`
- `PublicationSchedule`
- `ReviewRecord`
- `PerformanceMetric`
- `FallbackEvent`
- `ErrorLog`
- `StepExecution`
- `TopicRegistry`

Artefatos comuns por job:

```text
request.json
topic_plan.json
script.json
scene_plan.json
events.jsonl
publish_package.json
publish_metadata_overrides.json
publication_schedule.json
youtube_publish_attempts.json
publish_result.json
render/final.mp4
render/poster.jpg
render/ffmpeg.log
```

`artifact_url()` converte `file://...` dentro de `data/artifacts/` para `/artifacts/...`. Quando o arquivo ja foi removido, a UI nao renderiza link quebrado.

## Retencao automatica

O worker roda sweep periodico de retencao e classifica jobs em tres grupos:

- `hard_failure`: `24h`
- `recoverable`: `168h`
- `publishable`: `504h`

Regra atual:

- falhas criticas de pipeline entram no grupo curto
- `monetization_review`, `blocked_for_monetization`, `rejected` e `publish_failed` entram no grupo medio
- `ready_for_upload`, `approved_for_publish` e agendas `scheduled` entram no grupo longo
- `queued`, `running`, `publishing`, `published` e `cancelled` ficam fora do cleanup automatico

Quando o TTL vence:

1. o diretorio de artefatos do job e removido
2. o app grava `retention_cleanup.json`
3. o job preserva `quality_summary.retention`
4. o hub continua mostrando metadados e historico leve, mas esconde midia pesada

## Interface

Templates ativos:

- `app/templates/base.html`
- `app/templates/jobs.html`
- `app/templates/jobs_table.html`
- `app/templates/publication_dashboard.html`
- `app/templates/calendar.html`
- `app/templates/job_detail.html`

A job page atual e deliberadamente centrada em decisao:

1. assistir o video
2. aprovar
3. agendar ou publicar

Conteudo tecnico, erros e artefatos ficam colapsados em paines secundarios.

O calendario e uma superficie operacional secundaria. Ele mostra slots programados e publicados, mas tambem abre um modal de agenda pelo botao `+` de cada dia do mes atual. Esse modal lista apenas jobs em `approved_for_publish` que ainda nao estejam publicados nem tenham agenda ativa.

## Testes

O foco principal esta em `tests/test_e2e.py`. A suite cobre:

- pipeline ate review
- tabela e detalhe de jobs
- agenda e calendario
- publish manual e via API
- OAuth do YouTube
- retencao de artefatos
- gates de qualidade

Comando padrao:

```bash
pytest -q
```

## Onde alterar

Para mudar UX do hub:

- `app/main.py`
- `app/templates/*.html`
- `app/static/styles.css`

Para mudar publicacao e YouTube:

- `app/orchestrator.py`
- `app/youtube_api.py`
- `app/schemas.py`

Para mudar regras de retencao:

- `app/config.py`
- `app/orchestrator.py`
- `app/storage.py`
