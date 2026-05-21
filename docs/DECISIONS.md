# DECISIONS.md
# =============
# Every significant architectural decision, with reasoning.
# Add to this file whenever a decision is made.
# This file keeps Claude, Cursor, and the developer aligned.

**Hygiene note:** A later block reuses labels **D012–D014** for funding / coordination topics while **D012–D014** already appear as local-first AI / tier gating. When implementing, read the **heading title and date**, not the number alone. Renumbering is a planned doc cleanup.

| Reused id | Earlier section | Later section (lower in file) |
|-----------|-----------------|----------------------------------|
| D012 | Local-first AI architecture | Funding rate arbitrage as first arbitrage strategy |
| D013 | Dual-model ensemble consensus | Strategy coordination layer above strategy outputs |
| D014 | Materiality-based tier gating | Global edge coordinator vs D015-as-final allocator |

---

## D067 — Fee-first execution gating and accounting
**Date:** 2026-05-01
**Decision:** Transaction costs are treated as mandatory execution constraints across all strategy flows. The Wave 9 pre-flight cost gate is enabled by default, and per-fill fees are always persisted into daily P&L accumulation (including opening/add flows, not only realised closes).
**Reason:** Frequent churn can harvest gross unrealised moves while destroying net returns after commissions/fees/spread/slippage. Cost-awareness must be universal and strategy-agnostic at the execution boundary.
**Status:** Implemented in `config/execution_models.yaml`, `system/trading_loop/loop.py`, and `run_m5.py`.

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
**Status:** Primary non-crypto venue; IBKR adapter implemented (`brokers/ibkr/`). Account setup is operational (owner).

---

## D003 — Kraken + Binance as crypto layer
**Date:** 2026-04-04
**Decision:** Kraken is primary crypto exchange, Binance is secondary for liquidity/coverage.
**Reason:** IBKR crypto covers only 11 coins. Kraken adds 640+ pairs, GBP-native, UK-friendly.
Binance adds highest liquidity and widest coin selection.
**Status:** Adapters implemented; live use requires valid API keys in `.env`.

---

## D004 — Risk engine has unconditional veto power
**Date:** 2026-04-04
**Decision:** No order can be placed without passing through the risk engine.
No bypass, no flag, no override in code.
**Reason:** The single most dangerous failure mode is an automated system placing
orders the human would not have approved. Risk engine is the last line of defence.
**Status:** Skeleton implemented in `risk/engine.py`.

**Amendment (D015, 2026-04-11):** The D015 allocator may be the single source of truth for *sizing* and portfolio-level exposure targets. The risk engine still evaluates every order and may veto for kill switch, drawdown/daily loss, min order, M8 micro-live guards (when active), proportionality, confidence floor, asset-class limits, cooldown, and operational integrity — but when `allocator_d015_primary` is set (default unless `ALLOCATOR_D015_LEGACY_FALLBACK=true`), it does **not** re-apply duplicate caps that the allocator already encodes (`max_gross_exposure_pct`, `max_position_pct`, catalyst/quality/theme checks from `risk_modes.yaml`). Mode labels from `risk_modes.yaml` still apply to the risk config for display; numeric mode overlays are skipped in primary mode.

---

## D005 — AI advises, rules execute
**Date:** 2026-04-04
**Decision:** AI/LLM components are used for news classification, sentiment scoring,
and trade rationale generation only. They never have direct access to order placement.
**Reason:** LLMs are not deterministic and cannot be audited the same way rule-based
systems can. AI output is a score that feeds into the signal engine (and optionally
the signal accumulator), which feeds into the risk engine. Every trade must have a
traceable, auditable decision path.
**Status:** Implemented (M6); **superseded in provider choice** by **D012** (local-first routing). Invariant unchanged: **AI never executes orders.**

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

---

## D015 — Global opportunity replacement allocator
**Date:** 2026-04-11
**Decision:** Capital allocation is driven by a global opportunity ranking and replacement model, not by static capital sleeves, fixed position-count limits, or hard-coded exposure caps as primary logic.

The system must continuously compare (1) current positions using capital and (2) new candidate opportunities from all enabled strategies and brokers. If a candidate offers materially better expected value than one or more held positions, the system may reduce or close those positions—including small winners, flat P&L, or controlled small losses—to fund the stronger opportunity.

**What changes:**
- No fixed maximum position count as a primary trading rule
- No primary rejection path of “no free capital” when better opportunities exist
- No hard strategy sleeve barriers as primary allocation logic (sleeves remain optional for attribution/reporting)
- Held positions are always eligible for reduction or replacement; capital is continuously contestable

**What remains true:**
- The risk engine retains unconditional veto for ruin prevention, operational integrity, invalid market state, broker rejection, margin danger, impossible execution, or system-health failure
- AI advises, scores, and explains only; it never executes directly
- All replacement decisions, rejections, and reallocations must be logged with reasoning
- `Decimal` for prices, quantities, fees, P&L, and target weights

**Allocation philosophy:**
- Gross exposure, concentration, leverage, and replacement aggressiveness are computed outputs from regime, opportunity scores, liquidity, volume anomaly, breadth, drawdown, and execution quality
- Defender / Trader / Hunter shape behaviour through policy coefficients (see `config/profile_modes.yaml`), not static mode caps; explicit safety bounds remain configurable emergency rails only

**Operational question:** Not “Do we have spare cash?” but “Is this opportunity better than the weakest current use of capital?”

**Reason:** Static sleeves and fixed exposure buckets block the proactive, speculative reallocation the system is intended to support. This decision aligns implementation with layered parameters and auditable risk while preserving risk-engine supremacy.

**Status:** Implemented end-to-end: primary trading path in `system/trading_loop.py` batches `SignalCandidate`s → regime → `build_opportunities_async` → `build_allocation_decision` (replacement interval + churn from `config/allocation.yaml`) → `apply_allocation_smoothing` → `build_execution_plan` → `risk_signal_from_execution_instruction` → existing `RiskEngine` + `ExecutionEngine`. Volume escalation enqueues `d015_volume_refresh` on `CommandBus`; the next cycle merges refreshed features via `drain_volume_refresh_features`. `ALLOCATOR_D015_LEGACY_FALLBACK=true` restores the per-symbol legacy signal path. When `allocator_d015_primary` is active (default), `risk/engine.py` skips allocator-duplicative checks; kill switch, min order, drawdown/daily loss, proportionality, confidence, asset class limits, M8 guards remain.

**Env:** `ALLOCATOR_D015_SHADOW=true` logs legacy vs D015 summary (legacy path only). `ALLOCATOR_D015_LEGACY_FALLBACK=true` forces legacy loop. See `docs/D015_VALIDATION.md` and `scripts/d015_paper_report.py`.

---

## D012 — Funding rate arbitrage as first arbitrage strategy
**Date:** 2026-04-12
**Decision:** First arbitrage module is funding-rate carry (long spot / short perpetual) with broker-agnostic venue discovery via `data/capability_registry.py` and `execution/venue_selector.py`. Optional perp snapshot support lives on concrete adapters (e.g. Bybit linear `fetch_funding_market_snapshot`) without extending `brokers/base.py`.
**Reason:** Lower latency sensitivity than cross-exchange spot scalping; fits multi-broker adapters and pre-funded treasury model; structural edge is funding, not price prediction.
**Status:** Implemented (scan + signal + risk hooks + execution routing). Enable under `config/strategies.yaml` / `risk_limits.yaml` when ready.

---

## D013 — Strategy coordination layer above strategy outputs
**Date:** 2026-04-12
**Decision:** Add `portfolio/opportunity_book.py`, `portfolio/strategy_coordinator.py`, and `portfolio/capital_scheduler.py` to rank `StrategyOpportunity` objects across strategies before capital allocation. Coordinator ranks only; risk engine and execution paths remain authoritative.
**Reason:** Reduces strategy conflict, preserves optional reserve for short-lived arb, allows regime-weighted priority without bypassing risk veto.
**Status:** Implemented (library components; optional wiring into `system/trading_loop.py` later).

---

## D014 — Global edge coordinator vs D015-as-final allocator
**Date:** 2026-04-12
**Decision:** When `GLOBAL_EDGE_COORDINATOR=true` (or `enabled` in `config/global_edge.yaml`), the trading loop may use `portfolio/global_edge_coordinator.py` to rank held positions (`HeldPositionEdge`) and new `StrategyOpportunity` rows (directional batch + optional funding/cross-exchange arb scans) and emit **incremental** `CoordinatorAction`s only. Deployment intent for that tick comes from this coordinator; `build_allocation_decision` is skipped for that tick when the global-edge path runs. Coordinator output is converted via `signals/arb_bridge.py` into unified signals, then **ExecutionPlanner** (cross-exchange), **RiskEngine**, and **ExecutionEngine** unchanged — no risk bypass, no change to `brokers/base.py`.
**Reason:** Single place to compare “remaining edge” in existing positions vs new opportunities (including arb) under mode thresholds, without full liquidation/re-allocation in one step; keeps D015 available when the flag is off.
**Status:** Implemented behind env/YAML; `ENABLE_ARBITRAGE` gates arb scans; treasury snapshot merged via `portfolio/treasury_manager.merge_treasury_into_portfolio_state`.

---

## D016 — IBKR single-leg options (structured instrument, no strategy engine yet)
**Date:** 2026-04-12
**Decision:** Support US equity-style single-leg options on IBKR with a first-class `OptionContractSpec` (`core/instruments.py`), optional `Order.instrument_metadata` / `Position.instrument_metadata` on the frozen adapter models for serialization only, IBKR-specific chain/qualify/market-data helpers on `IBKRAdapter`, and a dedicated `options_trading` risk gate (`risk/engine.py` + `config/risk_limits.yaml` + env overrides in `risk/options_env.py`). No multi-leg, no Greeks/IV engine, no AI option reasoning; default policy is long-only opens in paper-first mode with explicit rejection reasons.
**Reason:** Options permissions are available on the account; the system must represent contracts cleanly, route orders through the same risk → execution path, persist option metadata for audit, and stay conservative until dedicated strategy and surface work exists.
**Status:** Implemented. Enable with `ENABLE_OPTIONS=true` and tighten limits via `OPTIONS_*` env vars or YAML.

---

## D017 — Stateful signal accumulation (per-asset conviction memory)
**Date:** 2026-04-12
**Decision:** Trading signals are not purely point-in-time. The system maintains a **persistent, time-decayed signal state per symbol** in `signals/accumulator.py`, combining quantitative strategy inputs, rolled-up AI news scores, and macro regime bias before the unified `Signal` is produced. Decay uses half-lives per horizon; reinforcing horizons increase conviction; divergence reduces confidence. The risk engine remains the final authority; the accumulator does not bypass risk.
**Reason:** Markets reflect accumulated information; several weak aligned inputs are often more meaningful than a single headline. Explicit state makes behaviour auditable and explainable.
**Implication:** `SignalEngine` accepts an optional `SignalAccumulator`; `config/strategies.yaml` `signal_engine.use_signal_accumulator` enables the path; `system/trading_loop/` / `run_m3.py` / `run_m5.py` ingest `AIPipelineResult` into the accumulator each AI cycle.
**Note:** `docs/DECISIONS.md` currently contains duplicate **D012–D014** section numbers after D015 (arb / coordinator entries). Renumber those in a dedicated doc cleanup; do not reuse those IDs for new decisions.

---

## D018 — Trading loop package, fast control commands, broker degradation
**Date:** 2026-04-12
**Decision:** (1) The orchestrator trading loop lives in the `system/trading_loop/` package (`TradingLoop` in `loop.py`, shared YAML/volume helpers in `helpers.py`) instead of a single oversized `trading_loop.py` module. (2) Control commands (`kill`, `set_parameter`, etc.) are processed on a short interval (`CONTROL_COMMAND_POLL_SEC`, default 2s) via a dedicated asyncio task so long iterations do not delay kill/parameter updates. (3) Execution auto-fail and reconciliation auto-fail default to **per-broker disable** (`RiskEngine.disable_broker`) rather than global kill; `EXECUTION_AUTO_KILL_GLOBAL=true` restores the old global kill behavior. (4) Optional `brokers` lists on kill/reset API payloads disable or re-enable specific venues without a full global kill.
**Reason:** Maintainability, operational responsiveness, and isolation when one venue fails while others remain healthy.
**Status:** Implemented.

**Risk parameter persistence (unchanged contract):** Regime overrides from the dashboard/API still merge into `ControlState`, persist to `config/risk_parameter_overrides.yaml` on successful `set_parameter`, and reload from disk on `ParameterManager` init; `hydrate_risk_parameters_from_bus` restores in-process state at runner startup.

---

## D019 — Dashboard “control tower” snapshot + period P&L
**Date:** 2026-04-12
**Decision:** The React dashboard (`ui/`) prioritises **decision transparency** over decorative charts. The trading loop persists a JSON snapshot to Postgres `ControlState` under key `dashboard.snapshot` (`system/dashboard_publish.py`): D015 path publishes opportunities, `RegimeState` components, `AllocationDecision` (including `allocation_targets`), `ExecutionPlan`, and `PortfolioState` pressure fields; the global-edge path publishes ranked `StrategyOpportunity` rows, held edges, and coordinator actions. `SignalAccumulator.dashboard_snapshot()` adds ranked conviction for the same payload. The API exposes `GET /dashboard/snapshot`; `GET /pnl` adds **calendar** week-to-date and month-to-date sums from `daily_pnl` (same `date` convention as `today`) plus lightweight `metrics` (win-rate on days with trades, max drawdown on stored portfolio value series when enough points exist). WebSocket `tick` frames include a small `dashboard` hint for change detection.
**Reason:** The operator must see allocator intent, risk outcomes, and capital context on one screen without reading logs; period P&L answers “how am I doing this week/month” without ad-hoc spreadsheets.
**Status:** Implemented.

---

## D020 — Unconditional dashboard heartbeat when allocator snapshot is skipped
**Date:** 2026-04-12
**Decision:** When a loop iteration does **not** run the full D015 or global-edge dashboard publish (e.g. `batch_candidates` empty, legacy per-symbol path, or publish failure), the loop still writes `dashboard.snapshot` via `publish_dashboard_snapshot_heartbeat()` in `system/dashboard_publish.py`. Payload includes `heartbeat_only`, `dashboard_feed` (`reason`, `message`, symbol/feature counts, batch size), empty `opportunities` / `allocation`, and current `portfolio` serialization so `GET /dashboard/snapshot` reflects each tick. The UI shows a short banner in `SignalBrain` when `heartbeat_only` is true.
**Reason:** Conditional publish made an empty feature store or mis-scoped universe **indistinguishable** from a genuinely quiet market; the API could return `{}` or a stale snapshot with no diagnostic.
**Status:** Implemented (`system/trading_loop/loop.py`; `_run_global_edge_tick` returns `(executed, dashboard_snapshot_written)` so heartbeat runs if global-edge publish fails). **`GET /system/status`** merges `trading.snapshot_published_at` from the same `dashboard.snapshot` `updated_at` so the UI can treat staleness against the loop clock without relying only on the last HTTP snapshot fetch.

---

## D021 — Shared DB pool, execution engine registry, signal veto Decimal hygiene
**Date:** 2026-04-18
**Decision:** (1) FastAPI startup calls `storage.db.bind_app_database(engine, session_factory)`; the trading loop prefers `get_app_database()` and **does not** open a second async engine when the API already bound one (still creates its own `CommandBus` wrapper over the shared factory). On loop-only entry points without a prior bind, behaviour is unchanged (`init_async_database`). The loop **only** disposes an engine it created (`owns_engine`); shared engines are never disposed from the loop. (2) `ExecutionEngine.__init__` registers `set_execution_engine(self)`; the loop clears it in `_run` `finally`. (3) `SignalEngine` news veto / confidence blending uses `Decimal` for thresholds and overlay scores; accumulator metadata stores string decimals; `accumulator_dual_ai_veto` no longer stacks a point-in-time news veto when an accumulator `NetSignal` exists. (4) IBKR option build uses `float(str(spec.strike))` from `Decimal` strike. (5) Orchestrator persists `last_start_error` across `errors.clear()` on retry start; status exposes it. (6) UI: first-cycle wait copy on `LiveStrip`, `last_start_error` on `error`, clear `snapshotFetchFailed` when not running.
**Reason:** Audit P0-1/P0-2/P0-5/P0-6/P0-7/P1-9/P1-10; reduce silent double pools, fix `/status` execution visibility, and avoid float drift in veto math.
**Status:** Implemented.

