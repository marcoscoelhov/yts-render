# Modularizacao Forte IA-friendly

## Objetivo

Reduzir `JobOrchestrator` a uma casca de coordenacao: lifecycle de job, worker, retry, lease, eventos, persistencia comum e delegacao de steps.

As regras de dominio devem morar em pipelines dedicados, mantendo os mesmos artifacts, `quality_summary` e estados publicos.

## Criterio IA-friendly

Uma area e considerada IA-friendly quando uma mudanca comum exige contexto de no maximo dois ou tres arquivos principais, com ownership claro e poucos acoplamentos privados entre modulos.

O objetivo nao e apenas separar arquivos. O modulo deve permitir manutencao local por area, reduzir necessidade de carregar `JobOrchestrator`, `providers.py` ou suites monoliticas inteiras e preservar contratos publicos com testes focados.

## Status

Concluido como baseline estavel. A modularizacao forte esta pronta para manutencao via IA porque os dominios principais ja tem owners explicitos, a suite foi dividida por dominio e os contratos externos do app foram preservados.

Ainda existem cortes incrementais possiveis, mas eles nao bloqueiam a manutencao segura: reduzir imports legados do orquestrador, mover rotas SSR para routers mais finos e adicionar mais testes unitarios para helpers pequenos.

## Corte Implementado

- `app/pipelines/script_pipeline.py`: entrada da etapa `script`.
- `app/pipelines/script_fact_pack.py`: dono de fact pack, queries, OpenAlex e alinhamento factual.
- `app/pipelines/script_audit.py`: dono da auditoria textual pre-assets.
- `app/pipelines/script_repair.py`: dono de pos-processamento, claim trace, consistencia factual e repair do roteiro.
- `app/pipelines/script_metrics.py`: normalizacao de metricas de roteiro.
- `app/pipelines/topic_pipeline.py`: dono de `topic_plan`, historico recente, learning brief, normalizacao e registry de topicos.
- `app/pipelines/scene_pipeline.py`: entrada da etapa `scene_plan` e fallback de cenas.
- `app/pipelines/asset_pipeline.py`: entrada das etapas de assets, TTS, legendas e musica.
- `app/pipelines/image_assets.py`: dono de geracao primaria, normalizacao de URI, score semantico, thresholds e prompts visuais.
- `app/pipelines/tts_assets.py`: dono do ajuste de duracao de TTS, escala de SRT e medicao de audio.
- `app/pipelines/subtitle_assets.py`: dono da segmentacao, reparo de fronteiras, drift e renderizacao de legendas.
- `app/pipelines/music_assets.py`: dono de debug, mix com repair, mix direto, musica de fundo e sound design.
- `app/pipelines/render_pipeline.py`: entrada da etapa `render`, retry de FFmpeg e mutacao segura do comando.
- `app/pipelines/monetization_pipeline.py`: entrada da etapa `monetization_readiness_gate`, rights, disclosure, fact claims, repeticao, metadata, publish package, hashtags, readiness e auditoria de publish.
- `app/pipelines/common.py`: exceptions de step e helper `model_payload`.
- `app/providers/`: providers separados por dominio, mantendo `app.providers` como fachada publica compativel.
- `app/publication_ops.py`: dono de review, publicacao, agenda por canal, retencao de artifacts, sync YouTube e fila TikTok.
- `app/hub_context.py`: dono dos context builders do hub, calendario, listas, status operacional e integracoes.
- `app/routes/health.py`: router isolado para `/healthz`.
- `tests/`: suites divididas por dominio, com `tests/e2e_support.py` e `tests/conftest.py` para fixtures/helpers compartilhados.

O `JobOrchestrator` continua expondo os mesmos metodos publicos e os mesmos step names. A compatibilidade foi preservada para UI, CLI, automacao, artifacts, estados e scripts operacionais.

Os wrappers privados de dominio do `JobOrchestrator` nao sao API estavel. Testes e manutencao devem chamar o modulo dono diretamente, por exemplo `script_pipeline`, `asset_pipeline`, `scene_pipeline`, `render_pipeline` ou `monetization_pipeline`.

