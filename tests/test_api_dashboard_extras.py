from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import app
from api.server import _command_bus
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


def test_get_control_command_by_id(client: TestClient):
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


def test_get_control_command_404(client: TestClient):
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


def test_healthz_unauthenticated_with_dashboard_token(monkeypatch, client: TestClient):
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "secret-read")
    r = client.get("/healthz")
    assert r.status_code == 200
