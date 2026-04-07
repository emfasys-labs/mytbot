from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.server import _session_factory, app


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _ScalarResult(self._rows)


class _FakeFactory:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        s = _FakeSession(self._rows)

        class _CM:
            async def __aenter__(self_inner):
                return s

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _CM()


def test_discovery_anomalies_endpoint() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            timestamp=datetime.now(timezone.utc),
            symbol="SPY",
            asset_class="etf",
            direction="up",
            price_move_pct=Decimal("1.23"),
            price_z_score=Decimal("2.34"),
            volume_z_score=Decimal("1.11"),
            news_velocity=Decimal("3.00"),
            news_sentiment=Decimal("0.20"),
            anomaly_score=Decimal("0.77"),
            opportunities_found=4,
            thesis_generated=True,
            signals_produced=2,
        )
    ]

    app.dependency_overrides[_session_factory] = lambda: _FakeFactory(rows)
    try:
        client = TestClient(app)
        r = client.get("/discovery/anomalies")
        assert r.status_code == 200
        body = r.json()
        assert "anomalies" in body
        assert body["anomalies"][0]["symbol"] == "SPY"
    finally:
        app.dependency_overrides.pop(_session_factory, None)


def test_discovery_theses_endpoint() -> None:
    rows = [
        SimpleNamespace(
            id=9,
            timestamp=datetime.now(timezone.utc),
            trigger_symbol="vix",
            trigger_direction="up",
            trigger_explanation="Risk-off move",
            overall_confidence=Decimal("0.81"),
            time_horizon_hours=12,
            opportunities=[{"symbol": "GLD", "direction": "up"}],
            invalidation_conditions=["VIX mean reversion"],
            model_used="stub_dependency_graph",
            tokens_used=0,
            ai_cost_usd=Decimal("0.000000"),
        )
    ]
    app.dependency_overrides[_session_factory] = lambda: _FakeFactory(rows)
    try:
        client = TestClient(app)
        r = client.get("/discovery/theses")
        assert r.status_code == 200
        body = r.json()
        assert "theses" in body
        assert body["theses"][0]["trigger_symbol"] == "vix"
    finally:
        app.dependency_overrides.pop(_session_factory, None)
