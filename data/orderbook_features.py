"""
data/orderbook_features.py
============================
Wave 10 — order-book feature aggregator.

Pure-function module computing a fixed feature block from a single
order-book snapshot:

    spread_bps             — (ask - bid) / mid * 10_000
    top_of_book_imbalance  — (bid_qty_top - ask_qty_top) / total_top
    depth_imbalance_k      — (Σ_k bid_qty - Σ_k ask_qty) / Σ_k total
    slope_k                — average of |Δprice| / Δqty across k levels
    liquidity_fragility    — std(per-level qty) / mean(qty); higher = thinner
    quote_staleness        — seconds since last update (caller supplies)
    vpin_proxy             — |bid_qty - ask_qty| / total_qty (Wave-3 style)

Defensive on degenerate input (empty book, zero qty, crossed book).

Asset-class allow-list lives in ``config/microstructure.yaml``; this
module just computes — caller decides whether to record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence


@dataclass(frozen=True)
class OrderbookLevel:
    """One bid or ask level. Decimal-typed to match broker adapters."""

    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class OrderbookSnapshot:
    symbol: str
    bids: tuple[OrderbookLevel, ...]   # descending by price
    asks: tuple[OrderbookLevel, ...]   # ascending by price
    timestamp: datetime
    asset_class: str = "crypto"
    metadata: dict = field(default_factory=dict)


# ── single-snapshot features ───────────────────────────────────────────────


def _f(d: Decimal) -> float:
    try:
        return float(d)
    except (TypeError, ValueError):
        return 0.0


def spread_bps(snap: OrderbookSnapshot) -> Optional[float]:
    if not snap.bids or not snap.asks:
        return None
    bid = _f(snap.bids[0].price)
    ask = _f(snap.asks[0].price)
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    mid = 0.5 * (bid + ask)
    return float((ask - bid) / mid * 10_000.0)


def top_of_book_imbalance(snap: OrderbookSnapshot) -> float:
    if not snap.bids or not snap.asks:
        return 0.0
    bq = _f(snap.bids[0].quantity)
    aq = _f(snap.asks[0].quantity)
    tot = bq + aq
    if tot <= 0:
        return 0.0
    return float((bq - aq) / tot)


def depth_imbalance(snap: OrderbookSnapshot, *, depth: int = 5) -> float:
    if not snap.bids or not snap.asks:
        return 0.0
    k = max(1, int(depth))
    bid_q = sum(_f(l.quantity) for l in snap.bids[:k])
    ask_q = sum(_f(l.quantity) for l in snap.asks[:k])
    tot = bid_q + ask_q
    if tot <= 0:
        return 0.0
    return float((bid_q - ask_q) / tot)


def book_slope(snap: OrderbookSnapshot, *, depth: int = 5) -> Optional[float]:
    """
    Mean ``|Δprice| / qty`` across the first ``depth`` levels per side
    — a thin book has a steep slope.
    """
    if len(snap.bids) < 2 or len(snap.asks) < 2:
        return None
    k = max(2, int(depth))

    def _side_slope(levels: Sequence[OrderbookLevel]) -> Optional[float]:
        used = list(levels[:k])
        if len(used) < 2:
            return None
        deltas: list[float] = []
        for i in range(1, len(used)):
            dp = abs(_f(used[i].price) - _f(used[i - 1].price))
            q = _f(used[i].quantity)
            if q <= 0:
                continue
            deltas.append(dp / q)
        return float(sum(deltas) / len(deltas)) if deltas else None

    b = _side_slope(snap.bids)
    a = _side_slope(snap.asks)
    parts = [v for v in (b, a) if v is not None]
    if not parts:
        return None
    return float(sum(parts) / len(parts))


def liquidity_fragility(snap: OrderbookSnapshot, *, depth: int = 5) -> Optional[float]:
    """``std(qty) / mean(qty)`` — high values flag a "wall" or a thin book."""
    qs = [_f(l.quantity) for l in (list(snap.bids[:depth]) + list(snap.asks[:depth])) if _f(l.quantity) > 0]
    if len(qs) < 3:
        return None
    n = len(qs)
    mean = sum(qs) / n
    var = sum((q - mean) ** 2 for q in qs) / max(1, n - 1)
    sd = math.sqrt(var)
    if mean <= 0:
        return None
    return float(sd / mean)


def vpin_proxy(snap: OrderbookSnapshot, *, depth: int = 5) -> float:
    """Single-snapshot VPIN-style toxicity: |bid-ask depth gap| / total."""
    bid_q = sum(_f(l.quantity) for l in snap.bids[:depth])
    ask_q = sum(_f(l.quantity) for l in snap.asks[:depth])
    tot = bid_q + ask_q
    if tot <= 0:
        return 0.0
    return float(abs(bid_q - ask_q) / tot)


def quote_staleness_seconds(snap: OrderbookSnapshot, *, now: Optional[datetime] = None) -> float:
    """Wall-clock age of the snapshot. Negative-clamped to 0."""
    ref = now or datetime.now(timezone.utc)
    age = (ref - snap.timestamp).total_seconds()
    return float(max(0.0, age))


def is_book_well_formed(snap: OrderbookSnapshot) -> bool:
    """
    Cheap sanity: at least one level per side, top-of-book uncrossed,
    all quantities non-negative.
    """
    if not snap.bids or not snap.asks:
        return False
    if _f(snap.asks[0].price) <= _f(snap.bids[0].price):
        return False
    for lvl in list(snap.bids) + list(snap.asks):
        if _f(lvl.quantity) < 0 or _f(lvl.price) <= 0:
            return False
    return True


# ── catch-all builder ──────────────────────────────────────────────────────


def build_orderbook_features(
    snap: OrderbookSnapshot,
    *,
    depth: int = 5,
    now: Optional[datetime] = None,
) -> dict[str, Optional[float]]:
    """All features in a flat dict — what the model trainer ingests."""
    if not is_book_well_formed(snap):
        return {
            "spread_bps": None,
            "top_of_book_imbalance": None,
            "depth_imbalance": None,
            "book_slope": None,
            "liquidity_fragility": None,
            "vpin_proxy": None,
            "quote_staleness": quote_staleness_seconds(snap, now=now),
            "well_formed": 0.0,
        }
    return {
        "spread_bps": spread_bps(snap),
        "top_of_book_imbalance": top_of_book_imbalance(snap),
        "depth_imbalance": depth_imbalance(snap, depth=depth),
        "book_slope": book_slope(snap, depth=depth),
        "liquidity_fragility": liquidity_fragility(snap, depth=depth),
        "vpin_proxy": vpin_proxy(snap, depth=depth),
        "quote_staleness": quote_staleness_seconds(snap, now=now),
        "well_formed": 1.0,
    }
