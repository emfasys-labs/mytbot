"""IBKR connect rescue when ib_insync times out after a live API handshake."""

from __future__ import annotations

import pytest

from brokers.ibkr.adapter import IBKRAdapter


class _FakeErrorEvent:
    def __iadd__(self, _handler):
        return self

    def __isub__(self, _handler):
        return self


class _FakeIBConnectedAfterTimeout:
    MaxSyncedSubAccounts = 0

    def __init__(self) -> None:
        self.errorEvent = _FakeErrorEvent()
        self.disconnected = False

    def isConnected(self) -> bool:  # noqa: N802 - mirrors ib_insync API
        return True

    async def connectAsync(self, **_kwargs):  # noqa: N802 - mirrors ib_insync API
        raise TimeoutError("startup sync timed out")

    def managedAccounts(self):  # noqa: N802 - mirrors ib_insync API
        return ["DUP694288"]

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_ibkr_connect_rescues_connected_session_after_startup_sync_timeout(monkeypatch) -> None:
    fake = _FakeIBConnectedAfterTimeout()
    monkeypatch.setattr("brokers.ibkr.adapter.IB", lambda: fake)

    adapter = IBKRAdapter(account_id="", paper_mode=True)

    assert await adapter.connect() is True
    assert adapter._ib is fake
    assert fake.disconnected is False
    assert adapter._last_connect_error is None