---

## D022 — Late venues on execution engine + AI health on status
**Date:** 2026-04-18
**Decision:** (1) `ExecutionEngine` accepts optional `broker_manager`; `_get_broker` prefers an already-connected adapter from `broker_manager.adapters` before calling `get_broker`, so late connects (e.g. IBKR) reuse the same instance as routing/reconciliation instead of a duplicate client. `TradingLoop._check_late_brokers` calls `execution_engine.add_allowed_broker(name)` so reconciliation preload includes the venue. (2) `AIRouter` / `NewsClassifier` expose `runtime_ai_status()`; `publish_runner_heartbeat` adds an `ai` object (kind, `providers_enabled`, `ai_degraded`); `GET /system/status` merges that `ai` blob into `trading` when `runtime.heartbeat` is present.
**Reason:** Audit P1-1 (late broker vs execution) and P1-8 (observable AI degradation without log diving).
**Status:** Implemented.

---

## D023 — Pytest read bypass, Docker retries, pipeline cancellable sleep, STARTING hint, IBKR jitter
**Date:** 2026-04-18
**Decision:** (1) Dashboard read middleware skips checks when `PYTEST_API_DISABLE_READ_MIDDLEWARE` is truthy; `tests/conftest.py` sets default `1` so TestClient works with a developer `.env` that defines `DASHBOARD_READ_TOKEN`. Tests that assert read protection call `monkeypatch.delenv("PYTEST_API_DISABLE_READ_MIDDLEWARE", raising=False)` first. (2) `dependency_manager._start_docker_service_with_retries` wraps `docker compose up -d` for `db` / `redis` (attempts `DOCKER_COMPOSE_UP_ATTEMPTS`, default 3; backoff + jitter between attempts). (3) Orchestrator pipeline uses `Orchestrator._sleep_cancellable` (~2s chunks) instead of one long `asyncio.sleep` so stop/cancel responds within seconds. (4) While `state == starting`, `Orchestrator.status()` adds `trading.orchestrator_starting: true`. (5) On `place_order` retries for broker `ibkr`, `ExecutionEngine` adds uniform jitter up to `IBKR_PLACE_ORDER_RETRY_JITTER_SEC` (default 0.5s) after linear backoff.
**Reason:** Audit P1-4/P1-5/P1-6/P1-7 and pytest stability; reduce TWS burst sensitivity on reconnect retries.
**Status:** Implemented.

---

## D024 — Live equity curve uses broker `get_last_price`, intraday client buffer
**Date:** 2026-04-21
**Decision:** `/pnl`'s `today.portfolio_value` is now re-computed on every poll by marking the latest `PositionLog` snapshot to the freshest available price. Price priority in `api.server._compute_live_unrealised_mtm`: (1) broker live `get_last_price(symbol)` — raced across **all** connected adapters via `_live_broker_prices`, first non-zero result wins with a 1.5s per-adapter timeout; (2) `FeatureSnapshot` latest close (hourly bar fallback); (3) `PositionLog.current_price`; (4) average entry (no movement). The redesign dashboard hook (`ui/src/app/redesign/useLiveSystem.ts`) samples the live NAV into a rolling intraday buffer (`liveNavSamples`, capped at `MAX_LIVE_NAV_SAMPLES = 360` ≈ 1h at 10s cadence) and blends it with `/pnl/history` daily rows into the hero `EquityCurve`. `EquityCurve` was also repadded (`padX=10`, `padY=6`, rounded stroke, non-scaling stroke width) so the pulsing endpoint dot never clips against the card's right edge.
**Reason:** Backend previously drove NAV from hourly `FeatureSnapshot` bars plus cached broker equities; a stale position on an idle venue froze the hero curve flat for hours. Racing adapters avoids being pinned to a 15-minute-delayed IBKR paper feed when Alpaca's IEX feed is live, and the client buffer guarantees ≥ 2 points as soon as the system runs so the curve always renders as a time-series (even when the price is flat between ticks).
**Status:** Implemented. Covered by `tests/test_live_broker_prices.py` (8 cases — fallback order, zero/exception handling, timeout isolation, multi-symbol resolution).

---

## D025 — Full strategy roster on `/system/status` (idle strategies remain visible)
**Date:** 2026-04-21
**Decision:** `TradingLoop.status_dict()` (→ `Orchestrator.status()` → `GET /system/status`) publishes `loaded_strategies: [{name, enabled, kind}]` covering every strategy the loop has registered — signal strategies (`momentum_breakout`, `mean_reversion`) **and** the arbitrage stack (`funding_rate_arbitrage`, `cross_exchange_arbitrage`). The redesign hook (`useLiveSystem.ts`) fetches intelligence signals at the endpoint's max (`limit=50` vs previous `16`) and merges the roster into the UI's strategy list via `mergeStrategiesWithSignals(snapshotStrategies, sigs, loadedStrategies)` in `mapping.ts`. Strategies with no opportunities in the current snapshot / signal window are rendered as zero-weight `idle` cards on `StrategiesScreen`, with an "arbitrage" pill for the arb stack and a "disabled" pill when `strategy.enabled=false`.
**Reason:** Previously the Strategy Mix card showed only strategies that had produced opportunities in the current allocator snapshot (or appeared in the newest 16 signals). During a regime where one strategy dominates (e.g. mean-reversion in a low-vol day), other registered strategies silently disappeared from the UI and the operator had no way to distinguish "strategy not running" from "strategy running but idle". Exposing the loop's registry makes the full system behaviour observable without reading logs.
**Status:** Implemented. Covered by `tests/test_loop_status_strategies.py` (6 cases — signal-only, arbitrage-only, missing `enabled` attr fallback, default arb display names, explicit `name` attr override, combined roster).

---

## D026 — In-flight order dedup + accurate position size / exposure display
**Date:** 2026-04-21
**Decision:** (1) `ExecutionEngine.execute()` consults the `orders` table before placement and short-circuits when a non-terminal order for the same `(symbol, side, broker)` exists within `EXECUTION_DEDUP_WINDOW_SEC` (default 900s). Statuses considered in-flight: `pending`, `open`, `partially_filled`. Skipped attempts increment `engine.dedup_skipped` and emit a single `DEDUP SKIP` log line with the existing order id and age. DB lookup failures fall through (best-effort — trading must not hang on a DB hiccup). (2) The dashboard `mapPositions()` prefers the authoritative `quantity` field returned by `/positions` and populates `Position.notional = |qty × last|`; the legacy `unrealised_pnl / (last − avg)` heuristic is kept only as a last-resort fallback. `Position.broker` is now surfaced so the Book row can attribute each holding to its venue. (3) `mapExposure()` and `numFromPortfolio()` auto-detect the unit of `portfolio.gross_exposure` / `net_exposure` (ratio, percent, or absolute £) and normalize by `nav` when the value exceeds 100 — previously absolute £ figures were silently clipped to `100%`, making the Exposure and Capital-at-work panels unusable. (4) `BookScreen` computes `deployedCapital` as `Σ positions.notional` instead of `nav × exposure.gross`, which was unreliable when exposure was mis-parsed; the row header now explicitly labels Symbol / Size / Avg / Last / P&L / Weight / Trend.
**Reason:** Auditing a paper run that showed one COHR fill plus 29 `pending` IBKR orders revealed three stacked issues: the allocator was re-emitting identical orders each loop (FUTY×8, FLMI×5, BFAM×4 duplicates); the Book row showed only `qty` with no notional, forcing the operator to compute size mentally; and the backend ships `gross_exposure` as absolute £ (e.g. `57919.88` against `nav=1055095.72`) which `parsePct()` mis-read as a percent and clipped to 100%, inflating Capital-at-work to nonsensical figures. Fixing all three at once restores operator trust in the Book view and eliminates the duplicate-order flood at the source.
**Status:** Implemented. Covered by `tests/test_execution_dedup.py` (5 cases — skip on in-flight, allow when clear, disable via env, DB-failure fall-through, no session-factory). UI `mapping.ts` helpers are plain functions unit-testable from Node; exposure normalization matches the same auto-detect rules used in `numFromPortfolio`.

---

## D027 — Wake the hunter: marketable limits, multi-asset-class routing, mode-aware cadence, forex coverage, admin cancel
**Date:** 2026-04-21
**Decision:** Five coupled changes that remove the structural reasons the system was placing one-a-day limit orders against a single asset class on a single broker:

1. **Marketable-limit rewrite at placement time.** `ExecutionEngine._apply_marketable_limit()` runs *after* `_build_order` and *before* `_normalize_order_for_broker`. It fetches live top-of-book via `broker.get_order_book(symbol, depth=1)` and rewrites the order's limit price to `ask × (1 + slip)` on BUY / `bid × (1 − slip)` on SELL. Falls back to `get_last_price` when the book is empty, then to the original `suggested_price` as last resort. Slippage buffer tunable via `EXECUTION_MARKETABLE_SLIP_BPS` (default `10` bps, `0` disables). Observability via `engine.marketable_adjusted` counter and a single `MARKETABLE LIMIT` info log per adjustment.

2. **Multi-asset-class strategies.** `Strategy.__init__` now consumes either the legacy scalar `asset_class` or the new list form `asset_classes: [equity, crypto, forex, future]` from YAML, exposing `supports_asset_class(ac)` that the loop uses as a gate before calling `generate_signal(symbol, df)`. Both `momentum_breakout` and `mean_reversion` declare `[equity, crypto, forex, future]`. The loop relabels every signal with `asset_class_for_symbol(symbol)` so `SmartOrderRouter` picks the venue per class (equity→ibkr/alpaca, crypto→binance/kraken, forex→ibkr).

3. **Mode-aware loop cadence.** `config/profile_modes.yaml` gains `loop_cadence_sec: {defender: 900, trader: 300, hunter: 120}`. `TradingLoop._load_mode_cadence_map()` validates + floors to 10s, and each iteration picks the current mode's cadence before `await asyncio.wait_for(stop_event, timeout=…)`. Mode switches take effect on the very next sleep — no restart needed. `ProfileModesConfig` in `config/models.py` was extended to accept the new key (pydantic `StrictBaseModel`).

4. **Forex + futures coverage.** `config/data_pipeline.yaml` seeds `EURUSD=X`, `GBPUSD=X`, `USDJPY=X`, `AUDUSD=X`, `USDCHF=X`, `USDCAD=X` for forex and `ES=F`, `NQ=F`, `YM=F`, `CL=F`, `GC=F`, `SI=F` for futures. New helpers `is_forex_symbol`, `is_futures_symbol`, `asset_class_for_symbol` classify them. `broker_symbol_for(symbol, broker)` strips the yfinance `=X` / `=F` suffix before the order reaches the broker, so `EURUSD=X` enters the IBKR adapter as `EURUSD` and maps cleanly to `Forex("EURUSD")`. Futures execution is **gated** behind `FUTURES_EXECUTION_ENABLED=0` (default off) until a contract-month resolver ships — data flows and signals are still logged/ranked for observability.

5. **Admin cancel-pending-orders.** `POST /admin/cancel_pending_orders` (optional `?broker=` filter, guarded by `X-Control-Token`). For every connected broker it calls `get_open_orders()` + `cancel_order()` per id, then bulk-updates the `orders` table: `WHERE status IN ('pending','open','partially_filled') → status='cancelled'`. Returns `{cancelled_by_broker, db_updated}`. Intended for one-shot use after execution-semantic changes (like this one) to clear the pre-change backlog and let the new dedup + marketable-limit logic start clean.

**Reason:** The diagnostic walk-through on 2026-04-21 showed a paper session with 218 orders in 12h, of which 1 (COHR) filled, 94 sat `pending` on IBKR at unmarketable bid prices, and every single signal was equity-on-IBKR even though binance/kraken/alpaca/bybit were connected. Root causes, in descending impact: (a) limit price was the 1h-bar close, never the current ask; (b) both signal strategies were hard-pinned to `asset_class: equity, preferred_broker: ibkr`; (c) `loop_interval_sec` was fixed at `120s` regardless of profile mode so "hunter" rotated the same as "defender"; (d) the universe excluded forex/futures entirely; (e) the pre-change stuck-order backlog kept re-blocking deduplication even after fixes. Shipping all five together is the only way to actually observe the hunter behaving like a hunter instead of a sleepy sniper at one equity price from an hour ago.

**Status:** Implemented. Covered by:
- `tests/test_execution_marketable_limits.py` (8 cases — buy bumps above ask, sell drops below bid, slip=0 disables, MARKET untouched, last-price fallback, no-reference preserves order, book exception tolerated, no-broker passthrough).
- `tests/test_asset_class_routing.py` (16 cases — symbol → asset-class classification for all 4 classes, strict forex/futures guards, `broker_symbol_for` passthrough + suffix stripping, multi-class strategy declaration, legacy single-class back-compat, empty-config default).
- `tests/test_mode_cadence.py` (7 cases — YAML load, missing block, invalid-entry filter, 10s minimum floor, `_read_active_mode` default, `_read_active_mode` JSON read, defender≥trader≥hunter invariant).
Full suite still green (276 passed, 3 skipped).

**Operator follow-ups (next cycle):**
- IBKR futures contract resolver (`ES=F` → `Future("ES", "<current-month>", "CME")`) to flip `FUTURES_EXECUTION_ENABLED=1`.
- Surface `marketable_adjusted` / `dedup_skipped` on `/system/status` so the UI can show "N orders priced-to-market / M deduped this hour".
- Treat stale pending orders > N minutes as auto-cancellable without operator interaction.

---

## D028 — Honest broker coverage: partial-NAV transparency + auto-disable on exclusion
**Date:** 2026-04-22
**Decision:** The aggregated NAV on the dashboard is now **coverage-aware**, and the risk engine auto-disables any broker that is not contributing to it. Three coordinated changes:

1. **Backend coverage contract.** `BrokerReport.coverage()` returns `{full, configured, included, excluded: [{name, connected, balance_ready, reason}]}`. `full` is true iff every configured broker is both connected and balance-ready — i.e. NAV truly reflects all wallets the operator asked for. `included` lists the brokers whose balances are in NAV right now; `excluded` carries the failing brokers with the concrete error from `BrokerStatus.error` (e.g. `"Startup connect deferred (transient exchange throttle/retry)"`). The orchestrator's `status()` exposes this as a top-level `coverage` key on `GET /system/status` (and therefore the WebSocket `tick.system` payload).

2. **Risk engine auto-sync.** A new orchestrator background task `_coverage_sync_loop` (tick `COVERAGE_SYNC_INTERVAL_SEC`, default 5s) diff-applies coverage transitions onto `RiskEngine._disabled_brokers`: every excluded broker gets `risk.disable_broker(name)`, every freshly-included broker gets `risk.enable_broker(name)`. This guarantees no new orders are routed to a broker whose position state is stale — the same gate used by the kill switch, so the existing `_check_broker_disabled` risk rule covers it with zero additional checks. Idempotent, cancellable, survives a missing risk engine (pre-loop phase) with a no-op cycle. Stopped cleanly in `Orchestrator.stop()`.

