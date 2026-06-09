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

# US broad-market index ETFs that share systematic US-equity beta.
EQUITY_INDEX_SYMBOLS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV", "MDY",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "QID", "QLD",
})


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

    return (None, 0)


def theme_sign_if_bought(symbol: str, asset_class: str) -> int:
    """The cluster theme_sign that results from BUYING this symbol.

    Used to translate a desired net theme direction back into a buy/sell on a
    chosen expression symbol: ``side = "buy" if theme_sign_if_bought == net_dir
    else "sell"``.
    """
    cluster, sign = theme_for(symbol, asset_class, "buy")
    return sign if cluster is not None else 0
