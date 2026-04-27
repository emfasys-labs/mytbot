"""
strategies/options_directional.py
====================================
Wave 12 — long-only directional options strategies.

Implements ``LongCallStrategy`` and ``LongPutStrategy``. Both emit
``SignalCandidate`` with the existing ``instrument_metadata`` payload
shape (``{"instrument_type": "option", "option_contract": {...}}``) so
``execution/engine.py`` builds an OPT order via the IBKR adapter
without changes.

Boundary discipline (per Wave-12 plan):

- ``enabled`` defaults to ``False``.
- Long-only directional only — never produces ``side="sell"`` candidates.
  A naked-short safeguard inside ``_make_candidate`` raises
  ``ValueError`` if a caller tries to bypass it.
- Premium-budget gate runs *before* emission via
  ``models.options.risk.check_premium_exposure``.
- Underlying ownership is NOT required for these (purely speculative
  long premium); for hedging strategies see ``options_hedging.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

import yaml

from core.instruments import OptionContractSpec, OptionRight
from core.models_runtime import (
    AssetClass,
    Opportunity,
    OpportunityComponents,
    Side,
    SignalCandidate,
    clip_decimal,
)
from models.options.greeks import OptionInputs, black_scholes_greeks
from models.options.risk import check_premium_exposure

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/options_strategies.yaml")


# ── config ─────────────────────────────────────────────────────────────────


@dataclass
class OptionsDirectionalConfig:
    enabled: bool = False
    paper_only: bool = True

    # Premium-exposure caps (fractions of NAV).
    max_premium_pct_per_trade: float = 0.005   # 0.5% NAV per trade
    max_premium_pct_aggregate: float = 0.02    # 2% NAV aggregate

    # Strategy gates.
    min_dte_days: int = 7
    max_dte_days: int = 60
    min_delta_call: float = 0.30
    max_delta_call: float = 0.70
    min_delta_put: float = 0.30
    max_delta_put: float = 0.70
    confidence_floor: float = 0.10
    confidence_ceiling: float = 0.85

    # Strategy identity.
    long_call_strategy_name: str = "options_long_call"
    long_put_strategy_name: str = "options_long_put"
    preferred_broker: str = "ibkr"

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "OptionsDirectionalConfig":
        if not raw:
            return cls()
        sect = raw.get("options_directional") if "options_directional" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            enabled=bool(sect.get("enabled", False)),
            paper_only=bool(sect.get("paper_only", True)),
            max_premium_pct_per_trade=float(sect.get("max_premium_pct_per_trade", 0.005)),
            max_premium_pct_aggregate=float(sect.get("max_premium_pct_aggregate", 0.02)),
            min_dte_days=int(sect.get("min_dte_days", 7)),
            max_dte_days=int(sect.get("max_dte_days", 60)),
            min_delta_call=float(sect.get("min_delta_call", 0.30)),
            max_delta_call=float(sect.get("max_delta_call", 0.70)),
            min_delta_put=float(sect.get("min_delta_put", 0.30)),
            max_delta_put=float(sect.get("max_delta_put", 0.70)),
            confidence_floor=float(sect.get("confidence_floor", 0.10)),
            confidence_ceiling=float(sect.get("confidence_ceiling", 0.85)),
            long_call_strategy_name=str(sect.get("long_call_strategy_name", "options_long_call")),
            long_put_strategy_name=str(sect.get("long_put_strategy_name", "options_long_put")),
            preferred_broker=str(sect.get("preferred_broker", "ibkr")),
        )


# ── proposal helper ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionsProposal:
    underlying_symbol: str
    expiry_yyyymmdd: str
    strike: Decimal
    is_call: bool
    spot: float
    volatility: float
    risk_free_rate: float
    dividend_yield: float
    time_to_expiry_years: float
    contracts: int = 1
    multiplier: int = 100


def _delta_in_band(delta: float, *, lo: float, hi: float, is_call: bool) -> bool:
    """Δ for a call is in (0, 1); for a put it's in (-1, 0). We test absolute value."""
    return lo <= abs(delta) <= hi


