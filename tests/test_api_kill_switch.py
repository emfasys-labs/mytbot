from __future__ import annotations

from fastapi.testclient import TestClient

from api.server import app
from control.runtime import set_execution_engine, set_risk_engine


class _FakeRiskEngine:
    def __init__(self):
        self.is_killed = False

    def kill(self) -> None:
        self.is_killed = True

    def reset_kill(self) -> None:
        self.is_killed = False


class _FakeExecutionEngine:
    def __init__(self):
        self.cancelled = False
        self._brokers = {"ibkr": object()}

    async def cancel_all(self) -> None:
        self.cancelled = True


def test_kill_endpoint_triggers_kill_and_cancel() -> None:
    risk = _FakeRiskEngine()
    exe = _FakeExecutionEngine()
    set_risk_engine(risk)
    set_execution_engine(exe)
    client = TestClient(app)

    r = client.post("/kill")
    assert r.status_code == 200
    assert r.json()["kill_switch"] is True
    assert risk.is_killed is True
    assert exe.cancelled is True


def test_kill_reset_and_status_reflect_runtime_state() -> None:
    risk = _FakeRiskEngine()
    exe = _FakeExecutionEngine()
    set_risk_engine(risk)
    set_execution_engine(exe)
    client = TestClient(app)

    client.post("/kill")
    status = client.get("/status")
    assert status.status_code == 200
    body = status.json()
    assert body["kill_switch"] is True
    assert "ibkr" in body["connected_brokers"]

    reset = client.post("/kill/reset")
    assert reset.status_code == 200
    assert reset.json()["kill_switch"] is False
    assert risk.is_killed is False

