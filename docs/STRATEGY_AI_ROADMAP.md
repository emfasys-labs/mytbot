# STRATEGY_AI_ROADMAP.md

**Status:** Wave 0 baseline (2026-04-27).
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
| 2 | Trained meta-labelling | **DONE + WIRED** (gated off by default) | Module: `models/meta_label/*`, `signals/trained_meta_labeler.py`, `config/meta_labeler.yaml`. Wired into `signals/engine.py` (both `process()` and `raw_to_signal_candidate()`) and `signals/opportunity_engine.py` (`build_opportunities` + async wrapper). Toggle: `signal_engine.use_trained_meta_labeler` in `config/strategies.yaml` (default false) plus `trained_meta_labeler.enabled` in `config/meta_labeler.yaml` (default false). 15 unit tests + 8 wiring tests. Heuristic in `signals/meta_labeler.py` remains the live filter until BOTH gates are flipped AND a registered+approved model exists. |
| 3 | Cross-sectional factor sleeve | **DONE + WIRED** (gated off by default) | Module: `data/factor_features.py`, `data/fundamental_features.py`, `signals/factor_scoring.py`, `strategies/factor_sleeve.py`, `strategies/factor_sleeve_runner.py`. Wired into `signals/opportunity_engine.build_opportunities_async` via auto-loaded `FactorSleeveConfig` (cached). When `factor_sleeve.enabled: true`, the runner pulls close-price history from `feature_snapshots` for the candidate universe, builds `FactorUniverseInput` rows, and merges sleeve candidates into the signal stream before scoring (with (symbol, side, strategy) dedup). Sleeve failures are caught and logged so the loop never crashes on a sleeve bug. 26 module tests + 9 wiring tests. |
| 4 | Regime / volatility / covariance upgrade | pending | GARCH, Ledoit-Wolf, HMM. |
| 5 | Research-grade relative value | pending | Johansen, Kalman hedge ratio, linked legs. |
| 6 | Forecast-native structured ML | pending | Multi-horizon return / vol / breakout. |
| 7 | Multimodal AI fusion | pending | LLM stays in classification/explanation role. |
| 8 | Portfolio optimisation upgrade | pending | HRP, CVaR, half-Kelly, vol targeting. |
| 9 | Execution cost / impact / scheduling | pending | Square-root impact, slicing, A-C lite. |
| 10 | Microstructure / LOB | pending | Crypto first; gated by data quality. |
| 11 | Deep sequence models | pending | Strictly gated; must beat tabular OOS. |
| 12 | Options strategy layer | pending | Single-leg first; conservative. |
| 13 | Dashboard + observability upgrade | pending | Funnel, model health, attribution. |
| 14 | Paper soak + activation gates | pending | 2-4 weeks per change before micro-live. |

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
