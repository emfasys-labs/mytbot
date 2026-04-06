"""
Capital allocator powered by ParameterManager.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import yaml
from loguru import logger

from risk.parameters import ParameterManager
from risk.provider import ParameterProvider


@dataclass
class DynamicScalars:
    volatility_scalar: float = 1.0
    drawdown_scalar: float = 1.0
    correlation_scalar: float = 1.0


@dataclass
class PositionSizeResult:
    asset_class: str
    portfolio_value: Decimal
    tier_name: str
    base_ceiling: Decimal
    volatility_scalar: float
    drawdown_scalar: float
    correlation_scalar: float
    combined_scalar: float
    final_size: Decimal
    blocked: bool
    block_reason: Optional[str]


class CapitalAllocator:
    def __init__(
        self,
        risk_limits_path: str = "config/risk_limits.yaml",
        fundamentals_path: str = "config/fundamentals.yaml",
    ):
        with open(risk_limits_path, encoding="utf-8") as f:
            self.risk_limits = yaml.safe_load(f) or {}
        self.parameters = ParameterManager(fundamentals_path, enable_db_logging=False)
        self.provider = ParameterProvider(self.parameters, self.risk_limits)
        self._hwm = Decimal("0")
        self._current_tier: Optional[dict] = None

    def get_current_tier(self, portfolio_value: Decimal) -> dict:
        # Backward-compatible API: no tiers in proportionality mode.
        return {
            "name": "proportional",
            "min": 0,
            "max": 10**18,
            "assets_allowed": ["dynamic_by_minimum_order"],
            "max_positions": None,
            "max_position_pct": float(
                self.provider.get_decimal("max_single_position_pct", config_fallback_key="max_position_pct")
            ),
            "strategies_enabled": ["all"],
        }

    def _minimum_order(self, asset_class: str) -> Decimal:
        minimums = self.risk_limits.get("minimum_order_sizes_gbp", {})
        return Decimal(str(minimums.get(asset_class, 0)))

    def is_asset_allowed(self, asset_class: str, portfolio_value: Decimal) -> bool:
        # Pure proportionality rule:
        # allow an asset iff minimum order < 5% of portfolio.
        threshold_pct = self.provider.get_decimal(
            "proportionality_threshold_pct",
            config_fallback_key="proportionality_threshold_pct",
            fallback=Decimal("0"),
        )
        min_order = self._minimum_order(asset_class)
        allowed = min_order < (portfolio_value * threshold_pct)
        if not allowed:
            logger.warning(
                "Asset class '{}' blocked by proportionality | min_order={} | portfolio={} | threshold_pct={}",
                asset_class,
                min_order,
                portfolio_value,
                threshold_pct,
            )
        return allowed

    def get_position_size(
        self,
        portfolio_value: Decimal,
        asset_class: str,
        scalars: Optional[DynamicScalars] = None,
    ) -> PositionSizeResult:
        if scalars is None:
            scalars = DynamicScalars()
        tier = self.get_current_tier(portfolio_value)
        if not self.is_asset_allowed(asset_class, portfolio_value):
            return PositionSizeResult(
                asset_class=asset_class,
                portfolio_value=portfolio_value,
                tier_name=tier["name"],
                base_ceiling=Decimal("0"),
                volatility_scalar=scalars.volatility_scalar,
                drawdown_scalar=scalars.drawdown_scalar,
                correlation_scalar=scalars.correlation_scalar,
                combined_scalar=0.0,
                final_size=Decimal("0"),
                blocked=True,
                block_reason="asset class blocked by tier",
            )

        base_ceiling = portfolio_value * self.provider.get_decimal(
            "max_single_position_pct", config_fallback_key="max_position_pct"
        )
        combined = min(scalars.volatility_scalar, scalars.drawdown_scalar, scalars.correlation_scalar)
        combined = max(0.0, min(1.0, combined))
        final = base_ceiling * Decimal(str(combined))

        min_order = self._minimum_order(asset_class)
        if final < min_order:
            return PositionSizeResult(
                asset_class=asset_class,
                portfolio_value=portfolio_value,
                tier_name=tier["name"],
                base_ceiling=base_ceiling,
                volatility_scalar=scalars.volatility_scalar,
                drawdown_scalar=scalars.drawdown_scalar,
                correlation_scalar=scalars.correlation_scalar,
                combined_scalar=combined,
                final_size=Decimal("0"),
                blocked=True,
                block_reason=f"below minimum order ({min_order})",
            )

        return PositionSizeResult(
            asset_class=asset_class,
            portfolio_value=portfolio_value,
            tier_name=tier["name"],
            base_ceiling=base_ceiling,
            volatility_scalar=scalars.volatility_scalar,
            drawdown_scalar=scalars.drawdown_scalar,
            correlation_scalar=scalars.correlation_scalar,
            combined_scalar=combined,
            final_size=final,
            blocked=False,
            block_reason=None,
        )

    def compute_volatility_scalar(self, current_vol: float) -> float:
        target = float(self.provider.get_decimal("target_annual_vol", fallback=Decimal("0")))
        if current_vol <= 0:
            return 1.0
        cfg = self.risk_limits.get("volatility_scaling", {})
        min_scalar = float(cfg.get("min_scalar", 0.0))
        max_scalar = float(cfg.get("max_scalar", 1.0))
        return max(min_scalar, min(max_scalar, target / current_vol))

    def compute_drawdown_scalar(self, current_value: Decimal) -> float:
        if current_value > self._hwm:
            self._hwm = current_value
        if self._hwm <= 0:
            return float(self.risk_limits.get("drawdown_scaling", {}).get("default_scalar", 1.0))
        dd = float((self._hwm - current_value) / self._hwm)
        cfg = self.risk_limits.get("drawdown_scaling", {})
        thresholds = cfg.get("thresholds", [])
        for row in sorted(thresholds, key=lambda x: x.get("drawdown", 0), reverse=True):
            if dd >= float(row.get("drawdown", 1)):
                return float(row.get("scalar", 0))
        return float(cfg.get("default_scalar", 1.0))

    def compute_correlation_scalar(self, avg_correlation: float) -> float:
        cfg = self.risk_limits.get("correlation_scaling", {})
        threshold = float(cfg.get("high_correlation_threshold", 1.0))
        return float(cfg.get("high_correlation_scalar", 1.0)) if avg_correlation >= threshold else float(
            cfg.get("normal_scalar", 1.0)
        )

    def on_balance_change(self, old_value: Decimal, new_value: Decimal, trigger: str = "unknown") -> dict:
        minimums = self.risk_limits.get("minimum_order_sizes_gbp", {})
        assets = sorted(minimums.keys())
        old_allowed = {a for a in assets if self.is_asset_allowed(a, old_value)}
        new_allowed = {a for a in assets if self.is_asset_allowed(a, new_value)}
        out = {
            "trigger": trigger,
            "old_value": old_value,
            "new_value": new_value,
            "old_tier": "proportional",
            "new_tier": "proportional",
            "tier_changed": False,
            "strategies_unlocked": [],
            "strategies_locked": [],
            "assets_unlocked": sorted(new_allowed - old_allowed),
            "assets_locked": sorted(old_allowed - new_allowed),
        }
        logger.info(
            "Balance change | trigger={} | {} -> {} | proportional mode",
            trigger,
            old_value,
            new_value,
        )
        return out
