# Documentacao do App

YTS Render e um app FastAPI para gerar Shorts verticais em pt-BR, revisar o resultado em um hub web e publicar no YouTube em fluxo manual ou via API.

## Visao geral

Blocos principais:

- `app/main.py`: rotas FastAPI, SSR com Jinja2, formularios do hub, calendario e OAuth do YouTube.
- `app/hub_context.py`: builders de contexto do hub, listas de jobs, dashboard de publicacao, calendario e status operacional.
- `app/orchestrator.py`: worker, maquina de estados do job, retries, lease, eventos e delegacao de steps.
- `app/publication_ops.py`: review, publicacao, agenda por canal, sync YouTube/TikTok e sweep de retencao de artefatos.
- `app/pipelines/`: etapas especializadas do pipeline.
- `app/providers/`: providers de texto, imagem, TTS, musica e fallback, com `app.providers` como fachada publica de compatibilidade.
- `app/routes/`: routers isolados, hoje com `/healthz`.
- `app/youtube_api.py`: integracao OAuth e upload real via YouTube Data API.
- `app/models.py`: persistencia SQLAlchemy de jobs, agenda, review, erros, retries, telemetria e artefatos logicos.

## Fronteiras de manutencao IA-friendly

O app foi modularizado para que uma mudanca comum exija contexto de poucos arquivos e preserve contratos publicos. O ponto de entrada continua sendo `JobOrchestrator`, mas ele deve ser tratado como casca de lifecycle: criar job, reivindicar trabalho, renovar lease, executar retry, registrar eventos, montar progresso e acionar publicacao agendada.

Mapa de ownership para novas mudancas:

| Area | Comece por | Evite comecar por |
| --- | --- | --- |
| Pauta, tendencia, learning brief e registry | `app/pipelines/topic_pipeline.py` | `app/orchestrator.py` |
| Roteiro, fact pack, auditoria textual e repair | `app/pipelines/script_pipeline.py`, `script_fact_pack.py`, `script_audit.py`, `script_repair.py` | `app/orchestrator.py` |
| Cenas | `app/pipelines/scene_pipeline.py` | `app/orchestrator.py` |
| Imagens, TTS, legendas e musica | `app/pipelines/asset_pipeline.py`, `image_assets.py`, `tts_assets.py`, `subtitle_assets.py`, `music_assets.py` | `app/orchestrator.py` |
| Render | `app/pipelines/render_pipeline.py` | `app/orchestrator.py` |
| Monetizacao e pacote de publish | `app/pipelines/monetization_pipeline.py` | `app/main.py` |
| Revisao, agenda, publish, performance, retencao e canais | `app/publication_ops.py` | `app/main.py` |
| Listas, calendario, status operacional e contexto SSR | `app/hub_context.py` | queries inline em templates |
| Providers | `app/providers/llm.py`, `image.py`, `music.py`, `tts.py`, `registry.py` | recriar `app/providers.py` |

`app.providers` e uma fachada de compatibilidade importavel. Novas implementacoes devem entrar no modulo dono dentro de `app/providers/`.

`app/main.py` ainda concentra rotas SSR principais. Para manter o contexto pequeno, novas regras de consulta, agregacao ou apresentacao de estado devem ir para `HubContext` ou para `PublicationOperations`; a rota deve apenas validar formulario, chamar o dono e redirecionar.

`tests/test_e2e.py` e ancora de compatibilidade. Testes novos devem preferir a suite de dominio correspondente: `test_pipeline_script.py`, `test_pipeline_assets.py`, `test_hub_publication.py`, `test_orchestrator_flow.py`, `test_providers_integrations.py` ou `test_deep_modules_unit.py`.

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
9. Em modo YouTube `api` no Hub com OAuth conectado, o worker processa slots vencidos e sobe o video no YouTube automaticamente.
10. Em modo `manual`, o hub continua servindo para aprovacao, agenda local e registro de publish manual.

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
| `background_music` | 1 | Seleciona ou gera trilha, faz mix e valida audio final. |
| `render` | 1 | Gera `render/final.mp4` vertical via FFmpeg. |
| `monetization_readiness_gate` | 0 | Consolida direitos, disclosure, factualidade, repeticao e publish readiness. |
| `publish_to_review_hub` | 0 | Persiste o pacote de publicacao e leva o job ao hub. |

