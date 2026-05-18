"""
system/edge_attribution.py
==========================
Per-bucket / per-symbol **net-of-cost** realised attribution, and the
strict auto-recovering edge governor that consumes it.

Why
---
The aggregate turnover governor (``system.adaptive_edge``) scales the
edge bar by *portfolio-wide* win-rate / return. That dilutes a
chronically money-losing path or symbol — e.g. ``global_edge_trim``
(−$11k) or ``ETH-USD`` (−$19.5k) — inside the average, so it keeps
trading. This module measures realised P&L **net of fees** per
*action-class bucket* and per *symbol* over a trailing window and turns
a persistently negative bucket/symbol into a steeply widened edge
threshold — effectively near-zero turnover while it bleeds, full
automatic recovery the moment its rolling net-of-cost attribution turns
positive again.

This is the project's own evidence-gated governance pattern (inert /
throttled until the data proves it), applied to live allocator churn —
not a hardcoded disable or a symbol blocklist.

Pure + dependency-light. The reconstruction mirrors
``run_m3._compute_today_realised_pnl`` (FIFO / average-cost replay per
(broker, symbol), summing ``gross - fee`` on the closing leg) so the
attribution is consistent with the persisted realised P&L.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


# ── Bucket normalisation ────────────────────────────────────────────────

def normalise_bucket(strategy_name: str | None, kind: str | None = None) -> str:
    """Collapse a strategy / coordinator-action label to a coarse bucket.

    The churn offenders Codex flagged are the *closing* action-classes
    (trim / recycle / rotation / shed); everything else keeps its
    strategy name so a genuinely good strategy is judged on its own.
    """
    s = str(strategy_name or "").strip().lower()
    k = str(kind or "").strip().lower()
    blob = f"{s} {k}"
    if "recycle" in blob:
        return "capital_recycle"
    if "rotation" in blob or "rotate" in blob:
        return "global_edge_rotation"
    if "trim" in blob:
        return "global_edge_trim"
    if "shed" in blob:
        return "adaptive_shed"
    if "flatten" in blob:
        return "global_edge_flatten"
    return s or "unknown"


def _norm_symbol(sym: str | None) -> str:
    return str(sym or "").strip().upper()


# ── Rolling net-of-cost reconstruction ──────────────────────────────────

async def compute_edge_attribution(
    session: Any,
    *,
    window_days: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replay the trailing window's filled orders → net-of-cost P&L by
    bucket and by symbol.

    A *close* (a fill that reduces an open position) realises
    ``gross - fee`` and is attributed to the bucket of the **closing**
    order's strategy (the action-class that chose to close) and to the
    symbol. Returns a plain dict (JSON-friendly, safe to inject into the
    coordinator cfg / log).
    """
    from sqlalchemy import and_, func, select
    from storage.models import OrderLog, PositionLog, SignalLog

    now = now or datetime.now(timezone.utc)
    start_dt = now - timedelta(days=float(window_days))

    rows = list(
        (
            await session.execute(
                select(OrderLog)
                .where(
                    OrderLog.timestamp >= start_dt,
                    OrderLog.status.in_(("filled", "partially_filled")),
                )
                .order_by(OrderLog.timestamp.asc(), OrderLog.id.asc())
            )
        ).scalars().all()
    )
    if not rows:
        return {
            "buckets": {},
            "symbols": {},
            "window_days": float(window_days),
            "computed_at": now.isoformat(),
            "orders": 0,
        }

    # Resolve signal_id → strategy in one query (avoid N+1).
    sig_ids = {str(r.signal_id) for r in rows if getattr(r, "signal_id", None)}
    strat_by_sig: dict[str, str] = {}
    if sig_ids:
        for sid, strat in (
            await session.execute(
                select(SignalLog.id, SignalLog.strategy).where(
                    SignalLog.id.in_(sig_ids)
                )
            )
        ).all():
            strat_by_sig[str(sid)] = str(strat or "")

    state: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    buckets: dict[str, dict[str, float]] = {}
    symbols: dict[str, dict[str, float]] = {}

    def _acc(table: dict[str, dict[str, float]], key: str, net: float) -> None:
        cur = table.setdefault(key, {"net": 0.0, "n": 0.0})
        cur["net"] += net
        cur["n"] += 1.0

    for r in rows:
        broker = str(r.broker or "").strip().lower()
        symbol = _norm_symbol(r.symbol)
        key = (broker, symbol)
        pos_qty, pos_avg = state.get(key, (Decimal("0"), Decimal("0")))
        try:
            fill_qty = Decimal(
                str(r.filled_quantity if r.filled_quantity is not None else r.quantity or 0)
            )
            fill_price = Decimal(
                str(r.avg_fill_price if r.avg_fill_price is not None else r.limit_price or 0)
            )
            fee = Decimal(str(r.fee or 0))
        except (TypeError, ValueError, InvalidOperation):
            continue
        side_l = str(r.side or "").strip().lower()
        if side_l not in {"buy", "sell"} or fill_qty <= 0 or fill_price <= 0:
            continue
        signed_fill = fill_qty if side_l == "buy" else -fill_qty
        closing_qty = Decimal("0")
        gross = Decimal("0")
        if pos_qty > 0 and signed_fill < 0:
            closing_qty = min(pos_qty, abs(signed_fill))
            gross = (fill_price - pos_avg) * closing_qty
        elif pos_qty < 0 and signed_fill > 0:
            closing_qty = min(abs(pos_qty), signed_fill)
            gross = (pos_avg - fill_price) * closing_qty
        if closing_qty > 0:
            fee_alloc = fee * (closing_qty / fill_qty) if fill_qty > 0 else Decimal("0")
            net = float(gross - fee_alloc)
            strat = strat_by_sig.get(str(getattr(r, "signal_id", "") or ""), "")
            _acc(buckets, normalise_bucket(strat), net)
            _acc(symbols, symbol, net)
        # advance position state (same maths as _compute_today_realised_pnl)
        new_qty = pos_qty + signed_fill
        eps = Decimal("0.00000001")
        if abs(new_qty) <= eps:
            new_qty = Decimal("0")
            new_avg = fill_price
        elif pos_qty == 0 or (pos_qty > 0 and signed_fill > 0) or (pos_qty < 0 and signed_fill < 0):
            total_abs = abs(pos_qty) + abs(signed_fill)
            new_avg = (
                ((abs(pos_qty) * pos_avg) + (abs(signed_fill) * fill_price)) / total_abs
                if total_abs > 0
                else fill_price
            )
        elif abs(signed_fill) < abs(pos_qty):
            new_avg = pos_avg
        else:
            new_avg = fill_price
        state[key] = (new_qty, new_avg)

    # ── Stage 2: opener/unrealised-aware. Fold each symbol's CURRENT open
    # unrealised P&L (latest snapshot, all brokers) into its net. This
    # closes the realised-only blind spot: a strategy that keeps OPENING
    # losing crypto (volatility_regime/mean_reversion on Kraken) never
    # realises a loss under its own name, but the symbol's open inventory
    # is deeply red — now the per-symbol governor sees it and clamps every
    # strategy on that symbol (openers included). Auto-recovers when the
    # unrealised improves.
    try:
        latest = (
            select(
                PositionLog.broker.label("broker"),
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("mx"),
            )
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        prows = list(
            (
                await session.execute(
                    select(PositionLog).join(
                        latest,
                        and_(
                            PositionLog.broker == latest.c.broker,
                            PositionLog.symbol == latest.c.symbol,
                            PositionLog.timestamp == latest.c.mx,
                        ),
                    )
                )
            ).scalars().all()
        )
        for p in prows:
            try:
                if Decimal(str(p.quantity or 0)) == 0:
                    continue
                u = float(Decimal(str(p.unrealised_pnl or 0)))
            except (TypeError, ValueError, InvalidOperation):
                continue
            sym = _norm_symbol(p.symbol)
            cur = symbols.setdefault(sym, {"net": 0.0, "n": 0.0})
            cur["net"] += u
            cur["unrealised"] = float(cur.get("unrealised", 0.0)) + u
    except Exception:  # noqa: BLE001 — unrealised overlay is best-effort
        pass

    return {
        "buckets": buckets,
        "symbols": symbols,
        "window_days": float(window_days),
        "computed_at": now.isoformat(),
        "orders": len(rows),
    }


# ── Strict auto-recovering governor ─────────────────────────────────────

def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)))
    except (TypeError, ValueError):
        return default