3. **UI "honest degrade".** `BrokerStatus.state` expanded from `live | warming | off` to `live | warming | offline | off`. `mapBrokers` now distinguishes a broker that is genuinely still connecting (no error, pill = `warming` / caution tone) from one that is down with a concrete failure (error present, pill = `offline` / danger tone with the backend's error surfaced on hover via `title`). The NAV card on the Dashboard renders an amber **"Partial NAV"** banner whenever `coverage.full === false`, naming the excluded brokers and exposing each one's reason on hover, plus a compact footnote `· partial NAV (excl. kraken)` next to the Tradable / Allocation chips. `useLiveSystem` exports a `coverage: Coverage` field so other screens (Book, Risk, Log) can reason about which venues are in the NAV.

**Reason:** On 2026-04-22 the system transitioned to `RUNNING` with IBKR showing a `warming` pill while its Gateway was actually not running at all — NAV read £98k instead of the real £1.05M because the aggregator had silently skipped the IBKR wallet, and the UI gave no indication that anything was wrong. The "warming" state conflated three meaningfully different backend conditions — genuinely connecting, connected but no balance yet, and configured-but-offline-with-an-error — and the orchestrator flipped to `RUNNING` the moment *any* broker was live, which is correct for trading availability but misleading for NAV interpretation. The right system behaviour is (a) always show an aggregated NAV for whatever wallets are currently trustworthy, (b) tell the operator explicitly that NAV is partial and which wallets are missing with their concrete reasons, and (c) refuse to route new orders to excluded brokers at the risk layer so partial coverage cannot drift into partial exposure.

**Rejected alternatives:** "Block `RUNNING` until every broker is up" (Option A): hides the working capital stack behind one broker's outage, which is exactly the opposite of the multi-venue architecture's value. "Kill-switch on partial coverage" (Option B): indistinguishable from a real emergency and would stop strategies on correctly-attributed wallets. Both failed the review because the real failure mode is *unknown* partial coverage, not partial coverage per se.

**Status:** Implemented. Covered by:
- `tests/test_broker_coverage.py` (10 cases — full coverage, partial-one-down, connected-but-no-balance-ready, unconfigured broker ignored, empty-configured is not "full", status dict shape with/without report, risk-engine disable on exclude, re-enable on recovery, no-risk-engine graceful no-op).
- Backend suite remains green (323 passed, 3 skipped).
- UI builds clean under Vite + TS strict.

**Operator follow-ups (next cycle):**
- Extend the coverage contract to the Risk screen: render an "excluded from NAV" chip on the Capital-at-work row when `coverage.full === false`.
- `POST /admin/retry_broker/{name}` to force an immediate reconnect attempt on an excluded broker instead of waiting for the background reconnect loop — operator can one-click recover after launching IB Gateway.

---

## D029 — Single canonical NAV aggregator (BASE-aware) + periodic heartbeat persistence
**Date:** 2026-04-22
**Decision:** The aggregated NAV is now computed in **exactly one place** and persisted on a cadence so shutdown can never lose it. Two coordinated changes:

1. **Single source of truth for live NAV.** `api/server.py::_live_portfolio_value()` used to duplicate (and subtly mis-implement) the trading-loop's broker aggregation: it took `max(balances)` per adapter, which for IBKR picked a single cash-currency line instead of the `BASE` row that carries `NetLiquidation`. The result was that `/pnl.today.portfolio_value` (which the UI NAV card reads) reported ~£884k while `/dashboard/snapshot.portfolio.nav` (built off the trading-loop aggregation) correctly reported ~£1,055k — a £170k phantom loss. `_live_portfolio_value()` now delegates to `system.portfolio_equity.live_portfolio_value()` — the canonical BASE-preferring helper that already backed the trading loop. One function, one behaviour, two callers.

2. **NAV heartbeat (periodic + on-shutdown).** Before this change, `daily_pnl` only received a row when a trade filled. A quiet trading day plus an ungraceful shutdown (OS kill, power loss, crash) could leave the DB with either no row for today or a stale one from yesterday, so the `/pnl` DB fallback had nothing fresh to show when brokers were slow to report balances post-restart. A new orchestrator background task `_nav_heartbeat_loop` (tick `NAV_HEARTBEAT_INTERVAL_SEC`, default 60s) calls `_upsert_daily_pnl` with the live BASE-aware equity every minute, and `Orchestrator.stop()` flushes one final heartbeat with a 10s timeout before disconnecting brokers. A tick that sees zero aggregated equity is a **no-op** — we never clobber a valid historical row with a spurious zero. `/pnl` also now falls back to the most recent persisted row (any date) if today's is missing, so the UI still shows a meaningful NAV during the all-brokers-still-connecting window after a restart.

**Reason:** On 2026-04-22 the operator reported NAV had "dropped by nearly £200,000 overnight" (£880k vs yesterday's £1M+). Root cause was entirely cosmetic — no capital had been lost; the UI was reading a buggy duplicate aggregator that understated IBKR's balance by ignoring the `BASE` NetLiquidation row. Secondary concern: "is it not secure to turn off the system?". It was safe (`daily_pnl` preserves yesterday's row on any shutdown) but the persistence cadence was fragile — tied to trade fills only — which created the exact class of "last persisted value is stale" failure modes that make honest NAV reporting impossible. Consolidating the aggregator and adding a heartbeat closes both gaps and makes the answer to "what is my NAV?" the same number from every code path.

**Rejected alternatives:** "Keep the two aggregators and add a test that they agree" — still ships two ways to make the same mistake. "Emit NAV only when exposure changes" — does not solve the empty-trading-day case that triggered the report.

**Status:** Implemented. Covered by:
- `tests/test_live_portfolio_value.py` (10 cases — zero/empty inputs, BASE preference over larger cash rows, BASE even when smaller than non-BASE, `max` fallback when no BASE row, per-adapter dedup, zero rows skipped, adapter exceptions swallowed, case-insensitive BASE match, API ↔ loop consistency).
- `tests/test_nav_heartbeat.py` (5 cases — upsert on non-zero equity, skip on zero equity, swallow DB errors, loop cancellable, idempotent start).
- Full backend suite: 344 passed, 3 skipped.
- Live reconciliation post-fix: `/pnl.today.portfolio_value` = `/dashboard/snapshot.portfolio.nav + today_unrealised` to the penny.

**Operator follow-ups (next cycle):**
- Consider surfacing `daily_pnl` as a TimescaleDB hypertable to make multi-year NAV history queries cheap.
- Surface "last NAV heartbeat" timestamp on the System screen so operators can spot a stuck heartbeat before it causes drift.

---

## D031 — NAV allowlist + live wins over stale `daily_pnl` (coverage / kill alignment)
**Date:** 2026-04-24
**Decision:** Two fixes that were stacking and making headline NAV "sticky" or inconsistent with broker coverage.

1. **`system.portfolio_equity.live_portfolio_value` must respect the same inclusion rules as the rest of the system.** It used to sum every object in `broker_manager.adapters` with no filter, so a venue could still add `NetLiquidation` after it was excluded from coverage (e.g. risk `disabled_brokers` after `coverage_sync_loop` or `POST /kill` with `brokers: ["ibkr"]`). The helper now:
   - includes only names in `BrokerManager.report.included_names` (connected + balance_ready), and
   - skips any name in `RiskEngine.disabled_brokers` (lowercased match).
   Stubs that omit `report` keep the previous "all adapters" behaviour for unit tests.

2. **`GET /pnl` headline `today.portfolio_value` no longer does `max(live, db, last_persisted, …)` when `live > 0`.** The `max` was added (D029) to avoid a drop to the `PORTFOLIO_VALUE` default while brokers reconnected, but it also pinned the UI to an old `daily_pnl` row that was written when an excluded broker still inflated the (pre-allowlist) live sum. If `live_value > 0` from the broker sum, that value is the display headline; `daily_pnl` is only used when `live_value` is still zero (first snapshot / cold path).

**Reason:** Operators saw ~£1.05M "forever" or numbers that ignored kill/coverage, because the UI floor and the adapter dict could both ignore exclusion.

**Status:** Implemented. `tests/test_live_portfolio_value.py` extended (allowlist + `disabled_brokers`); full suite green.

**Follow-up (IBKR single-currency rows):** If IB returns only `currency=USD` rows (no `BASE` line), `system.portfolio_equity` cannot disambiguate cash vs NAV. `brokers/ibkr/adapter.py` must pick **NetLiquidation** before **TotalCashValue** / **CashBalance** when building each `Balance.total` from account-summary tags; otherwise a USD cash line (~884K) wins over NetLiq (~1,055K). See `brokers/ibkr/adapter._total_from_account_summary_tags` and `tests/test_ibkr_summary_tags.py`.

**Follow-up (`run_m3._load_portfolio_state`):** The same `max(live, db)` anti-pattern lived in `run_m3._load_portfolio_state` (used by the NAV heartbeat and trading loop for `portfolio_state` / dashboard `nav`). It re-pinned stale `daily_pnl` into every snapshot and upsert, self-refreshing 884K. `run_m3._resolve_portfolio_value_for_state` now mirrors **GET /pnl**: when `fallback_portfolio_value` (live, post-allowlist) is **> 0**, it wins; DB is used only when live is still 0. See `tests/test_run_m3.py` (`test_resolve_portfolio_value_*`).

---

## D032 — `regime_strategy_gates` must list every live signal strategy
**Date:** 2026-04-24
**Decision:** `config/ai.yaml` `pipeline.regime_strategy_gates` lists, per `macro_regime` label, which `RawSignal.strategy` values survive `ai.regime.filter_by_allowed_strategies` in `system/trading_loop/loop.py` **before** `_pick_best_signal`. The previous lists only included `momentum_breakout` and/or `mean_reversion`, so **volume_flow**, **event_driven_news**, **pairs_trading**, **volatility_regime**, and **regime_rotation** were **dropped every tick** whenever the AI returned a known regime key — they looked “off” in the Strategy mix even though the loop and `config/strategies.yaml` had them enabled.

**Change:** All five regime keys use the same YAML anchor `default_signal_strategies` (seven names aligned with `strategies/*Strategy.name`). Operators who want a truly defensive sub-roster in `risk_off_stagflation` / `tightening` should **edit** that list rather than shipping an incomplete one by accident.

**Status:** Implemented. `tests/test_ai_pipeline.test_ai_yaml_regime_gates_lists_core_signal_strategies` guards drift.

---

## D033 — Multi-strategy candidates, `strategy_candidate_log`, coordinator per-symbol dedupe

**Date:** 2026-04-24
**Decision:** The batch (D015 / global-edge) path no longer calls `_pick_best_signal` before building the candidate set. For each symbol, every enabled strategy that returns a raw (or a logged skip) is visible: rows go to table `strategy_candidate_log` via `system/strategy_candidate_log.py`, separate from execution-path `SignalLog` (no change to meta_adaptation joins). The global-edge coordinator receives all `StrategyOpportunity` rows, then `dedupe_opportunities_by_symbol` keeps the highest `priority_score` per symbol before `propose_actions` (arbitrage sleeve names are excluded from same-symbol collapse). Legacy per-symbol mode still executes one signal per symbol but logs `lost_to_strategy` for non-winners. `event_driven_news` logs `ai_result_unavailable` when the AI cycle did not produce a result. API: `GET /diagnostics/strategy-candidates?since_hours=24`.

**Status:** Implemented. `tests/test_strategy_candidate_flow.py` covers dedupe.

**D033b (2026-04-24):** The redesign **Strategy mix** card consumes `GET /diagnostics/strategy-candidates?since_hours=24` (see `fetch_strategy_mix_diagnostics` in `system/strategy_candidate_log.py`): per-strategy counts, `last_evaluated_at` / `last_generated_at`, `top_skip_reason`, and a `lifecycle` key mapped in the UI (Scanning / Finding setups / Competing / Selected / Trading / Blocked by risk / Idle). “Idle” in the UI means **zero evaluation rows in the window**, not “no trade.” D015 non-global (allocator primary) now logs `selected_for_allocation` for each `ExecutionPlan` instruction and reuses the shared `_process_signal` `sc_log_buffer` for `risk_rejected` and `executed` with `metadata.path=d015`. Same-symbol coordinator dedupe rows use `reason=same_symbol_dedupe` and `metadata` `{winner_score, loser_score}`.

---

## D030 — Hunter must hunt: mode-aware capital fraction + broker-truth reconciliation

**Date:** 2026-04-22
**Decision:** Two tightly-coupled fixes addressing the "sleeping hunter" symptom — the system ran with hunter regime but deployed only ~6% of tradable capital while rejecting or parking the rest.

1. **Mode-aware `max_notional_fraction_per_action`.** `GlobalEdgeCoordinator` caps each emitted open action to `opp.capital_required × frac`. Prior to D030 `frac` was a single scalar (`0.15`) applied to every mode — so hunter (which wants to deploy aggressively) got the same 15% throttle as defender (which wants risk off). A strategy asking for £44,294 was trimmed to £6,644, exactly reproducing the observed 6.4% deployment. The config value is now either a scalar (legacy, preserved verbatim) **or** a dict keyed by mode. Defaults: `hunter: 1.00` (full strategy request), `trader: 0.50` (balanced), `defender: 0.15` (defensive, matches pre-D030 uniform behaviour). `min(1, frac)` clamp guards against accidental >100% blow-ups; malformed / unknown-mode values fall back to `0.15`. Lives alongside the already-mode-aware `max_actions_per_tick` — hunter now emits up to 10 actions × full-request capital per tick, which is what the mode was designed for.

2. **Reconciliation persists broker truth unconditionally.** `ExecutionEngine._reconcile_positions_internal` compared local `PositionLog` rows against each broker's `get_positions()` output and, on any quantity divergence, logged the mismatch and returned early *before* writing the fresh remote snapshot to the DB. The unintended effect: once the DB drifted (e.g. IBKR actually held 335 COHR while our DB said 164), every subsequent reconciliation noticed the gap, logged it, and did nothing — so `GlobalEdgeCoordinator.held` permanently consumed a stale view of our holdings and kept over-proposing new opens on top of risk we already had. The fix splits the comparison from the persistence: the loop collects *all* mismatches, ALWAYS persists the remote snapshot (the broker's books are ground truth for what we own), then returns `False` after persistence so upstream still sees the divergence signal and the opt-in `auto_kill_on_reconciliation_failure` hook still fires when enabled.

**Reason:** On 2026-04-22 the operator reported hunter was "sleeping again — only 6.3% of capital working". Investigation showed 4 compounding symptoms: 41 Alpaca rejections (`insufficient buying power [code=40310000]`), 33 IBKR limits sitting pending, 94 IBKR orders cancelled with zero fills, and COHR position reconciliation reporting `local_qty=164 remote_qty=335`. The two bugs above are the direct, mechanical causes of the deployment gap:
- *Allocator bug:* even when the loop generated valid opportunities, the coordinator silently deflated their requested capital by 85%, so any single tick could only deploy ~£26k of new risk on a £1.05M NAV.
- *Reconciliation bug:* the "held" input the coordinator used to rank new vs existing edges was stuck on a stale snapshot, which over time caused the system to either double-up (proposing opens for symbols we already held larger than we knew) or under-propose (if remote quantity grew). The snapshot drift also produced the £171k COHR discrepancy visible in the logs.

**Rejected alternatives:**
- *Raise the scalar `max_notional_fraction_per_action` to 1.0.* Fixes hunter but removes the risk-off brake on defender. The per-mode dict is the same amount of config with the correct semantics.
- *Force local DB to match broker by deleting mismatched rows.* Brittle and asymmetric — it also loses the mismatch signal to upstream. The cleaner contract is "broker is truth, always persist, log and `return False` so upstream can alert / auto-kill if configured".
- *Make the coordinator re-read broker positions directly instead of DB.* Bigger blast radius, couples allocator to broker I/O, and doesn't fix the UI which also reads `PositionLog`.

**Status:** Implemented. Covered by:
- `tests/test_global_edge_coordinator.py` — 6 new cases: scalar back-compat, mode-aware per-mode fractions, `min(1, frac)` clamp at >100%, unknown-mode → trader fallback, malformed → 0.15 default, missing-key → 0.15 default.
- `tests/test_execution_engine.py` — updated mismatch test asserts persistence happened; new `test_reconcile_persists_broker_truth_on_quantity_mismatch` exercises the local≠remote case explicitly (local qty=1, broker qty=2) and asserts the persisted row carries the broker's quantity.
- Config: `config/global_edge.yaml` ships the new dict form with hunter=1.00 / trader=0.50 / defender=0.15.

**Operator follow-ups (next cycle):**
- Per-broker buying-power-aware routing: even with the allocator deflation fix, we observed Alpaca rejecting ~£440k worth of orders sized against total NAV when only IBKR had room. Sizing at the execution layer should consult each broker's `get_balances()` and either re-route or size-down rather than bounce at the venue.
- Surface position-mismatch events to the System screen so operators can see the broker-truth refresh happen in real time (currently only visible via the error log).

---

## D031 — Respect strategy sizing: end of systematic over-sizing + sizing audit trail

**Date:** 2026-04-22
**Decision:** Five tightly-scoped fixes around the sizing pipeline in the global-edge path. Together they end a multi-week bug where every directional equity signal was silently deployed at `NAV × default_position_pct` (~5% of NAV, typically £50k on £1M NAV) regardless of what the strategy actually requested — producing a systematic 7–13× over-sizing of low/medium-conviction trades and, after D030 made the coordinator deploy its full action budget, large adverse P&L swings on any signal that did not immediately move in our favour.

1. **(D031A) Respect strategy sizing in `signal_candidate_to_strategy_opportunity`.** The D015 candidate → opportunity conversion ignored `candidate.metadata["risk_notional_override"]` and `candidate.metadata["target_notional"]` and instead set `capital_required = nav * position_pct`. This silently replaced the strategy's volatility-aware, conviction-weighted sizing (e.g. £750 for a weak FCOM mean-reversion probe at ATR 0.36%; £7,913 for a COHR momentum_breakout at ATR 1.8%) with a blanket fixed-size slug. The new sizing priority is explicit:
   1. `metadata["risk_notional_override"]` if present and > 0 (most specific signal-level override)
   2. else `metadata["target_notional"]` if present and > 0
   3. else `nav * position_pct` (legacy fallback for signals that carry no sizing metadata)

   A hard ceiling of `nav × max_position_pct` (default 0.10, from `config/risk_limits.yaml`) is applied AFTER the priority pick. The ceiling is a cap only — it never inflates a smaller strategy-requested size upwards. Under-requested sizes stay small; over-requested sizes get clipped and the clip is logged.

2. **(D031B) Sizing audit trail.** Every emitted `StrategyOpportunity` / `CoordinatorAction` now carries explicit sizing-provenance fields in its `metadata`: `sizing_source` (one of `risk_notional_override` | `target_notional` | `nav_fallback`), `sizing_strategy_target_notional`, `sizing_risk_notional_override`, `sizing_proposed_base_notional`, `sizing_hard_cap_notional`, `sizing_final_capital_required`, `sizing_clipped` (bool), `sizing_clip_reason`, `sizing_nav_at_decision`, `sizing_max_position_pct`, plus post-mode-fraction fields (`sizing_pre_mode_capital`, `sizing_mode`, `sizing_mode_fraction`, `sizing_final_action_capital`). `coordinator_action_to_raw_signal` preserves them into the `RawSignal` so they survive into the order placement path. This makes every sizing decision auditable from dashboard / logs / tests — one of the reasons D031 stayed invisible for as long as it did was that nothing in the logs ever said *why* a £750 idea had become a £10,000 order.

3. **(D031C) Execution-boundary sanity guard.** A new helper `ExecutionEngine._passes_sizing_boundary_guard` runs immediately before broker placement. It rejects any order whose `abs(quantity) * limit_price` exceeds the intended `sizing_final_capital_required` by more than 1.25× (configurable by editing the helper's `tolerance` constant) or exceeds the declared `sizing_hard_cap_notional`. Arbitrage legs are exempt (capital flows via different paths). Signals without sizing metadata (legacy path / external signals) pass through — the guard never fabricates a ceiling from nothing. This is a defensive backstop, not the primary sizing mechanism: it catches upstream bugs where quantity calculation drifts away from the coordinator's intent.

4. **(D031D) Oversized-held-position detection.** `held_positions_from_portfolio` now accepts `nav` and `max_position_pct`. When provided, each held position's live notional is compared against the ceiling and the result is written to metadata as `position_above_target_ratio` + `oversized_position_flag` (`True` when `ratio > oversize_flag_ratio`, default 1.25×). Detection only — no auto-liquidation. The flag is available for the dashboard to surface "this position is larger than it should be" warnings and for a future remediation task.

5. **(D031E) Stop-loss framework scaffold.** A new module `risk/stop_loss.py` exposes `evaluate_stop_loss(...) → StopLossDecision`, a pure function that given a position and NAV decides whether the position's per-trade loss budget (`nav * max_loss_per_trade_pct`) has been breached. Supports ATR-based structural stops when strategy metadata carries `stop_loss_atr` + (`atr` or `atr_pct`). Not yet wired to a runtime task — `risk/engine.py::_check_max_loss_per_trade_pct` remains the pre-open gate, and the post-open monitor is a scheduled follow-up. The scaffold is intentionally limited to freeze the decision logic so the wiring task cannot silently regress it.

**Reason:** On 2026-04-22 the operator asked "Why are we opening positions that go down and negatively effect our capital?" Investigation of two losing positions (COHR -£3,454, FCOM -£59) showed strategy metadata asked for £7,913 and £750 respectively, while the actual fills were £57,920 and £10,010 — over-sized by 7.3× and 13.3×. That over-sizing, not bad signals, was the mechanical cause of the adverse P&L magnitudes: at the strategy's intended sizing COHR's -2.9% drift would have cost ~£230 (not £3,454) and FCOM's -0.6% would have cost ~£4.50 (not £59). The bug violated the architecture's stated principle that sizing is a computed output respecting strategy intent (D015), not a uniform fixed slug applied by the allocator.

**Rejected alternatives:**
- *Patch the symptom at `coordinator_action_to_raw_signal`* (overwrite `target_notional` there). Rejected — moves the fix downstream of where the wrong size is decided and makes the audit trail lossy.
- *Lower `default_position_pct` to 0.01.* Rejected — shrinks everything uniformly (including legitimate high-conviction momentum breakouts) without restoring conviction-based scaling; also masks the underlying bug rather than fixing it.
- *Drop the hard cap entirely and trust strategies.* Rejected — a misconfigured strategy requesting 50% of NAV should still be clipped. The cap is a cheap safety net.
- *Enforce post-open stops in this same task.* Rejected — stop-loss enforcement is a separate surface (monitor cadence, close-order routing, idempotency) and mixing it into a sizing correction would bloat the blast radius. Scaffold now, wire in a dedicated follow-up.

**Status:** Implemented. Covered by:
- `tests/test_global_edge_coordinator.py` — 7 new cases: `target_notional` honoured, `risk_notional_override` wins, hard-cap clips absurd requests, `nav_fallback` preserved, small sizes never inflated, audit metadata completeness, arbitrage path unchanged, oversized held-position flag.
- `tests/test_execution_engine.py` — 5 new cases for the boundary guard: within-tolerance pass, gross over-sizing reject, hard-cap reject, no-metadata no-op, arbitrage exempt.
- `tests/test_stop_loss_scaffold.py` — 5 new cases for the pure `evaluate_stop_loss` helper: portfolio stop triggers / stays quiet, ATR-pct structural stop, short-position structural stop, invalid-price safe default.
- `system/trading_loop/loop.py` threads `max_position_pct` (read from risk-engine config, default 0.10) into both the opportunity builder and the held-position builder.
- No config schema changes required — all new behaviour derives from existing keys (`max_position_pct` in `config/risk_limits.yaml`).

**Expected behaviour after D031** (on the same £1M NAV, £7,913 COHR breakout, £750 FCOM mean-reversion):
- COHR opens at £7,913 not £57,920. A 1-ATR adverse move (~1.8%) loses ~£142 instead of ~£1,040.
- FCOM opens at £750 not £10,010. A 0.6% adverse drift loses ~£4.50 instead of ~£59.
- A hypothetical mis-configured strategy requesting £500k is clipped to £100k (10% NAV cap) and the clip is logged with reason `nav*0.10`.
- Existing oversized positions are flagged (`oversized_position_flag=True`) on every tick but NOT auto-trimmed; operator or a follow-up remediation task decides how to unwind.

**Important scope limitation: strategies do not yet emit per-signal target notional.**
The D031 audit trail (verified against live DB post-deploy) consistently records `sizing_source=nav_fallback` for directional signals because `strategies/momentum.py` and `strategies/mean_reversion.py` currently emit RawSignal metadata with `atr_pct`, `breakout_strength` etc. but NOT `target_notional` or `risk_notional_override`. The values we saw in earlier signal rows (e.g. COHR `target_notional=7913`, FCOM `target_notional=750`) came from the coordinator's own self-loop: `coordinator_action_to_raw_signal` writes `md["target_notional"] = str(action.capital)`, which on a subsequent accumulator round-trip can appear as if a strategy had "requested" that number. It did not.

Consequence: D031A as implemented strictly follows the brief (respect explicit metadata when present, fall back otherwise) but its *practical* effect on existing strategy sizes is (a) enforce the `nav * max_position_pct` hard cap on the nav-fraction baseline, and (b) make every sizing decision auditable. The *expected* win from "respect strategy intent" only materialises once a strategy actually emits intent — see D032 below.

**Operator follow-ups (next cycle):**
- **D032 — Strategies emit per-signal target notional.** Modify `strategies/momentum.py` and `strategies/mean_reversion.py` (and any other directional strategy) to populate `RawSignal.metadata["target_notional"]` based on the strategy's own conviction (confidence) and volatility (ATR%). Without this, the D031A priority path (step 1/2) never fires. Likely shape: a per-strategy `base_notional_usd` config × confidence scalar × volatility scalar, with the coordinator applying the hard cap on top. This is the change that actually turns D031's plumbing into the 7-13× sizing reduction the user expected.
- Wire `evaluate_stop_loss` into a 5–30 s monitor task in `system/orchestrator.py` (similar to the D029 NAV heartbeat) that closes positions whose loss exceeds budget.
- Surface `oversized_position_flag` on the Positions dashboard panel with a one-click "trim to target" action.
- Add confidence-aware scaling of the strategy target (confidence 0.85 → 1.3×, confidence 0.40 → 0.6×) so that hunter's lower confidence threshold doesn't translate to the same per-trade size as trader's higher bar.
- **Current oversized positions:** on the live paper book at deploy time, COHR sat at ~£118k (≈11% of £1.05M NAV) — marginally above the 10% hard cap — and is now flagged by D031D (`oversized_position_flag=True`). FCOM (£10k ≈ 1% NAV) and FIX (pre-existing IBKR manual position) are within cap. Per D031D semantics no auto-liquidation happens; operator decides whether to trim manually or wait for the D032 stop-loss wiring.

---

## D032 — Strategy emits explicit per-signal target notional

**Date:** 2026-04-22
**Decision:** Directional strategies now emit an explicit absolute `target_notional` in `RawSignal.metadata`, so the D031 sizing priority path (`risk_notional_override` > `target_notional` > `nav_fallback`) can use genuine strategy intent rather than defaulting to NAV fallback.

Implemented in:
- `strategies/momentum.py`
- `strategies/mean_reversion.py`
- `config/strategies.yaml` (`base_target_notional` for both strategies)

Each strategy now computes target size from:
1. `base_target_notional` (default 5000)
2. confidence scale (bounded 0.75x..1.25x)
3. ATR%-based volatility scale (bounded 0.70x..1.30x)
4. final clamp to 0.50x..1.50x of base notional

The emitted metadata fields are:
- `target_notional`
- `sizing_base_notional`
- `sizing_confidence_scale`
- `sizing_volatility_scale`
- `sizing_intent_source=strategy_confidence_volatility`

**Reason:** D031 fixed coordinator/execution plumbing but directional strategies still emitted no explicit target size, causing universal `sizing_source=nav_fallback`. That made the boundary guard protect against oversizing, but did not restore strategy-level notional intent. D032 makes the intent explicit at the source.

**Status:** Implemented and covered by tests:
- `tests/test_strategies.py` now asserts both strategies emit `target_notional` and validates ATR-aware scaling behavior.
- Existing D031/D031C suites remain green.

**Operational impact:** This removes the last structural reason for bulk sizing-guard rejects caused by mismatched intent vs computed quantity. Sizing is now strategy-owned, auditable, and still capped by downstream hard risk ceilings.

---

## D033 — Runtime post-open stop-loss monitor (D031E wired)

**Date:** 2026-04-22
**Decision:** Wire `risk/stop_loss.py::evaluate_stop_loss` into a dedicated orchestrator background task that runs every 5-30s (default 15s) and submits `reduce_only` close signals for positions breaching either:
- portfolio loss budget (`max_loss_per_trade_pct`), or
- structural stop metadata (`stop_loss_atr` + `atr`/`atr_pct`).

Implementation details:
- New orchestrator task lifecycle:
  - starts on `Orchestrator.start()`
  - cancels on `Orchestrator.stop()`
- Close path is **not** a risk bypass:
  - builds a `RiskSignal(strategy="stop_loss_monitor", reduce_only=True)`
  - runs `RiskEngine.evaluate_and_persist(...)`
  - only then routes to `ExecutionEngine.execute(...)`
- Added per-position close cooldown (`STOP_LOSS_CLOSE_COOLDOWN_SEC`, default 60s) to prevent repeated close spam while fills/reconciliation settle.

**Reason:** D031E delivered pure decision logic but left runtime enforcement pending. Without a monitor, `max_loss_per_trade_pct` only gates new entries and does not protect already-open positions intraday.

**Status:** Implemented in `system/orchestrator.py` with tests in `tests/test_stop_loss_monitor.py` (close on breach, no-op when within budget, loop cancellation). Full suite green.

---

## D034 — Strategy expansion wave: event-driven + pairs + volume/flow

**Date:** 2026-04-22
**Decision:** Expand the live strategy roster with three new modules wired into the existing `RawSignal -> SignalEngine -> RiskEngine -> ExecutionEngine` contract:
- `event_driven_news`: creates directional signals from AI/news shock context (`news_scores`, `news_details`, macro confidence).
- `pairs_trading`: creates relative-value signals from configured pair spreads using rolling hedge-ratio spread z-scores.
- `volume_flow`: creates continuation/exhaustion signals from volume anomaly + bar-return behavior.

Integration constraints kept:
- No risk bypasses.
- Strategy sizing intent stays metadata-driven (`target_notional`, scaling diagnostics).
- New strategies are YAML-gated in `config/strategies.yaml` and can be toggled via existing control flows.

**Reason:** The allocator/risk architecture was already mature, but alpha diversity lagged. This adds high-ROI edges (event, relative value, flow) without cross-layer coupling or broker interface changes.

**Status:** Implemented in `system/trading_loop/loop.py`, `strategies/event_driven.py`, `strategies/pairs_trading.py`, `strategies/volume_flow.py`, with coverage in `tests/test_strategies.py`.

---

## D035 — Demand-engine gating + volatility/regime/meta wave

**Date:** 2026-04-22
**Decision:** Introduce a global `DemandEngine` (`system/demand_engine.py`) that computes a bounded demand score from AI news/macro and cross-asset anchor returns, then apply it in three places: (1) strategy emission context (`regime_rotation`), (2) pre-allocation meta-label filtering (`signals/meta_labeler.py`) for candidate/raw signals, and (3) opportunity/coordinator ranking bias (`signals/opportunity_engine.py` demand multiplier + `portfolio/global_edge_coordinator.py` demand-adjusted regime-fit). Add two strategy modules: `volatility_regime` (ATR regime breakout/compression) and `regime_rotation` (risk-on/off proxy rotation).

**Reason:** Move from signal-only architecture to opportunity-driven behavior with an explicit latent-demand variable that influences both candidate quality and allocation ranking across D015 and global-edge paths.

**Status:** Implemented in loop/config/strategies with tests in `tests/test_strategies.py` and `tests/test_demand_meta.py`.

---

## D036 — Wave 3: cross-asset demand graph + volatility overlay + ML-style meta-labeling

**Date:** 2026-04-22
**Decision:** Add an explicit cross-asset demand graph module (`system/cross_asset_demand_graph.py`) and feed it into `DemandEngine` as a first-class component. Upgrade meta-labeling (`signals/meta_labeler.py`) from threshold-only rules to a feature-scored probability gate (sigmoid over confidence, demand alignment, volume/news features, strategy prior bias). Add a portfolio-level volatility overlay in `portfolio/allocation_engine.py` that scales gross exposure target using market-volatility proxies from regime metadata.

**Reason:** Improve robustness in regime transitions by (1) extracting demand from structured cross-asset relationships, (2) selecting higher-quality candidates probabilistically rather than by hard cuts, and (3) reducing capital deployment during volatility shocks at allocator level.

**Status:** Implemented with coverage in `tests/test_demand_meta.py` and `tests/test_allocation_vol_overlay.py`.

---

## D037 — Wave 4: demand telemetry + mode-calibrated meta-labeling + demand urgency planning

**Date:** 2026-04-22
**Decision:** Extend demand-awareness into observability and execution: (1) publish demand telemetry (`score`, `trend`, confidence, cross-asset components) via runner heartbeat and dashboard snapshots (`d015` and `global_edge` payloads), (2) support per-profile-mode calibration in meta-labeling (`defender/trader/hunter` probability thresholds), and (3) apply demand-conditioned urgency multipliers in `execution/planner.py` so aligned opens/increases are prioritized while countertrend expansion is de-prioritized.

**Reason:** Keep allocator/execution behavior aligned with the same latent demand state and make that state visible to operators in both control-tower and heartbeat channels.

**Status:** Implemented in `system/trading_loop/loop.py`, `system/dashboard_publish.py`, `signals/meta_labeler.py`, `execution/planner.py`, with tests in `tests/test_demand_meta.py` and `tests/test_execution_planner_demand_urgency.py`.

---

## D038 — Wave 5: demand-aware routing + adaptive meta priors + demand alerts

**Date:** 2026-04-22
**Decision:** Extend demand-awareness to execution venue selection and online strategy priors: `SmartOrderRouter.route(...)` now accepts optional metadata and applies demand/profile-aware venue preference for crypto/equity paths; trading loop periodically computes dynamic strategy bias from recent order outcomes (`signals/meta_adaptation.py`) and merges it into meta-label strategy priors; demand regime-shift alerts are emitted into heartbeat and dashboard snapshot payloads.

**Reason:** Improve realized execution quality and adapt signal acceptance to live fill behavior without bypassing risk or changing frozen broker interfaces.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `signals/meta_adaptation.py`, `system/dashboard_publish.py`, with tests in `tests/test_meta_adaptation.py` and `tests/test_router_demand_bias.py`.

---

## D039 — Wave 6: learned routing feedback + mode-adaptive demand thresholds + UI diagnostics

**Date:** 2026-04-22
**Decision:** Add a lightweight learned routing-quality map in `SmartOrderRouter` keyed by `(broker, symbol)`, updated from realized execution feedback (filled vs non-filled and slippage proxy) to influence future broker ranking. Extend demand-alert gating with per-mode thresholds (`defender/trader/hunter`) and persist short alert history in heartbeat/snapshot payloads. Surface demand/meta diagnostics in redesign Risk screen from runtime heartbeat and dashboard snapshot.

**Reason:** Close the loop between execution outcomes and routing choice, adapt demand sensitivity to operating mode, and make latent-state adaptation observable to the operator.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `config/strategies.yaml`, `ui/src/app/redesign/useLiveSystem.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_demand_bias.py` and `tests/test_demand_meta.py`.

---

## D040 — Wave 7: persistent routing trajectories + mode-adaptive alerts + diagnostics endpoint

**Date:** 2026-04-22
**Decision:** Persist learned routing quality state (`quality_map` + per-symbol short history) into control-state (`routing.quality.state`) and reload it on loop startup; apply configurable decay policy each N iterations to prevent stale overfit. Extend demand alerts with mode-aware thresholds and publish alert history in heartbeat/snapshot payloads. Add dedicated diagnostics API endpoint `/diagnostics/routing-quality` and wire redesign UI to show routing trajectories alongside demand/meta diagnostics.

**Reason:** Make routing learning durable across restarts, keep signal/reactivity mode-consistent, and give operators direct visibility into execution-learning dynamics.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `api/server.py`, `ui/src/app/redesign/useLiveSystem.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_demand_bias.py` and `tests/test_api_dashboard_extras.py`.

---

## D041 — Wave 8: routing confidence intervals + adaptive decay + trajectory sparklines

**Date:** 2026-04-22
**Decision:** Extend routing learning with broker-symbol confidence diagnostics and adaptive decay mechanics: routing export now includes per-broker sample stats (`n`, `std`, `ci95_half`) and decay can adapt to observation count, turnover/liquidity EMA proxies, and staleness. Trading loop now passes turnover/liquidity hints into routing feedback. Diagnostics payload/type expanded and redesign Risk diagnostics render per-symbol trajectory sparklines with CI95.

**Reason:** Raw point scores are insufficient for operator trust and can overfit stale sparse samples; confidence-aware routing telemetry and adaptive forgetting make the learning loop safer and more interpretable.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `ui/src/app/lib/api.ts`, `ui/src/app/redesign/screens.tsx`, with tests updated in `tests/test_router_demand_bias.py`.

---

## D042 — Wave 9: fee-prior fusion + slippage percentiles + broker comparison diagnostics

**Date:** 2026-04-22
**Decision:** Extend `SmartOrderRouter` with (1) a Bayesian-style **fused routing score** that blends a fee-derived prior (`ROUTING_PRIOR_PSEUDO_N` pseudo-observations) with online learned quality using observation count `n`, used as the secondary sort key after explicit fee; (2) persistent per-(broker, symbol) **execution sidecar** metrics: rolling absolute slippage samples (bounded window) with exported **p50/p90 slippage bps** and **fill rate**; (3) structured export fields `broker_comparison` and `exec_metrics` alongside existing `quality_map` / `quality_stats` / `history` in `routing.quality.state`; (4) `/diagnostics/routing-quality` returns the full persisted blob including `quality_stats` (previously omitted); (5) redesign Risk diagnostics **broker comparison table** plus fused-aware “best venue” selection for the trajectory column.

**Reason:** Sparse feedback should not dominate venue choice until evidence accumulates; operators need comparable slippage tail risk and fill reliability next to CI-aware scores; the diagnostics API should mirror what the loop persists so the UI and external clients stay consistent.

**Status:** Implemented in `execution/router.py`, `api/server.py`, `ui/src/app/lib/api.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_wave9.py` plus updates to `tests/test_router_demand_bias.py` and `tests/test_api_dashboard_extras.py`.

---

## D043 — Strategy mix: default roster + live intelligence sparklines

**Date:** 2026-04-22
**Decision:** The redesign Strategy mix grid always seeds the canonical signal + arbitrage roster (matching `TradingLoop` / `config/strategies.yaml`) via `DEFAULT_STRATEGY_MIX_ROSTER` in `ui/src/app/redesign/mapping.ts`, merged with `loaded_strategies` and allocator snapshot weights. Per-strategy sparklines use recent confidences from `/intelligence/signals` (`intelligenceSparkForStrategy`); allocator-active rows without a DB trace use a lightweight synthetic series from mix weight. `GET /system/status` responses without `loaded_strategies` clear client roster state so off-mode does not show stale loop registrations.

**Reason:** Operators saw an empty Strategy mix before the first allocator publish; the taxonomy roster and signal history should be visible whenever the dashboard loads.

**Status:** Implemented in `ui/src/app/redesign/mapping.ts`, `ui/src/app/redesign/screens.tsx`, `ui/src/app/redesign/data.ts`, `ui/src/app/redesign/useLiveSystem.ts`.

---

## D044 — Dashboard capital allocation: hybrid slider (raise commits · lower stages)

**Date:** 2026-04-23
**Decision:** The redesign dashboard grows a dedicated, full-width **Capital allocation** panel (`ui/src/app/redesign/capital.tsx`) mounted between the NAV hero and the conviction/live-feed row. Interaction is asymmetric by design:

- **Dragging up past the deployed line commits the ceiling on release** via `PUT /system/capital-allocation` (`live.setCapitalPct`) — new-position headroom expansions are low-risk and don't warrant a confirm step.
- **Dragging below the deployed line stages a trim** with a weakest-first preview (ascending unrealised P&L as the hold-score proxy), a per-symbol protect list, and an explicit *Confirm* that lowers the ceiling. The preview is honest: the engine unwinds on its own signals — this is **not** a force-close. Per-symbol close lives in Book.
- **Dragging below `FLATTEN_THRESHOLD` (3%) opens the flatten confirm** with a 1.2s hold-to-confirm. Until `POST /positions/flatten` ships, confirm lowers the ceiling to 0% (prevents new deploys) and surfaces a "backend pending" banner. No fake success.

`useLiveSystem.setCapitalPct` was hardened to **revert optimistic local state on failure** and to adopt the server-confirmed value when the backend clamps or rejects; `CapitalPanel` relies on this contract to suppress the "committed" banner when the PUT never took effect. Three keyframes were added to `src/styles/design-system.css`: `ds-tick-flash` (deployed-line crossing), `ds-danger-pulse` (flatten thumb halo), `ds-slide-up` (confirm/result banners). The panel is always mounted — even when the system is off — so the ceiling can be pre-set and will be honoured on next start.

The Kill Switch control that ships alongside the slider in the design bundle was deliberately **not** wired: the scope was the slider only, and the top-bar Power control already provides the graceful halt path.

**Reason:** Operators need a single, discoverable surface for "raise the cap now" and "reduce my book" that (a) distinguishes the safe case (raise headroom, commit immediately) from the consequential case (trim or flatten, explicit confirm with preview) and (b) is truthful about which backend actions are available today — a mis-labelled "flatten" button that silently lowers the ceiling would erode trust. The ledger contract for `PUT /system/capital-allocation` (idempotent, clamped, returns confirmed value) already supports the UI's optimistic-with-revert model.

**Status:** Implemented in `ui/src/app/redesign/capital.tsx` (new), `ui/src/app/redesign/dashboard.tsx` (mount + grid row), `ui/src/app/redesign/useLiveSystem.ts` (hardened `setCapitalPct`), `ui/src/styles/design-system.css` (three new keyframes). Sourced from `ui/newui/project/prototypes/redesign_capital_port/capital.tsx`; `KillSwitchButton` and inlined `CAPITAL_KEYFRAMES` export deliberately omitted.

### D044.1 — Gauge on *capital at work*, not positions-only

Follow-up to the initial D044 shipment: the slider originally drew its landmark line and computed "free to deploy" against **filled positions only**, while the Book screen's *Capital at work* card showed **positions + pending orders**. Operators saw two different percentages for the same underlying book (e.g. 46.3% on the slider vs 49.2% in the Book card) — a ~3pp gap that is exactly the reserved notional of unfilled orders.

The backend's `cap_slider` gates `deploy = NAV × ge × cap_slider` in `portfolio/allocation_engine.py`, and *deploy* in that context covers both new positions AND the buy orders feeding them (because a pending order has already consumed allocator budget). The slider must therefore gauge against **capital at work = filled positions + pending-order notional**, or the gauge is dishonest: the snap landmark sits in the wrong place, "free to deploy" overstates headroom by the pending amount, and the "raise the ceiling to match" mental model doesn't match what the allocator actually does.

Fix landed as a single shared helper `capitalAtWork(positions, orders)` in `ui/src/app/redesign/mapping.ts` (alongside `isPendingOrderStatus` / `pendingOrderNotional`). Both surfaces — the dashboard slider and the Book screen — consume this helper, so the two can never drift again. In the slider, the tick label changes from `deployed · N%` to `at work · N%`; `IdleInfo` now shows **At work** as the headline row with an optional `positions £X / pending £Y` sub-row when pending is materially non-zero. `computeTrim` also shifts its `mustRelease` baseline to `workingValue` so the over-commitment figure honestly includes pending-order notional — the close list remains positions-only (pending orders unwind via cancel / engine signals, not via this UI), and the existing shortage banner already narrates that gap.

Duplicated `isPendingOrder` / `toFiniteNumber` in `screens.tsx` were removed in favour of the shared helpers. No backend changes.

---

## D045 — Trading-engine audit: sizing guards, crypto venue routing, and trim exits

**Date:** 2026-04-25
**Decision:** Fix the global-edge/D015 execution path so missing feature prices never fall back to `1`, and global-edge opportunities carry the resolved `close`/`price` and `side` metadata into `SignalEngine`. Route spot crypto to dedicated crypto venues ahead of Alpaca unless explicitly opted in with `allow_alpaca_crypto`; fiat `*-USD` pairs are pinned to Kraken unless explicitly opted into stablecoin conversion, while USDT/USDC-style symbols can use Binance/Bybit. Enable global-edge replacement exits by emitting reduce-only `trim_symbol` actions for displaced held positions; close/trim actions preserve the position's broker and still pass through the normal risk and execution engines.

**Reason:** The audit found repeated broker rejections caused by target notional being interpreted as coin/share quantity (for example ~55k BTC/ETH/XRP units) when price metadata was missing. It also found Alpaca being selected for crypto because of the zero-fee prior despite no Alpaca USD buying power, and found the global-edge path ranking held positions without emitting any close/trim action, which left profitable or weak holdings unrealisable except by stop-loss/manual intervention.

**Status:** Implemented in `system/trading_loop/loop.py`, `portfolio/global_edge_coordinator.py`, `signals/arb_bridge.py`, `execution/d015_instruction_executor.py`, `execution/router.py`, and `config/global_edge.yaml`, with regression coverage in `tests/test_global_edge_coordinator.py`, `tests/test_d015_instruction_executor.py`, and `tests/test_router_demand_bias.py`. Live verification after restart showed a normal-sized `AMTM` reduce-only sell trim and no new 55k-unit crypto orders; IBKR was excluded during verification because Gateway/TWS was in an API zombie state.

---

## D046 — Kraken/Binance/Bybit paper book + global-edge churn guard

**Date:** 2026-04-26
**Decision:** (1) **Position reconciliation** — adapters in `_NO_NATIVE_PAPER_POSITION_BROKERS` (`kraken`, `binance`, `bybit`) do not host exchange-native paper positions. In `paper_mode`, reconciliation must treat the latest synthetic `PositionLog` snapshot for that broker as authoritative instead of replacing it with the live account’s empty spot book (which made the allocator think the book was flat after every fill). When merging per-broker latest rows into the local quantity map, skip double-counting rows already included in the global latest-timestamp snapshot (`b_ts == latest_ts`). (2) **Global-edge coordinator** — skip emitting `open_strategy` when an opportunity’s symbol and **side** already match a held position (instant paper fills made “open same leg every loop” too easy). Opposite-side opportunities are not blocked so trims/flips can still flow through risk and execution.

**Reason:** Operators saw repetitive Telegram `PAPER OPEN FILLED` lines with identical size/price: real synthetic orders were logged, but reconciliation cleared paper crypto legs while strategies kept re-proposing the same edge; the coordinator could also add the same-direction leg again because trim selection skips same-symbol displacement.

**Status:** Implemented in `execution/engine.py`, `core/broker_paper.py` (shared broker set), `portfolio/global_edge_coordinator.py`, and `GET /positions` now merges the latest `PositionLog` rows for `kraken` / `binance` / `bybit` whenever `APP_ENV` is not `live` and live adapters returned at least one row (`source=live_broker+synthetic_paper_log`), so the Book matches the persisted synthetic crypto leg. Tests in `tests/test_global_edge_coordinator.py`.

---

## D047 — Broker reconnect readiness bypass + immediate late-broker ingestion

**Date:** 2026-04-27
**Decision:** Keep the broker reconnect loop as the single retry mechanism for every configured registered broker, but make IBKR readiness edge-triggered: a cheap TCP/API probe runs while disconnected, and a transition from not-ready to ready bypasses any outstanding exponential full-connect backoff. The probe does not increment the full-connect failure counter, so a closed TWS/Gateway cannot push reconnects into multi-minute delay before the operator launches it. Separately, `TradingLoop` now runs a lightweight `broker-join-poll` (`BROKER_JOIN_POLL_SEC`, default 2s) so newly connected adapters are added to the router and execution engine without waiting for the next full strategy iteration.

**Reason:** IBKR/TWS is locally operated and frequently starts after the trading system. A prior failed full connect could leave IBKR offline until a long backoff expired, and even after connection the trading loop could take up to the strategy cadence to include it. The system should pick up a launched/restarted API client within the next health/join polls while still respecting backoff for expensive remote exchange auth retries.

**Status:** Implemented in `system/broker_manager.py`, `system/trading_loop/loop.py`, with regression coverage in `tests/test_broker_balance_ready.py`.

**Operational note:** IB Gateway can show "API Server connected" while refusing all third-party clients until the paper-trading disclaimer is accepted. Gateway logs this as "Paper trading disclaimer must first be accepted for API connection" and the UI row remains "API Client disconnected". Broker status now points operators at that prompt before treating the condition as a generic zombie/restart case.

---

## D049 — Strategy dashboard roster hygiene and advanced sleeve cards

**Date:** 2026-04-27
**Decision:** Keep the redesign Strategy screen as an alpha-strategy roster, not a catch-all runtime event list. `DEFAULT_STRATEGY_MIX_ROSTER` now includes the advanced strategy families that belong in this view (`factor_sleeve`, `stat_arb_pairs`, `options_long_call`, `options_long_put`, `options_protective_put`, `options_covered_call`) so disabled or idle sleeves are visible as explicit cards. Internal allocator maintenance actions (`global_edge_trim`, `trim_symbol`) are filtered out of strategy mapping and mix rows; they belong in allocation, portfolio-rotation, and execution observability, not the strategy taxonomy.

**Reason:** Operators need to see which strategy sleeves exist, which are disabled, and which are idle. Showing `global_edge_trim` as a strategy made the Strategy screen misleading because it is an allocator/execution action generated by D015 replacement logic, not a source of alpha.

**Status:** Implemented in `ui/src/app/redesign/mapping.ts` and `ui/src/app/redesign/screens.tsx`, with guard coverage in `tests/test_ui_strategy_roster.py`. Focused pytest checks and the Vite production build pass.

### D049.1 — Paper mode should exercise the advanced strategy sleeves

Follow-up: the Strategy dashboard is used during paper mode, so advanced
strategy sleeves should not appear as disabled merely because they are not yet
production-approved. `config/factor_sleeve.yaml`, `config/pairs_trading.yaml`,
and `config/options_strategies.yaml` now ship enabled for paper observability;
options remain `paper_only: true` and still pass through the options risk policy.
The redesign fallback roster mirrors that stance so a fresh dashboard render does
not show pink disabled badges before `/system/status` catches up.

`Idle` still has meaning: the sleeve can be enabled and evaluated but have no
setup, no eligible holding, no option proposal/chain data, no qualifying pair, or
no recent feature window. That is different from a config-disabled strategy and
is the correct paper-mode distinction.

---

## D051 — Correlation-aware Universe Intelligence Layer

**Date:** 2026-04-27
**Decision:** Add a standalone `universe/` package that compresses a large
candidate world into non-redundant tiers for the existing dynamic data/trading
loop. The layer performs eligibility filtering, return/correlation graph
construction, factor-similarity blending, connected-component clustering,
representative selection, and temporary promotion of cold instruments on
anomalies. It writes the existing compact contract
`data/runtime/universe_tiers.json` plus richer research metadata in
`data/runtime/universe_intelligence.json`.

**Reason:** Breadth should be measured as independent opportunity coverage per
unit of compute, not raw instrument count. Correlated instruments should not be
deleted: one or more representatives cover directional exposure, while cluster
members remain available for relative-value and event-driven promotion.

**Status:** Implemented in `universe/`, `config/universe_selection.yaml`,
`scripts/build_universe_tiers.py`, and `docs/UNIVERSE_INTELLIGENCE.md`, with
coverage in `tests/test_universe_intelligence.py`. No risk/execution path was
changed.

---

## D061 — Adaptive sizing rewrite (supersedes D015 / D030 / D031 / D032)

**Date:** 2026-04-30
**Decision:** Remove discretionary hard-coded numerical knobs from the sizing
pipeline (per-strategy notionals, integer action caps, per-action notional
fraction, flat per-position percentage ceiling). Replace them with a single
adaptive coordinator path that:

1. Filters opportunities by the existing displacement gate
   (`expected_edge > weakest_held_edge + edge_advantage(mode)`) and the
   churn / already-held / dedup rules — unchanged from D015.
2. Allocates capital across qualifying opportunities via softmax weights
   `w_i ∝ exp(λ_eff · priority_score_i)` where
   `λ_eff = softmax_lambda · concentration_exponent(mode)`.
3. Sizes each `CoordinatorAction` as `gross_target_capital * w_i` where
   `gross_target_capital = tradable_capital * gross_fraction(mode)` and
   `tradable_capital = NAV * capital_pct` is the operator's slider.
4. Enforces no fixed integer cap on the number of emitted opens — Hunter
   may emit 1 (winner-take-all) or 50 (broad book) depending purely on how
   many opps clear the displacement gate.
5. Reads per-position / concentration / asset-class ceilings from
   `config/risk_limits.yaml::mode_overrides[active_mode]` so Hunter can
   take 100% of the deployable sleeve in one symbol when the edge
   dominates; Defender keeps 20% / Trader 40%.
6. Cancels working orders and forces a fresh plan on the next tick whenever
   the operator moves the capital slider (orchestrator publishes
   `capital_allocation_changed`; loop drains working orders before
   re-running the iteration body against the new `tradable`).

**Reason:** Operator intent: "no hard-coded values — the slider is the only
operator-set capital cap; size, count, concentration, cadence emerge from
market state". Pre-D061 the system was bottlenecked by:

* `base_target_notional: 5000` (and per-mode 25k) on every strategy, which
  caused tiny $5–7k positions even when NAV was $1.07M.
* `max_position_pct: 0.10` flat cap, preventing Hunter from concentrating
  on a dominating opportunity.
* `max_actions_per_tick.hunter: 20` integer cap forcing the coordinator to
  slice gross target into N equal slots even when one opp deserved most.
* Static `liquidity_score=0.7 / execution_score=0.75 / risk_cost=0.05`
  literals in `signal_candidate_to_strategy_opportunity` flattening the
  priority softmax so concentration could not emerge.

**Status:** Implemented behind `USE_ADAPTIVE_SIZING=1` so the legacy path
remains intact. Touched files:

* `portfolio/global_edge_coordinator.py` — `_adaptive_priority_components`,
  `_propose_actions_adaptive`, env-flag gating; static stubs replaced at
  all four call sites (directional + 3 arb wrappers).
* `system/trading_loop/loop.py` — adaptive kwargs passed to
  `propose_actions`; per-mode `max_position_pct` from `mode_overrides`;
  slider event handler cancels working orders before next iter.
* `system/trading_loop/helpers.py` — `apply_saved_mode_to_risk_cfg`
  applies `mode_overrides` from `risk_limits.yaml`.
* `system/trading_loop/loop.py` `_capital_change_pending` flag plumbed via
  `request_iteration("capital_allocation_changed")`.
* `config/risk_limits.yaml` — `mode_overrides` block (defender / trader /
  hunter ceilings; hunter = 1.00 across the board).
* Tests: `tests/test_adaptive_sizing.py` (12 cases — priority components,
  no-integer-cap, dominant-opportunity-100%, capital-sums-to-target,
  legacy-fallback, audit metadata, already-held skip).

**Supersedes:** D015 (allocator) — concentration is now softmax-driven, no
fixed `max_actions_per_tick`. D030 (mode-aware capital fraction) — the
per-mode `max_notional_fraction_per_action` is bypassed; `gross_fraction(mode)`
applied at the loop / coordinator boundary. D031 (respect strategy sizing) —
strategies' `target_notional` is no longer the source of truth; the coordinator
sizes from gross_target × softmax_weight. D032 (per-signal `target_notional`
field) — the metadata field is preserved for audit but not used for sizing
decisions in the adaptive path.

**Migration:** When `USE_ADAPTIVE_SIZING` is unset (default), every legacy
behaviour is preserved bit-for-bit (verified: 50 pre-D061 tests pass
unchanged). To enable, set `USE_ADAPTIVE_SIZING=1` before launching
`python run.py`. To disable mid-flight, unset and restart the Python
process — `/system/stop` + `/system/start` does not reload module-level
imports.

## D106 — Two-agent accounting collision: restored proven state (2026-05-18)
A second agent (Cursor) made overlapping, uncommitted changes to the
accounting subsystem while this session's fixes were in flight:
`system/paper_nav.py` (compounded-paper-NAV model), a `run_m3.py`
rewrite that replaced `_compute_today_realised_pnl`, an `/pnl` NAV-path
swap, and it ran `scripts/backfill_daily_pnl.py` which **recomputed**
pre-2026-05-13 `daily_pnl` from the buggy-fee-era orders — directly
overwriting the operator's explicit, recorded decision (D-prior:
"flag pre-instrumentation as non-production and ZERO it"; recompute was
explicitly rejected).

Resolution (operator-chosen): keep ONE coherent, tested model.
- Cursor's uncommitted work preserved in `git stash@{0}` (recoverable,
  not adopted) — working tree restored to committed HEAD `630b337`.
- DB rectification re-applied (`scripts/rectify_daily_pnl.py --apply`):
  pre-05-13 re-zeroed; all-time realised = valid post-instrumentation
  days only. Cursor's recomputed state also backed up.
- Cherry-picked Cursor's two correct, non-conflicting fixes only:
  `trade_count` now counts filled `OrderLog` rows (was `SignalLog`,
  ~2x overcount); UI "Trades today" → "Fills today".
- NAV remains the synthetic-crypto-wallet + single-source `/pnl` model
  (`04d736a`/`6b3fd56`), NOT the compounded-paper model.

Governance note: autonomous agents must not run DB-mutating backfills
that reverse a recorded operator data decision without re-confirmation.

## D107 — Connect Hub plugin fabric and capability registry (2026-05-19)

myTbot now has a first Connect Hub foundation for adaptive onboarding across
four external dependency classes: brokers, information feeds, AI providers, and
treasury accounts.

Decision:
- External systems are declared in non-secret connector manifests
  (`config/connectors.yaml`) with category, auth type, required environment
  variables, roles, capabilities, and safety constraints.
- The runtime exposes a read-only Connect Hub snapshot via `GET /connect/hub`
  and embeds the same payload in `GET /system/status` as `connect_hub`.
- The redesign UI has a Connect screen that renders the same adaptive
  connector inventory, including next actions such as missing env vars,
  pipeline runs, broker start, or treasury approval requirements.
- Connector cards expose a guarded Configure wizard that writes declared
  credential env vars to `.env`, never echoes secret values, and can enable the
  connector/provider configuration where applicable.
- The snapshot adapts to the current user setup: broker rows merge live
  `BrokerManager` status, information-feed rows merge ingest telemetry,
  AI-provider rows merge `config/ai.yaml`, and treasury rows remain disabled
  unless explicitly configured.
- Secret values are never returned. The API reports only whether each required
  environment variable is configured.
- Treasury movement is deliberately metadata-only at this stage. A connector may
  declare future capabilities, but automatic transfer execution remains disabled
  by policy and requires a later approval workflow before any cash movement code
  exists.

Reason:
The one-button core must not assume that every operator has the same broker,
news stack, AI stack, or treasury account. The system should scale down to one
broker/no treasury/no paid AI and scale up to multiple venues, feeds, local and
paid LLMs, and governed treasury funding without changing allocator, risk, or
execution logic.

Status:
Implemented in `system/connect_hub.py`, `connectors/base.py`,
`config/connectors.yaml`, `api/server.py`, `docs/CONNECT_HUB.md`, redesign UI
route/screen files, and UI API types in `ui/src/app/lib/api.ts`, with focused coverage in
`tests/test_connect_hub.py`, `tests/test_connector_contracts.py`, and
`tests/test_api_dashboard_extras.py`. This is a read-only/onboarding inventory
slice; OAuth flows, generic unknown-protocol adapters, and treasury execution
are future work.

## D107 — Broker/market operating hours as a first-class decision input (2026-05-19)
Previously the market-session gate (`core/market_session.is_market_open`)
acted ONLY at the execution last-mile: the strategy/allocator selected
positions blind to venue hours, then orders bounced at `execute()`. This
wasted cycles, distorted allocation (un-tradeable names chosen over
tradeable ones), and made the harvest/stop monitors re-attempt closed-
market closes every cycle (the pre-market "389× did not execute" spam;
winners sat undefended).

Now broker/market hours are a first-class decision input:
- `config/market_hours.yaml` — declarative per-venue session policy
  (`always` for 24/7 crypto venues; `by_asset_class` otherwise).
- `core/market_session.is_tradeable(broker, asset_class, symbol)` — the
  single broker-aware authority. The proven `is_market_open` asset-class
  gate is left byte-identical (foundation is purely additive).
- Wired upstream: (a) the D015 allocator drops closed-venue opportunities
  BEFORE allocation; (b) profit-harvest / stop-loss / aggregate-de-risk
  monitors skip closed-venue positions quietly (DEBUG) instead of
  spamming failed closes — they re-evaluate automatically at reopen;
  (c) the `execute()` gate upgraded to the same broker-aware authority
  (defence-in-depth, unchanged role).

Honest scope: this is an efficiency/correctness/clarity change (capital
only allocated to currently-tradeable instruments; no pre-market spam),
NOT a profitability change. `MARKET_SESSION_GATE=0` disables; absent/
invalid YAML → built-in defaults (backward-safe).

## D124 — Auto-training embedded in orchestrator (2026-05-21)

**Decision:** Move daily auto-training from a standalone Windows
scheduled task into the orchestrator as a background coroutine.
Delete the `scripts/install_auto_training_task.ps1` installer and
unregister the existing `mytbot-auto-training` Task Scheduler entry.

**Why.** D123 installed the Windows task that
`config/auto_training.yaml` had been declaring for weeks without
anyone noticing it was never actually registered — that silent drift
is exactly the failure mode the one-button principle exists to
prevent. The separate scheduled task also:
- broke "`python run.py` starts everything" (CLAUDE.md rule),
- was invisible to `/system/status` and the UI,
- required re-installation on every new machine
  (`docs/NEW_MACHINE_SETUP.md`),
- and gave no real isolation benefit since the same `.venv` + `.env`
  was loaded anyway.

The original isolation arguments (crash containment, missed-run
recovery, ability to run while trading is OFF) are addressed by
running training as a subprocess from the orchestrator coroutine:
`asyncio.create_subprocess_exec(sys.executable,
"scripts/auto_train_models.py")`. A training crash dies in the
subprocess and the orchestrator logs the non-zero exit code; the
trading loop is unaffected.

**What changed.**

- `system/orchestrator.py`: new `_start_auto_training_loop` /
  `_auto_training_loop` / `_auto_training_tick` /
  `_run_auto_training_job` / `_resolve_auto_training_config` /
  `_persist_auto_training_last_run` /
  `_load_persisted_auto_training_last_run`. Wakes once per minute,
  checks `config/auto_training.yaml::auto_training.{enabled,
  schedule.start_time_local, timezone}`, fires once per local day
  after the configured time. Last-run timestamp persists to
  `ControlState` under `auto_training.last_run_at` (new key
  `Orchestrator.AUTO_TRAINING_STATE_KEY`) so a restart cannot
  trigger duplicate runs.
- `config/auto_training.yaml`: `schedule.windows_task_name` removed
  (no longer relevant); comment block updated to describe the
  embedded scheduler.
- `scripts/install_auto_training_task.ps1`: deleted.
- Windows Task Scheduler `mytbot-auto-training`: unregistered.
- `tests/test_auto_training_scheduler.py`: 8 unit tests covering
  enabled-flag gating, before/after scheduled time, prior-run-today
  vs prior-run-yesterday, subprocess-already-running guard, and YAML
  resolver.

**Operational notes.**

- The trading loop starts the scheduler in `start()` right after
  `_start_zero_alloc_flatten_watchdog()` and cancels it in `stop()`.
- The scheduler stays alive across `/system/stop` → `/system/start`
  cycles only if the trading loop itself stays up; this matches
  every other background task. If the operator wants training to
  run even while trading is OFF, leave `python run.py` alive (the
  scheduler runs inside the orchestrator process, not the trading
  loop, so it survives ON/OFF toggles of the trading loop).
- New-machine setup no longer requires installing a scheduled
  task; updating `docs/NEW_MACHINE_SETUP.md` to drop that step is
  a follow-up.

**Status:** Implemented. Restart `python run.py` to activate.

---

## D123 — Meta-labeler v0.2.0: dedup fix + 30-day retrain + auto-training task (2026-05-21)

**Decision:** Retrain `mytbot_meta_labeler` as v0.2.0 with two construction
fixes and install the daily auto-training scheduled task.

**Why.** A 12-hour live sample (3,672 candidates) showed v0.1.0 placed 67%
of candidates in calibration bin 0.25–0.30 (3.2% historical hit rate) and
only 1.6% above 0.40 — pinning live capital deployment at ~45% despite
the operator's 100% slider. Audit traced this to two defects, neither
of which is a D122 dynamic-threshold tuning issue:

1. **Feature duplication.** `scripts/build_meta_label_dataset.py` wrote
   both `news_score` and `accumulator_score` into the v0.1.0 CSV. At
   build time `sig.news_score` was `None`, the script fell back to
   `md["ai_news_score"]`, and that field was being populated from the
   accumulator's own AI-news rollup — so the two columns were
   byte-identical (`mean=0.3167, std=0.2176` for both). The logistic
   regression's effective weight on that signal was doubled.
2. **Stale, narrow window.** v0.1.0 was built 2026-04-27 from 3,679
   rows dominated by `mean_reversion`. Live distribution since has
   broadened to momentum/volume/volatility/event/regime/pairs, and
   `accumulator_score` has drifted from training mean +0.317 to live
   mean ~−0.014 — a 1.5σ shift that alone moves logreg predictions
   from ~0.42 to ~0.30.

**What changed.**

- `scripts/build_meta_label_dataset.py`: `news_score` removed from
  `FEATURE_COLUMNS`. With `sig.news_score` only (no `md` fallback),
  the live correlation between `news_score` and `accumulator_score`
  is still 0.967 — independence cannot be guaranteed today, so the
  column is dropped entirely. Accumulator carries the news
  information. The column can return in v0.3.0 when an independent
  point-in-time AI-news source is wired in.
- Fresh dataset: `data/research/meta_label/20260521_meta_label_v0_2_0`
  — 13,215 leakage-safe rows from 22,622 signal-log rows over the
  prior 30 days × 1,697,248 feature-snapshot rows.
- Artefact: `artifacts/models/meta_label/mytbot_meta_labeler-0.2.0.pkl`
  (logreg + Platt, 5-fold purged CV, embargo 10 bars). New
  `feature_contract_hash`:
  `e1d439adc21b8a120b22186b5f79a7261389e4155ad15254eb42df0ccbb8d9d6`.
- `config/model_registry.yaml`: v0.2.0 registered at
  `approval_status: paper`, `calibration_table` populated from the
  held-out 30% temporal-split OOS bins with `n≥100`. D122 reads
  this table directly.
- `config/meta_labeler.yaml`: `model_version: 0.2.0`,
  `artifact_path` updated. Rollback is single-key.
- Windows scheduled task `mytbot-auto-training` registered
  (daily 03:20 local, runs `scripts/auto_train_models.py` via venv
  python). Verified `State=Ready`; `config/auto_training.yaml`
  was declaring the cadence but the task itself had never been
  installed prior to this commit.
- Validation report:
  `reports/models/mytbot_meta_labeler/0.2.0/validation.md`.

**OOS results (temporal 70/30 split).** Brier 0.221 ≤ 0.25 (pass).
High-confidence lift: v0.2.0 best populated bin (predicted 0.758, n=93)
delivers observed 0.763 — vs v0.1.0 best populated bin (predicted
0.618, n=93) observed 0.419, on the same test slice. D122
simulation: at `target_win_rate=0.42`, v0.2.0 deploys ~95% of
candidates against a calibrated bin (predicted 0.228, observed 0.456,
n=136), where v0.1.0's equivalent threshold (0.330) lands on a noise
spike in a bin with no reliable mid-band signal.

**Known caveats (paper-soak monitoring).**

- Train→test base-rate drift (0.41 → 0.20) in the last 9 days drives
  high OOS ECE (0.259). The 30-day window straddles a regime shift.
- Mid-band calibration (predicted 0.43–0.62) over-predicts. D122
  `target_floor=0.20` keeps the operational gate below this band;
  do not raise the floor above 0.45 without a fresh calibration.
- Trainer convergence warnings (lbfgs hit `max_iter=400` on all 5
  CV folds + final fit) — features are not standardised in
  `models/meta_label/train.py::_make_classifier`. Out of scope for
  v0.2.0; flag for a future trainer-side StandardScaler step.

**Non-changes.** D122 dynamic threshold resolver and its config are
untouched. `target_floor` was not lowered (per non-goal: do not mask
calibration evidence). v0.1.0's row remains in the registry for
rollback; promotion to micro_live/live blocked until ≥14 days of paper
soak per `docs/MODEL_GOVERNANCE.md`.

**Status:** Implemented and live on the trading loop after restart.
Verification gate during paper soak: live candidate probability
histogram should center near ~0.43 (training mean) rather than v0.1.0's
~0.29; deployment % should organically climb toward 70–85% in neutral
regime at 100% slider. If deployment remains <60% in neutral regime,
escalate as a data-quality investigation (drift, not threshold-tuning).

---

## D118 — Self-tuning priority pre-filter + 6-stage universe funnel (2026-05-19)

The funnel between "every unique normalized symbol from connected brokers
+ the instrument registry" and "the small set we actually score with
yfinance" was a problem on three axes:

1. **It was opaque.** The operator-visible funnel jumped from
   `broker_listings` → `eligible` → `watching` → `active_reps` with no
   visibility into how we narrowed ~16k unique symbols down to the
   ~400 we sent to yfinance.
2. **It had hidden randomness.** `_stratified_sample_candidates` did a
   deterministic but effectively stratified-random pull weighted by
   broker tier — there was no causal rule the user could read.
3. **It still relied on hard-coded knobs.** The scoring budget (320),
   the eligible-vs-pinned cutoffs, and any "weights" that would be
   added later (liquidity, freshness, asset-class balance) were all
   tunable numbers in YAML. The operator demanded none of this be
   manually set.

**Decision.** Replace stratified-random candidate selection with a
deterministic, self-tuning rule that scores **every** unique
normalized symbol fast (microseconds per symbol; no I/O), picks the
top-N by score, and self-adjusts both the weights of the score
components and N itself based on observed outcomes. There are **no
operator-tunable numbers** for either the weights or the budget — only
master kill switches and (fixed, not-tunable) safety bounds in code.

**Four-stage funnel** (`universe/snapshot_service.py::_build_d118_funnel`):

1. `unique_normalized` — every unique broker-listed symbol + every
   instrument registry symbol after canonicalisation/deduplication.
   The raw broker-listing count is shown as a debug tooltip but is
   **not** a stage (the user explicitly rejected the 31k row).
2. `scored` — top-N priority pick **and** yfinance liquidity scoring in
   one pipeline pass. The self-tuning budget N and any timeout gap
   (`budget_attempted` vs count) are exposed on `scored.meta`.
3. `watching` — `core + scan` tiers from `universe_tiers.json`.
   Temporary anomaly promotions (`promoted_now`) are metadata on this
   stage, not a separate funnel step — they overlap scan/light and are
   not a filter between watching and active reps.
4. `active_reps` — non-redundant correlation representatives from
   universe intelligence clustering.

**Priority rule** (`data/universe_prefilter.py::compute_priority_scores`):

A symbol's priority is a weighted sum of six component subscores in
`[0, 1]`:
- `liquidity_prior` — registry liquidity bucket, fallback heuristics
  for crypto / FX / ETFs.
- `anchor_pin` — 1.0 for `UniverseManager.INITIAL_UNIVERSE` plus the
  IBKR curated seed, 0.0 otherwise. Anchors are pinned post-rank.
- `freshness_bonus` — decays from 1.0 (never scored) to ~0.0 (scored
  in the last 60s); promotes coverage of rarely-touched symbols.
- `registry_availability` — score is 1.0 when at least one broker has
  the symbol available, 0.0 when registry says unknown.
- `asset_class_balance` — boost when the current `unique_normalized`
  set is light in this asset class.
- `region_balance` — same idea for region.

**Self-tuning weights**
(`data/universe_weight_learner.py::WeightLearner`):

Online logistic regression with AdaGrad and EWMA decay. After every
pipeline cycle, each picked symbol is labelled by "did it actually
enter `watching` this cycle?". Weights are updated to maximise that
labelled likelihood, clamped to `[0.05, 0.50]` per component
(safety bounds, not tunable), then re-normalised to sum to 1.0. State
persisted atomically to `data/runtime/universe_weights.json`.

**Self-tuning budget**
(`data/universe_budget_controller.py::BudgetController`):

Two control laws run on every cycle and the smaller of the two values
wins:
- **AIMD throughput control** — measured cycle wall-time vs the
  scoring interval. If we ran in less than the configured throughput
  share we add `+25` (additive increase); if we overran we multiply by
  `0.75` (multiplicative decrease).
- **Utility saturation detection** — track `max_watching_rank` (the
  deepest priority-ranked index that made it into `watching`). If for
  several cycles all watching members sat in the top `0.6 * budget`
  ranks, the marginal symbol scored is not yielding new watching
  members; we shrink toward that observed cap.
A hard `[budget_floor, budget_ceiling]` (200–800 by default; safety
bounds, not tunable knobs) clamps the result. The currently *binding*
constraint (`aimd_grow` / `aimd_shrink` / `utility_saturation` /
`floor` / `ceiling` / `stable`) is exposed in the UI so the operator
can see *why* the budget moved. State persisted atomically to
`data/runtime/universe_budget.json`.

**Tier-transition stream**
(`data/universe_transitions.py::TransitionBuffer`):

Every cycle, the new vs previous tier maps are diffed and the changes
are appended to a ring buffer (default 500 events) with
`(ts, symbol, from_tier, to_tier, reason, score_delta)`. Reasons
include `promoted_to_watching`, `demoted_to_light`,
`promoted_to_active_reps`, `entered_unique_normalized`,
`removed_from_universe`. State persisted atomically to
`data/runtime/universe_transitions.json`.

**Per-symbol score-age telemetry**
(`data/universe_score_ages.py::ScoreAges`):

For every unique normalized symbol we keep
`(last_scored_at, last_score, score_count, first_seen_at)` with
atomic JSON persistence and a hard cap on tracked size with a
deterministic LRU eviction (unscored first, then oldest scored).
`/intelligence/universe` returns the per-symbol last-score timestamp,
and the Instruments tab renders a coloured age stripe.

**Wiring.** `_pipeline_runner` in `system/orchestrator.py` now:
1. Loads `ScoreAges`, `WeightLearner`, `BudgetController`, and the
   previous tier snapshot.
2. Computes `priority_scores` over the full `unique_normalized` set.
3. Calls `BudgetController.compute_next_budget()` for `N`.
4. Calls `UniverseBuilder.build_tiered_universe(...,
   priority_scores=..., target_budget=N, anchors=...,
   telemetry=BuildTelemetry())`.
5. After the build: updates `ScoreAges` from the telemetry, updates
   `WeightLearner` from
   `build_training_rows(picks_breakdowns, watching_now)`, observes a
   `CycleObservation` into `BudgetController`, diffs old vs new tiers
   and records transitions. All state is persisted atomically.

**Config surface** (`config/data_pipeline.yaml::dynamic_universe.ranking.priority_score`):
```
priority_score:
  enabled: true
  weight_learning_enabled: true
  budget_self_tune_enabled: true
  state_dir: data/runtime
```
Only master kill switches and a state directory. Disabling any switch
falls back to uniform weights or a fixed budget; the entire `enabled:
false` path is the legacy `_stratified_sample_candidates` behaviour
unchanged.

**Backward compatibility.** When the priority rule is disabled (or
when no scores are passed in), the builder still runs
`_stratified_sample_candidates` exactly as before. Anchors from
`UniverseManager.INITIAL_UNIVERSE` continue to be pinned. The risk
engine, order routing, and broker availability are untouched: D118 is
a *discovery-layer* control only.

**Status:** Implemented (`data/universe_score_ages.py`,
`data/universe_prefilter.py`, `data/universe_weight_learner.py`,
`data/universe_budget_controller.py`, `data/universe_transitions.py`,
`data/universe_builder.py`, `system/orchestrator.py`,
`universe/snapshot_service.py`, `ui/src/app/lib/api.ts`,
`ui/src/app/redesign/universe/UniverseScreen.tsx`,
`config/data_pipeline.yaml`). Tests:
`tests/test_universe_score_ages.py`,
`tests/test_universe_prefilter.py`,
`tests/test_universe_weight_learner.py`,
`tests/test_universe_budget_controller.py`,
`tests/test_universe_transitions.py`,
`tests/test_universe_builder_priority_selection.py`,
`tests/test_universe_snapshot_d118.py`.

## D117 — Adaptive universe-tier sizing (regime + signal pressure + cluster count + anti-churn) (2026-05-19)

The pre-D117 universe funnel used three hard-coded caps in
`config/data_pipeline.yaml::dynamic_universe` (`max_symbols=300`,
`ranking.core_max=50`, `ranking.scan_max=250`,
`ranking.max_candidates_to_score=400`). These were identical in every
market state — risk-on, risk-off, low-vol, crash — and identical
regardless of how many high-conviction candidates the allocator was
actually finding. That had two costs:

1. We spent the same yfinance/feature-ingest budget in calm and noisy
   periods, even when the allocator had no place to deploy it.
2. We had no automatic widening when more idiosyncratic correlation
   clusters appeared (more independent bets available), and no
   focusing when the active cluster count collapsed.

**Decision.** Add `universe/adaptive_caps.py` — a pure decision module
that takes
`(regime_state, signal_pressure, active_cluster_count, config_bounds)`
and returns the resolved caps `(candidates, watching, core, scan)`,
clamped to YAML-declared min/max bounds. Each axis contributes a
multiplier:

- **Regime axis** (`config/data_pipeline.yaml::dynamic_universe.adaptive.regime`):
  `risk_on=1.25`, `trend_up=1.15`, `volatile=1.30` (more places to
  fish), `mixed=1.00`, `range=0.90`, `risk_off=0.80`, `crash=0.65`,
  `insufficient_data=1.00`.
- **Signal-pressure axis** (`adaptive.signal_pressure`): the recent
  `dashboard_feed.batch_candidate_count` is read from the persisted
  `dashboard.snapshot`. When ≥ `high_threshold` (default 8) the scan
  tier widens by `1.20`; when ≤ `low_threshold` (default 2) it narrows
  to `0.80`.
- **Cluster-aware floor** (`adaptive.cluster_aware`): when an honest
  correlation-cluster count is available
  (`data/runtime/universe_intelligence.json::clusters`), watching is
  lifted to at least `max(watching_min_floor, watching_min_factor *
  active_reps)`, then clamped to the bounds. Default: 150 floor, 3.0
  factor — so 88 active clusters gets `max(150, 264)=264` watching.
- **Anti-churn hysteresis** (`adaptive.churn`): a symbol that was in
  the previous build's watchlist but missing this build is *graced*
  into the scan tier for up to `min_consecutive_drops` (default 3)
  consecutive rebuilds before it actually drops to light. Prevents
  single liquidity blips from churning feature ingest.

Bounds enforce a hard ceiling and floor per tier
(`candidates 200-800`, `watching 150-600`, `core 25-100`,
`scan 75-500` by default). Invariants are also enforced post-resolve:
`core <= watching` and `scan + core <= candidates`.

**Wiring.**

- `_pipeline_runner` in `system/orchestrator.py`: each tick now reads
  the persisted dashboard snapshot, builds an `AdaptiveCapsContext`
  via `universe.adaptive_context.build_adaptive_caps_context`,
  resolves caps via `compute_adaptive_caps`, applies them through
  `UniverseBuilder.update_caps(...)`, then calls the existing
  `build_tiered_universe`. After the build, `apply_churn_hysteresis`
  re-includes graced symbols and re-persists the tier file. Resolved
  caps + miss counter + grace history land in
  `data/runtime/universe_adaptive_state.json`.
- `universe/snapshot_service.py::_pipeline_caps()` overlays the
  resolved caps so `/intelligence/universe` shows the active values
  the builder actually used, not the static YAML anchor.
- `/intelligence/universe` now returns an `adaptive` block with the
  resolved caps, base anchor, composite multiplier, per-axis
  multipliers, cluster-floor flag, and reasons. The Universe
  dashboard renders an `adaptive Nx ↑ widen / ↓ focus / · neutral`
  badge with a tooltip listing reasons.

**Backward compatibility.** Set `dynamic_universe.adaptive.enabled:
false` and the resolved caps equal the base caps unconditionally,
matching pre-D117 behaviour exactly. Missing or malformed YAML, missing
dashboard snapshot, missing intelligence file → neutral context →
multiplier 1.0. The risk engine, order routing, and broker availability
are untouched: D117 is a *discovery-layer* control only.

**Status:** Implemented (`universe/adaptive_caps.py`,
`universe/adaptive_context.py`, `universe/adaptive_state.py`,
`config/data_pipeline.yaml::dynamic_universe.adaptive`,
`data/universe_builder.py::UniverseBuilder.update_caps()`,
`system/orchestrator.py::_pipeline_runner`,
`universe/snapshot_service.py`, `ui/src/app/redesign/universe/UniverseScreen.tsx`,
`ui/src/app/lib/api.ts`). Tests:
`tests/test_universe_adaptive_caps.py` — 26 tests covering disabled
fallback, each regime label, signal-pressure axis, bounds clamping,
cluster-aware floor (lift, max-clamp, no-shrink, disabled),
hysteresis grace (extend, drop-after-N, reset, no-previous-state),
config loader (good/missing/malformed YAML), runtime state round-trip,
and post-resolve invariants.

## D116 — Instrument Registry + Cross-Broker Availability Resolver (2026-05-19)

The hand-maintained per-broker symbol lists (especially IBKR's 61-line
curated YAML seed) were the binding constraint on universe coverage and
were not adapting when new brokers were added. The fix is a self-updating
master instrument table sourced from public maintained references, with
per-broker availability tracked separately.

Decision:

- A new instrument registry layer is introduced as a strictly read/observe
  consumer; it does not participate in any signal, risk, or order path.
  Schema (Postgres):
    * `instrument_registry` — canonical master (yfinance-style symbol PK,
      asset class, region, exchange, currency, sector/industry, ISIN,
      FIGI, first/last/refreshed seen timestamps, retired_at, metadata)
    * `instrument_source_membership` — one row per `(canonical_symbol,
      source_id)` with `source_version`, `external_id`, `last_seen_at`,
      `consecutive_miss_count`, `metadata`
    * `instrument_broker_availability` — one row per `(canonical_symbol,
      broker)` with status in `{unknown, available, unavailable,
      requires_qualification, blocked}`, last-checked timestamps, IBKR
      qualification payload
    * `instrument_source_runs` — audit log of every refresh
  Migration: `alembic/versions/d116a1b2c3d4_instrument_registry.py`.

- Canonical symbol module `instruments/canonical.py` centralises broker
  ↔ canonical translation. `data/universe_builder.py::_to_yf_symbol`
  becomes a thin wrapper.

- Source adapters (`instruments/sources/`) cover four families:
    * `wikipedia.py` (S&P 500 / 400 / 600, Nasdaq-100, Dow 30, FTSE,
      DAX, CAC, Euro Stoxx, Nikkei, TOPIX Core 30, Hang Seng, ASX 200,
      TSX 60 — ~20 indices)
    * `ishares.py` (~40 broad/sector/bond/commodity iShares ETF holdings
      via public CSV endpoints)
    * `openfigi.py` (bulk ISIN/FIGI + alternate-ticker enrichment via
      the OpenFIGI v3 mapping API)
    * `static_fx.py`, `static_futures.py` (G10 FX pairs + CME futures
      roots)
    * `broker_catalog.py` wraps `BrokerAdapter.get_supported_symbols()`
      for every connected adapter — this is how crypto exchanges feed
      the registry.
  All HTTP is funnelled through `instruments/sources/http.py`, a polite
  client with User-Agent, per-host rate limiting, ETag/Last-Modified
  caching, retry with jitter, and timeouts. Each source is fault-
  isolated; one failure cannot taint other sources.

- `instruments/availability.py::resolve_broker_availability(broker,
  adapter)` walks the canonical registry, attempts broker-side
  translation (via `instruments.canonical`), and writes a per-broker
  status row. IBKR uses both the broker catalog and the
  `brokers/ibkr/qualification.py` cache; symbols with no qualification
  record yet are marked `requires_qualification` rather than
  `unavailable`. Operator-pinned and operator-excluded symbols come from
  `config/instrument_registry.yaml::overrides` and become `blocked` or
  `available` regardless of catalog state.

- `instruments/builder.py` orchestrates refresh + availability. Retire
  policy: a symbol is `retired_at` only after
  `consecutive_miss_count >= min_consecutive_misses` (default 5) across
  at least `min_sources_missing` independent sources (default 2). No
  symbol is ever deleted.

- `instruments/scheduler.py` runs four background tasks at boot:
  constituents refresh, broker availability resolution, OpenFIGI
  enrichment, and a connect-event consumer. The scheduler subscribes to
  `BrokerManager.register_connect_callback`, so reconnecting a broker
  (or wiring up a new one) automatically re-evaluates availability for
  every canonical symbol on that broker. `system/orchestrator.py` owns
  the scheduler lifecycle and shuts it down cleanly on stop.

- `brokers/ibkr/adapter.py::get_supported_symbols()` now returns the
  union of (a) the curated YAML seed, (b) the IBKR qualification cache,
  and (c) the D116 registry's `available`/`requires_qualification`
  IBKR rows. Behaviour is gated by
  `IBKR_SUPPORTED_SYMBOLS_USE_REGISTRY` env var or
  `config/instrument_registry.yaml::ibkr_supported_symbols_use_registry`
  feature flag, defaulting to off until the registry is populated.
  Failures fall back silently to the curated YAML — IBKR's effective
  symbol list can only grow, never shrink. `place_order()` still calls
  `qualifyContractsAsync` before submission regardless of source.

- `universe/snapshot_service.py` adds `registry_known_count` and
  `registry_covered_count` per broker to the existing
  `coverage.by_broker` dashboard payload, alongside the broker-catalog
  funnel that drives the headline numbers.

- API additions (read-only): `GET /intelligence/instruments` (summary
  counts by asset class / region / source / broker availability), `GET
  /intelligence/instruments/{canonical}` (registry row + per-broker
  availability + source memberships), `GET /intelligence/instrument-
  sources` (recent run health per source).

- CLI: `python scripts/build_instrument_registry.py --sources=all
  --dry-run` for manual / scheduled refresh; `python
  scripts/qualify_instrument_registry.py --broker=ibkr --limit=100`
  for IBKR contract qualification cache warm-up.

- `config/instrument_registry.yaml` is the single configuration surface:
  enable/disable, IBKR feature flag, retire policy, overrides
  (pinned/excluded), availability timeout, and per-source toggles +
  cadences + sub-source IDs.

What did NOT change: `brokers/base.py`, `risk/engine.py`,
`execution/engine.py`, `signals/*`, `strategies/*`, `portfolio/*`,
`config/ibkr_universe.yaml` (preserved as a curated override layer that
the registry consumer unions in).

Tests:

- `tests/test_instruments_canonical.py` — 13 cases (symbol normalisation
  across equities, FX, crypto, futures, IBKR/Alpaca/Kraken/Binance
  broker translations).
- `tests/test_instruments_registry.py` — 7 cases (coerce_contribution
  with valid/invalid inputs, dataclass guarantees).
- `tests/test_instruments_sources_wikipedia.py` — 4 cases (S&P 500
  fixture parse, error handling, source-id filtering).
- `tests/test_instruments_sources_ishares.py` — 4 cases (CSV parse,
  cash-row filtering, ISIN/sector capture, missing-header errors).
- `tests/test_instruments_sources_openfigi.py` — 4 cases (mapping
  enrichment, empty seed, partial batch).
- `tests/test_instruments_sources_broker_catalog.py` — 5 cases
  (Kraken/Binance symbol normalisation, dedup, adapter failure
  isolation, broker exclusion).
- `tests/test_instruments_availability.py` — 8 cases (alpaca catalog
  resolution, IBKR `requires_qualification`, blocked override,
  end-to-end async resolver with mocked DB).
- `tests/test_instruments_builder.py` — 6 cases (config load defaults,
  YAML overrides, source build filtering, dry-run audit, failure
  isolation).
- `tests/test_instruments_scheduler.py` — 3 cases (broker-connect
  event consumer, async session factory, start/stop lifecycle).
- `tests/test_ibkr_supported_symbols_from_registry.py` — 4 cases
  (curated-seed default, empty-registry fallback, registry union,
  silent recovery from DB error).

Total: 59 D116 tests passing.

Verification:

- `python -m py_compile` for every new module (canonical, registry,
  http, wikipedia, ishares, openfigi, static_fx, static_futures,
  broker_catalog, availability, builder, scheduler, IBKR adapter,
  broker manager, orchestrator, snapshot service, api/server,
  scripts/build_instrument_registry.py,
  scripts/qualify_instrument_registry.py).
- `python -m pytest` D116-scoped suites → `59 passed`.

Rollout:

1. Migration + module + tests land with `enabled: true` but
   `ibkr_supported_symbols_use_registry: false` so behaviour is
   unchanged for IBKR routing.
2. `python scripts/build_instrument_registry.py --sources=all
   --dry-run` confirms source health.
3. First real refresh populates the registry; the orchestrator
   scheduler keeps it up to date.
4. Flipping `ibkr_supported_symbols_use_registry: true` (or the env
   override) grows the IBKR seed beyond the curated 61 names.
5. `tests/test_ibkr_supported_symbols_from_registry.py` guarantees the
   fallback path so IBKR remains routable even if the registry/DB is
   unhealthy.

---

## D115 — Anti-churn + cluster-aware risk + intraday derisk + stale-price gate (2026-05-19)

Five-tier rectification after the 2026-05-19 paper-trading audit. 224 fills
in 8 hours moved $6.0M of turnover on a $1.18M NAV and ended the session
at roughly -$5,000 / -0.4% with no individual stop-loss or daily-loss limit
having fired. The audit traced the bleed to three structural failures
(unbounded duplicate signals, undetected directional clusters, no graduated
portfolio-level defence) and one execution-layer leak (stale-price paper
fills). The risk engine retains unconditional veto power throughout; every
new gate is either a strict reject layer or a reduce-only emitter routed
through the normal SignalEngine + RiskEngine + ExecutionEngine path.

Decision:

- `signals/anti_churn.py` adds an `AntiChurnGate` with three production-
  grade rejects:
    * dedup        — same `(strategy, symbol, side, conf, price)` within
                     90s (per-strategy)
    * contradiction — strategy A long X + strategy B short X within 5min;
                     lower-confidence side rejected, both sides tombstoned
    * post_fill   — re-entry on `(broker, symbol)` within mode-aware
                     cooldown (hunter 120s, trader 180s, defender 600s)
  Wired into `SignalEngine.process()` and `raw_to_signal_candidate()` BEFORE
  meta-label, and `record_fill()` is called by `TradingLoop` after every
  confirmed fill. Operator closes, reduce-only trims, and allocator-
  selected opens are exempt. Config: `config/strategies.yaml::signal_engine.anti_churn`.

- `risk/engine.py::_check_fx_cluster_exposure` caps aggregate signed USD
  exposure across all held forex positions plus the proposed signal.
  Today's six FX legs (EURUSD long, GBPUSD long, AUDUSD long, USDCAD/CHF/
  JPY short) were one bet on dollar weakness sized as if they were six
  independent risks. Pair-orientation rules: xxxUSD long = short USD,
  USDxxx long = long USD. Reduce-only and neutralising legs are never
  blocked. Config: `config/risk_limits.yaml::fx_cluster`.

