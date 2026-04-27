"""
strategies/options_hedging.py
================================
Wave 12 — hedging options strategies.

``ProtectivePutStrategy``: BUY a put against an existing LONG stock
position (downside hedge).
``CoveredCallStrategy``: SELL a call against an existing LONG stock
position. The naked-call safeguard inside this class refuses to emit a
candidate when the operator does not hold the underlying.

Both default to ``enabled=False`` and ``paper_only=True``. The risk
engine remains the final veto layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

from core.instruments import OptionContractSpec, OptionRight
from core.models_runtime import (
    AssetClass,
    Side,
    SignalCandidate,
    clip_decimal,
)
from models.options.greeks import OptionInputs, black_scholes_greeks
from models.options.risk import (
    check_premium_exposure,
    check_underlying_required,
)

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/options_strategies.yaml")


@dataclass
class OptionsHedgingConfig:
    enabled: bool = False
    paper_only: bool = True

    # Premium-exposure caps (used for protective put — bought premium).
    max_premium_pct_per_trade: float = 0.005
    max_premium_pct_aggregate: float = 0.02

    min_dte_days: int = 14
    max_dte_days: int = 90
    confidence_floor: float = 0.10
    confidence_ceiling: float = 0.85

    protective_put_strategy_name: str = "options_protective_put"
    covered_call_strategy_name: str = "options_covered_call"
    preferred_broker: str = "ibkr"

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "OptionsHedgingConfig":
        if not raw:
            return cls()
        sect = raw.get("options_hedging") if "options_hedging" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            enabled=bool(sect.get("enabled", False)),
            paper_only=bool(sect.get("paper_only", True)),
            max_premium_pct_per_trade=float(sect.get("max_premium_pct_per_trade", 0.005)),
            max_premium_pct_aggregate=float(sect.get("max_premium_pct_aggregate", 0.02)),
            min_dte_days=int(sect.get("min_dte_days", 14)),
            max_dte_days=int(sect.get("max_dte_days", 90)),
            confidence_floor=float(sect.get("confidence_floor", 0.10)),
            confidence_ceiling=float(sect.get("confidence_ceiling", 0.85)),
            protective_put_strategy_name=str(
                sect.get("protective_put_strategy_name", "options_protective_put")
            ),
            covered_call_strategy_name=str(
                sect.get("covered_call_strategy_name", "options_covered_call")
            ),
            preferred_broker=str(sect.get("preferred_broker", "ibkr")),
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _premium_notional(price: float, contracts: int, multiplier: int) -> Decimal:
    return Decimal(str(price)) * Decimal(int(contracts)) * Decimal(int(multiplier))


# ── protective put ─────────────────────────────────────────────────────────


class ProtectivePutStrategy:
    """BUY a put to cap downside on an existing long stock position."""

    def __init__(self, config: Optional[OptionsHedgingConfig] = None):
        self.config = config or OptionsHedgingConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

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
        holdings_by_symbol: Iterable[tuple[str, Decimal]],
        as_of: Optional[datetime] = None,
    ) -> Optional[SignalCandidate]:
        if not self.config.enabled:
            return None
        ts = as_of or datetime.now(timezone.utc)

        required_qty = Decimal(int(contracts) * int(multiplier))
        und_check = check_underlying_required(
            underlying_symbol=underlying_symbol,
            holdings_by_symbol=holdings_by_symbol,
            required_quantity=required_qty,
            side_label="long",
        )
        if not und_check.allowed:
            logger.info(
                "protective_put | rejected: %s %s",
                und_check.reason, und_check.metadata,
            )
            return None

        dte_days = float(time_to_expiry_years) * 365.0
        if dte_days < self.config.min_dte_days or dte_days > self.config.max_dte_days:
            return None

        bs = black_scholes_greeks(
            OptionInputs(
                spot=float(spot),
                strike=float(strike),
                time_to_expiry_years=float(time_to_expiry_years),
                volatility=float(volatility),
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                is_call=False,
            )
        )
        if bs is None:
            return None

        new_premium = _premium_notional(bs.price, contracts, multiplier)
        gate = check_premium_exposure(
            new_premium_notional=new_premium,
            existing_premium_notional=existing_premium_notional,
            nav=nav,
            max_pct_per_trade=self.config.max_premium_pct_per_trade,
            max_pct_aggregate=self.config.max_premium_pct_aggregate,
        )
        if not gate.allowed:
            return None

        spec = OptionContractSpec(
            underlying_symbol=underlying_symbol,
            expiry=expiry_yyyymmdd,
            strike=Decimal(strike),
            right=OptionRight.PUT,
            multiplier=int(multiplier),
        )
        meta = {
            "strategy": self.config.protective_put_strategy_name,
            "instrument_type": "option",
            "option_contract": {
                "underlying_symbol": spec.underlying_symbol,
                "expiry": spec.expiry,
                "strike": str(spec.strike),
                "right": spec.right.value,
                "multiplier": spec.multiplier,
            },
            "options_paper_only": bool(self.config.paper_only),
            "options_buy_to_open": True,        # protective put is BOUGHT
            "options_hedge_role": "protective_put",
            "options_underlying_required_qty": str(required_qty),
            "options_underlying_held_qty": und_check.metadata.get("held"),
            "options_premium_notional": str(new_premium),
            "options_dte_days": dte_days,
            "options_delta": float(bs.greeks.delta),
        }

        # Confidence: |delta| in protective range — closer to ATM ⇒ better hedge ⇒ higher conf.
        conf_raw = abs(bs.greeks.delta)
        confidence = clip_decimal(
            Decimal(
                str(
                    self.config.confidence_floor
                    + (self.config.confidence_ceiling - self.config.confidence_floor) * conf_raw
                )
            ),
            Decimal("0"),
            Decimal("1"),
        )
        # Side semantics: protective put protects a LONG stock position;
        # the *option* order is BUY (options_buy_to_open=True). We mark
        # the candidate as "long" because the candidate represents the
        # protected portfolio direction.
        return SignalCandidate(
            symbol=underlying_symbol,
            asset_class="option",  # type: ignore[arg-type]
            side="long",
            timestamp=ts,
            raw_signal_strength=confidence,
            adjusted_signal_strength=confidence,
            confidence=confidence,
            strategy_name=self.config.protective_put_strategy_name,
            metadata=meta,
        )


# ── covered call ───────────────────────────────────────────────────────────


class CoveredCallStrategy:
    """SELL a call against an existing long stock position (income / yield enhancement)."""

    def __init__(self, config: Optional[OptionsHedgingConfig] = None):
        self.config = config or OptionsHedgingConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

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
        holdings_by_symbol: Iterable[tuple[str, Decimal]],
        as_of: Optional[datetime] = None,
    ) -> Optional[SignalCandidate]:
        if not self.config.enabled:
            return None
        ts = as_of or datetime.now(timezone.utc)

        # Underlying-required gate is the *naked-call safeguard*: refuse
        # to emit a covered-call candidate without enough long stock to
        # cover assignment. This is defence in depth — the risk engine
        # also rejects naked options via ``risk/options_env.py``.
        required_qty = Decimal(int(contracts) * int(multiplier))
        und_check = check_underlying_required(
            underlying_symbol=underlying_symbol,
            holdings_by_symbol=holdings_by_symbol,
            required_quantity=required_qty,
            side_label="long",
        )
        if not und_check.allowed:
            logger.warning(
                "covered_call | refusing naked-call attempt: %s %s",
                und_check.reason, und_check.metadata,
            )
            return None

        dte_days = float(time_to_expiry_years) * 365.0
        if dte_days < self.config.min_dte_days or dte_days > self.config.max_dte_days:
            return None

        bs = black_scholes_greeks(
            OptionInputs(
                spot=float(spot),
                strike=float(strike),
                time_to_expiry_years=float(time_to_expiry_years),
                volatility=float(volatility),
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                is_call=True,
            )
        )
        if bs is None:
            return None

        spec = OptionContractSpec(
            underlying_symbol=underlying_symbol,
            expiry=expiry_yyyymmdd,
            strike=Decimal(strike),
            right=OptionRight.CALL,
            multiplier=int(multiplier),
        )
        meta = {
            "strategy": self.config.covered_call_strategy_name,
            "instrument_type": "option",
            "option_contract": {
                "underlying_symbol": spec.underlying_symbol,
                "expiry": spec.expiry,
                "strike": str(spec.strike),
                "right": spec.right.value,
                "multiplier": spec.multiplier,
            },
            "options_paper_only": bool(self.config.paper_only),
            "options_sell_to_open": True,       # covered call is SOLD
            "options_hedge_role": "covered_call",
            "options_underlying_required_qty": str(required_qty),
            "options_underlying_held_qty": und_check.metadata.get("held"),
            "options_theoretical_premium": float(bs.price),
            "options_dte_days": dte_days,
            "options_delta": float(bs.greeks.delta),
        }

        # Confidence: lower delta = farther OTM = "safer" covered call, higher conf.
        conf_raw = max(0.0, 1.0 - abs(bs.greeks.delta))
        confidence = clip_decimal(
            Decimal(
                str(
                    self.config.confidence_floor
                    + (self.config.confidence_ceiling - self.config.confidence_floor) * conf_raw
                )
            ),
            Decimal("0"),
            Decimal("1"),
        )
        return SignalCandidate(
            symbol=underlying_symbol,
            asset_class="option",  # type: ignore[arg-type]
            side="long",  # the *portfolio* remains long-biased; option leg is short call
            timestamp=ts,
            raw_signal_strength=confidence,
            adjusted_signal_strength=confidence,
            confidence=confidence,
            strategy_name=self.config.covered_call_strategy_name,
            metadata=meta,
        )


# ── config IO ──────────────────────────────────────────────────────────────


def load_options_hedging_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> OptionsHedgingConfig:
    p = Path(path)
    if not p.exists():
        return OptionsHedgingConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"could not parse {p}: {exc}") from exc
    return OptionsHedgingConfig.from_dict(raw)
