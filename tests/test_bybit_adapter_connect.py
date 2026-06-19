"""
Focused tests for ``BybitAdapter.connect`` and lazy wallet detection.

These tests intentionally do not hit the network — they fake out the pybit
``HTTP`` client so we can verify:

* ``connect()`` completes after only the cheap ``get_server_time`` +
  ``get_api_key_information`` calls (no wallet probes).
* ``get_balance()`` lazily resolves the wallet ``accountType`` the first time
  it runs.
* When every ``accountType`` is rejected the adapter latches into
  ``_wallet_unavailable=True`` and stops hammering the endpoint, but remains
  connected so price/order data keeps flowing.
"""

from __future__ import annotations

import pytest

from brokers.bybit.adapter import BybitAdapter


class _StubHTTP:
    """Pybit HTTP stand-in. Every method just records the call."""

    def __init__(self, *, wallet_results: dict[str, object | Exception]) -> None:
        self.wallet_results = wallet_results
        self.calls: list[tuple[str, dict]] = []

    # Public
    def get_server_time(self) -> dict:  # noqa: D401
        self.calls.append(("server_time", {}))
        return {"retCode": 0, "result": {"timeSecond": "1"}}

    # Authenticated
    def get_api_key_information(self) -> dict:
        self.calls.append(("api_key_info", {}))
        return {"retCode": 0, "result": {"id": "stub", "readOnly": 0}}

    def get_wallet_balance(self, **params: object) -> dict:
        account_type = str(params.get("accountType", ""))
        self.calls.append(("wallet_balance", params))
        result = self.wallet_results.get(account_type)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError(
                f"unconfigured accountType {account_type!r} in test stub"
            )
        return result  # type: ignore[return-value]


class _AccountType400Error(Exception):
    """Mimic pybit's FailedRequestError string shape for a 400 accountType rejection."""

    def __str__(self) -> str:  # pragma: no cover — simple passthrough
        return (
            "Bad request. retries exceeded maximum. (ErrCode: 400) "
            "(ErrTime: 00:00:00). Request -> GET /v5/account/wallet-balance: accountType=X"
        )


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    wallet_results: dict[str, object | Exception],
) -> tuple[_StubHTTP, dict]:
    stub = _StubHTTP(wallet_results=wallet_results)
    factory_kwargs: dict = {}

    def _factory(*_a, **_kw) -> _StubHTTP:  # noqa: ANN001 — free form kwargs
        factory_kwargs.update(_kw)
        return stub

    monkeypatch.setattr("brokers.bybit.adapter.HTTP", _factory)
    return stub, factory_kwargs


@pytest.mark.asyncio
async def test_connect_does_not_probe_wallet_account_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() must not touch get_wallet_balance — it should be fast."""
    _install_stub(
        monkeypatch,
        wallet_results={"UNIFIED": {"retCode": 0, "result": {"list": []}}},
    )
    adapter = BybitAdapter(api_key="k", api_secret="s", paper_mode=True)

    ok = await adapter.connect()

    assert ok is True
    assert adapter._private_ok is True
    # wallet_account_type is resolved lazily on first get_balance().
    assert adapter._wallet_account_type is None
    assert adapter._wallet_unavailable is False


@pytest.mark.asyncio
async def test_connect_passes_configurable_recv_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYBIT_RECV_WINDOW_MS", "15000")
    _stub, factory_kwargs = _install_stub(
        monkeypatch,
        wallet_results={"UNIFIED": {"retCode": 0, "result": {"list": []}}},
    )
    adapter = BybitAdapter(api_key="k", api_secret="s", paper_mode=True)

    ok = await adapter.connect()

    assert ok is True
    assert factory_kwargs["recv_window"] == 15000


@pytest.mark.asyncio
async def test_get_balance_resolves_wallet_account_type_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(
        monkeypatch,
        wallet_results={
            "UNIFIED": _AccountType400Error(),
            "CONTRACT": {
                "retCode": 0,
                "result": {"list": [{"coin": [{"coin": "USDT", "walletBalance": "0", "availableBalance": "0"}]}]},
            },
            "SPOT": _AccountType400Error(),
            "FUND": _AccountType400Error(),
        },
    )
    adapter = BybitAdapter(api_key="k", api_secret="s", paper_mode=True)
    await adapter.connect()

    await adapter.get_balance()

    assert adapter._wallet_account_type == "CONTRACT"
    assert adapter._wallet_unavailable is False


@pytest.mark.asyncio
async def test_get_balance_latches_unavailable_when_all_account_types_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub(
        monkeypatch,
        wallet_results={
            "UNIFIED": _AccountType400Error(),
            "CONTRACT": _AccountType400Error(),
            "SPOT": _AccountType400Error(),
            "FUND": _AccountType400Error(),
        },
    )
    adapter = BybitAdapter(api_key="k", api_secret="s", paper_mode=True)
    await adapter.connect()

    first = await adapter.get_balance()
    assert first == []
    assert adapter._wallet_unavailable is True

    # Subsequent calls must return fast without re-probing any accountType.
    second = await adapter.get_balance()
    assert second == []

    stub = adapter._client  # type: ignore[assignment]
    # Exactly one probe round (UNIFIED, CONTRACT, SPOT, FUND) should have happened
    # despite two get_balance() calls.
    probe_calls = [c for c in stub.calls if c[0] == "wallet_balance"]  # type: ignore[attr-defined]
    assert len(probe_calls) == 4
