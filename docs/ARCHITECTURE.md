# ARCHITECTURE.md
# ================
# Full system architecture for mytbot.
# Read this before making any structural changes.

## Overview

mytbot is a personal autonomous multi-asset trading system.
**Primary runtime:** `python run.py` — orchestrator (`system/orchestrator.py`) brings up Docker-backed Postgres/Redis (via dependency manager), discovers brokers (`system/broker_manager.py`), runs the trading loop and optional pipeline, and serves **FastAPI** + **WebSocket** for the **React** dashboard in `ui/`.

Signals are produced by strategies (and optional D015 opportunity path), may pass through **stateful signal accumulation** (`signals/accumulator.py`), then the signal engine; every tradable intent is **veto-capable** by the risk engine before execution routes to adapters.

**Assets traded:** US equities, UK equities, ETFs, bonds, forex, crypto; **IBKR single-leg options** (opt-in via `options_trading` / `ENABLE_OPTIONS`, same Signal → Risk → Execution path)
**Primary broker:** Interactive Brokers Pro (IBKR)
**Crypto brokers:** Kraken, Binance
**Capital:** Personal funds only. Not a public product.

---

## Production philosophy

The system is built as a **final-form production architecture**: components are modular, testable, and intended for long-term operation. Work proceeds in **phased activation** (what is enabled in live trading, capital exposure, soak gates), not throwaway prototypes. There is no planned rewrite of core layers; new capability deepens implementations inside this structure.

---

## Research-Driven Architecture Notes (2026-04)

The research package does **not** change the top-level system architecture.
It strengthens implementation details within existing layers:

- Data/Feature layer: add fractional differencing, Hurst, GARCH, VPIN, funding-rate features.
- Strategy/Validation layer: add purged CV, triple-barrier labels, DSR/PBO gates.
- Risk layer: add half-Kelly/CVaR methods alongside existing hard limits.
- Execution layer: add square-root impact and Almgren-Chriss style scheduling.
- AI layer: move gradually to hybrid local/API routing as token volume grows.

Reference docs:
- `docs/trading_research.md`
- `docs/TECH_STACK.md`
- `docs/requirements_research.txt`

---

## System Layers (top to bottom)

```
┌─────────────────────────────────────────────────────┐
│                  DATA INGESTION                      │
│  Market prices · News feeds · Macro data (FRED)     │
│  IBKR · Kraken · Binance · NewsAPI · Polygon.io     │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│                 FEATURE STORE                        │
│  Technical indicators · Sentiment scores             │
│  Regime labels · Cross-asset correlations            │
│  TimescaleDB (time-series) + Redis (live cache)      │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              STRATEGY ENGINE                         │
│  Momentum breakout · Mean reversion                  │
│  Event-driven · (more added over time)               │
│  Each strategy → RawSignal (symbol, side, confidence)│
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│         SIGNAL ACCUMULATION ENGINE                   │
│  Persistent per-symbol state (`signals/accumulator`) │
│  Quant + rolled-up AI news + macro (half-life decay) │
│  Alignment bonus · conflict penalty · net score      │
│  Optional: `signal_engine.use_signal_accumulator`  │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              SIGNAL ENGINE                           │
│  RawSignal → accumulator update → unified Signal     │
│  AI news modifier (legacy point score + dual veto) │
│  Outputs unified Signal object                       │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│           RISK ENGINE  ◄── THE BOSS                 │
│  Pre-trade checks · Portfolio checks                 │
│  Circuit breakers · Kill switch                      │
│  APPROVES or REJECTS every signal                    │
│  No order bypasses this layer. Ever.                 │
└────────────────────┬────────────────────────────────┘
                     │ (approved only)
┌────────────────────▼────────────────────────────────┐
│             EXECUTION ENGINE                         │
│  Smart order routing (best broker for this asset)    │
│  Order placement with idempotency keys               │
│  Fill tracking · Reconciliation                      │
└──────┬──────────────┬──────────────┬────────────────┘
       │              │              │
   ┌───▼───┐      ┌───▼───┐     ┌───▼───┐
   │ IBKR  │      │Kraken │     │Binance│  ← Broker Adapters
   └───────┘      └───────┘     └───────┘  (plug in more anytime)
       │              │              │
┌──────▼──────────────▼──────────────▼────────────────┐
│             PORTFOLIO TRACKER                        │
│  Real-time positions · P&L · Drawdown from HWM       │
│  Strategy attribution · Performance metrics          │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│           OBSERVABILITY & CONTROL                    │
│  Audit log (every decision stored)                   │
│  Dashboard (FastAPI + React in `ui/`)              │
│  WebSocket ticks + `/status` + `/system/status` + `/dashboard/snapshot` │
│  (`balance_ready`, `runtime.heartbeat` incl. `ai`, allocator snapshot, P&L) │
│  Alerts (e.g. Telegram on failures)                 │
│  Kill switch (API + UI)                            │
└─────────────────────────────────────────────────────┘
```

