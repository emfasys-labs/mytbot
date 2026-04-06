from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from portfolio.allocator import CapitalAllocator, DynamicScalars
from risk.parameters import ParameterManager, ParameterRecommendation


def _assert_close(a: Decimal, b: Decimal, tol: Decimal = Decimal("0.00000001")) -> None:
    assert abs(a - b) <= tol, f"{a} != {b}"


def run() -> None:
    pm = ParameterManager("config/fundamentals.yaml")

    # 1) defaults load
    _assert_close(pm.get_value("half_kelly_fraction"), Decimal("0.50"))

    # 2) regime override applies and bounded
    assert pm.apply_regime_override("half_kelly_fraction", 0.40, "high vol", "risk-model") is True
    _assert_close(pm.get_value("half_kelly_fraction"), Decimal("0.40"))
    try:
        pm.apply_regime_override("half_kelly_fraction", 0.99, "bad", "risk-model")
        raise AssertionError("Expected bounded override to fail")
    except ValueError:
        pass

    # 3) ai recommendation rejected when regime override active
    rec = ParameterRecommendation(
        parameter="half_kelly_fraction",
        current_value=0.40,
        recommended_value=0.45,
        confidence=0.95,
        rationale="try increase",
        duration_hours=6,
        evidence=["vol normalization"],
    )
    assert pm.apply_ai_recommendation(rec) is False

    # reset manager to test AI paths cleanly
    pm = ParameterManager("config/fundamentals.yaml")

    # 4) ai recommendation applies with enough confidence
    rec_ok = ParameterRecommendation(
        parameter="cash_buffer_pct",
        current_value=0.10,
        recommended_value=0.08,
        confidence=0.90,
        rationale="stable regime",
        duration_hours=1,
        evidence=["vix<15"],
    )
    assert pm.apply_ai_recommendation(rec_ok) is True
    _assert_close(pm.get_value("cash_buffer_pct"), Decimal("0.08"))

    # 5) rejected when below confidence
    rec_low = ParameterRecommendation(
        parameter="cash_buffer_pct",
        current_value=0.08,
        recommended_value=0.09,
        confidence=0.60,
        rationale="weak signal",
        duration_hours=1,
        evidence=["low confidence"],
    )
    assert pm.apply_ai_recommendation(rec_low) is False

    # 6) rejected when outside bounds
    rec_oob = ParameterRecommendation(
        parameter="cash_buffer_pct",
        current_value=0.08,
        recommended_value=0.50,
        confidence=0.95,
        rationale="out of bounds",
        duration_hours=1,
        evidence=["none"],
    )
    assert pm.apply_ai_recommendation(rec_oob) is False

    # 7) expiry reverts to default
    # force expiry by patching internal override timestamp
    ov = pm._ai_overrides["cash_buffer_pct"]  # noqa: SLF001
    pm._ai_overrides["cash_buffer_pct"] = replace(ov, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))  # noqa: SLF001
    pm.check_expiries()
    _assert_close(pm.get_value("cash_buffer_pct"), Decimal("0.10"))

    # 8) history exists
    history = pm.get_parameter_history("cash_buffer_pct", days=30)
    assert len(history) >= 2

    # 9) allocator integration + dynamic scalar path
    allocator = CapitalAllocator(
        risk_limits_path="config/risk_limits.yaml",
        fundamentals_path="config/fundamentals.yaml",
    )
    tier = allocator.get_current_tier(Decimal("100"))
    assert tier["name"] == "micro"
    assert allocator.is_asset_allowed("crypto", Decimal("100")) is True
    assert allocator.is_asset_allowed("bond", Decimal("100")) is False

    size_base = allocator.get_position_size(Decimal("10000"), "crypto")
    assert size_base.blocked is False
    high_vol_scalar = allocator.compute_volatility_scalar(0.80)
    size_hv = allocator.get_position_size(
        Decimal("10000"), "crypto", DynamicScalars(volatility_scalar=high_vol_scalar)
    )
    assert size_hv.final_size <= size_base.final_size

    print("All parameter/allocator tests passed.")


if __name__ == "__main__":
    run()
