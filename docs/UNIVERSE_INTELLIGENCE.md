# Universe Intelligence Layer

Optional module that sits **above** the data pipeline and dynamic tier files. It does **not** execute trades, bypass `RiskEngine`, or modify `brokers/base.py`.

## Goals

- Compress a large candidate pool into **non-redundant representatives** for directional work while **retaining** cluster members for cheap scans and pair-style monitoring.
- Expose a **single JSON snapshot** for the dashboard (`GET /intelligence/universe`).
- **Fail open**: if the layer is disabled or persistence is missing, behaviour falls back to `config/data_pipeline.yaml` and `data/runtime/universe_tiers.json` (existing M2 / ranking path).

## Layout

| Path | Role |
|------|------|
| `config/universe_selection.yaml` | Feature flag, filters, promotion thresholds, cluster params |
| `universe/*.py` | Eligibility, correlation graph, clustering, representatives, promotion rules, snapshot builder |
| `data/runtime/universe_tiers.json` | Existing **core / scan / light** lists (ranking loop) |
| `data/runtime/universe_intelligence.json` | Optional clusters + cold/active/core hints (build script) |

## Tiers (intelligence model)

1. **candidate** — all monitorable names (broker catalogue × config).
2. **cold_scan** — large cheap universe (light tier + non-representatives after clustering).
3. **active** — names evaluated by the full engine (scan + core from `universe_tiers.json`).
4. **core** — cluster representatives + configured core list.

Correlated assets are **not** deleted: one **representative** is chosen per high-correlation cluster; other members remain in **cold_scan** for anomaly-driven promotion.

## Operations

1. Keep **`enabled: false`** until you are ready; the API still returns a coherent funnel derived from pipeline + tiers.
2. When enabled, run periodically (e.g. daily):

   ```bash
   python scripts/build_universe_tiers.py
   ```

   This reads `universe_tiers.json`, pulls recent daily closes via yfinance (up to `cluster_max_symbols`), builds correlation clusters, and writes `universe_intelligence.json`.

3. Dashboard **Universe** tab calls `GET /intelligence/universe`.

## Decimal policy

Eligibility uses `Decimal` for ADV / notional / spread limits where those fields are provided. Correlation code uses float returns (statistical layer only).

## Related decisions

- Complements D015 / dynamic universe caps in `config/data_pipeline.yaml`.
- Pair and relative-value logic remains in strategies; this layer only shapes **which symbols** surface for deep vs light processing.
