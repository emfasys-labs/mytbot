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

**New PC or full reinstall:** use the end-to-end checklist in **[`docs/NEW_MACHINE_SETUP.md`](docs/NEW_MACHINE_SETUP.md)** (Python, Docker, Ollama + local LLM models, FinBERT cache, UI build, `.env`, migrations). Optional: `scripts/setup_new_machine.ps1` (Windows) or `scripts/setup_new_machine.sh` (macOS/Linux) for venv + `pip install` only.

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

After migrations: `.\.venv\Scripts\python.exe run_pipeline.py --backfill` for historical bars + features (`config/data_pipeline.yaml`). Incremental: `run_pipeline.py` or `run_pipeline.py --loop`. Optional: `NEWS_API_KEY`, `ALPHAVANTAGE_API_KEY`, `FINNHUB_API_KEY`, `MARKETAUX_API_TOKEN`, `FRED_API_KEY`.

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

### UI (Vite dev — http://localhost:5173)

For the normal hot-reload dashboard, keep the API on **:8000** (`python run.py` or uvicorn) and run:

```bash
cd ui && npm install && npm run dev
```

Open **http://localhost:5173/** — `ui/.env.development` sets `VITE_API_BASE=http://127.0.0.1:8000` so the browser talks to FastAPI, not the Vite server. Optional: copy `ui/.env.example` to `ui/.env.local` to override. Set `UI_AUTO_BUILD=0` when running `run.py` if you want to skip `npm run build` while iterating on the UI only.

### Windows desktop launcher (server + UI)

Use the included scripts to launch the API/trading server and Vite UI in two separate PowerShell windows:

```powershell
.\scripts\create_desktop_launcher.ps1
```

This creates a desktop shortcut named **`mytbot`**. Double-click it to run:
- `python run.py` from repo root (uses `.venv\Scripts\python.exe` if present)
- `npm run dev` in `ui/`
- After a short delay, your default browser opens **http://localhost:5173/** (override with `-UiUrl`; `-BrowserDelaySec` defaults to 5).

If UI dependencies are missing, run once manually:

```powershell
.\scripts\start_mytbot_full_stack.ps1 -InstallUiDeps
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

## Active strategy roster

- Directional: `momentum_breakout`, `mean_reversion`
- Flow/volatility: `volume_flow`
- Volatility: `volatility_regime`
- Event-driven: `event_driven_news` (AI/news shock gated)
- Relative value: `pairs_trading` (config-driven pair list)
- Macro rotation: `regime_rotation` (demand-score driven proxies)
- Demand modeling: cross-asset demand graph + demand engine composite score
- Portfolio throttle: volatility overlay on gross exposure target
- Execution: demand-conditioned urgency in execution planning
- Routing: demand-aware venue preference (crypto/equity path bias)
- Routing: learned broker-symbol execution quality feedback (fill/slippage aware)
- Routing: persistent quality state + decay policy (control-state backed)
- Routing: broker-level confidence intervals (CI95) and adaptive decay by activity/liquidity proxies
- Meta-layer: adaptive strategy priors from recent execution outcomes
- Dashboard: demand regime-shift alerts/history + meta-calibration diagnostics
- Diagnostics: per-symbol routing quality trajectory endpoint (`/diagnostics/routing-quality`) + UI mini-sparklines
- Diagnostics: routing quality API returns persisted `quality_stats`; Wave 9 adds fused prior+evidence scores, slippage p50/p90, fill rate, and a broker comparison table in Risk diagnostics
- Structural arbitrage: `funding_rate_arbitrage`, `cross_exchange_arbitrage`
