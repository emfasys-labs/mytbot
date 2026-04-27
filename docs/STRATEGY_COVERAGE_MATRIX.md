# STRATEGY_COVERAGE_MATRIX.md

**Status:** Wave 0 baseline (2026-04-27).

Coverage of mainstream systematic-trading capability families. Each row lists
the implementing files (today) and the wave that closes the gap. "Coverage"
is one of: **none**, **partial-heuristic**, **partial-trained**, **full**.

| # | Category | Coverage | Implementing files (today) | Gap → Wave |
|--:|---|---|---|---|
| 1 | Rule-based time-series alpha | partial-heuristic | `strategies/momentum.py`, `strategies/mean_reversion.py`, `strategies/volatility_regime.py`, `strategies/regime_rotation.py`, `strategies/event_driven.py`, `strategies/volume_flow.py`, `signals/engine.py`, `signals/opportunity_engine.py`, `signals/accumulator.py` | Calibrate via meta-label → Wave 2 |
| 2 | Cross-sectional factor alpha | partial-trained (gated) | `strategies/factor_sleeve.py`, `signals/factor_scoring.py`, `data/factor_features.py`, `data/fundamental_features.py` (Wave 3 — disabled by default; awaits universe-pipeline wiring) | wiring follow-up |
| 3 | Relative value | partial-trained (gated) | `strategies/pairs_trading.py` (heuristic, live), `strategies/arbitrage/`, `signals/arb_bridge.py`, `data/arb_observability.py`, `strategies/stat_arb_pairs.py` + `models/pairs/*` (Wave 5 — disabled by default; Johansen / Kalman / linked legs) | wiring follow-up |
| 4 | Event / news alpha | partial-trained (gated) | `strategies/event_driven.py`, `ai/news_classifier.py`, `ai/router.py`, `ai/pipeline.py`, `data/marketaux_client.py`, `data/newsapi_client.py`, `data/news_quality.py`, `ai/news_event_memory.py`, `ai/market_context.py`, `ai/fusion.py`, `graph/relationship_loader.py` (Wave 7 — disabled by default) | wiring follow-up |
| 5 | Regime modelling | partial-trained (gated) | `risk/regime_state.py` (heuristic, live), `data/regime_metrics.py`, `ai/regime.py`, `risk/regime_models.py` (Wave 4 — `HMMRegimeClassifier`, disabled by default) | wiring follow-up |
| 6 | Volatility / covariance modelling | partial-trained (gated) | `data/features.py` (legacy proxies), `risk/volatility_models.py` (EWMA/GARCH/GJR), `portfolio/covariance.py` (sample + Ledoit-Wolf), `portfolio/correlation_monitor.py` (Wave 4) | wiring follow-up |
| 7 | ML meta-labelling | partial-heuristic | `signals/meta_labeler.py`, `signals/meta_adaptation.py`, `backtest/labels.py` (triple-barrier), `backtest/validation.py` (purged CV / DSR / PBO) | Wave 2 (`signals/trained_meta_labeler.py`, `models/meta_label/*`) |
| 8 | Forecast-native ML | partial-trained (gated) | `models/forecasts/{targets,dataset,train_tabular,infer_tabular,evaluate,ensemble}.py`, `signals/forecast_bridge.py` (Wave 6 — disabled by default; awaits trained members + opportunity-engine wiring) | wiring follow-up |
| 9 | Deep sequence models | partial-trained (gated; baseline floor + comparison harness) | `models/deep_sequence/*` (Wave 11 — disabled by default; `compare_against_baseline` enforces the rule that deep must beat the baseline on MSE-ratio + hit-rate margin + cost-aware net P&L OOS) | torch + real TCN/TFT impl follow-up |
| 10 | Portfolio optimisation | partial-trained (gated) | `portfolio/allocation_engine.py` (D015 softmax — live), `portfolio/optimizers.py`, `portfolio/hrp.py`, `portfolio/cvar.py`, `portfolio/kelly.py`, `portfolio/vol_targeting.py` (Wave 8 — disabled by default; switchable via YAML), plus existing D015 helpers | wiring follow-up |
| 11 | Execution cost and impact | partial-trained (gated) | `execution/router.py` (live), `execution/engine.py`, `execution/planner.py`, `execution/impact.py`, `execution/scheduler.py`, `execution/order_slicer.py`, `execution/slippage_model.py`, `execution/venue_quality.py` (Wave 9 — disabled by default) | router/engine wiring follow-up |
| 12 | Microstructure / LOB | partial-trained (gated) | `signals/microstructure/`, VPIN proxy in `data/features.py`, `data/orderbook_features.py`, `models/microstructure/*` (Wave 10 — disabled by default; crypto-only allow-list) | scheduler wiring follow-up |
| 13 | RL execution (future only) | none | — | Wave 11+ (gated; no live use without baseline beat) |

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

## How to update

After each wave:

1. Adjust the affected row's "Coverage" column.
2. Add or update implementing files.
3. Strike the wave number in "Gap → Wave" if the wave completed that
   category.
4. Cross-link to the model registry entry if the wave introduced a trained
   model.