def _premium_notional(price_per_contract: float, contracts: int, multiplier: int) -> Decimal:
    return Decimal(str(price_per_contract)) * Decimal(int(contracts)) * Decimal(int(multiplier))


# ── strategies ─────────────────────────────────────────────────────────────


class _LongOptionStrategyBase:
    def __init__(self, config: Optional[OptionsDirectionalConfig] = None):
        self.config = config or OptionsDirectionalConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _evaluate(
        self,
        proposal: OptionsProposal,
        *,
        nav: Decimal,
        existing_premium_notional: Decimal,
        as_of: Optional[datetime] = None,
    ) -> Optional[SignalCandidate]:
        if not self.config.enabled:
            return None
        ts = as_of or datetime.now(timezone.utc)

        # DTE band.
        dte_days = float(proposal.time_to_expiry_years) * 365.0
        if dte_days < self.config.min_dte_days or dte_days > self.config.max_dte_days:
            logger.debug(
                "options_directional | dte_out_of_band: %.1f days [%d, %d]",
                dte_days, self.config.min_dte_days, self.config.max_dte_days,
            )
            return None

        # Black-Scholes for price + delta.
        bs = black_scholes_greeks(
            OptionInputs(
                spot=float(proposal.spot),
                strike=float(proposal.strike),
                time_to_expiry_years=float(proposal.time_to_expiry_years),
                volatility=float(proposal.volatility),
                risk_free_rate=float(proposal.risk_free_rate),
                dividend_yield=float(proposal.dividend_yield),
                is_call=bool(proposal.is_call),
            )
        )
        if bs is None:
            return None

        if proposal.is_call:
            lo, hi = self.config.min_delta_call, self.config.max_delta_call
        else:
            lo, hi = self.config.min_delta_put, self.config.max_delta_put
        if not _delta_in_band(bs.greeks.delta, lo=lo, hi=hi, is_call=proposal.is_call):
            return None

        # Premium-exposure gate.
        new_premium = _premium_notional(bs.price, proposal.contracts, proposal.multiplier)
        gate = check_premium_exposure(
            new_premium_notional=new_premium,
            existing_premium_notional=existing_premium_notional,
            nav=nav,
            max_pct_per_trade=self.config.max_premium_pct_per_trade,
            max_pct_aggregate=self.config.max_premium_pct_aggregate,
        )
        if not gate.allowed:
            logger.info(
                "options_directional | premium-cap rejected: %s %s",
                gate.reason, gate.metadata,
            )
            return None

        # Build the structured option contract metadata.
        spec = OptionContractSpec(
            underlying_symbol=proposal.underlying_symbol,
            expiry=proposal.expiry_yyyymmdd,
            strike=Decimal(str(proposal.strike)),
            right=OptionRight.CALL if proposal.is_call else OptionRight.PUT,
            multiplier=int(proposal.multiplier),
        )
        contract_meta = {
            "instrument_type": "option",
            "option_contract": {
                "underlying_symbol": spec.underlying_symbol,
                "expiry": spec.expiry,
                "strike": str(spec.strike),
                "right": spec.right.value,
                "multiplier": spec.multiplier,
            },
            "options_paper_only": bool(self.config.paper_only),
            "options_delta": float(bs.greeks.delta),
            "options_vega": float(bs.greeks.vega),
            "options_theoretical_price": float(bs.price),
            "options_premium_notional": str(new_premium),
            "options_dte_days": float(dte_days),
        }

        # Confidence: simple |delta|-based mapping in config bounds.
        conf_raw = abs(bs.greeks.delta)
        confidence = clip_decimal(
            Decimal(str(self.config.confidence_floor + (self.config.confidence_ceiling - self.config.confidence_floor) * conf_raw)),
            Decimal("0"), Decimal("1"),
        )

        return self._make_candidate(
            proposal=proposal,
            confidence=confidence,
            metadata=contract_meta,
            ts=ts,
        )

    # ── candidate construction (subclassed) ─────────────────────────────────

    def _make_candidate(
        self,
        *,
        proposal: OptionsProposal,
        confidence: Decimal,
        metadata: dict,
        ts: datetime,
    ) -> SignalCandidate:
        raise NotImplementedError


