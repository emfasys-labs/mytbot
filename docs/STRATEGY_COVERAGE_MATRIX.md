# STRATEGY_COVERAGE_MATRIX.md

**Status:** Waves 0-14 scaffolded; activation/wiring audit pass updated 2026-04-27.

Coverage of mainstream systematic-trading capability families. Each row lists
the implementing files (today) and the wave that closes the gap. "Coverage"
is one of: **none**, **partial-heuristic**, **partial-trained**, **full**.

| # | Category | Coverage | Implementing files (today) | Gap → Wave |
|--:|---|---|---|---|
| 1 | Rule-based time-series alpha | partial-heuristic | `strategies/momentum.py`, `strategies/mean_reversion.py`, `strategies/volatility_regime.py`, `strategies/regime_rotation.py`, `strategies/event_driven.py`, `strategies/volume_flow.py`, `signals/engine.py`, `signals/opportunity_engine.py`, `signals/accumulator.py` | Calibrate via meta-label → Wave 2 |
| 2 | Cross-sectional factor alpha | partial-trained (paper enabled) | `strategies/factor_sleeve.py`, `strategies/factor_scoring.py`, `data/factor_features.py`, `data/fundamental_features.py` (Wave 3 — paper enabled; wired into `signals/opportunity_engine.build_opportunities_async`) | train/soak/activation follow-up |
| 3 | Relative value | partial-trained (paper enabled) | `strategies/pairs_trading.py` (heuristic, live), `strategies/arbitrage/`, `signals/arb_bridge.py`, `data/arb_observability.py`, `strategies/stat_arb_pairs.py` + `models/pairs/*` (Wave 5 — paper enabled; Johansen / Kalman / linked legs) | linked-leg execution follow-up |
| 4 | Event / news alpha | partial-trained (gated) | `strategies/event_driven.py`, `ai/news_classifier.py`, `ai/router.py`, `ai/pipeline.py`, `data/marketaux_client.py`, `data/newsapi_client.py`, `data/news_quality.py`, `ai/news_event_memory.py`, `ai/market_context.py`, `ai/fusion.py`, `graph/relationship_loader.py` (Wave 7 — disabled by default) | wiring follow-up |
| 5 | Regime modelling | partial-trained (gated) | `risk/regime_state.py` (heuristic, live), `data/regime_metrics.py`, `ai/regime.py`, `risk/regime_models.py` (Wave 4 — `HMMRegimeClassifier`, optionally wired when enabled and artefact path exists) | train/soak/activation follow-up |
| 6 | Volatility / covariance modelling | partial-trained (gated) | `data/features.py` (legacy proxies), `risk/volatility_models.py` (EWMA/GARCH/GJR), `portfolio/covariance.py` (sample + Ledoit-Wolf), `portfolio/correlation_monitor.py` (Wave 4) | wiring follow-up |
| 7 | ML meta-labelling | partial-trained (paper enabled) | `signals/meta_labeler.py`, `signals/meta_adaptation.py`, `backtest/labels.py` (triple-barrier), `backtest/validation.py` (purged CV / DSR / PBO), `signals/trained_meta_labeler.py`, `models/meta_label/*`, `config/model_registry.yaml` (`mytbot_meta_labeler@0.1.0`) | paper-soak + retrain follow-up |
| 8 | Forecast-native ML | partial-trained (gated) | `models/forecasts/{targets,dataset,train_tabular,infer_tabular,evaluate,ensemble}.py`, `signals/forecast_bridge.py` (Wave 6 — disabled by default; wired into opportunity scoring when configured members are registered and approved) | train/soak/activation follow-up |
| 9 | Deep sequence models | partial-trained (gated; baseline floor + comparison harness) | `models/deep_sequence/*` (Wave 11 — disabled by default; `compare_against_baseline` enforces the rule that deep must beat the baseline on MSE-ratio + hit-rate margin + cost-aware net P&L OOS) | torch + real TCN/TFT impl follow-up |
| 10 | Portfolio optimisation | partial-trained (gated) | `portfolio/allocation_engine.py` (D015 softmax — live), `portfolio/optimizers.py`, `portfolio/hrp.py`, `portfolio/cvar.py`, `portfolio/kelly.py`, `portfolio/vol_targeting.py` (Wave 8 — disabled by default; switchable via YAML), plus existing D015 helpers | wiring follow-up |
| 11 | Execution cost and impact | partial-trained (gated) | `execution/router.py` (live), `execution/engine.py`, `execution/planner.py`, `execution/impact.py`, `execution/scheduler.py`, `execution/order_slicer.py`, `execution/slippage_model.py`, `execution/venue_quality.py` (Wave 9 — disabled by default; pre-flight cost gate wired in execution engine) | calibration + slicer/scheduler follow-up |
| 12 | Microstructure / LOB | partial-trained (gated) | `signals/microstructure/`, VPIN proxy in `data/features.py`, `data/orderbook_features.py`, `models/microstructure/*` (Wave 10 — disabled by default; crypto-only allow-list) | scheduler wiring follow-up |
| 13 | RL execution (future only) | none | — | Wave 11+ (gated; no live use without baseline beat) |

## Paper-Mode Stance

Paper mode should exercise every strategy sleeve that can run without a live
activation gate. As of 2026-04-27, the paper-enabled advanced strategy sleeves
are: trained meta-labelling, factor sleeve, research stat-arb pairs, and the
conservative options directional/hedging strategies. A dashboard card may still
show `Idle` when the sleeve ran but had no valid setup, no eligible holdings, no
chain/proposal data, or no recent feature window. `Disabled` should now mean a
true operator/config gate, not merely "not production-approved".

## Risk and AI invariants (cross-cutting)

- `risk/engine.py` is the only place orders are vetoed for a tradable
  signal. Verified by `tests/test_wave0_safety_lock.py`.
- `ai/router.py`, `ai/pipeline.py`, `ai/escalation.py`, `ai/regime.py`,
  `ai/news_classifier.py`, `ai/thesis_generator.py`,
  `ai/sources/`, `ai/providers/` MUST NOT import `brokers.*`. Verified by
  `tests/test_wave0_safety_lock.py` static AST check.
- `brokers/base.py` `BrokerAdapter` ABC public method signatures are
  snapshotted by the same test; intentional changes require updating the
  golden snapshot in the test deliberately.
- `paper_mode = True` is the class-level default on `BrokerAdapter`.
  Verified by `tests/test_wave0_safety_lock.py`.
- Wave 13 dashboard counters are fed at runtime from trading-loop candidate
  rows and risk/execution outcomes.
- Wave 14 activation gates are enforced by `models/registry.py` before any
  registered model can be returned for `Mode.LIVE`.

## How to update

After each wave:

1. Adjust the affected row's "Coverage" column.
2. Add or update implementing files.
3. Strike the wave number in "Gap → Wave" if the wave completed that
   category.
4. Cross-link to the model registry entry if the wave introduced a trained
   model.