`app.providers` e fachada de compatibilidade. Novas manutencoes devem preferir os modulos donos: `app.providers.llm`, `app.providers.image`, `app.providers.music`, `app.providers.tts` e `app.providers.registry`.

`app.main` ainda concentra rotas SSR principais, mas query/contexto de hub ficam em `HubContext`. Mudancas em listas, calendario, status operacional e dashboard de publicacao devem comecar por `app/hub_context.py`.

`tests/test_e2e.py` e apenas ancora de compatibilidade. Novos testes devem entrar na suite de dominio correspondente: `test_hub_publication.py`, `test_orchestrator_flow.py`, `test_pipeline_assets.py`, `test_pipeline_script.py` ou `test_providers_integrations.py`.

## Tasklist Mestre

- [x] Fase 1: dividir `app.providers` em package por dominio.
- [x] Fase 2: remover ownership de dominio dos wrappers privados do `JobOrchestrator`.
- [x] Fase 3: tornar `subtitle_assets.py` dono de legendas.
- [x] Fase 4: tornar `tts_assets.py` dono de ajuste de duracao e SRT.
- [x] Fase 5: tornar `image_assets.py` dono de assets visuais.
- [x] Fase 6: tornar `music_assets.py` dono de musica de fundo e sound design.
- [x] Fase 7: extrair `topic_plan` do `JobOrchestrator`.
- [x] Fase 8: extrair publicacao, agenda por canal e retencao de artifacts do `JobOrchestrator`.
- [x] Fase 9: dividir `script_pipeline.py` em fact pack, auditoria textual e repair.
- [x] Fase 10: dividir `main.py` em routers e context builders.
- [x] Fase 11: dividir `tests/test_e2e.py` em suites por modulo, mantendo poucos e2e reais.

## Evidencia de Validacao

Validacao local do baseline:

```bash
.venv/bin/python -m pytest -q
```

Resultado validado: `255 passed, 4 warnings`.

Validacao real isolada, sem mock providers, gerou um job completo ate `monetization_review`, com OpenAI para pauta/roteiro, MiniMax para imagens, EdgeTTS para narracao, banco local para musica, MP4 final 1080x1920 e artifacts persistidos. O fluxo de revisao, agenda manual, publicacao manual e metricas de performance tambem foi validado.

O OAuth real do YouTube foi validado fora do diretorio isolado: `data/youtube_oauth_token.json` existe, refresh do token passou e chamada read-only `channels.list(mine=True)` retornou o canal configurado. Upload nativo real nao foi executado para evitar publicar/agendar conteudo externo sem autorizacao explicita.

`agent-browser` foi instalado e validado com `agent-browser doctor --offline --quick`.

## Proximos Cortes Seguros

1. Reduzir imports e helpers legados de `app/orchestrator.py` depois de confirmar que nenhum modulo novo depende deles.
2. Avaliar se rotas de publicacao e jobs devem sair de `app/main.py` para routers completos, agora que `HubContext` ja isolou os builders.
3. Criar testes unitarios menores para `PublicationOperations`, `HubContext` e dominios de script, reduzindo dependencia dos e2e longos.
4. Validar upload nativo real no YouTube somente quando houver autorizacao explicita para criar ou agendar conteudo externo no canal.

## Contratos Que Nao Devem Quebrar

- Artifacts: `fact_pack.json`, `script.json`, `scene_plan.json`, `render_output.json`, `monetization_report.json`.
- Artifacts de publicacao: `publish_package.json`, `publication_schedule.json`, `youtube_publish_attempts.json`, `publish_result.json`.
- Estados terminais ou operacionais: `monetization_review`, `blocked_for_monetization`, `ready_for_upload`, `approved_for_publish`, `published`, `rejected`.
- Chaves de `quality_summary`: `script`, `scene_plan`, `assets`, `render`, `monetization`.
- Eventos em `events.jsonl`.
- Step names em `JobOrchestrator._steps()`, porque aparecem em progresso, telemetria e artifacts.

## Regra Para Novas Mudancas

Antes de editar, identifique o owner do dominio em `docs/app.md`. Se a mudanca exigir abrir `app/orchestrator.py`, `app/main.py` e varios pipelines ao mesmo tempo, provavelmente a fronteira esta vazando e deve ser corrigida com um helper pequeno no modulo dono.
