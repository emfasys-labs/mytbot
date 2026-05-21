import pytest
from decimal import Decimal
from risk.engine import RiskEngine, Signal

def _default_cfg() -> dict:
    return {
        "enforce_static_exposure_caps": False,
        "max_daily_loss_pct": "0.02",
        "min_signal_confidence": "0.50",
        "min_trade_quality_score": "0.30",
        "arbitrage": {
            "enabled": True,
            "max_total_arbitrage_exposure": "0.20"
        },
        # D125 caps are tested separately in tests/test_d125_risk_caps.py;
        # this suite exercises dynamic daily-loss / confidence scaling and
        # uses 10% NAV per signal, which would otherwise trip D125.
        "single_name_notional": {"enabled": False},
        "intraday_symbol_adds": {"enabled": False},
    }

def _signal(
    *,
    qty: str = "10",
    price: str = "100",
    confidence: float = 1.0,
    metadata: dict | None = None,
    asset_class: str = "equity",
    symbol: str = "AAPL",
    side: str = "buy",
) -> Signal:
    return Signal(
        signal_id="s-1",
        symbol=symbol,
        side=side,
        strategy="momentum_breakout",
        confidence=confidence,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="ibkr",
        asset_class=asset_class,
        timestamp="2026-04-06T12:00:00+00:00",
        metadata=metadata or {},
    )


def test_dynamic_daily_loss_shrinks_in_poor_market() -> None:
    cfg = _default_cfg()
    eng = RiskEngine(cfg)
    
    # Portfolio is at 10000, 2% loss would normally be 200
    # But market state is 0.1 (poor), so limit shrinks to 10% of base -> 0.2% -> 20 limit
    portfolio = {
        "portfolio_value": "10000",
        "daily_realized_pnl": "-50",  # We are down 50, which is > 20 limit!
        "metadata": {"market_state_score": 0.1}
    }
    
    sig = _signal()
    decision = eng.evaluate(sig, portfolio)
    
    assert decision.verdict.value == "rejected"
    assert "daily_loss_limit" in (decision.reason or "")
    
    # If market state is 1.0 (great), limit expands to full 2% -> 200 allowed.
    # 50 loss is perfectly fine.
    portfolio["metadata"]["market_state_score"] = 1.0
    decision = eng.evaluate(sig, portfolio)
    assert decision.verdict.value == "approved"

def test_dynamic_confidence_threshold_rises_in_poor_market() -> None:
    cfg = _default_cfg()
    eng = RiskEngine(cfg)
    
    # Base confidence is 0.50. In poor market (0.0), threshold raises to base * (2 - 0.1) = 0.95
    portfolio = {
        "portfolio_value": "10000",
        "metadata": {"market_state_score": 0.0}
    }
    
    # Signal with 0.60 confidence
    sig = _signal(confidence=0.60)
    decision = eng.evaluate(sig, portfolio)
    
    assert decision.verdict.value == "rejected"
    assert "confidence_threshold" in (decision.reason or "")
    
    # If market state is 1.0, threshold is base * (2 - 1.0) = 0.50
    portfolio["metadata"]["market_state_score"] = 1.0
    decision = eng.evaluate(sig, portfolio)
    assert decision.verdict.value == "approved"


def test_dynamic_consecutive_losses() -> None:
    cfg = _default_cfg()
    cfg["max_consecutive_losses"] = 5
    cfg["cooldown_minutes"] = 10
    eng = RiskEngine(cfg)

    # Perfect market state (1.0) and normal volatility (1.0) -> scaled_max_losses = 5
    portfolio = {
        "portfolio_value": "10000",
        "consecutive_losses": 4,
        "metadata": {
            "market_state_score": 1.0,
            "market_volatility_scalar": 1.0,
        }
    }
    sig = _signal()
    eng.restore_runtime_state(portfolio)
    decision = eng.evaluate(sig, portfolio)
    assert decision.verdict.value == "approved"

    # consecutive_losses = 5 -> rejected
    portfolio["consecutive_losses"] = 5
    eng.restore_runtime_state(portfolio)
    decision = eng.evaluate(sig, portfolio)
    assert decision.verdict.value == "rejected"
    assert "consecutive_losses" in (decision.reason or "")

    # Poor market state (0.5) and high volatility (2.0)
    # multiplier = 0.5 / 2.0 = 0.25
    # scaled_max_losses = round(5 * 0.25) = round(1.25) = 1
    portfolio2 = {
        "portfolio_value": "10000",
        "consecutive_losses": 0,
        "metadata": {
            "market_state_score": 0.5,
            "market_volatility_scalar": 2.0,
        }
    }
    eng2 = RiskEngine(cfg)
    eng2.restore_runtime_state(portfolio2)
    decision = eng2.evaluate(sig, portfolio2)
    assert decision.verdict.value == "approved"

    # consecutive_losses = 1 -> rejected (since limit is now 1)
    portfolio2["consecutive_losses"] = 1
    eng2.restore_runtime_state(portfolio2)
    decision = eng2.evaluate(sig, portfolio2)
    assert decision.verdict.value == "rejected"
    assert "consecutive_losses" in (decision.reason or "")