---

## The Adapter Pattern — Most Important Design Decision

Every broker implements one identical interface defined in `brokers/base.py`.
The rest of the system never knows which broker it's talking to.

```
brokers/base.py          ← interface (FROZEN — backward-compatible extensions only; see CLAUDE.md)
brokers/registry.py      ← one-line registration of new brokers
brokers/ibkr/adapter.py  ← IBKR implementation
brokers/kraken/adapter.py
brokers/binance/adapter.py
brokers/alpaca/adapter.py
brokers/_template/adapter.py  ← copy this for any new exchange
```

**To add any new broker (Bybit, Deribit, OKX, anything):**
1. `cp -r brokers/_template brokers/newexchange`
2. Implement 6 methods in `adapter.py`
3. Add one line to `brokers/registry.py`
4. Done. Zero other files change.

---

## Data Flow in Detail

```
1. Market data arrives via adapters / pipeline (prices, candles; optional WebSocket streams)
2. Feature engine computes indicators (RSI, MACD, ATR, momentum); features stored in Postgres (`feature_snapshots`, etc.)
3. News arrives via NewsAPI / pipeline → **local-first AI** (`ai/router.py`, `config/ai.yaml`) classifies and scores; optional paid fallback
4. Strategy engine runs on loop / bar updates:
      features → strategy.generate_signal() → RawSignal or None
5. Optional: D015 opportunity / allocator batch produces or constrains portfolio-level intent (primary path when enabled)
6. Signal accumulation (optional, default on): updates per-symbol decayed conviction from quant + AI/macro rollups
7. Signal engine:
      RawSignal + accumulator state + news_score → Signal (with adjusted confidence)
      Strong negative news / dual veto policy may veto before risk
8. Risk engine evaluates Signal:
      Runs all checks → APPROVED or REJECTED
      Logs decision either way
9. Execution engine (approved only):
      Builds Order from Signal
      Router picks best broker
      Places order with idempotency key
      Tracks fill
10. Portfolio tracker updates positions and P&L
11. Everything written to audit log / DB
12. Dashboard reflects state via REST + WebSocket: **`GET /status`** (includes **`runtime`** from **`runtime.heartbeat`**: runner symbols, **`ai`** health when the loop publishes it), **`GET /system/status`** (orchestrator **`state`**, **`trading.orchestrator_starting`** during boot, **`trading.ai`** merged from heartbeat when present, **`trading.snapshot_published_at`** from **`dashboard.snapshot`**, broker **`balance_ready`**), **`GET /dashboard/snapshot`** (latest D015/global-edge decision snapshot from `ControlState` key `dashboard.snapshot`, written by `system/dashboard_publish.py` from the trading loop), **`GET /pnl`** (today + calendar week/month rollups), and WebSocket `tick` payloads that include a compact `dashboard` hint (`updated_at`, `fingerprint`, `path`) for cheap UI invalidation
```

---

## AI layer — what it does and does not do

**Does (local-first, `config/ai.yaml`):**
- Routes through **rules → FinBERT → local LLM → optional escalation / paid fallback** (`ai/router.py`, `ai/escalation.py`)
- Classifies headlines, scores sentiment / materiality per symbol, supports macro regime inputs
- Feeds **bounded** scores into the signal engine and accumulator; can contribute to vetoes **before** risk (policy in `signals/engine.py`)
- Produces rationale text stored in audit tables where configured

**Does not:**
- Place orders or call broker APIs
- Override an approved risk veto or bypass `risk/engine.py`
- Replace the allocator’s role: it **informs** signals and parameters; execution still follows risk-approved orders only