def governor_multiplier(net: float, n: float) -> float:
    """Map one bucket/symbol's rolling net-of-cost stats → edge-bar ×.

    Strict + auto-recovering:
      * net ≥ 0 with enough samples → 1.0 (proven; full freedom).
      * net < 0 with enough samples → steeply widened, scaled by how
        much it bled (|net| / ref), capped at ``MAX`` (≈ no turnover).
      * not enough clean samples yet → a cautious ``UNPROVEN`` (>1) bar
        that relaxes to 1.0 automatically as positive evidence accrues.
    Monotone: more loss never lowers the multiplier.
    """
    min_samples = _f("EDGE_ATTRIB_MIN_SAMPLES", 8.0)
    ref_loss = max(1.0, _f("EDGE_ATTRIB_REF_LOSS", 1500.0))
    max_mult = max(1.0, _f("EDGE_ATTRIB_MAX_MULT", 8.0))
    unproven = max(1.0, _f("EDGE_ATTRIB_UNPROVEN_MULT", 1.5))

    if n < min_samples:
        # Insufficient evidence. A bleeding-but-thin sample still gets the
        # cautious bar (never *less* than unproven); a thin positive
        # sample also stays cautious until it proves itself.
        return unproven
    if net >= 0.0:
        return 1.0
    severity = min(1.0, abs(net) / ref_loss)
    return 1.0 + severity * (max_mult - 1.0)


