from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from intelligence.trade_admission.schema import AdmissionConfig


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _dec01(v: Any, default: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        d = Decimal(default)
    if not d.is_finite():
        d = Decimal(default)
    return max(Decimal("0"), min(Decimal("1"), d))


def load_admission_config(path: str | Path = "config/trade_admission.yaml") -> AdmissionConfig:
    p = Path(path)
    if not p.exists():
        return AdmissionConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return AdmissionConfig()
    horizons_raw = raw.get("outcome_horizons_minutes", AdmissionConfig.outcome_horizons_minutes)
    horizons: list[int] = []
    if isinstance(horizons_raw, (list, tuple)):
        for h in horizons_raw:
            try:
                n = int(h)
            except (TypeError, ValueError):
                continue
            if n > 0:
                horizons.append(n)
    try:
        since = float(raw.get("diagnostics_since_hours", 24.0))
    except (TypeError, ValueError):
        since = 24.0
    try:
        cap = int(raw.get("max_rows_per_cycle", 500))
    except (TypeError, ValueError):
        cap = 500

    def _int(key: str, default: int, lo: int = 1) -> int:
        try:
            return max(lo, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    return AdmissionConfig(
        enabled=_bool(raw.get("enabled"), True),
        shadow_only=_bool(raw.get("shadow_only"), True),
        block_new_opens=_bool(raw.get("block_new_opens"), False),
        allow_size_haircuts=_bool(raw.get("allow_size_haircuts"), False),
        diagnostics_since_hours=max(1.0, since),
        outcome_horizons_minutes=tuple(horizons or AdmissionConfig.outcome_horizons_minutes),
        max_rows_per_cycle=max(1, cap),
        model_enabled=_bool(raw.get("model_enabled"), True),
        model_min_bucket_samples=_int("model_min_bucket_samples", 25),
        model_refresh_minutes=_int("model_refresh_minutes", 30),
        model_lookback_days=_int("model_lookback_days", 30),
        directional_news_weight=_dec01(raw.get("directional_news_weight"), "0.50"),
    )
