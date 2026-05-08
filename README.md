# YTS Render

MVP para gerar YouTube Shorts com IA, renderizar um MP4 vertical e revisar o resultado em um hub web local.

O projeto cria um `job` a partir de um tema, gera pauta, roteiro, plano de cenas, imagens, narracao, legendas e video final. Depois publica tudo em uma interface SSR para revisao humana antes de aprovar ou pedir retry.

## Estado Atual

- App FastAPI rodando com `uvicorn`.
- Hub web em `http://127.0.0.1:8080`.
- Pipeline assincrono com worker em thread iniciado no lifespan do FastAPI.
- Banco padrao local em SQLite, configuravel para PostgreSQL por `.env`.
- Artefatos de runtime em `data/artifacts/<job_id>/`.
- Provedores atuais:
  - texto: MiniMax, quando `YTS_MINIMAX_TEXT_API_KEY` ou `YTS_MINIMAX_API_KEY` esta configurado;
  - imagem: MiniMax dedicado, quando `YTS_MINIMAX_IMAGE_API_KEY` ou `YTS_MINIMAX_API_KEY` esta configurado;
  - TTS: Edge TTS, com fallback local;
  - imagem fallback: imagem semantica local, Pexels e Pixabay quando houver chaves;
  - mock providers para teste com `YTS_USE_MOCK_PROVIDERS=true`.
- Testes E2E em `tests/test_e2e.py`.

Importante: o repositorio nao versiona `.env`, banco, videos, imagens geradas, `.venv`, `node_modules` nem caches. Ao clonar, voce recupera o codigo e a configuracao exemplo, nao o historico local de jobs.

## Comeco Rapido Depois do Clone

```bash
git clone https://github.com/marcoscoelhov/yts-render.git
cd yts-render

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

cp .env.example .env
```

Para rodar sem gastar API, edite `.env` e use:

```env
YTS_USE_MOCK_PROVIDERS=true
YTS_DATABASE_URL=sqlite:///data/yts_render.db
```

Suba o app:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Abra:

```text
http://127.0.0.1:8080
```

Healthcheck:

```bash
curl http://127.0.0.1:8080/healthz
```

## Configuracao

As variaveis usam prefixo `YTS_` e sao lidas de `.env`.

Principais variaveis:

| Variavel | Uso |
| --- | --- |
| `YTS_APP_HOST` | Host do app, normalmente `127.0.0.1`. |
| `YTS_APP_PORT` | Porta do app, normalmente `8080`. |
| `YTS_DATA_DIR` | Diretorio de banco/artefatos, padrao `data`. |
| `YTS_DATABASE_URL` | URL SQLAlchemy. SQLite por padrao, PostgreSQL opcional. |
| `YTS_TARGET_DURATION_SEC` | Duracao alvo do Short, validada entre 25 e 45 segundos. |
| `YTS_SCENE_TARGET_COUNT` | Numero alvo de cenas, padrao `6`. |
| `YTS_USE_MOCK_PROVIDERS` | `true` para rodar local sem chamar APIs pagas. |
| `YTS_MINIMAX_API_KEY` | Chave MiniMax legada usada como fallback para texto e imagem. |
| `YTS_MINIMAX_TEXT_API_KEY` | Chave MiniMax dedicada para pauta, roteiro e plano de cenas. |
| `YTS_MINIMAX_IMAGE_API_KEY` | Chave MiniMax dedicada para geracao de imagens. |
| `YTS_MINIMAX_TEXT_TIMEOUT_SEC` | Timeout por chamada do provider de texto. |
| `YTS_MINIMAX_MUSIC_TIMEOUT_SEC` | Timeout por chamada do provider de musica MiniMax. |
| `YTS_MINIMAX_SCENE_PLAN_TIMEOUT_SEC` | Timeout especifico do planejamento de cenas antes de fallback local. |
| `YTS_PEXELS_API_KEY` | Chave Pexels para fallback visual. |
| `YTS_PIXABAY_API_KEY` | Chave Pixabay para fallback visual. |
| `YTS_TAILSCALE_HOSTNAME` | Nome usado no healthcheck/serve Tailscale. |
| `YTS_TAILNET_DOMAIN` | Dominio tailnet usado no healthcheck. |

Exemplo PostgreSQL local:

```env
YTS_DATABASE_URL=postgresql+psycopg://yts_render:yts_render@127.0.0.1:5432/yts_render
```

Se usar PostgreSQL, garanta que o driver `psycopg` esteja instalado no ambiente. No host atual ele ja estava disponivel, mas ele nao esta listado no `pyproject.toml`.

## Fluxo do Produto

1. A pagina inicial lista jobs e oferece formulario para criar um Short.
2. `POST /jobs` cria um job com `seed_theme`, idioma, nicho, tom e duracao.
3. O worker pega jobs `queued` e executa as etapas.
4. Quando o render termina, o job vai para `waiting_review`.
5. A tela `/jobs/{job_id}` mostra roteiro, cenas, assets, fallback events, audio e video.
6. O revisor pode aprovar, rejeitar ou pedir retry.
7. Ao aprovar, o topico entra no registro de aprovados para evitar repeticao futura.

Estados comuns:

| Status | Significado |
| --- | --- |
| `queued` | Job criado e aguardando worker. |
| `running` | Pipeline em execucao. |
| `waiting_review` | Video pronto para revisao humana. |
| `approved` | Revisado e aprovado. |
| `rejected` | Revisado e rejeitado. |
| `failed` | Pipeline falhou apos retries. |

