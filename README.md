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
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your API keys

# 3. Start infrastructure
docker-compose up -d db redis

# 4. Run in paper mode
python main.py
```

## Build Plan

| Milestone | Focus | Status |
|-----------|-------|--------|
| M1 | Broker connectivity, adapter pattern | 🚧 In progress |
| M2 | Data pipeline, feature store | ⏳ |
| M3 | First strategy, signal engine | ⏳ |
| M4 | Risk engine | ⏳ |
| M5 | Execution engine, full paper loop | ⏳ |
| M6 | AI intelligence layer | ⏳ |
| M7 | Dashboard & control | ⏳ |
| M8 | Micro-live trading | ⏳ |

## Architecture Principles

- **Risk engine is law** — no order bypasses it, ever
- **AI advises, rules execute** — Claude scores and explains, never places orders directly  
- **Adapters are isolated** — no broker-specific code outside `brokers/`
- **Paper mode first** — every strategy runs in paper mode before touching real capital
- **Everything is logged** — full audit trail, every decision
