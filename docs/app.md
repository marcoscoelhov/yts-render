# Documentacao do App

YTS Render e um app FastAPI para gerar YouTube Shorts verticais em pt-BR, renderizar um MP4 com audio e legenda queimada, e deixar o resultado em um hub web de revisao humana.

## Visao geral

O app tem quatro blocos principais:

- `app/main.py`: rotas FastAPI, paginas SSR com Jinja2, formularios do hub e endpoints de revisao.
- `app/orchestrator.py`: worker em thread, maquina de estados do job, retries, gates de qualidade e render com FFmpeg.
- `app/providers.py`: providers de pauta/roteiro/cenas, imagem, TTS, stock fallback e verificacao semantica.
- `app/models.py`: modelos SQLAlchemy que persistem jobs, requests, roteiro, cenas, assets, audio, legendas, render, reviews e logs.

O banco padrao e SQLite em `data/yts_render.db`. Os artefatos ficam em `data/artifacts/<job_id>/`.

## Ciclo de vida

1. O usuario cria um job pelo formulario da pagina inicial ou por `POST /jobs`.
2. `main.py` valida o formulario com `TopicRequestCreate` e chama `orchestrator.create_job`.
3. O job entra no banco com status `queued`.
4. O worker iniciado no lifespan do FastAPI reivindica jobs `queued` ou `running` com lease vencido.
5. O orquestrador executa as etapas do pipeline, grava artefatos e persiste registros no banco.
6. Ao terminar, o job vai para `waiting_review`.
7. O usuario aprova, rejeita ou pede retry pela tela `/jobs/{job_id}`.
8. Job aprovado vira `approved_for_publish`; publicacao manual/API marca como `published`.

## Estados de job

Estados comuns:

- `queued`: criado e aguardando worker.
- `running`: worker esta executando uma etapa.
- `waiting_review`: video final pronto para revisao.
- `approved_for_publish`: aprovado na revisao e liberado para publicacao.
- `published`: marcado como publicado.
- `rejected`: rejeitado na revisao.
- `failed`: falha durante o pipeline.

Tambem existem estados de falha especificos por gate, como `script_quality_failed`, `scene_plan_quality_failed`, `asset_quality_failed`, `subtitle_quality_failed` e `render_quality_failed`.

## Pipeline

As etapas ficam em `JobOrchestrator._steps()`:

| Etapa | Retry | Responsabilidade |
| --- | ---: | --- |
| `input_gate` | 0 | Valida parametros basicos do job. |
| `topic_plan` | 2 | Gera pauta canonica, angulo, promessa, entidades e candidatos de titulo. |
| `script` | 2 | Gera roteiro e passa pelo `ScriptQualityGate`; pode tentar reparo. |
| `scene_plan` | 1 | Divide o roteiro em cenas e valida estrutura visual. |
| `asset_generation` | 2 | Gera/seleciona imagens por cena e aplica score semantico. |
| `tts` | 2 | Gera narracao WAV, normaliza audio e cria SRT bruto. |
| `subtitle_alignment` | 1 | Normaliza chunks de legenda e gera ASS/SRT para render. |
| `render` | 1 | Usa FFmpeg para gerar `render/final.mp4` vertical. |
| `publish_to_review_hub` | 0 | Monta pacote de publicacao e deixa o job em revisao. |

Cada execucao de etapa cria um `StepExecution` com `input_hash`. Se uma etapa ja teve sucesso com o mesmo input, o orquestrador pode reutilizar o resultado.

O app tambem grava `performance_timeline.json` no diretorio do job com duracao por etapa, tentativa e refs geradas. Use esse artefato para comparar mudancas de performance em runs reais antes/depois.

## Rotas

| Metodo | Rota | Uso |
| --- | --- | --- |
| `GET` | `/` | Pagina principal do hub com filtros e formulario de criacao. |
| `GET` | `/jobs` | Fragmento HTML da tabela de jobs. |
| `POST` | `/jobs` | Cria um novo job e redireciona para o detalhe. |
| `GET` | `/jobs/{job_id}` | Tela de detalhe/revisao do job. |
| `GET` | `/api/jobs/{job_id}` | JSON compacto com status, request e render. |
| `POST` | `/jobs/{job_id}/review` | Aprova, rejeita ou cria retry a partir de uma etapa. |
| `POST` | `/jobs/{job_id}/publish` | Marca job aprovado como publicado. |
| `POST` | `/hub/prompt` | Salva ou reseta o prompt viral usado pelo hub. |
| `GET` | `/healthz` | Healthcheck do app. |

