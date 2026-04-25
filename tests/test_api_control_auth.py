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


class _Orchestrator:
    capital_pct = 1.0

    async def start(self):
        return {"state": "running"}

    async def stop(self):
        return {"state": "off"}

    def set_capital_pct(self, pct: float):
        self.capital_pct = pct


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


def test_system_mutations_require_token_when_configured(monkeypatch):
    monkeypatch.setattr("api.server.MUTATION_TOKEN", "abc123")
    monkeypatch.setattr("api.server._get_orchestrator", lambda: _Orchestrator())
    monkeypatch.setattr("api.server._write_active_mode", lambda mode: None)
    c = TestClient(app)

    cases = [
        ("post", "/system/start", {}),
        ("post", "/system/stop", {}),
        ("put", "/system/capital-allocation", {"json": {"pct": 0.5}}),
        ("post", "/system/mode", {"json": {"mode": "hunter"}}),
    ]
    for method, path, kwargs in cases:
        r = getattr(c, method)(path, **kwargs)
        assert r.status_code == 401, path
        ok = getattr(c, method)(path, headers={"X-Control-Token": "abc123"}, **kwargs)
        assert ok.status_code == 200, path
