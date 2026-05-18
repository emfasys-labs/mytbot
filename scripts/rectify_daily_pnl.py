#!/usr/bin/env python3
"""
scripts/rectify_daily_pnl.py
============================
One-off, audited, idempotent rectification of the ``daily_pnl`` ledger.

Why
---
The realised-P&L instrumentation (``_compute_today_realised_pnl``) only
began on the production cutoff date. Before it, every ``daily_pnl`` row
has ``realised_pnl = 0`` while fills *did* happen, and several early days
carry fee-unit bugs (e.g. ~$13k fees on 33 fills) and inflated
``trade_count`` values (signals/all-orders, not fills). That
bring-up/dev/test period is **not valid trading P&L** and was the source
of the scary, invalid minuses.

Policy (operator-chosen): *flag the pre-cutoff period as non-production
and zero it* — do NOT invent numbers from buggy orders, do NOT erase any
valid post-cutoff P&L.

What it does
------------
* **date < cutoff**  → realised/unrealised/fees = 0, ``trade_count`` =
  the *actual* filled-order count that day (truthful), and an audit note
  is written into ``strategy_breakdown`` preserving the original values.
* **cutoff ≤ date < today** → realised/unrealised left **untouched**
  (valid canonical P&L). Only the *inflated* ``trade_count`` and
  ``total_fees`` are realigned to the authoritative orders ledger, with
  an audit note. Real P&L is never modified.
* **today's row** → never touched (live; the loop owns it).

Safety
------
* ``--dry-run`` (DEFAULT): prints the full before/after diff, writes
  NOTHING.
* ``--apply``: writes a complete JSON backup of every row to
  ``data/runtime/daily_pnl_backup_<utc>.json`` FIRST, then updates, then
  prints a reconciliation summary.
* Idempotent: a row already carrying the audit marker with matching
  values is skipped; the *original* snapshot is preserved across re-runs.
* Never falsifies: only provably-invalid data is changed, always
  recomputed from the authoritative ``orders`` ledger, fully audited.

Usage
-----
    python scripts/rectify_daily_pnl.py                 # dry-run
    python scripts/rectify_daily_pnl.py --apply
    python scripts/rectify_daily_pnl.py --cutoff 2026-05-13 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

DEFAULT_CUTOFF = "2026-05-13"  # first day realised-P&L instrumentation existed
AUDIT_KEY = "pnl_rectification"


def _d(v: object) -> Decimal:
    try:
        return Decimal(str(v if v is not None else 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


async def _fills_by_day(session) -> dict[str, tuple[int, Decimal]]:
    """Authoritative per-UTC-day (fill_count, fee_sum) from the orders ledger."""
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                """
                SELECT date_trunc('day', timestamp)::date AS d,
                       count(*) AS n,
                       coalesce(sum(coalesce(fee, 0)), 0) AS fees
                FROM orders
                WHERE status IN ('filled', 'partially_filled')
                GROUP BY 1
                """
            )
        )
    ).all()
    out: dict[str, tuple[int, Decimal]] = {}
    for d, n, fees in rows:
        out[str(d)] = (int(n or 0), _d(fees))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="production P&L start (YYYY-MM-DD)")
    ap.add_argument("--backup-dir", default="data/runtime")
    args = ap.parse_args()

    from sqlalchemy import select
    from storage.db import init_async_database
    from storage.models import DailyPnL

    engine, sm = await init_async_database()
    if sm is None:
        raise SystemExit("DB unavailable")

    today_s = datetime.now(timezone.utc).date().isoformat()
    cutoff = args.cutoff.strip()

    async with sm() as session:
        fills = await _fills_by_day(session)
        rows = list(
            (await session.execute(select(DailyPnL).order_by(DailyPnL.date.asc()))).scalars().all()
        )

        backup: list[dict] = []
        planned: list[tuple] = []  # (date, kind, before, after, row)
        for r in rows:
            d = str(r.date)
            sb = dict(r.strategy_breakdown) if isinstance(r.strategy_breakdown, dict) else {}
            before = {
                "realised": str(_d(r.realised_pnl)),
                "unrealised": str(_d(r.unrealised_pnl)),
                "fees": str(_d(r.total_fees)),
                "trades": int(r.trade_count or 0),
            }
            backup.append({"date": d, **before, "strategy_breakdown": r.strategy_breakdown})

            if d >= today_s:
                planned.append((d, "skip_live_today", before, before, r))
                continue

            actual_fills, actual_fees = fills.get(d, (0, Decimal("0")))
            prior_audit = sb.get(AUDIT_KEY) if isinstance(sb.get(AUDIT_KEY), dict) else None
            original = (
                prior_audit.get("original")
                if prior_audit and isinstance(prior_audit.get("original"), dict)
                else before
            )

            if d < cutoff:
                kind = "pre_instrumentation_zeroed"
                after = {
                    "realised": "0",
                    "unrealised": "0",
                    "fees": "0",
                    "trades": int(actual_fills),
                }
            else:
                # Valid P&L stays. Only realign inflated count + fees.
                kind = "count_fee_realigned"
                after = {
                    "realised": before["realised"],   # untouched (valid)
                    "unrealised": before["unrealised"],  # untouched (valid)
                    "fees": str(actual_fees),
                    "trades": int(actual_fills),
                }

            unchanged = (
                after["realised"] == before["realised"]
                and after["unrealised"] == before["unrealised"]
                and Decimal(after["fees"]) == _d(before["fees"])
                and after["trades"] == before["trades"]
                and prior_audit is not None
            )
            if unchanged:
                planned.append((d, "noop_already_rectified", before, after, r))
                continue

            sb[AUDIT_KEY] = {
                "rectified": True,
                "kind": kind,
                "reason": (
                    "pre-2026-05-13 system bring-up — realised-P&L instrumentation "
                    "absent + fee-unit bugs; not valid trading P&L"
                    if kind == "pre_instrumentation_zeroed"
                    else "trade_count/total_fees realigned to authoritative orders "
                    "ledger; realised P&L unchanged"
                ),
                "rectified_at": datetime.now(timezone.utc).isoformat(),
                "cutoff": cutoff,
                "original": original,
            }
            planned.append((d, kind, before, after, r, sb))

    # ---- Report -----------------------------------------------------------
    def _q(v: object) -> str:
        return f"{_d(v):.2f}"

    print(f"\nrectify_daily_pnl | cutoff={cutoff} | mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("-" * 100)
    print(
        f"{'date':<12}{'action':<27}"
        f"{'realised(before->after)':>30}{'fees(before->after)':>26}{'trades':>13}"
    )
    sum_before = Decimal("0")
    sum_after = Decimal("0")
    fees_before = Decimal("0")
    fees_after = Decimal("0")
    changed = 0
    for p in planned:
        d, kind, before, after = p[0], p[1], p[2], p[3]
        sum_before += _d(before["realised"])
        sum_after += _d(after["realised"])
        fees_before += _d(before["fees"])
        fees_after += _d(after["fees"])
        mark = "" if kind in ("skip_live_today", "noop_already_rectified") else " *"
        if mark:
            changed += 1
        r_cell = f"{_q(before['realised'])} -> {_q(after['realised'])}"
        f_cell = f"{_q(before['fees'])} -> {_q(after['fees'])}"
        n_cell = f"{before['trades']} -> {after['trades']}"
        print(f"{d:<12}{kind:<27}{r_cell:>30}{f_cell:>26}{n_cell:>13}{mark}")
    print("-" * 100)
    print(
        f"TOTAL realised  before={sum_before:.2f}  after={sum_after:.2f}  "
        f"(delta={sum_after - sum_before:.2f})  | fees before={fees_before:.2f} "
        f"after={fees_after:.2f} | rows changed={changed}"
    )

    if not args.apply:
        print("\nDRY-RUN -- nothing written. Re-run with --apply to commit (a full "
              "JSON backup is written first).")
        await engine.dispose()
        return 0

    # ---- Apply (backup first) --------------------------------------------
    bdir = Path(args.backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bpath = bdir / f"daily_pnl_backup_{ts}.json"
    bpath.write_text(json.dumps(backup, indent=2, default=str), encoding="utf-8")
    print(f"\nbackup written: {bpath}  ({len(backup)} rows)")

    async with sm() as session:
        from storage.models import DailyPnL as _DP

        n = 0
        for p in planned:
            if p[1] in ("skip_live_today", "noop_already_rectified"):
                continue
            d, after, sb = p[0], p[3], p[5]
            row = (
                await session.execute(select(_DP).where(_DP.date == d).limit(1))
            ).scalars().first()
            if row is None:
                continue
            row.realised_pnl = Decimal(after["realised"])
            row.unrealised_pnl = Decimal(after["unrealised"])
            row.total_fees = Decimal(after["fees"])
            row.trade_count = int(after["trades"])
            row.strategy_breakdown = sb
            n += 1
        await session.commit()
    print(f"APPLIED — {n} row(s) rectified. Backup: {bpath}")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
