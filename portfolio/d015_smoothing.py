"""Soft damping of allocation targets vs prior cycle (config-driven, no hard caps)."""

from __future__ import annotations

import math
from copy import replace
from decimal import Decimal
from typing import Any

from config.models import AllocationStabilityConfig
from core.models_runtime import AllocationDecision, AllocationTarget, clip_decimal


def allocation_smoothing_snapshot(decision: AllocationDecision) -> dict[str, Any]:
    return {
        "gross_exposure_target": str(decision.gross_exposure_target),
        "weights_by_symbol": {t.symbol: str(t.target_weight) for t in decision.allocation_targets},
    }


def apply_allocation_smoothing(
    decision: AllocationDecision,
    *,
    prev: dict[str, Any] | None,
    stability_cfg: AllocationStabilityConfig,
    nav: Decimal,
) -> AllocationDecision:
    if not stability_cfg.enabled or prev is None or nav <= 0:
        return decision
    alpha = Decimal(str(stability_cfg.gross_exposure_smoothing_alpha))
    try:
        prev_ge = Decimal(str(prev.get("gross_exposure_target", "0")))
    except Exception:  # noqa: BLE001
        prev_ge = Decimal("0")
    raw_ge = decision.gross_exposure_target
    if raw_ge <= 0:
        return decision
    sm_ge = raw_ge * alpha + prev_ge * (Decimal("1") - alpha)
    sm_ge = clip_decimal(sm_ge, Decimal("0"), Decimal("10"))
    ge_scale = sm_ge / raw_ge

    prev_w_raw = prev.get("weights_by_symbol") or {}
    prev_w: dict[str, Decimal] = {}
    if isinstance(prev_w_raw, dict):
        for k, v in prev_w_raw.items():
            try:
                prev_w[str(k)] = Decimal(str(v))
            except Exception:  # noqa: BLE001
                continue

    turnover_sum = Decimal("0")
    for t in decision.allocation_targets:
        pw = prev_w.get(t.symbol, t.target_weight)
        turnover_sum += abs(t.target_weight - pw)

    lam = float(stability_cfg.turnover_damping_lambda)
    thr = float(stability_cfg.turnover_weight_sum_threshold)
    damp = Decimal(str(math.exp(-lam * max(0.0, float(turnover_sum) - thr))))

    scaled: list[AllocationTarget] = []
    for t in decision.allocation_targets:
        tw = t.target_weight * ge_scale * damp
        tw = clip_decimal(tw, Decimal("0"), Decimal("1"))
        tn = clip_decimal(nav * tw, Decimal("0"), nav * sm_ge)
        scaled.append(replace(t, target_weight=tw, target_notional=tn))

    meta = dict(decision.metadata)
    meta["d015_smoothing"] = True
    meta["turnover_weight_sum"] = str(turnover_sum)
    meta["damping_factor"] = str(damp)

    return replace(
        decision,
        gross_exposure_target=sm_ge,
        net_exposure_target=sm_ge,
        capital_deployment_target=clip_decimal(nav * sm_ge, Decimal("0"), nav),
        allocation_targets=scaled,
        metadata=meta,
    )
