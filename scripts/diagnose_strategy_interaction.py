"""
scripts/diagnose_strategy_interaction.py
========================================

Read-only diagnostic to answer: "why are we not growing?"

Pulls hard numbers from the live DB (no writes) to test the hypotheses:
  1. Per-strategy realised P&L — which strategies have edge, which bleed.
  2. Win/loss skew per strategy (avg win vs avg loss, win rate).
  3. Current open book: gross vs net, long/short split → self-hedging?
  4. Cross-strategy offsetting: are two strategies holding opposite sides
     of the same (or correlated) symbol, cancelling each other's edge?
  5. Churn: round-trips and median holding period (cost drag proxy).

Run:  python scripts/diagnose_strategy_interaction.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def _f(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


async def main() -> int:
    load_dotenv()
    from sqlalchemy import select, func
    from storage.db import init_async_database
    from storage.models import FillLog, PositionLog

    engine, sf = await init_async_database()
    if sf is None:
        print("NO DB — check POSTGRES_* and that Docker is up")
        return 1

    async with sf() as s:
        # ── 1+2. Per-strategy realised P&L, win/loss skew ──────────────────
        rows = list(
            (
                await s.execute(
                    select(FillLog.strategy, FillLog.realised_pnl, FillLog.fee, FillLog.holding_period_sec)
                    .where(FillLog.realised_pnl != 0)
                )
            ).all()
        )
        per = defaultdict(lambda: {"n": 0, "pnl": 0.0, "fee": 0.0, "wins": 0, "loss": 0,
                                   "win_sum": 0.0, "loss_sum": 0.0, "hold": []})
        for strat, rpnl, fee, hold in rows:
            k = str(strat or "?")
            p = per[k]
            v = _f(rpnl)
            p["n"] += 1
            p["pnl"] += v
            p["fee"] += _f(fee)
            if v >= 0:
                p["wins"] += 1; p["win_sum"] += v
            else:
                p["loss"] += 1; p["loss_sum"] += v
            if hold is not None:
                p["hold"].append(_f(hold))

        print("\n" + "=" * 92)
        print("1) PER-STRATEGY REALISED P&L  (closing fills only)")
        print("=" * 92)
        print(f"{'strategy':<26}{'closes':>8}{'net_pnl':>14}{'fees':>12}"
              f"{'win%':>7}{'avg_win':>11}{'avg_loss':>11}{'expectancy':>12}")
        print("-" * 92)
        for k in sorted(per, key=lambda k: per[k]["pnl"]):
            p = per[k]
            n = p["n"] or 1
            winr = 100.0 * p["wins"] / n
            avg_win = p["win_sum"] / p["wins"] if p["wins"] else 0.0
            avg_loss = p["loss_sum"] / p["loss"] if p["loss"] else 0.0
            expectancy = p["pnl"] / n
            print(f"{k:<26}{p['n']:>8}{p['pnl']:>14.2f}{p['fee']:>12.2f}"
                  f"{winr:>6.1f}%{avg_win:>11.2f}{avg_loss:>11.2f}{expectancy:>12.4f}")

        total_pnl = sum(p["pnl"] for p in per.values())
        total_fee = sum(p["fee"] for p in per.values())
        print("-" * 92)
        print(f"{'TOTAL':<26}{sum(p['n'] for p in per.values()):>8}"
              f"{total_pnl:>14.2f}{total_fee:>12.2f}")

        # ── 3. Current open book from latest PositionLog per (broker,symbol) ──
        latest = (
            select(PositionLog.broker, PositionLog.symbol,
                   func.max(PositionLog.timestamp).label("ts"))
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        pos_rows = list(
            (
                await s.execute(
                    select(PositionLog).join(
                        latest,
                        (PositionLog.broker == latest.c.broker)
                        & (PositionLog.symbol == latest.c.symbol)
                        & (PositionLog.timestamp == latest.c.ts),
                    )
                )
            ).scalars().all()
        )
        gross = net = Decimal(0)
        longs = shorts = 0
        live_unreal = Decimal(0)
        open_syms: dict[str, Decimal] = {}
        for r in pos_rows:
            q = Decimal(str(r.quantity or 0))
            if q == 0:
                continue
            mv = q * Decimal(str(r.current_price or 0))
            gross += abs(mv)
            net += mv
            live_unreal += Decimal(str(r.unrealised_pnl or 0))
            if q > 0:
                longs += 1
            else:
                shorts += 1
            open_syms[r.symbol] = open_syms.get(r.symbol, Decimal(0)) + mv

        print("\n" + "=" * 92)
        print("2) CURRENT OPEN BOOK")
        print("=" * 92)
        print(f"open positions : {longs + shorts}  ({longs} long / {shorts} short)")
        print(f"gross exposure : {gross:,.0f}")
        print(f"net  exposure  : {net:,.0f}   ({(net/gross*100) if gross else 0:.1f}% of gross)")
        print(f"live unrealised: {live_unreal:,.2f}")
        if open_syms:
            biggest = sorted(open_syms.items(), key=lambda kv: abs(kv[1]), reverse=True)[:12]
            print("\n largest net positions by |market value|:")
            for sym, mv in biggest:
                print(f"   {sym:<16}{mv:>16,.0f}  {'LONG' if mv > 0 else 'SHORT'}")

        # ── 4. Cross-strategy offsetting on the same symbol ─────────────────
        # Net signed qty per (symbol, strategy) from the fills ledger.
        fl = list(
            (
                await s.execute(
                    select(FillLog.symbol, FillLog.strategy,
                           func.sum(FillLog.signed_quantity))
                    .group_by(FillLog.symbol, FillLog.strategy)
                )
            ).all()
        )
        sym_strat: dict[str, dict[str, float]] = defaultdict(dict)
        for sym, strat, sq in fl:
            sym_strat[sym][str(strat or "?")] = _f(sq)
        offsetting = []
        for sym, sm in sym_strat.items():
            pos = [k for k, v in sm.items() if v > 1e-9]
            neg = [k for k, v in sm.items() if v < -1e-9]
            if pos and neg:
                offsetting.append((sym, pos, neg))
        print("\n" + "=" * 92)
        print("3) CROSS-STRATEGY OFFSETTING  (same symbol, strategies on opposite sides — net edge destroyed)")
        print("=" * 92)
        if not offsetting:
            print("  none found in fills history")
        else:
            print(f"  {len(offsetting)} symbol(s) where strategies took opposite sides:")
            for sym, pos, neg in offsetting[:25]:
                print(f"   {sym:<16} LONG via {pos}  vs  SHORT via {neg}")

        # ── 5. Churn / holding period ───────────────────────────────────────
        all_hold = [h for p in per.values() for h in p["hold"]]
        if all_hold:
            all_hold.sort()
            med = all_hold[len(all_hold) // 2]
            print("\n" + "=" * 92)
            print("4) CHURN")
            print("=" * 92)
            print(f"closing fills (round-trips): {len(all_hold)}")
            print(f"median holding period      : {med/60:.1f} min")
            print(f"shortest 10%               : {all_hold[len(all_hold)//10]/60:.1f} min")

    if engine is not None:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
