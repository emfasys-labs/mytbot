# STRATEGY_AI_ROADMAP.md

**Status:** Waves 0-14 scaffolded; activation/wiring audit pass updated 2026-04-27.
**Owner:** kvcom.
**Scope:** Roadmap to evolve mytbot from an AI-assisted rule-driven stack into a
forecast-native systematic trading platform without rewriting the architecture.

This document is the source of truth for *what is implemented*, *what is missing*,
and *which wave addresses each gap*. It is updated at the end of every wave.

## Prime directives (frozen)

1. Architecture is final-form and modular. New capability deepens existing
   layers (`data/`, `strategies/`, `signals/`, `risk/`, `portfolio/`,
   `execution/`, `ai/`, `storage/`, `ui/`).
2. `risk/engine.py` retains unconditional veto power. No bypass, ever.
3. AI never places orders or calls broker adapters. AI scores and explains.
4. `brokers/base.py` is frozen. Backward-compatible optional dataclass fields
   only (e.g. `instrument_metadata`, defaulting to `None`).
5. `Decimal` for every money/price/quantity/notional/fee/weight value.
6. Paper mode is default. Live activation is config-gated and off by default.

## Wave plan and current state

| Wave | Title | Status | Notes |
|----:|---|---|---|
| 0 | Baseline audit and safety lock | **DONE** | Roadmap, governance, coverage matrix, safety-lock tests landed. |
| 1 | Model registry + prediction storage + feature contracts | **DONE** | `models/` package (registry, schemas, feature_contracts, calibration, prediction_store), Alembic migration `b3a07f1e9c10`, `config/model_registry.yaml`, `config/ml_features.yaml`, `scripts/model_report.py`, 20 acceptance tests. |
| 2 | Trained meta-labelling | **DONE + WIRED + PAPER ENABLED** | Module: `models/meta_label/*`, `signals/trained_meta_labeler.py`, `config/meta_labeler.yaml`. Wired into `signals/engine.py` (both `process()` and `raw_to_signal_candidate()`) and `signals/opportunity_engine.py` (`build_opportunities` + async wrapper). First real paper candidate: `mytbot_meta_labeler@0.1.0`, logistic baseline, feature hash `8615c3cf26f8d684df3ebd90411af60039c380bd7ef154fd2b61be0cee1ab955`, registered in `config/model_registry.yaml` and DB `model_versions`, enabled in paper via `config/meta_labeler.yaml` + `config/strategies.yaml`. Not micro-live/live approved. |
| 3 | Cross-sectional factor sleeve | **DONE + WIRED + PAPER ENABLED** | Module: `data/factor_features.py`, `data/fundamental_features.py`, `signals/factor_scoring.py`, `strategies/factor_sleeve.py`, `strategies/factor_sleeve_runner.py`. Wired into `signals/opportunity_engine.build_opportunities_async` via auto-loaded `FactorSleeveConfig` (cached). `factor_sleeve.enabled: true` by default for paper observability; the runner pulls close-price history from `feature_snapshots` for the candidate universe, builds `FactorUniverseInput` rows, and merges sleeve candidates into the signal stream before scoring (with (symbol, side, strategy) dedup). Sleeve failures are caught and logged so the loop never crashes on a sleeve bug. 26 module tests + 9 wiring tests. |
| 4 | Regime / volatility / covariance upgrade | **DONE + WIRED** (gated off by default) | Module: `risk/volatility_models.py`, `portfolio/covariance.py`, `portfolio/correlation_monitor.py`, `risk/regime_models.py`. Wired via `risk/regime_state.py` — when `regime_models.classifier.enabled: true` and a fitted artefact path is configured, `compute_regime_state_from_inputs` consults the classifier; output is mapped to the existing `RegimeLabel` vocabulary, `regime_classifier_*` metadata is exposed for the dashboard, the `insufficient_data` sentinel is never overridden, and any classifier failure falls back to the heuristic label. 19 module tests + 6 wiring tests. Volatility-model and covariance wiring (allocator + sizing) deferred to Wave 8 (portfolio optimisation), per the recommended order. |
| 5 | Research-grade relative value | **DONE + PAPER ENABLED** (linked-leg execution still gated) | `models/pairs/{spread,johansen,kalman,risk,universe}.py` — `compute_spread`, `spread_zscore`, `half_life_ou` (OU regression), `engle_granger_test` (OLS hedge ratio + ADF screen with canned MacKinnon criticals), `johansen_eigen_test` (VECM eigendecomposition; statsmodels-optional), `KalmanHedgeRatio` (online time-varying β), `detect_spread_break` / `detect_correlation_decay` / `transaction_cost_aware_thresholds`, `discover_pair_candidates` (universe-level ranking by correlation + EG screen + half-life). `strategies/stat_arb_pairs.py` (`StatArbPairsStrategy` emitting `LinkedOpportunity` with `LinkagePolicy` enum: cancel_sibling / hedge_with_index / flatten_both). `config/pairs_trading.yaml` is enabled for paper observability; existing heuristic in `strategies/pairs_trading.py` remains the production-proven live pairs path until linked-leg allocator/execution promotion is complete. |
| 6 | Forecast-native structured ML | **DONE + WIRED** (gated off by default) | Module: `models/forecasts/*`, `signals/forecast_bridge.py`. Wired via `signals/opportunity_engine.py` — when `forecast_bridge.enabled: true` and members are registered+approved, every Opportunity is scored through the ensemble; populates `Opportunity.expected_return` (sign-aligned to side), `Opportunity.volatility` (from forward-vol forecasts), and modulates `Opportunity.confidence` via geometric-mean blend. Forecast metadata (`forecast_used / _reason / _expected_return / _expected_volatility / _confidence / _confidence_blended / _horizons / _members_used / _contributions`) is exposed on every Opportunity for the dashboard funnel. Bridge runs *before* the trained meta-labeller so the labeller sees forecast-modulated state. 21 module tests + 6 wiring tests. |
| 7 | Multimodal AI fusion | **DONE** (gated off, not yet wired) | `ai/news_event_memory.py` (materiality-weighted half-life decay; lookback windows; max-events bound), `ai/market_context.py` (`MarketContext` aggregator + `MarketContextBuilder.from_inputs(...)` wiring forecast/news/macro/graph/portfolio/execution/accumulator), `ai/fusion.py` (`MultimodalFusion.combine(context) → FusionScore` with directional_bias, confidence, conflict_score, decomposable `contributions`, `trigger_llm_ensemble` recommendation; LLM never invoked directly — verified by AST test that `ai/fusion.py` cannot import `brokers.*`), `graph/relationship_loader.py` (`Relationship`, `RelationshipIndex` for upstream→downstream lookup), `config/multimodal_fusion.yaml` (enabled=false), `scripts/evaluate_fusion.py`, 18 acceptance tests. Wiring into `signals/accumulator.py` / `signals/opportunity_engine.py` deferred. |
| 8 | Portfolio optimisation upgrade | **DONE + WIRED** (overlay gated off by default) | Module: `portfolio/{kelly,vol_targeting,hrp,cvar,optimizers}.py`. Wired via `portfolio/allocation_engine.py` — when `portfolio_optimisation.vol_targeting_overlay.enabled: true`, the D015 gross-exposure target `ge` is multiplied by `combined_scale(target_vol, realised_vol, drawdown)` from `portfolio/vol_targeting.py` using `regime_state.metadata['market_volatility']` and `portfolio_state.drawdown_from_hwm_pct`. Per-component scale + diagnostics (`wave8_vol_overlay_*`) surface on `AllocationDecision.metadata` for the dashboard. Defensive: any overlay failure falls back to unmodified `ge`. 26 module tests + 6 wiring tests. HRP/CVaR/Kelly target-weight switching deferred to a deeper follow-up (needs per-symbol returns matrix in the loop). |
| 9 | Execution cost / impact / scheduling | **DONE + WIRED** (gated off by default) | Module: `execution/{impact,slippage_model,venue_quality,order_slicer,scheduler}.py`, `execution/wave9_runtime.py`. Wired via `execution/engine.py` — `ExecutionEngine.__init__` auto-loads `Wave9RuntimeConfig`; `execute()` runs `pre_flight_cost_gate(...)` after dedup but before `_build_order`; `DO_NOT_TRADE` urgency short-circuits the order with logged metadata, `wave9_gate_blocked` / `wave9_gate_passed` counters increment for ops visibility, and `wave9_*` diagnostic keys are stamped on the placed order's `instrument_metadata` (cost breakdown, urgency, slippage source). Defensive try/except inside `pre_flight_cost_gate` ensures any failure returns `allow=True, used=False` so the engine never crashes on a Wave 9 bug. 22 module tests + 8 wiring tests. Slicer + scheduler-driven order shape (LIMIT vs PASSIVE vs SLICED child construction) deferred — gate is the high-value, low-risk slice of the wiring. |
| 10 | Microstructure / LOB | **DONE** (gated off, not yet wired) | `data/orderbook_features.py` (`OrderbookSnapshot`, `OrderbookLevel`, `build_orderbook_features` — spread/imbalance/slope/fragility/VPIN/staleness; defensive on crossed/empty/non-finite books), `models/microstructure/{features,imbalance,train_lob}.py` (`stack_lob_features` + `train_imbalance_forecaster` + `score_orderbook` with freshness gate; pickle save/load with feature-hash validation; `quote_staleness` intentionally excluded from model inputs to avoid wall-clock contamination), `config/microstructure.yaml` (enabled=false; crypto-only allow-list), `scripts/evaluate_microstructure.py`, 13 acceptance tests. Scheduler integration (LOB-driven nudge between MARKET / LIMIT urgency in Wave 9 scheduler) deferred. |
| 11 | Deep sequence models | **DONE** (gated off; comparison harness shipped) | `models/deep_sequence/{dataset,baseline,tcn,tft,train,evaluate,infer}.py`. Always-available `RidgeSequenceBaseline` (NumPy-only) is the floor; `compare_against_baseline` codifies the Wave-11 gate (deep must beat baseline on MSE-ratio AND hit-rate margin AND cost-aware net P&L OOS — failing any returns `deep_beats_baseline=False`); `train_deep_sequence_model` returns `promote_eligible` only when comparison wins; `build_tcn` / `build_tft` are torch-gated stubs that raise informative errors when PyTorch is absent. `config/deep_sequence.yaml` (enabled=false; default architecture=none), `scripts/train_deep_sequence.py`, 22 acceptance tests. AST test enforces `models/deep_sequence/` cannot import `brokers.*`. PyTorch + real TCN/TFT implementations are the operator's deferred work. |
| 12 | Options strategy layer | **DONE + PAPER ENABLED** (paper-only; conservative) | `models/options/{greeks,iv_surface,risk}.py` — Black-Scholes price + Δ/Γ/ν/Θ/ρ via NumPy + `math.erf` (no scipy); `IVSurface` with bilinear interpolation + calendar-arbitrage screen; `check_premium_exposure` (per-trade + aggregate caps as fractions of NAV); `check_underlying_required` (long-stock-required gate). `strategies/options_directional.py` (`LongCallStrategy`, `LongPutStrategy` — emit `SignalCandidate` with `instrument_metadata={"instrument_type": "option", "option_contract": {...}}` matching existing IBKR adapter shape; DTE band + delta band gates). `strategies/options_hedging.py` (`ProtectivePutStrategy` requires long stock; `CoveredCallStrategy` refuses naked-call attempts as a safeguard). `config/options_strategies.yaml` ships `enabled=true, paper_only=true`, `scripts/smoke_options_chain.py`, 25 acceptance tests including put-call parity, calendar-arbitrage detection, naked-call refusal, and premium-cap enforcement. |
| 13 | Dashboard + observability upgrade | **DONE + UI WIRED** | `system/funnel_telemetry.py` (`FunnelTelemetry` thread-safe counters: evaluated → generated → meta_label_kept/blocked → forecast_kept/blocked → risk_approved/rejected → execution_approved/blocked → executed; per-strategy + aggregate; isolated-copy snapshots), `api/wave13_dashboard.py` (`build_wave13_payload(...)` aggregator distinguishing the 4 block reasons: `blocked_by_model` / `blocked_by_risk` / `blocked_by_execution` / `no_signal` plus `idle` / `in_flight` / `trading`; surfaces strategy coverage from YAML gates, model health from `model_registry.yaml`, portfolio intelligence from `AllocationDecision.metadata` including `wave8_vol_overlay_*`, execution intelligence from `wave9_gate_passed/blocked` counters), new `GET /dashboard/wave13` endpoint in `api/server.py`. The redesign Strategy grid now seeds the full operator-facing strategy roster, including factor sleeve, stat-arb pairs, and conservative options cards, while filtering internal allocator actions (`global_edge_trim`, `trim_symbol`) out of the strategy view. |
| 14 | Paper soak + activation gates | **DONE + ENFORCED FOR LIVE MODELS** | `governance/activation_gates.py` — 8 gates from the roadmap as runtime checks (`feature_contract`, `model_registered`, `validation_report`, `paper_soak`, `risk_rejection`, `execution_cost`, `rollback`, `config_flag`); `ActivationContext` defaults are unsafe so a fresh context fails closed; `ActivationVerdict.cleared_for_activation` only True when all gates pass; `ActivationVerdict.failed_gates` lists every blocker. `models/registry.py` now enforces these gates before returning any model for `Mode.LIVE`; a manual YAML status flip without activation evidence fails closed. `system/paper_soak.py` (`build_paper_soak_report` rendering all 6 markdown sections from the Wave-13 dashboard payload + operator-supplied breakdowns). `scripts/build_paper_soak_reports.py` writes `reports/paper_soak/*.md`. `docs/PAPER_SOAK.md` is the operator runbook. |

