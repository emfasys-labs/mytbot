"""Instrument registry package (D116).

Self-updating cross-broker instrument master built from public maintained
sources. The registry feeds candidates into broker adapters and the
discovery funnel without ever bypassing the IBKR contract-qualification
gate or the risk engine.
"""

from instruments.canonical import (
    AssetClassHint,
    CanonicalSymbol,
    canonical_to_broker,
    detect_asset_class,
    from_broker_symbol,
    to_canonical,
)

__all__ = [
    "AssetClassHint",
    "CanonicalSymbol",
    "canonical_to_broker",
    "detect_asset_class",
    "from_broker_symbol",
    "to_canonical",
]
