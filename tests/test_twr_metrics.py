from decimal import Decimal
from types import SimpleNamespace

from api.pnl_periods import time_weighted_return_from_daily_rows


def _row(date: str, nav: str, realised: str = "0", fees: str = "0", unreal: str = "0"):
    return SimpleNamespace(
        date=date,
        portfolio_value=Decimal(nav),
        realised_pnl=Decimal(realised),
        total_fees=Decimal(fees),
        unrealised_pnl=Decimal(unreal),
    )


def test_twr_ignores_external_broker_balance_jump() -> None:
    out = time_weighted_return_from_daily_rows(
        [
            _row("2026-06-18", "1000"),
            # NAV jumps by 200, but trading P&L is zero: external flow, not return.
            _row("2026-06-19", "1200"),
        ]
    )

    assert out["twr_pct"] == 0.0
    assert out["external_flow"] == "200"


def test_twr_counts_loss_after_external_flow_against_active_capital() -> None:
    out = time_weighted_return_from_daily_rows(
        [
            _row("2026-06-18", "1000"),
            # +200 external flow and -10 trading P&L -> ending NAV 1190.
            _row("2026-06-19", "1190", realised="-10"),
        ]
    )

    assert round(out["twr_pct"], 4) == -0.8333
    assert out["external_flow"] == "200"
    assert out["net_trading_pnl"] == "-10"


def test_twr_includes_unrealised_mark_change() -> None:
    out = time_weighted_return_from_daily_rows(
        [
            _row("2026-06-18", "1000", unreal="0"),
            _row("2026-06-19", "1015", unreal="15"),
        ]
    )

    assert round(out["twr_pct"], 4) == 1.5
    assert out["net_trading_pnl"] == "15"
