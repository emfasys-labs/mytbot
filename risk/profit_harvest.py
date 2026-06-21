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
class ProfitHarvestV2Context:
    """Advisory context for the second-generation harvest policy.

    The fields are intentionally optional because live availability differs by
    asset and broker. Missing intelligence must degrade to a deterministic
    price/profit decision, never to a failed risk tick.
    """

    symbol: str = ""
    asset_class: str = ""
    accumulator_score: Decimal | None = None
    ai_news_score: Decimal | None = None
    meta_label_kept: bool | None = None
    meta_label_probability: Decimal | None = None
    age_sec: Decimal | None = None
    session_open: bool = True


@dataclass(frozen=True)
class ProfitHarvestV2Decision:
    action: str
    reason: str
    score: Decimal
    reduce_fraction: Decimal
    legacy_reason: str
    profit_absolute: Decimal
    profit_pct: Decimal
    profit_pct_of_nav: Decimal
    peak_profit_absolute: Decimal
    giveback_fraction: Decimal
    dynamic_giveback_pct: Decimal
    profit_to_partial_trigger: Decimal
    support_score: Decimal
    modifiers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HarvestThresholds:
    """Effective harvest thresholds for a single position at a single tick."""

    min_profit_pct: Decimal
    min_profit_nav_pct: Decimal
    full_close_profit_pct: Decimal
    trim_fraction: Decimal
    trailing_giveback_pct: Decimal
    peak_lock_min_nav_pct: Decimal = _ZERO
    inputs: dict[str, Any] = field(default_factory=dict)
    rationale: dict[str, str] = field(default_factory=dict)


def should_defer_profit_harvest_for_redeployment(
    *,
    cash_deployed: Decimal,
    nav: Decimal,
    capital_pct: Decimal,
    open_lock_active: bool,
    open_lock_blocks_redeployment: bool = True,
    tolerance_pct: Decimal = Decimal("0.0025"),
) -> bool:
    """Defer voluntary harvesting when cash cannot currently be redeployed.

    Profit harvesting is a recycling action: bank gains, then let the allocator
    reuse the freed cash. If fresh opens are blocked by a drawdown open-lock
    and the book is already below the operator's capital target, harvesting
    more winners only drains exposure further. Stop-loss and derisk monitors
    are separate safety paths and are intentionally unaffected.
    """
    if not open_lock_active or not open_lock_blocks_redeployment or nav <= 0 or capital_pct <= 0:
        return False
    target_cash = nav * max(_ZERO, min(_ONE, capital_pct))
    if target_cash <= 0:
        return False
    tolerance = target_cash * max(_ZERO, tolerance_pct)
    return cash_deployed < (target_cash - tolerance)


def should_suppress_harvest_for_horizon(
    *,
    decision: "ProfitHarvestDecision",
    age_sec: Decimal | None,
    min_hold_sec: Decimal,
    nav: Decimal,
    min_material_profit_nav_pct: Decimal = Decimal("0.0010"),
) -> tuple[bool, str]:
    """D168 — anti-churn guard for the profit-harvest monitor.

    The D163 scoreboard proved the realised loss lived in the *management*
    layers cutting daily-horizon theses on intraday noise. D166 gave the
    stop-loss / derisk monitors a horizon-aware min-hold gate, but the
    profit-harvest monitor was left ungated — and a live soak (2026-06-18)
    caught it churning: ``trailing_profit_lock`` fired on a position that
    ticked up ~0.05% of NAV then retraced, **closing it at a LOSS 32 min
    after open** (XLE −$138). ``evaluate_profit_harvest`` deliberately lets
    a trailing lock fire *into the red* to protect a real round-trip
    (+$3K→−$4K), but on a YOUNG daily-horizon position that same behaviour
    is pure churn tax.

    This guard suppresses ONLY a trailing-lock close that would realise a
    loss / immaterial gain on a position younger than ``min_hold_sec``.
    Everything that genuinely banks edge is always allowed:

      * ``full_take_profit`` / ``partial_take_profit`` (profit_abs > 0 above
        a real threshold) — banking a winner, never suppressed.
      * a ``trailing_profit_lock`` that still locks in a *materially positive*
        profit (>= ``min_material_profit_nav_pct`` of NAV) — a real winner
        being protected, never suppressed.
      * any harvest once the position has matured past ``min_hold_sec``.

    Returns ``(suppress, reason)``. ``suppress=False`` means "let the harvest
    proceed" (the pre-D168 behaviour for everything except young loss-locks).
    Missing age (``None``) is treated as "unknown" → never suppress (no
    evidence to gate on, mirrors the D166 protective gate).
    """
    if not decision.should_reduce:
        return (False, "no_reduce")
    # Only the trailing-lock path can fire into the red; the take-profit
    # paths are positive-profit by construction.
    if decision.reason != "trailing_profit_lock":
        return (False, "not_trailing_lock")
    # A trailing lock that still banks a materially positive profit is a real
    # winner being protected — never churn.
    material = (
        min_material_profit_nav_pct <= 0
        or (nav > 0 and decision.profit_pct_of_nav >= min_material_profit_nav_pct)
    )
    if decision.profit_absolute > 0 and material:
        return (False, "locks_material_profit")
    if age_sec is None:
        return (False, "age_unknown")
    if min_hold_sec > 0 and age_sec < min_hold_sec:
        return (True, "young_loss_lock")
    return (False, "matured")


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


