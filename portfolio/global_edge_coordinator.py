"""
Global opportunity ranking: new opportunities vs held positions (expected remaining edge).
Emits incremental actions only; does not bypass risk or execution.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from portfolio.d015_replacement_context import ReplacementContext, churn_penalty_for_pair
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score

# Coordinator ranks across strategies; for the same *tradable* symbol, keep one
# opportunity (highest :attr:`StrategyOpportunity.priority_score`) so we do not
# emit duplicate opens. Arbitrage sleeves use distinct strategy_name values and
# are not collapsed here.
_OPPORTUNITY_DEDUPE_EXCLUDE: frozenset[str] = frozenset(
    {
        "funding_rate_arbitrage",
        "cross_exchange_arbitrage",
    }
)

DEFAULT_MODE = "trader"


@dataclass
class HeldPositionEdge:
    """Open position with an estimated remaining edge for displacement comparison."""

    symbol: str
    notional: Decimal
    expected_remaining_edge: Decimal
    strategy_name: str = "held_position"
    broker: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoordinatorAction:
    """Single incremental deployment intent."""

    kind: str  # open_strategy | trim_symbol
    symbol: str
    strategy_name: str
    capital: Decimal
    priority_score: Decimal
    metadata: dict[str, Any] = field(default_factory=dict)


def _decimal_or_none(v: Any) -> Decimal | None:
    """Parse a value into ``Decimal``, returning ``None`` on missing/invalid.

    Used by the D031 sizing pipeline to coerce heterogenous metadata values
    (``str``, ``int``, ``float``, ``Decimal``) without throwing.
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d


def _crypto_venue_room_budget() -> Decimal | None:
    """D129 — combined deploy room across the crypto paper-wallet venues.

    Returns the shared notional budget a crypto opportunity can actually
    be filled into (sum of each venue's ``venue_deploy_room``), or
    ``None`` when the paper-wallet model is disabled / not applicable —
    in which case the allocator applies no extra crypto bound (prior
    behaviour). The allocator decrements this pool as it sizes each
    crypto opp so it never proposes more crypto than the venues can hold.
    """
    try:
        from system.paper_wallet import CRYPTO_PAPER_BROKERS, venue_deploy_room
    except Exception:  # noqa: BLE001
        return None
    total = Decimal("0")
    any_bound = False
    for venue in CRYPTO_PAPER_BROKERS:
        try:
            room = venue_deploy_room(venue)
        except Exception:  # noqa: BLE001
            room = None
        if room is not None:
            any_bound = True
            total += Decimal(str(room))
    return total if any_bound else None


def _canonical_position_side(raw: Any) -> str:
    """Normalise long/short labels for coordinator guards (buy/sell tolerant)."""
    s = str(raw or "long").strip().lower()
    if s in ("buy", "long", "b"):
        return "long"
    if s in ("sell", "short", "s"):
        return "short"
    return s


# Cash-deployment factor per asset class — what fraction of the position's
# notional actually consumes operator capital (margin / cash). Forex defaults
# to 20% (~5x notional-to-cash) so paper/live rehearsal cannot quietly create
# extreme FX notionals; equity/crypto spot
# consume their full cost. The slider's intent is "cash deployed", not
# "notional gross", so this lookup converts between the two views.
#
# These defaults can be overridden via ``config/global_edge.yaml::cash_factors``
# (mapping asset_class → fraction). Unknown classes fall back to 1.0
# (treat as fully-funded, conservative).
_DEFAULT_CASH_FACTORS: dict[str, Decimal] = {
    "equity": Decimal("1.0"),
    "etf": Decimal("1.0"),
    "stock": Decimal("1.0"),
    "crypto": Decimal("1.0"),
    "bond": Decimal("0.20"),
    "forex": Decimal("0.20"),
    "fx": Decimal("0.20"),
    "future": Decimal("0.15"),
    "option": Decimal("1.0"),
}


_FOREX_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURJPY", "GBPJPY", "EURGBP", "EURCHF", "AUDJPY", "EURAUD", "EURCAD",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD", "AUDCAD", "AUDCHF", "AUDNZD",
    "CADJPY", "CHFJPY", "NZDJPY", "USDSEK", "USDNOK", "USDDKK", "USDZAR",
    "USDMXN", "USDTRY", "USDHKD", "USDSGD", "USDCNH",
}


def _infer_asset_class_from_symbol(symbol: str) -> str | None:
    """Infer asset class from yfinance / IBKR / Kraken symbol conventions.

    Returns one of: ``forex``, ``future``, ``crypto``, or ``None`` (caller
    keeps whatever upstream classification it had — typically ``equity``).
    """
    if not symbol:
        return None
    s = symbol.strip().upper()
    if s.endswith("=X"):
        return "forex"
    if s.endswith("=F"):
        return "future"
    if s.endswith("-USD") or s.endswith("-USDT") or s.endswith("USDT"):
        return "crypto"
    # 6-char FX pairs (USDCHF, USDJPY, EURUSD, ...) often arrive without a
    # suffix from broker reconciliation.
    if len(s) == 6 and s in _FOREX_PAIRS:
        return "forex"
    return None


_DEFAULT_MIN_ORDER_USD: dict[str, Decimal] = {
    # Approximate USD minimums derived from the GBP scale in
    # ``config/risk_limits.yaml::minimum_order_sizes_gbp`` (~1.27 GBP/USD).
    # The adaptive coordinator drops opps whose softmax-allocated notional
    # falls below this floor and redistributes their cash to higher-priority
    # peers. This prevents the cycle-by-cycle "min_order_size" risk-engine
    # rejections that left cash undeployed when the budget was sliced thin.
    "equity": Decimal("65"),
    "etf": Decimal("65"),
    "stock": Decimal("65"),
    "crypto": Decimal("15"),
    "bond": Decimal("1300"),
    "forex": Decimal("1300"),
    "fx": Decimal("1300"),
    "future": Decimal("6500"),
    "option": Decimal("650"),
}


def _min_order_notional(
    asset_class: str | None,
    *,
    symbol: str | None = None,
    cfg_overrides: dict[str, Any] | None = None,
) -> Decimal:
    """Look up the minimum order notional (USD) for an asset class.

    Falls back to symbol-pattern inference when the explicit asset class is
    missing or generically ``equity`` but the symbol pattern says otherwise
    (USDCHF → forex, BTC-USD → crypto, NQ=F → future).
    """
    key = (asset_class or "").strip().lower() or "equity"
    if symbol and (not key or key in ("", "equity", "stock")):
        inferred = _infer_asset_class_from_symbol(symbol)
        if inferred is not None:
            key = inferred
    if cfg_overrides:
        v = cfg_overrides.get(key)
        if v is not None:
            try:
                return Decimal(str(v))
            except Exception:  # noqa: BLE001
                pass
    return _DEFAULT_MIN_ORDER_USD.get(key, Decimal("65"))


def cash_factor_for_asset_class(
    asset_class: str | None,
    overrides: dict[str, Any] | None = None,
    *,
    symbol: str | None = None,
) -> Decimal:
    """Return the cash-deployment factor for an asset class.

    ``factor = cash_used / notional`` — multiply notional by this to get the
    operator-capital impact. Forex defaults to 0.20, equity/crypto
    return 1.0, etc.

    Falls back to ``_infer_asset_class_from_symbol(symbol)`` when the
    asset_class metadata is missing or generically ``equity`` but the symbol
    pattern strongly suggests otherwise (USDCHF → forex, BTC-USD → crypto,
    NQ=F → future). This rescues operator capital from being mis-counted as
    1:1 when an upstream pipeline stripped the asset_class.

    ``overrides`` may be a ``config/global_edge.yaml::cash_factors`` mapping
    that supersedes the built-in defaults for any class.
    """
    key = (asset_class or "").strip().lower() or "equity"
    # If asset_class is missing or the generic ``equity`` default, try to
    # infer from the symbol — this catches the case where forex/crypto
    # positions arrive with asset_class hard-coded to ``equity``.
    if symbol and (not key or key in ("", "equity", "stock")):
        inferred = _infer_asset_class_from_symbol(symbol)
        if inferred is not None:
            key = inferred
    if overrides:
        v = overrides.get(key)
        if v is not None:
            try:
                return Decimal(str(v))
            except Exception:  # noqa: BLE001
                pass
    return _DEFAULT_CASH_FACTORS.get(key, Decimal("1.0"))


