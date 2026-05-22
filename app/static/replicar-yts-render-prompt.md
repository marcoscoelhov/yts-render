# Prompt para replicar o YTS Render

Voce e um agente senior de engenharia de software. Construa um app chamado **YTS Render**, um sistema para gerar Shorts verticais em portugues do Brasil, revisar o resultado em um hub web e preparar publicacao no YouTube.

O objetivo nao e criar uma landing page. O objetivo e criar o produto funcional: um hub operacional que recebe uma ideia, gera um video vertical, valida qualidade tecnica e editorial, deixa uma pessoa revisar, aprovar, agendar e publicar.

## Principios do produto

- O app deve priorizar resultado real, nao demo visual.
- O fluxo deve ser observavel por artifacts persistidos.
- Jobs reais nao devem cair em mock sem deixar isso claro.
- Mock so deve ser usado em modo de teste explicito.
- O hub deve ser uma superficie de decisao: criar, acompanhar, assistir, aprovar, agendar, publicar.
- Diagnosticos tecnicos devem existir, mas ficar atras de paineis secundarios.
- O usuario final precisa conseguir confiar no que aconteceu em cada etapa.

## Stack recomendada

Use:

- Python 3.12
- FastAPI
- SQLAlchemy
- SQLite local por padrao
- Jinja2 para SSR
- HTMX opcional para fragmentos dinamicos
- FFmpeg para renderizacao
- pytest para testes
- Pydantic para schemas e settings

Evite adicionar frontend SPA complexo. A interface deve ser SSR simples, responsiva e operacional.

## Estrutura de repositorio esperada

Crie algo semelhante a:

```text
app/
  main.py
  hub_context.py
  publication_ops.py
  config.py
  db.py
  models.py
  schemas.py
  orchestrator.py
  providers/
    __init__.py
    llm.py
    image.py
    music.py
    tts.py
    registry.py
  templates/
    base.html
    jobs.html
    jobs_table.html
    job_detail.html
    publication_dashboard.html
    calendar.html
  static/
    styles.css
  pipelines/
    script_pipeline.py
    script_fact_pack.py
    script_audit.py
    script_repair.py
    script_metrics.py
    topic_pipeline.py
    scene_pipeline.py
    asset_pipeline.py
    image_assets.py
    tts_assets.py
    subtitle_assets.py
    music_assets.py
    render_pipeline.py
    monetization_pipeline.py
  routes/
    health.py
  quality/
    script_gate.py
    scene_gate.py
    asset_gate.py
    subtitle_gate.py
    render_gate.py
    background_music_gate.py
tests/
  test_e2e.py
docs/
  app.md
  runbook-inicializacao.md
README.md
.env.example
```

## Entidades principais

Modele no banco:

- `Job`: unidade de trabalho do video.
- `TopicRequest`: entrada editorial do usuario.
- `TopicPlan`: pauta, angulo, promessa, termos de busca e candidatos de titulo.
- `Script`: roteiro final narrado.
- `ScenePlan`: cenas derivadas da narracao.
- `SceneAsset`: imagens por cena, scores e selecao.
- `NarrationAsset`: audio de narracao.
- `SubtitleTrack`: legendas alinhadas.
- `BackgroundMusicAsset`: musica de fundo e audio mixado.
- `RenderOutput`: MP4 final.
- `ReviewRecord`: aprovacao, rejeicao e retry humano.
- `PublicationSchedule`: agendamento local ou via YouTube.
- `PerformanceMetric`: metricas manuais do YouTube Studio.
- `FallbackEvent`: fallback explicito por etapa.
- `ErrorLog`: erro persistido.
- `StepExecution`: execucao de cada etapa do pipeline.

## Estados de job

Implemente pelo menos:

```text
queued
running
monetization_review
blocked_for_monetization
ready_for_upload
approved_for_publish
published
rejected
failed
script_quality_failed
scene_plan_quality_failed
asset_quality_failed
subtitle_quality_failed
render_quality_failed
cancelled
```

