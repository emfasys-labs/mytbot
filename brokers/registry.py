"""
brokers/registry.py
===================
The broker registry. The ONLY place in the codebase you touch when
adding a new exchange.

To add Deribit: 1. Create brokers/deribit/adapter.py
                2. Add "deribit": DeribitAdapter  below
                3. Done. Nothing else changes.
"""

from brokers.base import BrokerAdapter
from brokers.ibkr.adapter import IBKRAdapter
from brokers.kraken.adapter import KrakenAdapter
from brokers.binance.adapter import BinanceAdapter
from brokers.alpaca.adapter import AlpacaAdapter
from brokers.trading212.adapter import Trading212Adapter
from brokers.capitalcom.adapter import CapitalComAdapter
from brokers.coinbase.adapter import CoinbaseAdapter
from brokers.ig.adapter import IGAdapter

# ─── Registry ─────────────────────────────────────────────────────────────────
# Add new brokers here — one line each.
# Bybit is optional: requires ``pip install pybit`` (see requirements.txt).

BROKER_REGISTRY: dict[str, type[BrokerAdapter]] = {
    "ibkr": IBKRAdapter,
    "kraken": KrakenAdapter,
    "binance": BinanceAdapter,
    "alpaca": AlpacaAdapter,
    "trading212": Trading212Adapter,
    "capitalcom": CapitalComAdapter,
    "coinbase": CoinbaseAdapter,
    "ig": IGAdapter,
    # "deribit": DeribitAdapter,    ← uncomment when ready
    # "okx":     OKXAdapter,        ← uncomment when ready
}

try:
    from brokers.bybit.adapter import BybitAdapter
except ImportError:
    pass
else:
    BROKER_REGISTRY["bybit"] = BybitAdapter


# ─── Factory ──────────────────────────────────────────────────────────────────

def get_broker(name: str, paper_mode: bool = True, **credentials) -> BrokerAdapter:
    """
    Instantiate a broker adapter by name.

    Usage:
        broker = get_broker("ibkr", paper_mode=True, account_id="...", port=7497)
        broker = get_broker("kraken", paper_mode=False, api_key="...", api_secret="...")
    """
    if name not in BROKER_REGISTRY:
        available = ", ".join(BROKER_REGISTRY.keys())
        raise ValueError(f"Unknown broker '{name}'. Available: {available}")

    adapter_class = BROKER_REGISTRY[name]
    return adapter_class(paper_mode=paper_mode, **credentials)


def list_brokers() -> list[str]:
    """Return names of all registered brokers."""
    return list(BROKER_REGISTRY.keys())