## Activation audit update (2026-04-27)

The wave modules are present, but most advanced capabilities remain disabled
by design until trained artefacts, model registry entries, activation evidence,
and paper-soak reports exist. The first trained meta-label paper candidate is
now registered and enabled for paper soak. Forecast-native ML, microstructure,
and deep sequence models remain inactive.

Runtime gaps closed in this audit:

- Wave 13 funnel telemetry is now fed by the trading loop's existing
  `strategy_candidate_log` rows and by the risk/execution boundary.
- Wave 14 activation gates are now enforced by `models/registry.py` for live
  model use. YAML approval alone is not sufficient for `Mode.LIVE`.
- The options execution boundary now honours structured option open intent:
  long call → BUY CALL, long put/protective put → BUY PUT, covered call → SELL
  CALL.

## Recommended execution order

The plan author's recommended order is preserved here so future sessions do
not drift:

0 → 1 → 2 → 3 → 4 → 6 → 8 → 9 → 5 (parallel ok) → 7 → 10 → 11 → 12 → 13 → 14.

## What exists today (Wave 0 audit summary)

Audited files and their state, used as the starting point for waves 1-13:

- `data/features.py` — RSI, MACD, ATR, momentum, Bollinger, fractional
  differencing (with safe fallback), Hurst, GARCH-style volatility proxy,
  VPIN-style toxicity proxy, volume-flow keys (`volume_z`,
  `relative_dollar_volume`, `trade_count_anomaly`, `volume_persistence`,
  `fake_spike_penalty`).
