from __future__ import annotations

from decimal import Decimal

import pytest

from risk.engine import RiskEngine, RiskVerdict, Signal


def _base_risk_cfg() -> dict:
    return {
        "fundamentals_path": "config/fundamentals.yaml",
        "max_position_pct": 0.10,
        "max_concentration_pct": 0.20,
        "max_gross_exposure_pct": 0.80,
        "max_daily_loss_pct": 0.02,
        "max_drawdown_pct": 0.10,
        "max_crypto_pct": 0.30,
        "max_single_stock_pct": 0.10,
        "max_bond_pct": 0.50,
        "max_consecutive_losses": 3,
        "cooldown_minutes": 60,
        "min_signal_confidence": 0.55,
        "proportionality_threshold_pct": 0.05,
        "minimum_order_sizes_gbp": {
            "crypto": 10,
            "equity": 50,
            "etf": 50,
            "bond": 1000,
            "forex": 1000,
            "future": 5000,
            "option": 500,
        },
    }


def _portfolio_ok() -> dict:
    return {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
    }


def _sig(*, symbol: str = "SPY", strategy: str = "momentum_breakout", qty: str = "1", price: str = "100") -> Signal:
    return Signal(
        signal_id="s-m8",
        symbol=symbol,
        side="buy",
        strategy=strategy,
        confidence=0.9,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-04-06T12:00:00+00:00",
        metadata={},
    )


def test_m8_disabled_allows_any_symbol_in_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "paper")
    cfg = _base_risk_cfg()
    cfg["m8_micro_live"] = {
        "enabled": True,
        "symbol_whitelist": ["QQQ"],
        "strategy_whitelist": ["momentum_breakout"],
        "max_notional_usd_per_order": 125.0,
    }
    engine = RiskEngine(cfg)
    decision = engine.evaluate(_sig(symbol="SPY"), _portfolio_ok())
    assert decision.verdict == RiskVerdict.APPROVED


def test_m8_rejects_symbol_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    cfg = _base_risk_cfg()
    cfg["m8_micro_live"] = {
        "enabled": True,
        "symbol_whitelist": ["QQQ"],
        "strategy_whitelist": ["momentum_breakout"],
        "max_notional_usd_per_order": 125.0,
    }
    engine = RiskEngine(cfg)
    decision = engine.evaluate(_sig(symbol="SPY"), _portfolio_ok())
    assert decision.verdict == RiskVerdict.REJECTED
    assert "m8_symbol_whitelist" in decision.checks_failed


def test_m8_rejects_strategy_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    cfg = _base_risk_cfg()
    cfg["m8_micro_live"] = {
        "enabled": True,
        "symbol_whitelist": ["SPY"],
        "strategy_whitelist": ["mean_reversion"],
        "max_notional_usd_per_order": 125.0,
    }
    engine = RiskEngine(cfg)
    decision = engine.evaluate(_sig(strategy="momentum_breakout"), _portfolio_ok())
    assert decision.verdict == RiskVerdict.REJECTED
    assert "m8_strategy_whitelist" in decision.checks_failed


def test_m8_rejects_strategy_sleeve_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    cfg = _base_risk_cfg()
    cfg["m8_micro_live"] = {
        "enabled": True,
        "symbol_whitelist": ["SPY"],
        "strategy_whitelist": ["momentum_breakout"],
        "max_notional_usd_per_order": 1_000_000.0,
        "strategy_sleeve_caps": {
            "momentum_breakout": {"max_order_notional_pct_of_portfolio": 0.001},
        },
    }
    engine = RiskEngine(cfg)
    decision = engine.evaluate(_sig(qty="10", price="100"), _portfolio_ok())
    assert decision.verdict == RiskVerdict.REJECTED
    assert "m8_strategy_sleeve_cap" in decision.checks_failed


def test_m8_rejects_notional_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    cfg = _base_risk_cfg()
    cfg["m8_micro_live"] = {
        "enabled": True,
        "symbol_whitelist": ["SPY"],
        "strategy_whitelist": ["momentum_breakout"],
        "max_notional_usd_per_order": 50.0,
    }
    engine = RiskEngine(cfg)
    decision = engine.evaluate(_sig(qty="10", price="100"), _portfolio_ok())
    assert decision.verdict == RiskVerdict.REJECTED
    assert "m8_max_notional" in decision.checks_failed