- `risk/engine.py::_check_equity_index_cluster_exposure` is the symmetric
  cap for the US broad-market index family (SPY, QQQ, IWM, DIA, VTI, VOO,
  IVV, MDY, TQQQ/SQQQ/SPXL/SPXS). Same neutralise/reduce-only rules.
  Config: `config/risk_limits.yaml::equity_index_cluster`.

- `risk/intraday_derisk.py` implements a graduated portfolio-level defence
  that sits BEFORE the static `max_daily_loss_pct` kill switch. Three
  tiers: -0.5% intraday triggers a 20% trim of the worst losers (max 2),
  -1.0% triggers a 50% trim (max 4), -1.5% triggers a full close (max 6).
  Cooldown 120s per `(broker, symbol)`. Wired as
  `Orchestrator._intraday_derisk_loop` / `_run_intraday_derisk_tick`,
  cancelled on stop. All emitted actions are reduce-only, still routed
  through SignalEngine + RiskEngine + ExecutionEngine. Config:
  `config/risk_limits.yaml::intraday_derisk`. Profit-harvest peak
  persistence (D115 item 8) was already in place via
  `Orchestrator._persist_profit_harvest_peaks` and survives restart.

- `execution/engine.py::_simulate_fill` rejects an opening paper fill when
  `signal.suggested_price` has drifted against the trade direction by
  more than `stale_price_gate.max_adverse_drift_bps` (default 25 bps).
  Returns a REJECTED OrderResult with `filled_quantity=0` and sets
  `last_skip_reason`. Reduce-only / close intents are exempt. Config:
  `config/risk_limits.yaml::stale_price_gate`. Backtest harness disables
  this and the anti-churn gate for the duration of the run (wall-clock
  semantics do not apply when replaying historical bars in milliseconds).

