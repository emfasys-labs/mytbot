# mytbot_meta_labeler 0.1.0 Performance

This first model is a conservative logistic baseline trained from the audit DB.

Observed lift:

- Base rate: 0.4176922298
- Hit rate at threshold: 0.4649851667
- Absolute lift: approximately 4.7 percentage points
- Runtime threshold for first paper soak: 0.42 default, with mode-specific
  thresholds in `config/meta_labeler.yaml`.

Known limitations:

- Only 3,679 leakage-safe rows.
- Training history covers roughly 2026-04-20 to 2026-04-27.
- Strategy mix is concentrated in `mean_reversion`.
- No post-soak realised PnL attribution exists yet.
- No execution-cost-adjusted model acceptance decision exists yet.

Paper-soak objective:

Use this model to measure skip/keep behaviour, risk rejections, churn, and
execution quality in paper. Do not promote without a fresh validation report
after more data accumulates.
