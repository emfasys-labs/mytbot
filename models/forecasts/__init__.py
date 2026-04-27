"""
models/forecasts/
==================
Wave 6 — forecast-native structured ML.

Public surface (kept narrow):

- ``forward_return``, ``breakout_continuation``, ``mean_reversion_success``,
  ``realised_vol_forward``, ``drawdown_probability`` — leakage-safe target
  builders.
- ``ForecastDataset``, ``build_forecast_dataset_from_close``.
- ``TrainedForecastModel``, ``train_forecast_model``.
- ``ForecastResult``, ``score_forecast``.
- ``compute_information_coefficient``, ``compute_hit_rate_after_costs``,
  ``compute_calibration_summary``.
- ``ForecastEnsemble``, ``EnsembleResult``.

The runtime call site lives in ``signals/forecast_bridge.py`` so this
package stays import-light (numpy + pandas only; sklearn optional).
"""

from models.forecasts.dataset import (
    ForecastDataset,
    build_forecast_dataset_from_close,
)
from models.forecasts.ensemble import (
    EnsembleMember,
    ForecastEnsemble,
    EnsembleResult,
)
from models.forecasts.evaluate import (
    compute_calibration_summary,
    compute_hit_rate_after_costs,
    compute_information_coefficient,
)
from models.forecasts.infer_tabular import (
    ForecastResult,
    score_forecast,
)
from models.forecasts.targets import (
    TARGET_KINDS,
    breakout_continuation,
    drawdown_probability,
    forward_return,
    mean_reversion_success,
    realised_vol_forward,
)
from models.forecasts.train_tabular import (
    ForecastEvalReport,
    TrainedForecastModel,
    train_forecast_model,
)

__all__ = [
    "EnsembleMember",
    "EnsembleResult",
    "ForecastDataset",
    "ForecastEnsemble",
    "ForecastEvalReport",
    "ForecastResult",
    "TARGET_KINDS",
    "TrainedForecastModel",
    "breakout_continuation",
    "build_forecast_dataset_from_close",
    "compute_calibration_summary",
    "compute_hit_rate_after_costs",
    "compute_information_coefficient",
    "drawdown_probability",
    "forward_return",
    "mean_reversion_success",
    "realised_vol_forward",
    "score_forecast",
    "train_forecast_model",
]
