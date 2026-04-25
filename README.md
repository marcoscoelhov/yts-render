# YTS Render

MVP de geracao de YouTube Shorts em pipeline completo:

- cria `job` a partir de `seed_theme`
- gera `topic_plan`, `script`, `scene_plan`
- tenta MiniMax global (`api.minimax.io/v1` para texto e imagem)
- usa Pexels e Pixabay como fallback visual
- tenta Edge TTS e cai para gerador local quando o host bloqueia a voz
- renderiza MP4 vertical com legendas queimadas
- publica tudo num hub SSR para revisao humana

## Rodando localmente

Dependencias Python ja usadas no host desta validacao:

```bash
PATH="$HOME/.local/bin:$PATH" uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Abra `http://127.0.0.1:8080`.

## Testes

```bash
PATH="$HOME/.local/bin:$PATH" pytest -q
```

## Configuracao

Copie `.env.example` para `.env` se quiser customizar:

- `YTS_DATA_DIR`
- `YTS_DATABASE_URL`
- `YTS_APP_HOST`
- `YTS_APP_PORT`
- `YTS_USE_MOCK_PROVIDERS`
- `YTS_MINIMAX_API_KEY`
- `YTS_PEXELS_API_KEY`
- `YTS_PIXABAY_API_KEY`
- `YTS_TAILSCALE_HOSTNAME`
- `YTS_TAILNET_DOMAIN`

## Tailscale

O app foi mantido bindado em `127.0.0.1:8080`, seguindo o harness do documento.

Quando o host tiver Tailscale instalado e autenticado:

```bash
tailscale up --hostname=shorts-hub
tailscale serve --bg 8080
```

## Validacao realizada neste host

- pipeline completo ate `waiting_review`
- render MP4 acessivel pelo hub
- aprovacao manual pelo endpoint de review
- suite automatizada cobrindo E2E, repeticao e retry
- runtime normal com `.env` gerando jobs aprovados e persistindo artefatos em `data/artifacts`
