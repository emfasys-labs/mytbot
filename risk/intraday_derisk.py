"""
risk/intraday_derisk.py
========================
D115 — Intraday aggregate-derisk decision layer.

The static daily-loss limit (`max_daily_loss_pct: 0.02`, i.e. -2%) is a
last-resort kill switch — by the time it fires, two thirds of the day's
realisable drawdown is already locked in. The intraday derisk layer
sits BEFORE that limit and graduates the response by severity:

    *  -0.5% intraday : warn + light trim of the worst losers
    *  -1.0% intraday : meaningful trim of multiple worst losers
    *  -1.5% intraday : full close of the worst losers

It is purely a decision function. The orchestrator background task is
responsible for evaluating positions, calling this, and routing
reduce-only signals through the normal SignalEngine/RiskEngine/Router
path. There is no risk-engine bypass — every action it emits is still
checked.

Cooldowns prevent the same position from being trimmed every tick.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

_ZERO = Decimal("0")


@dataclass(frozen=True)
class DeriskTier:
    """One severity band of the graduated response."""

    threshold_pct: Decimal              # negative (e.g. -0.005 = -0.5%)
    trim_pct: Decimal                   # 0..1 fraction of position to close
    max_actions: int                    # maximum positions to act on per tick
    min_loss_pct: Decimal = _ZERO       # only trim positions losing more than this


@dataclass(frozen=True)
class DeriskAction:
    """One reduce-only intent emitted by the layer."""

    broker: str
    symbol: str
    side: str                           # 'sell' (close long) or 'buy' (close short)
    reduce_quantity: Decimal
    asset_class: str
    current_price: Decimal
    reason: str
    severity_tier_idx: int
    tier_threshold_pct: Decimal
    trim_fraction: Decimal
    metadata: dict = field(default_factory=dict)


def _to_decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _parse_tier(raw: Mapping[str, Any]) -> DeriskTier:
    return DeriskTier(
        threshold_pct=_to_decimal(raw.get("threshold_pct")),
        trim_pct=_to_decimal(raw.get("trim_pct")),
        max_actions=int(raw.get("max_actions") or 0),
        min_loss_pct=_to_decimal(raw.get("min_loss_pct")),
    )


def parse_tiers(raw: Iterable[Mapping[str, Any]] | None) -> list[DeriskTier]:
    """Parse tiers from YAML config. Sorts most-severe (most negative) first."""
    if not raw:
        return []
    tiers = [_parse_tier(t) for t in raw if isinstance(t, Mapping)]
    # Sort most-severe (most negative) first so we pick the tightest match.
    tiers.sort(key=lambda t: t.threshold_pct)
    return tiers


def parse_position_loss_tier(raw: Mapping[str, Any] | None) -> DeriskTier | None:
    """Parse the per-position Tier 0 derisk trigger.

    This tier is configured separately from NAV drawdown tiers and fires from
    position-level evidence: a losing holding may be trimmed even before the
    aggregate day reaches -0.5% NAV. All required values must be present; an
    incomplete block disables Tier 0 rather than inventing constants in code.
    """
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    required = ("min_loss_nav_pct", "trim_pct", "max_actions", "min_loss_pct")
    if any(k not in raw for k in required):
        return None
    return DeriskTier(
        threshold_pct=-_to_decimal(raw.get("min_loss_nav_pct")),
        trim_pct=_to_decimal(raw.get("trim_pct")),
        max_actions=int(raw.get("max_actions") or 0),
        min_loss_pct=_to_decimal(raw.get("min_loss_pct")),
    )


def _position_loss_pct(pos: Mapping[str, Any]) -> Decimal:
    """Loss % vs entry, expressed as a positive number (e.g. 0.0123 = 1.23% loss).
    Returns 0 for non-losing positions."""
    try:
        qty = Decimal(str(pos.get("quantity", "0") or "0"))
        entry = Decimal(str(pos.get("avg_entry_price", "0") or "0"))
        current = Decimal(str(pos.get("current_price", "0") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return _ZERO
    if qty == 0 or entry <= 0 or current <= 0:
        return _ZERO
    direction = Decimal(1) if qty > 0 else Decimal(-1)
    move_pct = (current - entry) / entry
    pnl_pct = direction * move_pct
    return -pnl_pct if pnl_pct < 0 else _ZERO


def _position_unrealised(pos: Mapping[str, Any]) -> Decimal:
    """Use the provided unrealised_pnl when present, otherwise compute from qty / entry / current."""
    upnl = pos.get("unrealised_pnl")
    if upnl is not None:
        d = _to_decimal(upnl, _ZERO)
        if d != _ZERO:
            return d
    try:
        qty = Decimal(str(pos.get("quantity", "0") or "0"))
        entry = Decimal(str(pos.get("avg_entry_price", "0") or "0"))
        current = Decimal(str(pos.get("current_price", "0") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return _ZERO
    if qty == 0 or entry <= 0 or current <= 0:
        return _ZERO
    return (current - entry) * qty


def _position_notional_nav_pct(pos: Mapping[str, Any], nav: Decimal) -> Decimal:
    if nav <= 0:
        return _ZERO
    try:
        qty = abs(Decimal(str(pos.get("quantity", "0") or "0")))
        current = Decimal(str(pos.get("current_price", "0") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return _ZERO
    if qty <= 0 or current <= 0:
        return _ZERO
    return (qty * current) / nav


def _position_observed_volatility(pos: Mapping[str, Any]) -> Decimal:
    meta = pos.get("instrument_metadata") if isinstance(pos.get("instrument_metadata"), Mapping) else {}
    for source in (pos, meta):
        for key in ("daily_volatility", "realised_vol", "realized_vol", "atr_pct", "volatility"):
            v = source.get(key) if isinstance(source, Mapping) else None
            d = _to_decimal(v, _ZERO)
            if d > 0:
                return d
    return _ZERO


def _resolve_dynamic_position_tier(
    *,
    base_tier: DeriskTier,
    pos: Mapping[str, Any],
    nav: Decimal,
    upnl: Decimal,
    portfolio_volatility_scalar: Decimal,
    notional_reference_pct: Decimal | None,
) -> DeriskTier:
    """Resolve a per-position Tier 0 from live position evidence.

    The configured Tier 0 remains the anchor. It is widened for genuinely
    volatile positions, tightened for oversized/concentrated positions, and
    the trim fraction grows with breach severity. No fallback thresholds are
    invented here; if a reference is missing, that dimension simply contributes
    a neutral scalar.
    """
    scalar = portfolio_volatility_scalar if portfolio_volatility_scalar > 0 else Decimal("1")
    observed_vol = _position_observed_volatility(pos)
    if observed_vol > 0 and base_tier.min_loss_pct > 0:
        scalar = max(scalar, observed_vol / base_tier.min_loss_pct)

    threshold_pct = base_tier.threshold_pct * scalar
    min_loss_pct = base_tier.min_loss_pct * scalar

    notional_pct = _position_notional_nav_pct(pos, nav)
    if notional_reference_pct is not None and notional_reference_pct > 0 and notional_pct > notional_reference_pct:
        concentration_scalar = notional_pct / notional_reference_pct
        threshold_pct = threshold_pct / concentration_scalar
        min_loss_pct = min_loss_pct / concentration_scalar

    loss_nav_pct = abs(upnl) / nav if nav > 0 else _ZERO
    breach = loss_nav_pct / abs(threshold_pct) if threshold_pct != 0 else Decimal("1")
    trim_pct = base_tier.trim_pct * max(Decimal("1"), breach)
    if trim_pct > Decimal("1"):
        trim_pct = Decimal("1")

    return DeriskTier(
        threshold_pct=threshold_pct,
        trim_pct=trim_pct,
        max_actions=base_tier.max_actions,
        min_loss_pct=min_loss_pct,
    )


def evaluate_intraday_derisk(
    *,
    nav: Decimal,
    day_pnl: Decimal,
    positions: Iterable[Mapping[str, Any]],
    tiers: list[DeriskTier],
    cooldown_seconds: float,
    last_action_ts: Mapping[str, float],
    now_ts: float,
    qty_decimals: int = 8,
    portfolio_volatility_scalar: Decimal = Decimal("1.0"),
    position_loss_tier: DeriskTier | None = None,
    dynamic_position_loss: bool = False,
    position_loss_notional_reference_pct: Decimal | None = None,
    require_open_book_loss_for_aggregate_actions: bool = True,
) -> tuple[list[DeriskAction], Optional[DeriskTier], int]:
    """Decide which positions to trim/close at current intraday drawdown.

    Returns ``(actions, active_tier, active_tier_idx)``:
        * ``actions``: empty list when no action required.
        * ``active_tier``: the most severe tier whose threshold has been
          breached, or ``None``.
        * ``active_tier_idx``: index into the input tiers list (most-
          severe-first ordering), or ``-1``.

    The function is pure — no I/O, no order placement. The orchestrator
    is responsible for routing the actions through risk + execution.
    """
    if nav <= 0 or (not tiers and position_loss_tier is None):
        return ([], None, -1)

    pnl_pct = day_pnl / nav if nav else _ZERO

    # Find the most-severe tier whose threshold is breached.
    # D120: Scale the threshold dynamically by portfolio volatility.
    active_idx = -1
    active_tier: Optional[DeriskTier] = None
    for idx, tier in enumerate(tiers):
        scalar = portfolio_volatility_scalar if portfolio_volatility_scalar > 0 else Decimal("1.0")
        dynamic_threshold = tier.threshold_pct * scalar
        if pnl_pct <= dynamic_threshold:
            active_idx = idx
            active_tier = tier
            break
    if active_tier is None and position_loss_tier is not None:
        active_tier = position_loss_tier
        active_idx = -2
    if active_tier is None:
        return ([], None, -1)

    if active_idx >= 0 and require_open_book_loss_for_aggregate_actions:
        open_book_unrealised = sum((_position_unrealised(pos) for pos in positions), _ZERO)
        if open_book_unrealised >= 0:
            return ([], active_tier, active_idx)

    # Rank positions by worst |unrealised_pnl| (i.e. biggest losers first).
    ranked: list[tuple[Decimal, Decimal, Mapping[str, Any]]] = []
    for pos in positions:
        upnl = _position_unrealised(pos)
        if upnl >= 0:
            continue
        candidate_tier = active_tier
        if active_idx == -2 and dynamic_position_loss and position_loss_tier is not None:
            candidate_tier = _resolve_dynamic_position_tier(
                base_tier=position_loss_tier,
                pos=pos,
                nav=nav,
                upnl=upnl,
                portfolio_volatility_scalar=portfolio_volatility_scalar,
                notional_reference_pct=position_loss_notional_reference_pct,
            )
        loss_pct = _position_loss_pct(pos)
        if loss_pct < candidate_tier.min_loss_pct:
            continue
        if active_idx == -2:
            loss_nav_pct = abs(upnl) / nav if nav > 0 else _ZERO
            if loss_nav_pct < abs(candidate_tier.threshold_pct):
                continue
        ranked.append((upnl, loss_pct, pos, candidate_tier))

    ranked.sort(key=lambda r: r[0])  # most negative upnl first

    tick = Decimal(1).scaleb(-qty_decimals)
    actions: list[DeriskAction] = []
    for upnl, loss_pct, pos, candidate_tier in ranked:
        if len(actions) >= active_tier.max_actions:
            break
        broker = str(pos.get("broker") or "").strip().lower()
        symbol = str(pos.get("symbol") or "").strip().upper()
        if not broker or not symbol:
            continue
        cool_key = f"{broker}:{symbol}"
        last_ts = float(last_action_ts.get(cool_key, 0.0) or 0.0)
        if now_ts - last_ts < cooldown_seconds:
            continue
        try:
            qty = Decimal(str(pos.get("quantity", "0") or "0"))
            current = Decimal(str(pos.get("current_price", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if qty == 0 or current <= 0:
            continue
        side = "sell" if qty > 0 else "buy"
        abs_qty = abs(qty)
        # Floor reduce quantity to a multiple of tick; clamp to abs(qty).
        if candidate_tier.trim_pct >= Decimal("1"):
            reduce_qty = abs_qty
        else:
            reduce_qty = (abs_qty * candidate_tier.trim_pct).quantize(tick)
            if reduce_qty > abs_qty:
                reduce_qty = abs_qty
        if reduce_qty <= 0:
            continue
        asset_class = str(pos.get("asset_class") or "equity").strip().lower()
        actions.append(
            DeriskAction(
                broker=broker,
                symbol=symbol,
                side=side,
                reduce_quantity=reduce_qty,
                asset_class=asset_class,
                current_price=current,
                reason=f"intraday_derisk_tier_{active_idx}",
                severity_tier_idx=active_idx,
                tier_threshold_pct=candidate_tier.threshold_pct,
                trim_fraction=candidate_tier.trim_pct,
                metadata={
                    "intraday_derisk": True,
                    "reduce_only": True,
                    "intraday_pnl_pct_of_nav": str(pnl_pct),
                    "position_unrealised_pnl": str(upnl),
                    "position_loss_pct": str(loss_pct),
                    "position_dynamic_loss": bool(active_idx == -2 and dynamic_position_loss),
                    "position_dynamic_threshold_pct": str(candidate_tier.threshold_pct),
                    "position_dynamic_min_loss_pct": str(candidate_tier.min_loss_pct),
                    "position_notional_nav_pct": str(_position_notional_nav_pct(pos, nav)),
                    "position_observed_volatility": str(_position_observed_volatility(pos)),
                },
            )
        )
    return (actions, active_tier, active_idx)
