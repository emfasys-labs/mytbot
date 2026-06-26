from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from intelligence.adaptive_tuner.schema import TunableParam, TunerConfig


def _dec(v: Any, default: str) -> Decimal:
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else Decimal(default)
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def load_tuner_config(path: str | Path = "config/adaptive_tuner.yaml") -> TunerConfig:
    p = Path(path)
    if not p.exists():
        return TunerConfig(enabled=False)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return TunerConfig(enabled=False)

    params: list[TunableParam] = []
    for row in raw.get("parameters") or []:
        if not isinstance(row, dict) or not row.get("name") or not row.get("namespace"):
            continue
        lo = _dec(row.get("min"), "0")
        hi = _dec(row.get("max"), "1")
        if hi < lo:
            lo, hi = hi, lo
        params.append(
            TunableParam(
                name=str(row["name"]).strip(),
                namespace=str(row["namespace"]).strip(),
                min_value=lo,
                max_value=hi,
                step=_dec(row.get("step"), "0.05"),
                regime_conditioned=_bool(row.get("regime_conditioned"), True),
                loss_guard_direction=str(row.get("loss_guard_direction") or "none").strip().lower(),
            )
        )

    ai = raw.get("ai_advisor") or {}

    def _int(key: str, default: int, lo: int = 1) -> int:
        try:
            return max(lo, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    return TunerConfig(
        enabled=_bool(raw.get("enabled"), True),
        apply_every_n_cycles=_int("apply_every_n_cycles", 20),
        attribution_window_hours=max(0.5, float(raw.get("attribution_window_hours", 6.0) or 6.0)),
        exploration_rate=_dec(raw.get("exploration_rate"), "0.15"),
        min_samples_to_exploit=_int("min_samples_to_exploit", 8),
        regime_conditioned=_bool(raw.get("regime_conditioned"), True),
        state_path=str(raw.get("state_path") or "data/state/adaptive_tuner_state.json"),
        ai_advisor_enabled=_bool(ai.get("enabled"), True),
        ai_min_cycles_between_calls=max(1, int(ai.get("min_cycles_between_calls", 5) or 5)),
        max_recent_proposals=_int("max_recent_proposals", 50),
        params=tuple(params),
    )
