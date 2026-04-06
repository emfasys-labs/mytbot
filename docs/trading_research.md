# trading_research.md

Consolidated implementation notes derived from the external report:
`Predictive and decision-making technologies for multi-asset algorithmic trading`.

This doc captures the parts that materially affect `mytbot` architecture and milestone scope.

## Executive takeaway

- Core architecture is validated and does **not** require a rewrite.
- Milestones M1–M8 remain the same sequence.
- The report increases implementation depth inside M2–M7 (features, validation, risk math, execution realism, hybrid AI routing).

## Mandatory upgrades from research

1. **M2 fractional differencing** (`fracdiff`) before ML feature engineering.
2. **M3 purged/combinatorial CV** (`timeseriescv`) instead of standard k-fold.
3. **M5 square-root impact model** in backtest and live execution quality reporting.

## M1–M8 impact map

### M1 (Foundation)
- No structural changes.
- Keep adapter pattern and paper-by-default operations.

### M2 (Data pipeline)
- Add/expand: fractional differencing, Hurst exponent, GARCH forecasts, VPIN, funding rates.
- Keep existing feature store; enrich schema payload fields progressively.

### M3 (Strategy + validation)
- Add mandatory anti-overfitting gates: purged CV, triple barrier labels, DSR, PBO.
- Prioritise volatility-managed momentum and meta-labeling.

### M4 (Risk)
- Upgrade sizing from fixed fraction to half-Kelly style controls.
- Add CVaR and correlation-regime stress handling.

### M5 (Execution)
- Add explicit cost decomposition and square-root market impact.
- Add Almgren-Chriss style scheduling for larger parent orders.

### M6 (AI)
- Move toward hybrid routing:
  - local high-volume sentiment/classification
  - API for complex reasoning and high-value decisions

### M7 (Monitoring)
- Surface DSR, PBO, and model-degradation alerts in dashboard.

### M8 (Micro-live)
- Incremental strategy/broker expansion remains valid.
- Add higher-complexity sleeves after validation gates pass.

## Architecture impact summary

No new top-level layer is required. Existing layers remain:
Data ingestion -> Feature store -> Strategy -> Signal -> Risk -> Execution -> Observability.

Changes are **inside layers**:
- richer M2 feature engineering,
- stricter M3 validation,
- stronger M4 risk mathematics,
- more realistic M5 execution cost modeling,
- hybrid M6 inference routing.

## Dependency planning note

The imported research dependency set is stored in:
- `docs/requirements_research.txt`

This is a planning reference, not an automatic replacement of runtime pins.
Adopt new dependencies milestone-by-milestone to avoid destabilizing production paths.
