# Phase B — AI price-curve sequence forecaster

## What is shipped (code-complete, tested, INERT by default)

The full pipeline for an AI sequence forecaster to influence trade edge
exists and is proven (1076 tests green). **Nothing is active**:
`config/forecast_models.yaml` has `enabled: false` and no `sequence`
member, so the system behaves exactly as before until a model is
*trained, validated, registered and explicitly activated*.

Pipeline:

1. **Architecture** — real Bai-style causal TCN (`models/deep_sequence/tcn.py`),
   torch-gated (degrades to the Ridge baseline if torch is absent).
2. **Training** — `models/deep_sequence/train.py` (`architecture="tcn"`):
   Adam + early-stopping, then the **mandatory OOS cost-aware baseline
   comparison** (`compare_against_baseline`). `promote_eligible` is the
   honest harness verdict — never hard-set.
3. **Artefact** — `TrainedSequenceForecast` (`models/deep_sequence/artefact.py`):
   stamps `deep_beats_baseline` from the harness; **refuses to load**
   unless it beat the baseline.
4. **Bridge** — `signals/forecast_bridge.py`: a `kind: sequence` member is
   scored via `score_sequence`; `_align_sequence_to_artefact` builds the
   contract-correct `(window, n_feat)` window from the recent feature
   history; the untrained-model safety gate refuses any deep artefact
   lacking `deep_beats_baseline=True` (fails *closed* in LIVE).
5. **Feature history** — the loop attaches recent numeric-feature history
   to candidates **only when a sequence member is enabled** (gated;
   zero-overhead default).
6. **Shadow** — once enabled, the forecast flows into `forecast_*`
   metadata → the Phase-A `price_forecast` fusion signal → `fusion_shadow`
   logs (set `FUSION_SHADOW=1`) for offline AI-vs-live comparison.

**Three independent gates** mean an untrained/unvalidated model cannot
load, cannot pass the bridge, and cannot be config-activated.

## The remaining step is governed and OFFLINE — by design

Activating a real model is deliberately *not* automated. It requires real
out-of-sample evidence; the harness decides honestly whether a model is
even eligible. Steps:

### 1. Build a training dataset
```
python run_pipeline.py --training-backfill
```
This pulls the live universe tiers (`data/runtime/universe_tiers.json`,
default `core,scan`) into `feature_snapshots` using
`historical_training_backfill` from `config/data_pipeline.yaml`
(currently yfinance `1h` / `730d`) and skips news/FRED so the run is pure
price/feature history.

Useful operator variants:
```
python run_pipeline.py --training-backfill --dry-run
python run_pipeline.py --training-backfill --universe-scope core --max-symbols 50
python run_pipeline.py --training-backfill --universe-scope all --max-symbols 300
```

Then export/build leakage-safe sequence windows
(`models/deep_sequence/dataset.py::make_sequence_windows`) from the
backfilled `feature_snapshots` for your chosen symbol/timeframe/horizon.

### 2. Train + let the harness judge it
```python
from models.deep_sequence.train import train_deep_sequence_model, DeepSequenceConfig
res = train_deep_sequence_model(
    dataset=ds,
    config=DeepSequenceConfig(enabled=True, architecture="tcn"),
)
# res.promote_eligible is True ONLY if the TCN beat the Ridge baseline
# OOS on mse-ratio AND hit-rate AND cost-aware net P&L. If False: stop.
```

### 3. Package + register (only if it actually won)
```python
from models.deep_sequence.artefact import build_sequence_forecast_artefact
art = build_sequence_forecast_artefact(res, feature_specs=fs,
                                       target_kind="forward_return", horizon=H)
art.save("artifacts/models/forecast/seq_fc-0.1.0.pkl")  # .load() refuses if it didn't beat baseline
```
Add a `models:` entry to `config/model_registry.yaml` (template:
`mytbot_meta_labeler@0.1.0`) with `approval_status: paper`, the artefact's
`feature_contract_hash`, and the full `metadata.activation_gates` block.
Insert the matching `model_versions` DB row.

### 4. Wire it (still shadow until soaked)
Add to `config/forecast_models.yaml`:
```yaml
forecast_bridge:
  enabled: true
  members:
    - name: seq_fc
      version: "0.1.0"
      kind: sequence
      target_kind: forward_return
      horizon: <H>
      artifact_path: artifacts/models/forecast/seq_fc-0.1.0.pkl
```
With `FUSION_SHADOW=1`, run a **2–4 week paper soak** and review the
`fusion_shadow` logs / `model_predictions` (IC, hit-rate, calibration)
before trusting it. The registry's `require_for_mode` + activation gates
keep it fail-closed for LIVE until the soak evidence is in.

## Bottom line

The AI-trend-forecasting machinery and all safety rails are done and
proven. Promoting an actual model to influence trades is the governed,
evidence-gated step above — it is the operator's call, made on real
out-of-sample data, exactly as the discipline of this whole effort
requires. No model is active; the system is unchanged until you complete
the steps here.
