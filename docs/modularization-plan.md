# Plano de Modularizacao do Orquestrador

## Objetivo

Reduzir `JobOrchestrator` a uma casca de coordenacao: lifecycle de job, worker, retry, lease, eventos, persistencia comum e delegacao de steps.

As regras de dominio devem morar em pipelines dedicados, mantendo os mesmos artifacts, `quality_summary` e estados publicos.

## Corte Implementado

- `app/pipelines/script_pipeline.py`: entrada da etapa `script`.
- `app/pipelines/scene_pipeline.py`: entrada da etapa `scene_plan` e fallback de cenas.
- `app/pipelines/asset_pipeline.py`: entrada das etapas de assets, TTS, legendas e musica.
- `app/pipelines/image_assets.py`: adaptador de dominio para assets visuais.
- `app/pipelines/tts_assets.py`: adaptador de dominio para TTS e ajuste de duracao.
- `app/pipelines/subtitle_assets.py`: adaptador de dominio para segmentacao e renderizacao de legendas.
- `app/pipelines/music_assets.py`: adaptador de dominio para musica de fundo e sound design.
- `app/pipelines/render_pipeline.py`: entrada da etapa `render`, retry de FFmpeg e mutacao segura do comando.
- `app/pipelines/monetization_pipeline.py`: entrada da etapa `monetization_readiness_gate`, rights, disclosure, fact claims, repeticao, metadata, publish package, hashtags, readiness e auditoria de publish.
- `app/pipelines/common.py`: exceptions de step e helper `model_payload`.

O `JobOrchestrator` continua expondo os mesmos metodos publicos e os mesmos step names. A compatibilidade foi preservada para UI, testes e scripts operacionais.

## Proximo Corte Seguro

1. Transformar os adaptadores de asset em donos da implementacao, reduzindo gradualmente os wrappers privados no `AssetPipeline`.
2. Remover wrappers privados do `JobOrchestrator` quando os consumidores antigos forem migrados para os pipelines.
3. Revisar imports mortos em `app/orchestrator.py` depois da remocao dos wrappers de compatibilidade.

## Contratos Que Nao Devem Quebrar

- Artifacts: `fact_pack.json`, `script.json`, `scene_plan.json`, `render_output.json`, `monetization_report.json`.
- Estados terminais: `monetization_review`, `blocked_for_monetization`, `ready_for_upload`.
- Chaves de `quality_summary`: `script`, `scene_plan`, `assets`, `render`, `monetization`.
- Eventos em `events.jsonl`.
