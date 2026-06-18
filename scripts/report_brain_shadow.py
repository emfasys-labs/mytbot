"""
scripts/report_brain_shadow.py
==============================

D169 (Phase 4) — the brain shadow scorecard.

Phase 1 (D163) SHADOWED the one clearly-unproven active brain layer: the
trained meta-labeller (``signal_engine.use_trained_meta_labeler: false``).
Phase 4 re-admits it *evidence-first*: D169 runs it in SHADOW (score every
signal, stamp the would-keep/would-drop decision on the signal metadata,
but NEVER drop on it) and this read-only report grades those shadow
decisions against the live fills SCOREBOARD.

The question it answers: **if we had let the trained meta-labeller filter
entries, would the live book have made more money?**

How it joins the two sources of truth:

  1. SHADOW DECISIONS — ``signals`` rows whose ``metadata.meta_label_shadow``
     is true and that carry a real ``meta_label_probability`` (the model
     actually scored). Each is an ENTRY opinion: would-KEEP vs would-DROP.
  2. REALISED OUTCOME — the ``fills`` ledger. We reconstruct each
     ``(broker, symbol)`` position lifecycle into round-trip "streaks"
     (open → flat) from the signed ``position_qty_after``; a streak's
     realised P&L is the sum of its closing-fill ``realised_pnl``.

Each round-trip streak is attributed to the shadow ENTRY decision nearest
its open (same symbol, same side, decision at/just before the streak
start). The streak's realised P&L then lands in the KEEP or DROP bucket
according to what the meta-labeller said about that entry.

VERDICT:
  * RE-ADMIT          — would-DROP round-trips were net-negative by a
                        material margin AND would-KEEP net >= would-DROP net
                        (filtering them out would have improved the book),
                        with enough attributed round-trips on both sides.
  * DO_NOT_ADMIT      — would-DROP round-trips were net-positive (the model
                        would have thrown away good trades).
  * INSUFFICIENT_DATA — not enough attributed round-trips yet (the shadow
                        soak needs to accrue).

Strictly read-only. No DB writes, no config changes, no orders.

Run:  python scripts/report_brain_shadow.py
      python scripts/report_brain_shadow.py --json
      python scripts/report_brain_shadow.py --since-hours 336 --min-sample 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _as_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "on")
    return bool(x)


def _norm_side(side: Any) -> str:
    s = str(side or "").strip().lower()
    return "long" if s in ("buy", "long", "b") else "short"


# ── pure helpers (unit-tested) ───────────────────────────────────────────────


def extract_shadow_decisions(signal_rows: Iterable[tuple]) -> list[dict]:
    """From ``(symbol, side, timestamp, metadata)`` rows, keep only the ones
    the shadow meta-labeller actually scored.

    A usable shadow decision has ``meta_label_shadow == true`` and a
    non-null ``meta_label_probability`` (a passthrough / not-approved row
    has ``probability == None`` and is ignored — it carries no opinion).
    """
    out: list[dict] = []
    for symbol, side, ts, metadata in signal_rows:
        md = metadata if isinstance(metadata, dict) else {}
        if not _as_bool(md.get("meta_label_shadow")):
            continue
        prob = md.get("meta_label_probability")
        if prob is None:
            continue
        out.append(
            {
                "symbol": str(symbol),
                "side": _norm_side(side),
                "ts": ts,
                "kept": _as_bool(md.get("meta_label_kept")),
                "probability": _to_float(prob),
                "threshold": _to_float(md.get("meta_label_threshold")),
            }
        )
    return out


def reconstruct_streaks(fill_rows: Iterable[tuple]) -> list[dict]:
    """Reconstruct round-trip position "streaks" per ``(broker, symbol)``.

    ``fill_rows`` items: ``(broker, symbol, timestamp, realised_pnl,
    position_qty_after, notional)``. A streak starts when the running
    position leaves flat and ends when it returns to flat; its realised
    P&L is the sum of the realised_pnl of every fill in the streak, and
    its direction is the sign of the position held during the streak.
    Open (not-yet-flat) streaks at the end are still returned with
    ``closed=False`` so the report can note them.
    """
    by_key: dict[tuple, list[tuple]] = defaultdict(list)
    for broker, symbol, ts, rpnl, qty_after, notional in fill_rows:
        by_key[(str(broker), str(symbol))].append(
            (ts, _to_float(rpnl), _to_float(qty_after), abs(_to_float(notional)))
        )

    streaks: list[dict] = []
    for (broker, symbol), rows in by_key.items():
        rows.sort(key=lambda r: r[0])
        cur: dict | None = None
        prev_qty = 0.0
        for ts, rpnl, qty_after, notional in rows:
            # A streak opens on the transition out of flat.
            if cur is None and abs(qty_after) > 1e-12:
                cur = {
                    "broker": broker,
                    "symbol": symbol,
                    "start_ts": ts,
                    "end_ts": ts,
                    "direction": "long" if qty_after > 0 else "short",
                    "realised_pnl": 0.0,
                    "entry_notional": notional,
                    "fills": 0,
                    "closed": False,
                }
            if cur is not None:
                cur["realised_pnl"] += rpnl
                cur["end_ts"] = ts
                cur["fills"] += 1
                # The direction is anchored at open; a flip through zero is
                # rare for the netted orchestrator, but if it happens the
                # streak closes at flat and a new one opens next iteration.
                if abs(qty_after) <= 1e-12:
                    cur["closed"] = True
                    streaks.append(cur)
                    cur = None
            prev_qty = qty_after
        if cur is not None:
            streaks.append(cur)  # still-open streak (closed=False)
    return streaks


def attribute_streaks(decisions: list[dict], streaks: list[dict]) -> dict:
    """Attribute each CLOSED round-trip streak to the nearest shadow entry
    decision (same symbol + direction, decision at/just before the open).

    Returns ``{"keep": bucket, "drop": bucket, "unattributed_streaks": int,
    "decisions": int}`` where each bucket has ``count``, ``net_realised``,
    ``wins``, ``losses`` and ``avg_probability``.
    """
    # Index decisions by (symbol, side), each sorted by ts ascending.
    by_sym: dict[tuple, list[dict]] = defaultdict(list)
    for d in decisions:
        if d["ts"] is not None:
            by_sym[(d["symbol"], d["side"])].append(d)
    for lst in by_sym.values():
        lst.sort(key=lambda d: d["ts"])

    def _empty() -> dict:
        return {"count": 0, "net_realised": 0.0, "wins": 0, "losses": 0, "prob_sum": 0.0}

    keep, drop = _empty(), _empty()
    unattributed = 0

    for st in streaks:
        if not st.get("closed"):
            continue
        cands = by_sym.get((st["symbol"], st["direction"]))
        if not cands:
            unattributed += 1
            continue
        # Controlling decision = latest decision at/before the streak open
        # (the entry that opened it); fall back to the earliest decision
        # within the streak window if none precedes the open.
        start = st["start_ts"]
        end = st["end_ts"]
        chosen: dict | None = None
        for d in cands:
            if d["ts"] <= start:
                chosen = d  # keep advancing → latest before/at open
            elif start < d["ts"] <= end and chosen is None:
                chosen = d
                break
        if chosen is None:
            unattributed += 1
            continue
        bucket = keep if chosen["kept"] else drop
        pnl = st["realised_pnl"]
        bucket["count"] += 1
        bucket["net_realised"] += pnl
        bucket["prob_sum"] += chosen["probability"]
        if pnl >= 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1

    for b in (keep, drop):
        b["avg_probability"] = (b["prob_sum"] / b["count"]) if b["count"] else 0.0
        del b["prob_sum"]

    return {
        "keep": keep,
        "drop": drop,
        "unattributed_streaks": unattributed,
        "decisions": len(decisions),
    }


def build_verdict(
    attribution: dict,
    *,
    min_sample: int = 8,
    material_loss: float = 1.0,
) -> dict:
    """Decide whether the shadow meta-labeller earns re-admission.

    Re-admission requires evidence that filtering the would-DROP entries
    would have improved the book: the dropped round-trips were net-negative
    by at least ``material_loss``, and the kept round-trips did at least as
    well as the dropped ones — with at least ``min_sample`` attributed
    round-trips on BOTH sides (no acting on a handful of trades).
    """
    keep = attribution["keep"]
    drop = attribution["drop"]
    n_keep, n_drop = keep["count"], drop["count"]
    if n_keep < min_sample or n_drop < min_sample:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "reason": (
                f"need >= {min_sample} attributed round-trips per side "
                f"(keep={n_keep}, drop={n_drop})"
            ),
        }
    drop_net = drop["net_realised"]
    keep_net = keep["net_realised"]
    if drop_net >= 0:
        return {
            "verdict": "DO_NOT_ADMIT",
            "reason": (
                f"would-DROP round-trips were net-POSITIVE ({drop_net:,.2f}) — "
                "the model would have thrown away good trades"
            ),
        }
    if drop_net <= -abs(material_loss) and keep_net >= drop_net:
        return {
            "verdict": "RE-ADMIT",
            "reason": (
                f"would-DROP net {drop_net:,.2f} (loser) vs would-KEEP net "
                f"{keep_net:,.2f} — filtering the dropped entries improves the book"
            ),
        }
    return {
        "verdict": "DO_NOT_ADMIT",
        "reason": (
            f"would-DROP net {drop_net:,.2f} not materially negative or "
            f"would-KEEP net {keep_net:,.2f} worse — no clear improvement"
        ),
    }


# ── DB collection ────────────────────────────────────────────────────────────


async def collect(since_hours: float | None) -> dict:
    from sqlalchemy import select
    from storage.db import init_async_database
    from storage.models import SignalLog, FillLog

    engine, sf = await init_async_database()
    if sf is None:
        return {"_error": "no_db"}

    cutoff = None
    if since_hours and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    try:
        async with sf() as s:
            sig_stmt = select(
                SignalLog.symbol,
                SignalLog.side,
                SignalLog.timestamp,
                SignalLog.metadata_,
            )
            if cutoff is not None:
                sig_stmt = sig_stmt.where(SignalLog.timestamp >= cutoff)
            signal_rows = list((await s.execute(sig_stmt)).all())

            fill_stmt = select(
                FillLog.broker,
                FillLog.symbol,
                FillLog.timestamp,
                FillLog.realised_pnl,
                FillLog.position_qty_after,
                FillLog.notional,
            )
            if cutoff is not None:
                fill_stmt = fill_stmt.where(FillLog.timestamp >= cutoff)
            fill_rows = list((await s.execute(fill_stmt)).all())
    finally:
        if engine is not None:
            await engine.dispose()

    decisions = extract_shadow_decisions(signal_rows)
    streaks = reconstruct_streaks(fill_rows)
    attribution = attribute_streaks(decisions, streaks)
    return {
        "decisions": decisions,
        "streak_count": len(streaks),
        "closed_streaks": sum(1 for st in streaks if st.get("closed")),
        "attribution": attribution,
        "signal_rows": len(signal_rows),
        "fill_rows": len(fill_rows),
    }


def _decision_summary(decisions: list[dict]) -> dict:
    kept = [d for d in decisions if d["kept"]]
    dropped = [d for d in decisions if not d["kept"]]
    return {
        "total": len(decisions),
        "would_keep": len(kept),
        "would_drop": len(dropped),
        "avg_prob_keep": (sum(d["probability"] for d in kept) / len(kept)) if kept else 0.0,
        "avg_prob_drop": (sum(d["probability"] for d in dropped) / len(dropped)) if dropped else 0.0,
    }


def print_report(result: dict, min_sample: int) -> None:
    dec = result["decisions"]
    summ = _decision_summary(dec)
    attribution = result["attribution"]
    keep, drop = attribution["keep"], attribution["drop"]
    verdict = build_verdict(attribution, min_sample=min_sample)

    print("\n" + "=" * 92)
    print("BRAIN SHADOW SCORECARD - trained meta-labeller (shadow) vs live fills")
    print("=" * 92)
    print(
        f"shadow signals scored: {summ['total']}  "
        f"(would-KEEP {summ['would_keep']}, would-DROP {summ['would_drop']})"
    )
    print(
        f"avg probability: keep={summ['avg_prob_keep']:.3f}  drop={summ['avg_prob_drop']:.3f}"
    )
    print(
        f"fills scanned: {result['fill_rows']}  round-trip streaks: "
        f"{result['streak_count']} ({result['closed_streaks']} closed)"
    )

    print("\n" + "-" * 92)
    print("ATTRIBUTED ROUND-TRIPS - realised P&L of round-trips by what the model said at entry")
    print("-" * 92)
    print(f"{'bucket':<16}{'round_trips':>12}{'net_realised':>16}{'wins':>8}{'losses':>8}{'avg_prob':>10}")
    for label, b in (("would_KEEP", keep), ("would_DROP", drop)):
        print(
            f"{label:<16}{b['count']:>12}{b['net_realised']:>16,.2f}"
            f"{b['wins']:>8}{b['losses']:>8}{b['avg_probability']:>10.3f}"
        )
    if attribution["unattributed_streaks"]:
        print(
            f"\n  ({attribution['unattributed_streaks']} closed round-trips could not be "
            "matched to a shadow entry decision - pre-shadow or netted)"
        )

    print("\n" + "=" * 92)
    print(f"VERDICT: {verdict['verdict']}")
    print(f"  {verdict['reason']}")
    print("=" * 92)
    print("  RE-ADMIT          -> flip config/strategies.yaml::signal_engine.use_trained_meta_labeler: true")
    print("  DO_NOT_ADMIT      -> keep it shadow-only; the heuristic chain + edge gate remain the filter")
    print("  INSUFFICIENT_DATA -> let the shadow soak accrue more attributed round-trips\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Brain shadow scorecard: shadow meta-label vs live fills.")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--since-hours", type=float, default=None, help="only count rows newer than N hours")
    ap.add_argument("--min-sample", type=int, default=8, help="min attributed round-trips per side for a verdict")
    args = ap.parse_args()

    load_dotenv()
    result = await collect(args.since_hours)
    if result.get("_error") == "no_db":
        print("NO DB - check POSTGRES_* and that Docker is up")
        return 1

    if args.json:
        out = {
            "decision_summary": _decision_summary(result["decisions"]),
            "attribution": result["attribution"],
            "streak_count": result["streak_count"],
            "closed_streaks": result["closed_streaks"],
            "verdict": build_verdict(result["attribution"], min_sample=args.min_sample),
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(result, args.min_sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
