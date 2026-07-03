"""
scripts/flatten_all_positions.py
=================================
D231 follow-up — emergency flatten of EVERY open position, across every
connected broker, in one shot.

The 2026-07-03 loss-attribution review left a 210-position, ~$1.03M gross
book behind (109 Alpaca, 99 IBKR, 1 Kraken, 1 Capital.com). Existing one-shot
flatten tools only cover a single broker each
(``flatten_ibkr_paper.py`` = IBKR only) or never touch a broker at all
(``flatten_local_paper_book.py`` / ``flatten_orphaned_remnants.py`` write
local ledger tombstones only). Neither is safe to use alone for a full
multi-broker reset.

WHY THIS GOES THROUGH RiskEngine + ExecutionEngine, NOT ``adapter.place_order()``
----------------------------------------------------------------------------
``flatten_ibkr_paper.py`` calls the IBKR adapter directly because IBKR has a
genuinely separate paper port (7497 vs 7496) — a direct call is safe there.
That is NOT true for every broker in this book:

  * Kraken (and Binance/Bybit) have NO native paper trading. Their adapters
    self-reject ``place_order()`` in paper mode
    (see ``brokers/kraken/adapter.py`` — ``paper_mode_no_native_order``).
    Paper fills for these venues are SIMULATED by
    ``execution/engine.py::ExecutionEngine._simulate_fill`` — a direct
    adapter call would silently do nothing.
  * IBKR / Alpaca / Capital.com DO have genuine separate paper/demo
    endpoints, so a real close order to their PAPER account is correct
    and desired.

``ExecutionEngine`` already implements this per-broker paper/native
distinction correctly (the same code path ``stop_loss_monitor``/
``capital_recycle`` use live). This script reuses it exactly — one code
path, uniformly correct for every broker — rather than re-deriving
per-broker paper semantics in a one-off script.

SAFETY
------
* Dry-run by default. Nothing is submitted without ``--apply``.
* Refuses to run when ``APP_ENV=live``.
* Every close is ``reduce_only``/``close_only`` — this can only shrink
  exposure, never open a new position.
* Positions are read from the ``fills`` ledger (``SUM(signed_quantity)``
  per broker+symbol) — the D126 authoritative source — not from live broker
  balance queries (which for crypto venues would reflect the REAL account,
  not the paper-simulated position).
* Cancels open/pending orders per broker first (best-effort) so a stale
  order can't fight the close.

Usage:
    python scripts/flatten_all_positions.py              # dry-run
    python scripts/flatten_all_positions.py --apply
    python scripts/flatten_all_positions.py --apply --brokers ibkr,alpaca
    python scripts/flatten_all_positions.py --apply --symbols AAPL,MARA
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402


def _refuse_if_live() -> None:
    env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    if env == "live":
        logger.error("APP_ENV=live — refusing to run. This script is paper-only.")
        sys.exit(2)


async def _open_positions(session_factory) -> list[dict]:
    """(broker, symbol, asset_class, signed_qty, last_fill_price) for every
    (broker, symbol) with a non-zero net position, per the fills ledger."""
    from sqlalchemy import func, select

    from storage.models import FillLog

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    FillLog.broker,
                    FillLog.symbol,
                    func.sum(FillLog.signed_quantity).label("qty"),
                )
                .group_by(FillLog.broker, FillLog.symbol)
                .having(func.sum(FillLog.signed_quantity) != 0)
            )
        ).all()
        out: list[dict] = []
        for broker, symbol, qty in rows:
            last_q = await session.execute(
                select(FillLog.asset_class, FillLog.fill_price)
                .where(FillLog.broker == broker, FillLog.symbol == symbol)
                .order_by(FillLog.timestamp.desc(), FillLog.id.desc())
                .limit(1)
            )
            last = last_q.first()
            asset_class = str(last[0] or "equity") if last else "equity"
            last_price = Decimal(str(last[1])) if last and last[1] is not None else Decimal("0")
            out.append(
                {
                    "broker": str(broker),
                    "symbol": str(symbol),
                    "asset_class": asset_class,
                    "qty": Decimal(str(qty)),
                    "fallback_price": last_price,
                }
            )
        return out


async def _latest_position_prices(session_factory) -> dict[tuple[str, str], Decimal]:
    """{(broker, symbol): current_price} from the latest PositionLog snapshot."""
    from sqlalchemy import func, select

    from storage.models import PositionLog

    async with session_factory() as session:
        latest_by_key = (
            select(
                PositionLog.broker.label("broker"),
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("max_ts"),
            )
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        rows = (
            await session.execute(
                select(PositionLog.broker, PositionLog.symbol, PositionLog.current_price).join(
                    latest_by_key,
                    (PositionLog.broker == latest_by_key.c.broker)
                    & (PositionLog.symbol == latest_by_key.c.symbol)
                    & (PositionLog.timestamp == latest_by_key.c.max_ts),
                )
            )
        ).all()
        return {
            (str(b).strip().lower(), str(s).strip().upper()): Decimal(str(p))
            for b, s, p in rows
            if p is not None and Decimal(str(p)) > 0
        }


async def _cancel_open_orders(broker_manager) -> int:
    cancelled = 0
    for name, adapter in list(broker_manager.adapters.items()):
        try:
            opens = await adapter.get_open_orders()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cancel-open | {} | get_open_orders failed: {}", name, exc)
            continue
        for o in opens:
            bid = getattr(o, "broker_order_id", None) or getattr(o, "id", None)
            if not bid:
                continue
            try:
                await adapter.cancel_order(bid)
                cancelled += 1
                logger.info("cancelled open order | {} | {} {}", name, bid, getattr(o, "symbol", "?"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("cancel-open | {} | cancel {} failed: {}", name, bid, exc)
    return cancelled


async def _amain(args: argparse.Namespace) -> int:
    _refuse_if_live()
    load_dotenv(ROOT / ".env")

    # python run.py is expected to still be running (this script never stops
    # it). system/broker_manager.py reads IBKR_CLIENT_ID directly from env
    # for its own IBKR connection, which would collide with the already-live
    # process's socket. Bump it, same convention as flatten_ibkr_paper.py.
    try:
        base_client_id = int(os.getenv("IBKR_CLIENT_ID", "1") or "1")
    except ValueError:
        base_client_id = 1
    os.environ["IBKR_CLIENT_ID"] = str(base_client_id + 999)

    # EXECUTION_PAPER_USE_BROKER_ORDERS is unset in this deployment, so every
    # fill (all brokers, not just the no-native-paper crypto venues) is a
    # LOCAL SIMULATION recorded straight into the fills ledger — confirmed
    # empirically (a direct Alpaca paper-API query returns zero positions
    # despite 109 "open" rows in the local ledger). There is no real broker
    # exposure for execution/engine.py's market-session gate to protect here;
    # that gate exists to stop the simulator fabricating an unrealistic fill
    # during ORGANIC trading (e.g. filling a fresh signal at a stale
    # Friday-close price over a holiday weekend). A deliberate, one-time
    # administrative flatten-for-reset is not organic trading — closing out
    # the simulated book at its last known price right now is safe and
    # intentional. Disable the gate for this process only.
    os.environ["MARKET_SESSION_GATE"] = "0"

    from storage.db import dispose_engine, init_async_database
    from system.broker_manager import BrokerManager
    from system.portfolio_equity import live_portfolio_value
    from system.trading_loop.helpers import load_yaml
    from execution.engine import ExecutionEngine
    from risk.engine import RiskEngine, RiskVerdict
    from risk.engine import Signal as RiskSignal
    from run_m3 import _load_portfolio_state

    engine, sf = await init_async_database()
    if sf is None:
        logger.error("database unavailable; check POSTGRES_* in .env")
        return 2

    broker_manager = BrokerManager(paper_mode=True)
    try:
        await broker_manager.discover_and_connect()
        connected = sorted(broker_manager.adapters.keys())
        logger.info("connected brokers: {}", ", ".join(connected) or "(none)")

        positions = await _open_positions(sf)
        wanted_brokers = (
            {b.strip().lower() for b in args.brokers.split(",") if b.strip()}
            if args.brokers
            else None
        )
        wanted_symbols = (
            {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
            if args.symbols
            else None
        )
        positions = [
            p
            for p in positions
            if (not wanted_brokers or p["broker"].strip().lower() in wanted_brokers)
            and (not wanted_symbols or p["symbol"].strip().upper() in wanted_symbols)
        ]

        if not positions:
            logger.info("no open positions matching the filter — nothing to do")
            return 0

        live_prices = await _latest_position_prices(sf)

        logger.info(
            "found {} open position(s) across {} broker(s)",
            len(positions),
            len({p["broker"] for p in positions}),
        )

        if not args.apply:
            print(f"\n{'broker':<12}{'symbol':<12}{'asset_class':<12}{'qty':>14}{'price':>14}")
            print("-" * 66)
            for p in sorted(positions, key=lambda p: (p["broker"], p["symbol"])):
                px = live_prices.get(
                    (p["broker"].strip().lower(), p["symbol"].strip().upper())
                ) or p["fallback_price"]
                print(f"{p['broker']:<12}{p['symbol']:<12}{p['asset_class']:<12}{p['qty']:>14}{px:>14}")
            print(f"\n[DRY-RUN] {len(positions)} position(s) would be flattened. Re-run with --apply.")
            return 0

        if args.cancel_open:
            n = await _cancel_open_orders(broker_manager)
            logger.info("cancelled {} open order(s) before flattening", n)

        nav = await live_portfolio_value(broker_manager)
        if nav <= 0:
            logger.error("NAV<=0 (incomplete broker coverage?) — refusing to evaluate risk on a bad NAV")
            return 2

        risk_cfg = load_yaml("config/risk_limits.yaml")
        risk_engine = RiskEngine(risk_cfg)
        execution_engine = ExecutionEngine(
            broker_configs={},
            paper_mode=True,
            allowed_brokers=connected,
            broker_manager=broker_manager,
        )

        portfolio_state = await _load_portfolio_state(
            sf,
            fallback_portfolio_value=nav,
            signal_price_fallback=Decimal("0"),
            capital_pct=Decimal(str(os.getenv("CAPITAL_PCT", "1.0"))),
        )
        risk_engine.update_high_watermark(
            Decimal(str(portfolio_state.get("high_watermark_value", nav)))
        )
        risk_engine.restore_runtime_state(portfolio_state)

        closed, rejected, failed = 0, 0, 0
        for p in sorted(positions, key=lambda p: (p["broker"], p["symbol"])):
            bname = p["broker"].strip().lower()
            sym = p["symbol"].strip().upper()
            qty = p["qty"]
            side = "sell" if qty > 0 else "buy"
            price = live_prices.get((bname, sym)) or p["fallback_price"]
            if price <= 0:
                logger.warning("skip {} {} | no usable price", bname, sym)
                failed += 1
                continue

            signal = RiskSignal(
                signal_id=f"flatten-all-{uuid.uuid4().hex[:12]}",
                symbol=sym,
                side=side,
                strategy="manual_flatten_all",
                confidence=1.0,
                suggested_quantity=abs(qty),
                suggested_price=price,
                broker=bname,
                asset_class=p["asset_class"],
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "reduce_only": True,
                    "close_only": True,
                    "flatten_all": True,
                    "flatten_reason": "manual_reset",
                },
            )

            risk_decision = await risk_engine.evaluate_and_persist(sf, signal, portfolio_state)
            if risk_decision.verdict != RiskVerdict.APPROVED:
                logger.warning(
                    "REJECTED | {} {} side={} qty={} | {}",
                    bname, sym, side, abs(qty), risk_decision.reason,
                )
                rejected += 1
                continue

            result = await execution_engine.execute(signal, risk_decision, session_factory=sf)
            if result is None:
                logger.warning("NOT EXECUTED | {} {} side={} qty={}", bname, sym, side, abs(qty))
                failed += 1
                continue

            logger.info(
                "CLOSED | {} {} side={} qty={} | status={}",
                bname, sym, side, abs(qty), getattr(result, "status", "?"),
            )
            closed += 1

        logger.info(
            "flatten complete | closed={} rejected={} failed={} total={}",
            closed, rejected, failed, len(positions),
        )
        return 0
    finally:
        await broker_manager.disconnect_all()
        await dispose_engine(engine)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Flatten every open paper position across every connected broker")
    p.add_argument("--apply", action="store_true", help="actually submit closes (default: dry-run)")
    p.add_argument("--no-cancel-open", dest="cancel_open", action="store_false",
                   help="skip cancelling open orders before closing positions")
    p.add_argument("--brokers", type=str, default="", help="comma-separated broker filter (default: all)")
    p.add_argument("--symbols", type=str, default="", help="comma-separated symbol filter (default: all)")
    p.set_defaults(cancel_open=True)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(_amain(args)))
