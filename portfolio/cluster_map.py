"""
portfolio/cluster_map.py
========================
Theme/cluster detection for the portfolio orchestrator (D160).

Several "independent" positions are often ONE bet:
  * five forex pairs (AUDUSD long, EURUSD long, USDJPY short, …) are all
    "short the US dollar";
  * SPY long + QQQ long + IWM long is one "long US-equity beta" bet;
  * BTC long + ETH long + SOL long is one "long crypto beta" bet.

This maps a (symbol, asset_class, side) to a (cluster, theme_sign) so the
orchestrator can recognise the shared bet and express it ONCE — big — instead
of fragmenting capital and conviction across many correlated names.

``theme_sign`` is +1 / -1 in the cluster's reference direction:
  * fx_usd:       +1 = LONG USD,  -1 = SHORT USD
  * crypto_beta:  +1 = LONG crypto, -1 = SHORT crypto
  * equity_index: +1 = LONG beta,   -1 = SHORT beta

Definitions mirror the risk engine's cluster checks (D115/D131) so awareness
and (former) caps speak the same language.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.instrument_semantics import instrument_role

# US broad-market index ETFs that share systematic US-equity beta.
EQUITY_INDEX_SYMBOLS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV", "MDY",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "QID", "QLD",
})

# Near-substitutes that should be expressed once.  These are deliberately
# narrower than the factor map below: two companies in the same sector are
# correlated, but are not interchangeable; AGG/BND/IUSB and SPY/IVV/VOO are.
CORE_BOND_SYMBOLS: frozenset[str] = frozenset({"AGG", "BND", "IUSB"})
EX_US_BROAD_SYMBOLS: frozenset[str] = frozenset({"EFA", "VXUS"})

_FACTOR_MEMBERS: dict[str, frozenset[str]] = {
    "us_equity_beta": frozenset(
        set(EQUITY_INDEX_SYMBOLS)
        | {"VT", "VOOG", "DGRO", "IWD", "XLY"}
    ),
    "global_ex_us_equity": frozenset({"EFA", "VXUS", "VT", "EWT"}),
    "core_bonds": CORE_BOND_SYMBOLS,
    "short_duration_credit": frozenset({"VCSH", "MINT", "BOXX"}),
    "high_yield_credit": frozenset({"HYG"}),
    "municipal_bonds": frozenset({"MUB"}),
    "semiconductors": frozenset({"SOXX", "SMH", "AVGO", "QCOM", "LRCX"}),
    "consumer_staples": frozenset({"XLP", "KO", "MNST", "WMT"}),
    "health_care": frozenset({"VHT", "XBI", "CI", "CVS", "NVS"}),
    "utilities": frozenset({"XLU", "AEE"}),
    "energy_producers": frozenset({"XLE", "COP", "EOG", "FANG", "PBR"}),
}

_ETF_FACTORS: dict[str, dict[str, Decimal]] = {
    "SPY": {"us_equity_beta": Decimal("1")},
    "IVV": {"us_equity_beta": Decimal("1")},
    "VOO": {"us_equity_beta": Decimal("1")},
    "VT": {
        "us_equity_beta": Decimal("0.62"),
        "global_ex_us_equity": Decimal("0.38"),
    },
    "EFA": {"global_ex_us_equity": Decimal("1")},
    "VXUS": {"global_ex_us_equity": Decimal("1")},
    "SOXX": {"semiconductors": Decimal("1")},
    "SMH": {"semiconductors": Decimal("1")},
    "XLP": {"consumer_staples": Decimal("1")},
    "VHT": {"health_care": Decimal("1")},
    "XBI": {"health_care": Decimal("1")},
    "XLU": {"utilities": Decimal("1")},
    "XLE": {"energy_producers": Decimal("1")},
}


def _side_sign(side: str) -> int:
    s = (side or "").strip().lower()
    if s in ("buy", "long", "b"):
        return 1
    if s in ("sell", "short", "s"):
        return -1
    return 0


def fx_orientation(symbol: str) -> int:
    """+1 if USDxxx (long = long USD), -1 if xxxUSD (long = short USD), 0 otherwise.

    Mirrors ``RiskEngine._fx_pair_orientation``.
    """
    sym = (symbol or "").strip().upper().replace("/", "").replace("-", "").replace("=X", "")
    if "USD" not in sym:
        return 0
    if sym.startswith("USD"):
        return 1
    if sym.endswith("USD"):
        return -1
    return 0


def _is_crypto(symbol: str, asset_class: str) -> bool:
    if (asset_class or "").strip().lower() == "crypto":
        return True
    s = (symbol or "").strip().upper()
    return s.endswith("-USD") or s.endswith("USDT")


def theme_for(symbol: str, asset_class: str, side: str) -> tuple[str | None, int]:
    """Return ``(cluster_name, theme_sign)`` for a signal, or ``(None, 0)`` if
    the symbol is not part of a recognised correlated cluster (trade it on its
    own). ``theme_sign`` is the signed direction in the cluster's frame.
    """
    ss = _side_sign(side)
    if ss == 0:
        return (None, 0)

    # Crypto first — a crypto pair like BTC-USD also "contains USD" but is a
    # crypto-beta bet, not an FX bet.
    if _is_crypto(symbol, asset_class):
        return ("crypto_beta", ss)

    orient = fx_orientation(symbol)
    if orient != 0:
        return ("fx_usd", orient * ss)

    if (symbol or "").strip().upper() in EQUITY_INDEX_SYMBOLS:
        return ("equity_index", ss)

    sym = (symbol or "").strip().upper()
    if sym in CORE_BOND_SYMBOLS:
        return ("core_bonds", ss)
    if sym in EX_US_BROAD_SYMBOLS:
        return ("global_ex_us_equity", ss)

    return (None, 0)


def theme_sign_if_bought(symbol: str, asset_class: str) -> int:
    """The cluster theme_sign that results from BUYING this symbol.

    Used to translate a desired net theme direction back into a buy/sell on a
    chosen expression symbol: ``side = "buy" if theme_sign_if_bought == net_dir
    else "sell"``.
    """
    cluster, sign = theme_for(symbol, asset_class, "buy")
    return sign if cluster is not None else 0


def economic_factor_loadings(
    symbol: Any,
    asset_class: Any = "",
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Decimal]:
    """Return auditable factor loadings for portfolio overlap/risk controls."""
    sym = str(symbol or "").strip().upper().replace("=X", "")
    ac = str(getattr(asset_class, "value", asset_class) or "").strip().lower()
    role = instrument_role(sym, asset_class=ac, metadata=metadata)
    if role.value in {"cash_equivalent", "liquidity_reserve"}:
        return {"liquidity_reserve": Decimal("1")}
    if _is_crypto(sym, ac):
        return {"crypto_beta": Decimal("1")}
    orient = fx_orientation(sym)
    if orient:
        return {"usd_factor": Decimal(orient)}
    if sym in _ETF_FACTORS:
        return dict(_ETF_FACTORS[sym])
    factors = {
        name: Decimal("1")
        for name, members in _FACTOR_MEMBERS.items()
        if sym in members
    }
    if factors:
        return factors
    if sym:
        return {f"instrument:{sym}": Decimal("1")}
    return {"unclassified": Decimal("1")}


def factor_overlap(
    symbol_a: Any,
    asset_class_a: Any,
    symbol_b: Any,
    asset_class_b: Any,
) -> Decimal:
    """Weighted Jaccard-style economic overlap in ``[0, 1]``."""
    a = economic_factor_loadings(symbol_a, asset_class_a)
    b = economic_factor_loadings(symbol_b, asset_class_b)
    keys = set(a) | set(b)
    if not keys:
        return Decimal("0")
    numerator = sum(min(abs(a.get(k, 0)), abs(b.get(k, 0))) for k in keys)
    denominator = sum(max(abs(a.get(k, 0)), abs(b.get(k, 0))) for k in keys)
    return numerator / denominator if denominator > 0 else Decimal("0")
