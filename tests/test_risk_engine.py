from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from risk.engine import RiskEngine, RiskVerdict, Signal


def _risk_cfg() -> dict:
    return {
        "fundamentals_path": "config/fundamentals.yaml",
        "enforce_static_exposure_caps": True,
        "enforce_static_order_caps": True,
        "max_position_pct": 0.10,
        "max_concentration_pct": 0.20,
        "max_gross_exposure_pct": 0.80,
        "max_daily_loss_pct": 0.02,
        "max_drawdown_pct": 0.10,
        "max_crypto_pct": 0.30,
        "max_forex_pct": 0.20,
        "max_future_pct": 0.0,
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


def _signal(
    *,
    qty: str = "1",
    price: str = "100",
    confidence: float = 0.9,
    metadata: dict | None = None,
    asset_class: str = "equity",
    symbol: str = "SPY",
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


def _spy_call_option_metadata() -> dict:
    return {
        "option_contract": {
            "underlying_symbol": "SPY",
            "expiry": "20260420",
            "strike": "500",
            "right": "C",
            "multiplier": 100,
        }
    }


def test_rejects_on_daily_loss_limit() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal()
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("-2500"),  # 2.5% > 2%
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["daily_loss_limit"]


def test_rejects_on_position_size_limit() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="20", price="1000")  # 20k notional on 100k > 10%
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["position_size"]


def test_rejects_on_asset_proportionality() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="1", price="100")
    sig.asset_class = "bond"
    decision = engine.evaluate(
        sig,
        {
            "portfolio_value": Decimal("10000"),  # 5% threshold=500; bond min=1000
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["asset_proportionality"]


def test_rejects_on_minimum_order_size() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="0.1", price="100")
    sig.asset_class = "equity"  # notional 10 < min 50
    decision = engine.evaluate(
        sig,
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["minimum_order_size"]


def test_rejects_on_max_exposure_limit() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="5", price="1000")  # 5k
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("79000"),  # projected 84k > 80k max
        "symbol_exposure": {},
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_exposure"]


def test_rejects_on_concentration_limit() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="5", price="1000")  # 5k
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {"SPY": Decimal("19000")},  # projected 24k > 20k max
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["concentration"]


def test_rejects_on_drawdown_limit() -> None:
    engine = RiskEngine(_risk_cfg())
    # Seed engine high watermark to 100k
    engine.update_high_watermark(Decimal("100000"))
    sig = _signal(qty="1", price="100")
    portfolio = {
        "portfolio_value": Decimal("85000"),  # 15% drawdown > 10% max
        "high_watermark_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["drawdown_limit"]


def test_drawdown_limit_is_fixed_operator_contract_not_regime_scaled() -> None:
    cfg = _risk_cfg()
    cfg["max_drawdown_pct"] = 0.20
    engine = RiskEngine(cfg)
    engine.update_high_watermark(Decimal("100000"))
    sig = _signal(qty="1", price="100", metadata={})
    portfolio = {
        "portfolio_value": Decimal("85000"),  # 15% drawdown: inside fixed 20% breaker
        "high_watermark_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
        "metadata": {"market_state_score": 0.30},
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.APPROVED
    assert Decimal(sig.metadata["risk_drawdown_limit_pct"]) == Decimal("0.20")


def test_rejects_on_asset_class_limit_crypto() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="5", price="1000")
    sig.asset_class = "crypto"
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {"crypto": Decimal("29000")},  # +5k => 34k > 30k max
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["asset_class_limit"]


def test_rejects_on_asset_class_limit_bond() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal(qty="2", price="1000")
    sig.asset_class = "bond"
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {"bond": Decimal("49500")},  # +2k => 51.5k > 50k max
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["asset_class_limit"]


@dataclass
class _FakeSession:
    rows: list
    committed: bool = False

    def add(self, row) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self):
        self.rows = []
        self.last_session = _FakeSession(self.rows)

    def __call__(self):
        session = self.last_session

        class _CM:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _CM()


