"""
models/microstructure/imbalance.py
====================================
Wave 10 — runtime helper for the LOB imbalance forecaster.

``score_orderbook(snapshot, artefact, ...)`` is the single call site
the execution scheduler / opportunity engine will hit (post-wiring).
It returns ``None`` when the snapshot is stale or malformed, so a
broken upstream feed quietly disables the model rather than producing
garbage forecasts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from data.orderbook_features import (
    OrderbookSnapshot,
    build_orderbook_features,
    is_book_well_formed,
    quote_staleness_seconds,
)

logger = logging.getLogger(__name__)


@dataclass
class LOBForecastResult:
    """Per-snapshot LOB verdict for the dashboard / scheduler."""

    used: bool
    reason: str           # "ok" | "stale" | "malformed" | "missing_artefact" | "predict_failed"
    probability_up: Optional[float] = None
    imbalance_signal: Optional[float] = None  # signed in [-1, 1]
    spread_bps: Optional[float] = None
    quote_staleness: Optional[float] = None
    metadata: dict = field(default_factory=dict)


def score_orderbook(
    snapshot: OrderbookSnapshot,
    artefact,                     # TrainedLOBForecaster | None
    *,
    max_staleness_seconds: float = 5.0,
    depth: int = 5,
    now: Optional[datetime] = None,
) -> LOBForecastResult:
    """
    Compute features, gate on freshness + book health, then score via
    the artefact. ``artefact=None`` returns a "missing_artefact" result
    so the caller can degrade to its baseline behaviour.
    """
    ref = now or datetime.now(timezone.utc)
    staleness = quote_staleness_seconds(snapshot, now=ref)
    if not is_book_well_formed(snapshot):
        return LOBForecastResult(
            used=False, reason="malformed", quote_staleness=staleness
        )
    if staleness > float(max_staleness_seconds):
        return LOBForecastResult(
            used=False,
            reason="stale",
            quote_staleness=staleness,
            metadata={"max_staleness_seconds": float(max_staleness_seconds)},
        )

    feats = build_orderbook_features(snapshot, depth=depth, now=ref)
    spread = feats.get("spread_bps")
    imbalance = feats.get("top_of_book_imbalance")

    if artefact is None:
        return LOBForecastResult(
            used=False,
            reason="missing_artefact",
            spread_bps=spread,
            imbalance_signal=imbalance,
            quote_staleness=staleness,
        )

    cols = [s.name for s in artefact.feature_specs]
    row = []
    for c in cols:
        v = feats.get(c)
        row.append(float(v) if v is not None else 0.0)
    try:
        prob = float(np.asarray(artefact.predict(np.asarray(row, dtype=float))).ravel()[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("models/microstructure | score_orderbook predict failed: %s", exc)
        return LOBForecastResult(
            used=False,
            reason="predict_failed",
            spread_bps=spread,
            imbalance_signal=imbalance,
            quote_staleness=staleness,
            metadata={"error": str(exc)},
        )

    return LOBForecastResult(
        used=True,
        reason="ok",
        probability_up=prob,
        imbalance_signal=imbalance,
        spread_bps=spread,
        quote_staleness=staleness,
    )
