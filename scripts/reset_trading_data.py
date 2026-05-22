"""
scripts/reset_trading_data.py
==============================
D126 — wipe corrupted trading history and reset to a clean slate.

The 2026-05-21 audit found the `orders` table corrupted by a
snapshot-resurrection race (phantom oversells — e.g. 79,910 BALL shares
sold against 10,586 ever bought). Per-symbol / per-broker P&L is
unrecoverable from that history. This script wipes the trading-derived
tables so the system restarts from 100%-accurate data, with the new
`fills` ledger (D126) as the authoritative record going forward.

SAFETY
------
* Dry-run by default. Nothing is deleted without ``--execute``.
* Refuses to run when ``APP_ENV=live``.
* Stop the trading system (`/system/stop` or kill `run.py`) BEFORE
  running with ``--execute`` — wiping tables under a live writer is
  unsafe.

WHAT IT WIPES
-------------
Tables (TRUNCATE):
  orders, positions, fills, daily_pnl, signals, risk_decisions,
  strategy_candidate_log, thesis_log, anomaly_log
control_state: every key EXCEPT the operator/config whitelist
  (paper.nav_seed, system.capital_allocation, strategy.enabled.*,
   auto_training.last_run_at).
Runtime files: data/runtime/risk_state.json (loss/cooldown counters).

WHAT IT KEEPS
-------------
feature_snapshots, price_history, instrument_registry + sources,
model_* tables, news_headlines, macro_observations, parameter_log,
feature_contracts, training_datasets, ai_outputs, control_commands,
config files, model artefacts.

NAV CAVEAT (D126.1)
-------------------
This wipe clears mytbot's DB ledger only. NAV is computed LIVE as the
sum of the connected brokers' paper-account balances — IBKR's TWS
paper account, Alpaca's paper account, and the crypto paper wallets.
The wipe cannot reset those broker-side balances, so after restart NAV
reflects the real broker totals, NOT ``paper.nav_seed``. ``nav_seed``
is only a pre-broker-connect fallback. For a coherent return baseline,
re-anchor ``paper.nav_seed`` to the real broker total after the
brokers connect (or reset the broker paper accounts at their source).

Usage:
    python scripts/reset_trading_data.py              # dry-run
    python scripts/reset_trading_data.py --execute    # perform the wipe
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from sqlalchemy import text  # noqa: E402

from storage.db import dispose_engine, init_async_database  # noqa: E402


# Tables truncated wholesale.
WIPE_TABLES = [
    "orders",
    "positions",
    "fills",
    "daily_pnl",
    "signals",
    "risk_decisions",
    "strategy_candidate_log",
    "thesis_log",
    "anomaly_log",
]

# control_state keys preserved across the reset (operator / config).
# Everything else in control_state is trading-derived telemetry and is
# deleted. `strategy.enabled.` is a prefix match.
KEEP_STATE_EXACT = {
    "paper.nav_seed",
    "system.capital_allocation",
    "auto_training.last_run_at",
}
KEEP_STATE_PREFIXES = ("strategy.enabled.",)

# Runtime files reset (loss / cooldown counters).
RUNTIME_FILES = [
    ROOT / "data" / "runtime" / "risk_state.json",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="D126 — reset corrupted trading data")
    p.add_argument(
        "--execute",
        action="store_true",
        help="actually perform the wipe (default: dry-run, no changes)",
    )
    return p.parse_args()


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


def _keep_state_key(key: str) -> bool:
    if key in KEEP_STATE_EXACT:
        return True
    return any(key.startswith(pfx) for pfx in KEEP_STATE_PREFIXES)


async def _main() -> int:
    args = _parse_args()
    _load_env()

    app_env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    if app_env == "live":
        print("REFUSING: APP_ENV=live. This script is paper-only.")
        return 2

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== D126 trading-data reset [{mode}] ===\n")

    engine, sf = await init_async_database()
    if sf is None:
        print("ERROR: database unavailable; check POSTGRES_* in .env")
        return 2
    try:
        async with sf() as session:
            # Snapshot current row counts.
            print("Tables to wipe (TRUNCATE):")
            for tbl in WIPE_TABLES:
                try:
                    r = await session.execute(text(f"SELECT COUNT(*) FROM {tbl}"))
                    print(f"  {tbl:<28} {r.scalar():>10,} rows")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {tbl:<28} (missing: {exc})")

            # control_state classification.
            r = await session.execute(text("SELECT key FROM control_state ORDER BY key"))
            keys = [row[0] for row in r]
            keep = [k for k in keys if _keep_state_key(k)]
            drop = [k for k in keys if not _keep_state_key(k)]
            print(f"\ncontrol_state: {len(keys)} keys total")
            print(f"  KEEP ({len(keep)}): {', '.join(keep) or '(none)'}")
            print(f"  DROP ({len(drop)}): {', '.join(drop) or '(none)'}")

            # NAV seed → only a pre-broker-connect fallback. Real NAV is
            # broker-derived (see the NAV CAVEAT in the module docstring).
            r = await session.execute(
                text("SELECT value FROM control_state WHERE key='paper.nav_seed'")
            )
            seed = r.scalar()
            seed_val = (seed or {}).get("seed") if isinstance(seed, dict) else None
            print(f"\npaper.nav_seed (pre-connect fallback only) = {seed_val}")
            print("  NOTE: real post-restart NAV is the sum of broker paper")
            print("  balances — re-anchor nav_seed to that total once brokers connect.")

            print("\nRuntime files to delete:")
            for f in RUNTIME_FILES:
                print(f"  {f}  ({'exists' if f.is_file() else 'absent'})")

            if not args.execute:
                print("\n[DRY-RUN] No changes made. Re-run with --execute to perform the wipe.")
                print("IMPORTANT: stop the trading system before executing.")
                return 0

            # ── EXECUTE ──────────────────────────────────────────────────────
            print("\nExecuting wipe...")
            for tbl in WIPE_TABLES:
                try:
                    await session.execute(
                        text(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
                    )
                    print(f"  truncated {tbl}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  SKIP {tbl}: {exc}")
            if drop:
                await session.execute(
                    text("DELETE FROM control_state WHERE key = ANY(:keys)"),
                    {"keys": drop},
                )
                print(f"  deleted {len(drop)} control_state key(s)")
            await session.commit()
            print("  committed.")
    finally:
        await dispose_engine(engine)

    if args.execute:
        for f in RUNTIME_FILES:
            try:
                if f.is_file():
                    f.unlink()
                    print(f"  removed runtime file {f}")
            except Exception as exc:  # noqa: BLE001
                print(f"  could not remove {f}: {exc}")
        print("\n=== reset complete — restart `python run.py` for a clean slate ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
