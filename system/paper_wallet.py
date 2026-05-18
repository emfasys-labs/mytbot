"""
system/paper_wallet.py
======================
Synthetic paper wallet for venues with **no exchange-native paper account**
(Kraken / Binance / Bybit). Before this, their ``get_balance()`` returned
empty, so crypto contributed **$0 to NAV** even though crypto fills were
simulated locally and their P&L *was* booked into ``daily_pnl`` — a
structural inconsistency (realised ledger ≠ NAV; crypto traded unbounded
"phantom" capital with $163k of unbacked open positions).

This module gives each such venue a real (paper) capital base:

    venue_equity = seed + realised_net(all-time) + unrealised(open)

Both P&L terms are derived from the **same authoritative ``OrderLog`` /
``PositionLog`` ledger** the rest of the system uses (identical FIFO /
average-cost replay as ``run_m3._compute_today_realised_pnl``), so the
synthetic equity is *consistent by construction* with ``daily_pnl`` — it
cannot double-count. ``seed + realised + unrealised`` mirrors a broker
NetLiquidation figure, so crypto NAV now behaves exactly like IBKR /
Alpaca NAV.

Flow (decoupled, restart-safe, zero adapter→DB coupling):
  * the orchestrator NAV heartbeat (it already holds a DB session, runs
    ~60s) computes each venue's equity and writes a tiny JSON snapshot;
  * the crypto adapters' ``get_balance()`` read that snapshot in paper
    mode and report it, so ``live_portfolio_snapshot`` folds crypto into
    NAV through the normal path — no NAV-path change needed.

Off switch: ``CRYPTO_PAPER_WALLET=0`` restores the exact prior behaviour
(adapters return empty, crypto contributes $0). Fully reversible.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from loguru import logger

# Keep in sync with core.broker_paper.NO_NATIVE_PAPER_POSITION_BROKERS.
CRYPTO_PAPER_BROKERS: frozenset[str] = frozenset({"kraken", "binance", "bybit"})

_WALLET_FILE = Path(
    os.getenv("CRYPTO_PAPER_WALLET_FILE", "data/runtime/paper_wallet.json")
)
_DEFAULT_SEED = "50000"


def crypto_paper_wallet_enabled() -> bool:
    return os.getenv("CRYPTO_PAPER_WALLET", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def seed_for(broker: str) -> Decimal:
    """Per-venue paper seed. ``PAPER_WALLET_<BROKER>_USD`` overrides the
    shared ``CRYPTO_PAPER_WALLET_USD`` default."""
    b = str(broker or "").strip().upper()
    raw = os.getenv(
        f"PAPER_WALLET_{b}_USD",
        os.getenv("CRYPTO_PAPER_WALLET_USD", _DEFAULT_SEED),
    )
    try:
        v = Decimal(str(raw))
        return v if v >= 0 else Decimal(_DEFAULT_SEED)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(_DEFAULT_SEED)


def _d(v: object) -> Decimal:
    try:
        return Decimal(str(v if v is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


# ── ledger-derived venue equity (single source of truth) ────────────────

async def compute_venue_equity(session: Any, broker: str) -> dict[str, str]:
    """Return ``{seed, realised, unrealised, equity}`` for one venue from
    the authoritative order/position ledger. Never raises (returns the
    seed alone on any error — degrade safe)."""
    b = str(broker or "").strip().lower()
    seed = seed_for(b)
    try:
        from sqlalchemy import and_, func, select
        from storage.models import OrderLog, PositionLog

        rows = list(
            (
                await session.execute(
                    select(OrderLog)
                    .where(
                        func.lower(OrderLog.broker) == b,
                        OrderLog.status.in_(("filled", "partially_filled")),
                    )
                    .order_by(OrderLog.timestamp.asc(), OrderLog.id.asc())
                )
            ).scalars().all()
        )
        # FIFO / average-cost replay → realised net of fees (same maths as
        # run_m3._compute_today_realised_pnl, scoped to this broker, all-time).
        state: dict[str, tuple[Decimal, Decimal]] = {}
        realised = Decimal("0")
        for r in rows:
            sym = str(r.symbol or "").strip().upper()
            pq, pa = state.get(sym, (Decimal("0"), Decimal("0")))
            try:
                q = _d(r.filled_quantity if r.filled_quantity is not None else r.quantity)
                px = _d(r.avg_fill_price if r.avg_fill_price is not None else r.limit_price)
                fee = _d(r.fee)
            except Exception:  # noqa: BLE001
                continue
            sd = str(r.side or "").strip().lower()
            if sd not in ("buy", "sell") or q <= 0 or px <= 0:
                continue
            sf = q if sd == "buy" else -q
            cq = Decimal("0")
            g = Decimal("0")
            if pq > 0 and sf < 0:
                cq = min(pq, abs(sf))
                g = (px - pa) * cq
            elif pq < 0 and sf > 0:
                cq = min(abs(pq), sf)
                g = (pa - px) * cq
            if cq > 0:
                realised += g - (fee * (cq / q) if q > 0 else Decimal("0"))
            nq = pq + sf
            if abs(nq) <= Decimal("1e-9"):
                nq, na = Decimal("0"), px
            elif pq == 0 or (pq > 0 and sf > 0) or (pq < 0 and sf < 0):
                ta = abs(pq) + abs(sf)
                na = ((abs(pq) * pa) + (abs(sf) * px)) / ta if ta > 0 else px
            elif abs(sf) < abs(pq):
                na = pa
            else:
                na = px
            state[sym] = (nq, na)

        # Unrealised = sum of latest-snapshot unrealised for this venue's
        # still-open positions (mirror of how NetLiq embeds open MTM).
        latest = (
            select(
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("mx"),
            )
            .where(func.lower(PositionLog.broker) == b)
            .group_by(PositionLog.symbol)
            .subquery()
        )
        prows = list(
            (
                await session.execute(
                    select(PositionLog).join(
                        latest,
                        and_(
                            PositionLog.symbol == latest.c.symbol,
                            PositionLog.timestamp == latest.c.mx,
                        ),
                    ).where(func.lower(PositionLog.broker) == b)
                )
            ).scalars().all()
        )
        unreal = Decimal("0")
        for p in prows:
            if _d(p.quantity) == 0:
                continue
            unreal += _d(p.unrealised_pnl)

        equity = seed + realised + unreal
        return {
            "seed": str(seed),
            "realised": str(realised),
            "unrealised": str(unreal),
            "equity": str(equity if equity > 0 else Decimal("0")),
        }
    except Exception as exc:  # noqa: BLE001 — degrade to seed, never break NAV
        logger.debug("paper_wallet | compute_venue_equity({}) failed: {}", b, exc)
        return {
            "seed": str(seed),
            "realised": "0",
            "unrealised": "0",
            "equity": str(seed),
        }


# ── file-backed snapshot (decouples adapter from the DB) ────────────────

def write_snapshot(by_broker: dict[str, dict[str, str]]) -> None:
    try:
        _WALLET_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "venues": by_broker,
        }
        fd, tmp = tempfile.mkstemp(
            prefix=".paper_wallet_", suffix=".json", dir=str(_WALLET_FILE.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, _WALLET_FILE)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 — snapshot is best-effort
        logger.debug("paper_wallet | snapshot write failed: {}", exc)


def _read_snapshot() -> dict[str, Any]:
    try:
        if not _WALLET_FILE.exists():
            return {}
        data = json.loads(_WALLET_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def venue_equity(broker: str) -> Decimal | None:
    """Latest persisted equity for a crypto venue, or the seed if no
    snapshot exists yet (so NAV is sane from the very first tick).
    ``None`` only when the wallet feature is disabled."""
    if not crypto_paper_wallet_enabled():
        return None
    b = str(broker or "").strip().lower()
    if b not in CRYPTO_PAPER_BROKERS:
        return None
    snap = _read_snapshot()
    venues = snap.get("venues") if isinstance(snap, dict) else None
    if isinstance(venues, dict) and b in venues:
        try:
            return Decimal(str((venues[b] or {}).get("equity", seed_for(b))))
        except (InvalidOperation, TypeError, ValueError):
            return seed_for(b)
    return seed_for(b)