Arquivos em `data/artifacts/` sao servidos por `/artifacts/...`.

## Providers

`ProviderRegistry` monta os providers conforme `.env`:

- `creative`: `ResilientCreativeProvider`, com primary/fallback/draft/repair/scene configurados por `YTS_LLM_*`.
- `image`: `MockImageProvider` quando `YTS_USE_MOCK_PROVIDERS=true`; caso contrario `MinimaxImageProvider`.
- `stock`: `ResilientStockProvider`, com fallback Pexels/Pixabay/local quando aplicavel.
- `tts`: `LocalSpeechFallbackProvider` em mock; caso contrario `EdgeTTSProvider`.
- `semantic`: `SemanticVerifier`, com heuristica local e verificacao por MiniMax/mmx quando disponivel.
- `local_image`: `LocalSemanticImageProvider`, usado como fallback visual local.

Variaveis principais:

- `YTS_USE_MOCK_PROVIDERS`: liga fluxo local sem API paga.
- `YTS_LLM_PRIMARY_PROVIDER`: provider primario de texto, normalmente `minimax`.
- `YTS_LLM_FALLBACK_PROVIDER`: fallback barato de texto, normalmente `deepseek`.
- `YTS_LLM_SCRIPT_DRAFT_PROVIDER`: provider rapido para o primeiro draft de roteiro, normalmente `deepseek`.
- `YTS_LLM_REPAIR_PROVIDER`: provider forte para repair de roteiro, normalmente `qwen`.
- `YTS_LLM_SCENE_PROVIDER`: provider forte para refazer cenas quando MiniMax falha ou o gate reprova, normalmente `qwen`.
- `YTS_REAL_RUN_ALLOW_MOCK_FALLBACK`: deve ficar `false` em runs reais; impede que falha de provider caia em mock silencioso.
- `YTS_LLM_TOPIC_TIMEOUT_SEC`, `YTS_LLM_SCRIPT_DRAFT_TIMEOUT_SEC`, `YTS_LLM_SCENE_PLAN_TIMEOUT_SEC` e `YTS_LLM_PUBLISH_AUDIT_TIMEOUT_SEC`: limites por papel antes de cair para fallback real, preservando `YTS_STRICT_MINIMAX_VALIDATION`.
- `YTS_ASSET_GENERATION_PARALLELISM`: quantidade de cenas geradas em paralelo; a persistencia no banco continua serializada no worker.
- `YTS_MINIMAX_TEXT_API_KEY`: chave para pauta, roteiro, cenas e auditoria.
- `YTS_DEEPSEEK_API_KEY`: chave do fallback barato OpenAI-compatible.
- `YTS_QWEN_API_KEY`: chave do provider Qwen OpenAI-compatible para repair e cenas.
- `YTS_MINIMAX_IMAGE_API_KEY`: chave para geracao de imagens.
- `YTS_PEXELS_API_KEY` e `YTS_PIXABAY_API_KEY`: fallback de stock.
- `YTS_CHANNEL_AI_GENERATED_CONTENT`: quando `true`, o disclosure de IA e inferido automaticamente no review.

## Pipelines

`JobOrchestrator` coordena jobs, retries, leases, worker e eventos. A execucao de steps pesados fica em `app/pipelines/`:

- `ScriptPipeline`: etapa `script`.
- `ScenePipeline`: etapa `scene_plan`.
- `AssetPipeline`: etapa `asset_generation`.
- `RenderPipeline`: etapa `render`.
- `MonetizationPipeline`: etapa `monetization_readiness_gate`.

O plano de modularizacao e proximos cortes esta em `docs/modularization-plan.md`.

## Qualidade

Os gates ficam em `app/quality/`:

