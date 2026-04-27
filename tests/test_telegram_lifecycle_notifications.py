from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.base import Balance
from system.telegram_notify import send_lifecycle_notification


class _Adapter:
    def __init__(self, balances: list[Balance]) -> None:
        self._balances = balances

    async def get_balance(self) -> list[Balance]:
        return list(self._balances)


class _Report:
    @property
    def included_names(self) -> list[str]:
        return ["ibkr", "bybit"]

    def coverage(self) -> dict:
        return {
            "full": False,
            "configured": ["ibkr", "bybit", "kraken"],
            "included": ["ibkr", "bybit"],
            "excluded": [{"name": "kraken", "reason": "offline"}],
        }


class _BrokerManager:
    def __init__(self) -> None:
        self.adapters = {
            "ibkr": _Adapter([Balance("BASE", Decimal("1000000"), Decimal("1000000"), Decimal("0"))]),
            "bybit": _Adapter([]),
        }
        self.report = _Report()


@pytest.mark.asyncio
async def test_lifecycle_notification_includes_nav_and_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    async def _fake_send(message: str) -> None:
        captured.append(message)

    monkeypatch.setattr("system.telegram_notify._send_telegram", _fake_send)

    bm = _BrokerManager()
    await send_lifecycle_notification(
        "started",
        broker_manager=bm,
        broker_report=bm.report,
        paper_mode=True,
    )

    assert len(captured) == 1
    msg = captured[0]
    assert "SYSTEM STARTED (PAPER)" in msg
    assert "NAV: 1,000,000.00" in msg
    assert "Included: ibkr, bybit" in msg
    assert "Excluded: kraken" in msg