- `scripts/flatten_orphaned_remnants.py` is the operator-facing housekeeping
  tool for the post-incident cleanup. Identifies and (with `--apply`)
  flattens paper-ledger positions below a configurable notional ceiling
  (default $25,000) with optional symbol/broker/loss-pct filters. Paper-mode
  only; refuses live. Dry-run by default. Writes filled close `OrderLog`
  rows plus zero-quantity `PositionLog` tombstones using the same helper
  that backs the D070 local-paper-flatten path.

Tests:

- `tests/test_anti_churn_gate.py` — 17 cases (dedup, contradiction,
  post-fill cooldown, mode-aware cooldowns, operator-close exemption,
  engine integration).
- `tests/test_fx_cluster_exposure.py` — 9 cases (orientation helper, signed
  exposure math, cap enforcement, neutralising / reduce-only / non-FX
  exemptions, disabled-gate passthrough).
- `tests/test_equity_index_cluster.py` — 6 cases (cluster membership,
  additive vs opposite-direction, reduce-only, disabled-gate).
- `tests/test_intraday_derisk.py` — 11 cases (no-action when positive,
  tier ladder, short-position close direction, cooldown, min-loss filter,
  winners never trimmed).
- `tests/test_stale_price_gate.py` — 7 cases (BUY/SELL adverse rejection,
  favorable drift fill, sub-threshold fill, reduce-only exemption,
  disabled-gate passthrough, missing-quote passthrough).