## Etapas do Pipeline

As etapas ficam em `app/orchestrator.py`:

| Etapa | O que faz | Retry |
| --- | --- | --- |
| `input_gate` | Valida entrada e parametros. | 0 |
| `topic_plan` | Gera pauta e evita topicos muito repetidos. | 2 |
| `script` | Gera roteiro curto e aplica gate de qualidade. | 2 |
| `scene_plan` | Divide roteiro em cenas com prompts de imagem. | 1 |
| `asset_generation` | Gera imagens, calcula score semantico e seleciona assets. | 2 |
| `tts` | Gera narracao em WAV e SRT bruto. | 2 |
| `subtitle_alignment` | Normaliza legenda para render. | 1 |
| `render` | Usa ffmpeg para criar MP4 vertical com legenda queimada. | 1 |
| `publish_to_review_hub` | Marca o job como pronto para revisao. | 0 |

Cada etapa grava linhas em `StepExecution`, eventos em `events.jsonl` e arquivos JSON/asset no diretorio do job.

## Estrutura do Projeto

```text
app/
  main.py             Rotas FastAPI, SSR e arquivos estaticos.
  orchestrator.py     Worker, estados, retries e pipeline completo.
  providers.py        Provedores de texto, imagem, TTS e verificacao semantica.
  models.py           Modelos SQLAlchemy.
  schemas.py          Schemas Pydantic de entrada/revisao.
  config.py           Settings via .env.
  storage.py          Escrita/leitura de artefatos.
  templates/          Telas HTML do hub.
  static/styles.css   CSS do hub.
scripts/
  migrate_sqlite_to_postgres.py
tests/
  test_e2e.py
```

Diretorios locais ignorados:

```text
data/        Banco SQLite, videos, imagens, audio e JSONs de jobs.
data-test/   Banco temporario dos testes.
.venv/       Ambiente Python local.
node_modules/
.env         Segredos e configuracao local.
```

## Rodando com Providers Reais

Configure `.env` com pelo menos:

```env
YTS_USE_MOCK_PROVIDERS=false
YTS_MINIMAX_TEXT_API_KEY=...
YTS_MINIMAX_IMAGE_API_KEY=...
```

Opcionalmente:

```env
YTS_PEXELS_API_KEY=...
YTS_PIXABAY_API_KEY=...
```

Depois reinicie o `uvicorn`. O worker le as settings no startup, entao alteracoes no `.env` pedem restart do app.

## Testes

```bash
source .venv/bin/activate
pytest -q
```

Validacao recente neste repo:

```text
5 passed
```

Os testes usam providers mock e cobrem fluxo E2E, retry e repeticao.

## Retomando Estado de Runtime

O clone novo nao inclui jobs existentes. Para mover o estado de uma maquina para outra:

1. copie o `.env` manualmente, sem commitar;
2. copie `data/yts_render.db` se estiver usando SQLite;
3. copie `data/artifacts/` se quiser manter videos, imagens, audio e manifests;
4. se estiver usando PostgreSQL, faca dump/restore do banco e copie os artefatos correspondentes.

Sem os artefatos, a interface pode listar metadados no banco, mas links de videos/imagens antigos podem quebrar.

## Tailscale

O app foi mantido em `127.0.0.1:8080`. Para expor via Tailscale em uma maquina autenticada:

```bash
tailscale up --hostname=shorts-hub
tailscale serve --bg 8080
```

## Onde Continuar o Desenvolvimento

Para entender a arquitetura e os pontos de manutencao, leia a [documentacao do app](docs/app.md).

Prioridade sugerida para a proxima fase:

1. Criar um provider de imagem barato, por exemplo Z-Image Turbo, e plugar em `ProviderRegistry.image`.
2. Adicionar variaveis como `YTS_IMAGE_PROVIDER`, `YTS_ZIMAGE_API_KEY` e `YTS_ZIMAGE_BASE_URL`.
3. Gerar imagens em `720x1280` para reduzir custo por Short.
4. Manter o score semantico atual e permitir apenas 1 retry visual por cena no inicio.
5. Guardar `seed`, modelo, tamanho e custo estimado em `SceneAsset.scores` ou `provider_metadata`.
6. Criar uma tela/resumo de custo por job antes de escalar volume.
7. Separar o worker em processo proprio se o volume aumentar.

Arquivos mais importantes para essa mudanca:

- `app/config.py`: novas variaveis.
- `app/providers.py`: nova classe do provider de imagem.
- `app/orchestrator.py`: nomes de provider nos fallback events e politica de retry visual.
- `tests/test_e2e.py`: teste com provider mock equivalente ao novo contrato.

## Comandos Uteis

Para retomar o projeto do zero, use o [runbook de inicializacao](docs/runbook-inicializacao.md).

```bash
# Rodar app
uvicorn app.main:app --host 127.0.0.1 --port 8080

# Rodar testes
pytest -q

# Ver status git
git status --short --branch

# Ver healthcheck
curl http://127.0.0.1:8080/healthz
```

## Cuidados Antes de Publicar no YouTube

- Revise manualmente cada video antes de postar.
- Evite templates identicos em massa; varie angulo, ritmo e visual.
- Nao dependa apenas de stock generico ou slideshow sem transformacao.
- Mantenha registro de prompts, assets e provider usado por job.
- Quando uma cena de IA puder parecer evento real, trate como conteudo sintetico e siga as regras de disclosure do YouTube.
