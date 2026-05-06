# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `app/`. `app/main.py` exposes the FastAPI app and SSR routes, `app/orchestrator.py` runs the background pipeline, and `app/providers.py` contains text, image, and TTS provider integrations. Quality gates are grouped under `app/quality/`. HTML templates are in `app/templates/`, and shared styling is in `app/static/styles.css`.

Tests currently live in `tests/test_e2e.py` and cover the end-to-end job flow. Operational notes and architecture context are in `docs/`. Runtime artifacts, SQLite databases, generated media, and temp test data belong in `data/`, `data-real-renders/`, and `data-test/`; treat these as local state, not source.

## Build, Test, and Development Commands
Create an environment and install the app:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .[dev]
```

Run the local server:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Run tests:

```bash
pytest -q
```

Check the app health endpoint with `curl http://127.0.0.1:8080/healthz`. Use `node` tooling only if you need Playwright-related work; `package.json` is minimal.

## Coding Style & Naming Conventions
Follow existing Python style: 4-space indentation, type hints where practical, and `snake_case` for functions, variables, and module names. Keep FastAPI route handlers, SQLAlchemy models, and Pydantic schemas in their current files unless a change clearly justifies a new module. Prefer small helper functions over deeply nested orchestration logic.

## Testing Guidelines
Add or update `pytest` coverage for behavioral changes, especially pipeline states, quality gates, and review hub flows. Name tests `test_<behavior>()`. Tests default to mock providers via `YTS_USE_MOCK_PROVIDERS=true`; preserve that pattern so the suite stays deterministic and cheap to run.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit style such as `feat: gate publish readiness` and `fix: harden shorts quality gates`; keep using `feat:`, `fix:`, and similarly clear prefixes. PRs should describe the user-visible or pipeline-visible change, note config or data impacts, link related issues, and include screenshots when updating hub templates or styles.

## Configuration & Data Notes
Copy `.env.example` to `.env` for local setup. Keep secrets, generated artifacts, SQLite files, and provider outputs out of commits. If you change settings or provider behavior, update `README.md` or the relevant file in `docs/`.