@pytest.mark.asyncio
async def test_evaluate_and_persist_writes_risklog_row() -> None:
    engine = RiskEngine(_risk_cfg())
    sig = _signal()
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
    }
    sf = _FakeSessionFactory()
    decision = await engine.evaluate_and_persist(sf, sig, portfolio)
    assert decision.verdict == RiskVerdict.APPROVED
    assert len(sf.rows) == 1
    row = sf.rows[0]
    assert row.signal_id == sig.signal_id
    assert row.verdict == "approved"
    assert sf.last_session.committed is True


def test_rejects_when_kill_switch_active() -> None:
    engine = RiskEngine(_risk_cfg())
    engine.kill()
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["kill_switch"]


def test_rejects_when_broker_disabled() -> None:
    engine = RiskEngine(_risk_cfg())
    engine.disable_broker("ibkr")
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["broker_disabled"]


def test_kill_switch_persists_across_engine_restart(tmp_path) -> None:
    state_path = tmp_path / "risk_state.json"
    cfg = _risk_cfg()
    cfg["runtime_state_path"] = str(state_path)
    engine = RiskEngine(cfg)
    engine.kill()

    restarted = RiskEngine(cfg)
    assert restarted.is_killed is True
    assert json.loads(state_path.read_text())["is_killed"] is True


def test_runtime_snapshot_restores_disabled_brokers_and_kill(tmp_path) -> None:
    cfg = _risk_cfg()
    cfg["runtime_state_path"] = str(tmp_path / "risk_state.json")
    engine = RiskEngine(cfg)
    engine.restore_runtime_state({"is_killed": True, "disabled_brokers": ["IBKR"]})

    assert engine.is_killed is True
    assert engine.is_broker_disabled("ibkr") is True
    snap = engine.snapshot_runtime_state()
    assert snap["is_killed"] is True
    assert snap["disabled_brokers"] == ["ibkr"]


def test_stale_runtime_snapshot_cannot_clear_persisted_kill(tmp_path) -> None:
    cfg = _risk_cfg()
    cfg["runtime_state_path"] = str(tmp_path / "risk_state.json")
    engine = RiskEngine(cfg)
    engine.kill()

    restarted = RiskEngine(cfg)
    restarted.restore_runtime_state({"is_killed": False, "disabled_brokers": []})
    assert restarted.is_killed is True


def test_rejects_on_consecutive_losses_and_enters_cooldown() -> None:
    cfg = _risk_cfg()
    cfg["max_consecutive_losses"] = 2
    engine = RiskEngine(cfg)
    engine.record_loss(Decimal("100"))
    engine.record_loss(Decimal("50"))
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["consecutive_losses"]
    # Next evaluation should fail cooldown first.
    decision2 = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision2.verdict == RiskVerdict.REJECTED
    assert decision2.checks_failed == ["cooldown"]


def test_consecutive_losses_disabled_when_zero_or_missing() -> None:
    cfg = _risk_cfg()
    cfg["max_consecutive_losses"] = 0
    engine = RiskEngine(cfg)
    engine.record_loss(Decimal("100"))
    engine.record_loss(Decimal("50"))
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED


def test_rejects_on_confidence_threshold() -> None:
    cfg = _risk_cfg()
    cfg["min_signal_confidence"] = 0.90
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(confidence=0.65),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["confidence_threshold"]


