"""
Global opportunity ranking: new opportunities vs held positions (expected remaining edge).
Emits incremental actions only; does not bypass risk or execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from portfolio.d015_replacement_context import ReplacementContext, churn_penalty_for_pair
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score

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


def signal_candidate_to_strategy_opportunity(
    cand: Any,
    *,
    nav: Decimal,
    position_pct: Decimal,
    price: Decimal,
) -> StrategyOpportunity | None:
    """Map a D015 ``SignalCandidate`` to comparable ``StrategyOpportunity`` (edge ~ adjusted strength)."""
    try:
        edge = Decimal(str(getattr(cand, "adjusted_signal_strength", "0") or "0"))
        conf = Decimal(str(getattr(cand, "confidence", "0") or "0"))
    except Exception:  # noqa: BLE001
        return None
    if price <= 0 or nav <= 0:
        return None
    cap = nav * position_pct
    if cap <= 0:
        return None
    liq = Decimal("0.7")
    exe = Decimal("0.75")
    reg = Decimal("0.8")
    risk = Decimal("0.05")
    ps = compute_priority_score(edge, conf, reg, exe, risk)
    # Carry the candidate's asset_class into metadata so it survives the
    # SignalCandidate → StrategyOpportunity → CoordinatorAction → RawSignal
    # round-trip. Without this, ``coordinator_action_to_raw_signal`` defaults
    # to "equity" and mislabels crypto / forex / futures signals on the way
    # back to the execution engine (which then routes to the wrong broker).
    cand_meta = dict(getattr(cand, "metadata", {}) or {})
    cand_ac = getattr(cand, "asset_class", None)
    if cand_ac and "asset_class" not in cand_meta:
        cand_meta["asset_class"] = str(cand_ac)
    return StrategyOpportunity(
        strategy_name=str(getattr(cand, "strategy_name", "unknown")),
        symbol=str(getattr(cand, "symbol", "")),
        side=str(getattr(cand, "side", "long")),
        created_at=getattr(cand, "timestamp", datetime.now(timezone.utc)),
        expected_edge=edge,
        confidence=conf,
        capital_required=cap,
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
    ps = compute_priority_score(edge, conf, Decimal("0.85"), Decimal("0.9"), Decimal("0.04"))
    meta = {
        "arbitrage_kind": "funding_rate",
        "spot_venue": sig.spot_venue,
        "perp_venue": sig.perp_venue,
        "annualised_net_yield": str(ann),
        "basis_bps": str(sig.basis_bps),
        **dict(sig.metadata or {}),
    }
    return StrategyOpportunity(
        strategy_name=sig.strategy_name,
        symbol=sig.symbol,
        side=sig.side,
        created_at=sig.created_at,
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=sig.expected_hold_hours,
        liquidity_score=Decimal("0.9"),
        execution_score=Decimal("0.85"),
        regime_fit_score=Decimal("0.9"),
        risk_cost_score=Decimal("0.03"),
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
    ps = compute_priority_score(edge, conf, Decimal("0.85"), Decimal("0.9"), Decimal("0.04"))
    snap = getattr(opp, "snapshot", None)
    meta = {
        "arbitrage_kind": "funding_rate",
        "spot_venue": getattr(opp, "spot_venue", ""),
        "perp_venue": getattr(opp, "perp_venue", ""),
        "annualised_net_yield": str(ann),
    }
    return StrategyOpportunity(
        strategy_name="funding_rate_arbitrage",
        symbol=str(getattr(opp, "symbol", "")),
        side="ARBITRAGE_LONG_SPOT_SHORT_PERP",
        created_at=datetime.now(timezone.utc),
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=int(getattr(opp, "expected_hold_hours", 72)),
        liquidity_score=Decimal("0.9"),
        execution_score=Decimal("0.85"),
        regime_fit_score=Decimal("0.9"),
        risk_cost_score=Decimal("0.03"),
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
    ps = compute_priority_score(edge, conf, Decimal("0.85"), Decimal("0.7"), Decimal("0.06"))
    return StrategyOpportunity(
        strategy_name="cross_exchange_arbitrage",
        symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "ARBITRAGE_SPOT_SPREAD")),
        created_at=datetime.now(timezone.utc),
        expected_edge=edge,
        confidence=conf,
        capital_required=capital,
        expected_holding_hours=1,
        liquidity_score=Decimal("0.75"),
        execution_score=Decimal("0.65"),
        regime_fit_score=Decimal("0.85"),
        risk_cost_score=Decimal("0.06"),
        priority_score=ps,
        metadata=meta,
    )


class GlobalEdgeCoordinator:
    def __init__(self, config: dict[str, Any], logger: Any | None = None) -> None:
        self._cfg = config
        self._logger = logger

    def _threshold(self, mode: str) -> Decimal:
        ea = self._cfg.get("edge_advantage") or {}
        key = (mode or DEFAULT_MODE).strip().lower()
        if key not in ("hunter", "trader", "defender"):
            key = DEFAULT_MODE
        return Decimal(str(ea.get(key, ea.get("trader", "0.05"))))

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
    ) -> list[CoordinatorAction]:
        """
        Rank new opportunities vs weakest held edge; emit open actions that clear the bar.
        Trim actions are not emitted in v1 (incremental unwind handled by D015 replacement separately).
        """
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
        out: list[CoordinatorAction] = []

        for opp in ranked:
            if len(out) >= cap_n:
                break
            if opp.expected_edge <= weakest_edge + thresh:
                continue
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

            frac = self._notional_fraction_for_mode(active_mode)
            # Hunter ``frac == 1.0`` → deploy the strategy-requested amount in
            # full; defender ``frac == 0.15`` → trim to 15% of request. The
            # ``min(1, frac)`` clamp prevents an accidental >100% blow-up if
            # ops configures e.g. ``1.5``.
            cap = opp.capital_required * min(Decimal("1"), frac) if opp.capital_required > 0 else Decimal("0")

            out.append(
                CoordinatorAction(
                    kind="open_strategy",
                    symbol=opp.symbol,
                    strategy_name=opp.strategy_name,
                    capital=cap if cap > 0 else opp.capital_required,
                    priority_score=opp.priority_score,
                    metadata=dict(opp.metadata),
                )
            )

        return out


def held_positions_from_portfolio(
    portfolio: dict[str, Any],
    *,
    decay: Decimal = Decimal("0.08"),
) -> list[HeldPositionEdge]:
    """
    Build held edges from portfolio ``positions``; expected_remaining_edge is a proxy (v1).
    """
    pos = portfolio.get("positions") or {}
    if not isinstance(pos, dict):
        return []
    out: list[HeldPositionEdge] = []
    for sym, row in pos.items():
        if not isinstance(row, dict):
            continue
        try:
            qty = Decimal(str(row.get("quantity", "0")))
            px = Decimal(str(row.get("current_price", "0")))
        except Exception:  # noqa: BLE001
            continue
        n = abs(qty) * px
        if n <= 0:
            continue
        base_edge = Decimal("0.15")
        rem = max(Decimal("0"), base_edge * (Decimal("1") - decay))
        out.append(
            HeldPositionEdge(
                symbol=str(sym),
                notional=n,
                expected_remaining_edge=rem,
                broker=str(row.get("broker", "")),
                metadata={"source": "portfolio_snapshot"},
            )
        )
    return out
