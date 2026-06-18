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


def _f(x) -> float:
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return 0.0


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

    per: dict[str, dict] = defaultdict(
        lambda: {
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
    )
    try:
        async with sf() as s:
            stmt = select(
                FillLog.strategy,
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

    for strat, rpnl, fee, notional, hold, slip in rows:
        k = str(strat or "?")
        p = per[k]
        p["all_fills"] += 1
        p["fees_all"] += _f(fee)
        p["notional_all"] += abs(_f(notional))
        if slip is not None:
            p["slippage_bps"].append(_f(slip))
        v = _f(rpnl)
        if abs(v) > 1e-9:  # closing fill
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
    return dict(per)


def build_report(verdicts: dict, live: dict) -> dict:
    strategies = sorted(set(verdicts) | {k for k in live if not k.startswith("_")})
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
    totals = {
        "realised_gross": sum(r["realised_gross"] for r in rows),
        "fees_all": sum(r["fees_all"] for r in rows),
        "net": sum(r["net"] for r in rows),
        "notional_all": sum(r["notional_all"] for r in rows),
        "live_closes": sum(r["live_closes"] for r in rows),
    }
    totals["fee_drag"] = (totals["fees_all"] / totals["notional_all"]) if totals["notional_all"] else 0.0
    return {"rows": rows, "totals": totals}


def print_report(report: dict, verdicts_age: str) -> None:
    rows = report["rows"]
    t = report["totals"]

    print("\n" + "=" * 116)
    print("EDGE SCORECARD - backtest (edge gate) vs live (fills ledger)")
    print(f"verdicts file: {VERDICTS_PATH}  (updated {verdicts_age})")
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
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report, verdicts_age)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