O sucesso operacional de um render nao significa publicacao pronta. Um job pode terminar em `monetization_review` com MP4 final valido e ainda exigir revisao humana.

## Modos de entrada

O hub deve aceitar tres modos:

1. `theme`: tema bruto. Se vier vazio, o app deve tentar buscar tendencia real e registrar a origem.
2. `title`: titulo completo fornecido pelo usuario. Preserve a promessa central, mas permita otimizar pauta e roteiro.
3. `script`: roteiro pronto em texto rotulado. Preserve o texto como fonte editorial e nao gere outro roteiro.

Formato de roteiro pronto:

```text
Titulo: ...
Hook: ...
Loop: ...
Beats:
- ...
- ...
Payoff: ...
Fechamento: ...
Hashtags: #opcional #opcional
```

Regras:

- `Titulo` e metadado, nao narracao.
- A narracao usa `Hook`, `Loop`, `Beats`, `Payoff` e `Fechamento`.
- `Loop` e tensao editorial, nao claim factual por si so.
- O modo roteiro pronto exige confirmacao humana de factualidade.
- O app nao deve reescrever automaticamente hook, beats, payoff ou fechamento nesse modo.
- Desvios graves de formato ou duracao devem bloquear antes de midia.

## Pipeline

Implemente um worker que rode estas etapas em ordem:

```text
input_gate
topic_plan
script
scene_plan
asset_generation
tts
subtitle_alignment
background_music
render
monetization_readiness_gate
publish_to_review_hub
```

Cada etapa deve:

- criar ou atualizar `StepExecution`
- persistir artifacts JSON ou midia
- emitir eventos em `events.jsonl`
- ter retry limitado quando fizer sentido
- falhar com estado especifico quando for qualidade de etapa

## Artifacts por job

Grave tudo em:

```text
data/artifacts/<job_id>/
```

Artifacts importantes:

```text
request.json
input_gate.json
topic_plan.json
research_brief.json
fact_pack.json
script.json
script_generation_debug.json
script_repair_telemetry.json
text_publish_audit.json
scene_plan_raw.json
scene_plan.json
assets/<scene_id>/ai.jpg
audio/narration.wav
audio/raw.srt
audio/subtitles.ass
audio/background_source.wav
audio/mixed.wav
background_music.json
background_music_debug.json
background_music_quality_report.json
render/final.mp4
render/poster.jpg
render/ffmpeg.log
render_output.json
rights_registry.json
ai_disclosure.json
fact_claims_report.json
metadata_review.json
monetization_report.json
publish_package.json
performance_timeline.json
events.jsonl
```

## Providers de texto

Crie uma interface `LLMProvider` com:

- `plan_topic`
- `generate_script`
- `repair_script`
- `plan_scenes`
- `audit_publish_package`

Implemente providers:

- MiniMax text
- DeepSeek V4 Flash via API OpenAI-compatible
- OpenAI via Responses ou Chat Completions, conforme SDK disponivel
- Mock apenas para testes

Tenha registry configuravel por env:

```env
YTS_LLM_PRIMARY_PROVIDER=deepseek
YTS_LLM_FALLBACK_PROVIDER=deepseek
YTS_LLM_REPAIR_PROVIDER=deepseek
YTS_LLM_SCENE_PROVIDER=deepseek
YTS_LLM_ENABLE_FALLBACK=true
```

O provider de cenas deve ser fallback especializado. O plano de cenas tenta primeiro o primary configurado; se ele falhar ou se o plano nao passar no gate, use `YTS_LLM_SCENE_PROVIDER`.

## Regras de roteiro

O prompt de roteiro deve pedir JSON estrito com:

```text
title
hook
body_beats
ending
cta
full_narration
estimated_duration_sec
key_facts
source_fact_ids
claim_trace
token_count
language
retention_map
visual_opening
qa_metrics
prompt_version
```

Regras editoriais:

- portugues do Brasil
- 35 a 55 segundos
- primeira frase com no maximo 12 palavras
- media por frase <= 14
- frase maxima <= 20 palavras
- 80 a 120 palavras quando possivel
- sem travessao nos campos narrados
- sem markup, SSML, HTML ou XML
- sem mistura de idiomas
- sem comeco generico como "voce sabia"
- sem frases com cara de IA
- uma ideia central por short
- hook de choque ou contraste
- loop aberto
- escalada de beats
- payoff no ultimo terco
- fechamento que recontextualiza o hook
- fatos acima de viralidade
- se houver fact pack verificado, claims de risco devem mapear `claim_trace`

## Gate de roteiro

Crie `ScriptQualityGate` que bloqueie ou sinalize:

- `missing_full_narration`
- `language_field_not_pt_br`
- `markup_or_ssml_leaked`
- `em_dash_or_en_dash_detected`
- `non_latin_text_detected`
- `foreign_language_detected`
- `suspicious_glued_words`
- `generic_hook_opening`
- `generic_ai_style_phrase`
- `placeholder_source_language`
- `truncated_ending_logic`
- `repeated_clause`
- `overconfident_or_unsupported_factual_claim`
- `factual_claim_trace_missing`
- `factual_risk_requires_conservative_rewrite`
- `ending_not_connected_to_hook`
- `weak_loop_closure`
- `estimated_duration_outside_absolute_range`
- `avg_sentence_too_long`
- `sentence_too_long`
- score faltando ou abaixo do minimo

Normalize scores de providers que vierem em escala `0-10` para `0-1`.

## Factualidade

Nao use Wikipedia como fonte confiavel, principalmente para ciencia, medicina, engenharia, historia ou tecnologia.

Prefira:

- artigos cientificos
- OpenAlex ou indices academicos
- fontes institucionais
- documentacao oficial
- fontes primarias

Se nao houver fonte boa, use linguagem conservadora e evite numeros, datas ou causalidade forte.

## Planejamento de cenas

O plano de cenas deve:

- cobrir toda a narracao
- manter ordem narrativa
- dividir por tokens ou trechos da narracao
- gerar `image_prompt` em ingles para o provider de imagem
- manter campos textuais internos em pt-BR
- nao inventar beats novos
- nao colocar texto nas imagens

Campos por cena:

```text
scene_id
order
narration_text
token_start
token_end
estimated_duration_sec
visual_intent
primary_subject
image_prompt
fallback_queries
```

Valide:

- quantidade esperada de cenas
- cobertura do primeiro ao ultimo token
- sem buracos de narracao
- prompts com sujeito visual claro

## Imagens

Use MiniMax para imagens reais.

Regra de chaves:

1. imagem tenta primeiro a chave resolvida de texto MiniMax
2. se a chave de texto bater limite de provedor, marque como esgotada para o job atual
3. use `YTS_MINIMAX_IMAGE_API_KEY`
4. se nao houver chave de texto, use diretamente `YTS_MINIMAX_IMAGE_API_KEY`
5. se nenhuma chave existir, falhe

Limite de provedor significa quota, saldo, credito ou rate limit. Timeout, 5xx, conexao e resposta invalida sao falhas transientes, nao motivo para trocar de chave.

Persistir metadata:

```text
credential_role
fallback_from_text_key
text_key_exhausted_for_job
```

Em modo real, nao use imagem mock como fallback silencioso. Se usar fallback local por qualidade, registre `FallbackEvent`.

## TTS e legendas

Use Edge TTS ou provider equivalente.

Regras:

- audio em WAV local
- loudness normalizado
- legendas SRT ou ASS
- cobertura de legenda >= 99%
- drift medido e registrado
- chunks curtos o suficiente para Shorts

## Musica de fundo

Use Banco de Trilhas Aprovadas como caminho primario para jobs reais. MiniMax Music so deve ser usado quando `YTS_BACKGROUND_MUSIC_PROVIDER=minimax` ou quando fallback por API estiver explicitamente habilitado.

