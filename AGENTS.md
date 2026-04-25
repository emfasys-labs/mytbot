# AGENTS.md
# ==========
# This file is for Codex (Codex.ai).
# Paste the contents of this file at the start of any Codex conversation
# to instantly bring Codex up to speed on the project state.
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
Signals pass through an optional **Signal Accumulation Engine** (`signals/accumulator.py`) that maintains time-decayed, multi-source conviction per asset (quant + news + macro) before the signal engine emits a unified Signal.
The signal engine aggregates strategy outputs into a Signal (with legacy point-in-time AI modifier and optional accumulated overlay).
Every Signal passes through the risk engine (unconditional veto power).
Approved signals go to the execution engine which routes to the best broker.
Everything is logged. Runtime broker permissions support graceful fallback routing.
Risk parameters are managed by `ParameterManager` with layered overrides
(regime > AI > defaults) and bounded, auditable changes.
AI is local-first (rules → FinBERT → local LLM → optional paid fallback) — never places orders.

## KEY FILES
- `run.py`                   — **THE entry point** (`python run.py` starts everything)
- `system/orchestrator.py`   — state machine: OFF → STARTING → RUNNING → STOPPING → OFF
- `system/dependency_manager.py` — auto-start Postgres/Redis via Docker
- `system/broker_manager.py` — auto-discover and connect available brokers
- `system/trading_loop/`    — controllable async trading loop package (`TradingLoop`, `helpers.py`)
- `brokers/base.py`          — the adapter interface (FROZEN: backward-compatible optional fields only)
- `brokers/registry.py`      — add new brokers here (one line)
- `brokers/ibkr/adapter.py`  — IBKR + single-leg options (chain / qualify / `Order.instrument_metadata`)
- `core/instruments.py`      — `OptionContractSpec` (options as structured instruments)
- `risk/options_env.py`      — `ENABLE_OPTIONS` / `OPTIONS_*` env merged into risk config
- `brokers/bybit/adapter.py` — Bybit V5 (spot / USDT linear)
- `brokers/_template/`       — copy this to add any new exchange
- `risk/engine.py`           — risk checks, kill switch
- `risk/parameters.py`       — parameter manager (defaults + overrides + expiry)
- `signals/accumulator.py`   — stateful time-decayed conviction per symbol (optional; YAML-gated)
- `signals/engine.py`        — signal aggregation + accumulator integration
- `strategies/momentum.py`   — first strategy (momentum breakout)
- `execution/engine.py`      — order placement
- `execution/router.py`      — smart order routing
- `storage/models.py`        — database schema
- `api/server.py`            — FastAPI: `/system/start`, `/system/stop`, `/system/status`, `/dashboard/snapshot`, `/pnl` (week/month)
- `api/pnl_periods.py`       — calendar week/month rollups over `daily_pnl` for `/pnl`
- `system/dashboard_publish.py` — persists allocator snapshot (`dashboard.snapshot` in `ControlState`) for the UI
- `config/risk_limits.yaml`  — all risk thresholds (editable without code change)
- `config/m8_micro_live.yaml` — optional micro-live profile (symbol/strategy/notional caps when `APP_ENV=live`)
- `config/fundamentals.yaml` — parameter defaults, absolute bounds, AI policy
- `config/broker_permissions.yaml` — runtime broker permission/fallback map
- `config/data_pipeline.yaml` — M2 symbols, intervals, News/FRED toggles
- `config/ai.yaml`          — local-first AI config: providers, escalation policy, no daily caps
- `config/profile_modes.yaml` — D015 mode coefficients + emergency safety_bounds (allocator)
- `config/allocation.yaml`  — D015 global opportunity replacement policy (validated by `config/models.py`)
- `config/loaders.py`       — `load_profile_modes()` / `load_allocation()`
- `core/models_runtime.py`  — runtime types: Opportunity, RegimeState, AllocationDecision, ExecutionPlan
- `signals/volume_anomaly.py` — D015 volume/flow features, detection vs scoring, YAML-weighted component
- `signals/opportunity_engine.py` — D015 opportunities; blends momentum proxy + volume component from M2 JSON + metadata
- `risk/regime_state.py` — D015 market/regime context for allocator (stub)
- `portfolio/allocation_engine.py` — D015 global replacement allocator (stub)
- `execution/planner.py` — D015 AllocationDecision → ExecutionPlan
- `data/regime_metrics.py` — cross-section feature fetch + aggregates for regime
- `data/feature_lookup.py` — latest `feature_snapshots.features` per symbol
- `portfolio/d015_hold_switch.py` — hold score + switching-cost penalty
- `signals/d015_weights.py` — profile/regime dynamic coefficient resolver
- `signals/opportunity_components.py` — momentum/news/liquidity/structure component scores
- `system/d015_shadow.py` — optional per-signal D015 vs legacy log (env `ALLOCATOR_D015_SHADOW`)
- `core/signal_math.py` — bounded_sigmoid, tanh_clip, normalize_zscore (Decimal)
- `run_pipeline.py`          — M2: yfinance → features → Postgres; NewsAPI + FRED
- `ai/router.py`             — local-first AI router (drop-in for NewsClassifier)
- `ai/providers/`            — provider implementations (rules, FinBERT, Ollama, Codex fallback)
- `ai/escalation.py`         — necessity-based escalation engine (no hard daily caps)
- `ai/schemas.py`            — shared AI types (ProviderResult, EscalationContext)
- `ai/news_classifier.py`    — legacy Codex classifier (kept for backward compat)
- `ai/pipeline.py`           — M6 orchestration: symbol news score + macro regime
- `docs/DECISIONS.md`        — architectural decision log
- `docs/BUILD_PLAN.md`       — milestones M1–M10 + task history
- `docs/NEW_MACHINE_SETUP.md` — new PC / full reinstall (Python, Docker, Ollama, UI, `.env`)
- `docs/M8_MICRO_LIVE.md`    — micro-live guardrails (`APP_ENV=live`, YAML profile)
- `alembic/`                 — DB migrations (URL from POSTGRES_* in env.py)
- `tests/`                   — pytest smoke tests
- `.cursorrules`             — Cursor AI alignment rules