def _adaptive_sizing_enabled() -> bool:
    """Feature flag for the adaptive-sizing rewrite (Phase 1+).

    When OFF (default), the coordinator falls back to the legacy static
    priority-score stubs. When ON, ``_adaptive_priority_components`` derives
    liquidity / execution / regime_fit / risk_cost from candidate features so
    the coordinator's softmax ranking can actually differentiate opportunities.
    """
    return os.environ.get("USE_ADAPTIVE_SIZING", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _clip01(value: Any, *, lo: str = "0", hi: str = "1") -> Decimal:
    """Coerce *value* to ``Decimal`` and clip to ``[lo, hi]``. Safe on bad input."""
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(lo)
    lo_d = Decimal(lo)
    hi_d = Decimal(hi)
    if d < lo_d:
        return lo_d
    if d > hi_d:
        return hi_d
    return d


def _adaptive_priority_components(
    cand_meta: dict[str, Any],
    *,
    side: str = "long",
    asset_class: str = "",
    legacy_liq: Decimal = Decimal("0.7"),
    legacy_exe: Decimal = Decimal("0.75"),
    legacy_reg: Decimal = Decimal("0.8"),
    legacy_risk: Decimal = Decimal("0.05"),
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Derive (liquidity, execution, regime_fit, risk_cost) from candidate features.

    All scores are in ``[0, 1]`` (risk_cost is "lower is better" per
    :func:`compute_priority_score` convention). When a feature is missing the
    function falls back to the supplied legacy value, so swapping to adaptive
    mode never *worsens* the ranking — it can only sharpen it where features
    are present. When :func:`_adaptive_sizing_enabled` is False, the legacy
    quad is returned unchanged.

    Sources consulted (all optional):
      * ``spread_bps`` / ``spread_pct``           → liquidity penalty
      * ``depth_score`` / ``volume_z_score``      → liquidity boost
      * ``broker_quality`` / ``execution_quality``→ execution score (router feedback)
      * ``demand_score`` + ``side`` (long/short)  → regime alignment
      * ``atr_pct`` / ``realised_vol``            → risk_cost
    """
    if not _adaptive_sizing_enabled():
        return legacy_liq, legacy_exe, legacy_reg, legacy_risk

    # ---- Liquidity ---------------------------------------------------------
    liq = legacy_liq
    spread_bps = cand_meta.get("spread_bps")
    if spread_bps is None and cand_meta.get("spread_pct") is not None:
        try:
            spread_bps = float(cand_meta.get("spread_pct")) * 10000.0
        except Exception:  # noqa: BLE001
            spread_bps = None
    if spread_bps is not None:
        try:
            sb = float(spread_bps)
            # 0 bps spread → 1.0, 50+ bps → 0.0, linear in between
            liq_from_spread = max(0.0, min(1.0, 1.0 - sb / 50.0))
            liq = Decimal(str(liq_from_spread))
        except Exception:  # noqa: BLE001
            pass
    # Volume / depth boost — high z-score adds liquidity confidence
    vz = cand_meta.get("volume_z_score") or cand_meta.get("volume_z")
    if vz is not None:
        try:
            boost = max(0.0, min(0.20, float(vz) * 0.05))
            liq = _clip01(liq + Decimal(str(boost)))
        except Exception:  # noqa: BLE001
            pass
    depth = cand_meta.get("depth_score")
    if depth is not None:
        try:
            liq = _clip01((liq + Decimal(str(float(depth)))) / Decimal("2"))
        except Exception:  # noqa: BLE001
            pass

    # ---- Execution ---------------------------------------------------------
    # Routing quality is normalised by the router into roughly [-1, 1]; map
    # to [0, 1] with 0 → ~0.5 (neutral) so unseen broker/symbol pairs default
    # to the legacy value.
    exe = legacy_exe
    bq = cand_meta.get("execution_quality")
    if bq is None:
        bq = cand_meta.get("broker_quality")
    if bq is not None:
        try:
            v = float(bq)
            mapped = max(0.0, min(1.0, 0.5 + v * 0.5))
            exe = Decimal(str(mapped))
        except Exception:  # noqa: BLE001
            pass

    # ---- Regime alignment --------------------------------------------------
    # Keep the existing demand-alignment derivation; it's already adaptive.
    reg = legacy_reg
    try:
        demand_score = float(cand_meta.get("demand_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        demand_score = 0.0
    side_sign = 1.0 if side.strip().lower() in ("long", "buy") else -1.0
    align = max(-1.0, min(1.0, demand_score * side_sign))
    reg = Decimal(str(max(0.55, min(0.95, 0.8 + align * 0.12))))
    cand_meta["demand_alignment"] = round(align, 6)

    # ---- Risk cost (lower is better) --------------------------------------
    risk = legacy_risk
    atr_pct = cand_meta.get("atr_pct")
    if atr_pct is None:
        atr_pct = cand_meta.get("realised_vol")
    if atr_pct is not None:
        try:
            v = float(atr_pct)
            # 1% atr → 0.02, 5% atr → 0.10, capped
            risk = Decimal(str(max(0.01, min(0.30, v * 2.0))))
        except Exception:  # noqa: BLE001
            pass

    return liq, exe, reg, risk


def signal_candidate_to_strategy_opportunity(
    cand: Any,
    *,
    nav: Decimal,
    position_pct: Decimal,
    price: Decimal,
    max_position_pct: Decimal = Decimal("0.10"),
) -> StrategyOpportunity | None:
    """Map a D015 ``SignalCandidate`` to comparable ``StrategyOpportunity``.

    **Sizing pipeline (D031)** — strategy intent is the source of truth:

      1. If ``cand.metadata["risk_notional_override"]`` is present, it is the
         proposed base notional. This is the volatility / ATR-aware size the
         strategy actually asked for (see ``signals/engine.py``).
      2. Else if ``cand.metadata["target_notional"]`` is present, use it.
      3. Else fall back to ``nav * position_pct`` (legacy behaviour).

    Then apply a **hard cap only** of ``nav * max_position_pct`` (risk-limits
    ceiling). The cap is a ceiling, never a floor — small strategy-requested
    sizes are NEVER inflated upwards.

    All sizing decisions are recorded transparently in ``metadata`` under
    ``sizing_*`` keys so that the dashboard, logs and tests can audit why
    each trade was sized the way it was.

    Prior to D031 this function silently threw away the strategy's
    volatility-aware sizing and forced every directional signal through
    ``cap = nav * position_pct``, producing a systematic 7-13× over-sizing
    of low/medium-conviction trades (see ``docs/DECISIONS.md`` D031).
    """
    try:
        edge = Decimal(str(getattr(cand, "adjusted_signal_strength", "0") or "0"))
        conf = Decimal(str(getattr(cand, "confidence", "0") or "0"))
    except Exception:  # noqa: BLE001
        return None
    if price <= 0 or nav <= 0:
        return None
    # YFinance continuous futures (NQ=F, ES=F, ...) are data-only until the
    # runtime futures contract resolver is enabled. Suppress them before global
    # edge ranking so executable equity/crypto/FX opportunities can surface.
    _sym = str(getattr(cand, "symbol", "") or "")
    futures_enabled = os.environ.get("FUTURES_EXECUTION_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    skip_futures = os.environ.get("SKIP_CONTINUOUS_FUTURES", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if _sym.endswith("=F") and (skip_futures or not futures_enabled):
        return None

    cand_meta = dict(getattr(cand, "metadata", {}) or {})
    cand_ac = getattr(cand, "asset_class", None)
    if cand_ac and "asset_class" not in cand_meta:
        cand_meta["asset_class"] = str(cand_ac)
    cand_meta.setdefault("close", str(price))
    cand_meta.setdefault("price", str(price))
    cand_meta.setdefault("side", str(getattr(cand, "side", "long")))

    # ------------------------------------------------------------------ D031A
    # Priority: risk_notional_override > target_notional > nav * position_pct
    strategy_target_notional = _decimal_or_none(cand_meta.get("target_notional"))
    strategy_risk_override = _decimal_or_none(cand_meta.get("risk_notional_override"))
    nav_fallback = nav * position_pct

    if strategy_risk_override is not None and strategy_risk_override > 0:
        proposed_base = strategy_risk_override
        sizing_source = "risk_notional_override"
    elif strategy_target_notional is not None and strategy_target_notional > 0:
        proposed_base = strategy_target_notional
        sizing_source = "target_notional"
    else:
        proposed_base = nav_fallback
        sizing_source = "nav_fallback"

    if proposed_base <= 0:
        return None

    # TEMP book-build multiplier — read once from env, applied before hard cap.
    # Set BOOK_BUILD_NOTIONAL_MULTIPLIER=1.0 (or unset) to disable.
    try:
        _bbm = Decimal(str(os.environ.get("BOOK_BUILD_NOTIONAL_MULTIPLIER", "1.0")))
        if _bbm > 0 and _bbm != Decimal("1.0"):
            proposed_base = proposed_base * _bbm
            cand_meta["sizing_buildup_multiplier"] = str(_bbm)
    except Exception:  # noqa: BLE001
        pass

    hard_cap = nav * max_position_pct
    if hard_cap > 0 and proposed_base > hard_cap:
        final_cap = hard_cap
        sizing_clipped = True
        sizing_clip_reason = f"nav*{max_position_pct}"
    else:
        final_cap = proposed_base
        sizing_clipped = False
        sizing_clip_reason = None

    if final_cap <= 0:
        return None

    # ------------------------------------------------------------------ D031B
    cand_meta["sizing_strategy_target_notional"] = (
        str(strategy_target_notional) if strategy_target_notional is not None else None
    )
    cand_meta["sizing_risk_notional_override"] = (
        str(strategy_risk_override) if strategy_risk_override is not None else None
    )
    cand_meta["sizing_source"] = sizing_source
    cand_meta["sizing_proposed_base_notional"] = str(proposed_base)
    cand_meta["sizing_hard_cap_notional"] = str(hard_cap)
    cand_meta["sizing_final_capital_required"] = str(final_cap)
    cand_meta["sizing_clipped"] = sizing_clipped
    cand_meta["sizing_clip_reason"] = sizing_clip_reason
    cand_meta["sizing_nav_at_decision"] = str(nav)
    cand_meta["sizing_max_position_pct"] = str(max_position_pct)

    side_txt = str(getattr(cand, "side", "long")).strip().lower()
    asset_class_txt = str(cand_meta.get("asset_class", "")).strip().lower()
    liq, exe, reg, risk = _adaptive_priority_components(
        cand_meta,
        side=side_txt,
        asset_class=asset_class_txt,
    )
    if not _adaptive_sizing_enabled():
        # Preserve legacy behaviour: keep the existing demand_alignment side-
        # effect on cand_meta even when adaptive helper is disabled.
        try:
            demand_score = float(cand_meta.get("demand_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            demand_score = 0.0
        side_sign = 1.0 if side_txt in ("long", "buy") else -1.0
        align = max(-1.0, min(1.0, demand_score * side_sign))
        reg = Decimal(str(max(0.55, min(0.95, 0.8 + align * 0.12))))
        cand_meta["demand_alignment"] = round(align, 6)
    cand_meta["priority_components"] = {
        "liquidity_score": str(liq),
        "execution_score": str(exe),
        "regime_fit_score": str(reg),
        "risk_cost_score": str(risk),
        "adaptive": _adaptive_sizing_enabled(),
    }
    ps = compute_priority_score(edge, conf, reg, exe, risk)
    return StrategyOpportunity(
        strategy_name=str(getattr(cand, "strategy_name", "unknown")),
        symbol=str(getattr(cand, "symbol", "")),
        side=str(getattr(cand, "side", "long")),
        created_at=getattr(cand, "timestamp", datetime.now(timezone.utc)),
        expected_edge=edge,
        confidence=conf,
        capital_required=final_cap,
        expected_holding_hours=24,
        liquidity_score=liq,
        execution_score=exe,
        regime_fit_score=reg,
        risk_cost_score=risk,
        priority_score=ps,
        metadata=cand_meta,
    )


def funding_arb_signal_to_strategy_opportunity(
    sig: Any,
    *,
    capital: Decimal,
    edge_boost: Decimal,
) -> StrategyOpportunity:
    """From ``FundingArbSignal`` after strategy evaluation."""
    from strategies.arbitrage.models import FundingArbSignal

    if not isinstance(sig, FundingArbSignal):
        raise TypeError("expected FundingArbSignal")
    ann = sig.annualised_net_yield
    edge = min(Decimal("1"), ann + edge_boost)
    conf = sig.confidence
    meta = {
        "arbitrage_kind": "funding_rate",
        "spot_venue": sig.spot_venue,
        "perp_venue": sig.perp_venue,
        "annualised_net_yield": str(ann),
        "basis_bps": str(sig.basis_bps),
        **dict(sig.metadata or {}),
    }
    liq, exe, reg, risk = _adaptive_priority_components(
        meta,
        side=str(sig.side or "long"),
        asset_class="crypto",
        legacy_liq=Decimal("0.9"),
        legacy_exe=Decimal("0.85"),
        legacy_reg=Decimal("0.9"),
        legacy_risk=Decimal("0.03"),
    )
    ps = compute_priority_score(edge, conf, reg, exe, risk)
    return StrategyOpportunity(
        strategy_name=sig.strategy_name,
        symbol=sig.symbol,
        side=sig.side,
        created_at=sig.created_at,
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=sig.expected_hold_hours,
        liquidity_score=liq,
        execution_score=exe,
        regime_fit_score=reg,
        risk_cost_score=risk,
        priority_score=ps,
        metadata=meta,
    )


def funding_arb_to_strategy_opportunity(
    opp: Any,
    *,
    capital: Decimal,
    edge_boost: Decimal,
) -> StrategyOpportunity:
    """Wrap funding arb opportunity scoring object into ``StrategyOpportunity``."""
    ann = getattr(opp, "annualised_net_yield", Decimal("0"))
    edge = min(Decimal("1"), ann + edge_boost)
    conf = getattr(opp, "confidence", Decimal("0.8"))
    snap = getattr(opp, "snapshot", None)
    meta = {
        "arbitrage_kind": "funding_rate",
        "spot_venue": getattr(opp, "spot_venue", ""),
        "perp_venue": getattr(opp, "perp_venue", ""),
        "annualised_net_yield": str(ann),
    }
    liq, exe, reg, risk = _adaptive_priority_components(
        meta,
        side="long",
        asset_class="crypto",
        legacy_liq=Decimal("0.9"),
        legacy_exe=Decimal("0.85"),
        legacy_reg=Decimal("0.9"),
        legacy_risk=Decimal("0.03"),
    )
    ps = compute_priority_score(edge, conf, reg, exe, risk)
    return StrategyOpportunity(
        strategy_name="funding_rate_arbitrage",
        symbol=str(getattr(opp, "symbol", "")),
        side="ARBITRAGE_LONG_SPOT_SHORT_PERP",
        created_at=datetime.now(timezone.utc),
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=int(getattr(opp, "expected_hold_hours", 72)),
        liquidity_score=liq,
        execution_score=exe,
        regime_fit_score=reg,
        risk_cost_score=risk,
        priority_score=ps,
        metadata=meta,
    )


def cross_exchange_dict_to_strategy_opportunity(
    d: dict[str, Any],
    *,
    capital: Decimal,
    edge_boost: Decimal,
) -> StrategyOpportunity:
    meta = dict(d.get("metadata") or {})
    try:
        net_spread = Decimal(str(meta.get("net_spread", "0")))
    except Exception:  # noqa: BLE001
        net_spread = Decimal("0")
    if capital > 0:
        edge = (net_spread / capital).quantize(Decimal("0.0001")) + edge_boost
    else:
        edge = edge_boost
    edge = min(Decimal("1"), max(Decimal("0"), edge))
    conf = Decimal(str(d.get("confidence", "0.75")))
    liq, exe, reg, risk = _adaptive_priority_components(
        meta,
        side=str(d.get("side", "long")),
        asset_class="crypto",
        legacy_liq=Decimal("0.75"),
        legacy_exe=Decimal("0.65"),
        legacy_reg=Decimal("0.85"),
        legacy_risk=Decimal("0.06"),
    )
    ps = compute_priority_score(edge, conf, reg, exe, risk)
    return StrategyOpportunity(
        strategy_name="cross_exchange_arbitrage",
        symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "ARBITRAGE_SPOT_SPREAD")),
        created_at=datetime.now(timezone.utc),
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=1,
        liquidity_score=liq,
        execution_score=exe,
        regime_fit_score=reg,
        risk_cost_score=risk,
        priority_score=ps,
        metadata=meta,
    )


class GlobalEdgeCoordinator:
    def __init__(self, config: dict[str, Any], logger: Any | None = None) -> None:
        self._cfg = config
        self._logger = logger

    def _attrib_mult(
        self,
        symbol: str | None,
        strategy_name: str | None,
        kind: str | None = None,
    ) -> Decimal:
        """Net-of-cost evidence governor multiplier (≥1.0) for the edge bar.

        Reads the rolling per-bucket / per-symbol attribution the loop
        injects as ``cfg['edge_attribution']``. A persistently
        money-losing bucket/symbol widens its required edge steeply
        (auto-recovering when it turns net-positive). Returns 1.0 — a
        no-op — when attribution is absent, so every other path/test is
        unaffected.
        """
        attr = self._cfg.get("edge_attribution")
        if not isinstance(attr, dict) or not attr:
            return Decimal("1")
        try:
            from system.edge_attribution import required_threshold_multiplier

            m = required_threshold_multiplier(
                symbol, strategy_name, attr, kind=kind
            )
            return Decimal(str(max(1.0, float(m))))
        except Exception:  # noqa: BLE001 — governor must never break ranking
            return Decimal("1")

    def _threshold(self, mode: str) -> Decimal:
        """Edge threshold for the displacement gate.

        Phase 2 adaptive path: the threshold is computed from current
        execution costs (fee + spread + slippage) and recent realised
        outcomes via :mod:`system.adaptive_edge`. The static YAML map
        (``edge_advantage: {hunter, trader, defender}``) is preserved as
        a floor — we never go more aggressive than the operator's
        existing setting until Phase 5 strips the YAML entirely.

        Falls back to the pure-static behaviour on any error so a buggy
        adaptive layer can never block the gate.
        """
        # Phase 5: ``edge_advantage`` is now a scalar (hunter value) in
        # YAML. We still accept the legacy dict shape so older configs
        # don't break — fall through to the scalar form once collapsed.
        ea = self._cfg.get("edge_advantage", "0.05")
        key = (mode or DEFAULT_MODE).strip().lower()
        if key not in ("hunter", "trader", "defender"):
            key = DEFAULT_MODE
        if isinstance(ea, dict):
            static_floor = float(ea.get(key, ea.get("trader", "0.05")))
        else:
            try:
                static_floor = float(ea)
            except (TypeError, ValueError):
                static_floor = 0.05
        try:
            from system.adaptive_edge import (
                EdgeThresholdInputs,
                compute_edge_threshold,
            )
            adaptive_cfg = self._cfg.get("adaptive_edge") or {}
            cost_bps = adaptive_cfg.get("cross_venue_cost_bps")
            win_rate = adaptive_cfg.get("recent_win_rate")
            avg_ret = adaptive_cfg.get("recent_avg_return")
            return compute_edge_threshold(
                EdgeThresholdInputs(
                    mode=key,
                    cross_venue_cost_bps=float(cost_bps) if cost_bps is not None else None,
                    recent_win_rate=float(win_rate) if win_rate is not None else None,
                    recent_avg_return=float(avg_ret) if avg_ret is not None else None,
                    static_floor=static_floor,
                )
            )
        except Exception:  # noqa: BLE001
            return Decimal(str(static_floor))

    def _max_actions_for_mode(self, mode: str) -> int:
        """Per-mode cap on coordinator actions emitted per tick.

        Accepts either a scalar (``max_actions_per_tick: 3``) — applied
        uniformly to every mode, preserving v1 behaviour — or a dict
        keyed by mode name. Unknown / malformed values fall back to 3.
        """
        raw = self._cfg.get("max_actions_per_tick", 3)
        key = (mode or DEFAULT_MODE).strip().lower()
        if key not in ("hunter", "trader", "defender"):
            key = DEFAULT_MODE
        # Scalar: legacy behaviour — one number for all modes.
        if isinstance(raw, (int, float)):
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                return 3
        if isinstance(raw, str):
            try:
                return max(1, int(raw))
            except ValueError:
                return 3
        if isinstance(raw, dict):
            v = raw.get(key)
            if v is None:
                v = raw.get("trader", 3)
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                return 3
        return 3

    def _notional_fraction_for_mode(self, mode: str) -> Decimal:
        """Per-mode fraction applied to each opportunity's requested capital.

        Defender trims aggressively (risk off), trader scales moderately, hunter
        deploys the full strategy-requested size. Prior to D030 this was a single
        scalar (``0.15``) uniformly applied to every mode — which silently
        throttled hunter to ~15% of its intended deployment and produced the
        "sleeping hunter" symptom where only a small fraction of capital was put
        to work despite many valid opportunities.

        Accepts:
          * scalar (``"0.15"``) — legacy uniform cap, preserved for back-compat,
          * dict keyed by mode (``{hunter: "1.0", trader: "0.5", defender: "0.15"}``).
        Unknown / malformed values fall back to ``0.15``.
        """
        raw = self._cfg.get("max_notional_fraction_per_action", "0.15")
        key = (mode or DEFAULT_MODE).strip().lower()
        if key not in ("hunter", "trader", "defender"):
            key = DEFAULT_MODE
        # Scalar: legacy behaviour — one value for all modes.
        if isinstance(raw, (int, float, str)):
            try:
                return Decimal(str(raw))
            except Exception:  # noqa: BLE001
                return Decimal("0.15")
        if isinstance(raw, dict):
            v = raw.get(key)
            if v is None:
                v = raw.get("trader", "0.15")
            try:
                return Decimal(str(v))
            except Exception:  # noqa: BLE001
                return Decimal("0.15")
        return Decimal("0.15")

    def propose_actions(
        self,
        held: list[HeldPositionEdge],
        new_opportunities: list[StrategyOpportunity],
        *,
        active_mode: str = DEFAULT_MODE,
        replacement_context: ReplacementContext | None = None,
        max_actions: int | None = None,
        gross_target_capital: Decimal | None = None,
        concentration_exponent: Decimal | None = None,
        max_position_notional: Decimal | None = None,
    ) -> list[CoordinatorAction]:
        """Rank new opportunities vs weakest held edge and emit replacement actions.

        **Adaptive sizing path (Phase 2+3, behind ``USE_ADAPTIVE_SIZING``):**
        when both ``gross_target_capital`` and ``concentration_exponent`` are
        supplied AND the feature flag is on, the per-mode integer action cap
        and the per-mode notional fraction are *both* removed. Capital is
        allocated across the qualifying opportunities via a priority-weighted
        softmax against ``gross_target_capital``. With one dominant opportunity
        and high concentration, a single position can absorb ~100% of the
        target — this is the design intent for Hunter aggressive deployment.
        """
        if (
            _adaptive_sizing_enabled()
            and gross_target_capital is not None
            and gross_target_capital > 0
        ):
            return self._propose_actions_adaptive(
                held,
                new_opportunities,
                active_mode=active_mode,
                replacement_context=replacement_context,
                gross_target_capital=gross_target_capital,
                concentration_exponent=(
                    concentration_exponent
                    if concentration_exponent is not None
                    else Decimal("1.0")
                ),
                max_position_notional=max_position_notional,
            )

        thresh = self._threshold(active_mode)
        cap_n = (
            max_actions
            if max_actions is not None
            else self._max_actions_for_mode(active_mode)
        )

        weakest_edge = Decimal("0")
        if held:
            weakest_edge = min(h.expected_remaining_edge for h in held)

        ranked = sorted(new_opportunities, key=lambda o: o.priority_score, reverse=True)
        available_held = sorted(held, key=lambda h: h.expected_remaining_edge)
        out: list[CoordinatorAction] = []
        emit_trim = bool(self._cfg.get("emit_trim_actions", True))

        emit_trim_cfg = bool(self._cfg.get("emit_trim_actions", True))
        for opp in ranked:
            if len(out) >= cap_n:
                break
            # Net-of-cost evidence governor: a persistently bleeding
            # bucket/symbol must clear a steeply widened bar (auto-recovers
            # when net-positive). When a trim will be paired (this path
            # emits one whenever a held partner exists), also fold in the
            # trim bucket's bleed so global_edge_trim is throttled too.
            eff_thresh = thresh * self._attrib_mult(
                opp.symbol, getattr(opp, "strategy_name", None)
            )
            if emit_trim_cfg and held:
                eff_thresh = eff_thresh * self._attrib_mult(
                    None, "global_edge_trim", "trim"
                )
            if opp.expected_edge <= weakest_edge + eff_thresh:
                continue
            opp_side = _canonical_position_side(getattr(opp, "side", None))
            if any(
                h.symbol.strip().upper() == opp.symbol.strip().upper()
                and _canonical_position_side((h.metadata or {}).get("side")) == opp_side
                for h in held
            ):
                # Already deployed this directional exposure; avoid re-opening the same
                # leg every loop (especially when paper fills are instant). Opposite-side
                # opportunities still flow through for trims / flips via risk+execution.
                continue
            trim_edge: HeldPositionEdge | None = None
            if available_held:
                for i, h in enumerate(available_held):
                    if h.symbol.strip().upper() == opp.symbol.strip().upper():
                        continue
                    trim_edge = available_held.pop(i)
                    break
            if replacement_context is not None and held:
                skip_churn = False
                for h in held:
                    if h.symbol.strip().upper() == opp.symbol.strip().upper():
                        continue
                    pen = churn_penalty_for_pair(
                        h.symbol,
                        opp.symbol,
                        recent_events=replacement_context.recent_events,
                        max_events=10,
                        penalty_per_event=Decimal("0.02"),
                    )
                    if pen > Decimal("0.15"):
                        skip_churn = True
                        if self._logger:
                            self._logger.debug(
                                "global_edge | skip churn | {} -> {} | pen={}",
                                h.symbol,
                                opp.symbol,
                                pen,
                            )
                        break
                if skip_churn:
                    continue
            if emit_trim and trim_edge is not None and len(out) < cap_n:
                trim_meta = dict(trim_edge.metadata or {})
                trim_meta["coordinator_kind"] = "trim_symbol"
                trim_meta["reduce_only"] = True
                trim_meta["close_only"] = True
                trim_meta["target_notional"] = str(trim_edge.notional)
                trim_meta["risk_notional_override"] = str(trim_edge.notional)
                if trim_edge.broker:
                    trim_meta["broker"] = trim_edge.broker
                out.append(
                    CoordinatorAction(
                        kind="trim_symbol",
                        symbol=trim_edge.symbol,
                        strategy_name="global_edge_trim",
                        capital=trim_edge.notional,
                        priority_score=opp.priority_score,
                        metadata=trim_meta,
                    )
                )
                if len(out) >= cap_n:
                    break

            frac = self._notional_fraction_for_mode(active_mode)
            # Hunter ``frac == 1.0`` → deploy the strategy-requested amount in
            # full; defender ``frac == 0.15`` → trim to 15% of request. The
            # ``min(1, frac)`` clamp prevents an accidental >100% blow-up if
            # ops configures e.g. ``1.5``.
            clamped_frac = min(Decimal("1"), frac)
            cap = opp.capital_required * clamped_frac if opp.capital_required > 0 else Decimal("0")
            final_capital = cap if cap > 0 else opp.capital_required

            # D031B: propagate + extend sizing audit trail so the final
            # CoordinatorAction carries the full sizing provenance (pre-mode
            # capital_required, the mode fraction applied, and the post-mode
            # final capital). ``coordinator_action_to_raw_signal`` preserves
            # these fields into the RawSignal metadata for execution-boundary
            # guards and dashboard display.
            action_meta = dict(opp.metadata)
            action_meta.setdefault("confidence", str(opp.confidence))
            action_meta.setdefault("expected_edge", str(opp.expected_edge))
            action_meta.setdefault("side", str(opp.side))
            action_meta["allocation_selected"] = True
            action_meta["sizing_pre_mode_capital"] = str(opp.capital_required)
            action_meta["sizing_mode"] = (active_mode or DEFAULT_MODE).strip().lower()
            action_meta["sizing_mode_fraction"] = str(clamped_frac)
            action_meta["sizing_final_action_capital"] = str(final_capital)

            out.append(
                CoordinatorAction(
                    kind="open_strategy",
                    symbol=opp.symbol,
                    strategy_name=opp.strategy_name,
                    capital=final_capital,
                    priority_score=opp.priority_score,
                    metadata=action_meta,
                )
            )

        return out

    def _propose_actions_adaptive(
        self,
        held: list[HeldPositionEdge],
        new_opportunities: list[StrategyOpportunity],
        *,
        active_mode: str,
        replacement_context: ReplacementContext | None,
        gross_target_capital: Decimal,
        concentration_exponent: Decimal,
        max_position_notional: Decimal | None = None,
    ) -> list[CoordinatorAction]:
        """Adaptive book sizing — no fixed action count, no fixed notional fraction.

        Algorithm:
          1. Filter ``new_opportunities`` by the same dedup / churn / already-held
             rules as the legacy path.
          2. Compute the displacement gate: ``opp.expected_edge > weakest_held_edge
             + edge_advantage(mode)``. Surviving opps are *qualifying*.
          3. Softmax weights ``w_i ∝ exp(lambda * priority_score_i ^
             concentration_exponent)`` across qualifying opps. ``lambda`` is
             pulled from cfg ``adaptive.softmax_lambda`` (default ``1.0``).
          4. Allocate ``capital_i = gross_target_capital * w_i``.
          5. Emit one ``open_strategy`` per qualifying opp + matching ``trim_symbol``
             when ``emit_trim_actions`` is True and a held position is being
             displaced.

        With a single dominant opportunity, the softmax collapses to ~1.0 on
        the winner — Hunter naturally goes ~100% into the rocketing name.
        """
        import math

        thresh = self._threshold(active_mode)
        emit_trim = bool(self._cfg.get("emit_trim_actions", True))
        adaptive_cfg = self._cfg.get("adaptive") or {}
        # ``softmax_lambda`` is the base sharpness — bigger ⇒ more
        # winner-take-all. Default 5.0 makes a priority gap of ~0.7 produce a
        # ≈95% softmax mass on the leader (with concentration_exponent=1).
        # ``concentration_exponent`` (passed in by the loop, mode-derived)
        # multiplies that sharpness so Hunter (ce≈2.5) goes essentially 100%
        # on a dominant opp; Defender (ce≈1.0) keeps softer concentration.
        try:
            lam = float(adaptive_cfg.get("softmax_lambda", 5.0))
        except (TypeError, ValueError):
            lam = 5.0
        try:
            ce = float(concentration_exponent)
        except (TypeError, ValueError):
            ce = 1.0
        # Effective sharpness — ce acts as a temperature scale.
        lam_eff = lam * max(0.1, ce)

        weakest_edge = Decimal("0")
        if held:
            weakest_edge = min(h.expected_remaining_edge for h in held)

        ranked = sorted(new_opportunities, key=lambda o: o.priority_score, reverse=True)
        available_held = sorted(held, key=lambda h: h.expected_remaining_edge)

        # ---- Build-up vs. displacement mode (CASH-DEPLOYED basis) ----------
        # The slider's intent is "cash deployed", not "notional gross". Forex
        # positions show larger notionals but consume only their margin factor;
        # equity/crypto positions consume their full cost. Both the
        # build-up gate and the per-opp sizing therefore work in *cash* space.
        #
        # ``gross_target_capital`` is the *remaining cash budget* (the loop
        # passes ``absolute_cash_target - held_cash_used``). Absolute cash
        # target is reconstructed as ``held_cash_used + gross_target_capital``.
        # Trims only fire when cash usage exceeds 105% of target.
        cash_overrides = self._cfg.get("cash_factors") or None
        held_cash_used = sum(
            (
                h.notional
                * cash_factor_for_asset_class(
                    str((h.metadata or {}).get("asset_class") or ""),
                    cash_overrides,
                    symbol=h.symbol,
                )
                for h in held
            ),
            Decimal("0"),
        )
        absolute_cash_target = held_cash_used + gross_target_capital
        displacement_mode = held_cash_used > absolute_cash_target * Decimal("1.05")

        # Re-entry debounce (close→reopen churn fix). The recycle/shed path
        # culls a dead-edge position; without this the build-up path re-opens
        # the SAME symbol next iteration → it's flat again → culled again,
        # bleeding spread+fees every loop on the same ~10 names. A symbol the
        # recycle/shed path culled within ``symbol_cooldown_sec`` is not
        # re-opened here. Dynamic, config-driven debounce — NOT a position
        # cap; uses the same window the recycle/shed/rotation paths use.
        _rc_cfg = self._cfg.get("capital_recycle") or {}
        _sh_cfg = self._cfg.get("shed") or {}
        _rt_cfg = self._cfg.get("rotation") or {}
        try:
            _reentry_cd = int(
                _rc_cfg.get(
                    "symbol_cooldown_sec",
                    _sh_cfg.get(
                        "symbol_cooldown_sec",
                        _rt_cfg.get("symbol_cooldown_sec", 900),
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            _reentry_cd = 900
        _now_reentry = datetime.now(timezone.utc)

        def _recently_culled(sym: str) -> bool:
            if replacement_context is None or _reentry_cd <= 0:
                return False
            cull_map = getattr(replacement_context, "last_cull_at_by_symbol", None) or {}
            if not cull_map:
                return False
            key = str(sym).strip().upper()
            last = cull_map.get(key)
            if last is None:
                n = key
                for suf in ("=X", "=F"):
                    if n.endswith(suf):
                        n = n[: -len(suf)]
                if n.endswith("-USD") and len(n) > 4:
                    n = n[:-4]
                last = cull_map.get(n)
            if last is None:
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (_now_reentry - last.astimezone(timezone.utc)).total_seconds() < _reentry_cd

        # ---- Filter to qualifying opportunities --------------------------
        qualifying: list[tuple[StrategyOpportunity, HeldPositionEdge | None, HeldPositionEdge | None]] = []
        for opp in ranked:
            opp_side = _canonical_position_side(getattr(opp, "side", None))
            if _recently_culled(opp.symbol):
                continue
            same_side_held = next(
                (
                    h
                    for h in held
                    if h.symbol.strip().upper() == opp.symbol.strip().upper()
                    and _canonical_position_side((h.metadata or {}).get("side")) == opp_side
                ),
                None,
            )
            # Net-of-cost evidence governor: widen the edge bar for a
            # bucket/symbol that is persistently bleeding (auto-recovers
            # when it turns net-positive). 1.0 = no-op when no attribution.
            eff_thresh = thresh * self._attrib_mult(
                opp.symbol, getattr(opp, "strategy_name", None)
            )
            # In displacement mode every qualifying open forces a paired
            # trim — so the trim bucket's own bleed must also raise the
            # bar for opening here (throttles the worst churn engine,
            # global_edge_trim, at its source).
            if displacement_mode:
                eff_thresh = eff_thresh * self._attrib_mult(
                    None, "global_edge_trim", "trim"
                )
            is_topup = same_side_held is not None and not displacement_mode
            if is_topup:
                if opp.expected_edge <= eff_thresh:
                    continue
            elif opp.expected_edge <= weakest_edge + eff_thresh:
                continue
            if same_side_held is not None and not is_topup:
                continue
            # Churn skip
            skip_churn = False
            if replacement_context is not None and held:
                for h in held:
                    if h.symbol.strip().upper() == opp.symbol.strip().upper():
                        continue
                    pen = churn_penalty_for_pair(
                        h.symbol,
                        opp.symbol,
                        recent_events=replacement_context.recent_events,
                        max_events=10,
                        penalty_per_event=Decimal("0.02"),
                    )
                    if pen > Decimal("0.15"):
                        skip_churn = True
                        break
            if skip_churn:
                continue
            # Reserve a trim partner ONLY when we are above target — i.e. the
            # new open must displace an existing position to fit. During
            # build-up we never pop a trim_edge so no trims are emitted.
            trim_edge: HeldPositionEdge | None = None
            if displacement_mode and available_held:
                for i, h in enumerate(available_held):
                    if h.symbol.strip().upper() == opp.symbol.strip().upper():
                        continue
                    trim_edge = available_held.pop(i)
                    break
            qualifying.append((opp, trim_edge, same_side_held if is_topup else None))

        if not qualifying and not displacement_mode and gross_target_capital > 0:
            for h in sorted(held, key=lambda x: x.expected_remaining_edge, reverse=True):
                if h.expected_remaining_edge <= thresh * self._attrib_mult(
                    h.symbol, getattr(h, "strategy_name", None)
                ):
                    continue
                h_meta = dict(h.metadata or {})
                h_side = _canonical_position_side(h_meta.get("side"))
                qualifying.append(
                    (
                        StrategyOpportunity(
                            strategy_name=str(h.strategy_name or "held_edge_topup"),
                            symbol=h.symbol,
                            side=h_side,
                            created_at=datetime.now(timezone.utc),
                            expected_edge=h.expected_remaining_edge,
                            confidence=Decimal(str(h_meta.get("confidence", "0.65") or "0.65")),
                            capital_required=gross_target_capital,
                            expected_holding_hours=24,
                            liquidity_score=Decimal("0.75"),
                            execution_score=Decimal("0.75"),
                            regime_fit_score=Decimal("0.75"),
                            risk_cost_score=Decimal("0"),
                            priority_score=max(h.expected_remaining_edge, Decimal("0")),
                            metadata={
                                **h_meta,
                                "sizing_topup_source": "held_remaining_edge",
                            },
                        ),
                        None,
                        h,
                    )
                )

        if not qualifying:
            return []

        # ---- Softmax weights with minimum-order enforcement --------------
        # raw_i = lam_eff * priority_i, then standard exp-normalise. Subtract
        # max for numerical stability. With lam_eff = lam * concentration,
        # Hunter (high ce) sharpens; Defender (low ce) stays softer.
        #
        # If the resulting per-opp notional falls below its asset-class
        # minimum order size, drop the lowest-priority opp and recompute the
        # softmax across the survivors. This prevents the risk engine from
        # silently rejecting tiny slices and keeps the cash budget fully
        # deployed across fewer, bigger positions.
        min_overrides = self._cfg.get("minimum_order_sizes_usd") or None

        def _softmax(
            opps: list[tuple[StrategyOpportunity, HeldPositionEdge | None, HeldPositionEdge | None]]
        ) -> list[float]:
            raws: list[float] = []
            for opp, _, _ in opps:
                try:
                    p = float(opp.priority_score)
                except (TypeError, ValueError):
                    p = 0.0
                raws.append(lam_eff * max(0.0, p))
            mx = max(raws) if raws else 0.0
            exps = [math.exp(r - mx) for r in raws]
            tot = sum(exps) or 1.0
            return [e / tot for e in exps]

        def _norm_sym(s: str) -> str:
            s = s.strip().upper()
            for suf in ("=X", "=F"):
                if s.endswith(suf):
                    s = s[: -len(suf)]
                    break
            if s.endswith("-USD") and len(s) > 4:
                # crypto: CAKE-USD ≡ CAKEUSDT for some reconciliation
                s = s[:-4]
            return s

        # Iteratively shrink the qualifying set until every survivor's
        # softmax-allocated notional clears its asset-class minimum.
        while qualifying:
            weights = _softmax(qualifying)
            below_min: list[int] = []
            for idx, ((opp, _trim, _topup), w) in enumerate(zip(qualifying, weights, strict=True)):
                opp_md = opp.metadata or {}
                ac = str(opp_md.get("asset_class") or "").strip().lower()
                cf = cash_factor_for_asset_class(ac, cash_overrides, symbol=opp.symbol)
                if cf <= 0:
                    cf = Decimal("1.0")
                cash_share = gross_target_capital * Decimal(str(w))
                notional = cash_share / cf
                # Apply the same per-symbol concentration cap as the emit
                # loop so we don't fail-and-drop opps that would have been
                # legitimately clipped (and still clear the minimum).
                if max_position_notional is not None and max_position_notional > 0:
                    opp_norm = _norm_sym(opp.symbol)
                    existing_sym_notional = sum(
                        (h.notional for h in held if _norm_sym(h.symbol) == opp_norm),
                        Decimal("0"),
                    )
                    # Add a small safety buffer (1%) so we land just inside the
                    # cap rather than exactly at it (avoids float-edge rejects).
                    room = (max_position_notional - existing_sym_notional) * Decimal("0.99")
                    if room < 0:
                        room = Decimal("0")
                    if notional > room:
                        notional = room
                min_n = _min_order_notional(ac, symbol=opp.symbol, cfg_overrides=min_overrides)
                if notional < min_n:
                    below_min.append(idx)
            if not below_min:
                break
            # Drop the LOWEST-priority below-minimum opp (last in the
            # priority-sorted list). Its cash mass redistributes via softmax
            # to the survivors. Repeat until nothing is below min or the
            # qualifying set is empty.
            drop_idx = max(below_min, key=lambda i: i)  # last (lowest priority)
            del qualifying[drop_idx]

        if not qualifying:
            return []
        weights = _softmax(qualifying)

        # ---- Emit actions -------------------------------------------------
        # ``gross_target_capital`` is the remaining CASH budget. Each opp's
        # cash share is ``w_i * cash_budget``; we then convert to notional
        # via the asset-class cash factor (forex default 20% → ~5x on
        # notional, equity 1.0 → notional == cash).
        #
        # D129 — venue-aware crypto sizing. The crypto paper venues each
        # have a small wallet (~$50k); their combined deploy room is a
        # single shared pool. Without this, the allocator sizes a crypto
        # opp at 5% of *total* NAV (~$61k) blind to the venue wallet, so
        # the order is skipped/rerouted at execution — wasted cycles.
        # Here the allocator clamps crypto opps to the real crypto-sleeve
        # room and decrements the pool as it allocates.
        crypto_room_budget = _crypto_venue_room_budget()
        out: list[CoordinatorAction] = []
        for (opp, trim_edge, topup_edge), w in zip(qualifying, weights, strict=True):
            opp_meta = opp.metadata or {}
            opp_ac = str(opp_meta.get("asset_class") or "").strip().lower()
            cf = cash_factor_for_asset_class(opp_ac, cash_overrides, symbol=opp.symbol)
            cash_i = gross_target_capital * Decimal(str(w))
            # Convert cash to notional. Avoid div-by-zero for misconfigured
            # cash_factor; fall back to cash == notional (factor 1.0).
            if cf <= 0:
                cf = Decimal("1.0")
            cap_i = cash_i / cf
            # Per-position concentration cap — the risk engine will reject
            # any position whose new+existing notional exceeds NAV ×
            # max_concentration_pct. Account for existing held exposure on
            # this symbol so the new open lands inside the remaining cap.
            if max_position_notional is not None and max_position_notional > 0:
                opp_norm = _norm_sym(opp.symbol)
                existing_sym_notional = sum(
                    (h.notional for h in held if _norm_sym(h.symbol) == opp_norm),
                    Decimal("0"),
                )
                # Add a small safety buffer (1%) so we land just inside the
                # cap rather than exactly at it (avoids float-edge rejects).
                room = (max_position_notional - existing_sym_notional) * Decimal("0.99")
                if room <= 0:
                    continue  # this symbol is already at-cap; skip
                if cap_i > room:
                    cap_i = room
                    cash_i = cap_i * cf
            # D129 — venue-aware crypto clamp. A crypto opp can only
            # execute against the crypto paper venues' shared deploy
            # room. Clamp to the remaining pool and decrement it; when
            # the pool is exhausted, skip the opp cleanly here rather
            # than letting execution waste a cycle on a doomed order.
            crypto_clamped = False
            if crypto_room_budget is not None and opp_ac == "crypto":
                if crypto_room_budget <= 0:
                    continue
                if cap_i > crypto_room_budget:
                    cap_i = crypto_room_budget
                    cash_i = cap_i * cf
                    crypto_clamped = True

            cap_i = cap_i.quantize(Decimal("0.01"))
            cash_i = cash_i.quantize(Decimal("0.01"))
            if cap_i <= 0:
                continue
            min_n = _min_order_notional(opp_ac, symbol=opp.symbol, cfg_overrides=min_overrides)
            if cap_i < min_n:
                continue
            if crypto_room_budget is not None and opp_ac == "crypto":
                crypto_room_budget -= cap_i

            if emit_trim and trim_edge is not None:
                trim_meta = dict(trim_edge.metadata or {})
                trim_meta["coordinator_kind"] = "trim_symbol"
                trim_meta["reduce_only"] = True
                trim_meta["close_only"] = True
                trim_meta["target_notional"] = str(trim_edge.notional)
                trim_meta["risk_notional_override"] = str(trim_edge.notional)
                if trim_edge.broker:
                    trim_meta["broker"] = trim_edge.broker
                out.append(
                    CoordinatorAction(
                        kind="trim_symbol",
                        symbol=trim_edge.symbol,
                        strategy_name="global_edge_trim",
                        capital=trim_edge.notional,
                        priority_score=opp.priority_score,
                        metadata=trim_meta,
                    )
                )

            action_meta = dict(opp.metadata)
            action_meta.setdefault("confidence", str(opp.confidence))
            action_meta.setdefault("expected_edge", str(opp.expected_edge))
            action_meta.setdefault("side", str(opp.side))
            action_meta["allocation_selected"] = True
            action_meta["sizing_mode"] = (active_mode or DEFAULT_MODE).strip().lower()
            action_meta["sizing_path"] = "adaptive_softmax"
            action_meta["sizing_softmax_weight"] = f"{w:.6f}"
            action_meta["sizing_softmax_lambda"] = str(lam)
            action_meta["sizing_softmax_lambda_effective"] = str(lam_eff)
            action_meta["sizing_concentration_exponent"] = str(ce)
            action_meta["sizing_cash_used"] = str(cash_i)
            action_meta["sizing_cash_factor"] = str(cf)
            action_meta["sizing_held_cash_used"] = str(held_cash_used)
            action_meta["sizing_cash_target_absolute"] = str(absolute_cash_target)
            # Boundary guard (execution/engine.py::_passes_sizing_boundary_guard)
            # reads ``sizing_final_capital_required`` to validate that the
            # actual order notional matches coordinator intent within 1.25×.
            # Set it here so the adaptive path participates in the same audit
            # contract as the legacy directional path.
            action_meta["sizing_final_capital_required"] = str(cap_i)
            if crypto_clamped:
                action_meta["sizing_crypto_venue_clamped"] = True
            action_meta["sizing_gross_target_capital"] = str(gross_target_capital)
            action_meta["sizing_qualifying_count"] = str(len(qualifying))
            action_meta["sizing_pre_mode_capital"] = str(opp.capital_required)
            action_meta["sizing_final_action_capital"] = str(cap_i)
            if topup_edge is not None:
                action_meta["sizing_topup_existing"] = True
                action_meta["sizing_existing_notional"] = str(topup_edge.notional)

            out.append(
                CoordinatorAction(
                    kind="open_strategy",
                    symbol=opp.symbol,
                    strategy_name=opp.strategy_name,
                    capital=cap_i,
                    priority_score=opp.priority_score,
                    metadata=action_meta,
                )
            )

        return out

    def propose_shed_actions(
        self,
        held: list[HeldPositionEdge],
        *,
        cash_target_absolute: Decimal,
        active_mode: str = DEFAULT_MODE,
        replacement_context: ReplacementContext | None = None,
    ) -> list[CoordinatorAction]:
        """Emit reduce-only ``trim_symbol`` actions to bring held cash usage
        down to ``cash_target_absolute``.

        Used when the operator slides the capital allocation DOWN — instead
        of waiting for natural displacement to roll positions off, the loop
        immediately closes the largest cash-using positions until the book
        fits inside the new sleeve. Closes happen via the same execution
        path as normal trims; risk_engine re-validates each close intent.

        Selection order: largest cash-used first, breaking ties by weakest
        expected_remaining_edge (so we shed the least-promising holdings).
        """
        if not held:
            return []
        cash_overrides = self._cfg.get("cash_factors") or None

        def _cash_used(h: HeldPositionEdge) -> Decimal:
            ac = str((h.metadata or {}).get("asset_class") or "")
            cf = cash_factor_for_asset_class(ac, cash_overrides, symbol=h.symbol)
            return h.notional * cf

        held_cash_total = sum((_cash_used(h) for h in held), Decimal("0"))
        if held_cash_total <= cash_target_absolute:
            return []
        shed_cfg = self._cfg.get("shed") or {}
        rot_cfg = self._cfg.get("rotation") or {}
        try:
            symbol_cooldown_sec = int(shed_cfg.get("symbol_cooldown_sec", rot_cfg.get("symbol_cooldown_sec", 900)))
        except Exception:  # noqa: BLE001
            symbol_cooldown_sec = 900
        now = datetime.now(timezone.utc)

        def _norm_sym(s: str) -> str:
            x = s.strip().upper()
            for suf in ("=X", "=F"):
                if x.endswith(suf):
                    return x[: -len(suf)]
            if x.endswith("-USD") and len(x) > 4:
                return x[:-4]
            return x

        def _recently_touched(sym: str) -> bool:
            if replacement_context is None or symbol_cooldown_sec <= 0:
                return False
            last = replacement_context.last_event_at_by_symbol.get(_norm_sym(sym))
            if last is None:
                last = replacement_context.last_event_at_by_symbol.get(str(sym).strip().upper())
            if last is None:
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (now - last.astimezone(timezone.utc)).total_seconds() < symbol_cooldown_sec

        excess = held_cash_total - cash_target_absolute
        # Largest-cash-first, tie-break weakest-edge.
        ranked = sorted(
            held,
            key=lambda h: (-float(_cash_used(h)), float(h.expected_remaining_edge)),
        )
        out: list[CoordinatorAction] = []
        shed_so_far = Decimal("0")
        for h in ranked:
            if shed_so_far >= excess:
                break
            if _recently_touched(h.symbol):
                continue
            cu = _cash_used(h)
            if cu <= 0:
                continue
            ac = str((h.metadata or {}).get("asset_class") or "")
            cf = cash_factor_for_asset_class(ac, cash_overrides, symbol=h.symbol)
            if cf <= 0:
                cf = Decimal("1.0")
            remaining_excess = max(Decimal("0"), excess - shed_so_far)
            trim_notional = min(h.notional, remaining_excess / cf)
            if trim_notional <= 0:
                continue
            trim_cash = trim_notional * cf
            meta = dict(h.metadata or {})
            meta["coordinator_kind"] = "trim_symbol"
            meta["reduce_only"] = True
            if trim_notional < h.notional:
                meta["partial_reduce_only"] = True
                meta["close_only"] = False
            else:
                meta["close_only"] = True
            meta["target_notional"] = str(trim_notional)
            meta["risk_notional_override"] = str(trim_notional)
            meta["sizing_path"] = "adaptive_shed_to_target"
            meta["sizing_cash_used"] = str(trim_cash)
            meta["sizing_cash_target_absolute"] = str(cash_target_absolute)
            meta["sizing_held_cash_total_pre"] = str(held_cash_total)
            meta["sizing_final_capital_required"] = str(trim_notional)
            meta["shed_symbol_cooldown_sec"] = str(symbol_cooldown_sec)
            if h.broker:
                meta["broker"] = h.broker
            out.append(
                CoordinatorAction(
                    kind="trim_symbol",
                    symbol=h.symbol,
                    strategy_name="adaptive_shed",
                    capital=trim_notional,
                    priority_score=Decimal("0"),
                    metadata=meta,
                )
            )
            shed_so_far += trim_cash
        return out

    def propose_capital_recycle_actions(
        self,
        held: list[HeldPositionEdge],
        *,
        active_mode: str = DEFAULT_MODE,
        replacement_context: ReplacementContext | None = None,
    ) -> list[CoordinatorAction]:
        """Free capital when the book is full, *independent of rotation edge*.

        The rotation path only fires when a fresh opportunity beats a held
        position by ``min_edge_advantage + fees``. When nothing clears that
        bar the book stays 100% deployed and idle indefinitely — realised
        P&L flat-lines even though winners could be banked and dead weight
        culled. This method is the missing capital-recycling path:

          * **Take-profit** — a position whose unrealised return exceeds its
            live remaining-edge estimate gets a reduce-only trim of
            ``take_profit_trim_fraction`` (lock the gain, let the rest run).
          * **Dead-edge cull** — a position whose live
            ``expected_remaining_edge`` ≤ ``dead_edge_floor`` (flat/losing
            per the live held-edge proxy) is closed reduce-only.

        Freed cash is redeployed by the build-up path on the next iteration,
        so the system continuously recycles worst→best instead of locking
        solid. Bounded by ``max_actions_per_tick`` to avoid fee churn. All
        knobs are YAML-driven (``global_edge.yaml: capital_recycle``); the
        defaults are deliberately conservative.
        """
        if not held:
            return []
        cfg = self._cfg.get("capital_recycle") or {}
        if not bool(cfg.get("enabled", True)):
            return []

        def _d(key: str, default: str) -> Decimal:
            try:
                return Decimal(str(cfg.get(key, default)))
            except (TypeError, ValueError):
                return Decimal(default)

        take_profit_edge_multiplier = _d("take_profit_edge_multiplier", "1")
        trim_fraction = _d("take_profit_trim_fraction", "0.50")
        dead_edge_floor = _d("dead_edge_floor", "0.01")
        # NOTE: the net-of-cost governor must NOT throttle this path. A
        # dead-edge cull is a reduce-only RISK EXIT of a position with no
        # remaining edge — exactly what should happen to a loser. An
        # earlier build shrank ``dead_edge_floor`` when the capital_recycle
        # bucket was net-negative, but that bucket is *structurally*
        # net-negative (cutting losers realises losses), so it suppressed
        # loss-cutting and froze underwater positions for days. Anti-churn
        # is enforced on the RE-ENTRY side instead — the just-culled symbol
        # is blocked by ``last_cull_at_by_symbol`` re-entry cooldown +
        # churn penalty (both governed) — so we cull freely here and stop
        # the open→reopen churn where it actually occurs.
        try:
            max_actions = int(cfg.get("max_actions_per_tick", 3))
        except (TypeError, ValueError):
            max_actions = 3
        rot_cfg = self._cfg.get("rotation") or {}
        try:
            symbol_cooldown_sec = int(cfg.get("symbol_cooldown_sec", rot_cfg.get("symbol_cooldown_sec", 900)))
        except Exception:  # noqa: BLE001
            symbol_cooldown_sec = 900
        if max_actions <= 0:
            return []
        trim_fraction = min(Decimal("1"), max(Decimal("0"), trim_fraction))
        now = datetime.now(timezone.utc)

        def _norm_sym(s: str) -> str:
            x = s.strip().upper()
            for suf in ("=X", "=F"):
                if x.endswith(suf):
                    return x[: -len(suf)]
            if x.endswith("-USD") and len(x) > 4:
                return x[:-4]
            return x

        def _recently_touched(sym: str) -> bool:
            if replacement_context is None or symbol_cooldown_sec <= 0:
                return False
            last = replacement_context.last_event_at_by_symbol.get(_norm_sym(sym))
            if last is None:
                last = replacement_context.last_event_at_by_symbol.get(str(sym).strip().upper())
            if last is None:
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (now - last.astimezone(timezone.utc)).total_seconds() < symbol_cooldown_sec

        def _unrl(h: HeldPositionEdge) -> Decimal:
            try:
                return Decimal(str((h.metadata or {}).get("unrealised_return", "0") or "0"))
            except (TypeError, ValueError):
                return Decimal("0")

        def _take_profit_trigger(h: HeldPositionEdge) -> Decimal:
            return max(Decimal("0"), h.expected_remaining_edge) * max(
                Decimal("0"), take_profit_edge_multiplier
            )

        def _is_recyclable_winner(h: HeldPositionEdge) -> bool:
            ret = _unrl(h)
            return ret > 0 and ret >= _take_profit_trigger(h)

        winners = [h for h in held if _is_recyclable_winner(h)]
        winners.sort(key=lambda h: float(_unrl(h)), reverse=True)
        dead = [h for h in held if h.expected_remaining_edge <= dead_edge_floor]
        dead.sort(key=lambda h: float(h.expected_remaining_edge))

        out: list[CoordinatorAction] = []
        seen: set[str] = set()
        for h in winners + dead:
            if len(out) >= max_actions:
                break
            if h.symbol in seen or h.notional <= 0:
                continue
            if _recently_touched(h.symbol):
                continue
            seen.add(h.symbol)
            is_winner = _is_recyclable_winner(h)
            if is_winner and trim_fraction < 1:
                trim_notional = (h.notional * trim_fraction)
                close_only = False
                reason = "take_profit_trim"
            else:
                trim_notional = h.notional
                close_only = True
                reason = "take_profit_close" if is_winner else "dead_edge_cull"
            if trim_notional <= 0:
                continue
            meta = dict(h.metadata or {})
            meta["coordinator_kind"] = "trim_symbol"
            meta["reduce_only"] = True
            meta["partial_reduce_only"] = not close_only
            meta["close_only"] = close_only
            meta["target_notional"] = str(trim_notional)
            meta["risk_notional_override"] = str(trim_notional)
            meta["sizing_path"] = "capital_recycle"
            meta["sizing_final_capital_required"] = str(trim_notional)
            meta["capital_recycle_reason"] = reason
            meta["capital_recycle_symbol_cooldown_sec"] = str(symbol_cooldown_sec)
            meta["capital_recycle_take_profit_trigger"] = str(_take_profit_trigger(h))
            if h.broker:
                meta["broker"] = h.broker
            out.append(
                CoordinatorAction(
                    kind="trim_symbol",
                    symbol=h.symbol,
                    strategy_name="capital_recycle",
                    capital=trim_notional,
                    priority_score=Decimal("0"),
                    metadata=meta,
                )
            )
        return out

    def propose_idle_loss_recycle_actions(
        self,
        held: list[HeldPositionEdge],
        *,
        active_mode: str = DEFAULT_MODE,
        replacement_context: ReplacementContext | None = None,
    ) -> list[CoordinatorAction]:
        """Free the weakest losing holding when the book is otherwise idle.

        This covers the under-deployed dead zone: the build path wants more
        positions, but every new open is gated out, so existing losers can sit
        indefinitely. The selection is rank/evidence based rather than a fixed
        stop distance: only holdings with negative live unrealised return are
        eligible, then the lowest remaining-edge names are recycled first.
        """
        if not held:
            return []
        cfg = self._cfg.get("capital_recycle") or {}
        if not bool(cfg.get("enabled", True)):
            return []
        if not bool(cfg.get("idle_loss_recycle_enabled", True)):
            return []

        try:
            max_actions = int(cfg.get("idle_loss_max_actions_per_tick", cfg.get("max_actions_per_tick", 1)))
        except (TypeError, ValueError):
            max_actions = 1
        if max_actions <= 0:
            return []

        rot_cfg = self._cfg.get("rotation") or {}
        try:
            symbol_cooldown_sec = int(cfg.get("symbol_cooldown_sec", rot_cfg.get("symbol_cooldown_sec", 900)))
        except Exception:  # noqa: BLE001
            symbol_cooldown_sec = 900

        now = datetime.now(timezone.utc)

        def _norm_sym(s: str) -> str:
            x = s.strip().upper()
            for suf in ("=X", "=F"):
                if x.endswith(suf):
                    return x[: -len(suf)]
            if x.endswith("-USD") and len(x) > 4:
                return x[:-4]
            return x

        def _recently_touched(sym: str) -> bool:
            if replacement_context is None or symbol_cooldown_sec <= 0:
                return False
            last = replacement_context.last_event_at_by_symbol.get(_norm_sym(sym))
            if last is None:
                last = replacement_context.last_event_at_by_symbol.get(str(sym).strip().upper())
            if last is None:
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (now - last.astimezone(timezone.utc)).total_seconds() < symbol_cooldown_sec

        def _unrl(h: HeldPositionEdge) -> Decimal:
            try:
                return Decimal(str((h.metadata or {}).get("unrealised_return", "0") or "0"))
            except (TypeError, ValueError):
                return Decimal("0")

        losers = [h for h in held if h.notional > 0 and _unrl(h) < 0 and not _recently_touched(h.symbol)]
        losers.sort(key=lambda h: (h.expected_remaining_edge, _unrl(h), -h.notional))

        out: list[CoordinatorAction] = []
        for h in losers[:max_actions]:
            meta = dict(h.metadata or {})
            meta["coordinator_kind"] = "trim_symbol"
            meta["reduce_only"] = True
            meta["close_only"] = True
            meta["partial_reduce_only"] = False
            meta["target_notional"] = str(h.notional)
            meta["risk_notional_override"] = str(h.notional)
            meta["sizing_path"] = "idle_loss_recycle"
            meta["sizing_final_capital_required"] = str(h.notional)
            meta["capital_recycle_reason"] = "idle_loss_recycle"
            meta["capital_recycle_symbol_cooldown_sec"] = str(symbol_cooldown_sec)
            if h.broker:
                meta["broker"] = h.broker
            out.append(
                CoordinatorAction(
                    kind="trim_symbol",
                    symbol=h.symbol,
                    strategy_name="capital_recycle",
                    capital=h.notional,
                    priority_score=Decimal("0"),
                    metadata=meta,
                )
            )
        return out

    def propose_rotation_actions(
        self,
        held: list[HeldPositionEdge],
        new_opportunities: list[StrategyOpportunity],
        *,
        active_mode: str = DEFAULT_MODE,
        replacement_context: ReplacementContext | None = None,
    ) -> list[CoordinatorAction]:
        """Fee-aware close-and-replace actions when the book is already at target.

        Adaptive build-up intentionally stops once the cash target is met. Hunter
        still needs a pulse: if fresh opportunity quality beats weak held edge by
        enough to pay estimated switching costs, rotate the cash into the better
        instrument instead of standing still.
        """
        if not held or not new_opportunities:
            return []
        rot_cfg = self._cfg.get("rotation") or {}
        if not bool(rot_cfg.get("enabled", True)):
            return []
        mode = (active_mode or DEFAULT_MODE).strip().lower()
        raw_max = rot_cfg.get("max_replacements_per_tick", 0)
        if isinstance(raw_max, dict):
            raw_max = raw_max.get(mode, raw_max.get("trader", 0))
        try:
            max_repl = int(raw_max)
        except Exception:  # noqa: BLE001
            max_repl = 0
        if max_repl <= 0:
            return []

        raw_adv = rot_cfg.get("min_edge_advantage", self._threshold(mode))
        if isinstance(raw_adv, dict):
            raw_adv = raw_adv.get(mode, raw_adv.get("trader", self._threshold(mode)))
        try:
            min_adv = Decimal(str(raw_adv))
        except Exception:  # noqa: BLE001
            min_adv = self._threshold(mode)
        # Net-of-cost evidence governor: when the rotation bucket is
        # persistently bleeding, every rotation must clear a steeply
        # widened edge advantage (auto-recovers once rotation turns
        # net-positive). Bucket-level throttle on the worst churn engine.
        min_adv = min_adv * self._attrib_mult(None, "global_edge_rotation", "rotation")
        try:
            fee_bps = Decimal(str(rot_cfg.get("estimated_round_trip_fee_bps", "20")))
        except Exception:  # noqa: BLE001
            fee_bps = Decimal("20")
        try:
            fee_mult = Decimal(str(rot_cfg.get("fee_edge_multiplier", "1.0")))
        except Exception:  # noqa: BLE001
            fee_mult = Decimal("1.0")
        try:
            symbol_cooldown_sec = int(rot_cfg.get("symbol_cooldown_sec", 900))
        except Exception:  # noqa: BLE001
            symbol_cooldown_sec = 900
        try:
            min_hold_sec = int(rot_cfg.get("min_hold_sec", symbol_cooldown_sec))
        except Exception:  # noqa: BLE001
            min_hold_sec = symbol_cooldown_sec
        try:
            churn_penalty = Decimal(str(rot_cfg.get("churn_penalty_per_event", "0.08")))
        except Exception:  # noqa: BLE001
            churn_penalty = Decimal("0.08")
        try:
            churn_block_threshold = Decimal(str(rot_cfg.get("churn_block_threshold", "0.16")))
        except Exception:  # noqa: BLE001
            churn_block_threshold = Decimal("0.16")
        fee_edge_cost = max(Decimal("0"), fee_bps) / Decimal("10000") * max(Decimal("0"), fee_mult)
        required_advantage = min_adv + fee_edge_cost

        cash_overrides = self._cfg.get("cash_factors") or None

        def _cash_used(h: HeldPositionEdge) -> Decimal:
            ac = str((h.metadata or {}).get("asset_class") or "")
            cf = cash_factor_for_asset_class(ac, cash_overrides, symbol=h.symbol)
            if cf <= 0:
                cf = Decimal("1.0")
            return h.notional * cf

        def _norm_sym(s: str) -> str:
            x = s.strip().upper()
            for suf in ("=X", "=F"):
                if x.endswith(suf):
                    return x[: -len(suf)]
            if x.endswith("-USD") and len(x) > 4:
                return x[:-4]
            return x

        ranked_held = sorted(held, key=lambda h: (h.expected_remaining_edge, -_cash_used(h)))
        ranked_new = sorted(new_opportunities, key=lambda o: o.priority_score, reverse=True)
        out: list[CoordinatorAction] = []
        used_new: set[str] = set()
        used_held: set[str] = set()
        now = datetime.now(timezone.utc)

        def _recently_touched(sym: str, seconds: int) -> bool:
            if replacement_context is None or seconds <= 0:
                return False
            last = replacement_context.last_event_at_by_symbol.get(_norm_sym(sym))
            if last is None:
                last = replacement_context.last_event_at_by_symbol.get(str(sym).strip().upper())
            if last is None:
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (now - last.astimezone(timezone.utc)).total_seconds()
            return age < seconds

        for weak in ranked_held:
            if len(out) // 2 >= max_repl:
                break
            weak_norm = _norm_sym(weak.symbol)
            if weak_norm in used_held:
                continue
            if _recently_touched(weak.symbol, min_hold_sec):
                continue
            weak_side = _canonical_position_side((weak.metadata or {}).get("side"))
            cash_budget = _cash_used(weak)
            if cash_budget <= 0:
                continue
            for opp in ranked_new:
                opp_norm = _norm_sym(opp.symbol)
                if opp_norm == weak_norm or opp_norm in used_new:
                    continue
                if _recently_touched(opp.symbol, symbol_cooldown_sec):
                    continue
                opp_side = _canonical_position_side(getattr(opp, "side", None))
                if any(
                    _norm_sym(h.symbol) == opp_norm
                    and _canonical_position_side((h.metadata or {}).get("side")) == opp_side
                    for h in held
                ):
                    continue
                if replacement_context is not None:
                    pen = churn_penalty_for_pair(
                        weak.symbol,
                        opp.symbol,
                        recent_events=replacement_context.recent_events,
                        max_events=10,
                        penalty_per_event=churn_penalty,
                    )
                    if pen >= churn_block_threshold:
                        continue
                advantage = Decimal(str(opp.priority_score)) - Decimal(str(weak.expected_remaining_edge))
                if advantage <= required_advantage:
                    continue

                trim_meta = dict(weak.metadata or {})
                trim_meta["coordinator_kind"] = "trim_symbol"
                trim_meta["reduce_only"] = True
                trim_meta["close_only"] = True
                trim_meta["target_notional"] = str(weak.notional)
                trim_meta["risk_notional_override"] = str(weak.notional)
                trim_meta["rotation_reason"] = "hunter_fee_aware_replacement"
                trim_meta["rotation_replacement_symbol"] = opp.symbol
                trim_meta["rotation_advantage"] = str(advantage)
                trim_meta["rotation_required_advantage"] = str(required_advantage)
                trim_meta["rotation_estimated_round_trip_fee_bps"] = str(fee_bps)
                if weak.broker:
                    trim_meta["broker"] = weak.broker

                opp_meta = dict(opp.metadata)
                opp_ac = str(opp_meta.get("asset_class") or "").strip().lower()
                cf = cash_factor_for_asset_class(opp_ac, cash_overrides, symbol=opp.symbol)
                if cf <= 0:
                    cf = Decimal("1.0")
                open_notional = (cash_budget / cf).quantize(Decimal("0.01"))
                open_cash = (open_notional * cf).quantize(Decimal("0.01"))
                opp_meta.setdefault("confidence", str(opp.confidence))
                opp_meta.setdefault("expected_edge", str(opp.expected_edge))
                opp_meta.setdefault("side", str(opp.side))
                opp_meta["allocation_selected"] = True
                opp_meta["sizing_mode"] = mode
                opp_meta["sizing_path"] = "fee_aware_rotation"
                opp_meta["sizing_cash_used"] = str(open_cash)
                opp_meta["sizing_cash_factor"] = str(cf)
                opp_meta["sizing_final_capital_required"] = str(open_notional)
                opp_meta["sizing_pre_mode_capital"] = str(opp.capital_required)
                opp_meta["sizing_final_action_capital"] = str(open_notional)
                opp_meta["rotation_replaced_symbol"] = weak.symbol
                opp_meta["rotation_advantage"] = str(advantage)
                opp_meta["rotation_required_advantage"] = str(required_advantage)
                opp_meta["rotation_estimated_round_trip_fee_bps"] = str(fee_bps)

                out.append(
                    CoordinatorAction(
                        kind="trim_symbol",
                        symbol=weak.symbol,
                        strategy_name="global_edge_rotation",
                        capital=weak.notional,
                        priority_score=opp.priority_score,
                        metadata=trim_meta,
                    )
                )
                out.append(
                    CoordinatorAction(
                        kind="open_strategy",
                        symbol=opp.symbol,
                        strategy_name=opp.strategy_name,
                        capital=open_notional,
                        priority_score=opp.priority_score,
                        metadata=opp_meta,
                    )
                )
                used_new.add(opp_norm)
                used_held.add(weak_norm)
                if replacement_context is not None:
                    ts = now.astimezone(timezone.utc)
                    replacement_context.last_event_at_by_symbol[weak_norm] = ts
                    replacement_context.last_event_at_by_symbol[opp_norm] = ts
                    replacement_context.recent_events.append(
                        {
                            "old": weak.symbol,
                            "new": opp.symbol,
                            "ts": ts.isoformat(),
                            "source": "global_edge_rotation",
                        }
                    )
                    if len(replacement_context.recent_events) > 50:
                        replacement_context.recent_events = replacement_context.recent_events[-50:]
                break
        return out

    def propose_flatten_actions(
        self,
        held: list[HeldPositionEdge],
        *,
        active_mode: str = DEFAULT_MODE,
        max_actions: int | None = None,
    ) -> list[CoordinatorAction]:
        """Emit reduce-only close intents when the operator sets allocation to zero.

        This is the capital-slider "flatten book" path. It deliberately emits
        normal ``trim_symbol`` coordinator actions so every close still flows
        through SignalEngine -> RiskEngine -> ExecutionEngine; it only changes
        the selection policy from "replace weak held edge with better new edge"
        to "close held exposure because the deployment ceiling is zero".
        """
        cap_n = max_actions if max_actions is not None else len(held)
        if cap_n <= 0:
            return []

        out: list[CoordinatorAction] = []
        # Close the largest exposures first, then weakest edge, so over-lev is
        # relieved quickly while remaining deterministic for tests/audits.
        ranked = sorted(
            held,
            key=lambda h: (h.notional, -h.expected_remaining_edge),
            reverse=True,
        )
        for h in ranked[:cap_n]:
            meta = dict(h.metadata or {})
            meta["coordinator_kind"] = "trim_symbol"
            meta["reduce_only"] = True
            meta["close_only"] = True
            meta["flatten_all"] = True
            meta["flatten_reason"] = "capital_allocation_zero"
            meta["force_market_order"] = True
            meta["target_notional"] = str(h.notional)
            meta["risk_notional_override"] = str(h.notional)
            if h.broker:
                meta["broker"] = h.broker
            out.append(
                CoordinatorAction(
                    kind="trim_symbol",
                    symbol=h.symbol,
                    strategy_name="global_edge_flatten",
                    capital=h.notional,
                    priority_score=Decimal("1"),
                    metadata=meta,
                )
            )
        return out

    def propose_session_exit_actions(
        self,
        held: list[HeldPositionEdge],
        *,
        active_mode: str = DEFAULT_MODE,
        now: datetime | None = None,
        max_actions: int | None = None,
    ) -> list[CoordinatorAction]:
        """Emit pre-close reduce-only actions from the session-exit policy.

        This is deliberately not a close-all-at-close rule. The policy reviews
        each held position's horizon / overnight permission / mode / P&L and
        only emits a reduce action when that position profile says it should
        not be carried, or should be defensively trimmed, through the session
        boundary.
        """
        if not held:
            return []
        try:
            from core.session_exit_policy import evaluate_session_exit
        except Exception:  # noqa: BLE001
            return []

        cap_n = max_actions if max_actions is not None else len(held)
        if cap_n <= 0:
            return []

        out: list[CoordinatorAction] = []
        for h in held:
            if len(out) >= cap_n:
                break
            md = dict(h.metadata or {})
            broker = str(h.broker or md.get("broker") or "").strip().lower()
            asset_class = str(md.get("asset_class") or "equity").strip().lower()
            try:
                qty = Decimal(str(md.get("quantity", "0") or "0"))
                px = Decimal(str(md.get("close") or md.get("price") or "0"))
                avg = Decimal(str(md.get("avg_entry_price") or md.get("entry_price") or px or "0"))
            except Exception:  # noqa: BLE001
                continue
            if qty == 0 or px <= 0:
                continue
            decision = evaluate_session_exit(
                broker=broker,
                asset_class=asset_class,
                symbol=h.symbol,
                quantity=qty,
                avg_entry_price=avg,
                current_price=px,
                strategy_name=h.strategy_name,
                profile_mode=active_mode,
                metadata=md,
                now=now,
            )
            if not decision.should_submit_order or decision.reduce_fraction <= 0:
                continue
            frac = min(Decimal("1"), max(Decimal("0"), decision.reduce_fraction))
            reduce_notional = (h.notional * frac).quantize(Decimal("0.00000001"))
            if reduce_notional <= 0:
                continue
            close_only = frac >= Decimal("0.999999")
            meta = dict(md)
            meta["coordinator_kind"] = "trim_symbol"
            meta["reduce_only"] = True
            meta["close_only"] = close_only
            meta["partial_reduce_only"] = not close_only
            meta["session_exit"] = True
            meta["session_exit_action"] = decision.action
            meta["session_exit_reason"] = decision.reason
            meta["session_exit_minutes_to_close"] = (
                None if decision.minutes_to_close is None else round(float(decision.minutes_to_close), 4)
            )
            meta["session_exit_reduce_fraction"] = str(frac)
            meta["target_notional"] = str(reduce_notional)
            meta["risk_notional_override"] = str(reduce_notional)
            if broker:
                meta["broker"] = broker
            out.append(
                CoordinatorAction(
                    kind="trim_symbol",
                    symbol=h.symbol,
                    strategy_name="session_exit_policy",
                    capital=reduce_notional,
                    priority_score=Decimal("0.99"),
                    metadata=meta,
                )
            )
        return out


def dedupe_opportunities_by_symbol(
    opportunities: list[StrategyOpportunity],
) -> tuple[list[StrategyOpportunity], list[tuple[StrategyOpportunity, StrategyOpportunity]]]:
    """When multiple strategies propose the same symbol, keep the highest ``priority_score``.

    Returns:
        (deduplicated list for the coordinator, (loser, winner) pairs for logging
        e.g. ``lost_to_strategy`` in ``strategy_candidate_log``).
    """
    if not opportunities:
        return [], []
    arbs: list[StrategyOpportunity] = []
    by_sym: dict[str, list[StrategyOpportunity]] = defaultdict(list)
    for o in opportunities:
        name = (getattr(o, "strategy_name", None) or "").strip()
        if name in _OPPORTUNITY_DEDUPE_EXCLUDE:
            arbs.append(o)
        else:
            by_sym[str(getattr(o, "symbol", "")).strip().upper()].append(o)
    out: list[StrategyOpportunity] = list(arbs)
    lost_to_winner: list[tuple[StrategyOpportunity, StrategyOpportunity]] = []
    for _sym, group in by_sym.items():
        if not group:
            continue
        if len(group) == 1:
            out.append(group[0])
            continue
        best = max(group, key=lambda x: x.priority_score)
        out.append(best)
        for o in group:
            if o is not best:
                lost_to_winner.append((o, best))
    return out, lost_to_winner


def held_positions_from_portfolio(
    portfolio: dict[str, Any],
    *,
    decay: Decimal = Decimal("0.08"),
    nav: Decimal | None = None,
    max_position_pct: Decimal = Decimal("0.10"),
    oversize_flag_ratio: Decimal = Decimal("1.25"),
) -> list[HeldPositionEdge]:
    """
    Build held edges from portfolio ``positions``; ``expected_remaining_edge`` is a proxy (v1).

    **D031D — oversized position detection.** When ``nav`` is supplied, each
    held position is compared against the intended ceiling
    ``nav * max_position_pct`` (default 10 %). Positions whose live notional
    exceeds the ceiling by more than ``oversize_flag_ratio`` (default 1.25×)
    are flagged in ``metadata`` with:

      * ``oversized_position_flag`` — ``True``
      * ``position_above_target_ratio`` — live notional / ceiling

    This is **detection only** — no auto-liquidation is performed here. The
    dashboard and monitoring layer surface the flag so operators (or a later
    remediation step) can trim. See docs/DECISIONS.md D031 for rationale.
    """
    pos = portfolio.get("positions") or {}
    if not isinstance(pos, dict):
        return []
    out: list[HeldPositionEdge] = []
    ceiling: Decimal | None = None
    if nav is not None and nav > 0 and max_position_pct > 0:
        ceiling = nav * max_position_pct
    for sym, row in pos.items():
        if not isinstance(row, dict):
            continue
        actual_symbol = str(row.get("symbol") or sym)
        try:
            qty = Decimal(str(row.get("quantity", "0")))
            px = Decimal(str(row.get("current_price", "0")))
        except Exception:  # noqa: BLE001
            continue
        n = abs(qty) * px
        if n <= 0:
            continue
        side = str(row.get("side") or ("short" if qty < 0 else "long")).strip().lower()
        # ── Live held-edge proxy (replaces the old constant 0.15·(1−decay)) ──
        # The old code gave EVERY holding the same 0.138 remaining edge, so a
        # new opportunity had to clear 0.138 + threshold + fees to displace
        # anything — structurally unwinnable, the book locked solid once full.
        #
        # Remaining edge now reflects how the position is actually doing:
        #   * a small residual "option to keep holding" floor,
        #   * plus a momentum term from the position's own unrealised return
        #     (let winners run — a position working in our favour keeps edge),
        #   * minus for losers/flat (cut them — they're the cheapest to shed).
        # Scale is matched to typical opportunity priority_scores (~0.05–0.25)
        # so rotation/displacement maths is winnable for genuinely better
        # ideas while a strong winner still resists being churned out.
        try:
            avg = Decimal(str(row.get("avg_entry_price", "0") or "0"))
        except Exception:  # noqa: BLE001
            avg = Decimal("0")
        unrl_ret = Decimal("0")
        if avg > 0 and px > 0:
            raw_ret = (px - avg) / avg
            unrl_ret = -raw_ret if side == "short" else raw_ret
        core_hold = Decimal("0.04")
        # k=1.5 → a +8% winner adds ≈ +0.12; clamp asymmetric: winners can
        # build edge up to +0.12, losers bleed it down to −0.06 (→ floored 0).
        momentum = unrl_ret * Decimal("1.5")
        if momentum > Decimal("0.12"):
            momentum = Decimal("0.12")
        elif momentum < Decimal("-0.06"):
            momentum = Decimal("-0.06")
        rem = max(Decimal("0"), (core_hold + momentum) * (Decimal("1") - decay))
        oversize_cap = Decimal("0.15")
        meta: dict[str, Any] = {
            "source": "portfolio_snapshot",
            "quantity": str(qty),
            "close": str(px),
            "price": str(px),
            "avg_entry_price": str(avg),
            "side": side,
            "asset_class": str(row.get("asset_class", "equity") or "equity"),
            "unrealised_return": str(unrl_ret.quantize(Decimal("0.000001"))),
            "held_edge_basis": "live_unrealised_momentum_v2",
        }
        if actual_symbol != str(sym):
            meta["position_key"] = str(sym)
        if ceiling is not None and ceiling > 0:
            ratio = (n / ceiling).quantize(Decimal("0.0001"))
            meta["position_above_target_ratio"] = str(ratio)
            meta["sizing_hard_cap_notional"] = str(ceiling)
            meta["oversized_position_flag"] = ratio > oversize_flag_ratio
            if ratio > Decimal("1"):
                oversize_penalty = min(oversize_cap, (ratio - Decimal("1")) * Decimal("0.05"))
                rem = max(Decimal("0"), rem - oversize_penalty)
        out.append(
            HeldPositionEdge(
                symbol=actual_symbol,
                notional=n,
                expected_remaining_edge=rem,
                broker=str(row.get("broker", "")),
                metadata=meta,
            )
        )
    return out
