from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation
from core.models_runtime import PortfolioState
from risk.regime_state import compute_regime_state_from_inputs


def _portfolio() -> PortfolioState:
    now = datetime.now(timezone.utc)
    return PortfolioState(
        timestamp=now,
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("50000"),
        available_buying_power=Decimal("50000"),
        gross_exposure=Decimal("50000"),
        net_exposure=Decimal("50000"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal("0.02"),
    )


def test_regime_insufficient_symbols() -> None:
    cfg = load_allocation()
    p = _portfolio()
    r = compute_regime_state_from_inputs(
        portfolio_state=p,
        allocation_cfg=cfg,
        feature_rows=[],
        news_dispersion=None,
    )
    assert r.regime_label == "insufficient_data"


def test_regime_from_synthetic_rows() -> None:
    cfg = load_allocation()
    p = _portfolio()
    rows = [
        {
            "symbol": "A",
            "features": {"mom_10": 2.0, "rsi_14": 60.0, "volume_z": 0.5, "relative_dollar_volume": 1.1},
        },
        {
            "symbol": "B",
            "features": {"mom_10": 1.5, "rsi_14": 58.0, "volume_z": 0.4, "relative_dollar_volume": 1.05},
        },
        {
            "symbol": "C",
            "features": {"mom_10": 1.8, "rsi_14": 57.0, "volume_z": 0.3, "relative_dollar_volume": 1.02},
        },
    ]
    r = compute_regime_state_from_inputs(
        portfolio_state=p,
        allocation_cfg=cfg,
        feature_rows=rows,
        news_dispersion=(0.1, 0.4),
    )
    assert r.regime_label != "insufficient_data"
    assert r.components.trend_strength > Decimal("0")
    assert r.market_state_score == r.market_state_score  # finite