- `backtest/labels.py` — Triple-barrier labelling with `TripleBarrierSpec`;
  optional sklearn `RandomForestClassifier` import. Foundation for Wave 2.
- `backtest/validation.py` — `purged_time_series_splits` with embargo;
  Deflated Sharpe and PBO-style helpers. Optional `timeseriescv`
  `CombPurgedKFoldCV` import. Foundation for Wave 2/6.
- `signals/meta_labeler.py` — **Heuristic only.** Probability-style filtering
  with mode calibration but no trained model. Replaced by Wave 2.
- `signals/opportunity_engine.py` — D015 opportunity scoring with full
  component blend and dynamic profile/regime weights; consumes
  `data/feature_lookup.py`. Forecast inputs land here in Wave 6.
- `portfolio/allocation_engine.py` — D015 global replacement allocator;
  softmax weights, replacement advantage vs hold scores, safety bounds from
  `profile_modes.yaml`. Optimiser plug-points for Wave 8.
- `risk/regime_state.py` — Regime from M2 cross-section + optional news
  dispersion; heuristic `MarketStateComponents`. Upgraded in Wave 4.
- `execution/router.py` — SOR with broker permissions, fee priors, learned
  quality, fused score, demand bias. Cost-aware extensions in Wave 9.
- `execution/engine.py` — Idempotent placement, paper-aware, dedup window,
  reconciliation. Slicing/scheduling added in Wave 9.
