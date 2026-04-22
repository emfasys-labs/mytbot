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
