# Runbook de Inicializacao

Este runbook serve para retomar o projeto em uma nova sessao, subir o hub local e gerar/revisar Shorts sem precisar redescobrir os comandos.

## 1. Entrar no projeto

```bash
cd /root/yts-render
```

Confira se ha alteracoes locais antes de mexer em arquivos:

```bash
git status --short --branch
```

## 2. Preparar o ambiente Python

Se o ambiente virtual ja existir:

```bash
source .venv/bin/activate
```

Se estiver em uma maquina nova ou sem `.venv`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## 3. Conferir `.env`

O app le configuracao de `.env` com prefixo `YTS_`.

Para rodar local sem gastar API:

```env
YTS_USE_MOCK_PROVIDERS=true
YTS_DATABASE_URL=sqlite:///data/yts_render.db
YTS_DATA_DIR=data
```

Para rodar com providers reais:

```env
YTS_USE_MOCK_PROVIDERS=false
YTS_MINIMAX_TEXT_API_KEY=...
YTS_MINIMAX_IMAGE_API_KEY=...
```

Reinicie o `uvicorn` sempre que alterar `.env`, porque o worker carrega as settings no startup.

## 4. Subir o hub

Padrao local:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Se a porta `8080` estiver ocupada, use outra porta:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8081
```

Se precisar expor na interface Tailscale da maquina, use o IP Tailscale como host:

```bash
uvicorn app.main:app --host 100.125.130.88 --port 8081
```

Abra o hub:

```text
http://127.0.0.1:8080
```

Ou, se estiver usando Tailscale:

```text
http://100.125.130.88:8081
```

## 5. Validar que iniciou corretamente

Healthcheck:

```bash
curl http://127.0.0.1:8080/healthz
```

Resposta esperada:

```json
{"status":"ok","app":"YTS Render","bind":"127.0.0.1:8080","tailnet_url":"https://shorts-hub.example.ts.net"}
```

Se estiver usando outra porta ou host, ajuste a URL do `curl`.

Para ver processos ativos:

```bash
ps -ef | rg 'uvicorn|app.main'
```

## 6. Criar um video pelo hub

Pelo navegador, preencha o formulario da pagina inicial e envie. O app cria um job em `queued`, o worker processa as etapas e o job deve chegar em `waiting_review`.

Tambem da para criar via `curl`:

```bash
curl -i -X POST http://127.0.0.1:8080/jobs \
  -F seed_theme="polvos" \
  -F target_duration_sec=35 \
  -F tone="intrigante_direto" \
  -F cta_style="none"
```

O header `location` aponta para `/jobs/<job_id>`.

## 7. Onde ficam os artefatos

Cada job grava os arquivos em:

```text
data/artifacts/<job_id>/
```

Arquivos mais importantes:

```text
render/final.mp4
render/poster.jpg
render/ffmpeg.log
publish_package.json
events.jsonl
```

Na interface do job, use a tela de revisao para assistir ao MP4, ver cenas, assets, audio, legendas e logs.

## 8. Rodar testes

```bash
source .venv/bin/activate
pytest -q
```

Os testes usam providers mock e gravam em `data-test/`.

## 9. Problemas comuns

Porta ocupada:

```bash
ps -ef | rg 'uvicorn|app.main'
```

Use outra porta ou encerre o processo antigo se ele for seu.

Healthcheck retorna `Not Found`:

Provavelmente outro app esta ouvindo naquela porta. Confira os processos e teste a porta/host corretos.

Job falhou:

Abra a tela do job e verifique `failure_reason`, `events.jsonl` e `render/ffmpeg.log`. Se o erro aconteceu apos mudar `.env`, reinicie o servidor.

Videos antigos sem arquivo:

O banco pode listar jobs antigos, mas os links quebram se `data/artifacts/` nao foi copiado junto com `data/yts_render.db`.

## 10. Encerrar

No terminal do `uvicorn`, use `Ctrl+C`.

Se o processo ficou em background, localize e encerre com cuidado:

```bash
ps -ef | rg 'uvicorn|app.main'
kill <pid>
```

