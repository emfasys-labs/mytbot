from __future__ import annotations

import pytest

from execution.engine import ExecutionEngine


@pytest.mark.asyncio
async def test_send_critical_alert_is_suppressed_under_pytest(monkeypatch) -> None:
    # Ensure env is present even if test runner changes behavior.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("httpx.AsyncClient should not be constructed under pytest")

    # Patch httpx client used by ExecutionEngine.
    monkeypatch.setattr("execution.engine.httpx.AsyncClient", _BoomClient)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    await engine._send_critical_alert("should not send")

