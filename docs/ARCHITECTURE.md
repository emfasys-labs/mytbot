# ARCHITECTURE.md
# ================
# Full system architecture for mytbot.
# Read this before making any structural changes.

## Overview

mytbot is a personal autonomous multi-asset trading system.
It monitors markets 24/7, generates trading signals, validates them
through a strict risk engine, and executes orders across multiple
brokers — all without human intervention.

**Assets traded:** US equities, UK equities, ETFs, bonds, forex, crypto
**Primary broker:** Interactive Brokers Pro (IBKR)
**Crypto brokers:** Kraken, Binance
**Capital:** Personal funds only. Not a public product.

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
│              SIGNAL ENGINE                           │
│  Aggregates strategy outputs                         │
│  Applies AI news modifier (boost or veto)            │
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
│  Dashboard (FastAPI + React)                         │
│  Alerts (Telegram/email on failures)                 │
│  Kill switch (one button halts everything)           │
└─────────────────────────────────────────────────────┘
```

---

## The Adapter Pattern — Most Important Design Decision

Every broker implements one identical interface defined in `brokers/base.py`.
The rest of the system never knows which broker it's talking to.

```
brokers/base.py          ← interface (FROZEN — never changes)
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
1. Market data arrives via WebSocket (prices, candles, order book)
2. Feature engine computes indicators (RSI, MACD, ATR, momentum)
3. News arrives via NewsAPI/RSS → Claude API classifies it
4. Strategy engine runs on every new candle:
      features → strategy.generate_signal() → RawSignal or None
5. Signal engine:
      RawSignal + news_score → Signal (with adjusted confidence)
      If news strongly negative → Signal vetoed before risk engine
6. Risk engine evaluates Signal:
      Runs all checks → APPROVED or REJECTED
      Logs decision either way
7. Execution engine (approved only):
      Builds Order from Signal
      Router picks best broker
      Places order with idempotency key
      Tracks fill
8. Portfolio tracker updates positions and P&L
9. Everything written to audit log
10. Dashboard reflects real-time state
```

---

## AI Layer — What It Does and Doesn't Do

**Does:**
- Reads news headlines every few minutes
- Classifies event type (earnings, macro, regulatory, etc.)
- Scores sentiment per asset (-1.0 to +1.0)
- Boosts or vetoes signals based on news context
- Generates plain-English rationale for every trade
- Detects unusual narrative patterns (anomaly detection)

**Does NOT:**
- Place orders (ever)
- Access broker APIs directly
- Make portfolio allocation decisions
- Override risk engine decisions

---

## Key Principles

| Principle | Rule |
|-----------|------|
| Risk engine is law | No order bypasses it. No exceptions. |
| AI advises, rules execute | AI scores signals. Rules place orders. |
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
signals/      Signal aggregation and AI modifier.
ai/           Claude API calls only. No trading decisions.
risk/         Risk checks and kill switch only.
execution/    Order management and routing only.
portfolio/    Position tracking and P&L only.
storage/      Database models and queries only.
api/          FastAPI endpoints only. Read-only mostly.
monitoring/   Alerts and uptime checks only.
config/       Configuration files only.
docs/         Documentation only.
dashboard/    React frontend only.
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