def test_approves_when_all_checks_pass() -> None:
    engine = RiskEngine(_risk_cfg())
    decision = engine.evaluate(
        _signal(qty="1", price="100"),
        {
            "portfolio_value": Decimal("100000"),
            "high_watermark_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {"SPY": Decimal("1000")},
            "asset_class_exposure": {"equity": Decimal("1000")},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED
    assert not decision.checks_failed


def test_allocator_d015_primary_keeps_hard_exposure_checks() -> None:
    cfg = _risk_cfg()
    cfg["allocator_d015_primary"] = True
    cfg["max_gross_exposure_pct"] = Decimal("0.01")
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(qty="1", price="100"),
        {
            "portfolio_value": Decimal("100000"),
            "high_watermark_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("50000"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_exposure"]


def test_allocator_d015_keeps_hard_exposure_checks() -> None:
    cfg = _risk_cfg()
    cfg["allocator_d015_enabled"] = True
    cfg["max_gross_exposure_pct"] = Decimal("0.01")
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(qty="1", price="100"),
        {
            "portfolio_value": Decimal("100000"),
            "high_watermark_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("50000"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_exposure"]


def test_rejects_order_above_absolute_notional_cap() -> None:
    cfg = _risk_cfg()
    cfg["max_order_notional_usd"] = "100000"
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(qty="1200", price="100"),
        {
            "portfolio_value": Decimal("1000000"),
            "high_watermark_value": Decimal("1000000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_order_notional"]


def test_rejects_allocator_notional_far_above_strategy_intent() -> None:
    cfg = _risk_cfg()
    cfg["max_position_pct"] = Decimal("1.0")
    cfg["max_concentration_pct"] = Decimal("1.0")
    cfg["max_gross_exposure_pct"] = Decimal("2.0")
    cfg["max_forex_pct"] = Decimal("2.0")
    cfg["max_allocator_notional_multiple"] = "3"
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(
            symbol="USDCHF",
            asset_class="forex",
            qty="1363399.15586220",
            price="0.77921582",
            metadata={"sizing_strategy_target_notional": "26991.91"},
        ),
        {
            "portfolio_value": Decimal("1072898.74361650"),
            "high_watermark_value": Decimal("1072898.74361650"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["allocator_amplification"]


def test_static_order_caps_are_opt_in_legacy_guardrails() -> None:
    cfg = _risk_cfg()
    cfg["enforce_static_exposure_caps"] = False
    cfg["enforce_static_order_caps"] = False
    cfg["max_order_notional_usd"] = "100000"
    cfg["max_allocator_notional_multiple"] = "3"
    # D125 introduced unconditional single-name and cumulative-add
    # caps. They're a separate hard rail (and have their own tests),
    # not in the "legacy opt-in" family this test pins down.
    cfg["single_name_notional"] = {"enabled": False}
    cfg["intraday_symbol_adds"] = {"enabled": False}
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(
            symbol="USDCHF",
            asset_class="forex",
            qty="1363399.15586220",
            price="0.77921582",
            metadata={"sizing_strategy_target_notional": "26991.91"},
        ),
        {
            "portfolio_value": Decimal("1072898.74361650"),
            "high_watermark_value": Decimal("1072898.74361650"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED


def test_static_exposure_caps_are_opt_in_legacy_guardrails() -> None:
    cfg = _risk_cfg()
    cfg["enforce_static_exposure_caps"] = False
    # See D125 note above.
    cfg["single_name_notional"] = {"enabled": False}
    cfg["intraday_symbol_adds"] = {"enabled": False}
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(qty="1200", price="100"),
        {
            "portfolio_value": Decimal("100000"),
            "high_watermark_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "current_gross_exposure": Decimal("500000"),
            "symbol_exposure": {"SPY": Decimal("500000")},
            "asset_class_exposure": {"equity": Decimal("500000")},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED


@pytest.mark.skip(reason="RiskEngine has no max_trades_per_day check wired yet (config key unused).")
def test_rejects_on_max_trades_per_day() -> None:
    cfg = _risk_cfg()
    cfg["max_trades_per_day"] = 2
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "trades_today": 2,
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_trades_per_day"]


def test_rejects_on_max_loss_per_trade_pct() -> None:
    cfg = _risk_cfg()
    cfg["max_loss_per_trade_pct"] = 0.01
    engine = RiskEngine(cfg)
    decision = engine.evaluate(
        _signal(qty="50", price="100", metadata={"stop_loss_pct": "0.30"}),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "trades_today": 0,
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["max_loss_per_trade_pct"]


def test_rejects_duplicate_same_direction_theme() -> None:
    engine = RiskEngine(_risk_cfg())
    decision = engine.evaluate(
        _signal(symbol="SPY", side="buy", qty="1", price="100"),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "trades_today": 0,
            "current_gross_exposure": Decimal("1000"),
            "symbol_exposure": {"SPY": Decimal("1000")},
            "asset_class_exposure": {"equity": Decimal("1000")},
            "positions": {"SPY": {"quantity": 10}},
        },
    )
    assert decision.verdict == RiskVerdict.REJECTED
    assert decision.checks_failed == ["theme_uniqueness"]


def test_allocator_topup_bypasses_duplicate_theme_only() -> None:
    engine = RiskEngine(_risk_cfg())
    decision = engine.evaluate(
        _signal(
            symbol="SPY",
            side="buy",
            qty="1",
            price="100",
            metadata={"coordinator_kind": "open_strategy", "sizing_topup_existing": True},
        ),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "trades_today": 0,
            "current_gross_exposure": Decimal("1000"),
            "symbol_exposure": {"SPY": Decimal("1000")},
            "asset_class_exposure": {"equity": Decimal("1000")},
            "positions": {"SPY": {"quantity": 10}},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED


def test_options_rejected_when_disabled() -> None:
    cfg = _risk_cfg()
    cfg["options_trading"] = {"enabled": False}
    engine = RiskEngine(cfg)
    sig = _signal(metadata=_spy_call_option_metadata(), asset_class="option")
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
        "positions": {},
        "option_premium_exposure": Decimal("0"),
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert "options_disabled" in decision.checks_failed


def test_options_rejected_short_opening() -> None:
    cfg = _risk_cfg()
    cfg["options_trading"] = {
        "enabled": True,
        "paper_only": False,
        "allowed_underlyings": ["SPY"],
        "max_premium_per_trade": "2000",
        "max_contracts_per_trade": 5,
        "max_total_premium_exposure": "5000",
        "allow_sell_to_close": True,
    }
    engine = RiskEngine(cfg)
    sig = _signal(
        side="sell",
        price="2",
        metadata=_spy_call_option_metadata(),
        asset_class="option",
        symbol="SPY|20260420|C|500",
    )
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
        "positions": {},
        "option_premium_exposure": Decimal("0"),
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.REJECTED
    assert "options_short_opening_rejected" in decision.checks_failed


def test_options_approved_long_call_within_limits() -> None:
    cfg = _risk_cfg()
    cfg["options_trading"] = {
        "enabled": True,
        "paper_only": False,
        "allowed_underlyings": ["SPY"],
        "max_premium_per_trade": "2000",
        "max_contracts_per_trade": 5,
        "max_total_premium_exposure": "5000",
        "allow_sell_to_close": True,
    }
    engine = RiskEngine(cfg)
    sig = _signal(
        qty="1",
        price="2",
        metadata=_spy_call_option_metadata(),
        asset_class="option",
        symbol="SPY|20260420|C|500",
    )
    portfolio = {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
        "positions": {},
        "option_premium_exposure": Decimal("0"),
    }
    decision = engine.evaluate(sig, portfolio)
    assert decision.verdict == RiskVerdict.APPROVED


def test_cooldown_expires_after_elapsed_time() -> None:
    cfg = _risk_cfg()
    cfg["cooldown_minutes"] = 60
    engine = RiskEngine(cfg)
    engine._cooldown_until = datetime.now(timezone.utc)  # immediately expired
    decision = engine.evaluate(
        _signal(),
        {
            "portfolio_value": Decimal("100000"),
            "daily_realized_pnl": Decimal("0"),
            "trades_today": 0,
            "current_gross_exposure": Decimal("0"),
            "symbol_exposure": {},
            "asset_class_exposure": {},
        },
    )
    assert decision.verdict == RiskVerdict.APPROVED
