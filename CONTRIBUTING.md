# Contributing to Shelf

Thanks for your interest in improving Shelf. This guide covers local setup, the
test/lint commands CI runs, and how to propose changes.

## Ground rules

- **Respect the sourcing policy.** Shelf does not ship scrapers for arbitrary
  sites. Every ingestion adapter carries a `ComplianceDeclaration` and the engine
  refuses sources whose `tos_permitted` flag is off. PRs that add adapters for
  content the operator has no right to read will not be merged.
- **Keep secrets out of commits.** Config lives in `backend/.env` (gitignored) or
  `SHELF_*` env vars; see [`backend/.env.example`](backend/.env.example).
- **Small, focused PRs** with a clear description are easier to review and ship.

## Local setup

```bash
# Backend (Python ≥ 3.11)
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,render,rar]"     # render = Playwright; rar = CBR support
playwright install chromium            # once, for JS/anti-bot sources (render_js)
python -m app                          # serves API + built UI on 0.0.0.0:8000

# Frontend (Node ≥ 18) — dev only; production serves the built dist from FastAPI
cd frontend
npm install
npm run dev                            # :5173, proxies /api → :8000
```

Or just run `./install.sh` from the repo root — it provisions Python/Node, builds
the UI, and runs the app as a systemd service. It is idempotent.

## Tests & linting (what CI enforces)

Run these before opening a PR — the CI workflow (`.github/workflows/ci.yml`) runs
the same checks:

```bash
# Backend
cd backend
pytest                                 # full suite
ruff check .                           # lint (if ruff is installed via .[dev])

# Frontend
cd frontend
npm run build                          # tsc -b && vite build (typecheck + bundle)
npm run lint                           # eslint
```

A PR should keep the backend suite green and the frontend typechecking + building
cleanly.

## Pull request process

1. Branch off `main` (`git switch -c fix/short-description`).
2. Make the change with tests where it makes sense.
3. Run the checks above.
4. Open a PR using the template; describe **what** changed and **why**, and how you
   verified it.
5. CI must pass and the PR needs review approval before merge (see below).

## Project layout

```
backend/app/            FastAPI app
  routers/              HTTP API layer
  ingestion/            crawl/fetch engine, adapters, catalog, downloads, scheduler
  models.py schemas.py  SQLAlchemy models + Pydantic schemas
backend/tests/          pytest suite
frontend/src/           React + TS SPA (pages/, components/, api/client.ts, store.ts)
deploy/  scripts/       systemd unit, tunnel guide, packaging
docs/                   design / migration notes
```

## Commit style

Conventional, imperative subject lines (`fix(crawl): …`, `feat(reader): …`,
`docs: …`). Reference issues where relevant.
