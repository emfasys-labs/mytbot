"""Profit harvesting decisions for already-open positions.

Two layers:

1. ``evaluate_profit_harvest`` is pure numeric — given thresholds and a
   position, decide whether to trim or close. Does not place orders.
2. ``resolve_harvest_thresholds`` derives those thresholds **dynamically**
   from market volatility, the operator's profile mode (defender / trader /
   hunter), and optional per-position strategy overrides. There are no
   one-size-fits-all numbers: a calm 1% mover and a 12% biotech are not
   harvested at the same level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class ProfitHarvestDecision:
    should_reduce: bool
    reason: str
    reduce_fraction: Decimal
    profit_absolute: Decimal
    profit_pct: Decimal
    profit_pct_of_nav: Decimal
    peak_profit_absolute: Decimal
    giveback_fraction: Decimal


@dataclass(frozen=True)
class HarvestThresholds:
    """Effective harvest thresholds for a single position at a single tick."""

    min_profit_pct: Decimal
    min_profit_nav_pct: Decimal
    full_close_profit_pct: Decimal
    trim_fraction: Decimal
    trailing_giveback_pct: Decimal
    peak_lock_min_nav_pct: Decimal = Decimal("0.0005")
    inputs: dict[str, Any] = field(default_factory=dict)
    rationale: dict[str, str] = field(default_factory=dict)


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return default


def _clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if hi < lo:
        lo, hi = hi, lo
    return max(lo, min(hi, value))


def _lerp(lo: Decimal, hi: Decimal, t: Decimal) -> Decimal:
    t = _clamp(t, _ZERO, _ONE)
    return lo + (hi - lo) * t


def resolve_harvest_thresholds(
    *,
    config: Mapping[str, Any] | None,
    profile_mode: str = "trader",
    volatility_pct: Decimal | str | float | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> HarvestThresholds:
    """Derive position-specific harvest thresholds.

    Inputs:
    * ``config`` is the ``profit_harvest`` block from ``risk_limits.yaml``.
    * ``profile_mode`` is the live operator mode; biases tightness vs runner-room.
    * ``volatility_pct`` is the asset's recent realised volatility as a
      fraction (e.g. ``0.018`` = 1.8%). ``None`` falls back to the configured
      neutral vol so we never harvest on hard-coded numbers — every threshold
      is at least anchored to a measured-or-defaulted vol.
    * ``overrides`` is per-position strategy intent, persisted at order time
      via ``instrument_metadata.profit_harvest``. Overrides win when present.
    """
    cfg = dict(config or {})
    base = dict(cfg.get("base") or {})
    vol_cfg = dict(cfg.get("volatility") or {})
    mode_cfg = dict(cfg.get("mode_bias") or {})
    bounds = dict(cfg.get("bounds") or {})
    ovr = dict(overrides or {})

    # ── Volatility: prefer measured, then per-position override hint, then config fallback.
    fallback_vol = _to_decimal(vol_cfg.get("fallback_vol"), Decimal("0.015"))
    if volatility_pct is None and "volatility_pct" in ovr:
        volatility_pct = ovr.get("volatility_pct")
    vol = _to_decimal(volatility_pct, fallback_vol)
    if vol < 0:
        vol = fallback_vol

    vol_low = _to_decimal(vol_cfg.get("vol_low"), Decimal("0.005"))
    vol_high = _to_decimal(vol_cfg.get("vol_high"), Decimal("0.05"))
    span = vol_high - vol_low
    if span <= 0:
        vol_norm = Decimal("0.5")
    else:
        vol_norm = _clamp((vol - vol_low) / span, _ZERO, _ONE)

    # ── Mode bias coefficients (defender tightens, hunter loosens).
    active_mode = (profile_mode or "trader").strip().lower()
    mode = dict(mode_cfg.get(active_mode) or mode_cfg.get("trader") or {})
    threshold_mult = _to_decimal(mode.get("threshold_mult"), _ONE)
    giveback_mult = _to_decimal(mode.get("giveback_mult"), _ONE)
    trim_mult = _to_decimal(mode.get("trim_fraction_mult"), _ONE)

    # ── Min-profit (partial take-profit trigger), volatility-anchored.
    k_min = _to_decimal(vol_cfg.get("min_profit_k"), Decimal("1.5"))
    min_floor = _to_decimal(vol_cfg.get("min_profit_floor"), Decimal("0.004"))
    min_ceil = _to_decimal(vol_cfg.get("min_profit_ceil"), Decimal("0.05"))
    base_min = _to_decimal(base.get("min_profit_pct"), Decimal("0.012"))
    min_dynamic = _clamp(k_min * vol, min_floor, min_ceil)
    # Blend the operator-set base with the vol-anchored value so the YAML floor
    # is honoured and the live signal can stretch above it.
    min_profit_pct = max(base_min, min_dynamic) * threshold_mult

    # ── Full-close trigger (let winners run further, but cap eventually).
    k_full = _to_decimal(vol_cfg.get("full_close_k"), Decimal("4.0"))
    full_floor = _to_decimal(vol_cfg.get("full_close_floor"), Decimal("0.012"))
    full_ceil = _to_decimal(vol_cfg.get("full_close_ceil"), Decimal("0.20"))
    base_full = _to_decimal(base.get("full_close_profit_pct"), Decimal("0.035"))
    full_dynamic = _clamp(k_full * vol, full_floor, full_ceil)
    full_close_profit_pct = max(base_full, full_dynamic) * threshold_mult

    # Always keep full > min so the state machine has both bands.
    if full_close_profit_pct <= min_profit_pct:
        full_close_profit_pct = min_profit_pct + Decimal("0.005")

    # ── Trim fraction: defender takes more off the table; hunter leaves a runner.
    base_trim = _to_decimal(base.get("trim_fraction"), Decimal("0.50"))
    trim_lo = _to_decimal(bounds.get("trim_fraction_min"), Decimal("0.10"))
    trim_hi = _to_decimal(bounds.get("trim_fraction_max"), Decimal("0.95"))
    trim_fraction = _clamp(base_trim * trim_mult, trim_lo, trim_hi)

    # ── Trailing giveback: scale up with vol so chop doesn't stop us out.
    gb_low = _to_decimal(vol_cfg.get("giveback_vol_low"), Decimal("0.25"))
    gb_high = _to_decimal(vol_cfg.get("giveback_vol_high"), Decimal("0.55"))
    base_gb = _to_decimal(base.get("trailing_giveback_pct"), Decimal("0.35"))
    gb_dynamic = _lerp(gb_low, gb_high, vol_norm)
    # Use whichever is larger (config base acts as a floor) then bias by mode.
    gb_lo_bound = _to_decimal(bounds.get("giveback_min"), Decimal("0.10"))
    gb_hi_bound = _to_decimal(bounds.get("giveback_max"), Decimal("0.80"))
    trailing_giveback_pct = _clamp(
        max(base_gb, gb_dynamic) * giveback_mult, gb_lo_bound, gb_hi_bound
    )

    # ── NAV-relative floor — small slivers vs NAV aren't worth chasing.
    base_nav = _to_decimal(base.get("min_profit_nav_pct"), Decimal("0.001"))
    min_profit_nav_pct = base_nav * threshold_mult

    # ── Peak-lock material floor: how big the peak (vs NAV) must have been
    # before a trailing-lock close is allowed. Defender tightens, hunter loosens.
    base_peak_lock = _to_decimal(base.get("peak_lock_min_nav_pct"), Decimal("0.0005"))
    peak_lock_min_nav_pct = base_peak_lock * threshold_mult

    # ── Per-position strategy overrides win unconditionally over computed values.
    if "min_profit_pct" in ovr:
        min_profit_pct = _to_decimal(ovr.get("min_profit_pct"), min_profit_pct)
    if "full_close_profit_pct" in ovr:
        full_close_profit_pct = _to_decimal(
            ovr.get("full_close_profit_pct"), full_close_profit_pct
        )
    if "trim_fraction" in ovr:
        trim_fraction = _clamp(
            _to_decimal(ovr.get("trim_fraction"), trim_fraction), trim_lo, trim_hi
        )
    if "trailing_giveback_pct" in ovr:
        trailing_giveback_pct = _clamp(
            _to_decimal(ovr.get("trailing_giveback_pct"), trailing_giveback_pct),
            gb_lo_bound,
            gb_hi_bound,
        )
    if "min_profit_nav_pct" in ovr:
        min_profit_nav_pct = _to_decimal(
            ovr.get("min_profit_nav_pct"), min_profit_nav_pct
        )
    if "peak_lock_min_nav_pct" in ovr:
        peak_lock_min_nav_pct = _to_decimal(
            ovr.get("peak_lock_min_nav_pct"), peak_lock_min_nav_pct
        )

    inputs = {
        "profile_mode": active_mode,
        "volatility_pct": str(vol),
        "vol_norm": str(vol_norm),
        "threshold_mult": str(threshold_mult),
        "giveback_mult": str(giveback_mult),
        "trim_fraction_mult": str(trim_mult),
        "override_keys": sorted(ovr.keys()),
    }
    rationale = {
        "min_profit_pct": (
            f"max(base={base_min}, k_min*vol={min_dynamic})*mode={threshold_mult}"
        ),
        "full_close_profit_pct": (
            f"max(base={base_full}, k_full*vol={full_dynamic})*mode={threshold_mult}"
        ),
        "trim_fraction": f"base*{trim_mult} clamped [{trim_lo},{trim_hi}]",
        "trailing_giveback_pct": (
            f"lerp(vol_norm={vol_norm})*mode={giveback_mult} floor=base={base_gb}"
        ),
    }

    return HarvestThresholds(
        min_profit_pct=min_profit_pct,
        min_profit_nav_pct=min_profit_nav_pct,
        full_close_profit_pct=full_close_profit_pct,
        trim_fraction=trim_fraction,
        trailing_giveback_pct=trailing_giveback_pct,
        peak_lock_min_nav_pct=peak_lock_min_nav_pct,
        inputs=inputs,
        rationale=rationale,
    )


def evaluate_profit_harvest(
    *,
    quantity: Decimal,
    avg_entry_price: Decimal,
    current_price: Decimal,
    nav: Decimal,
    peak_profit_absolute: Decimal | None = None,
    min_profit_pct: Decimal = Decimal("0.01"),
    min_profit_nav_pct: Decimal = Decimal("0.001"),
    trim_fraction: Decimal = Decimal("0.50"),
    full_close_profit_pct: Decimal = Decimal("0.03"),
    trailing_giveback_pct: Decimal = Decimal("0.35"),
    peak_lock_min_nav_pct: Decimal = Decimal("0.0005"),
) -> ProfitHarvestDecision:
    """Decide whether an open position should bank profit.

    Triggers:
    * ``full_close_profit_pct``: close the whole position after a large move.
    * ``min_profit_pct`` + ``min_profit_nav_pct``: trim a configured fraction.
    * ``trailing_giveback_pct``: after a profitable peak, close if enough of
      the open profit has been given back.
    """
    zero = _ZERO
    if quantity == 0 or avg_entry_price <= 0 or current_price <= 0:
        return ProfitHarvestDecision(False, "invalid_position", zero, zero, zero, zero, zero, zero)

    direction = _ONE if quantity > 0 else Decimal("-1")
    profit_abs = direction * (current_price - avg_entry_price) * abs(quantity)
    position_cost = avg_entry_price * abs(quantity)
    profit_pct = profit_abs / position_cost if position_cost > 0 else zero
    profit_nav_pct = profit_abs / nav if nav > 0 else zero
    peak = max(peak_profit_absolute or zero, profit_abs)
    # Compute giveback unconditionally so trailing-lock can fire even when the
    # current mark has retraced through zero (the +$3K → −$4K round-trip).
    giveback = ((peak - profit_abs) / peak) if peak > 0 else zero

    trim_fraction = max(zero, min(_ONE, trim_fraction))
    trailing_giveback_pct = max(zero, min(_ONE, trailing_giveback_pct))

    # Trailing profit lock — checked before the "not_profitable" short-circuit
    # so a meaningful peak that has retraced enough always fires a hard close,
    # *including* into the red. Gated by ``peak_lock_min_nav_pct`` so chop on
    # tiny positions doesn't trigger spurious locks.
    peak_nav_pct = (peak / nav) if nav > 0 and peak > 0 else zero
    peak_is_material = peak_lock_min_nav_pct <= 0 or peak_nav_pct >= peak_lock_min_nav_pct
    if (
        peak > 0
        and trailing_giveback_pct > 0
        and giveback >= trailing_giveback_pct
        and peak_is_material
    ):
        return ProfitHarvestDecision(
            True,
            "trailing_profit_lock",
            _ONE,
            profit_abs,
            profit_pct,
            profit_nav_pct,
            peak,
            giveback,
        )

    if profit_abs <= 0:
        return ProfitHarvestDecision(False, "not_profitable", zero, profit_abs, profit_pct, profit_nav_pct, peak, giveback)

    if full_close_profit_pct > 0 and profit_pct >= full_close_profit_pct:
        return ProfitHarvestDecision(
            True,
            "full_take_profit",
            _ONE,
            profit_abs,
            profit_pct,
            profit_nav_pct,
            peak,
            giveback,
        )

    if (
        trim_fraction > 0
        and min_profit_pct > 0
        and profit_pct >= min_profit_pct
        and (min_profit_nav_pct <= 0 or profit_nav_pct >= min_profit_nav_pct)
    ):
        return ProfitHarvestDecision(
            True,
            "partial_take_profit",
            trim_fraction,
            profit_abs,
            profit_pct,
            profit_nav_pct,
            peak,
            giveback,
        )

    return ProfitHarvestDecision(
        False,
        "below_harvest_threshold",
        zero,
        profit_abs,
        profit_pct,
        profit_nav_pct,
        peak,
        giveback,
    )
