# CLAUDE.md
# ==========
# This file is for Claude (claude.ai).
# Paste the contents of this file at the start of any Claude conversation
# to instantly bring Claude up to speed on the project state.
#
# Update the CURRENT STATE section after each work session.

## PROJECT
`mytbot` — personal autonomous multi-asset trading system.
GitHub: https://github.com/kvcom/mytbot.git
Owner: UK-based, trading stocks, bonds, ETFs, forex, crypto.
Primary broker: IBKR Pro. Crypto: Kraken + Binance.

## ARCHITECTURE IN ONE PARAGRAPH
**One-button system.** `python run.py` starts everything — orchestrator brings up
Docker (Postgres, Redis), auto-discovers available brokers (skipping unavailable),
runs the trading loop and data pipeline, and exposes an API+WebSocket for the UI.
The UI has a single ON/OFF button (`POST /system/start`, `POST /system/stop`).
All brokers implement a single abstract interface in `brokers/base.py`.
The signal engine aggregates strategy outputs into a Signal.
Every Signal passes through the risk engine (unconditional veto power).
Approved signals go to the execution engine which routes to the best broker.
Everything is logged. Runtime broker permissions support graceful fallback routing.
Risk parameters are managed by `ParameterManager` with layered overrides
(regime > AI > defaults) and bounded, auditable changes.
AI (Claude API) classifies news and generates rationale — never places orders.

## KEY FILES
- `run.py`                   — **THE entry point** (`python run.py` starts everything)
- `system/orchestrator.py`   — state machine: OFF → STARTING → RUNNING → STOPPING → OFF
- `system/dependency_manager.py` — auto-start Postgres/Redis via Docker
- `system/broker_manager.py` — auto-discover and connect available brokers
- `system/trading_loop.py`   — controllable async trading loop (start/stop)
- `brokers/base.py`          — the adapter interface (FROZEN, never change)
- `brokers/registry.py`      — add new brokers here (one line)
- `brokers/bybit/adapter.py` — Bybit V5 (spot / USDT linear)
- `brokers/_template/`       — copy this to add any new exchange
- `risk/engine.py`           — risk checks, kill switch
- `risk/parameters.py`       — parameter manager (defaults + overrides + expiry)
- `signals/engine.py`        — signal aggregation
- `strategies/momentum.py`   — first strategy (momentum breakout)
- `execution/engine.py`      — order placement
- `execution/router.py`      — smart order routing
- `storage/models.py`        — database schema
- `api/server.py`            — FastAPI: includes `/system/start`, `/system/stop`, `/system/status`
- `config/risk_limits.yaml`  — all risk thresholds (editable without code change)
- `config/m8_micro_live.yaml` — optional micro-live profile (symbol/strategy/notional caps when `APP_ENV=live`)
- `config/fundamentals.yaml` — parameter defaults, absolute bounds, AI policy
- `config/broker_permissions.yaml` — runtime broker permission/fallback map
- `config/data_pipeline.yaml` — M2 symbols, intervals, News/FRED toggles
- `config/ai.yaml`          — M6 AI toggles and pipeline settings
- `run_pipeline.py`          — M2: yfinance → features → Postgres; NewsAPI + FRED
- `ai/news_classifier.py`    — Claude-first news scoring + rationale generation
- `ai/pipeline.py`           — M6 orchestration: symbol news score + macro regime
- `docs/DECISIONS.md`        — architectural decision log
- `docs/M2_READINESS.md`     — M1 verification summary + M2 prep checklist
- `alembic/`                 — DB migrations (URL from POSTGRES_* in env.py)
- `tests/`                   — pytest smoke tests
- `.cursorrules`             — Cursor AI alignment rules

## CURRENT STATE
<!-- Update this section after each work session -->
- Milestone: M9 — One-Button System ✅
- Last completed task: Full orchestrator redesign — `run.py` single entry point, `system/` package (orchestrator, dependency_manager, broker_manager, trading_loop), API endpoints (`/system/start`, `/system/stop`, `/system/status`), UI MasterControl wired to system start/stop with real-time state
- Next task: Operational soak — `python run.py` end-to-end testing, broker credential setup, live validation
- Blockers: IBKR stream/order need local IB Gateway/TWS; exchange live keys
- Notes: .env not committed — use .env.example; `python run.py` is the ONLY command needed to run the entire system

## RULES CLAUDE MUST FOLLOW IN THIS PROJECT
1. Never change `brokers/base.py` interface — it is frozen
2. Never add a bypass to the risk engine
3. Decimal for all prices and quantities, never float
4. paper_mode=True is always the default
5. Every new broker = one new file + one line in registry.py, nothing else
6. Log every signal, risk decision, order, and fill
7. AI (Claude API) only scores and explains — never executes
8. Check `docs/DECISIONS.md` before making architectural choices

## HOW TO USE THIS FILE
At the start of a Claude session, say:
"Here is my project context: [paste this file]
Current task: [describe what you want to work on]"

Claude will immediately understand the full architecture and constraints
without needing to re-explain everything.
