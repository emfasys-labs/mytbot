"""api/performance.py
====================
D130 — the fills-based performance scorecard.

This module computes the system's self-measured performance from two
sources of truth:

* the ``fills`` ledger (D126) — every confirmed fill, with
  weighted-average-cost realised P&L, holding period and (D130) per-fill
  slippage. Trade-quality metrics (profit factor, win rate, attribution,
  turnover, fees, holding-period and slippage distributions) are computed
  directly here. They are ALWAYS computable — but with only a few hours
  of soak data they are descriptive, not statistically significant, so
  the payload carries an honest ``data_quality`` block.

* the ``daily_pnl`` ledger — one row per trading day. Time-series risk
  metrics (Sharpe, Sortino, max drawdown, Calmar, CAGR, volatility) need
  a multi-day daily-return series. Until enough rows exist the
  ``time_series`` block returns ``status="insufficient_history"`` rather
  than a misleading number computed from one day.

Nothing here mutates state — it is a pure read/aggregate path.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from storage.models import DailyPnL, FillLog

# A daily-return series shorter than this cannot yield a meaningful
# Sharpe/Sortino/drawdown — below it the time-series block is suppressed.
_MIN_TIMESERIES_DAYS = 20
# Below this many *closing trades* the trade-quality metrics are flagged
# as descriptive-only (not statistically significant).
_MIN_MEANINGFUL_TRADES = 200
_TRADING_DAYS_PER_YEAR = 252

_ZERO = Decimal("0")


def _dec(v: Any) -> Decimal:
    if v is None:
        return _ZERO
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return _ZERO
    if d != d:  # NaN
        return _ZERO
    return d


def _dstr(v: Any) -> str:
    d = _dec(v)
    return "0" if d == 0 else str(d)


def _percentile(sorted_vals: list[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in [0, 1]) of a sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _round(v: Optional[float], ndigits: int = 4) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except Exception:  # noqa: BLE001
        return None


def _bucket() -> dict[str, Any]:
    return {
        "gross_realised": _ZERO,
        "fees": _ZERO,
        "turnover": _ZERO,
        "fills": 0,
        "closing_trades": 0,
        "wins": 0,
        "losses": 0,
    }


def _finalise_bucket(key: str, b: dict[str, Any]) -> dict[str, Any]:
    net = b["gross_realised"] - b["fees"]
    closed = b["closing_trades"]
    decided = b["wins"] + b["losses"]
    return {
        "key": key,
        "net_realised": _dstr(net),
        "gross_realised": _dstr(b["gross_realised"]),
        "fees": _dstr(b["fees"]),
        "turnover": _dstr(b["turnover"]),
        "fills": b["fills"],
        "closing_trades": closed,
        "wins": b["wins"],
        "losses": b["losses"],
        "win_rate": _round(b["wins"] / decided, 4) if decided else None,
    }


async def build_performance_scorecard(
    session_factory,
    *,
    window_days: int = 0,
) -> dict[str, Any]:
    """Compute the full performance scorecard.

    ``window_days`` of 0 means *all history*; a positive value limits the
    fills considered to the trailing N days.
    """
    now = datetime.now(timezone.utc)
    cutoff: Optional[datetime] = None
    if window_days and window_days > 0:
        cutoff = now - timedelta(days=window_days)

    async with session_factory() as session:
        stmt = select(FillLog)
        if cutoff is not None:
            stmt = stmt.where(FillLog.timestamp >= cutoff)
        stmt = stmt.order_by(FillLog.timestamp.asc())
        fills = list((await session.execute(stmt)).scalars().all())

        dq = await session.execute(select(DailyPnL).order_by(DailyPnL.date.asc()))
        daily_rows = list(dq.scalars().all())

    return {
        "as_of": now.isoformat(),
        "window_days": window_days or None,
        **_fills_section(fills),
        **{"time_series": _time_series_section(daily_rows)},
    }


def _fills_section(fills: list[FillLog]) -> dict[str, Any]:
    total = len(fills)

    gross_realised = _ZERO
    total_fees = _ZERO
    turnover = _ZERO
    opening = 0
    closing = 0

    wins = 0
    losses = 0
    breakeven = 0
    gross_profit = _ZERO
    gross_loss = _ZERO          # held as a positive magnitude
    largest_win = _ZERO
    largest_loss = _ZERO        # most-negative realised_pnl

    holding_secs: list[float] = []
    slip_bps: list[float] = []
    slip_cost = _ZERO

    by_strategy: dict[str, dict[str, Any]] = {}
    by_broker: dict[str, dict[str, Any]] = {}
    by_asset: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}

    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    for f in fills:
        fee = _dec(f.fee)
        notional = _dec(f.notional)
        realised = _dec(f.realised_pnl)
        total_fees += fee
        turnover += notional

        ts = f.timestamp
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        is_closing = f.holding_period_sec is not None or realised != 0
        if is_closing:
            closing += 1
            gross_realised += realised
            if realised > 0:
                wins += 1
                gross_profit += realised
                largest_win = max(largest_win, realised)
            elif realised < 0:
                losses += 1
                gross_loss += -realised
                largest_loss = min(largest_loss, realised)
            else:
                breakeven += 1
        else:
            opening += 1

        if f.holding_period_sec is not None:
            try:
                holding_secs.append(float(f.holding_period_sec))
            except Exception:  # noqa: BLE001
                pass

        if f.slippage_bps is not None:
            try:
                bps = float(f.slippage_bps)
                slip_bps.append(bps)
                slip_cost += _dec(f.slippage_bps) / Decimal("10000") * notional
            except Exception:  # noqa: BLE001
                pass

        # ── attribution ─────────────────────────────────────────────
        for table, raw_key in (
            (by_strategy, f.strategy),
            (by_broker, f.broker),
            (by_asset, f.asset_class),
            (by_symbol, f.symbol),
        ):
            key = (raw_key or "unknown").strip() or "unknown"
            b = table.get(key)
            if b is None:
                b = _bucket()
                table[key] = b
            b["fills"] += 1
            b["fees"] += fee
            b["turnover"] += notional
            if is_closing:
                b["closing_trades"] += 1
                b["gross_realised"] += realised
                if realised > 0:
                    b["wins"] += 1
                elif realised < 0:
                    b["losses"] += 1

    decided = wins + losses
    net_realised = gross_realised - total_fees

    profit_factor: Optional[float] = None
    if gross_loss > 0:
        profit_factor = _round(float(gross_profit / gross_loss), 4)
    elif gross_profit > 0:
        profit_factor = None  # no losses yet — undefined, not "infinite"

    avg_win = (gross_profit / wins) if wins else _ZERO
    avg_loss = (gross_loss / losses) if losses else _ZERO
    payoff_ratio: Optional[float] = None
    if avg_loss > 0:
        payoff_ratio = _round(float(avg_win / avg_loss), 4)
    expectancy = (gross_realised / closing) if closing else _ZERO

    span_hours: Optional[float] = None
    if first_ts is not None and last_ts is not None:
        span_hours = _round((last_ts - first_ts).total_seconds() / 3600.0, 2)

    holding = None
    if holding_secs:
        hs = sorted(holding_secs)
        holding = {
            "count": len(hs),
            "mean_sec": _round(sum(hs) / len(hs), 1),
            "p50_sec": _round(_percentile(hs, 0.50), 1),
            "p90_sec": _round(_percentile(hs, 0.90), 1),
            "min_sec": _round(hs[0], 1),
            "max_sec": _round(hs[-1], 1),
        }

    slippage: dict[str, Any] = {
        "captured_fills": len(slip_bps),
        "coverage_pct": _round(100.0 * len(slip_bps) / total, 1) if total else 0.0,
        "note": (
            "Slippage is captured forward-only from D130. Pre-D130 fills "
            "carry no intended price and are excluded from this section."
        ),
    }
    if slip_bps:
        sb = sorted(slip_bps)
        slippage.update(
            {
                "mean_bps": _round(sum(sb) / len(sb), 2),
                "p50_bps": _round(_percentile(sb, 0.50), 2),
                "p90_bps": _round(_percentile(sb, 0.90), 2),
                "worst_bps": _round(sb[-1], 2),
                "best_bps": _round(sb[0], 2),
                "estimated_cost": _dstr(slip_cost),
            }
        )

    def _ranked(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [_finalise_bucket(k, v) for k, v in table.items()]
        rows.sort(key=lambda r: _dec(r["net_realised"]), reverse=True)
        return rows

    symbol_rows = _ranked(by_symbol)

    return {
        "fills": {
            "total": total,
            "opening": opening,
            "closing": closing,
            "first_fill_at": first_ts.isoformat() if first_ts else None,
            "last_fill_at": last_ts.isoformat() if last_ts else None,
            "span_hours": span_hours,
        },
        "pnl": {
            "gross_realised": _dstr(gross_realised),
            "total_fees": _dstr(total_fees),
            "net_realised": _dstr(net_realised),
            "turnover": _dstr(turnover),
        },
        "trade_quality": {
            "closing_trades": closing,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": _round(wins / decided, 4) if decided else None,
            "profit_factor": profit_factor,
            "avg_win": _dstr(avg_win),
            "avg_loss": _dstr(avg_loss),
            "payoff_ratio": payoff_ratio,
            "expectancy": _dstr(expectancy),
            "largest_win": _dstr(largest_win),
            "largest_loss": _dstr(largest_loss),
        },
        "holding_period": holding,
        "slippage": slippage,
        "attribution": {
            "by_strategy": _ranked(by_strategy),
            "by_broker": _ranked(by_broker),
            "by_asset_class": _ranked(by_asset),
            "by_symbol_top": symbol_rows[:10],
            "by_symbol_bottom": list(reversed(symbol_rows[-10:])) if len(symbol_rows) > 10 else [],
        },
        "data_quality": {
            "statistically_meaningful": closing >= _MIN_MEANINGFUL_TRADES,
            "closing_trades": closing,
            "min_trades_for_significance": _MIN_MEANINGFUL_TRADES,
            "note": (
                f"Trade-quality metrics are computed from {closing} closing "
                f"trade(s)"
                + (
                    f" over {span_hours}h. Treat them as descriptive only "
                    "until the soak accumulates a larger sample."
                    if span_hours is not None
                    else "."
                )
            ),
        },
    }


def _time_series_section(daily_rows: list[DailyPnL]) -> dict[str, Any]:
    """Sharpe / Sortino / drawdown / Calmar / CAGR from ``daily_pnl``.

    Returns ``status="insufficient_history"`` until the daily series is
    long enough for the numbers to mean anything.
    """
    n = len(daily_rows)
    if n < _MIN_TIMESERIES_DAYS:
        return {
            "status": "insufficient_history",
            "daily_rows": n,
            "min_required": _MIN_TIMESERIES_DAYS,
            "metrics": None,
            "message": (
                f"Time-series risk metrics need at least {_MIN_TIMESERIES_DAYS} "
                f"daily P&L rows; {n} present. Sharpe, Sortino, max drawdown, "
                "Calmar, CAGR and volatility are suppressed until the soak "
                "builds enough runway."
            ),
        }

    # Portfolio-value series → daily simple returns.
    pv = [float(_dec(r.portfolio_value)) for r in daily_rows]
    returns: list[float] = []
    for i in range(1, len(pv)):
        prev = pv[i - 1]
        if prev > 0:
            returns.append((pv[i] - prev) / prev)
    if len(returns) < 2:
        return {
            "status": "insufficient_history",
            "daily_rows": n,
            "min_required": _MIN_TIMESERIES_DAYS,
            "metrics": None,
            "message": "Daily portfolio-value series has too few usable points.",
        }

    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    downside = [r for r in returns if r < 0]
    dvar = (sum(r ** 2 for r in downside) / len(downside)) if downside else 0.0
    dstd = math.sqrt(dvar)

    ann = math.sqrt(_TRADING_DAYS_PER_YEAR)
    sharpe = (mean_r / std * ann) if std > 0 else None
    sortino = (mean_r / dstd * ann) if dstd > 0 else None

    # Max drawdown over the portfolio-value curve.
    peak = pv[0]
    max_dd = 0.0
    for v in pv:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)

    # CAGR from first to last portfolio value.
    cagr: Optional[float] = None
    if pv[0] > 0 and pv[-1] > 0:
        years = max(len(pv) / _TRADING_DAYS_PER_YEAR, 1e-9)
        cagr = (pv[-1] / pv[0]) ** (1.0 / years) - 1.0

    calmar: Optional[float] = None
    if cagr is not None and max_dd > 0:
        calmar = cagr / max_dd

    return {
        "status": "available",
        "daily_rows": n,
        "min_required": _MIN_TIMESERIES_DAYS,
        "metrics": {
            "sharpe": _round(sharpe, 3),
            "sortino": _round(sortino, 3),
            "max_drawdown_pct": _round(max_dd * 100.0, 3),
            "calmar": _round(calmar, 3),
            "cagr_pct": _round(cagr * 100.0, 3) if cagr is not None else None,
            "annualised_volatility_pct": _round(std * ann * 100.0, 3),
            "best_day_pct": _round(max(returns) * 100.0, 3),
            "worst_day_pct": _round(min(returns) * 100.0, 3),
        },
        "message": None,
    }