- `tests/test_flatten_orphaned_remnants.py` — 5 cases (loss-pct math).

Verification:
- `python -m py_compile signals/anti_churn.py signals/engine.py risk/engine.py risk/intraday_derisk.py execution/engine.py system/orchestrator.py system/trading_loop/loop.py scripts/flatten_orphaned_remnants.py backtest/harness.py`
- `python -m pytest -q` → `1235 passed, 3 skipped, 1 warning` (the warning
  is a pre-existing AsyncMock fixture leak in `test_profit_harvest.py`,
  unchanged by this work).
- Targeted: anti-churn / FX cluster / equity-index cluster / intraday
  derisk / stale-price gate / remnants suites → `55 passed`.

## D114 — Session-exit policy embedded in global-edge decisions (2026-05-19)

Market-session intelligence now includes pre-close position review without
becoming a blunt "close everything at the bell" rule.

Decision:
- `core/market_session.py` exposes `session_close_at()` and
  `minutes_to_session_close()` as broker-aware timing helpers. 24/7 venues and
  unknown/closed markets return `None` so no synthetic close is invented.
- `config/session_exit_policy.yaml` defines the governed pre-close windows and
  mode/horizon policy. Intraday/scalp positions default to no overnight carry;
  swing/position trades default to holding through the close.
- `core/session_exit_policy.py` evaluates each position into one of:
  `hold_through_close`, `trim_before_close`, `close_before_close`, or
  `defer_action`, with an explicit reduce fraction and reason.
- `GlobalEdgeCoordinator.propose_session_exit_actions()` converts only
  executable close/trim decisions into normal `trim_symbol` reduce-only
  coordinator actions. These still pass through SignalEngine, RiskEngine,
  router, and ExecutionEngine; there is no risk bypass and no forced flatten.
- `TradingLoop._run_global_edge_tick()` prepends session-exit reduce-only
  actions ahead of ordinary opens/replacements, de-duplicating against other
  reduce actions already selected in the same loop.

Implication:
The system can close or trim positions before a venue shuts only when the
position's own profile says that is appropriate: explicit intraday/no-overnight
positions close near the bell; defender mode can bank profitable swing exposure;
normal swing/position theses can remain open overnight/days/weeks. The
execution gate remains the final binary physics check for closed venues.