Cada etapa grava `StepExecution`, eventos em `events.jsonl` e artefatos JSON ou midia no diretorio do job.

Os nomes de etapa, artefatos e chaves principais de `quality_summary` sao contratos publicos do app. Refatoracoes internas podem trocar classes ou helpers, mas nao devem renomear esses contratos sem migracao e teste dedicado.

## Publicacao e YouTube

Comportamento atual:

- `manual`: o formulario de publish exige `youtube_video_id` ou `youtube_url` e apenas registra a publicacao no hub.
- `api`: o hub pode subir o video direto pela YouTube Data API.
- agenda automatica: so e consumida pelo worker quando o modo efetivo e `api`.
- automacao diaria: um CLI pode gerar ate tres tentativas, autoaprovar apenas `ready_for_upload` com score suficiente e agendar nativamente no YouTube para o primeiro dia vago.

Fluxo OAuth:

- `GET /youtube/connect` cria a URL de autorizacao e persiste `youtube_oauth_state.json`.
- `GET /youtube/oauth/callback` troca `code` por token e salva `youtube_oauth_token.json`.
- `POST /youtube/disconnect` remove token e state locais.

## Publicacao cruzada no TikTok

Quando `YTS_TIKTOK_AUTO_PUBLISH_ENABLED=true`, jobs que ja entraram na agenda ou publicacao do YouTube ganham um registro em `ChannelPublication` para o canal `tiktok`. Jobs com agenda futura seguem o mesmo horario planejado; jobs ja publicados entram em retropostagem controlada, limitada por `YTS_TIKTOK_RETROPOST_DAILY_LIMIT` (padrao 1 por dia).

O envio usa a Content Posting API oficial do TikTok com `YTS_TIKTOK_ACCESS_TOKEN` e escopo `video.publish`. A API exige consulta de creator info, privacidade compativel com a conta e pode restringir clientes nao auditados a publicacoes privadas; essas recusas ficam registradas como `publish_failed` no canal TikTok.

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
| `POST` | `/automation/toggle` | Liga ou pausa a automacao diaria. |
| `POST` | `/automation/run` | Executa um ciclo de automacao sob demanda. |
| `POST` | `/automation/ready-scripts/import` | Importa lote de roteiros prontos confirmados. |
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
- `llm_primary_provider=openai`
- `llm_fallback_provider=deepseek`
- `youtube_publish_mode=manual`
- `youtube_api_enabled=false`
- `automation_enabled=false`
- `automation_daily_timezone=America/Sao_Paulo`
- `automation_daily_run_time=02:00`
- `automation_publish_time=11:00`
- `artifact_retention_enabled=true`

Camadas de configuracao:

- `.env`: boot, infraestrutura e segredos. Inclui `YTS_APP_URL`, `YTS_HUB_AUTH_TOKEN`, `YTS_DATABASE_URL`, chaves de provedores, OAuth do YouTube, token do TikTok e exposicao Tailnet.
- Hub de Revisao: ajustes operacionais nao secretos. Inclui LLM ativo, fallback de LLM, planejador de cenas, fonte de musica, autopopulacao do banco local, modo de publicacao, API do YouTube, publicacao cruzada no TikTok, horario do ciclo diario, horario padrao de publicacao, janela da agenda e score minimo. O gerador de imagens aparece como informacao operacional; hoje, em execucao real, ele e MiniMax.
- defaults do codigo: valores seguros usados quando nem `.env` nem Hub definem uma sobreposicao.

As sobreposicoes do Hub ficam na tabela `operational_settings`. Elas sao aplicadas no startup do FastAPI e no comando `yts-render automation-run`. Segredos nunca devem ser adicionados a essa tabela; novos campos editaveis precisam entrar pela allowlist em `app/operational_settings.py`.

Terminologia do painel:

- **Planejador de cenas (LLM)**: escolhe o LLM que cria `scene_plan.json`, com cenas, intencao visual e prompts. Ele nao gera imagens.
- **Gerador de imagens**: provider que gera ou seleciona os assets visuais no passo `asset_generation`. Hoje, em execucao real, e MiniMax; por isso aparece como leitura operacional, nao como seletor editavel.

