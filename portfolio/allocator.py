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
        self.parameters = ParameterManager(fundamentals_path)
        self._hwm = Decimal("0")
        self._current_tier: Optional[dict] = None

    def get_current_tier(self, portfolio_value: Decimal) -> dict:
        tiers = self.risk_limits.get("capital_tiers", {})
        if not tiers:
            # Compatibility fallback when tiers are absent.
            default = {
                "name": "default",
                "min": 0,
                "max": 10**18,
                "assets_allowed": ["crypto", "equity", "etf", "bond", "forex", "future", "option"],
                "max_positions": 20,
                "max_position_pct": float(self.parameters.get_value("max_single_position_pct")),
                "strategies_enabled": ["all"],
            }
            self._current_tier = default
            return default

        for name, cfg in tiers.items():
            lo = Decimal(str(cfg.get("min", 0)))
            hi = Decimal(str(cfg.get("max", 10**18)))
            if lo <= portfolio_value < hi:
                tier = {"name": name, **cfg}
                if self._current_tier and self._current_tier.get("name") != name:
                    logger.warning(
                        "Portfolio tier changed: {} -> {} | value={}",
                        self._current_tier.get("name"),
                        name,
                        portfolio_value,
                    )
                self._current_tier = tier
                return tier
        raise ValueError(f"No tier found for portfolio value {portfolio_value}")

    def is_asset_allowed(self, asset_class: str, portfolio_value: Decimal) -> bool:
        tier = self.get_current_tier(portfolio_value)
        allowed = asset_class in tier.get("assets_allowed", [])
        if not allowed:
            logger.warning(
                "Asset class '{}' not available at {} tier ({} portfolio).",
                asset_class,
                tier["name"],
                portfolio_value,
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

        tier_pct = Decimal(str(tier.get("max_position_pct", self.parameters.get_value("max_single_position_pct"))))
        fundamental_cap = self.parameters.get_value("max_single_position_pct")
        base_ceiling = portfolio_value * min(tier_pct, fundamental_cap)
        combined = min(scalars.volatility_scalar, scalars.drawdown_scalar, scalars.correlation_scalar)
        combined = max(0.0, min(1.0, combined))
        final = base_ceiling * Decimal(str(combined))

        minimums = self.risk_limits.get("minimum_order_sizes_gbp", {})
        min_order = Decimal(str(minimums.get(asset_class, 0)))
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
        target = float(self.parameters.get_value("target_annual_vol"))
        if current_vol <= 0:
            return 1.0
        return max(0.20, min(1.0, target / current_vol))

    def compute_drawdown_scalar(self, current_value: Decimal) -> float:
        if current_value > self._hwm:
            self._hwm = current_value
        if self._hwm <= 0:
            return 1.0
        dd = float((self._hwm - current_value) / self._hwm)
        if dd >= 0.20:
            return 0.0
        if dd >= 0.15:
            return 0.25
        if dd >= 0.10:
            return 0.50
        if dd >= 0.05:
            return 0.75
        return 1.0

    def compute_correlation_scalar(self, avg_correlation: float) -> float:
        return 0.60 if avg_correlation >= 0.70 else 1.0

    def on_balance_change(self, old_value: Decimal, new_value: Decimal, trigger: str = "unknown") -> dict:
        old_tier = self.get_current_tier(old_value)
        new_tier = self.get_current_tier(new_value)
        old_strats = set(old_tier.get("strategies_enabled", []))
        new_strats = set(new_tier.get("strategies_enabled", []))
        out = {
            "trigger": trigger,
            "old_value": old_value,
            "new_value": new_value,
            "old_tier": old_tier["name"],
            "new_tier": new_tier["name"],
            "tier_changed": old_tier["name"] != new_tier["name"],
            "strategies_unlocked": sorted(new_strats - old_strats),
            "strategies_locked": sorted(old_strats - new_strats),
            "assets_unlocked": sorted(set(new_tier.get("assets_allowed", [])) - set(old_tier.get("assets_allowed", []))),
            "assets_locked": sorted(set(old_tier.get("assets_allowed", [])) - set(new_tier.get("assets_allowed", []))),
        }
        logger.info(
            "Balance change | trigger={} | {} -> {} | tier {} -> {}",
            trigger,
            old_value,
            new_value,
            old_tier["name"],
            new_tier["name"],
        )
        return out
