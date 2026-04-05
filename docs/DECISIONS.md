# DECISIONS.md
# =============
# Every significant architectural decision, with reasoning.
# Add to this file whenever a decision is made.
# This file keeps Claude, Cursor, and the developer aligned.

---

## D001 — Adapter pattern for all brokers
**Date:** 2026-04-04
**Decision:** Every broker implements a single abstract interface (`brokers/base.py`).
The rest of the system only speaks this interface, never broker-specific code.
**Reason:** Adding a new exchange should require zero changes to strategy, risk, or execution code.
Bybit, Deribit, OKX, or any future exchange can be added with one new file.
**Status:** Implemented in M1.

---

## D002 — IBKR as primary broker
**Date:** 2026-04-04
**Decision:** Interactive Brokers Pro is the primary broker for all non-crypto assets.
**Reason:** Only single platform that covers US stocks, UK stocks, bonds, ETFs, forex, options,
futures, and now 11 crypto assets. Full API access. Used by professional firms.
**Status:** Account to be opened. M1 implementation pending.

---

## D003 — Kraken + Binance as crypto layer
**Date:** 2026-04-04
**Decision:** Kraken is primary crypto exchange, Binance is secondary for liquidity/coverage.
**Reason:** IBKR crypto covers only 11 coins. Kraken adds 640+ pairs, GBP-native, UK-friendly.
Binance adds highest liquidity and widest coin selection.
**Status:** Accounts already exist. API keys to be generated.

---

## D004 — Risk engine has unconditional veto power
**Date:** 2026-04-04
**Decision:** No order can be placed without passing through the risk engine.
No bypass, no flag, no override in code.
**Reason:** The single most dangerous failure mode is an automated system placing
orders the human would not have approved. Risk engine is the last line of defence.
**Status:** Skeleton implemented in `risk/engine.py`.

---

## D005 — AI advises, rules execute
**Date:** 2026-04-04
**Decision:** Claude API is used for news classification, sentiment scoring, and
trade rationale generation only. It never has direct access to order placement.
**Reason:** LLMs are not deterministic and cannot be audited the same way rule-based
systems can. AI output is a score that feeds into the signal engine, which feeds
into the risk engine. Every trade must have a traceable, auditable decision path.
**Status:** Architecture defined. Implementation in M6.

---

## D006 — Paper mode before live, always
**Date:** 2026-04-04
**Decision:** Every strategy runs minimum 2 weeks in paper mode before any real capital.
Paper mode is the default. Live mode requires explicit `APP_ENV=live` in `.env`.
**Reason:** Live trading behaviour differs from paper in ways that only become visible
over time. Operational failures (connectivity, reconciliation, error handling) must
be discovered in paper mode, not with real money.
**Status:** Enforced via `paper_mode` flag on all adapters.

---

## D007 — Decimal for all monetary values
**Date:** 2026-04-04
**Decision:** All prices, quantities, fees, and P&L use `Decimal`, never `float`.
**Reason:** Float arithmetic introduces rounding errors that compound over thousands
of trades. In financial systems this is unacceptable.
**Status:** Enforced in `brokers/base.py` data models.

---

## D008 — TimescaleDB for time-series data
**Date:** 2026-04-04
**Decision:** TimescaleDB (PostgreSQL extension) for all OHLCV and tick data.
**Reason:** Standard PostgreSQL is slow on time-series queries (rolling windows,
range queries). TimescaleDB is purpose-built for this and is fully compatible
with SQLAlchemy and the rest of the PostgreSQL ecosystem.
**Status:** In docker-compose.yml. Schema in `storage/models.py`.

---

## D009 — Momentum breakout as first strategy
**Date:** 2026-04-04
**Decision:** First strategy to implement is momentum breakout on liquid assets.
**Reason:** Most debuggable — every signal has a clear, human-readable reason.
Parameters are intuitive. Backtesting is straightforward. Good first strategy
to validate the full Signal → Risk → Execution pipeline.
**Status:** Implemented in `strategies/momentum.py`. Needs live data to test.

---

## D010 — Smart order routing prefers IBKR for non-crypto
**Date:** 2026-04-04
**Decision:** Smart order router defaults to IBKR for equities, bonds, ETFs, forex.
Routes to Kraken/Binance for crypto not available on IBKR.
**Reason:** IBKR has regulatory clarity, superior execution for traditional assets,
and lowest effective cost for equities ($0.005/share vs % fees on crypto exchanges).
**Status:** Implemented in `execution/router.py`.