def _signed_optional_score(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return _clamp(Decimal(str(value)), Decimal("-1"), _ONE)
    except Exception:  # noqa: BLE001
        return None


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
      via ``instrument_metadata.profit_harvest``. Absolute profit-threshold
      overrides are intentionally ignored: live harvest bands must be derived
      from volatility and mode context, not fixed percentages hidden in order
      metadata.
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

    # ── Min-profit (partial take-profit trigger), volatility-anchored only.
    # No operator-set fixed profit floors here. The configured values are
    # dimensionless coefficients applied to live/measured volatility.
    k_min = _to_decimal(
        vol_cfg.get("min_profit_vol_multiplier", vol_cfg.get("min_profit_k")),
        Decimal("1.5"),
    )
    min_profit_pct = max(_ZERO, k_min * vol * threshold_mult)

    # ── Full-close trigger is a multiple of the partial trigger, not a fixed
    # return target. If the partial trigger is zero, both bands stay zero and
    # the pure evaluator's profit>0 checks still prevent meaningless closes.
    full_multiple = _to_decimal(
        vol_cfg.get("full_close_multiple_of_partial", vol_cfg.get("full_close_k")),
        Decimal("3"),
    )
    if full_multiple <= _ONE:
        full_multiple = _ONE + vol_norm
    full_close_profit_pct = min_profit_pct * full_multiple

    # ── Trim fraction: defender takes more off the table; hunter leaves a runner.
    base_trim = _to_decimal(base.get("trim_fraction"), Decimal("0.50"))
    trim_lo = _to_decimal(bounds.get("trim_fraction_min"), Decimal("0.10"))
    trim_hi = _to_decimal(bounds.get("trim_fraction_max"), Decimal("0.95"))
    trim_fraction = _clamp(base_trim * trim_mult, trim_lo, trim_hi)

    # ── Trailing giveback: scale up with vol so chop doesn't stop us out.
    gb_low = _to_decimal(vol_cfg.get("giveback_vol_low"), Decimal("0.25"))
    gb_high = _to_decimal(vol_cfg.get("giveback_vol_high"), Decimal("0.55"))
    gb_dynamic = _lerp(gb_low, gb_high, vol_norm)
    gb_lo_bound = _to_decimal(bounds.get("giveback_min"), Decimal("0.10"))
    gb_hi_bound = _to_decimal(bounds.get("giveback_max"), Decimal("0.80"))
    trailing_giveback_pct = _clamp(gb_dynamic * giveback_mult, gb_lo_bound, gb_hi_bound)

    # ── NAV-relative floor is intentionally disabled. The position-relative
    # threshold already scales to the instrument's own volatility and notional;
    # adding a fixed NAV percentage was what blocked sensible small winner
    # harvesting on the current book.
    min_profit_nav_pct = _ZERO

    # ── Peak-lock material floor is derived from the same dynamic band rather
    # than a fixed NAV percentage. The pure evaluator compares peak P&L to NAV;
    # here the materiality gate is zero so the dynamic giveback/profit bands
    # and D168 horizon guard own the actual decision.
    peak_lock_min_nav_pct = _ZERO

    # ── Per-position strategy overrides may tune sizing/giveback, but not
    # absolute profit thresholds. That prevents metadata from bypassing the
    # project-level no-static-threshold rule.
    ignored_absolute_overrides = sorted(
        k
        for k in ovr.keys()
        if k
        in {
            "min_profit_pct",
            "full_close_profit_pct",
            "min_profit_nav_pct",
            "peak_lock_min_nav_pct",
        }
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

    inputs = {
        "profile_mode": active_mode,
        "volatility_pct": str(vol),
        "vol_norm": str(vol_norm),
        "threshold_mult": str(threshold_mult),
        "giveback_mult": str(giveback_mult),
        "trim_fraction_mult": str(trim_mult),
        "override_keys": sorted(ovr.keys()),
        "ignored_absolute_override_keys": ignored_absolute_overrides,
    }
    rationale = {
        "min_profit_pct": (
            f"vol={vol}*multiplier={k_min}*mode={threshold_mult}"
        ),
        "full_close_profit_pct": (
            f"min_profit_pct*full_multiple={full_multiple}"
        ),
        "trim_fraction": f"base*{trim_mult} clamped [{trim_lo},{trim_hi}]",
        "trailing_giveback_pct": (
            f"lerp(vol_norm={vol_norm})*mode={giveback_mult}"
        ),
        "min_profit_nav_pct": "disabled; position-relative volatility band owns harvest trigger",
        "peak_lock_min_nav_pct": "disabled; D168 horizon guard and dynamic giveback own lock quality",
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


def evaluate_profit_harvest_v2(
    *,
    quantity: Decimal,
    avg_entry_price: Decimal,
    current_price: Decimal,
    nav: Decimal,
    thresholds: HarvestThresholds,
    peak_profit_absolute: Decimal | None = None,
    context: ProfitHarvestV2Context | None = None,
) -> ProfitHarvestV2Decision:
    """Second-generation, shadow-first profit harvesting recommendation.

    This does **not** place orders. It classifies an open position into one of:

    * ``DO_NOT_TOUCH`` — no usable position, flat/negative profit with no
      thesis damage, or a closed venue.
    * ``HOLD_RUNNER`` — profitable position with supportive signal/news/meta
      evidence; leave the position alone and let the edge work.
    * ``TRIM_PARTIAL`` — bank some profit while leaving a runner.
    * ``CLOSE_FULL`` — severe giveback, very large win, or advisory evidence
      says the thesis has deteriorated.

    The v2 policy deliberately consumes the existing deterministic v1 decision
    as one input. That makes shadow comparisons explainable: any divergence is
    visible in ``legacy_reason`` and ``modifiers``.
    """
    ctx = context or ProfitHarvestV2Context()
    legacy = evaluate_profit_harvest(
        quantity=quantity,
        avg_entry_price=avg_entry_price,
        current_price=current_price,
        nav=nav,
        peak_profit_absolute=peak_profit_absolute,
        min_profit_pct=thresholds.min_profit_pct,
        min_profit_nav_pct=thresholds.min_profit_nav_pct,
        trim_fraction=thresholds.trim_fraction,
        full_close_profit_pct=thresholds.full_close_profit_pct,
        trailing_giveback_pct=thresholds.trailing_giveback_pct,
        peak_lock_min_nav_pct=thresholds.peak_lock_min_nav_pct,
    )

    zero = _ZERO
    modifiers: dict[str, str] = {}
    if quantity == 0 or avg_entry_price <= 0 or current_price <= 0:
        return ProfitHarvestV2Decision(
            action="DO_NOT_TOUCH",
            reason="invalid_position",
            score=zero,
            reduce_fraction=zero,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=thresholds.trailing_giveback_pct,
            profit_to_partial_trigger=zero,
            support_score=zero,
            modifiers=modifiers,
        )

    direction = _ONE if quantity > 0 else Decimal("-1")
    support = zero
    weight = zero
    acc = _signed_optional_score(ctx.accumulator_score)
    if acc is not None:
        # Accumulator/news scores are symbol-direction signed. Convert them to
        # support/opposition for the current position direction.
        support += direction * acc * Decimal("0.45")
        weight += Decimal("0.45")
        modifiers["accumulator_score"] = str(acc)
    news = _signed_optional_score(ctx.ai_news_score)
    if news is not None:
        support += direction * news * Decimal("0.25")
        weight += Decimal("0.25")
        modifiers["ai_news_score"] = str(news)
    meta_prob = _signed_optional_score(ctx.meta_label_probability)
    if meta_prob is not None:
        # Probability is expected in [0, 1]; map 0.5 to neutral.
        support += _clamp((meta_prob - Decimal("0.5")) * Decimal("2"), Decimal("-1"), _ONE) * Decimal("0.20")
        weight += Decimal("0.20")
        modifiers["meta_label_probability"] = str(meta_prob)
    if ctx.meta_label_kept is not None:
        support += (Decimal("0.15") if ctx.meta_label_kept else Decimal("-0.35"))
        weight += Decimal("0.15")
        modifiers["meta_label_kept"] = str(ctx.meta_label_kept)
    support_score = _clamp(support / weight, Decimal("-1"), _ONE) if weight > 0 else zero

    partial_abs = max(
        abs(quantity) * avg_entry_price * thresholds.min_profit_pct,
        nav * thresholds.min_profit_nav_pct if nav > 0 else zero,
    )
    profit_to_partial = (
        legacy.profit_absolute / partial_abs if partial_abs > 0 else zero
    )

    dynamic_giveback = thresholds.trailing_giveback_pct
    if profit_to_partial >= Decimal("4"):
        dynamic_giveback -= Decimal("0.25")
        modifiers["ratchet"] = "very_large_profit"
    elif profit_to_partial >= Decimal("2"):
        dynamic_giveback -= Decimal("0.15")
        modifiers["ratchet"] = "large_profit"
    if support_score >= Decimal("0.35"):
        dynamic_giveback += Decimal("0.10")
        modifiers["support_bias"] = "let_runner_breathe"
    elif support_score <= Decimal("-0.25"):
        dynamic_giveback -= Decimal("0.15")
        modifiers["support_bias"] = "thesis_deteriorating"
    dynamic_giveback = _clamp(dynamic_giveback, Decimal("0.20"), Decimal("0.75"))

    score = _clamp(
        (profit_to_partial * Decimal("0.35"))
        + (legacy.giveback_fraction * Decimal("0.40"))
        - (support_score * Decimal("0.45")),
        Decimal("-1"),
        Decimal("3"),
    )

    if not ctx.session_open:
        return ProfitHarvestV2Decision(
            action="DO_NOT_TOUCH",
            reason="venue_closed_shadow_only",
            score=score,
            reduce_fraction=zero,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=dynamic_giveback,
            profit_to_partial_trigger=profit_to_partial,
            support_score=support_score,
            modifiers=modifiers,
        )

    if legacy.profit_absolute <= 0:
        action = "CLOSE_FULL" if support_score <= Decimal("-0.55") else "DO_NOT_TOUCH"
        reason = "thesis_invalidated_loss" if action == "CLOSE_FULL" else "not_profitable"
        return ProfitHarvestV2Decision(
            action=action,
            reason=reason,
            score=score,
            reduce_fraction=_ONE if action == "CLOSE_FULL" else zero,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=dynamic_giveback,
            profit_to_partial_trigger=profit_to_partial,
            support_score=support_score,
            modifiers=modifiers,
        )

    if legacy.reason == "full_take_profit" or profit_to_partial >= Decimal("4"):
        return ProfitHarvestV2Decision(
            action="CLOSE_FULL",
            reason="very_large_profit_bank",
            score=score,
            reduce_fraction=_ONE,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=dynamic_giveback,
            profit_to_partial_trigger=profit_to_partial,
            support_score=support_score,
            modifiers=modifiers,
        )

    if legacy.giveback_fraction >= dynamic_giveback:
        severe = legacy.giveback_fraction >= max(Decimal("0.80"), dynamic_giveback + Decimal("0.20"))
        action = "CLOSE_FULL" if severe or support_score <= Decimal("-0.35") else "TRIM_PARTIAL"
        return ProfitHarvestV2Decision(
            action=action,
            reason="dynamic_trailing_lock",
            score=score,
            reduce_fraction=_ONE if action == "CLOSE_FULL" else thresholds.trim_fraction,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=dynamic_giveback,
            profit_to_partial_trigger=profit_to_partial,
            support_score=support_score,
            modifiers=modifiers,
        )

    if profit_to_partial >= _ONE:
        if support_score >= Decimal("0.35") and legacy.giveback_fraction < dynamic_giveback:
            action = "HOLD_RUNNER"
            reason = "supported_runner"
            fraction = zero
        else:
            action = "TRIM_PARTIAL"
            reason = "bank_profit_leave_runner"
            fraction = thresholds.trim_fraction
        return ProfitHarvestV2Decision(
            action=action,
            reason=reason,
            score=score,
            reduce_fraction=fraction,
            legacy_reason=legacy.reason,
            profit_absolute=legacy.profit_absolute,
            profit_pct=legacy.profit_pct,
            profit_pct_of_nav=legacy.profit_pct_of_nav,
            peak_profit_absolute=legacy.peak_profit_absolute,
            giveback_fraction=legacy.giveback_fraction,
            dynamic_giveback_pct=dynamic_giveback,
            profit_to_partial_trigger=profit_to_partial,
            support_score=support_score,
            modifiers=modifiers,
        )

    return ProfitHarvestV2Decision(
        action="HOLD_RUNNER" if support_score >= Decimal("0.25") else "DO_NOT_TOUCH",
        reason="below_bank_threshold",
        score=score,
        reduce_fraction=zero,
        legacy_reason=legacy.reason,
        profit_absolute=legacy.profit_absolute,
        profit_pct=legacy.profit_pct,
        profit_pct_of_nav=legacy.profit_pct_of_nav,
        peak_profit_absolute=legacy.peak_profit_absolute,
        giveback_fraction=legacy.giveback_fraction,
        dynamic_giveback_pct=dynamic_giveback,
        profit_to_partial_trigger=profit_to_partial,
        support_score=support_score,
        modifiers=modifiers,
    )
