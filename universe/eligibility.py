from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EligibilityResult:
    ok: bool
    reason: str


def _dec(x: Any, default: Decimal = Decimal("0")) -> Decimal:
    if x is None:
        return default
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return default


def evaluate_eligibility(
    symbol: str,
    *,
    asset_class: str,
    rules: dict[str, Any],
    broker_allowed: bool = True,
    has_history: bool = True,
    adv_usd: Decimal | None = None,
    spread_bps: Decimal | None = None,
    data_age_sec: float | None = None,
    min_order_notional_usd: Decimal | None = None,
) -> EligibilityResult:
    """
    Hard filters for candidate universe. Monetary inputs use Decimal where provided.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return EligibilityResult(False, "empty_symbol")
    if not broker_allowed:
        return EligibilityResult(False, "broker_unavailable")
    allowed = rules.get("allowed_asset_classes") or []
    if allowed and asset_class.lower() not in {str(a).lower() for a in allowed}:
        return EligibilityResult(False, "asset_class_excluded")

    min_hist = int(rules.get("min_history_bars", 20) or 20)
    if not has_history:
        return EligibilityResult(False, f"insufficient_history_need_{min_hist}")

    min_adv = _dec(rules.get("min_adv_usd"), Decimal("0"))
    if adv_usd is not None and min_adv > 0 and adv_usd < min_adv:
        return EligibilityResult(False, "low_liquidity_adv")

    max_spread = _dec(rules.get("max_spread_bps"), Decimal("9999"))
    if spread_bps is not None and spread_bps > max_spread:
        return EligibilityResult(False, "spread_too_wide")

    max_age = float(rules.get("max_data_age_sec", 1e9) or 1e9)
    if data_age_sec is not None and data_age_sec > max_age:
        return EligibilityResult(False, "stale_data")

    min_notional = _dec(rules.get("min_order_notional_usd"), Decimal("0"))
    if min_order_notional_usd is not None and min_notional > 0 and min_order_notional_usd < min_notional:
        return EligibilityResult(False, "below_min_order_size")

    return EligibilityResult(True, "ok")
