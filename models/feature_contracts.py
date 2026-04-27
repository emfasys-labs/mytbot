"""
models/feature_contracts.py
============================
Wave 1 — feature-contract hashing and as-of safety checks.

Two responsibilities:

1. ``compute_feature_hash`` — deterministic SHA-256 over the canonical
   feature list. Two contracts with the same features in different
   orders MUST hash differently — the hash also encodes the ordering
   the model was trained on, because re-ordered columns produce a
   different fitted model. This matches the governance contract in
   ``docs/MODEL_GOVERNANCE.md``.

2. ``require_as_of_safe`` — runtime guard against future-stamped
   features. Used by ``models.prediction_store.write_prediction`` and by
   any feature-loader that wants to enforce the "as_of_ts <=
   prediction_ts" invariant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from models.schemas import FeatureSpec, canonical_feature_list


def compute_feature_hash(features: list[FeatureSpec | dict[str, Any]]) -> str:
    """
    Deterministic SHA-256 hex digest over the canonical feature list.

    Order-sensitive on purpose: a model trained on columns
    ``[a, b, c]`` is not the same as a model trained on ``[c, b, a]``
    even with identical data. Mutating ``transform`` without retraining
    is also detected because the transform is part of the digest.
    """
    canonical = canonical_feature_list(features)
    payload = json.dumps(canonical, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AsOfLeakageError(ValueError):
    """Raised when a prediction's feature timestamp is in the future."""


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def require_as_of_safe(
    *,
    as_of_ts: datetime,
    prediction_ts: datetime,
    tolerance_seconds: float = 0.0,
) -> None:
    """
    Raise ``AsOfLeakageError`` if ``as_of_ts`` is after ``prediction_ts``.

    ``tolerance_seconds`` allows for clock skew in distributed feature
    pipelines but defaults to zero — the strict mode required by
    governance for live writes.
    """
    a = _as_utc(as_of_ts)
    p = _as_utc(prediction_ts)
    delta = (a - p).total_seconds()
    if delta > tolerance_seconds:
        raise AsOfLeakageError(
            f"as_of_ts ({a.isoformat()}) is after prediction_ts "
            f"({p.isoformat()}) by {delta:.3f}s "
            f"(tolerance={tolerance_seconds}s). Future-stamped features "
            "would leak forward information into a live prediction."
        )
