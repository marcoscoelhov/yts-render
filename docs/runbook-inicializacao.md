# Runbook de Inicializacao

Este runbook serve para retomar o projeto, subir o hub e validar o fluxo atual de geracao, aprovacao e publicacao.

## 1. Entrar no projeto

```bash
cd /root/yts-render
git status --short --branch
```

## 2. Preparar o ambiente Python

Se a venv ja existir:

```bash
source .venv/bin/activate
```

Se estiver em maquina nova:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## 3. Preparar `.env`

Copie o exemplo:

```bash
cp .env.example .env
```

### Mock local

```env
YTS_USE_MOCK_PROVIDERS=true
YTS_DATABASE_URL=sqlite:///data/yts_render.db
YTS_DATA_DIR=data
```

### Providers reais

```env
YTS_USE_MOCK_PROVIDERS=false
YTS_MINIMAX_TEXT_API_KEY=...
YTS_MINIMAX_IMAGE_API_KEY=...
```

### Upload real no YouTube

```env
YTS_YOUTUBE_PUBLISH_MODE=api
YTS_YOUTUBE_API_ENABLED=true
YTS_YOUTUBE_CLIENT_ID=...
YTS_YOUTUBE_CLIENT_SECRET=...
YTS_YOUTUBE_CHANNEL_ID=...
```

Sempre reinicie o `uvicorn` depois de mudar `.env`.

## 4. Subir o hub

Padrao local:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Se a porta estiver ocupada:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8081
```

## 5. Validar que iniciou corretamente

```bash
curl http://127.0.0.1:8080/healthz
```

Resposta esperada:

```json
{"status":"ok","app":"YTS Render","bind":"127.0.0.1:8080","tailnet_url":"https://shorts-hub.example.ts.net"}
```

Se estiver usando outra porta, ajuste a URL do `curl`.

## 6. Abrir o hub

- Home: `http://127.0.0.1:8080/`
- Centro de publicacao: `http://127.0.0.1:8080/publication-hub`
- Calendario: `http://127.0.0.1:8080/calendar`

## 7. Criar um job

Pelo navegador, use o formulario da home.

Via `curl`:

```bash
curl -i -X POST http://127.0.0.1:8080/jobs \
  -F seed_theme="polvos" \
  -F target_duration_sec=35 \
  -F tone="intrigante_direto" \
  -F cta_style="none"
```

O `location` aponta para `/jobs/<job_id>`.

## 8. Acompanhar o estado correto

O job nao vai mais para `waiting_review`.

Estados esperados depois do pipeline:

- `monetization_review`
- `blocked_for_monetization`
- `ready_for_upload`

Se o job ficar bom para revisar, abra `/jobs/<job_id>` e siga o fluxo:

1. assistir ao video
2. aprovar ou rejeitar
3. se aprovado, agendar ou publicar

## 9. Conectar o YouTube, quando necessario

Se o objetivo for upload real via API:

1. abra `http://127.0.0.1:8080/youtube/connect`
2. conclua o OAuth na conta do canal
3. confirme que surgiu `data/youtube_oauth_token.json`
4. volte ao hub e confira o bloco de integracao

Se `YTS_YOUTUBE_OAUTH_REDIRECT_URI` estiver vazio, o app usa a URL atual do hub como callback.

## 10. Agendar ou publicar

### Modo manual

- o hub serve para aprovacao, agenda local e registro da publicacao
- `Publicar agora` exige `youtube_video_id` ou `youtube_url`
- a agenda automatica nao e executada pelo worker em `manual`

### Modo API

- jobs aprovados podem entrar em agenda
- quando o horario chega, o worker muda a agenda para `publishing` e sobe o video
- falha de upload vira `publish_failed`

## 11. Onde ficam os artefatos

Cada job grava em:

```text
data/artifacts/<job_id>/
```

Arquivos comuns:

```text
render/final.mp4
render/poster.jpg
render/ffmpeg.log
publish_package.json
publication_schedule.json
youtube_publish_attempts.json
events.jsonl
```

## 12. Retencao automatica

O worker tambem limpa artefatos temporarios:

- falha critica: 24h
- job corrigivel: 7 dias
- pronto para publicar ou com agenda ativa: 21 dias

Importante:

- isso remove arquivos pesados
- nao apaga o job do banco
- o hub continua abrindo o job, mas pode mostrar banner de artefatos expirados

Se um job antigo abrir sem video local, isso pode ser retencao normal, nao corrupcao.

## 13. Testes

Suite completa:

```bash
pytest -q
```

Se mexer em hub, agenda, publicacao ou retencao, prefira ao menos uma fatia focada de `tests/test_e2e.py`.

## 14. Expor via Tailscale

Mantendo o app local:

```bash
tailscale serve --bg http://127.0.0.1:8080
```

Valide:

```bash
curl https://<hostname>.<tailnet>/healthz
```

## 15. Encerrar

No terminal do `uvicorn`, use `Ctrl+C`.

Se o processo ficou em background:

```bash
ps -ef | rg 'uvicorn|app.main'
kill <pid>
```
