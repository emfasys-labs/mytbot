# PAPER_SOAK.md

**Status:** Wave 14 baseline.
**Owner:** kvcom.
**Scope:** How a model or strategy gets promoted past `research` status.

This document is the **operator's runbook** for activation. It pairs
with `docs/MODEL_GOVERNANCE.md` (the contract the model must satisfy)
and the runtime gate code in `governance/activation_gates.py`. The
gate code is authoritative — if it disagrees with this doc, the code
wins and this doc is wrong.

## The 8 activation gates

Encoded in `governance/activation_gates.py` as
`ActivationGates.evaluate(ctx)`. **Default state: every gate fails.**
The operator must affirmatively populate each piece of the
`ActivationContext`.

| # | Gate | Field(s) | What "pass" means |
|--:|---|---|---|
| 1 | feature_contract | `feature_contract_hash`, `feature_contract_frozen=True` | Hash recorded; ordering immutable for the model version |
| 2 | model_registered | `registered_in_yaml=True`, `registered_in_db=True`, `registry_status >= target_status` | Both `config/model_registry.yaml` row and `model_versions` DB row exist with status ≥ what we're promoting to |
| 3 | validation_report | `validation_report_path` exists; optional metric ≥ threshold | A file under `reports/models/<name>/<version>/validation.md` is present and the OOS metric clears the operator-set threshold |
| 4 | paper_soak | `paper_soak_start` set; days elapsed ≥ `paper_soak_min_days`; `paper_soak_anomalies == []` | 2-4 weeks of paper run with no flagged anomaly. Skipped when `target_status == "paper"` (the soak is *into* paper, not before it) |
| 5 | risk_rejection | `risk_rejection_review_signed_off=True`; `risk_rejection_rate <= max` | Operator has read the risk-rejection breakdown and signed off; rejection rate is below the configured ceiling (default 40%) |
| 6 | execution_cost | `realised_slippage_bps_p95 <= expected_slippage_bps × tolerance` | P95 slippage doesn't exceed Wave-9 model expectation × 1.5 |
| 7 | rollback | `rollback_documented=True`, `rollback_test_passed=True` | A documented procedure exists AND has been exercised |
| 8 | config_flag | `config_flag_path`, `config_flag_value=True` | The relevant per-strategy YAML gate is on AND points to a real path |

`ActivationVerdict.cleared_for_activation` is `True` only when *all*
gates pass. The verdict is decomposable: `verdict.failed_gates`
returns the specific gates that blocked promotion.

## Lifecycle (mirrors `docs/MODEL_GOVERNANCE.md`)

```
research → paper → micro_live → live → retired
```

- **research → paper**: gates 1-3, 5-8 (gate 4 is N/A here).
- **paper → micro_live**: all 8 gates.
- **micro_live → live**: all 8 gates plus the operator's existing
  `docs/M8_MICRO_LIVE.md` gates.

Demotion is automatic on the conditions in
`docs/MODEL_GOVERNANCE.md` (feature freshness breach, calibration
drift, slippage > 1.5× model assumption for N consecutive sessions,
etc.).

## The six paper-soak reports

`scripts/build_paper_soak_reports.py` writes:

```
reports/paper_soak/
    model_health.md
    strategy_attribution.md
    execution_quality.md
    risk_rejections.md
    d015_replacement_behaviour.md
    drawdown_report.md
    _summary.md
```

The reports pull from the Wave-13 dashboard payload (`build_wave13_payload`)
plus operator-supplied JSON files for risk-rejection breakdowns and
drawdown metrics. The renderer is pure Python — no Jinja / external
templating.

## Runbook

1. **Train + register**.
   ```
   python scripts/train_meta_labeler.py ...                 # or train_forecasts.py / etc.
   ```
   Add an entry to `config/model_registry.yaml` with
   `approval_status: research` and a matching `model_versions` DB row.

2. **Validate offline**.
   Produce `reports/models/<name>/<version>/validation.md`.
   Confirm the OOS metric clears your threshold.

3. **Promote to paper** (gate 4 skipped at this step).
   Update YAML status → `paper`; check the registry sync.

4. **Soak** for 2-4 weeks.
   Watch the Wave-13 dashboard funnel. Note any anomalies in
   `paper_soak_anomalies`. Calibrate Wave-9 cost coefficients
   against realised slippage (`scripts/evaluate_execution_quality.py`).

5. **Build the report bundle**.
   ```
   python scripts/build_paper_soak_reports.py \
       --model my_model --model-version 0.1.0 \
       --soak-started 2026-04-01T00:00:00+00:00
   ```
   Read every section. Sign off the risk-rejection review.

6. **Promote to micro_live**.
   Build an `ActivationContext` and call
   `evaluate_activation(ctx)`. **Refuse to flip the YAML if any
   gate fails.**

7. **Operate**.
   Wave-13 dashboard renders block reasons in real time
   (`/dashboard/wave13`). Demotion triggers in
   `docs/MODEL_GOVERNANCE.md` apply continuously.

## Why gates are code, not docs

Process documents drift. Code is enforced. By making the gates
runtime checks (`governance/activation_gates.py`) we ensure that:

- A future operator cannot accidentally flip a flag without satisfying
  every gate.
- The verdict is auditable — each gate emits a structured
  `ActivationGateResult` with `passed`, `reason`, and `details`.
- The aggregate `cleared_for_activation` is a single boolean an
  admin endpoint or deploy script can consume.

This module deliberately does not place orders, query brokers, or
mutate state. It is a verdict generator.
