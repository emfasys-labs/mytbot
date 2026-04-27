# STRATEGY_COVERAGE_MATRIX.md

**Status:** Wave 0 baseline (2026-04-27).

Coverage of mainstream systematic-trading capability families. Each row lists
the implementing files (today) and the wave that closes the gap. "Coverage"
is one of: **none**, **partial-heuristic**, **partial-trained**, **full**.

| # | Category | Coverage | Implementing files (today) | Gap → Wave |
|--:|---|---|---|---|
| 1 | Rule-based time-series alpha | partial-heuristic | `strategies/momentum.py`, `strategies/mean_reversion.py`, `strategies/volatility_regime.py`, `strategies/regime_rotation.py`, `strategies/event_driven.py`, `strategies/volume_flow.py`, `signals/engine.py`, `signals/opportunity_engine.py`, `signals/accumulator.py` | Calibrate via meta-label → Wave 2 |
| 2 | Cross-sectional factor alpha | partial-trained (gated) | `strategies/factor_sleeve.py`, `signals/factor_scoring.py`, `data/factor_features.py`, `data/fundamental_features.py` (Wave 3 — disabled by default; awaits universe-pipeline wiring) | wiring follow-up |
| 3 | Relative value | partial-heuristic | `strategies/pairs_trading.py`, `strategies/arbitrage/`, `signals/arb_bridge.py`, `data/arb_observability.py` | Wave 5 (Johansen + Kalman + linked legs in `models/pairs/`) |
| 4 | Event / news alpha | partial-heuristic | `strategies/event_driven.py`, `ai/news_classifier.py`, `ai/router.py`, `ai/pipeline.py`, `data/marketaux_client.py`, `data/newsapi_client.py`, `data/news_quality.py` | Wave 7 (multimodal fusion: `ai/fusion.py`, `ai/news_event_memory.py`, `graph/dependency_graph.py`) |
| 5 | Regime modelling | partial-heuristic | `risk/regime_state.py`, `data/regime_metrics.py`, `ai/regime.py` | Wave 4 (`risk/regime_models.py`, HMM + GARCH per asset class) |
| 6 | Volatility / covariance modelling | partial-heuristic | `data/features.py` (GARCH-style proxy, ATR), `signals/volume_anomaly.py` | Wave 4 (`risk/volatility_models.py`, `portfolio/covariance.py` with Ledoit-Wolf, `portfolio/correlation_monitor.py`) |
| 7 | ML meta-labelling | partial-heuristic | `signals/meta_labeler.py`, `signals/meta_adaptation.py`, `backtest/labels.py` (triple-barrier), `backtest/validation.py` (purged CV / DSR / PBO) | Wave 2 (`signals/trained_meta_labeler.py`, `models/meta_label/*`) |
| 8 | Forecast-native ML | none | — | Wave 6 (`models/forecasts/*`, `signals/forecast_bridge.py`) |
| 9 | Deep sequence models | none | — | Wave 11 (`models/deep_sequence/*`, gated by tabular baseline) |
| 10 | Portfolio optimisation | partial-heuristic | `portfolio/allocation_engine.py` (D015 softmax), `portfolio/d015_hold_switch.py`, `portfolio/d015_smoothing.py`, `portfolio/d015_replacement_context.py`, `portfolio/global_edge_coordinator.py`, `portfolio/strategy_coordinator.py`, `portfolio/capital_scheduler.py`, `portfolio/treasury_manager.py`, `portfolio/allocator.py`, `portfolio/opportunity_book.py` | Wave 8 (`portfolio/optimizers.py`, `portfolio/hrp.py`, `portfolio/cvar.py`, `portfolio/kelly.py`, `portfolio/vol_targeting.py`) |
| 11 | Execution cost and impact | partial-heuristic | `execution/router.py` (fee + learned quality + demand bias), `execution/engine.py`, `execution/planner.py`, `execution/arbitrage_executor.py`, `execution/arbitrage_spot_executor.py` | Wave 9 (`execution/impact.py`, `execution/scheduler.py`, `execution/order_slicer.py`, `execution/slippage_model.py`, `execution/venue_quality.py`) |
| 12 | Microstructure / LOB | partial-heuristic | `signals/microstructure/`, VPIN proxy in `data/features.py` | Wave 10 (`data/orderbook_features.py`, `models/microstructure/*`) |
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