Musica de fundo:

- o padrao e banco local, alteravel no Hub de Revisao
- `local_bank` le `YTS_MUSIC_BANK_DIR/manifest.json` e usa apenas faixas aprovadas para YouTube, com licenca ou origem rastreavel
- a autopopulacao do banco local pode ser ligada ou desligada no Hub
- trilhas MiniMax antigas podem ser importadas com `scripts/import_minimax_music_artifacts.py` e recebem prioridade sobre as sinteticas locais
- o fallback para API fica desligado por padrao para impedir custo silencioso quando o banco local falha
- `minimax` força MiniMax Music como fonte primaria
- `auto` tenta o banco local e depois MiniMax, quando houver chave
- o manifest pode ser uma lista ou um objeto com `tracks`; cada item deve ter `path`, `license` ou `license_note`, `source_url` ou `license_file`, `approved_for_youtube=true`, e nao deve estar marcado como Content ID registrado
- veja `docs/music-bank.md` para o formato recomendado do banco local

Credenciais MiniMax por midia:

- texto usa `YTS_MINIMAX_TEXT_API_KEY` ou `YTS_MINIMAX_API_KEY`
- imagem tenta primeiro a chave resolvida de texto
- imagem usa `YTS_MINIMAX_IMAGE_API_KEY` so depois de limite ou quota na chave de texto, e marca essa chave como esgotada para o job atual
- se nao houver chave de texto, imagem usa diretamente `YTS_MINIMAX_IMAGE_API_KEY`
- musica usa `YTS_MINIMAX_MUSIC_API_KEY` ou a chave resolvida de texto apenas quando MiniMax Music esta configurado como provider ou fallback

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
- `ChannelPublication`
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

## Automacao diaria

A primeira versao roda por CLI e systemd timer, nao por scheduler interno do FastAPI:

```bash
python -m app.cli automation-run
scripts/install_automation_timer.sh
```

O ciclo verifica pausa global, preflight do YouTube API, lock por data local de Sao Paulo e janela de 14 dias a partir de amanha. Quando encontra dia vago na agenda interna, tenta primeiro consumir um roteiro pronto aleatorio do banco, filtrado por similaridade narrativa. Se o banco estiver vazio ou saturado por similaridade, usa Tema Automatico.

Um job so entra em publicacao automatizada se terminar em `ready_for_upload`, passar no score composto minimo de `0.82`, nao tiver repeticao alta e cumprir os thresholds de factualidade, retencao, metadados e assets. Ao passar, o sistema aprova o job e usa agendamento nativo do YouTube com `publishAt` para 11h em `America/Sao_Paulo`; isso registra agenda `scheduled`, nao `published`.

## Testes

A suite principal foi dividida por dominio. `tests/test_e2e.py` fica apenas como ancora de compatibilidade; os testes reais vivem em:

- `tests/test_hub_publication.py`: hub, calendario, agenda, publish, OAuth e automacao.
- `tests/test_orchestrator_flow.py`: lifecycle, worker, estados, retries e fluxo completo.
- `tests/test_pipeline_assets.py`: cenas, assets, TTS, legendas, musica e render.
- `tests/test_pipeline_script.py`: roteiro, fact pack, auditoria textual, repair e monetizacao textual.
- `tests/test_providers_integrations.py`: providers e registries.
- `tests/e2e_support.py` e `tests/conftest.py`: fixtures e helpers compartilhados.

Comando padrao:

```bash
.venv/bin/python -m pytest -q
```

## Onde alterar

Para mudar UX do hub:

- `app/main.py`
- `app/hub_context.py`
- `app/templates/*.html`
- `app/static/styles.css`

Para mudar publicacao e YouTube:

- `app/publication_ops.py`
- `app/youtube_api.py`
- `app/schemas.py`

Para mudar automacao diaria:

- `app/automation.py`
- `app/cli.py`
- `app/models.py`
- `app/templates/publication_dashboard.html`

Para mudar regras de retencao:

- `app/config.py`
- `app/publication_ops.py`
- `app/storage.py`
