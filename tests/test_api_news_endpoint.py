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


class _SeqSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            return _ScalarResult([])
        return self._results.pop(0)


class _Factory:
    def __init__(self, results):
        self._results = results

    def __call__(self):
        s = _SeqSession(self._results)

        class _CM:
            async def __aenter__(self_inner):
                return s

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _CM()


def test_news_impactful_only_returns_ai_linked_headlines() -> None:
    now = datetime.now(timezone.utc)
    signal_symbols = _ScalarResult(["SPY"])
    ai_rows = _ScalarResult(
        [
            SimpleNamespace(
                symbol="SPY",
                score=Decimal("0.42"),
                confidence=Decimal("0.75"),
                event_type="earnings",
                rationale="Earnings beat supports momentum",
                timestamp=now,
                payload={"headline": "SPY jumps after earnings surprise", "provider": "local_llm"},
                source="local",
            )
        ]
    )
    app.dependency_overrides[_session_factory] = lambda: _Factory([signal_symbols, ai_rows])
    try:
        client = TestClient(app)
        r = client.get("/news?limit=10&impactful_only=true")
        assert r.status_code == 200
        body = r.json()
        assert len(body["headlines"]) == 1
        assert body["headlines"][0]["title"] == "SPY jumps after earnings surprise"
        assert body["headlines"][0]["source"] == "local_llm"
    finally:
        app.dependency_overrides.pop(_session_factory, None)