- `ScriptQualityGate`: duracao, idioma, abertura generica, repeticao, densidade e markup estranho.
- `ScenePlanGate`: contagem e estrutura das cenas.
- `AssetGate`: quantidade e score dos assets selecionados.
- `SubtitleGate`: cobertura e drift das legendas.
- `RenderGate`: arquivo decodificavel, duracao, resolucao, bitrate e codecs.

Falhas recuperaveis geram retry quando a etapa permite. Falhas finais registram `ErrorLog`, eventos em `events.jsonl` e `failure_reason` no job.

## Banco de dados

Modelos principais:

- `Job`: estado central, lease do worker, resumo de qualidade e indice de artefatos.
- `TopicRequest`: entrada original do usuario.
- `TopicPlan`: pauta canonica e metadados editoriais.
- `Script`: roteiro, narracao completa e metricas.
- `ScenePlan`: lista JSON das cenas.
- `SceneAsset`: imagens candidatas/selecionadas por cena e scores.
- `NarrationAsset`: audio, SRT bruto, loudness e metadados de voz.
- `SubtitleTrack`: legendas alinhadas e arquivos ASS/SRT.
- `RenderOutput`: MP4 final, poster, duracao, codecs e log FFmpeg.
- `ReviewRecord`: acoes humanas de revisao.
- `FallbackEvent`: trocas de provider/fallbacks.
- `ErrorLog`: falhas estruturadas.
- `StepExecution`: historico de execucao por etapa.
- `TopicRegistry`: topicos aprovados para evitar repeticao.

`app/db.py` usa `create_engine`, `SessionLocal` e `Base.metadata.create_all`. Nao ha sistema de migrations completo; existe apenas script auxiliar em `scripts/migrate_sqlite_to_postgres.py`.

## Artefatos

`StorageManager` grava JSON, texto e bytes no diretorio do job e retorna `file://...`.

Estrutura comum:

```text
data/artifacts/<job_id>/
  request.json
  topic_plan.json
  script.json
  scene_plan.json
  events.jsonl
  assets/
  narration/
  subtitles/
  render/
    final.mp4
    poster.jpg
    ffmpeg.log
  publish_package.json
```

O helper `artifact_url` em `main.py` converte `file://` dentro de `data/artifacts/` para `/artifacts/...`, permitindo abrir videos, imagens e logs pela interface.

## Interface

Templates:

- `app/templates/base.html`: layout base e cabecalho.
- `app/templates/jobs.html`: pagina principal com filtros, tabela e formulario.
- `app/templates/jobs_table.html`: tabela parcial de jobs.
- `app/templates/job_detail.html`: revisao detalhada de roteiro, cenas, assets, audio, legendas, render, fallbacks e erros.

CSS:

- `app/static/styles.css`

O app usa SSR simples com Jinja2. Nao ha build frontend obrigatorio para rodar o hub.

## Testes

Os testes ficam em `tests/test_e2e.py` e cobrem:

- fluxo completo ate `waiting_review`;
- URL de artefatos;
- criacao pelo hub com modo titulo, tom, angulo e prompt SEO;
- prompt viral customizado;
- gates de script, cena, asset, legenda e render;
- fallback/retry;
- normalizacao de audio e uso de FFmpeg.

Rodar:

```bash
pytest -q
```

## Onde alterar

Para adicionar um provider de imagem:

- `app/config.py`: novas variaveis `YTS_*`.
- `app/providers.py`: nova classe e registro no `ProviderRegistry`.
- `app/orchestrator.py`: politica de fallback, metadados e eventos.
- `tests/test_e2e.py`: cobertura do contrato com provider mock.

Para mudar copywriting/prompt do hub:

- `app/main.py`: `DEFAULT_VIRAL_PROMPT_TEMPLATE`, composicao de notas e defaults do hub.
- `data/hub_settings.json`: prompt customizado salvo em runtime.

Para mudar render:

- `app/orchestrator.py`: etapa `_step_render`.
- `app/quality/render_gate.py`: validacoes do MP4.
- `app/templates/job_detail.html`: exibicao do resultado.

Para mudar banco/modelos:

- `app/models.py`: schema SQLAlchemy.
- `app/db.py`: engine/session.
- `scripts/migrate_sqlite_to_postgres.py`: migracao auxiliar quando aplicavel.