---

## Key Principles

| Principle | Rule |
|-----------|------|
| Risk engine is law | No order bypasses it. No exceptions. |
| AI advises, rules execute | Local-first (or fallback) models **score**; only execution places orders after risk. |
| Paper mode first | 2+ weeks paper before any real capital |
| Adapters are isolated | No broker-specific code outside `brokers/` |
| Everything logged | Every signal, veto, order, fill — stored |
| Decimal not float | All monetary values use Python Decimal |
| One source of truth | Broker state is reconciled vs internal state |

---

## Folder Responsibilities

```
brokers/      Adapter layer only. No business logic.
data/         Market data ingestion, news, macro, features.
strategies/   Signal generation only. No broker calls.
signals/      Signal aggregation, accumulator, opportunity/D015 hooks, AI modifier.
ai/           Provider routing, scoring, rationale — no order placement.
risk/         Risk checks and kill switch only.
execution/    Order management and routing only.
portfolio/    Position tracking and P&L only.
storage/      Database models and queries only.
api/          FastAPI + WebSocket; thin controllers — domain logic in system/core.
system/       Orchestrator, trading loop, broker manager, dependency startup.
ui/           React (Vite) dashboard; production build → `ui/dist`.
monitoring/   Alerts and uptime checks only.
config/       Configuration files only.
docs/         Documentation only.
```

**Import direction (one way only):**
```
data → strategies → signals → risk → execution → portfolio
```
Never import "upward". Risk engine does not import from strategies.

---

## Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Language | Python 3.12+ | Core system |
| Database | PostgreSQL + TimescaleDB | Orders, signals, OHLCV |
| Cache | Redis | Live prices, active signals |
| ORM | SQLAlchemy | Database abstraction |
| API | FastAPI | Dashboard backend |
| Frontend | React + Recharts | Dashboard UI |
| Containers | Docker + Compose | Reproducible environment |
| Server | VPS (Hetzner/DO) | Always-on deployment |
| Logging | Loguru | Structured logs |
| Async | asyncio + aiohttp | Concurrent broker streams |

---

## Orchestrator, dependencies, and operational contracts

**Orchestrator** (`system/orchestrator.py`): single state machine (**off → starting → running → stopping → off**). On **start**, **`DependencyManager`** ensures Postgres (required) and Redis (best-effort): tries TCP first, then **`docker compose up`** for **`db`** / **`redis`** with **`_start_docker_service_with_retries`** (**`DOCKER_COMPOSE_UP_ATTEMPTS`**, backoff + jitter). **`BrokerManager`** discovers/connects adapters; **`TradingLoop`** receives **`broker_manager`** so execution matches router/reconcile adapters (including brokers that attach after startup — **D022**). Background tasks run alongside the trading loop: (1) **data pipeline** (`_pipeline_runner`) wraps **`data.pipeline.run_once`** on an interval (**D023**); (2) **coverage sync** (`_coverage_sync_loop`, tick `COVERAGE_SYNC_INTERVAL_SEC`) diffs `BrokerReport.coverage()` onto `RiskEngine._disabled_brokers` so excluded brokers never receive new orders (**D028**); (3) **NAV heartbeat** (`_nav_heartbeat_loop`, tick `NAV_HEARTBEAT_INTERVAL_SEC`, default 60s) upserts today's `daily_pnl` row using the BASE-aware `system.portfolio_equity.live_portfolio_value` so NAV history is preserved regardless of whether trades fill, with a final flush on graceful shutdown (**D029**). All three loops use **chunked sleep** so **`stop()`** cancellation drains within seconds, not only after a full interval.

**Execution engine** (`execution/engine.py`): optional **`broker_manager`** injection for shared adapters; **`place_order`** retries use linear backoff plus extra **uniform jitter** for **`ibkr`** (**`IBKR_PLACE_ORDER_RETRY_JITTER_SEC`**) to reduce burstiness against TWS (**D023**).

**FastAPI read auth** (`api/server.py`): When **`DASHBOARD_READ_TOKEN`** is set, **`_DashboardReadMiddleware`** protects read routes unless **`PYTEST_API_DISABLE_READ_MIDDLEWARE`** is truthy (pytest **`conftest`** defaults it **on** so CI/local tests work with an operator `.env`; tests that assert auth clear it — **D023**). Mutating routes may require **`API_CONTROL_TOKEN`** (**`X-Control-Token`**).

