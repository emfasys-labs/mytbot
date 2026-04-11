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

---

## D011 — M2 feature store table + yfinance research feed
**Date:** 2026-04-05
**Decision:** Store OHLCV plus JSON feature payloads in `feature_snapshots` (unique on symbol, timeframe, bar timestamp). Ingest historical and incremental bars via yfinance into Postgres; NewsAPI and FRED are optional parallel feeds with dedupe (headline hash) and macro upsert.
**Reason:** Single queryable store for backtests and live features; yfinance is sufficient for milestone research data before paid market-data vendors. Validation metadata attaches to the latest bar per ingest batch to limit row bloat.
**Status:** Implemented in M2 (`storage/models.py`, `data/`, `run_pipeline.py`).

---

## D012 — Local-first AI architecture (rules + FinBERT + local LLM)
**Date:** 2026-04-11
**Decision:** Replace Claude-first AI layer with local-first provider chain:
rules → FinBERT → local LLM (Ollama) → optional premium fallback (Claude, disabled by default).
No hard daily API call caps. Escalation is necessity-based using materiality, ambiguity,
novelty, and provider disagreement scores. Thresholds are starting heuristics that should
evolve into dynamic parameters via ParameterManager with regime/exposure overrides.
**Reason:** Claude API cost was economically irrational at current scale (~£20 spend for ~£20 profit).
The AI tasks (headline sentiment, event classification, rationale) are structured classification
problems that do not require frontier-model intelligence. FinBERT is purpose-built for financial
sentiment. Local LLMs (Llama, Gemma, Qwen) handle nuance. This eliminates recurring API cost,
reduces latency, improves resilience, and removes vendor lock-in — while keeping Claude available
as an escalation path for genuinely ambiguous or high-impact events.
**Status:** Implemented. Provider architecture in `ai/providers/`, router in `ai/router.py`,
escalation engine in `ai/escalation.py`, config in `config/ai.yaml`.

---

## D013 — Dual-model ensemble consensus for LLM escalation
**Date:** 2026-04-11
**Decision:** When rules + FinBERT are insufficient and local LLM escalation triggers,
run BOTH Qwen 2.5:7b and Llama 3.1:8b in parallel on the same headline. Compare results:
- **Agree** (same direction, both confident): accept with boosted confidence, skip premium.
- **Soft disagree** (same direction, weak): average scores, accept locally.
- **Hard disagree** (opposite directions): this IS the complexity signal — escalate to premium.
LLM disagreement is now 25% of the premium escalation score, making it the strongest single factor.
**Reason:** The user's insight: "how does the system know a task is complex if it's not powerful
enough to understand it?" Two independent architectures (Alibaba Qwen vs Meta Llama) trained on
different data disagreeing is a far more reliable complexity signal than any single model's
self-reported confidence. Agreement between independent models is also more trustworthy than
any single model's high confidence. This turns the fallback model from a crash-only backup
into an active participant in quality control.
**Status:** Implemented in `ai/router.py` (Phase 4 ensemble), `ai/escalation.py`
(`evaluate_ensemble`), `ai/schemas.py` (`EnsembleVerdict`), `config/ai.yaml` (ensemble settings).

---

## D014 — Materiality-based tier gating (GPU-optimized)
**Date:** 2026-04-11
**Decision:** Replace confidence-only LLM escalation gate with materiality-aware routing:
- HIGH materiality (macro, geopolitical, M&A): ALWAYS escalate to LLM ensemble,
  regardless of FinBERT confidence. FinBERT is a 110M-param model that only does
  surface sentiment — it should never make final decisions on portfolio-moving events.
- MEDIUM materiality (earnings, regulatory, crypto): escalate if FinBERT confidence < 0.75.
- LOW materiality (other, sector, company): escalate only if FinBERT confidence < 0.55.
Materiality is classified by the rules engine using a configurable event_type-to-materiality
map. GPU concurrency raised to 8 (from 3) and timeout reduced to 15s (from 60s) for
RTX 5080 deployment.
**Reason:** With GPU inference (2-3s per headline vs 60-90s on CPU), there is no longer a
performance reason to skip the LLM ensemble on material headlines. FinBERT remains valuable
as a fast pre-filter for noise (~40% of headlines) and as an independent data point, but should
not be the sole decision maker for events that could move the portfolio.
**Status:** Implemented in `ai/escalation.py` (`should_escalate_to_local_llm`),
`ai/providers/rules_provider.py` (configurable `materiality_map`), `ai/router.py`
(new config params passed through), `config/ai.yaml` (materiality_map, confidence bars, gpu settings).