## CURRENT STATE
<!-- Update this section after each work session -->
- Milestone: M10 — Local-First AI Architecture ✅
- Last completed task: **D043 — Strategy mix default roster + live sparklines from `/intelligence/signals`**. Redesign Strategy screen always shows the full signal + arb taxonomy; sparklines use recent DB signal confidences per strategy; `loaded_strategies` cleared when absent from `/system/status`.
- Last completed task: **D042 — Wave 9 (fee-prior + online fusion, slippage p50/p90, broker comparison)**. Router blends fee prior with learned quality for ranking (`fused_routing_score`), tracks rolling slippage |bps| and fill counts per broker-symbol, persists `broker_comparison` + `exec_metrics` in `routing.quality.state`; `GET /diagnostics/routing-quality` returns full state including `quality_stats`; Risk diagnostics adds a broker comparison table and uses fused scores for best-venue display.
- Last completed task: **D041 — Wave 8 (routing confidence intervals + adaptive decay + trajectory sparklines)**. Router now exports per-broker CI95 stats and uses adaptive decay driven by sample/activity/staleness proxies; loop feeds turnover/liquidity hints into routing feedback; diagnostics contract includes `quality_stats`; redesign Risk diagnostics adds routing trajectory mini-sparklines with CI display.
- Last completed task: **D040 — Wave 7 (persistent routing trajectories + mode-adaptive alerts + diagnostics endpoint)**. Routing learning now persists in control-state and reloads on startup; decay policy runs per configured cadence; demand alert history is mode-threshold driven; new `/diagnostics/routing-quality` endpoint and redesign diagnostics panel show per-symbol routing quality trajectories.
- Last completed task: **D039 — Wave 6 (learned routing feedback + mode-adaptive demand thresholds + UI diagnostics)**. Added learned broker-symbol routing feedback in `execution/router.py`; loop now records execution outcomes into router quality; demand alert thresholds are mode-aware with alert history telemetry; redesign Risk screen now shows demand/meta diagnostics from snapshot/runtime heartbeat.
- Last completed task: **D038 — Wave 5 (demand-aware routing + adaptive meta priors + demand alerts)**. Added metadata-aware routing preferences in `execution/router.py`; added online strategy-bias calibration hooks in `signals/meta_adaptation.py` and loop integration; added demand-regime alert payloads to heartbeat/dashboard snapshots.
- Last completed task: **D037 — Wave 4 (demand telemetry + mode-calibrated meta-labeling + demand-conditioned planner urgency)**. Added demand sections to dashboard/global-edge snapshots and heartbeat, added mode-specific meta-label calibration (`defender/trader/hunter`) in strategy config + filter logic, and applied demand-alignment urgency multipliers in `execution/planner.py`.
- Last completed task: **D036 — Wave 3 (cross-asset demand graph + volatility overlay + ML-style meta-labeling)**. Added `system/cross_asset_demand_graph.py`; upgraded `system/demand_engine.py` to include graph outputs (`market_volatility`, coverage); upgraded `signals/meta_labeler.py` to probability-scored filtering; added allocator gross-exposure volatility overlay in `portfolio/allocation_engine.py`; tests in `tests/test_demand_meta.py` and `tests/test_allocation_vol_overlay.py`.
- Last completed task: **D035 — Demand-engine + volatility/regime/meta wave**. Added `system/demand_engine.py`, `signals/meta_labeler.py`, `strategies/volatility_regime.py`, `strategies/regime_rotation.py`; wired demand score + meta filtering into `system/trading_loop/loop.py`, plus demand-aware weighting in `signals/opportunity_engine.py` and `portfolio/global_edge_coordinator.py`.
- Last completed task: **D034 — Strategy expansion wave (event-driven, pairs, volume-flow)**. Added `strategies/event_driven.py`, `strategies/pairs_trading.py`, `strategies/volume_flow.py`; wired into `system/trading_loop/loop.py` for both legacy and batch candidate paths; added YAML gates in `config/strategies.yaml`; tests in `tests/test_strategies.py` (all green).
- Last completed task: **D033 — Post-open stop-loss monitor wired at runtime**. `Orchestrator` now starts a dedicated `stop-loss-monitor` background task that evaluates live positions with `risk.stop_loss.evaluate_stop_loss` every `STOP_LOSS_MONITOR_INTERVAL_SEC` (default 15s, min 5s). Breached positions are closed via normal risk+execution flow (`RiskEngine.evaluate_and_persist` then `ExecutionEngine.execute`) using `strategy=stop_loss_monitor` and `metadata.reduce_only=true`; no risk bypass. Added close-throttle `STOP_LOSS_CLOSE_COOLDOWN_SEC` (default 60s) per broker/symbol/direction to avoid repeated close spam while fills/reconciliation settle. Files: `system/orchestrator.py`, `tests/test_stop_loss_monitor.py`, `docs/DECISIONS.md` D033, `docs/ARCHITECTURE.md`. **Full suite: 382 passed, 3 skipped.**
- Prior: **D030 — Hunter must hunt** — mode-aware `max_notional_fraction_per_action` (hunter 1.00 / trader 0.50 / defender 0.15) + reconciliation persists broker-truth unconditionally.
- Prior: **`docs/NEW_MACHINE_SETUP.md`** + `scripts/setup_new_machine.{ps1,sh}` — full new-PC migration (requirements, Docker, Ollama models per `config/ai.yaml`, FinBERT, UI, `.env`); README + `Codex` key files + `.env.example` pointer.
- Prior: **Docs sync (audit D020–D023)** — `docs/BUILD_PLAN.md` **G6**; `docs/ARCHITECTURE.md` orchestrator/contracts. Prior implementation: DECISIONS D023.
- Prior: **D023 Operational hardening** — pytest `PYTEST_API_DISABLE_READ_MIDDLEWARE` + conftest default; Docker compose up retries (`DOCKER_COMPOSE_UP_ATTEMPTS`); orchestrator pipeline chunked sleep + `trading.orchestrator_starting`; IBKR `place_order` retry jitter (`IBKR_PLACE_ORDER_RETRY_JITTER_SEC`). Docs: `docs/DECISIONS.md` D023.
- Prior: **D022 Late brokers + AI status** — `ExecutionEngine(broker_manager=...)`, `_get_broker` prefers `broker_manager.adapters`, `add_allowed_broker` from `TradingLoop._check_late_brokers`; heartbeat `ai` via `runtime_ai_status()` (`AIRouter` / `NewsClassifier`); `GET /system/status` merges `trading.ai` from `runtime.heartbeat`. Docs: `docs/DECISIONS.md` D022.
- Prior: **D021 Audit hardening** — shared `bind_app_database` / trading loop reuses API pool (no second engine); `ExecutionEngine.__init__` registers `set_execution_engine`; `SignalEngine` Decimal veto + dual-AI gate; IBKR `str(strike)`; orchestrator `last_start_error`; LiveStrip first-cycle + error copy; `snapshotFetchFailed` clears when not running. Docs: `docs/DECISIONS.md` D021.
- Prior: **D020 (amend)** — `GET /system/status` **`trading.snapshot_published_at`**; UI stale clock; heartbeat + `SignalBrain`. Docs: `docs/DECISIONS.md` D020.
- Prior: **D019 Dashboard V2 control tower** — `signals/accumulator.py` `dashboard_snapshot()`, `system/dashboard_publish.py` persists `dashboard.snapshot` to `ControlState` from D015 + global-edge ticks; `GET /dashboard/snapshot`, `GET /pnl` week/month + metrics, WebSocket `payload.dashboard` hint; React `ui/` layout (`LiveStrip`, `SignalBrain`, `AllocationCenter`, `RiskGate`, `PerformancePanel`) with top `NewsTicker` + bottom `OpportunityTicker` preserved. Docs: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` D019.
- Prior: **D017 Stateful signal accumulation** — `signals/accumulator.py` (`SignalAccumulator`, half-life decay, alignment/conflict), `SignalEngine` optional integration + metadata (`accumulator_*`), `feed_ai_pipeline_result` after AI compute in `system/trading_loop.py`, `run_m3.py`, `run_m5.py`; `config/strategies.yaml` `signal_engine.use_signal_accumulator` (default on). Docs: `docs/ARCHITECTURE.md`, `docs/BUILD_PLAN.md` G5, `docs/DECISIONS.md` D017.
- Prior: **D016 IBKR single-leg options** — `core/instruments.OptionContractSpec`, optional `instrument_metadata` on `Order`/`Position` in `brokers/base.py` (serialization only; defaults preserve existing adapters), `IBKRAdapter.get_option_chain` / `qualify_option_contract` / `get_option_market_data`, `ExecutionEngine` passes option payload into `Order`, `RiskEngine._check_options_trading_policy` + `config/risk_limits.yaml` `options_trading` + `risk/options_env.py`, portfolio `option_premium_exposure` in `run_m3._load_portfolio_state`, Alembic `c8f2a1d0e4aa`, smoke script `scripts/smoke_ibkr_options.py`. Default **options disabled** (`ENABLE_OPTIONS=false`).
- Prior: D015 **primary** operational path (allocator batch → risk → execution); see DECISIONS D004 amendment.
- Next task: Paper soak on primary path; IBKR options paper validation with `scripts/smoke_ibkr_options.py` when enabled.
- Blockers: GPU server for faster inference; IBKR stream/order need local IB Gateway/TWS
- Notes: .env not committed — use .env.example; `python run.py` is the ONLY command needed; Codex API disabled by default in config/ai.yaml; Ollama running on localhost:11434 with qwen2.5:7b + llama3.1:8b. **2026-04-12:** codebase assessment follow-up — IBKR `_remaining_safe` (NaN/stale `remaining`, PAXOS cash-qty fills), broker reconnect backoff jitter, `tests/test_ibkr_map_status.py` + execution `cancel_all` test, DECISIONS duplicate D012–D014 table. M2 already wraps yfinance/News/FRED in `_to_thread_with_retry`; Bollinger via `pandas_ta.bbands` in `data/features.py`.

## RULES Codex MUST FOLLOW IN THIS PROJECT
1. Never break `brokers/base.py` — the `BrokerAdapter` ABC and existing fields are frozen; backward-compatible optional dataclass fields (e.g. `instrument_metadata`) are allowed when all adapters default them to `None`
2. Never add a bypass to the risk engine
3. Decimal for all prices and quantities, never float
4. paper_mode=True is always the default
5. Every new broker = one new file + one line in registry.py, nothing else
6. Log every signal, risk decision, order, and fill
7. AI only scores and explains — never executes (local-first; paid API is optional fallback)
8. Check `docs/DECISIONS.md` before making architectural choices

## HOW TO USE THIS FILE
At the start of a Codex session, say:
"Here is my project context: [paste this file]
Current task: [describe what you want to work on]"

Codex will immediately understand the full architecture and constraints
without needing to re-explain everything.
