"""
risk/protective_exit_gate.py
============================
D166 (Phase 2) — horizon-aware anti-churn gate for the protective-exit layers.

The 2026-06-18 profitability audit (D163 scoreboard) showed the entire
realised loss (-$49k) sat in the MANAGEMENT layers — ``stop_loss_monitor``,
``intraday_derisk_monitor``, ``aggregate_derisk`` — which were cutting
DAILY-horizon theses on INTRADAY noise (median hold ~8.4h, 75% intraday)
before any thesis could mature. The four proven daily weapons never even
round-tripped.

The orchestrator already protects a maturing position from a marginal
*flat-flip* (D158 Phase 3.1 ``min_hold_sec_before_flip`` = 3 days). The
two independent background monitors (stop-loss + intraday derisk) had NO
such protection — this module gives them the same horizon awareness.

This is anti-CHURN, not a risk bypass (rule 2 is intact):

  * A *soft* protective cut (position-% stop, per-position loss tier, the
    milder aggregate tiers) is SUPPRESSED only while the position is younger
    than ``min_hold_sec``.
  * A genuinely CATASTROPHIC loss (beyond the NAV-% or position-% band), a
    STRUCTURAL ATR stop, and the MOST-SEVERE aggregate survival tier ALWAYS
    fire regardless of age. The portfolio survives first.

Everything here is pure decision logic — no I/O, all ``Decimal`` (rule 3).
The caller (orchestrator) supplies the position age (computed from the
``fills`` ledger) and routes the surviving actions through the unchanged
RiskEngine + ExecutionEngine path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

_ZERO = Decimal("0")


def _to_decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ProtectiveExitConfig:
    """Parsed ``config/risk_limits.yaml::protective_exit_min_hold`` block."""

    enabled: bool = False
    # A soft protective cut is suppressed while the position is younger than this.
    min_hold_sec: Decimal = _ZERO
    # ... UNLESS the single position's loss is catastrophic by NAV-% ...
    catastrophic_loss_pct_nav: Decimal = _ZERO
    # ... or by a fraction of its OWN cost basis.
    catastrophic_loss_pct_position: Decimal = _ZERO
    # A structural ATR stop is a thesis-invalidation signal, not noise.
    always_allow_structural_stop: bool = True
    # The most-severe aggregate derisk tier is the portfolio survival floor.
    always_allow_most_severe_aggregate_tier: bool = True
    # Explicit stop-loss decisions are thesis invalidation, not allocator
    # churn. These switches keep the min-hold shield scoped to soft derisk.
    always_allow_portfolio_stop: bool = False
    always_allow_position_stop: bool = False
    # Asset classes that should not receive daily-horizon min-hold protection
    # in the protective-exit monitors.
    always_allow_asset_classes: frozenset[str] = field(default_factory=frozenset)
    # Some asset classes move too quickly for a point-in-time position stop to
    # be treated as daily-horizon noise. If their explicit position stop fires,
    # let that stop through even while the position is young.
    always_allow_position_stop_asset_classes: frozenset[str] = field(default_factory=frozenset)


def parse_protective_exit_config(raw: Mapping[str, Any] | None) -> ProtectiveExitConfig:
    """Parse the YAML block. A missing/disabled block returns a disabled gate
    (i.e. the pre-D166 protective behaviour is preserved exactly)."""
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return ProtectiveExitConfig(enabled=False)
    raw_position_stop_classes = raw.get("always_allow_position_stop_asset_classes") or ()
    if isinstance(raw_position_stop_classes, str):
        raw_position_stop_classes = [raw_position_stop_classes]
    raw_always_allow_classes = raw.get("always_allow_asset_classes") or ()
    if isinstance(raw_always_allow_classes, str):
        raw_always_allow_classes = [raw_always_allow_classes]
    always_allow_classes = frozenset(
        str(item).strip().lower()
        for item in raw_always_allow_classes
        if str(item).strip()
    )
    position_stop_classes = frozenset(
        str(item).strip().lower()
        for item in raw_position_stop_classes
        if str(item).strip()
    )
    return ProtectiveExitConfig(
        enabled=True,
        min_hold_sec=_to_decimal(raw.get("min_hold_sec")),
        catastrophic_loss_pct_nav=_to_decimal(raw.get("catastrophic_loss_pct_nav")),
        catastrophic_loss_pct_position=_to_decimal(raw.get("catastrophic_loss_pct_position")),
        always_allow_structural_stop=bool(raw.get("always_allow_structural_stop", True)),
        always_allow_most_severe_aggregate_tier=bool(
            raw.get("always_allow_most_severe_aggregate_tier", True)
        ),
        always_allow_portfolio_stop=bool(
            raw.get("always_allow_portfolio_stop", False)
        ),
        always_allow_position_stop=bool(
            raw.get("always_allow_position_stop", False)
        ),
        always_allow_asset_classes=always_allow_classes,
        always_allow_position_stop_asset_classes=position_stop_classes,
    )


def position_age_seconds_from_fills(
    fills: Iterable[Any],
    *,
    now: datetime | None = None,
) -> Optional[Decimal]:
    """Age (seconds) of the CURRENT open streak for one ``(broker, symbol)``.

    ``fills`` is an iterable of rows each exposing ``timestamp`` (aware
    ``datetime``) and ``position_qty_after`` (signed running position) — either
    as attributes or mapping keys. The open streak begins at the first fill
    AFTER the position was last flat (``position_qty_after == 0``); the age is
    ``now - streak_start``.

    Returns ``None`` when the age cannot be determined (no fills, or the
    position is currently flat) — the caller treats ``None`` as "unknown" and
    falls back to the pre-existing protective behaviour (never suppresses on
    missing evidence).
    """
    rows: list[tuple[datetime, Decimal]] = []
    for f in fills:
        ts = _get(f, "timestamp")
        qty_after = _to_decimal(_get(f, "position_qty_after"), _ZERO)
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rows.append((ts, qty_after))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    # Position is flat now → no open streak to age.
    if rows[-1][1] == 0:
        return None
    # Walk back to the last flat point; the streak starts at the next fill.
    streak_start = rows[0][0]
    for i in range(len(rows) - 1, -1, -1):
        if rows[i][1] == 0:
            streak_start = rows[i + 1][0] if i + 1 < len(rows) else rows[-1][0]
            break
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    age = (now_dt - streak_start).total_seconds()
    return Decimal(str(age)) if age >= 0 else _ZERO


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def should_suppress_protective_exit(
    *,
    config: ProtectiveExitConfig,
    age_sec: Decimal | None,
    loss_pct_nav: Decimal,
    loss_pct_position: Decimal,
    structural_breach: bool = False,
    is_most_severe_aggregate_tier: bool = False,
    asset_class: str | None = None,
    portfolio_stop_breached: bool = False,
    position_stop_breached: bool = False,
) -> tuple[bool, str]:
    """Decide whether to SUPPRESS a soft protective cut on a young position.

    ``loss_pct_nav`` / ``loss_pct_position`` are POSITIVE loss magnitudes
    (this position's unrealised loss as a fraction of NAV, and of its own
    cost basis). Returns ``(suppress, reason)``.

    Order of precedence (anything that should always fire returns
    ``suppress=False`` immediately):

      1. gate disabled                         -> allow (pre-D166 behaviour)
      2. structural ATR stop breached          -> allow (thesis invalidated)
      3. most-severe aggregate survival tier   -> allow (portfolio first)
      4. explicit portfolio/position stop       -> allow (thesis invalidated)
      5. asset-class exception                 -> allow
      6. asset-class position-stop exception   -> allow
      7. catastrophic NAV-% loss               -> allow
      8. catastrophic position-% loss          -> allow
      9. age unknown                           -> allow (no evidence to gate on)
      10. age < min_hold_sec                   -> SUPPRESS (let it mature)
      11. otherwise (matured)                  -> allow
    """
    if not config.enabled:
        return (False, "gate_disabled")
    if structural_breach and config.always_allow_structural_stop:
        return (False, "structural_stop")
    if is_most_severe_aggregate_tier and config.always_allow_most_severe_aggregate_tier:
        return (False, "most_severe_tier")
    if portfolio_stop_breached and config.always_allow_portfolio_stop:
        return (False, "portfolio_stop")
    if position_stop_breached and config.always_allow_position_stop:
        return (False, "position_stop")
    asset_class_key = str(asset_class or "").strip().lower()
    if asset_class_key and asset_class_key in config.always_allow_asset_classes:
        return (False, f"asset_class_{asset_class_key}")
    if (
        position_stop_breached
        and asset_class_key
        and asset_class_key in config.always_allow_position_stop_asset_classes
    ):
        return (False, f"position_stop_{asset_class_key}")
    if config.catastrophic_loss_pct_nav > 0 and loss_pct_nav >= config.catastrophic_loss_pct_nav:
        return (False, "catastrophic_nav")
    if (
        config.catastrophic_loss_pct_position > 0
        and loss_pct_position >= config.catastrophic_loss_pct_position
    ):
        return (False, "catastrophic_position")
    if age_sec is None:
        return (False, "age_unknown")
    if config.min_hold_sec > 0 and age_sec < config.min_hold_sec:
        return (True, "within_min_hold")
    return (False, "matured")
