from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from data.symbol_mapper import canonical_symbol, to_venue_symbol
from execution.orderbook_analyzer import OrderBookAnalyzer
from signals.microstructure.imbalance_detector import ImbalanceDetector
from signals.microstructure.liquidity_tracker import LiquidityTracker
from strategies.arbitrage.models import FundingRateSnapshot


def _d(v: object) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return Decimal(str(v))


def _parse_ob_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


class FundingRateDataProvider:
    """
    Normalises funding + top-of-book for (spot_venue, perp_venue) pairs.
    Uses optional adapter method ``fetch_funding_market_snapshot`` on the perp broker
    (e.g. Bybit linear) and ``get_order_book`` on the spot broker — no changes to ``BrokerAdapter`` ABC.
    """

    def __init__(
        self,
        broker_getter: Callable[[str], Awaitable[Any | None]],
        logger: Any | None = None,
        *,
        max_stale_seconds: float = 15.0,
        liquidity_tracker: LiquidityTracker | None = None,
    ) -> None:
        """
        ``broker_getter(name)`` must return a connected adapter or None.
        """
        self._get_broker = broker_getter
        self._logger = logger
        self._max_stale = max(1.0, float(max_stale_seconds))
        self._liquidity_tracker = liquidity_tracker or LiquidityTracker()

    async def get_snapshot(
        self,
        symbol: str,
        perp_venue: str,
        spot_venue: str,
    ) -> Optional[FundingRateSnapshot]:
        perp = await self._get_broker(perp_venue.strip().lower())
        spot = await self._get_broker(spot_venue.strip().lower())
        if perp is None or spot is None:
            return None

        canon = canonical_symbol(symbol)
        perp_sym = to_venue_symbol(perp_venue, canon)
        spot_sym = to_venue_symbol(spot_venue, canon)

        fn = getattr(perp, "fetch_funding_market_snapshot", None)
        if not callable(fn):
            if self._logger:
                self._logger.debug(
                    "funding_rates | perp %s has no fetch_funding_market_snapshot",
                    perp_venue,
                )
            return None

        try:
            snap = await fn(perp_sym)
        except Exception as exc:  # noqa: BLE001
            if self._logger:
                self._logger.warning("funding_rates | perp snapshot failed | %s | %s", perp_venue, exc)
            return None
        if not isinstance(snap, dict):
            return None

        funding_rate = _d(snap.get("funding_rate"))
        interval_h = int(snap.get("funding_interval_hours") or 8)
        nft = snap.get("next_funding_time")
        if isinstance(nft, datetime):
            next_funding = nft if nft.tzinfo else nft.replace(tzinfo=timezone.utc)
        else:
            next_funding = datetime.now(timezone.utc)

        perp_mark = _d(snap.get("mark_price"))
        perp_bid = _d(snap.get("bid"))
        perp_ask = _d(snap.get("ask"))
        if perp_bid <= 0 and perp_ask > 0:
            perp_bid = perp_ask
        if perp_ask <= 0 and perp_bid > 0:
            perp_ask = perp_bid

        try:
            ob = await spot.get_order_book(spot_sym, depth=5)
        except Exception as exc:  # noqa: BLE001
            if self._logger:
                self._logger.warning("funding_rates | spot book failed | %s | %s", spot_venue, exc)
            return None

        if not ob.bids or not ob.asks:
            return None

        spot_bid = ob.bids[0][0]
        spot_ask = ob.asks[0][0]
        if spot_bid <= 0 or spot_ask <= 0:
            return None

        spot_mid = (spot_bid + spot_ask) / Decimal("2")
        bid_lv, ask_lv = OrderBookAnalyzer.from_snapshot(ob)
        imb = ImbalanceDetector.compute_imbalance(bid_lv, ask_lv, depth=5)
        unstable = self._liquidity_tracker.detect_disappearing_liquidity(
            f"{spot_venue}:{canon}",
            bid_lv,
            ask_lv,
            depth=5,
        )
        ts = _parse_ob_ts(ob.timestamp)
        ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if (now - ts_aware).total_seconds() > self._max_stale:
            return None

        sym = str(snap.get("symbol") or canon).strip().upper()

        return FundingRateSnapshot(
            symbol=sym,
            perp_venue=perp_venue.strip().lower(),
            spot_venue=spot_venue.strip().lower(),
            funding_rate=funding_rate,
            funding_interval_hours=max(1, interval_h),
            next_funding_time=next_funding,
            perp_bid=perp_bid,
            perp_ask=perp_ask,
            perp_mark=perp_mark if perp_mark > 0 else (perp_bid + perp_ask) / Decimal("2"),
            spot_bid=spot_bid,
            spot_ask=spot_ask,
            spot_mid=spot_mid,
            timestamp=ts,
            spot_imbalance=imb,
            liquidity_unstable=unstable,
        )

    async def scan_symbol(
        self,
        symbol: str,
        perp_venues: Iterable[str],
        spot_venues: Iterable[str],
    ) -> list[FundingRateSnapshot]:
        out: list[FundingRateSnapshot] = []
        for pv in perp_venues:
            for sv in spot_venues:
                if pv.strip().lower() == sv.strip().lower():
                    continue
                s = await self.get_snapshot(symbol, pv, sv)
                if s is not None:
                    out.append(s)
        return out

    async def get_spot_quote(self, symbol: str, spot_venue: str) -> Optional[Dict[str, Decimal]]:
        """Top-of-book quote for cross-exchange spot strategies."""
        spot = await self._get_broker(spot_venue.strip().lower())
        if spot is None:
            return None
        spot_sym = to_venue_symbol(spot_venue, canonical_symbol(symbol))
        try:
            ob = await spot.get_order_book(spot_sym, depth=3)
        except Exception:  # noqa: BLE001
            return None
        if not ob.bids or not ob.asks:
            return None
        return {"bid": ob.bids[0][0], "ask": ob.asks[0][0]}
