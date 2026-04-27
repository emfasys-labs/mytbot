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
class Wave9RuntimeConfig:
    enabled: bool = False
    impact_coefficients: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_IMPACT_COEFFICIENTS)
    )
    urgency_policy: UrgencyPolicy = field(default_factory=UrgencyPolicy)
    venue_priors: VenuePriors = field(default_factory=VenuePriors)
    slippage_model: SlippageModel = field(default_factory=SlippageModel)

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

        return cls(
            enabled=bool(sect.get("enabled", False)),
            impact_coefficients=coefs,
            urgency_policy=policy,
            venue_priors=priors,
            slippage_model=slip,
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


def _extract_signal_inputs(signal_metadata: Mapping[str, Any]) -> dict[str, float]:
    """Pull the inputs Wave 9 needs out of ``Signal.metadata``."""
    md = signal_metadata or {}
    return {
        "daily_volume": _safe_float(
            md.get("daily_volume") or md.get("avg_daily_volume") or md.get("volume"),
            0.0,
        ),
        "daily_volatility": _safe_float(
            md.get("daily_volatility") or md.get("realised_vol") or md.get("atr_pct"),
            0.0,
        ),
        "edge_bps": _safe_float(md.get("forecast_expected_return"), 0.0) * 10_000.0,
        "signal_urgency": _safe_float(md.get("urgency_score") or md.get("opportunity_urgency"), 0.5),
        "demand_alignment": _safe_float(md.get("demand_alignment"), 0.0),
        "regime_label": md.get("regime_label") or md.get("market_state_label"),
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
        meta_in = _extract_signal_inputs(signal_metadata or {})
        ac = (asset_class or "other").strip().lower()
        coef = config.impact_coefficients.get(ac, config.impact_coefficients.get("other", 0.10))
        fee_bps = config.venue_priors.fee_for(broker, taker=True)
        spread_bps = config.venue_priors.spread_for(broker, ac)
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

        regime_label = meta_in["regime_label"]
        regime_str = str(regime_label) if regime_label is not None else None

        verdict = decide_urgency(
            expected_cost_bps=cost.total_bps,
            edge_bps=meta_in["edge_bps"],
            signal_urgency=meta_in["signal_urgency"],
            demand_alignment=meta_in["demand_alignment"],
            regime_label=regime_str,
            policy=config.urgency_policy,
        )

        breakdown = {
            "fee_bps": cost.fee_bps,
            "spread_bps": cost.spread_bps,
            "slippage_bps": cost.slippage_bps,
            "impact_bps": cost.impact_bps,
            "total_bps": cost.total_bps,
        }
        meta_out = {
            "wave9_urgency": verdict.urgency.value,
            "wave9_reason": verdict.reason,
            "wave9_expected_cost_bps": cost.total_bps,
            "wave9_edge_bps": meta_in["edge_bps"],
            "wave9_slippage_source": slip_est.source,
            "wave9_broker": broker,
            "wave9_asset_class": ac,
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
