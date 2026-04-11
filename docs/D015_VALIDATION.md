# D015 validation playbook (paper)

Minimum soak: **2–4 weeks** in `APP_ENV=paper` with the **primary** D015 path (default). Use `ALLOCATOR_D015_LEGACY_FALLBACK=true` only for A/B comparison or recovery.

## What to watch

1. **Turnover** — `d015_primary` log lines per iteration (`instructions`, `turnover_est`); run `scripts/d015_paper_report.py` for signal counts by strategy/symbol.
2. **Churn** — spikes when `regime` flips to `volatile` and `anomaly_breadth` is high; `replacement_logic.churn` and `min_replacement_interval_seconds` damp oscillation.
3. **Drawdown** — portfolio drawdown vs `drawdown_throttle` in regime state.
4. **Anomaly reaction** — `volume_z` / `relative_dollar_volume` in feature JSON vs opportunity score; `d015_volume_refresh` commands on `ControlCommand` when escalation fires.

## Tuning (YAML only)

- Switching friction: `config/allocation.yaml` → `replacement_logic.switching_cost`, `switching_cost_normalisation`, `thresholds.minimum_replacement_advantage`, `churn`.
- Volume sensitivity: `config/profile_modes.yaml` → `volume_anomaly_weight` and `config/data_pipeline.yaml` → `volume_flow_features`.
- Regime strictness: `config/allocation.yaml` → `market_state` weights, `liquidity_enrichment`, `min_symbols_for_regime`.
- Smoothing: `config/allocation.yaml` → `allocation_stability`.

## After sign-off (Phase 12)

- Narrow `risk_modes.yaml` to reporting/labels for UIs that still read it; numeric overlays are ignored when `allocator_d015_primary` is active.
