"""
execution/wave9_runtime.py
============================
Wave 9 — runtime hook for the cost-aware execution layer.

Bridge between ``execution/engine.py`` and the Wave 9 cost / urgency /
slicing modules. Defaults to disabled — when ``execution_models.enabled``
in ``config/execution_models.yaml`` is False, ``pre_flight_cost_gate``
returns ``CostGateDecision(allow=True, used=False)`` and the engine
proceeds unmodified.

When enabled, the gate computes the expected execution cost for the
candidate order and runs the urgency policy. ``urgency=DO_NOT_TRADE``
short-circuits the order; every other urgency stamps diagnostic
metadata on the order so the dashboard can render the funnel.

The runtime is *intentionally* shallow — it does not yet construct
sliced child orders or change the venue. The full integration (cost-
aware ranking in router.py, urgency-driven order shape in engine.py,
real child slicing) is a follow-up wired off the same config flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from execution.impact import (
    CostBreakdown,
    DEFAULT_IMPACT_COEFFICIENTS,
    total_execution_cost_bps,
)
from execution.scheduler import (
    Urgency,
    UrgencyPolicy,
    decide_urgency,
)
from execution.slippage_model import SlippageModel
from execution.venue_quality import VenuePriors

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/execution_models.yaml")


# ── config ─────────────────────────────────────────────────────────────────


@dataclass
class DynamicSafetyConfig:
    """Anchors + weights for the regime-aware edge/cost safety cushion.

    The previous design pinned ``edge_to_cost_safety`` to a frozen
    ``2.0`` (with a single high-fee override to ``1.3``). This violated
    the project rule that no operational threshold may be an absolute
    constant. The new design treats safety as a *break-even multiple*
    that scales with regime risk and volatility:

        safety = base_anchor
               * (1 + risk_off_weight * (1 - market_state_score))
               * (1 + vol_weight * max(0, vol_scalar - 1))
               * (1 + high_fee_lift * max(0, (fee_bps - fee_anchor) / fee_anchor))

    Clamped to ``[safety_min, safety_max]``. ``base_anchor`` is the
    break-even cushion at neutral regime (mss=1, vol=1, low-fee venue):
    1.0 means "cost must equal edge"; 2.0 means "edge must be twice
    cost". Default 1.2 (a 20% cushion over break-even) is the
    project's calibration anchor — gentle but not negative-EV-friendly.

    ``high_fee_lift`` raises the cushion on expensive venues
    (replacement for the static high_fee override). Above the anchor
    the cushion grows linearly with the *fee ratio*, not a binary flip.
    """

    base_anchor: float = 1.2
    risk_off_weight: float = 0.8
    vol_weight: float = 0.5
    fee_anchor_bps: float = 5.0
    high_fee_lift: float = 0.0  # set >0 to *increase* cushion on high-fee venues; 0 = no lift
    safety_min: float = 0.8
    safety_max: float = 2.5


@dataclass
class Wave9RuntimeConfig:
    enabled: bool = False
    impact_coefficients: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_IMPACT_COEFFICIENTS)
    )
    urgency_policy: UrgencyPolicy = field(default_factory=UrgencyPolicy)
    venue_priors: VenuePriors = field(default_factory=VenuePriors)
    slippage_model: SlippageModel = field(default_factory=SlippageModel)
    unknown_liquidity_penalty_bps: float = 5.0
    dynamic_safety: DynamicSafetyConfig = field(default_factory=DynamicSafetyConfig)
    # ``urgency_policy.edge_to_cost_safety`` is now ignored at runtime
    # for the gate's central decision — the dynamic resolver computes
    # safety per-candidate. The field is still parsed for backward
    # compatibility (and used as ``base_anchor`` fallback when the
    # ``dynamic_safety`` block is absent).
    high_fee_threshold_bps: float = 25.0  # deprecated; retained for legacy YAML

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "Wave9RuntimeConfig":
        if not raw:
            return cls()
        sect = raw.get("execution_models") if "execution_models" in raw else raw
        sect = dict(sect or {})

        coefs = dict(DEFAULT_IMPACT_COEFFICIENTS)
        impact_block = sect.get("impact") or {}
        for k, v in (impact_block.get("coefficients") or {}).items():
            try:
                coefs[str(k).lower()] = float(v)
            except (TypeError, ValueError):
                continue

        up_block = sect.get("urgency_policy") or {}
        policy = UrgencyPolicy(
            market_cost_ceiling=float(up_block.get("market_cost_ceiling", 8.0)),
            limit_cost_ceiling=float(up_block.get("limit_cost_ceiling", 25.0)),
            passive_cost_ceiling=float(up_block.get("passive_cost_ceiling", 60.0)),
            do_not_trade_ceiling=float(up_block.get("do_not_trade_ceiling", 150.0)),
            edge_to_cost_safety=float(up_block.get("edge_to_cost_safety", 1.0)),
            high_urgency_multiplier=float(up_block.get("high_urgency_multiplier", 1.5)),
            high_urgency_threshold=float(up_block.get("high_urgency_threshold", 0.8)),
        )

        priors = VenuePriors.from_dict({"venue_priors": sect.get("venue_priors")})

        slip_block = sect.get("slippage") or {}
        slip = SlippageModel(
            decay_specific=float(slip_block.get("decay_specific", 0.10)),
            decay_group=float(slip_block.get("decay_group", 0.04)),
            decay_global=float(slip_block.get("decay_global", 0.01)),
            default_bps=float(slip_block.get("default_bps", 5.0)),
            min_samples_specific=int(slip_block.get("min_samples_specific", 10)),
        )
        priors.slippage = slip

        # Dynamic safety block. When absent, fall back to a sensible
        # base_anchor derived from the legacy urgency_policy field
        # (preserves backward compat for older YAML files).
        ds_block = sect.get("dynamic_safety") or {}
        ds_base = float(ds_block.get("base_anchor", policy.edge_to_cost_safety or 1.2))
        ds = DynamicSafetyConfig(
            base_anchor=ds_base,
            risk_off_weight=float(ds_block.get("risk_off_weight", 0.8)),
            vol_weight=float(ds_block.get("vol_weight", 0.5)),
            fee_anchor_bps=float(ds_block.get("fee_anchor_bps", 5.0)),
            high_fee_lift=float(ds_block.get("high_fee_lift", 0.0)),
            safety_min=float(ds_block.get("safety_min", 0.8)),
            safety_max=float(ds_block.get("safety_max", 2.5)),
        )

        return cls(
            enabled=bool(sect.get("enabled", False)),
            impact_coefficients=coefs,
            urgency_policy=policy,
            venue_priors=priors,
            slippage_model=slip,
            unknown_liquidity_penalty_bps=float(sect.get("unknown_liquidity_penalty_bps", 5.0)),
            dynamic_safety=ds,
            high_fee_threshold_bps=float(sect.get("high_fee_threshold_bps", 25.0)),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "Wave9RuntimeConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── decision ───────────────────────────────────────────────────────────────


@dataclass
class CostGateDecision:
    """One pre-flight verdict from the Wave 9 gate."""

    allow: bool
    used: bool  # False ⇒ gate is disabled or input incomplete; allow defaults True
    reason: str
    urgency: Optional[Urgency] = None
    expected_cost_bps: float = 0.0
    edge_bps: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── helpers ────────────────────────────────────────────────────────────────


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _extract_signal_inputs(
    signal_metadata: Mapping[str, Any],
    *,
    fee_bps: float = 0.0,
) -> dict[str, float]:
    """Pull the inputs Wave 9 needs out of ``Signal.metadata``.

    When a calibrated forecast return is present we use it directly. Absent
    that, ``expected_edge`` / ``priority_score`` are bounded conviction
    scores (typically ~0.05–0.30 for live-generated signals), NOT calibrated
    returns. The previous proxy capped at ``max(25, 2·fee_bps)`` and scaled
    linearly, so a normal signal (score ≈ 0.15) mapped to only ≈ 3–6 bps —
    permanently below the ≈ 11–18 bps round-trip cost. The edge/cost cushion
    in :func:`decide_urgency` then vetoed *every* trade with
    ``cost_exceeds_edge`` once the book was full, locking the system solid.

    The proxy ceiling is now ``max(200, 4·fee_bps)`` bps. Linear mapping is
    kept (score is monotone in conviction), so:

      * a weak signal (score ≈ 0.03) → ≈ 6 bps  → still vetoed vs ~12 bps cost
        (the gate still filters genuinely thin edges — its real job),
      * a decent signal (score ≈ 0.15) → ≈ 30 bps → clears typical equity/FX
        cost so the system actually trades,
      * a strong signal (score ≈ 0.5) → 100 bps → ample headroom,
      * a high-fee venue (Kraken ~26 bps fee → cap 200, ~40 bps cost) still
        needs proportionally stronger conviction — uneconomic churn stays out.

    This re-scales an uncalibrated proxy to the cost's bps scale; it does not
    disable the cushion. Genuinely negative-EV trades are still refused.
    """
    md = signal_metadata or {}
    edge_bps = _safe_float(md.get("forecast_expected_return"), 0.0) * 10_000.0
    if edge_bps <= 0:
        edge_bps = _safe_float(md.get("expected_edge_bps"), 0.0)
    if edge_bps <= 0:
        score_proxy = max(
            _safe_float(md.get("expected_edge"), 0.0),
            _safe_float(md.get("priority_score"), 0.0),
        )
        # Venue-aware proxy ceiling matched to real round-trip cost scale.
        proxy_cap_bps = max(200.0, 4.0 * max(0.0, float(fee_bps)))
        edge_bps = max(0.0, min(proxy_cap_bps, score_proxy * proxy_cap_bps))
    return {
        "daily_volume": _safe_float(
            md.get("daily_volume") or md.get("avg_daily_volume") or md.get("volume"),
            0.0,
        ),
        "daily_volatility": _safe_float(
            md.get("daily_volatility") or md.get("realised_vol") or md.get("atr_pct"),
            0.0,
        ),
        "edge_bps": edge_bps,
        "signal_urgency": _safe_float(md.get("urgency_score") or md.get("opportunity_urgency"), 0.5),
        "demand_alignment": _safe_float(md.get("demand_alignment"), 0.0),
        "regime_label": md.get("regime_label") or md.get("market_state_label"),
        # Inputs to the dynamic safety cushion.
        "market_state_score": _safe_float(md.get("market_state_score"), 1.0),
        "market_volatility_scalar": _safe_float(md.get("market_volatility_scalar"), 1.0),
    }


# ── public API ─────────────────────────────────────────────────────────────


def pre_flight_cost_gate(
    *,
    config: Wave9RuntimeConfig,
    broker: str,
    symbol: str,
    asset_class: str,
    quantity: float,
    signal_metadata: Optional[Mapping[str, Any]] = None,
) -> CostGateDecision:
    """
    Compute the all-in expected cost and the urgency verdict.

    Returns ``CostGateDecision`` with ``allow=False`` only when the
    urgency policy resolves to ``DO_NOT_TRADE``. Every other urgency
    is allowed; the engine should record the metadata on the order.
    """
    if not config.enabled:
        return CostGateDecision(
            allow=True,
            used=False,
            reason="disabled",
        )

    try:
        ac = (asset_class or "other").strip().lower()
        coef = config.impact_coefficients.get(ac, config.impact_coefficients.get("other", 0.10))
        fee_bps = config.venue_priors.fee_for(broker, taker=True)
        spread_bps = config.venue_priors.spread_for(broker, ac)
        meta_in = _extract_signal_inputs(signal_metadata or {}, fee_bps=fee_bps)
        slip_est = config.slippage_model.estimate(
            broker=broker, symbol=symbol, asset_class=ac
        )
        cost = total_execution_cost_bps(
            order_qty=float(quantity),
            daily_volume=meta_in["daily_volume"],
            daily_volatility=meta_in["daily_volatility"],
            asset_class=ac,
            fee_bps=fee_bps,
            spread_bps=spread_bps,
            slippage_bps=slip_est.bps,
            coefficient=coef,
        )
        unknown_liquidity_penalty = (
            max(0.0, float(config.unknown_liquidity_penalty_bps or 0.0))
            if meta_in["daily_volume"] <= 0
            else 0.0
        )
        if unknown_liquidity_penalty:
            cost = CostBreakdown(
                fee_bps=cost.fee_bps,
                spread_bps=cost.spread_bps,
                slippage_bps=cost.slippage_bps,
                impact_bps=cost.impact_bps + unknown_liquidity_penalty,
            )

        regime_label = meta_in["regime_label"]
        regime_str = str(regime_label) if regime_label is not None else None

        # Dynamic edge/cost cushion (replaces the legacy static
        # ``edge_to_cost_safety = 2.0`` + binary high-fee override). The
        # cushion scales with regime risk, volatility, and venue cost,
        # all continuous — see DynamicSafetyConfig.
        ds = config.dynamic_safety
        mss = _safe_float(meta_in.get("market_state_score"), 1.0)
        vol = _safe_float(meta_in.get("market_volatility_scalar"), 1.0)
        risk_off_lift = ds.risk_off_weight * max(0.0, 1.0 - mss)
        vol_lift = ds.vol_weight * max(0.0, vol - 1.0)
        fee_lift = 0.0
        if ds.high_fee_lift > 0 and ds.fee_anchor_bps > 0 and fee_bps > ds.fee_anchor_bps:
            fee_lift = ds.high_fee_lift * ((fee_bps - ds.fee_anchor_bps) / ds.fee_anchor_bps)
        dyn_safety = ds.base_anchor * (1.0 + risk_off_lift) * (1.0 + vol_lift) * (1.0 + fee_lift)
        dyn_safety = max(ds.safety_min, min(ds.safety_max, dyn_safety))

        from dataclasses import replace as _dc_replace
        effective_policy = _dc_replace(
            config.urgency_policy,
            edge_to_cost_safety=dyn_safety,
        )

        verdict = decide_urgency(
            expected_cost_bps=cost.total_bps,
            edge_bps=meta_in["edge_bps"],
            signal_urgency=meta_in["signal_urgency"],
            demand_alignment=meta_in["demand_alignment"],
            regime_label=regime_str,
            policy=effective_policy,
        )

        breakdown = {
            "fee_bps": cost.fee_bps,
            "spread_bps": cost.spread_bps,
            "slippage_bps": cost.slippage_bps,
            "impact_bps": cost.impact_bps,
            "unknown_liquidity_penalty_bps": unknown_liquidity_penalty,
            "total_bps": cost.total_bps,
        }
        meta_out = {
            "wave9_urgency": verdict.urgency.value,
            "wave9_reason": verdict.reason,
            "wave9_expected_cost_bps": cost.total_bps,
            "wave9_edge_bps": meta_in["edge_bps"],
            "wave9_slippage_source": slip_est.source,
            "wave9_liquidity_known": meta_in["daily_volume"] > 0,
            "wave9_broker": broker,
            "wave9_asset_class": ac,
            "wave9_edge_to_cost_safety_applied": effective_policy.edge_to_cost_safety,
            "wave9_safety_base_anchor": ds.base_anchor,
            "wave9_safety_risk_off_lift": risk_off_lift,
            "wave9_safety_vol_lift": vol_lift,
            "wave9_safety_fee_lift": fee_lift,
            "wave9_market_state_score": mss,
            "wave9_market_volatility_scalar": vol,
            "wave9_fee_bps": fee_bps,
        }

        if verdict.urgency is Urgency.DO_NOT_TRADE:
            logger.warning(
                "WAVE9 GATE | DO_NOT_TRADE | %s %s qty=%s cost=%.2fbps reason=%s",
                symbol, broker, quantity, cost.total_bps, verdict.reason,
            )
            return CostGateDecision(
                allow=False,
                used=True,
                reason=verdict.reason,
                urgency=verdict.urgency,
                expected_cost_bps=cost.total_bps,
                edge_bps=meta_in["edge_bps"],
                cost_breakdown=breakdown,
                metadata=meta_out,
            )

        return CostGateDecision(
            allow=True,
            used=True,
            reason=verdict.reason,
            urgency=verdict.urgency,
            expected_cost_bps=cost.total_bps,
            edge_bps=meta_in["edge_bps"],
            cost_breakdown=breakdown,
            metadata=meta_out,
        )
    except Exception as exc:  # noqa: BLE001
        # Defensive — gate failure must never block live execution.
        logger.warning("wave9_runtime | pre_flight_cost_gate failed: %s — allowing", exc)
        return CostGateDecision(
            allow=True,
            used=False,
            reason=f"gate_error:{exc.__class__.__name__}",
            metadata={"wave9_error": str(exc)},
        )