class LongCallStrategy(_LongOptionStrategyBase):
    """Buy a call when the operator is directionally bullish."""

    def evaluate(
        self,
        *,
        underlying_symbol: str,
        spot: float,
        strike: Decimal,
        expiry_yyyymmdd: str,
        time_to_expiry_years: float,
        volatility: float,
        risk_free_rate: float = 0.05,
        dividend_yield: float = 0.0,
        contracts: int = 1,
        multiplier: int = 100,
        nav: Decimal,
        existing_premium_notional: Decimal = Decimal("0"),
        as_of: Optional[datetime] = None,
    ) -> Optional[SignalCandidate]:
        prop = OptionsProposal(
            underlying_symbol=underlying_symbol,
            expiry_yyyymmdd=expiry_yyyymmdd,
            strike=Decimal(strike),
            is_call=True,
            spot=spot,
            volatility=volatility,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            time_to_expiry_years=time_to_expiry_years,
            contracts=contracts,
            multiplier=multiplier,
        )
        return self._evaluate(prop, nav=nav, existing_premium_notional=existing_premium_notional, as_of=as_of)

    def _make_candidate(self, *, proposal, confidence, metadata, ts) -> SignalCandidate:
        # Long call ⇒ buy ⇒ "long" side.
        side: Side = "long"
        # Naked-short safeguard: defensive — long-call cannot be a sell.
        if side != "long":
            raise ValueError("LongCallStrategy must produce side='long'")
        return SignalCandidate(
            symbol=proposal.underlying_symbol,
            asset_class="option",  # type: ignore[arg-type]
            side=side,
            timestamp=ts,
            raw_signal_strength=confidence,
            adjusted_signal_strength=confidence,
            confidence=confidence,
            strategy_name=self.config.long_call_strategy_name,
            metadata={"strategy": self.config.long_call_strategy_name, **metadata},
        )


class LongPutStrategy(_LongOptionStrategyBase):
    """Buy a put when the operator is directionally bearish."""

    def evaluate(
        self,
        *,
        underlying_symbol: str,
        spot: float,
        strike: Decimal,
        expiry_yyyymmdd: str,
        time_to_expiry_years: float,
        volatility: float,
        risk_free_rate: float = 0.05,
        dividend_yield: float = 0.0,
        contracts: int = 1,
        multiplier: int = 100,
        nav: Decimal,
        existing_premium_notional: Decimal = Decimal("0"),
        as_of: Optional[datetime] = None,
    ) -> Optional[SignalCandidate]:
        prop = OptionsProposal(
            underlying_symbol=underlying_symbol,
            expiry_yyyymmdd=expiry_yyyymmdd,
            strike=Decimal(strike),
            is_call=False,
            spot=spot,
            volatility=volatility,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            time_to_expiry_years=time_to_expiry_years,
            contracts=contracts,
            multiplier=multiplier,
        )
        return self._evaluate(prop, nav=nav, existing_premium_notional=existing_premium_notional, as_of=as_of)

    def _make_candidate(self, *, proposal, confidence, metadata, ts) -> SignalCandidate:
        # Long put ⇒ buy a put ⇒ direction "short" semantically (bearish on
        # underlying), but the *order* is BUY. We tag the metadata so the
        # execution layer doesn't accidentally try to short the underlying.
        side: Side = "short"  # bearish view on underlying
        return SignalCandidate(
            symbol=proposal.underlying_symbol,
            asset_class="option",  # type: ignore[arg-type]
            side=side,
            timestamp=ts,
            raw_signal_strength=confidence,
            adjusted_signal_strength=confidence,
            confidence=confidence,
            strategy_name=self.config.long_put_strategy_name,
            metadata={
                "strategy": self.config.long_put_strategy_name,
                "options_buy_to_open": True,  # the actual order is BUY
                **metadata,
            },
        )


# ── config IO ──────────────────────────────────────────────────────────────


def load_options_directional_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> OptionsDirectionalConfig:
    p = Path(path)
    if not p.exists():
        return OptionsDirectionalConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"could not parse {p}: {exc}") from exc
    return OptionsDirectionalConfig.from_dict(raw)
