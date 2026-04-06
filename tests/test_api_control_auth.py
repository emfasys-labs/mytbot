from __future__ import annotations

from fastapi.testclient import TestClient

from api.server import app
from control.runtime import set_execution_engine, set_risk_engine


class _Risk:
    def __init__(self):
        self.is_killed = False

    def kill(self):
        self.is_killed = True

    def reset_kill(self):
        self.is_killed = False


class _Exec:
    async def cancel_all(self):
        return None


def test_kill_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr("api.server.MUTATION_TOKEN", "abc123")
    set_risk_engine(_Risk())
    set_execution_engine(_Exec())
    c = TestClient(app)
    r = c.post("/kill")
    assert r.status_code == 401
    ok = c.post("/kill", headers={"X-Control-Token": "abc123"})
    assert ok.status_code == 200


def test_strategy_toggle_needs_command_bus_when_no_db(monkeypatch):
    monkeypatch.setattr("api.server.MUTATION_TOKEN", "")
    c = TestClient(app)
    r = c.post("/strategy/momentum_breakout/toggle", json={"enabled": False})
    assert r.status_code == 503
