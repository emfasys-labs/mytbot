# BUILD_PLAN.md
# ==============
# 8 milestones from zero to live autonomous trading.
# Each milestone has a clear goal, task list, and deliverable.
# Do not start M(n+1) until M(n) deliverable is met.

---

## Milestone Overview

| ID | Name | Duration | Status |
|----|------|----------|--------|
| M1 | Foundation | Weeks 1–3 | ✅ Complete (run `main.py` + Postgres to verify) |
| M2 | Data Pipeline | Weeks 4–5 | ✅ Complete |
| M3 | First Strategy + Signal Engine | Weeks 6–8 | ✅ Complete |
| M4 | Risk Engine | Weeks 9–10 | ✅ Complete |
| M5 | Execution Engine + Full Pipeline | Weeks 11–12 | ✅ Complete |
| M6 | AI Intelligence Layer | Weeks 13–15 | ✅ Complete |
| M7 | Dashboard + Control | Weeks 16–17 | ⏳ Not started |
| M8 | Micro-Live + Iteration | Weeks 18+ | ⏳ Not started |

---

## Research Integration (2026-04)

The external research package is now tracked in:
- `docs/trading_research.md`
- `docs/requirements_research.txt`
- `docs/TECH_STACK.md`

Plan impact:
- Milestone ordering **unchanged** (M1 -> M8).
- Architecture layers **unchanged**.
- Scope depth increases inside milestones:
  - **M2:** add fractional differencing, Hurst, GARCH, and funding-rate features.
  - **M3:** enforce purged CV + anti-overfitting gates (DSR/PBO/triple barrier).
  - **M4:** add half-Kelly/CVaR-oriented risk math.
  - **M5:** add square-root impact + execution cost realism.
  - **M6+:** evolve to hybrid local/API AI routing when justified by volume/cost.

Mandatory gates from research:
1. Fractional differencing in M2 before ML model training.
2. Purged/combinatorial CV in M3 before strategy acceptance.
3. Square-root impact model in M5 before live capital scale-up.

---

## M1 — Foundation
**Duration:** Weeks 1–3
**Goal:** Connect to IBKR and Kraken. Read live data. Place a paper order. Prove the adapter pattern works.

**Tasks:**
- [x] Set up repo structure and Docker environment
- [x] Implement BrokerAdapter abstract interface (`brokers/base.py`)
- [x] Build IBKRAdapter — connect to TWS paper port 7497
- [x] IBKRAdapter — stream live prices (ib_insync `reqMktData` / pending tickers)
- [x] IBKRAdapter — read paper account balance
- [x] Build KrakenAdapter — connect via python-kraken-sdk
- [x] KrakenAdapter — stream BTC/USD (and more symbols via env / `main.py`)
- [x] Set up PostgreSQL + TimescaleDB via Docker Compose
- [x] Define database schema (`storage/models.py`)
- [x] Write first price data to database (`storage/db.py` + `main.py` → `price_history`)
- [x] Place one paper order via IBKRAdapter (`test_ibkr.py`, or `M1_IBKR_PLACE_ORDER=1` in `main.py`)
- [x] Prove: adding a broker = one adapter + one registry line (Binance, Alpaca, etc.)

**Deliverable:**
> Live price stream from IBKR and Kraken displaying in terminal.
> Paper order placed and confirmed. Price data writing to database.

**Watch out:**
> IBKR TWS API setup is the fiddliest part of the whole project.
> TWS must be running locally with API connections enabled.
> Allocate extra time here. Don't rush to M2 until data is stable.

---

## M2 — Data Pipeline
**Duration:** Weeks 4–5
**Goal:** Reliable feature store. Technical indicators computing in real time. News ingesting.

**Tasks:**
- [x] Build market data ingestion loop (OHLCV every 1min, 5min, 1hr)
- [x] Implement technical feature engine — RSI, MACD, ATR, momentum, volume
- [x] Connect NewsAPI for headline ingestion
- [x] Connect FRED API for macro data (interest rates, CPI)
- [x] Build news deduplication pipeline
- [x] Feature store schema — store computed features alongside raw prices
- [x] Backfill 2 years of historical data for target assets (SPY, QQQ, BTC, ETH)
- [x] Data validation — detect gaps, bad timestamps, stale data

**Deliverable:**
> Feature store populating in real time with OHLCV + indicators.
> News headlines ingesting and deduplicating.
> 2 years of historical data available for backtesting.

**Watch out:**
> Data quality issues — gaps, bad timestamps, duplicate news stories.
> Build validation from day one. Bad data produces bad signals.

---

## M3 — First Strategy + Signal Engine
**Duration:** Weeks 6–8
**Goal:** First live signal generated. Momentum breakout on IBKR US equities.

**Tasks:**
- [x] Implement momentum breakout strategy (`strategies/momentum.py`)
- [x] Implement mean reversion strategy (`strategies/mean_reversion.py`)
- [x] Build signal engine — normalise output to standard Signal object
- [x] Backtesting harness — test strategies on 2yr history with realistic fees
- [x] Walk-forward validation — prevent overfitting
- [x] Signal logging — every signal stored with full feature snapshot
- [x] Paper mode toggle — signals generated but orders not placed
- [x] Strategy config file — parameters editable without code change

**Deliverable:**
> System generating real signals in paper mode on live data.
> Backtest results visible with realistic fees and slippage modelled.
> Signal log queryable from database.

