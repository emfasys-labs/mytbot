# MODEL_GOVERNANCE.md

**Status:** Wave 0 baseline (2026-04-27).
**Scope:** How trained models are introduced, registered, validated, deployed,
monitored, and retired in mytbot.

This document is a contract. A model that does not satisfy these rules MUST
NOT be allowed to influence live trading.

## Non-negotiable governance rules

1. **No model runs live without registration.** The model registry
   (`models/registry.py`, landed in Wave 1) is the only gateway. A model that
   is not registered and `approved` cannot be referenced by signal,
   opportunity, allocation, or execution code paths in live mode.
2. **Every model declares a contract.** Required fields:
   - `name`, `version` (semver), `task` (classification | regression),
     `target` (e.g. `triple_barrier_outcome`, `forward_return_1h`),
   - `horizon` (timedelta or bar count),
   - `feature_list` (frozen ordering),
   - `feature_hash` (deterministic hash over name + dtype + transform of
     each feature column),
   - `training_window` (start, end),
   - `validation_method` (`purged_kfold`, `walk_forward`, etc., with
     `n_splits` and `embargo_bars`),
   - `calibration_method` (`isotonic` | `platt` | `none`),
   - `min_sample_size`,
   - `approval_status` (`research` | `paper` | `micro_live` | `retired`),
   - `created_at`, `created_by`,
   - `notes` / `risks`.
3. **Features must be as-of safe.** Feature timestamps must be strictly less
   than the prediction timestamp. Future-stamped rows MUST be rejected at
   load time. Wave 0 does not implement loading; Wave 1 enforces this.
4. **Predictions are auditable.** Every live prediction stored in
   `model_predictions` must include `model_name`, `model_version`,
   `feature_hash`, `as_of_ts`, `prediction_ts`, `symbol`, `horizon`,
   `predicted_probability` and/or `expected_return`, `expected_volatility`,
   `confidence`, `mode` (`research` | `paper` | `live`), and free-form
   `metadata`.
5. **AI cannot execute.** The escalation chain in `ai/router.py` and the
   pipeline in `ai/pipeline.py` must not import `brokers.*` and must not
   call execution primitives. Wave 0 safety-lock tests enforce this.
6. **Risk engine is final.** Even an `approved` model's recommendation only
   modulates `Signal.confidence`, `Opportunity.confidence`, target notional,
   or skip reason. The veto in `risk/engine.py` is independent and absolute.

## Lifecycle

`research` → `paper` → `micro_live` → `live` → `retired`.

Promotion gates (cumulative — earlier gates must remain green):

- **research → paper.**
  - Feature contract is stable and frozen.
  - Validation report exists in `reports/models/<name>/<version>/`.
  - Out-of-sample metric ≥ baseline (information coefficient, Brier,
    PR-AUC, or hit rate after costs depending on task).
  - Calibration plot attached if classification.
  - `paper` row in `model_registry.yaml` and DB `model_versions`.
- **paper → micro_live.**
  - 2-4 weeks paper soak under D015 with no anomalous behaviour
    (turnover, churn, drawdown, rejection profile).
  - Risk-rejection profile reviewed and understood.
  - Execution cost report shows model edge survives slippage + fees.
  - Rollback flag exists; toggling it back to `paper` reverts behaviour.
- **micro_live → live.** Only after a clean micro-live window per
  `docs/M8_MICRO_LIVE.md`.

Demotion is automatic on:

- feature freshness breach (any required feature stale beyond contract),
- calibration drift beyond threshold,
- realised slippage > 1.5x model assumption for N consecutive sessions,
- risk-rejection rate spike beyond pre-set band,
- DB write failure for predictions (model goes `paper` immediately).

## Storage (landed in Wave 1)

Tables (Alembic migration in Wave 1):

- `model_runs` — one row per training/eval invocation.
- `model_versions` — registered, immutable model artefacts.
- `model_predictions` — one row per live or paper prediction.
- `feature_contracts` — frozen feature lists with hash.
- `training_datasets` — pointer to dataset snapshot used.

## Reports

`reports/models/<name>/<version>/`:

- `validation.md` — purged CV / walk-forward, DSR/PBO summary.
- `calibration.png` — reliability curve.
- `performance.md` — IC, hit rate, turnover, P&L net of costs.
- `risk_interaction.md` — rejection rate, mode breakdown.
- `feature_importance.md` — per-feature contribution.

## Approval

Approval is a manual flip in `config/model_registry.yaml` and a matching
DB row update. Wave 0 leaves the YAML stub empty. Wave 1 wires loading.

## Audit hooks

- Any prediction-emitting module must call `prediction_store.write()`
  (Wave 1) — failure to write is a hard error in `live`, a logged warning
  in `paper`, ignored in `research`.
- `dashboard/` reads `model_predictions` for the Wave 13 model-health panel.