def required_threshold_multiplier(
    symbol: str | None,
    strategy_name: str | None,
    attribution: dict[str, Any] | None,
    *,
    kind: str | None = None,
) -> float:
    """Worst-offender multiplier for a proposed (symbol, bucket) action.

    The larger of the symbol's and the bucket's governor multipliers
    governs — a bad symbol on a good strategy (or vice-versa) is still
    throttled. Returns 1.0 (no-op) when attribution is absent so every
    other code path / test is unaffected.
    """
    if not isinstance(attribution, dict):
        return 1.0
    bucket = normalise_bucket(strategy_name, kind)
    sym = _norm_symbol(symbol)
    mult = 1.0
    b = (attribution.get("buckets") or {}).get(bucket)
    if isinstance(b, dict):
        mult = max(mult, governor_multiplier(float(b.get("net", 0.0)), float(b.get("n", 0.0))))
    s = (attribution.get("symbols") or {}).get(sym)
    if isinstance(s, dict):
        s_net = float(s.get("net", 0.0))
        s_n = float(s.get("n", 0.0))
        mult = max(mult, governor_multiplier(s_net, s_n))
        # Open-inventory bleed is a real-time signal, NOT statistical noise:
        # a symbol sitting on a large unrealised loss must clamp every
        # strategy on it (openers included) even with few realised closes
        # — bypass the min-samples gate for that component only.
        s_unreal = float(s.get("unrealised", 0.0))
        ref_loss = max(1.0, _f("EDGE_ATTRIB_REF_LOSS", 1500.0))
        max_mult = max(1.0, _f("EDGE_ATTRIB_MAX_MULT", 8.0))
        unreal_floor = max(1.0, _f("EDGE_ATTRIB_UNREAL_FLOOR", ref_loss))
        if s_unreal <= -unreal_floor:
            severity = min(1.0, abs(s_net) / ref_loss)
            mult = max(mult, 1.0 + severity * (max_mult - 1.0))
    return mult
