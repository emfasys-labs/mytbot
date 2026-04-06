# mytbot — Autonomous Multi-Asset Trading System

Personal autonomous trading system. Stocks, bonds, ETFs, forex, crypto.
Built on a pluggable broker architecture — add any exchange without touching existing code.

## Brokers

| Broker | Status | Assets |
|--------|--------|--------|
| IBKR | 🔧 M1 | Stocks, bonds, ETFs, forex, options, futures, crypto (11) |
| Kraken | 🔧 M1 | Crypto spot (640+ pairs) |
| Binance | 🔧 M1 | Crypto (1000+ pairs) |
| Alpaca | 🔧 M1 | US equities, ETFs (paper trading) |
| Bybit | ⏳ Later | Crypto futures/derivatives |
| Deribit | ⏳ Later | Crypto options |

## Adding a New Broker

```bash
cp -r brokers/_template brokers/newexchange
# Edit brokers/newexchange/adapter.py — implement 6 methods
# Add to brokers/registry.py: "newexchange": NewExchangeAdapter
# Done. Zero other changes needed.
```

## Setup

```bash
# 1. Clone and install
git clone https://github.com/kvcom/mytbot.git
cd mytbot
python -m venv .venv
# Windows: .\.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env: IBKR_*, POSTGRES_* (must match Docker), API keys when needed

# 3. Start Postgres + Redis (Docker Desktop on Windows)
docker compose up -d
# Uses TimescaleDB (Postgres-compatible); data persists in a Docker volume.
# Optional later: docker compose --profile app up -d --build  (bot + API in containers)

# 4. Run M1 (IBKR + Kraken streams, optional Postgres writes)
# Use the venv interpreter so dependencies (ib_insync, kraken SDK, etc.) resolve:
#   Windows: .\.venv\Scripts\python.exe main.py
#   POSIX:   .venv/bin/python main.py
# Plain `python main.py` uses whatever is first on PATH and often lacks packages.
# Requires IB Gateway/TWS for IBKR ticks; KRAKEN_* for Kraken. See .env.example (M1_*).
```

**Windows:** after `python -m venv .venv` and `pip install -r requirements.txt`, use `.\.venv\Scripts\python.exe` (or activate the venv) so imports match the project. For the IBKR integration script you can run `.\test-ibkr.ps1`, which always uses `.venv`.

**Postgres via Docker:** `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env` must match what Compose passes into the container (defaults in `docker-compose.yml` are `mytbot` / `changeme` / `mytbot`). The app connects to `localhost` on `POSTGRES_PORT` (default `5432`).

**Migrations:** Alembic uses the same `POSTGRES_*` vars (sync `psycopg2` URL). From repo root: `alembic upgrade head`. See `docs/M2_READINESS.md` for stamp vs upgrade on an existing DB.

**M2 data pipeline:** `alembic upgrade head`, then `.\.venv\Scripts\python.exe run_pipeline.py --backfill` (2y daily bars + features for symbols in `config/data_pipeline.yaml`). Incremental: `run_pipeline.py` or `run_pipeline.py --loop`. Set `NEWS_API_KEY` / `FRED_API_KEY` in `.env` for news and macro rows.

**Tests:** `pytest` (smoke tests under `tests/`). Requires `pip install -r requirements.txt`.

## Build Plan

| Milestone | Focus | Status |
|-----------|-------|--------|
| M1 | Broker connectivity, adapter pattern | ✅ |
| M2 | Data pipeline, feature store | ✅ |
| M3 | First strategy, signal engine | ✅ |
| M4 | Risk engine | ✅ |
| M5 | Execution engine, full paper loop | ✅ |
| M6 | AI intelligence layer | ⏳ |
| M7 | Dashboard & control | ⏳ |
| M8 | Micro-live trading | ⏳ |

## Architecture Principles

- **Risk engine is law** — no order bypasses it, ever
- **AI advises, rules execute** — Claude scores and explains, never places orders directly  
- **Adapters are isolated** — no broker-specific code outside `brokers/`
- **Paper mode first** — every strategy runs in paper mode before touching real capital
- **Everything is logged** — full audit trail, every decision
