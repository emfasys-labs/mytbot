from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import app
from api.server import _command_bus
from system.dashboard_publish import DASHBOARD_SNAPSHOT_KEY


class _FakeBus:
    def __init__(self, row=None):
        self._row = row

    async def get_command(self, command_id: int):
        _ = command_id
        return self._row


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_login_ok(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "p")
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "tok")
    r = client.post("/auth/dashboard/login", json={"password": "p"})
    assert r.status_code == 200
    assert r.json()["token"] == "tok"


def test_dashboard_login_bad_password(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "p")
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "tok")
    r = client.post("/auth/dashboard/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_get_control_command_by_id(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "")
    row = SimpleNamespace(
        id=42,
        command_type="kill",
        payload={},
        status="done",
        created_at=datetime.now(timezone.utc),
        claimed_at=None,
        processed_at=datetime.now(timezone.utc),
        error=None,
        source="api",
    )

    def override_bus():
        return _FakeBus(row)

    app.dependency_overrides[_command_bus] = override_bus
    try:
        r = client.get("/control/commands/42")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == 42
        assert data["type"] == "kill"
        assert data["status"] == "done"
    finally:
        app.dependency_overrides.pop(_command_bus, None)


def test_get_control_command_404(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "")
    def override_bus():
        return _FakeBus(None)

    app.dependency_overrides[_command_bus] = override_bus
    try:
        r = client.get("/control/commands/999")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(_command_bus, None)


def test_read_requires_dashboard_token_when_set(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "secret-read")
    r = client.get("/status")
    assert r.status_code == 401
    ok = client.get("/status", headers={"X-Dashboard-Token": "secret-read"})
    assert ok.status_code == 200


def test_spa_shell_get_allowed_without_token_when_read_token_set(monkeypatch, client: TestClient):
    """Browser open of / has no X-Dashboard-Token — must still load the SPA shell."""
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "secret-read")
    r = client.get("/")
    assert r.status_code != 401


def test_healthz_unauthenticated_with_dashboard_token(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "secret-read")
    r = client.get("/healthz")
    assert r.status_code == 200


class _FakeBusSnapshot:
    async def get_state(self, key: str, default=None):
        if key == DASHBOARD_SNAPSHOT_KEY:
            return {"updated_at": "2026-04-12T00:00:00+00:00", "path": "d015", "fingerprint": "abc"}
        return default


def test_dashboard_snapshot_ok(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "tok")

    def override_bus():
        return _FakeBusSnapshot()

    app.dependency_overrides[_command_bus] = override_bus
    try:
        r = client.get("/dashboard/snapshot", headers={"X-Dashboard-Token": "tok"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("path") == "d015"
        assert data.get("fingerprint") == "abc"
    finally:
        app.dependency_overrides.pop(_command_bus, None)


def test_pnl_includes_week_month_metrics(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "tok")
    r = client.get("/pnl", headers={"X-Dashboard-Token": "tok"})
    if r.status_code == 503:
        pytest.skip("database not available in test environment")
    assert r.status_code == 200
    data = r.json()
    assert "week" in data
    assert "month" in data
    assert "period_start" in data["week"]
    assert "metrics" in data
    assert "win_rate_days" in data["metrics"]
    assert "max_drawdown_pct" in data["metrics"]
