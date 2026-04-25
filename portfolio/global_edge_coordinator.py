"""
Global opportunity ranking: new opportunities vs held positions (expected remaining edge).
Emits incremental actions only; does not bypass risk or execution.
"""

from __future__ import annotations

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

    liq = Decimal("0.7")
    exe = Decimal("0.75")
    reg = Decimal("0.8")
    try:
        demand_score = float(cand_meta.get("demand_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        demand_score = 0.0
    side_txt = str(getattr(cand, "side", "long")).strip().lower()
    side_sign = 1.0 if side_txt in ("long", "buy") else -1.0
    align = max(-1.0, min(1.0, demand_score * side_sign))
    reg = Decimal(str(max(0.55, min(0.95, 0.8 + align * 0.12))))
    cand_meta["demand_alignment"] = round(align, 6)
    risk = Decimal("0.05")
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
        """Rank new opportunities vs weakest held edge and emit replacement actions."""
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

        for opp in ranked:
            if len(out) >= cap_n:
                break
            if opp.expected_edge <= weakest_edge + thresh:
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
        meta: dict[str, Any] = {
            "source": "portfolio_snapshot",
            "quantity": str(qty),
            "close": str(px),
            "price": str(px),
            "side": str(row.get("side", "long") or "long"),
            "asset_class": str(row.get("asset_class", "equity") or "equity"),
        }
        if ceiling is not None and ceiling > 0:
            ratio = (n / ceiling).quantize(Decimal("0.0001"))
            meta["position_above_target_ratio"] = str(ratio)
            meta["sizing_hard_cap_notional"] = str(ceiling)
            meta["oversized_position_flag"] = ratio > oversize_flag_ratio
        out.append(
            HeldPositionEdge(
                symbol=str(sym),
                notional=n,
                expected_remaining_edge=rem,
                broker=str(row.get("broker", "")),
                metadata=meta,
            )
        )
    return out
