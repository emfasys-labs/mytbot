# mytbot_meta_labeler 0.1.0 Risk Interaction

The model is allowed to affect signal keep/skip decisions only. It does not
place orders and does not bypass `RiskEngine`.

Current status:

- Risk-review sign-off: not complete.
- Paper-soak rejection profile: not available yet.
- Activation gate for risk review: intentionally not cleared.

During paper soak, track:

- Risk rejection rate for kept model candidates.
- Risk rejection reasons by strategy and symbol.
- Whether model-kept trades concentrate in one risk failure mode.
- Whether model-skipped trades would have been risk-approved.

Promotion blocker:

Risk rejection behaviour must be reviewed and signed off before micro-live or
live promotion.