Regras obrigatorias:

- `YTS_BACKGROUND_MUSIC_GAIN_DB=-17.0`
- `YTS_BACKGROUND_MUSIC_PROVIDER=local_bank` por padrao
- o banco local deve usar apenas faixas com origem e licenca rastreaveis e `approved_for_youtube=true`
- `YTS_MUSIC_BANK_AUTO_POPULATE=true` pode criar trilhas sinteticas locais, sem download externo, quando o banco estiver vazio
- trilhas MiniMax antigas com quality gate aprovado podem ser importadas para o banco e devem ter prioridade sobre trilhas sinteticas locais
- mock de musica so pode existir quando `YTS_USE_MOCK_PROVIDERS=true`
- se a fonte de musica configurada falhar em job real, a etapa `background_music` deve falhar
- nao caia para mock em job real
- persista `background_music_debug.json` com erro, payload, `base_resp`, `trace_id`, `data_keys`, `extra_info`, `analysis_info`
- use `output_format=url` no payload MiniMax quando MiniMax Music estiver configurado
- converta a fonte selecionada para WAV
- se o provider devolver duracao maior que o alvo, corte localmente
- misture abaixo da narracao com `sidechaincompress + amix + loudnorm`
- render deve usar `mixed_audio_uri` quando existir

Payload MiniMax Music recomendado:

```json
{
  "model": "music-2.6",
  "prompt": "...",
  "lyrics": "",
  "is_instrumental": true,
  "lyrics_optimizer": false,
  "output_format": "url",
  "audio_setting": {
    "sample_rate": 44100,
    "bitrate": 256000,
    "format": "mp3"
  }
}
```

Se a resposta vier sem `data.audio`, trate como falha. Se `base_resp.status_msg` indicar limite, exponha isso no debug.

## Render

Use FFmpeg para gerar:

- MP4 vertical
- `1080x1920`
- H.264
- AAC
- 30fps
- poster JPG
- log FFmpeg

Valide:

- arquivo existe
- duracao dentro da janela
- bitrate minimo
- resolucao correta
- audio presente
- poster existe

## Monetization readiness

Depois do render, gere relatorios:

- direitos de uso
- disclosure de IA
- fact claims
- repeticao de canal
- metadata review
- monetization report
- publish package

Estados:

- se tudo automatico passar: `ready_for_upload`
- se render esta bom mas requer revisao: `monetization_review`
- se houver bloqueio serio: `blocked_for_monetization`

Nao confunda render valido com video publicavel.

## Hub web

Crie rotas:

```text
GET  /
GET  /jobs
POST /jobs
GET  /api/jobs/{job_id}
GET  /jobs/{job_id}
POST /jobs/{job_id}/review
POST /jobs/{job_id}/publish-metadata
POST /jobs/{job_id}/publish
POST /jobs/{job_id}/schedule
POST /jobs/{job_id}/reopen-publication
POST /jobs/{job_id}/performance
GET  /publication-hub
GET  /calendar
POST /calendar/schedule
GET  /youtube/connect
GET  /youtube/oauth/callback
POST /youtube/disconnect
GET  /healthz
```

Hub deve mostrar:

- formulario de criacao
- fila paginada de jobs
- filtros por status
- resumo operacional
- detalhe do job
- video final
- checklist de review
- aprovar, rejeitar e retry
- agendar
- publicar
- calendario mensal
- estado da integracao YouTube
- artifacts e telemetria em paineis recolhidos

## Calendario

O calendario nao e apenas visualizacao.

Ele deve:

- mostrar programados e publicados
- permitir clicar em um dia
- listar jobs `approved_for_publish` sem agenda ativa
- criar `PublicationSchedule`
- respeitar timezone
- mostrar status `scheduled`, `publishing`, `publish_failed`, `published`, `cancelled`

## YouTube

Implemente dois modos:

