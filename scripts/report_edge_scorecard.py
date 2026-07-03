"""
scripts/report_edge_scorecard.py
================================

D163 Phase 0 — the scoreboard.

Read-only report that answers the only question that matters: **is the live
paper book actually earning the edge the backtest promised, after costs?**

It joins two sources of truth:

  1. BACKTEST edge — ``data/state/edge_gate_verdicts.json`` (per-strategy,
     per-side out-of-sample expectancy / profit factor / win rate from the
     walk-forward edge gate).
  2. LIVE realised edge — the ``fills`` ledger (``FillLog``): realised P&L,
     fees, holding period, win rate, computed per strategy from confirmed
     closing fills.

Three blocks are printed:

  * EXPECTANCY SCORECARD — backtest vs live expectancy/trade and profit
    factor side by side, so you can see immediately whether a "proven"
    weapon is delivering or whether reality diverged from the harness.
  * COST LEDGER — total fees, gross realised, NET realised (gross − all
    fees), and fee drag as a fraction of traded notional. This is where
    churn shows up as a tax.
  * CHURN — closing-fill count, median holding period, and the share of
    round-trips that lasted less than a day (the daily-horizon army should
    almost never round-trip intraday).

Strictly read-only. No DB writes, no config changes, no orders.

Run:  python scripts/report_edge_scorecard.py
      python scripts/report_edge_scorecard.py --json
      python scripts/report_edge_scorecard.py --since-hours 168
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

VERDICTS_PATH = Path("data/state/edge_gate_verdicts.json")
STRATEGIES_CONFIG_PATH = Path("config/strategies.yaml")


def _f(x) -> float:
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return 0.0


def backtest_cost_bps() -> dict:
    """Read the edge-gate / backtest cost assumption from config.

    The walk-forward harness charges ``fee_bps + slippage_bps`` on EACH side
    (entry and exit), so a round-trip costs ``2 * (fee_bps + slippage_bps)``.
    That is the cost the ``allowed`` verdicts were proven against. Returns the
    per-side and round-trip assumption in bps. Falls back to the documented
    defaults (10 + 5 = 15/side, 30 round-trip) when config is unreadable.
    """
    fee, slip = 10.0, 5.0
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(STRATEGIES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        bt = raw.get("backtest") or {}
        fee = _f(bt.get("fee_bps", fee))
        slip = _f(bt.get("slippage_bps", slip))
    except Exception:  # noqa: BLE001
        pass
    per_side = fee + slip
    return {"fee_bps": fee, "slippage_bps": slip, "per_side_bps": per_side, "round_trip_bps": per_side * 2.0}


def cost_reconciliation(row: dict, bt_cost: dict) -> dict:
    """Compare a strategy's LIVE round-trip cost vs the backtest assumption.

    ``fee_drag`` is fees / traded notional summed over ALL fills, i.e. the
    average per-fill fee rate; a round trip is two fills, so live round-trip
    fee ≈ ``2 * fee_drag``. Slippage is the average |slippage| per fill, also
    doubled for a round trip. ``BACKTEST_TOO_KIND`` means an ``allowed``
    verdict was proven against costs cheaper than reality is charging — the
    edge may not survive live, so don't trust the harness until investigated.
    """
    live_fee_bps = row.get("fee_drag", 0.0) * 1e4
    avg_slip = row.get("avg_slippage_bps")
    live_slip_bps = abs(avg_slip) if avg_slip is not None else 0.0
    live_round_trip_bps = 2.0 * (live_fee_bps + live_slip_bps)
    bt_round_trip = bt_cost.get("round_trip_bps", 30.0)
    if row.get("live_closes", 0) <= 0:
        flag = "no_live_data"
    elif live_round_trip_bps > bt_round_trip * 1.05:  # 5% tolerance band
        flag = "BACKTEST_TOO_KIND"
    else:
        flag = "OK"
    return {
        "live_fee_bps": live_fee_bps,
        "live_slip_bps": live_slip_bps,
        "live_round_trip_bps": live_round_trip_bps,
        "backtest_round_trip_bps": bt_round_trip,
        "cost_flag": flag,
    }


def verdicts_staleness() -> tuple[float, int] | None:
    """(age_days, max_verdict_age_days) for ``VERDICTS_PATH``, or ``None`` if
    not stale / check disabled / file missing (nothing to warn about)."""
    from backtest.edge_gate import is_verdicts_stale, verdicts_age_days

    max_age_days = 14
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(STRATEGIES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        eg = raw.get("edge_gate") or {}
        if eg.get("max_verdict_age_days") is not None:
            max_age_days = int(eg["max_verdict_age_days"])
    except Exception:  # noqa: BLE001
        pass
    try:
        mtime = VERDICTS_PATH.stat().st_mtime
    except OSError:
        return None
    if not is_verdicts_stale(mtime, max_age_days):
        return None
    return (verdicts_age_days(mtime), max_age_days)


def load_verdicts() -> dict:
    """Return ``{canonical_strategy: {long: {...}, short: {...}}}``."""
    if not VERDICTS_PATH.exists():
        return {}
    try:
        raw = json.loads(VERDICTS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for key, v in (raw.get("verdicts") or {}).items():
        if "#" in key:
            base, side = key.split("#", 1)
        else:
            base, side = key, "long"
        out[base][side] = v
    return out


def _live_pf(win_sum: float, loss_sum: float) -> float:
    """Profit factor = gross wins / |gross losses|."""
    denom = abs(loss_sum)
    if denom <= 1e-9:
        return float("inf") if win_sum > 0 else 0.0
    return win_sum / denom


async def collect_live(since_hours: float | None) -> dict:
    from sqlalchemy import select
    from storage.db import init_async_database
    from storage.models import FillLog

    engine, sf = await init_async_database()
    if sf is None:
        return {"_error": "no_db"}

    cutoff = None
    if since_hours and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    def _new_bucket() -> dict:
        return {
            "closes": 0,
            "realised_gross": 0.0,
            "win_sum": 0.0,
            "loss_sum": 0.0,
            "wins": 0,
            "losses": 0,
            "hold": [],
            "all_fills": 0,
            "fees_all": 0.0,
            "notional_all": 0.0,
            "slippage_bps": [],
        }

    per: dict[str, dict] = defaultdict(_new_bucket)
    # D231 (P1.5) — ``strategy`` on a closing fill names the EXIT mechanism
    # (stop_loss_monitor / capital_recycle / ...), not the strategy that
    # opened the lot. ``opening_strategy`` (added by the D231 migration,
    # NULL on pre-migration fills) is the true entry attribution. This
    # second bucket, keyed by opening_strategy and populated from CLOSING
    # fills only, is what should be joined against the edge-gate verdicts
    # (which are keyed by entry-strategy name) to answer "did trend_following
    # actually make money live" — the exit-mechanism view above cannot.
    per_entry: dict[str, dict] = defaultdict(_new_bucket)
    try:
        async with sf() as s:
            stmt = select(
                FillLog.strategy,
                FillLog.opening_strategy,
                FillLog.realised_pnl,
                FillLog.fee,
                FillLog.notional,
                FillLog.holding_period_sec,
                FillLog.slippage_bps,
            )
            if cutoff is not None:
                stmt = stmt.where(FillLog.timestamp >= cutoff)
            rows = list((await s.execute(stmt)).all())
    finally:
        if engine is not None:
            await engine.dispose()

    for strat, opening_strat, rpnl, fee, notional, hold, slip in rows:
        k = str(strat or "?")
        p = per[k]
        p["all_fills"] += 1
        p["fees_all"] += _f(fee)
        p["notional_all"] += abs(_f(notional))
        if slip is not None:
            p["slippage_bps"].append(_f(slip))
        v = _f(rpnl)
        is_close = abs(v) > 1e-9
        if is_close:
            p["closes"] += 1
            p["realised_gross"] += v
            if v >= 0:
                p["wins"] += 1
                p["win_sum"] += v
            else:
                p["losses"] += 1
                p["loss_sum"] += v
            if hold is not None:
                p["hold"].append(_f(hold))
            if opening_strat:  # None on pre-D231 fills — can't attribute those
                pe = per_entry[str(opening_strat)]
                pe["closes"] += 1
                pe["realised_gross"] += v
                pe["fees_all"] += _f(fee)          # approximation: this fill's own fee only
                pe["notional_all"] += abs(_f(notional))
                if v >= 0:
                    pe["wins"] += 1
                    pe["win_sum"] += v
                else:
                    pe["losses"] += 1
                    pe["loss_sum"] += v
                if hold is not None:
                    pe["hold"].append(_f(hold))
    out = dict(per)
    out["_by_entry_strategy"] = dict(per_entry)
    return out


# D231 (P3.7) — a daily-horizon book (3-day min hold, D166) round-tripping
# a single symbol more than a handful of times inside 24h is churn, not
# strategy activity, almost by definition. The loss-attribution review found
# this by hand (AAPL 30 fills/8d, AUDUSD 29/8d, ETH-USD 55/8d, XRP-USD
# 35-49/8d — several of them net LOSERS purely on fee drag despite
# near-flat/positive raw price moves). This makes that check routine instead
# of requiring manual SQL each time.
CHURN_FILLS_PER_DAY_THRESHOLD = 5


async def collect_symbol_churn(since_hours: float | None) -> list[dict]:
    """Flag ``(broker, symbol, day)`` buckets with more than
    ``CHURN_FILLS_PER_DAY_THRESHOLD`` fills — a day-bucketed proxy for "more
    than N fills in a rolling 24h window" (exact sliding-window churn needs
    per-fill gap analysis; day-bucketing catches the same pattern with one
    query and no false negatives on the cases this review found).
    """
    from sqlalchemy import func, select
    from storage.db import init_async_database
    from storage.models import FillLog

    engine, sf = await init_async_database()
    if sf is None:
        return []

    cutoff = None
    if since_hours and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    try:
        async with sf() as s:
            day_col = func.date_trunc("day", FillLog.timestamp).label("day")
            stmt = (
                select(
                    FillLog.broker,
                    FillLog.symbol,
                    day_col,
                    func.count(FillLog.id).label("fill_count"),
                    func.sum(FillLog.realised_pnl).label("realised"),
                    func.sum(FillLog.fee).label("fees"),
                )
                .group_by(FillLog.broker, FillLog.symbol, day_col)
                .having(func.count(FillLog.id) > CHURN_FILLS_PER_DAY_THRESHOLD)
                .order_by(func.count(FillLog.id).desc())
            )
            if cutoff is not None:
                stmt = stmt.where(FillLog.timestamp >= cutoff)
            rows = list((await s.execute(stmt)).all())
    finally:
        if engine is not None:
            await engine.dispose()

    return [
        {
            "broker": broker,
            "symbol": symbol,
            "day": day.date().isoformat() if day is not None else "?",
            "fill_count": int(fill_count),
            "realised": _f(realised),
            "fees": _f(fees),
            "net": _f(realised) - _f(fees),
        }
        for broker, symbol, day, fill_count, realised, fees in rows
    ]


def print_symbol_churn(rows: list[dict]) -> None:
    print("\n" + "=" * 116)
    print(
        f"SYMBOL CHURN (D231) - (broker,symbol) days with > {CHURN_FILLS_PER_DAY_THRESHOLD} fills "
        "(daily-horizon book should rarely touch one name this often)"
    )
    print("=" * 116)
    if not rows:
        print("  (none — no symbol exceeded the threshold on any day)")
        return
    print(f"{'broker':<12}{'symbol':<12}{'day':<12}{'fills':>7}{'realised':>14}{'fees':>12}{'net':>14}")
    print("-" * 116)
    for r in rows:
        print(
            f"{r['broker']:<12}{r['symbol']:<12}{r['day']:<12}{r['fill_count']:>7}"
            f"{r['realised']:>14,.2f}{r['fees']:>12,.2f}{r['net']:>14,.2f}"
        )


def build_report(verdicts: dict, live: dict) -> dict:
    strategies = sorted(set(verdicts) | {k for k in live if not k.startswith("_")})
    bt_cost = backtest_cost_bps()
    rows = []
    for name in strategies:
        bt = (verdicts.get(name) or {}).get("long") or {}
        bt_m = bt.get("metrics") or {}
        lv = live.get(name) or {}
        closes = lv.get("closes", 0)
        win_rate = (lv.get("wins", 0) / closes) if closes else 0.0
        expectancy = (lv.get("realised_gross", 0.0) / closes) if closes else 0.0
        pf = _live_pf(lv.get("win_sum", 0.0), lv.get("loss_sum", 0.0))
        net = lv.get("realised_gross", 0.0) - lv.get("fees_all", 0.0)
        notional = lv.get("notional_all", 0.0)
        fee_drag = (lv.get("fees_all", 0.0) / notional) if notional else 0.0
        hold = sorted(lv.get("hold", []))
        med_hold = hold[len(hold) // 2] if hold else 0.0
        intraday = sum(1 for h in hold if h < 86400)
        intraday_share = (intraday / len(hold)) if hold else 0.0
        slip = lv.get("slippage_bps", [])
        avg_slip = (sum(slip) / len(slip)) if slip else None
        rows.append(
            {
                "strategy": name,
                "verdict": bt.get("verdict", "-"),
                "size_mult": _f(bt.get("size_multiplier", 0)),
                "bt_expectancy": _f(bt_m.get("expectancy_per_trade", 0)),
                "bt_pf": _f(bt_m.get("profit_factor", 0)),
                "bt_win": _f(bt_m.get("avg_win_rate", 0)),
                "bt_trades": int(_f(bt_m.get("total_trades", 0))),
                "live_closes": closes,
                "live_expectancy": expectancy,
                "live_pf": pf,
                "live_win": win_rate,
                "realised_gross": lv.get("realised_gross", 0.0),
                "fees_all": lv.get("fees_all", 0.0),
                "net": net,
                "notional_all": notional,
                "fee_drag": fee_drag,
                "med_hold_sec": med_hold,
                "intraday_share": intraday_share,
                "avg_slippage_bps": avg_slip,
            }
        )
        rows[-1].update(cost_reconciliation(rows[-1], bt_cost))
    totals = {
        "realised_gross": sum(r["realised_gross"] for r in rows),
        "fees_all": sum(r["fees_all"] for r in rows),
        "net": sum(r["net"] for r in rows),
        "notional_all": sum(r["notional_all"] for r in rows),
        "live_closes": sum(r["live_closes"] for r in rows),
    }
    totals["fee_drag"] = (totals["fees_all"] / totals["notional_all"]) if totals["notional_all"] else 0.0

    # D231 (P1.5) — entry-strategy attribution (opening_strategy), separate
    # from the exit-mechanism rows above. Only entry strategies actually
    # named in the verdicts file are shown (matches what the gate governs).
    by_entry = live.get("_by_entry_strategy", {}) if isinstance(live, dict) else {}
    entry_rows = []
    for name in sorted(verdicts):
        bt = (verdicts.get(name) or {}).get("long") or {}
        bt_m = bt.get("metrics") or {}
        lv = by_entry.get(name) or {}
        closes = lv.get("closes", 0)
        win_rate = (lv.get("wins", 0) / closes) if closes else 0.0
        expectancy = (lv.get("realised_gross", 0.0) / closes) if closes else 0.0
        pf = _live_pf(lv.get("win_sum", 0.0), lv.get("loss_sum", 0.0))
        net = lv.get("realised_gross", 0.0) - lv.get("fees_all", 0.0)
        entry_rows.append(
            {
                "strategy": name,
                "verdict": bt.get("verdict", "-"),
                "bt_pf": _f(bt_m.get("profit_factor", 0)),
                "live_closes": closes,
                "live_expectancy": expectancy,
                "live_pf": pf,
                "live_win": win_rate,
                "net": net,
            }
        )
    return {"rows": rows, "totals": totals, "backtest_cost": bt_cost, "entry_rows": entry_rows}


def print_report(report: dict, verdicts_age: str) -> None:
    rows = report["rows"]
    t = report["totals"]

    print("\n" + "=" * 116)
    print("EDGE SCORECARD - backtest (edge gate) vs live (fills ledger)")
    print(f"verdicts file: {VERDICTS_PATH}  (updated {verdicts_age})")
    stale_days = report.get("verdicts_stale_days")
    if stale_days is not None:
        print(
            f"  ! STALE: {stale_days:.1f} days old (> {report.get('verdicts_max_age_days')}d) — "
            "an 'allowed' verdict this old was not proven against current market conditions "
            "or cost model. Re-run: python scripts/run_edge_gate.py"
        )
    print("=" * 116)
    print(
        f"{'strategy':<20}{'verdict':>12}{'mult':>6}"
        f"{'bt_exp':>10}{'bt_pf':>7}{'bt_win':>7}"
        f"{'|':>3}{'closes':>8}{'live_exp':>11}{'live_pf':>9}{'live_win':>9}"
    )
    print("-" * 116)
    for r in rows:
        live_pf = r["live_pf"]
        live_pf_s = "inf" if live_pf == float("inf") else f"{live_pf:.2f}"
        print(
            f"{r['strategy']:<20}{r['verdict']:>12}{r['size_mult']:>6.2f}"
            f"{r['bt_expectancy']:>10.1f}{r['bt_pf']:>7.2f}{r['bt_win']*100:>6.0f}%"
            f"{'|':>3}{r['live_closes']:>8}{r['live_expectancy']:>11.2f}{live_pf_s:>9}"
            f"{r['live_win']*100:>8.0f}%"
        )

    entry_rows = report.get("entry_rows") or []
    print("\n" + "=" * 116)
    print(
        "ENTRY-STRATEGY LIVE EXPECTANCY (D231, opening_strategy attribution — "
        "the actual answer to 'did this strategy make money live')"
    )
    print("=" * 116)
    if not any(r["live_closes"] for r in entry_rows):
        print(
            "  (no closes with a known opening_strategy yet — this needs closing fills recorded\n"
            "   AFTER the D231 migration; pre-migration fills have opening_strategy=NULL)"
        )
    else:
        print(
            f"{'strategy':<20}{'verdict':>12}{'bt_pf':>7}"
            f"{'|':>3}{'closes':>8}{'live_exp':>11}{'live_pf':>9}{'live_win':>9}{'net':>12}"
        )
        print("-" * 116)
        for r in entry_rows:
            live_pf = r["live_pf"]
            live_pf_s = "inf" if live_pf == float("inf") else f"{live_pf:.2f}"
            flag = ""
            if r["verdict"] == "allowed" and r["live_closes"] > 0 and r["net"] < 0:
                flag = "  ! allowed but net<0 live"
            print(
                f"{r['strategy']:<20}{r['verdict']:>12}{r['bt_pf']:>7.2f}"
                f"{'|':>3}{r['live_closes']:>8}{r['live_expectancy']:>11.2f}{live_pf_s:>9}"
                f"{r['live_win']*100:>8.0f}%{r['net']:>12,.2f}{flag}"
            )

    print("\n" + "=" * 116)
    print("COST LEDGER - net = realised_gross minus ALL fees (open + close); fee_drag = fees / traded notional")
    print("=" * 116)
    print(
        f"{'strategy':<20}{'realised_gross':>16}{'fees':>12}{'NET':>14}"
        f"{'notional':>16}{'fee_drag':>10}{'avg_slip_bps':>14}"
    )
    print("-" * 116)
    for r in rows:
        if r["live_closes"] == 0 and r["fees_all"] == 0:
            continue
        slip_s = "-" if r["avg_slippage_bps"] is None else f"{r['avg_slippage_bps']:.1f}"
        print(
            f"{r['strategy']:<20}{r['realised_gross']:>16,.2f}{r['fees_all']:>12,.2f}"
            f"{r['net']:>14,.2f}{r['notional_all']:>16,.0f}{r['fee_drag']*1e4:>9.1f}b{slip_s:>14}"
        )
    print("-" * 116)
    print(
        f"{'TOTAL':<20}{t['realised_gross']:>16,.2f}{t['fees_all']:>12,.2f}"
        f"{t['net']:>14,.2f}{t['notional_all']:>16,.0f}{t['fee_drag']*1e4:>9.1f}b"
    )

    bt_cost = report.get("backtest_cost", {})
    print("\n" + "=" * 116)
    print(
        "COST-MODEL RECONCILIATION - is live round-trip cost <= the cost the edge gate proved against?"
    )
    print(
        f"  backtest assumption: {bt_cost.get('fee_bps', 0):.1f} fee + {bt_cost.get('slippage_bps', 0):.1f} slip "
        f"= {bt_cost.get('per_side_bps', 0):.1f} bps/side -> {bt_cost.get('round_trip_bps', 0):.1f} bps round-trip"
    )
    print("=" * 116)
    print(
        f"{'strategy':<20}{'live_fee_bps':>14}{'live_slip_bps':>15}"
        f"{'live_rt_bps':>13}{'bt_rt_bps':>11}{'verdict':>20}"
    )
    print("-" * 116)
    any_too_kind = False
    for r in rows:
        if r.get("cost_flag", "no_live_data") == "no_live_data":
            continue
        if r.get("cost_flag") == "BACKTEST_TOO_KIND":
            any_too_kind = True
        print(
            f"{r['strategy']:<20}{r.get('live_fee_bps', 0):>13.1f}b{r.get('live_slip_bps', 0):>14.1f}b"
            f"{r.get('live_round_trip_bps', 0):>12.1f}b{r.get('backtest_round_trip_bps', 0):>10.1f}b"
            f"{r.get('cost_flag', '-'):>20}"
        )
    if not any(r.get("cost_flag", "no_live_data") != "no_live_data" for r in rows):
        print("  (no live closes yet - reconciliation populates as the soak runs)")
    elif any_too_kind:
        print(
            "\n  ! BACKTEST_TOO_KIND: live cost exceeds the harness assumption. An 'allowed' verdict\n"
            "    may NOT survive live - raise backtest fee_bps/slippage_bps to live levels and re-run\n"
            "    the edge gate before trusting (or sizing up) the affected weapon."
        )

    print("\n" + "=" * 116)
    print("CHURN - daily-horizon army should rarely round-trip intraday (<1d)")
    print("=" * 116)
    print(f"{'strategy':<20}{'closes':>8}{'median_hold':>16}{'intraday_share':>16}")
    print("-" * 116)
    for r in rows:
        if r["live_closes"] == 0:
            continue
        h = r["med_hold_sec"]
        hold_s = f"{h/86400:.1f}d" if h >= 86400 else f"{h/3600:.1f}h" if h >= 3600 else f"{h/60:.0f}m"
        print(
            f"{r['strategy']:<20}{r['live_closes']:>8}{hold_s:>16}{r['intraday_share']*100:>15.0f}%"
        )

    print("\n" + "=" * 116)
    print("READING THE BOARD")
    print("=" * 116)
    print("  * live_pf > 1 AND net > 0  -> the weapon is delivering its edge after costs. Keep / scale.")
    print("  * verdict=allowed but net < 0 -> backtest edge is NOT surviving live costs. Investigate")
    print("    cost model / slippage / churn before trusting the harness.")
    print("  * fee_drag high with thin net -> churn tax. Lengthen holds / route cheaper / size up per trade.")
    print("  * verdict=blocked/insufficient_data but live closes > 0 -> leakage of an unproven weapon.\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Edge scorecard: backtest vs live, with cost + churn ledgers.")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of tables")
    ap.add_argument("--since-hours", type=float, default=None, help="only count fills newer than N hours")
    args = ap.parse_args()

    load_dotenv()
    verdicts = load_verdicts()
    try:
        raw = json.loads(VERDICTS_PATH.read_text(encoding="utf-8")) if VERDICTS_PATH.exists() else {}
        verdicts_age = raw.get("updated_at", "unknown")
    except Exception:  # noqa: BLE001
        verdicts_age = "unknown"

    live = await collect_live(args.since_hours)
    if live.get("_error") == "no_db":
        print("NO DB - check POSTGRES_* and that Docker is up")
        return 1

    report = build_report(verdicts, live)
    symbol_churn = await collect_symbol_churn(args.since_hours)
    report["symbol_churn"] = symbol_churn
    stale = verdicts_staleness()
    if stale is not None:
        report["verdicts_stale_days"], report["verdicts_max_age_days"] = stale
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report, verdicts_age)
        print_symbol_churn(symbol_churn)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
