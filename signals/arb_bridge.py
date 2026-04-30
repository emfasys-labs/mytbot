"""
Convert global edge ``CoordinatorAction`` and ``StrategyOpportunity`` into ``RawSignal`` / processed ``Signal``.
Risk engine and execution paths stay unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from portfolio.global_edge_coordinator import CoordinatorAction
from portfolio.strategy_opportunity import StrategyOpportunity
from signals.engine import RawSignal, Signal, SignalEngine


def strategy_opportunity_to_raw_signal(opp: StrategyOpportunity, *, nav: Decimal) -> RawSignal:
    """Map coordinator opportunity to a raw signal (directional or placeholder side)."""
    side = opp.side
    if not side.startswith("ARBITRAGE_"):
        side = "buy" if side == "long" else "sell"
    broker = (opp.metadata or {}).get("broker") or (opp.metadata or {}).get("spot_venue") or (opp.metadata or {}).get("buy_venue") or "ibkr"
    md = dict(opp.metadata)
    md.setdefault("target_notional", str(opp.capital_required))
    md.setdefault("risk_notional_override", str(min(opp.capital_required, nav * Decimal("0.15"))))
    md["expected_edge"] = str(opp.expected_edge)
    md["priority_score"] = str(opp.priority_score)
    return RawSignal(
        strategy=opp.strategy_name,
        symbol=opp.symbol,
        side=side,
        confidence=float(opp.confidence),
        broker=str(broker)[:32],
        asset_class="crypto" if "ARBITRAGE" in opp.side or opp.strategy_name.endswith("arbitrage") else "equity",
        metadata=md,
    )


def coordinator_action_to_raw_signal(action: CoordinatorAction, *, nav: Decimal) -> RawSignal:
    """Build ``RawSignal`` from a coordinator incremental action.

    D031 note: ``target_notional`` / ``risk_notional_override`` here always
    equal the coordinator's ``action.capital`` because that is the final
    intended deployment after (1) strategy-requested sizing and (2) mode /
    hard-cap adjustments. The full audit trail (``sizing_source``,
    ``sizing_strategy_target_notional``, ``sizing_clipped`` etc.) was
    populated upstream in ``signal_candidate_to_strategy_opportunity`` /
    ``propose_actions`` and is passed through via ``dict(action.metadata)``
    so downstream execution guards and the dashboard can inspect it.
    """
    md = dict(action.metadata)
    md["coordinator_kind"] = action.kind
    md["target_notional"] = str(action.capital)
    md["risk_notional_override"] = str(action.capital)
    md["priority_score"] = str(action.priority_score)
    # Execution-boundary guard (D031C) reads this to compare against the
    # actual order notional about to be placed.
    md.setdefault("sizing_final_capital_required", str(action.capital))

    if action.kind == "trim_symbol":
        md["reduce_only"] = True
        if not bool(md.get("partial_reduce_only")):
            md["close_only"] = True
        return RawSignal(
            strategy=action.strategy_name,
            symbol=action.symbol,
            side="sell" if md.get("side", "long") != "short" else "buy",
            confidence=min(0.95, max(0.55, float(md.get("confidence", 0.85)))),
            broker=str(md.get("broker", "ibkr")),
            asset_class=str(md.get("asset_class", "equity")),
            metadata=md,
        )

    if action.strategy_name == "funding_rate_arbitrage":
        return RawSignal(
            strategy=action.strategy_name,
            symbol=action.symbol,
            side="ARBITRAGE_LONG_SPOT_SHORT_PERP",
            confidence=min(0.95, max(0.5, float(md.get("confidence", 0.8)))),
            broker=str(md.get("spot_venue", "unknown")),
            asset_class="crypto",
            metadata={
                **md,
                "perp_venue": md.get("perp_venue", ""),
                "spot_venue": md.get("spot_venue", ""),
                "annualised_net_yield": md.get("annualised_net_yield", "0"),
                "basis_bps": md.get("basis_bps", "0"),
            },
        )

    if action.strategy_name == "cross_exchange_arbitrage":
        return RawSignal(
            strategy=action.strategy_name,
            symbol=action.symbol,
            side="ARBITRAGE_SPOT_SPREAD",
            confidence=min(0.95, max(0.5, float(md.get("confidence", 0.75)))),
            broker=str(md.get("buy_venue", "unknown")),
            asset_class="crypto",
            metadata={
                **md,
                "buy_venue": md.get("buy_venue", ""),
                "sell_venue": md.get("sell_venue", ""),
            },
        )

    return RawSignal(
        strategy=action.strategy_name,
        symbol=action.symbol,
        side="buy" if md.get("side") != "short" else "sell",
        confidence=min(0.95, max(0.3, float(md.get("confidence", 0.6)))),
        broker=str(md.get("broker", "ibkr")),
        asset_class=str(md.get("asset_class", "equity")),
        metadata=md,
    )


def process_coordinator_action(
    action: CoordinatorAction,
    sig_engine: SignalEngine,
    *,
    portfolio_value: Decimal,
    news_score: Optional[float] = None,
) -> Optional[Signal]:
    raw = coordinator_action_to_raw_signal(action, nav=portfolio_value)
    return sig_engine.process(raw, portfolio_value=portfolio_value, news_score=news_score)


def process_strategy_opportunity(
    opp: StrategyOpportunity,
    sig_engine: SignalEngine,
    *,
    portfolio_value: Decimal,
    news_score: Optional[float] = None,
) -> Optional[Signal]:
    raw = strategy_opportunity_to_raw_signal(opp, nav=portfolio_value)
    return sig_engine.process(raw, portfolio_value=portfolio_value, news_score=news_score)
