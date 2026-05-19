from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.server import app
from system.connect_hub import add_connector_manifest, build_connect_hub_snapshot, load_connector_manifests, update_env_file


def test_connector_manifest_secret_state_without_secret_values(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "connectors.yaml"
    cfg.write_text(
        """
brokers:
  demo:
    label: Demo Broker
    enabled: true
    required_secrets:
      - env: DEMO_API_KEY
        label: API key
    capabilities:
      can_trade: true
information_feeds: {}
ai_providers: {}
treasury_accounts: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEMO_API_KEY", "super-secret-value")

    rows = load_connector_manifests(cfg)
    assert len(rows) == 1
    snap = build_connect_hub_snapshot(config_path=cfg)
    demo = snap["categories"]["brokers"][0]

    assert demo["configured"] is True
    assert demo["required_secrets"] == [
        {"env": "DEMO_API_KEY", "label": "API key", "required": True, "configured": True}
    ]
    assert demo["next_actions"][0]["kind"] == "start_system"
    assert "super-secret-value" not in str(demo)


def test_connect_hub_adapts_to_broker_and_feed_status(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "connectors.yaml"
    cfg.write_text(
        """
brokers:
  alpaca:
    label: Alpaca
    enabled: true
    required_secrets:
      - env: ALPACA_API_KEY
      - env: ALPACA_API_SECRET
    capabilities:
      can_trade: true
information_feeds:
  fred:
    label: FRED
    enabled: true
    required_secrets:
      - env: FRED_API_KEY
    capabilities:
      can_ingest_macro: true
ai_providers: {}
treasury_accounts:
  treasury:
    label: Treasury
    enabled: false
    required_secrets:
      - env: TREASURY_API_KEY
    capabilities:
      can_initiate_transfer: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-key")

    class _Orch:
        def status(self) -> dict:
            return {
                "brokers": {
                    "alpaca": {
                        "configured": True,
                        "connected": True,
                        "balance_ready": True,
                        "error": None,
                    }
                }
            }

    snap = build_connect_hub_snapshot(
        orchestrator=_Orch(),
        news_data_providers=[
            {
                "id": "fred",
                "label": "FRED",
                "configured": True,
                "state": "live",
                "last_ingest_at": "2026-05-19T00:00:00+00:00",
                "age_label": "1h ago",
                "ok": True,
                "error": None,
            }
        ],
        config_path=cfg,
    )

    alpaca = snap["categories"]["brokers"][0]
    fred = snap["categories"]["information_feeds"][0]
    treasury = snap["categories"]["treasury_accounts"][0]
    assert alpaca["state"] == "connected"
    assert alpaca["healthy"] is True
    assert fred["state"] == "live"
    assert snap["capability_flags"]["can_trade"] is True
    assert snap["capability_flags"]["has_information_feed"] is True
    assert treasury["enabled"] is False
    assert snap["capability_flags"]["can_auto_transfer"] is False


def test_connect_hub_endpoint_returns_categories(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "")
    monkeypatch.setattr("api.server._get_orchestrator", lambda: None)

    client = TestClient(app)
    r = client.get("/connect/hub")

    assert r.status_code == 200
    data = r.json()
    assert "brokers" in data["categories"]
    assert "information_feeds" in data["categories"]
    assert "ai_providers" in data["categories"]
    assert "treasury_accounts" in data["categories"]
    assert data["capability_flags"]["can_auto_transfer"] is False


def test_update_env_file_upserts_without_exposing_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=old\nKEEP_ME=1\n", encoding="utf-8")

    written = update_env_file(
        {"ALPACA_API_KEY": "new value", "ALPACA_API_SECRET": "secret"},
        env_path=env,
    )

    text = env.read_text(encoding="utf-8")
    assert written == ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
    assert 'ALPACA_API_KEY="new value"' in text
    assert "ALPACA_API_SECRET=secret" in text
    assert "KEEP_ME=1" in text


def test_configure_connector_endpoint_writes_only_manifest_secrets(monkeypatch) -> None:
    from api.server import _command_bus

    monkeypatch.setenv("DASHBOARD_READ_TOKEN", "")

    calls: dict[str, object] = {}

    def fake_update_env_file(updates):
        calls["updates"] = updates
        return sorted(updates)

    def fake_set_connector_enabled(**kwargs):
        calls["enabled"] = kwargs

    monkeypatch.setattr("system.connect_hub.update_env_file", fake_update_env_file)
    monkeypatch.setattr("system.connect_hub.set_connector_enabled", fake_set_connector_enabled)

    def override_bus():
        class B:
            async def get_state(self, key: str, default=None):
                return default

        return B()

    app.dependency_overrides[_command_bus] = override_bus
    try:
        client = TestClient(app)
        r = client.post(
            "/connect/configure",
            json={
                "category": "brokers",
                "connector_id": "alpaca",
                "secrets": {"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"},
                "enable": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["written_env"] == ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
        assert calls["updates"] == {"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}

        bad = client.post(
            "/connect/configure",
            json={
                "category": "brokers",
                "connector_id": "alpaca",
                "secrets": {"NOT_ALLOWED": "x"},
            },
        )
        assert bad.status_code == 400
    finally:
        app.dependency_overrides.pop(_command_bus, None)


def test_add_connector_manifest_creates_custom_feed(tmp_path: Path) -> None:
    cfg = tmp_path / "connectors.yaml"
    cfg.write_text("brokers: {}\ninformation_feeds: {}\nai_providers: {}\ntreasury_accounts: {}\n", encoding="utf-8")

    created = add_connector_manifest(
        category="information_feeds",
        connector_id="My Premium Feed",
        label="My Premium Feed",
        required_env=["MY_PREMIUM_FEED_KEY"],
        capabilities={"can_ingest_news": True},
        path=cfg,
    )

    assert created["id"] == "my_premium_feed"
    rows = load_connector_manifests(cfg)
    assert any(r.id == "my_premium_feed" and r.category == "information_feeds" for r in rows)
