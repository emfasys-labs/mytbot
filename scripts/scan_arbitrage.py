"""
Read-only arbitrage scanner: funding carry + cross-venue spot spread.

Usage (from repo root):
  python scripts/scan_arbitrage.py

Requires public market data (Bybit linear + spot order books on configured venues).
Does not enable strategy flags in YAML; logs ranked opportunities only.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from loguru import logger

from brokers.registry import get_broker
from data.capability_registry import CapabilityRegistry
from data.funding_rates import FundingRateDataProvider
from execution.venue_selector import VenueSelector
from strategies.arbitrage.cross_exchange import CrossExchangeArbitrageStrategy
from strategies.arbitrage.funding_rate import FundingRateArbitrageStrategy


async def _main() -> None:
    strat_path = ROOT / "config" / "strategies.yaml"
    with strat_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cap_cfg = cfg.get("arbitrage_capabilities") or {}
    registry = CapabilityRegistry(logger=logger)
    registry.load_from_config(cap_cfg)

    fcfg = dict(cfg.get("funding_rate_arbitrage") or {})
    ccfg = dict(cfg.get("cross_exchange_arbitrage") or {})
    fcfg["enabled"] = True
    ccfg["enabled"] = True

    brokers: dict = {}

    async def broker_getter(name: str):
        n = name.strip().lower()
        if n in brokers:
            return brokers[n]
        bc = _broker_configs()
        kwargs = dict(bc.get(n, {}))
        if n == "bybit":
            kwargs.setdefault("category", "linear")
            kwargs.setdefault("paper_mode", True)
        b = get_broker(n, paper_mode=True, **kwargs)
        try:
            if await b.connect():
                brokers[n] = b
                return b
        except Exception as exc:  # noqa: BLE001
            logger.warning("connect failed | {} | {}", n, exc)
        return None

    provider = FundingRateDataProvider(broker_getter, logger=logger)
    venue_selector = VenueSelector(
        registry,
        provider,
        logger,
        fcfg,
    )
    funding = FundingRateArbitrageStrategy(fcfg, venue_selector, logger=logger)
    cross = CrossExchangeArbitrageStrategy(registry, provider, ccfg, logger=logger)

    notional = Decimal(str(fcfg.get("min_liquidity_notional", "5000")))
    symbols = list(fcfg.get("symbols") or ["BTCUSDT"])

    for sym in symbols:
        sig = await funding.evaluate_symbol(sym, notional)
        if sig:
            logger.info(
                "FUNDING_ARB | {} | spot={} perp={} | net_y={} | basis_bps={}",
                sym,
                sig.spot_venue,
                sig.perp_venue,
                sig.annualised_net_yield,
                sig.basis_bps,
            )
        else:
            logger.info("FUNDING_ARB | {} | no opportunity", sym)

        cex = await cross.evaluate_symbol(sym, notional)
        if cex:
            logger.info(
                "CROSS_SPOT | {} | buy={} sell={} | meta={}",
                sym,
                cex.get("buy_venue"),
                cex.get("sell_venue"),
                cex.get("metadata"),
            )
        else:
            logger.info("CROSS_SPOT | {} | no opportunity", sym)

    for b in brokers.values():
        try:
            await b.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _broker_configs() -> dict:
    import os

    return {
        "ibkr": {
            "host": os.getenv("IBKR_HOST", "127.0.0.1"),
            "port": int(os.getenv("IBKR_PORT", "7497")),
            "client_id": int(os.getenv("IBKR_CLIENT_ID", "1")),
        },
        "kraken": {
            "api_key": os.getenv("KRAKEN_API_KEY", "").strip(),
            "api_secret": os.getenv("KRAKEN_API_SECRET", "").strip(),
        },
        "binance": {
            "api_key": os.getenv("BINANCE_API_KEY", "").strip(),
            "api_secret": os.getenv("BINANCE_API_SECRET", "").strip(),
        },
        "bybit": {
            "api_key": os.getenv("BYBIT_API_KEY", "").strip(),
            "api_secret": os.getenv("BYBIT_API_SECRET", "").strip(),
            "testnet": os.getenv("BYBIT_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
            "category": (os.getenv("BYBIT_CATEGORY", "linear") or "linear").strip().lower(),
        },
    }


if __name__ == "__main__":
    asyncio.run(_main())
