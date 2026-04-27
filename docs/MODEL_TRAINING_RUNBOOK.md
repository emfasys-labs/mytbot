# MODEL_TRAINING_RUNBOOK.md

This repo now has a research/paper-only model training conductor:

```powershell
python scripts\auto_train_models.py --plan-only
python scripts\auto_train_models.py
```

The plan is controlled by `config/auto_training.yaml`.

## What It Trains

- `meta_labeler`: rebuilds the local audit-log dataset and trains the next
  meta-label artefact.
- `forecasts`: exports as-of-safe `feature_snapshots` rows to CSV and trains
  configured tabular forecast targets.
- `regime_classifier`: trains an HMM classifier if the configured regime feature
  CSV exists.
- `microstructure`: trains the order-book forecaster only if the configured LOB
  CSV exists.

## Safety Rules

- The conductor never promotes any model to `micro_live` or `live`.
- It writes artefacts under `artifacts/models/`.
- It writes run reports under `reports/models/auto_training/`.
- Missing data is a skip, not a failure.
- Paper registration stays explicit. The current default is to train and report,
  then let the operator inspect metrics before adding a paper registry entry.

## Windows Automation

To schedule daily research/paper training:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_auto_training_task.ps1
```

The default task name is `mytbot-auto-training`, scheduled daily at `03:20`
local time. The task runs:

```powershell
python scripts\auto_train_models.py
```

## Promotion Flow

1. Review the JSON report in `reports/models/auto_training/`.
2. Review any model-specific validation output printed by the underlying trainer.
3. Add the chosen model to `config/model_registry.yaml` as `approval_status:
   paper`.
4. Insert/update the matching DB `model_versions` row.
5. Enable the runtime config member, for example `config/forecast_models.yaml`.
6. Paper soak for 2-4 weeks before any micro-live discussion.
