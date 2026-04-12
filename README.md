# mytbot — Autonomous Multi-Asset Trading System

Personal autonomous trading system: equities, bonds, ETFs, forex, and crypto.  
**Primary control:** `python run.py` — orchestrator starts dependencies, brokers, trading loop, pipeline, FastAPI + WebSocket, and serves the React dashboard (or use `POST /system/start` / `stop` from the UI).

Architecture is **adapter-based**: add a venue without changing strategy, risk, or execution logic.

## Brokers

| Broker | Status | Assets |
|--------|--------|--------|
| IBKR | ✅ Production path | Stocks, bonds, ETFs, forex, options (single-leg, opt-in), futures, IBKR crypto (11) |
| Kraken | ✅ | Crypto spot (640+ pairs) |
| Binance | ✅ | Crypto spot / futures (wide universe) |
| Alpaca | ✅ | US equities, ETFs (strong paper environment) |
| Bybit | ✅ | Spot + USDT **linear** perps (`BYBIT_CATEGORY=linear` or `spot`) |
| Deribit | ⏳ Later | Crypto options (hedging / vol strategies) |

## Adding a new broker

```bash
cp -r brokers/_template brokers/newexchange
# Edit brokers/newexchange/adapter.py — implement BrokerAdapter
# Add one line to brokers/registry.py
# Wire routing / permissions / .env.example as needed (see docs/BROKERS.md)
```

## Setup

```bash
git clone https://github.com/kvcom/mytbot.git
cd mytbot
python -m venv .venv
# Windows: .\.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: POSTGRES_* (match Docker), IBKR_*, broker API keys as needed

docker compose up -d
```

**Windows:** Prefer `.\.venv\Scripts\python.exe` so dependencies resolve (`ib_insync`, SDKs, etc.).

**Postgres:** `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` in `.env` must match `docker-compose.yml` (defaults `mytbot` / `changeme` / `mytbot`). App connects to `localhost` on `POSTGRES_PORT` (default `5432`).

### Database migrations (Alembic)

Alembic uses a **sync** URL built from `POSTGRES_*` (see `alembic/env.py`). From repo root:

```bash
alembic upgrade head
pytest
```

**Existing database** created with SQLAlchemy `create_all` before Alembic: if tables already exist, early revisions may no-op DDL. To align Alembic’s version table only (no schema change): `alembic stamp head`.  
**Downgrades:** only use on disposable DBs — some revisions call `drop_all()`.

### Data pipeline (M2)

After migrations: `.\.venv\Scripts\python.exe run_pipeline.py --backfill` for historical bars + features (`config/data_pipeline.yaml`). Incremental: `run_pipeline.py` or `run_pipeline.py --loop`. Optional: `NEWS_API_KEY`, `FRED_API_KEY`.

### Run the full system

```bash
python run.py
```

Legacy / dev-only entrypoints (e.g. `main.py`, `run_m3.py`, `run_m5.py`) still exist for targeted tests; **normal operation is `run.py`**.

### UI (production build)

Dashboard lives in `ui/` (Vite + React). If you serve static `ui/dist` from the API:

```bash
cd ui && npm install && npm run build
```

### Tests & release gate

```bash
pytest
python scripts/release_gate.py
```

**Dev bootstrap:** `python scripts/setup_dev.py` (installs `requirements-dev.txt`).

## Build plan (milestones)

| Milestone | Focus | Status |
|-----------|-------|--------|
| M1 | Broker connectivity, adapter pattern | ✅ |
| M2 | Data pipeline, feature store | ✅ |
| M3 | First strategies, signal engine | ✅ |
| M4 | Risk engine | ✅ |
| M5 | Execution engine, full paper loop | ✅ |
| M6 | AI intelligence layer (scoring, rationale) | ✅ |
| M7 | Dashboard & control (REST + WebSocket) | ✅ |
| M8 | Micro-live trading, Bybit, vol sizing | ✅ |
| M9 | D015 allocator primary path, opportunity engine | ✅ |
| M10 | Local-first AI (`ai/router.py`), signal accumulation | ✅ |

Detail: `docs/BUILD_PLAN.md`. Decisions: `docs/DECISIONS.md`. Architecture: `docs/ARCHITECTURE.md`.

## Architecture principles

- **Risk engine is law** — no order bypasses it.
- **AI advises, rules execute** — local-first scoring (`config/ai.yaml`); optional paid fallback; **never** places orders.
- **Adapters are isolated** — no venue-specific imports outside `brokers/` (use `brokers/registry.py`).
- **Paper mode first** — `paper_mode=True` default; live requires explicit config (e.g. `APP_ENV=live`, M8 profile when used).
- **Decimal for money** in core trading logic; full audit logging of signals, risk, orders, and fills.