**Global edge coordinator** (`portfolio/global_edge_coordinator.py`): ranks held-position expected-remaining-edge against incoming strategy opportunities and emits incremental `open_strategy` actions. Two controls are **mode-aware** (hunter / trader / defender) — `max_actions_per_tick` caps per-tick open count, and `max_notional_fraction_per_action` caps each action's capital as a fraction of the strategy's requested amount. Hunter defaults: 10 actions × 1.00 fraction (aggressive deployment); defender: 1 × 0.15 (risk-off). Both values accept either a scalar (legacy uniform behaviour) or a per-mode dict in `config/global_edge.yaml` (**D030**).

**Sizing pipeline (D031 + D032).** `signal_candidate_to_strategy_opportunity` derives `capital_required` from strategy intent, not from a blanket NAV fraction. Priority: `metadata["risk_notional_override"]` → `metadata["target_notional"]` → `nav × position_pct` (legacy fallback). A hard ceiling of `nav × max_position_pct` (default 0.10 from `config/risk_limits.yaml`) is applied as a cap only — it never inflates small strategy-requested sizes upward. Every decision is recorded in `metadata` under `sizing_*` keys (`sizing_source`, `sizing_proposed_base_notional`, `sizing_hard_cap_notional`, `sizing_final_capital_required`, `sizing_clipped`, `sizing_clip_reason`) and propagated through `CoordinatorAction` → `RawSignal` → `Signal` for downstream auditability. D032 completes the intent path by making directional strategies (`momentum_breakout`, `mean_reversion`) emit `target_notional` directly from confidence + ATR-aware scaling (`sizing_intent_source=strategy_confidence_volatility`), so the coordinator no longer has to rely on universal nav fallback.

**Execution-boundary sizing guard (D031C).** `ExecutionEngine._passes_sizing_boundary_guard` runs before broker placement and rejects any order whose actual notional (`abs(quantity) × limit_price`) exceeds the coordinator's `sizing_final_capital_required` by more than 1.25×, or exceeds the declared `sizing_hard_cap_notional`. Arbitrage legs are exempt; legacy signals without sizing metadata pass through unmodified. This is a defensive backstop, not the primary sizing mechanism — normal flows never trigger it.

**Held-position oversize detection (D031D).** `held_positions_from_portfolio` accepts `nav` and `max_position_pct`; when provided, each held position is compared to the ceiling and flagged in metadata as `oversized_position_flag` + `position_above_target_ratio` when live notional exceeds the ceiling by more than `oversize_flag_ratio` (default 1.25×). Detection only — no auto-liquidation.

**Post-open stop-loss framework (D031E + D033 runtime wiring).** `risk/stop_loss.py::evaluate_stop_loss` remains the pure decision function, and `system/orchestrator.py` now runs a dedicated `stop-loss-monitor` task (default every 15s; env `STOP_LOSS_MONITOR_INTERVAL_SEC`, min 5s). For each live broker position, if the evaluator returns `should_close=True`, orchestrator emits a `reduce_only` close signal (`strategy=stop_loss_monitor`) that still passes through the normal risk gate (`RiskEngine.evaluate_and_persist`) and then execution (`ExecutionEngine.execute`) — no bypass. A per-position cooldown (`STOP_LOSS_CLOSE_COOLDOWN_SEC`, default 60s) prevents repeated close spam while fills/reconciliation settle.

**Position reconciliation** (`execution/engine.py::_reconcile_positions_internal`): periodic tick compares the latest `PositionLog` snapshot against each connected broker's `get_positions()`. The broker is the authoritative source of truth for holdings: the remote snapshot is **persisted unconditionally** (so the allocator's `held` view and the UI positions always reflect reality), mismatches are logged, and the function returns `False` so upstream can surface the divergence and the opt-in `auto_kill_on_reconciliation_failure` hook still fires when configured (**D030**).

**Decision references:** **D020–D023** in **`docs/DECISIONS.md`** for dashboard clock, shared DB bind, late venues + AI status, pytest/Docker/pipeline/jitter behaviour.
