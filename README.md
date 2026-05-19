# YTS Render

App FastAPI para gerar Shorts verticais em pt-BR, revisar o resultado em um hub web e publicar no YouTube em fluxo manual ou via API.

O produto atual nao termina em "video pronto". Ele cobre criacao do job, pipeline multimidia, gate de monetizacao, aprovacao humana, agenda de publicacao, calendario, metadados de upload e integracao OAuth com YouTube.

## Estado atual

- Hub SSR em `http://127.0.0.1:8080`, com lista paginada de jobs, detalhe focado em aprovar e agendar, dashboard de publicacao e calendario mensal.
- Worker em thread, iniciado no lifespan do FastAPI, responsavel pelo pipeline e tambem pela publicacao agendada quando o modo YouTube esta em `api`.
- Banco padrao em SQLite e artefatos em `data/artifacts/<job_id>/`.
- Integracao real com YouTube disponivel por OAuth e upload via API quando `YTS_YOUTUBE_PUBLISH_MODE=api` e `YTS_YOUTUBE_API_ENABLED=true`.
- Politica de retencao automatica para artefatos temporarios: jobs continuam visiveis no hub mesmo depois da limpeza dos arquivos pesados.

## Comeco rapido

```bash
git clone https://github.com/marcoscoelhov/yts-render.git
cd yts-render

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cp .env.example .env
```

Para rodar sem custo de API:

```env
YTS_USE_MOCK_PROVIDERS=true
YTS_DATABASE_URL=sqlite:///data/yts_render.db
YTS_DATA_DIR=data
```

Para subir o app:

```bash
scripts/install_systemd_service.sh
```

O servico systemd fixa o hub em `127.0.0.1:8080`, reinicia em falhas e roda um
port guard antes do start para liberar instancias antigas do proprio YTS
Render. O instalador renderiza a unit de `deploy/systemd/yts-render-hub.service.in`
com o caminho real do checkout. Para desenvolvimento manual sem systemd:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Validacao minima:

```bash
curl http://127.0.0.1:8080/healthz
```

## Fluxo do produto

1. `POST /jobs` cria um job.
2. O worker processa `input_gate`, `topic_plan`, `script`, `scene_plan`, `asset_generation`, `tts`, `subtitle_alignment`, `background_music`, `render`, `monetization_readiness_gate` e `publish_to_review_hub`.
3. O job termina em `monetization_review`, `blocked_for_monetization` ou `ready_for_upload`.
4. O revisor abre `/jobs/{job_id}`, assiste ao video, confere checklist e aprova ou rejeita.
5. Job aprovado vira `approved_for_publish`.
6. O operador pode salvar metadados de upload, agendar data e hora, publicar imediatamente, ou reabrir para republicacao depois de um publish errado.
7. O calendario tambem permite escolher um dia e agendar um job aprovado que ainda nao esteja publicado nem tenha agenda ativa.
8. Quando o modo YouTube esta em `api`, o worker consome agendas vencidas e faz o upload automaticamente.
9. Quando o modo esta em `manual`, o hub continua util para aprovacao, agenda local e registro de publicacao manual.

## Entradas do hub

O formulario principal aceita tres modos:

- `Tema`: assunto bruto. Se ficar vazio, o app tenta buscar tendencia real automaticamente e registra a origem no job.
- `Titulo completo`: promessa editorial fornecida pelo operador, que o app usa como direcao central.
- `Roteiro pronto`: texto rotulado fornecido por uma pessoa e preservado como fonte editorial.

O formato canonico de `Roteiro pronto` e:

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

Nesse modo, `Titulo` vira metadado, a narracao usa `Hook`, `Loop`, `Beats`, `Payoff` e `Fechamento`, e o app exige confirmacao humana de factualidade antes de aceitar o job. `Loop` e tensao narrativa, nao claim factual a ser mapeada como fonte.

## Estados principais

### Jobs

| Status | Significado |
| --- | --- |
| `queued` | Job criado e aguardando worker. |
| `running` | Pipeline em execucao. |
| `monetization_review` | Render pronto, mas ainda faltam confirmacoes humanas. |
| `blocked_for_monetization` | Houve bloqueio de compliance, factualidade, direitos ou qualidade. |
| `ready_for_upload` | Passou no gate final e esta pronto para aprovacao humana. |
| `approved_for_publish` | Aprovado no hub e liberado para agenda/publicacao. |
| `published` | Publicado e registrado pelo hub. |
| `rejected` | Reprovado na revisao humana. |
| `failed` | Falha geral no pipeline. |

Tambem existem falhas especificas por etapa, como `script_quality_failed`, `scene_plan_quality_failed`, `asset_quality_failed`, `subtitle_quality_failed` e `render_quality_failed`.

### Agenda de publicacao

| Status | Significado |
| --- | --- |
| `scheduled` | Slot salvo e aguardando horario. |
| `publishing` | Upload em andamento pelo worker. |
| `publish_failed` | Tentativa de publicacao falhou. |
| `published` | Publicacao concluida e registrada. |
| `cancelled` | Agenda limpa ou reaberta para republicacao. |