```env
YTS_YOUTUBE_PUBLISH_MODE=manual
YTS_YOUTUBE_API_ENABLED=false
```

Modo manual:

- hub registra publicacao feita fora
- exige `youtube_video_id` ou `youtube_url`

Modo API:

- OAuth
- upload via YouTube Data API
- agendamento nativo quando possivel
- worker processa slots vencidos

## Configuracao

Use `.env` com:

```env
YTS_USE_MOCK_PROVIDERS=false
YTS_DATABASE_URL=sqlite:///data/yts_render.db
YTS_DATA_DIR=data

YTS_LLM_PRIMARY_PROVIDER=deepseek
YTS_LLM_FALLBACK_PROVIDER=deepseek
YTS_LLM_REPAIR_PROVIDER=deepseek
YTS_LLM_SCENE_PROVIDER=deepseek

YTS_MINIMAX_API_KEY=
YTS_MINIMAX_TEXT_API_KEY=
YTS_MINIMAX_IMAGE_API_KEY=
YTS_MINIMAX_MUSIC_API_KEY=
YTS_MINIMAX_TEXT_BASE_URL=https://api.minimax.io/v1
YTS_MINIMAX_IMAGE_BASE_URL=https://api.minimax.io/v1/image_generation
YTS_MINIMAX_MUSIC_BASE_URL=https://api.minimax.io/v1
YTS_MINIMAX_TEXT_TIMEOUT_SEC=180
YTS_MINIMAX_MUSIC_TIMEOUT_SEC=240

YTS_DEEPSEEK_API_KEY=
YTS_DEEPSEEK_BASE_URL=https://api.deepseek.com
YTS_DEEPSEEK_MODEL=deepseek-v4-flash
YTS_DEEPSEEK_TIMEOUT_SEC=90

YTS_BACKGROUND_MUSIC_ENABLED=true
YTS_BACKGROUND_MUSIC_GAIN_DB=-17.0

YTS_YOUTUBE_PUBLISH_MODE=manual
YTS_YOUTUBE_API_ENABLED=false
```

## Observabilidade

Implemente:

- logs por etapa no stdout
- `events.jsonl`
- `StepExecution`
- `performance_timeline.json`
- debug JSON por provider
- artifacts de repair
- fallback events

Ao rodar pelo terminal, imprima algo como:

```text
[yts HH:MM:SS] job=<id> stage=script started 3/11 attempt=1/3
[yts HH:MM:SS] job=<id> stage=script done 3/11 16.3s
```

## Testes

Crie testes para:

- pipeline completo com mock providers
- `ScriptQualityGate`
- DeepSeek V4 Flash provider OpenAI-compatible
- fallback de LLM
- imagem MiniMax usando chave de texto antes da chave dedicada
- troca para chave dedicada apenas por limite/quota
- timeout de imagem nao troca chave
- MiniMax Music sem mock em modo real
- mock music permitido apenas em mock mode
- calendario criando schedule
- publish manual
- OAuth YouTube
- artifact retention
- render gate
- subtitle gate

Comando:

```bash
pytest -q
```

## Criterios de aceite

O app esta pronto quando:

- `POST /jobs` cria job
- worker executa todas as etapas
- MP4 final e gerado em `render/final.mp4`
- hub abre o job
- video toca no hub
- artifacts existem
- provider usado aparece nos artifacts
- mocks nao entram em run real sem configuracao explicita
- falha de MiniMax Music falha a etapa em run real
- calendario agenda job aprovado
- testes principais passam
- README e docs explicam como operar

## Pedido final para o agente executor

Construa esse app de forma incremental. Comece pelo modelo de dados, schemas, pipeline mockado e hub basico. Depois adicione providers reais, gates, render, musica, publicacao e calendario. Mantenha cada etapa testavel, persistida e observavel. Nao entregue apenas prototipo visual. Entregue um sistema que gere, valide e revise Shorts de ponta a ponta.
