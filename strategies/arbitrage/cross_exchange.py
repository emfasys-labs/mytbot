from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from data.capability_registry import CapabilityRegistry
from signals.engine import RawSignal
from strategies.arbitrage.models import CrossExchangeOpportunity
from strategies.arbitrage.spread_calculator import compute_net_spread, compute_spread_bps


def cross_exchange_signal_to_raw(d: dict) -> RawSignal:
    md = dict(d.get("metadata") or {})
    md.setdefault("buy_venue", d.get("buy_venue", ""))
    md.setdefault("sell_venue", d.get("sell_venue", ""))
    return RawSignal(
        strategy=str(d.get("strategy", "cross_exchange_arbitrage")),
        symbol=str(d["symbol"]),
        side=str(d["side"]),
        confidence=float(d.get("confidence", 0.75)),
        broker=str(d.get("buy_venue", "unknown")),
        asset_class=str(d.get("asset_class", "crypto")),
        metadata=md,
    )


class CrossExchangeArbitrageStrategy:
    STRATEGY_NAME = "cross_exchange_arbitrage"

    def __init__(
        self,
        capability_registry: CapabilityRegistry,
        data_provider: Any,
        config: dict,
        logger: Any | None = None,
    ) -> None:
        self._registry = capability_registry
        self._data = data_provider
        self._config = config
        self._logger = logger

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    async def evaluate_symbol(self, symbol: str, notional: Decimal) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None

        min_liq = Decimal(str(self._config.get("min_liquidity_score", "0")))
        max_lat = int(self._config.get("max_latency_ms", 10_000))
        spot_brokers = CapabilityRegistry.filter_by_liquidity(
            self._registry.get_spot_brokers(symbol),
            min_liq,
        )
        spot_brokers = CapabilityRegistry.filter_by_latency(spot_brokers, max_latency_ms=max_lat)

        if len(spot_brokers) < 2:
            return None

        best: CrossExchangeOpportunity | None = None
        min_spread_bps = Decimal(str(self._config["min_spread_bps"]))
        fee_bps = Decimal(str(self._config["fee_buffer_bps"]))
        slip_bps = Decimal(str(self._config["slippage_buffer_bps"]))

        for buy in spot_brokers:
            for sell in spot_brokers:
                if buy.name == sell.name:
                    continue
                quote_buy = await self._data.get_spot_quote(symbol, buy.name)
                quote_sell = await self._data.get_spot_quote(symbol, sell.name)
                if not quote_buy or not quote_sell:
                    continue

                ask = quote_buy["ask"]
                bid = quote_sell["bid"]
                spread_bps = compute_spread_bps(bid, ask)
                if spread_bps <= min_spread_bps:
                    continue

                net = compute_net_spread(notional, bid, ask, fee_bps=fee_bps, slippage_bps=slip_bps)
                if net <= 0:
                    continue

                gross_spread = notional * spread_bps / Decimal("10000")
                opp = CrossExchangeOpportunity(
                    symbol=symbol.strip().upper(),
                    buy_venue=buy.name,
                    sell_venue=sell.name,
                    buy_price=ask,
                    sell_price=bid,
                    spread_bps=spread_bps,
                    gross_spread=gross_spread,
                    estimated_fees=Decimal("0"),
                    estimated_slippage=Decimal("0"),
                    net_spread=net,
                    notional=notional,
                    confidence=Decimal("0.75"),
                )
                if best is None or net > best.net_spread:
                    best = opp

        if best is None:
            return None
        return self._to_signal_dict(best)

    def _to_signal_dict(self, opp: CrossExchangeOpportunity) -> dict[str, Any]:
        return {
            "strategy": self.STRATEGY_NAME,
            "symbol": opp.symbol,
            "side": "ARBITRAGE_SPOT_SPREAD",
            "confidence": float(opp.confidence),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "buy_venue": opp.buy_venue,
            "sell_venue": opp.sell_venue,
            "metadata": {
                "spread_bps": str(opp.spread_bps),
                "net_spread": str(opp.net_spread),
                "buy_price": str(opp.buy_price),
                "sell_price": str(opp.sell_price),
                "notional": str(opp.notional),
                "buy_venue": opp.buy_venue,
                "sell_venue": opp.sell_venue,
                "arbitrage_kind": "cross_exchange_spot",
            },
        }
