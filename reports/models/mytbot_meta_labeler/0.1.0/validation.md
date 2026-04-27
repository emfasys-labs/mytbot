# mytbot_meta_labeler 0.1.0 Validation

Status: paper candidate only.

Training command:

```powershell
python scripts\build_meta_label_dataset.py --out-dir data/research/meta_label/20260427_meta_label_v0_1_0 --min-rows 200
python scripts\train_meta_labeler.py --features data\research\meta_label\20260427_meta_label_v0_1_0\features.csv --labels data\research\meta_label\20260427_meta_label_v0_1_0\labels.csv --out artifacts\models\meta_label\mytbot_meta_labeler-0.1.0.pkl --classifier logreg --calibration platt --n-splits 5 --embargo-bars 10
```

Dataset:

- Rows: 3,679
- Timeframe: 1h
- Horizon: 10 bars
- Profit barrier: 2.0 x rolling volatility
- Stop barrier: 1.5 x rolling volatility
- Feature contract hash: `8615c3cf26f8d684df3ebd90411af60039c380bd7ef154fd2b61be0cee1ab955`
- Label counts: 0 = 2,142; 1 = 1,537

Model:

- Classifier: logistic regression fallback
- Calibration: Platt
- Validation: purged k-fold with 10-bar embargo

Metrics:

- Brier mean: 0.2607230002
- Log loss mean: 0.7218351618
- Hit rate at threshold mean: 0.4649851667
- Base rate mean: 0.4176922298
- Folds: 5
- Paper-soak runtime threshold: 0.42 default. This is intentionally mild for
  the first soak so the model measures behaviour without starving the loop.

Calibration:

- ECE: 0.0711
- Bin 0.25: predicted 0.294, observed 0.032, n=94
- Bin 0.35: predicted 0.362, observed 0.267, n=1226
- Bin 0.45: predicted 0.443, observed 0.450, n=1943
- Bin 0.55: predicted 0.520, observed 0.732, n=310
- Bin 0.65: predicted 0.625, observed 1.000, n=106

Conclusion:

This is acceptable only as a first paper-soak baseline. It shows modest lift
over the base rate, but history is short and dominated by `mean_reversion`.
It is not suitable for micro-live or live promotion without a clean paper-soak
window and a broader retrain.