## Configuracao

O arquivo `.env.example` agora e exaustivo e segue `app/config.py` como fonte de verdade.

Blocos mais importantes:

- app e hub: `YTS_APP_URL`, `YTS_HUB_AUTH_TOKEN`
- banco e SQLite: `YTS_DATABASE_URL`, `YTS_SQLITE_*`
- defaults editoriais: `YTS_NICHE_ID`, `YTS_LANGUAGE`, `YTS_SIMPLE_SHORTS_MODE`
- providers e timeouts: `YTS_LLM_*`, `YTS_MINIMAX_*`, `YTS_OPENAI_*`, `YTS_DEEPSEEK_*`, `YTS_QWEN_*`
- YouTube: `YTS_YOUTUBE_PUBLISH_MODE`, `YTS_YOUTUBE_API_ENABLED`, `YTS_YOUTUBE_CLIENT_ID`, `YTS_YOUTUBE_CLIENT_SECRET`, `YTS_YOUTUBE_OAUTH_REDIRECT_URI`
- retencao: `YTS_ARTIFACT_RETENTION_*`

Defaults atuais importantes:

- `YTS_SIMPLE_SHORTS_MODE=true`
- `YTS_LLM_PRIMARY_PROVIDER=minimax`
- `YTS_LLM_FALLBACK_PROVIDER=deepseek`
- `YTS_YOUTUBE_PUBLISH_MODE=manual`
- `YTS_YOUTUBE_API_ENABLED=false`

### MiniMax para imagens

A geracao de imagens usa a mesma chave resolvida de texto MiniMax como credencial primaria:

```env
YTS_MINIMAX_TEXT_API_KEY=...
YTS_MINIMAX_IMAGE_API_KEY=...
```

`YTS_MINIMAX_IMAGE_API_KEY` e a **Chave Dedicada de Imagem**. Ela so e usada quando a chave de texto retorna limite de provedor, como quota, saldo, credito ou rate limit. Timeout, erro de conexao e `5xx` nao disparam troca de chave. Se nao houver chave de texto configurada, a chave dedicada de imagem e usada diretamente.

## YouTube e OAuth

Para upload real via API:

```env
YTS_USE_MOCK_PROVIDERS=false
YTS_YOUTUBE_PUBLISH_MODE=api
YTS_YOUTUBE_API_ENABLED=true
YTS_YOUTUBE_CLIENT_ID=...
YTS_YOUTUBE_CLIENT_SECRET=...
YTS_YOUTUBE_CHANNEL_ID=...
```

Depois de subir o app:

1. abra `/youtube/connect`
2. conclua o OAuth do canal
3. verifique o token salvo em `data/youtube_oauth_token.json`
4. use o hub para aprovar, agendar ou publicar

Quando `YTS_YOUTUBE_OAUTH_REDIRECT_URI` estiver vazio, o app usa a URL atual do hub como callback efetivo.

## Artefatos e retencao

Cada job grava arquivos em `data/artifacts/<job_id>/`.

Exemplos comuns:

```text
request.json
topic_plan.json
script.json
scene_plan.json
events.jsonl
render/final.mp4
render/poster.jpg
publish_package.json
publication_schedule.json
youtube_publish_attempts.json
```

O worker tambem executa uma limpeza periodica de artefatos temporarios:

- falha critica: `24h`
- job corrigivel ou reaproveitavel: `7 dias`
- job pronto para publicar ou com agenda ativa: `21 dias`

Essa limpeza remove os arquivos pesados, mas preserva o job no banco e no hub. Quando isso acontece, o detalhe do job mostra aviso de retencao e usa `retention_cleanup.json` para manter metadados e historico basico.

## Interface

Rotas principais:

- `/`: home do hub com formulario, resumo do fluxo e jobs
- `/publication-hub`: centro de publicacao
- `/calendar`: calendario de slots programados e publicados, com atalho para agendar jobs aprovados livres
- `/jobs/{job_id}`: detalhe do job, revisao, agenda, metadados e performance
- `/youtube/connect`: inicio do OAuth
- `/healthz`: healthcheck

## Testes

Suite principal:

```bash
pytest -q
```

A cobertura mais importante hoje esta em `tests/test_e2e.py` e inclui:

- pipeline completo ate review
- UI do hub
- aprovacao e agenda
- publish manual e via API
- OAuth do YouTube
- retencao de artefatos

## Exposicao por Tailscale

Mantendo o app local em uma porta `127.0.0.1`:

```bash
tailscale serve --bg http://127.0.0.1:8080
```

Valide a URL final com:

```bash
curl https://<hostname>.<tailnet>/healthz
```

## Documentacao tecnica

- [docs/app.md](docs/app.md): arquitetura, estados, rotas, persistencia e operacao tecnica
- [docs/runbook-inicializacao.md](docs/runbook-inicializacao.md): passos operacionais para subir, validar e usar o hub
