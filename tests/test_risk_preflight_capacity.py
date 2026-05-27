from __future__ import annotations

from decimal import Decimal

from risk.engine import RiskEngine, Signal


def _signal(
    *,
    symbol: str = "BF-B",
    side: str = "buy",
    qty: str = "1000",
    price: str = "25",
    asset_class: str = "equity",
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        signal_id=f"sig-{symbol}-{side}",
        symbol=symbol,
        side=side,
        strategy="mean_reversion",
        confidence=0.8,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="ibkr",
        asset_class=asset_class,
        timestamp="2026-05-27T12:00:00Z",
        metadata=metadata or {},
    )


def _portfolio() -> dict:
    return {
        "portfolio_value": "1000000",
        "tradable_capital": "1000000",
        "positions": {
            "BF-B": {
                "symbol": "BF-B",
                "quantity": "2000",
                "avg_entry_price": "25",
                "current_price": "25",
                "asset_class": "equity",
            }
        },
    }


def test_preflight_uses_single_name_gate_without_mutating_original_signal() -> None:
    engine = RiskEngine(
        {
            "single_name_notional": {"enabled": True, "max_pct_nav": "0.05"},
            "intraday_symbol_adds": {"enabled": False},
            "fx_cluster": {"enabled": False},
            "crypto_cluster": {"enabled": False},
            "equity_index_cluster": {"enabled": False},
        }
    )
    sig = _signal(qty="1000", price="25")

    decision = engine.preflight_capacity(sig, _portfolio())

    assert decision.ok is False
    assert decision.reason == "single_name_notional"
    assert sig.suggested_quantity == Decimal("1000")


def test_preflight_reports_clamped_effective_notional_when_room_exists() -> None:
    engine = RiskEngine(
        {
            "single_name_notional": {"enabled": True, "max_pct_nav": "0.06"},
            "intraday_symbol_adds": {"enabled": False},
            "fx_cluster": {"enabled": False},
            "crypto_cluster": {"enabled": False},
            "equity_index_cluster": {"enabled": False},
        }
    )
    sig = _signal(qty="1000", price="25")

    decision = engine.preflight_capacity(sig, _portfolio())

    assert decision.ok is True
    assert decision.reason == "preflight_capacity_ok"
    assert decision.effective_notional == Decimal("10000")
    assert sig.suggested_quantity == Decimal("1000")


def test_preflight_never_blocks_reduce_only_capacity() -> None:
    engine = RiskEngine(
        {
            "single_name_notional": {"enabled": True, "max_pct_nav": "0.01"},
            "intraday_symbol_adds": {"enabled": True, "max_pct_nav": "0.01"},
            "fx_cluster": {"enabled": True, "max_usd_directional_exposure_pct": "0.01"},
        }
    )
    sig = _signal(qty="1000", price="25", metadata={"reduce_only": True})

    decision = engine.preflight_capacity(sig, _portfolio())

    assert decision.ok is True
    assert decision.checks_failed == []
