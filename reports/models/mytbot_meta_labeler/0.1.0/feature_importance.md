# mytbot_meta_labeler 0.1.0 Feature Contract

Feature contract hash:

`8615c3cf26f8d684df3ebd90411af60039c380bd7ef154fd2b61be0cee1ab955`

Features:

- `strategy_confidence`
- `raw_confidence`
- `side_sign`
- `news_score`
- `accumulator_score`
- `accumulator_confidence`
- `atr_pct`
- `volume_z_score`
- `demand_score`
- `demand_confidence`
- `ai_macro_confidence`
- `rsi_14`
- `mom_10`
- `fracdiff_0_4`
- `garch_vol_1d`
- `hurst_dfa_128`
- `vpin_proxy_50`
- `relative_dollar_volume`
- `volume_z`
- `vol_ratio`

Feature-importance caveat:

The environment currently lacks scikit-learn, so the first artefact uses the
NumPy logistic fallback. A richer model inspection pass should be added after
installing the research dependencies and retraining.