- `strategies/` — `momentum`, `mean_reversion`, `event_driven`,
  `pairs_trading`, `regime_rotation`, `volatility_regime`, `volume_flow`,
  `arbitrage/`. All emit `SignalCandidate` / `Opportunity`, never orders.
- `ai/router.py` — Local-first provider chain (rules → FinBERT → local LLM →
  optional premium). Necessity-based escalation. Same interface as
  `NewsClassifier`.
- `ai/pipeline.py` — Orchestrates symbol news scoring + macro regime;
  produces `AIPipelineResult`. No execution access.

## What is missing (the gap that motivates this roadmap)

- No model registry, no prediction store, no feature contracts. Trained
  models cannot be governed, audited, or reproduced.
- Meta-labelling is heuristic; no trained probability model with calibration.
- No cross-sectional factor alpha layer.
- Regime/volatility/covariance is heuristic; no GARCH, no shrunk covariance,
  no HMM.
- No forecast-native ML producing first-class
  `Opportunity.expected_return` / `confidence` inputs.
- Pairs trading is implemented but not research-grade (no Johansen, no
  Kalman, no linked-leg execution semantics).
- Portfolio construction is softmax + safety bounds; no HRP, CVaR, or
  Kelly-sized vol targeting wired into live sizing.
- Execution is router-feedback driven, not impact/scheduling driven.
- No order-book forecasting, no deep sequence models, no RL execution.

Each gap maps to a wave in the table above. None of these gaps may be
"fixed" by bypassing the risk engine, by giving AI broker access, or by
breaking `brokers/base.py`. Those constraints are enforced by the Wave 0
safety-lock tests in `tests/test_wave0_safety_lock.py`.

## Update protocol

At the end of every wave:

1. Update the status column in this file.
2. Add a short "what changed" note under the wave's row.
3. Update `docs/STRATEGY_COVERAGE_MATRIX.md` with new coverage.
4. If any new model is introduced, register it per
   `docs/MODEL_GOVERNANCE.md` and update its row there.