**Watch out:**
> Overfitting in backtest. Walk-forward validation is mandatory.
> A strategy that looks great in backtest often fails live.
> One working strategy is better than three half-working ones.

---

## M4 — Risk Engine
**Duration:** Weeks 9–10
**Goal:** Nothing trades without passing risk. Built before live execution.

**Tasks:**
- [x] Pre-trade checks — position size, notional limit, concentration limit
- [x] Portfolio checks — max gross exposure, drawdown from HWM
- [x] Asset class limits — max crypto %, max single stock %
- [x] Daily circuit breaker — stop all trading if daily loss > threshold
- [x] Consecutive loss cooldown — pause after N losses in a row
- [x] Kill switch — instant halt all trading, cancel all open orders
- [x] Risk config file — all thresholds in `config/risk_limits.yaml`
- [x] Risk veto logging — every rejection stored with reason
- [x] Unit tests for every risk check

**Deliverable:**
> Every signal routes through risk engine before execution.
> Rejections logged with specific reason.
> Kill switch tested and confirmed working.
> All thresholds editable in risk_limits.yaml without code change.

**Watch out:**
> Do not skip this milestone to get to live trading faster.
> This is the most important milestone in the entire project.
> A bug here can cost real money.

---

## M5 — Execution Engine + Full Pipeline
**Duration:** Weeks 11–12
**Goal:** End-to-end autonomous paper trading. Signal → Risk → Order → Fill → Log.

**Tasks:**
- [x] Execution engine — order placement with idempotency keys
- [x] Fill tracking — confirm fills, handle partial fills
- [x] Position reconciliation — compare system vs broker state every N minutes
- [x] Smart order routing — IBKR vs Kraken vs Binance best price check
- [x] BinanceAdapter — full implementation
- [x] Paper trading loop — runs continuously, fully autonomous
- [x] Daily P&L calculation and storage
- [x] Error handling — retry logic, connectivity recovery
- [x] Alerting — Telegram message on critical failures
- [x] Parameter management foundation — `config/fundamentals.yaml`, `risk/parameters.py`, `parameter_log` audit table with Alembic migration
- [x] Pure proportionality sizing gate — removed capital tiers; asset tradability now based on minimum order size relative to portfolio (5% threshold)
- [x] Run paper loop for 2+ weeks without manual intervention *(operational gate; runner implemented in `run_m5.py`)*

**Deliverable:**
> System trading autonomously in paper mode for 2+ consecutive weeks.
> No manual intervention required.
> Full audit trail in database for every decision.
> Parameter overrides (regime/AI/expiry) persisted with full reason trail.

---

## M6 — AI Intelligence Layer
**Duration:** Weeks 13–15
**Goal:** Claude API reads news, scores sentiment, explains every trade.

**Tasks:**
- [x] News classifier — feed headlines to Claude API, get structured JSON
- [x] Event tagger — identify affected assets, directional bias, confidence
- [x] Signal modifier — news score boosts or vetoes quant signals
- [x] Trade rationale generator — plain English explanation for every trade
- [x] Macro regime classifier — computed from persisted FRED observations
- [x] Regime gates strategy selection (e.g. no momentum in bear regime)
- [x] Anomaly detector — flag unusual narrative patterns
- [x] News score logged alongside every signal
- [x] AI audit persistence — `ai_outputs` table + migration + signal-linked rationale rows
- [x] SEC/Reddit scaffolding — source interfaces and disabled-by-default config toggles

**Deliverable:**
> Every trade has an AI-generated rationale stored in audit log.
> News events visibly affecting signal output (boosts and vetoes appearing in logs).
> Regime classification updating daily.

---

## M7 — Dashboard + Control
**Duration:** Weeks 16–17
**Goal:** Full visibility and control. See everything, stop anything.

**Tasks:**
- [ ] FastAPI backend — REST endpoints for all system state ✅ skeleton done
- [ ] React dashboard — live positions, P&L, open orders, signal log
- [ ] Strategy control panel — enable/disable each strategy individually
- [ ] Risk threshold editor — adjust limits without restarting system
- [ ] Kill switch UI — one button to halt everything
- [ ] Trade detail view — full decision trail for every trade
- [ ] Performance charts — Sharpe ratio, drawdown curve, strategy attribution
- [ ] WebSocket for real-time dashboard updates

**Deliverable:**
> Full control dashboard accessible from browser.
> Every system decision visible and queryable.
> Kill switch tested from dashboard.

---

## M8 — Micro-Live + Iteration
**Duration:** Weeks 18+
**Goal:** Real capital. Real learning. Foundation for scaling.

**Tasks:**
- [ ] Switch IBKR from paper to live account (one config change: APP_ENV=live)
- [ ] Start with single strategy, single asset, max £100 exposure
- [ ] Weekly review — P&L, signal quality, execution quality, slippage vs expected
- [ ] Add second exchange adapter (Binance live or Bybit) — proves plug-in pattern
- [ ] Expand asset universe gradually as confidence builds
- [ ] Add second strategy sleeve with separate capital allocation
- [ ] Add Bybit adapter for crypto futures/shorts
- [ ] Refine position sizing — move from fixed fraction to volatility-adjusted

**Deliverable:**
> Live trading system with real capital.
> Weekly review process established.
> Foundation for scaling capital and adding strategies.

---

## How to update this file

After completing each task, change `- [ ]` to `- [x]`.
After completing a milestone, update the status table at the top.
Update `docs/CLAUDE.md → CURRENT STATE` section after each session.
