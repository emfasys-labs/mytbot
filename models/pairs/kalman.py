"""
models/pairs/kalman.py
========================
Wave 5 — Kalman filter for time-varying hedge ratio.

State-space model:

    state:        [intercept_t, beta_t]
    transition:   identity (random walk on the parameters)
    observation:  y_t = intercept_t + beta_t * x_t + ε_t,
                  ε_t ~ N(0, R)

Process noise ``Q`` is a small diagonal matrix; observation noise
``R`` is scalar. The defaults are tuned for daily / hourly equity
pairs and should be adjusted with operator domain knowledge.

Online updates: ``KalmanHedgeRatio`` exposes ``update(y, x)`` so the
filter can be run iteratively over a streaming series, or batched via
``run(y_series, x_series)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class KalmanState:
    intercept: float
    beta: float
    cov: np.ndarray  # 2x2 state covariance


@dataclass
class KalmanHedgeRatio:
    process_noise_intercept: float = 1e-5
    process_noise_beta: float = 1e-5
    observation_noise: float = 1e-3
    initial_intercept: float = 0.0
    initial_beta: float = 1.0
    initial_state_var: float = 1.0
    state: Optional[KalmanState] = None

    def reset(self) -> None:
        self.state = KalmanState(
            intercept=float(self.initial_intercept),
            beta=float(self.initial_beta),
            cov=float(self.initial_state_var) * np.eye(2),
        )

    def _ensure_state(self) -> KalmanState:
        if self.state is None:
            self.reset()
        return self.state  # type: ignore[return-value]

    def update(self, y: float, x: float) -> KalmanState:
        """
        Run one filter step. Returns the *posterior* state. The caller
        gets ``state.intercept`` and ``state.beta`` for the current
        timestamp.
        """
        st = self._ensure_state()
        # Predict (random walk: state unchanged).
        Q = np.array(
            [
                [self.process_noise_intercept, 0.0],
                [0.0, self.process_noise_beta],
            ]
        )
        cov_pred = st.cov + Q

        # Observation: H = [1, x_t].
        H = np.array([1.0, float(x)])
        R = float(self.observation_noise)
        y_hat = float(H @ np.array([st.intercept, st.beta]))
        innovation = float(y) - y_hat
        S = float(H @ cov_pred @ H + R)
        if S <= 0 or not np.isfinite(S):
            # Skip update on numerical pathology.
            return st
        K = (cov_pred @ H) / S
        new_state = np.array([st.intercept, st.beta]) + K * innovation
        new_cov = cov_pred - np.outer(K, H @ cov_pred)
        # Symmetrise.
        new_cov = 0.5 * (new_cov + new_cov.T)
        self.state = KalmanState(
            intercept=float(new_state[0]),
            beta=float(new_state[1]),
            cov=new_cov,
        )
        return self.state

    def run(
        self,
        y_series: pd.Series,
        x_series: pd.Series,
    ) -> pd.DataFrame:
        """
        Batch-run the filter over aligned ``y`` and ``x`` series.

        Returns a DataFrame indexed like ``y_series`` with columns
        ``intercept``, ``beta``, ``var_intercept``, ``var_beta``.
        """
        df = pd.concat([y_series.astype(float), x_series.astype(float)], axis=1, join="inner")
        df.columns = ["y", "x"]
        if df.empty:
            return pd.DataFrame(columns=["intercept", "beta", "var_intercept", "var_beta"])
        self.reset()
        out_intercept: list[float] = []
        out_beta: list[float] = []
        out_var_a: list[float] = []
        out_var_b: list[float] = []
        for y, x in df.itertuples(index=False, name=None):
            st = self.update(float(y), float(x))
            out_intercept.append(st.intercept)
            out_beta.append(st.beta)
            out_var_a.append(float(st.cov[0, 0]))
            out_var_b.append(float(st.cov[1, 1]))
        return pd.DataFrame(
            {
                "intercept": out_intercept,
                "beta": out_beta,
                "var_intercept": out_var_a,
                "var_beta": out_var_b,
            },
            index=df.index,
        )
