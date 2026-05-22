"""D127 Connect Hub v2 — Phase 1 tests.

Covers the connector lifecycle state machine, the connector_state store,
and the broker/feed capability probe.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.models import Base
from connectors import lifecycle as lc
from connectors.lifecycle import StatusInputs, resolve_status, can_transition, is_usable
from connectors.capability_probe import probe_connector
from connectors.state_store import upsert_state, load_state, load_all_states


# ── lifecycle state machine ───────────────────────────────────────────────────


def test_resolve_disabled_when_not_enabled():
    s = resolve_status(StatusInputs(
        enabled=False, credentials_complete=True, has_any_credential=True,
        test_ok=True,
    ))
    assert s == lc.DISABLED


def test_resolve_not_configured_when_no_credentials():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=False, has_any_credential=False,
        test_ok=None,
    ))
    assert s == lc.NOT_CONFIGURED


def test_resolve_needs_credentials_when_partial_credentials():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=False, has_any_credential=True,
        test_ok=None,
    ))
    assert s == lc.NEEDS_CREDENTIALS


def test_resolve_testing_when_configured_but_untested():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=True, has_any_credential=True,
        test_ok=None,
    ))
    assert s == lc.TESTING


def test_resolve_connected_on_passing_test():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=True, has_any_credential=True,
        test_ok=True, test_partial=False,
    ))
    assert s == lc.CONNECTED


def test_resolve_connected_limited_on_partial_test():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=True, has_any_credential=True,
        test_ok=True, test_partial=True,
    ))
    assert s == lc.CONNECTED_LIMITED


def test_resolve_error_on_failing_test():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=True, has_any_credential=True,
        test_ok=False,
    ))
    assert s == lc.ERROR


def test_resolve_unsupported_in_live_for_paper_only():
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=True, has_any_credential=True,
        test_ok=True, paper_only=True, system_live_mode=True,
    ))
    assert s == lc.UNSUPPORTED_IN_LIVE


def test_resolve_no_auth_connector_skips_credential_gate():
    # Rules engine: auth_required=False — never blocked on credentials.
    s = resolve_status(StatusInputs(
        enabled=True, credentials_complete=False, has_any_credential=False,
        test_ok=True, auth_required=False,
    ))
    assert s == lc.CONNECTED


def test_transition_legal_and_illegal():
    assert can_transition(lc.TESTING, lc.CONNECTED) is True
    assert can_transition(lc.CONNECTED, lc.DISABLED) is True
    assert can_transition(lc.DISABLED, lc.TESTING) is True
    assert can_transition(lc.CONNECTED, lc.CONNECTED) is True       # no-op legal
    assert can_transition(lc.NOT_CONFIGURED, lc.CONNECTED) is False  # must test first
    assert can_transition(lc.ERROR, lc.CONNECTED) is False           # must re-test


def test_is_usable():
    assert is_usable(lc.CONNECTED) is True
    assert is_usable(lc.CONNECTED_LIMITED) is True
    assert is_usable(lc.ERROR) is False
    assert is_usable(lc.DISABLED) is False
    assert is_usable(lc.TESTING) is False


# ── connector_state store ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_state_store_insert_and_load(sf):
    saved = await upsert_state(
        sf, category="brokers", connector_id="kraken",
        status=lc.CONNECTED, enabled=True,
        detected_capabilities={"can_trade": True, "can_withdraw": False},
    )
    assert saved is not None
    assert saved["status"] == lc.CONNECTED
    loaded = await load_state(sf, "brokers", "kraken")
    assert loaded["detected_capabilities"]["can_trade"] is True
    assert loaded["detected_capabilities"]["can_withdraw"] is False


@pytest.mark.asyncio
async def test_state_store_upsert_updates_in_place(sf):
    await upsert_state(sf, category="brokers", connector_id="ibkr",
                       status=lc.TESTING, enabled=True)
    await upsert_state(sf, category="brokers", connector_id="ibkr",
                       status=lc.CONNECTED)
    loaded = await load_state(sf, "brokers", "ibkr")
    assert loaded["status"] == lc.CONNECTED
    assert loaded["enabled"] is True                 # preserved — not overwritten
    alls = await load_all_states(sf)
    assert len(alls) == 1                            # upsert, not insert


@pytest.mark.asyncio
async def test_state_store_load_missing_returns_none(sf):
    assert await load_state(sf, "brokers", "nonexistent") is None


# ── capability probe ──────────────────────────────────────────────────────────


class _FakeSecret:
    def __init__(self, env, required=True, configured=True):
        self.env = env
        self.required = required
        self.configured = configured


class _FakeManifest:
    def __init__(self, *, id, category, auth_type="api_key",
                 required_secrets=(), capabilities=None):
        self.id = id
        self.category = category
        self.auth_type = auth_type
        self.required_secrets = required_secrets
        self.capabilities = capabilities or {}


class _FakeOrchestrator:
    def __init__(self, brokers):
        self._brokers = brokers

    def status(self):
        return {"brokers": self._brokers}


def test_probe_broker_missing_credentials():
    m = _FakeManifest(
        id="kraken", category="brokers",
        required_secrets=(_FakeSecret("KRAKEN_API_KEY", configured=False),),
        capabilities={"can_trade": True},
    )
    r = probe_connector(category="brokers", manifest=m, orchestrator=None)
    assert r.ok is False
    assert "credential" in r.reason.lower()


def test_probe_broker_connected_detects_capabilities():
    m = _FakeManifest(
        id="kraken", category="brokers",
        required_secrets=(_FakeSecret("KRAKEN_API_KEY"),),
        capabilities={"can_trade": True, "can_read_balance": True, "supports_paper": True},
    )
    orc = _FakeOrchestrator({"kraken": {"connected": True, "balance_ready": True}})
    r = probe_connector(category="brokers", manifest=m, orchestrator=orc)
    assert r.ok is True and r.partial is False
    assert r.detected_capabilities["can_trade"] is True
    assert r.detected_capabilities["can_read_balance"] is True
    assert r.detected_capabilities["can_withdraw"] is False     # always off


def test_probe_broker_connected_but_balance_not_ready_is_partial():
    m = _FakeManifest(
        id="ibkr", category="brokers", auth_type="gateway",
        capabilities={"can_trade": True, "can_read_balance": True},
    )
    orc = _FakeOrchestrator({"ibkr": {"connected": True, "balance_ready": False}})
    r = probe_connector(category="brokers", manifest=m, orchestrator=orc)
    assert r.ok is True and r.partial is True
    assert r.detected_capabilities["can_read_balance"] is False


def test_probe_broker_not_connected():
    m = _FakeManifest(
        id="ibkr", category="brokers", auth_type="gateway",
        capabilities={"can_trade": True},
    )
    orc = _FakeOrchestrator({"ibkr": {"connected": False, "error": "gateway down"}})
    r = probe_connector(category="brokers", manifest=m, orchestrator=orc)
    assert r.ok is False
    assert "gateway down" in r.reason


def test_probe_feed_live():
    m = _FakeManifest(
        id="newsapi", category="information_feeds",
        required_secrets=(_FakeSecret("NEWS_API_KEY"),),
        capabilities={"can_ingest_news": True},
    )
    r = probe_connector(
        category="information_feeds", manifest=m,
        news_provider_statuses=[{"id": "newsapi", "state": "live"}],
    )
    assert r.ok is True and r.partial is False
    assert r.detected_capabilities["can_ingest_news"] is True


def test_probe_feed_stale_is_partial():
    m = _FakeManifest(
        id="finnhub", category="information_feeds",
        required_secrets=(_FakeSecret("FINNHUB_API_KEY"),),
        capabilities={"can_ingest_news": True},
    )
    r = probe_connector(
        category="information_feeds", manifest=m,
        news_provider_statuses=[{"id": "finnhub", "state": "stale"}],
    )
    assert r.ok is True and r.partial is True


def test_probe_unsupported_category_phase1():
    m = _FakeManifest(id="external_treasury", category="treasury_accounts")
    r = probe_connector(category="treasury_accounts", manifest=m)
    assert r.ok is False
    assert "later" in r.reason.lower() or "phase" in r.reason.lower()


# ── P2: certification tiers + risk-engine gate ────────────────────────────────

from connectors import certification as cert  # noqa: E402


class _CertManifest:
    def __init__(self, *, id="x", category="brokers", certification="experimental",
                 capabilities=None):
        self.id = id
        self.category = category
        self.certification = certification
        self.capabilities = capabilities or {}


def test_resolve_tier():
    assert cert.resolve_tier(_CertManifest(certification="certified")) == cert.CERTIFIED
    assert cert.resolve_tier(_CertManifest(certification="experimental")) == cert.EXPERIMENTAL
    assert cert.resolve_tier(_CertManifest(certification="")) == cert.EXPERIMENTAL
    assert cert.resolve_tier(None) == cert.EXPERIMENTAL          # fail-closed


def test_may_execute_certified_allowed():
    m = _CertManifest(certification="certified",
                      capabilities={"supports_paper": True, "supports_live": True})
    allowed, reason = cert.may_execute(m, system_live_mode=False)
    assert allowed is True and reason == "certified"


def test_may_execute_experimental_blocked():
    m = _CertManifest(certification="experimental")
    allowed, reason = cert.may_execute(m)
    assert allowed is False and reason == "broker_not_certified"


def test_may_execute_paper_only_blocked_in_live():
    m = _CertManifest(certification="certified",
                      capabilities={"supports_paper": True, "supports_live": False})
    allowed_paper, _ = cert.may_execute(m, system_live_mode=False)
    allowed_live, reason = cert.may_execute(m, system_live_mode=True)
    assert allowed_paper is True
    assert allowed_live is False and reason == "broker_unsupported_in_live"


def test_broker_execution_decision_real_catalogue_brokers():
    # The 5 production brokers are marked certified in connectors.yaml.
    for b in ("ibkr", "kraken", "binance", "bybit", "alpaca"):
        allowed, reason = cert.broker_execution_decision(b)
        assert allowed is True, f"{b}: {reason}"


def test_broker_execution_decision_unknown_fails_open():
    allowed, reason = cert.broker_execution_decision("totally_unknown_broker")
    assert allowed is True                       # fail-open — no manifest, no adapter
    assert reason == "broker_not_in_catalogue"


# ── risk-engine certification gate ────────────────────────────────────────────

def _cert_signal(*, broker="ibkr", side="buy", reduce_only=False):
    from risk.engine import Signal
    from decimal import Decimal
    return Signal(
        signal_id=f"c-{broker}-{side}",
        symbol="AAPL",
        side=side,
        strategy="momentum_breakout",
        confidence=0.9,
        suggested_quantity=Decimal("10"),
        suggested_price=Decimal("100"),
        broker=broker,
        asset_class="equity",
        timestamp="2026-05-22T12:00:00+00:00",
        metadata={"reduce_only": reduce_only} if reduce_only else {},
    )


def test_risk_gate_certified_broker_passes():
    from risk.engine import RiskEngine
    eng = RiskEngine({"connector_certification": {"enforce": True}})
    ok, label = eng._check_broker_certification(_cert_signal(broker="ibkr"), {})
    assert ok is True and label == "broker_certification"


def test_risk_gate_enforcement_off_passes_anything():
    from risk.engine import RiskEngine
    eng = RiskEngine({"connector_certification": {"enforce": False}})
    ok, _ = eng._check_broker_certification(_cert_signal(broker="anything"), {})
    assert ok is True


def test_risk_gate_blocks_uncertified(monkeypatch):
    from risk.engine import RiskEngine
    monkeypatch.setattr(
        "connectors.certification.broker_execution_decision",
        lambda broker_id, **kw: (False, "broker_not_certified"),
    )
    eng = RiskEngine({"connector_certification": {"enforce": True}})
    ok, label = eng._check_broker_certification(_cert_signal(broker="some_experimental"), {})
    assert ok is False
    assert label.startswith("broker_certification:")


def test_risk_gate_reduce_only_exempt(monkeypatch):
    """Even an uncertified broker must allow a reduce-only exit."""
    from risk.engine import RiskEngine
    monkeypatch.setattr(
        "connectors.certification.broker_execution_decision",
        lambda broker_id, **kw: (False, "broker_not_certified"),
    )
    eng = RiskEngine({"connector_certification": {"enforce": True}})
    ok, _ = eng._check_broker_certification(
        _cert_signal(broker="some_experimental", side="sell", reduce_only=True), {}
    )
    assert ok is True                            # exits always allowed


# ── P3: AI pipeline as managed stages ─────────────────────────────────────────

from connectors.ai_pipeline import build_ai_pipeline_view, can_disable_ai_stage, STAGE_ORDER  # noqa: E402


def test_ai_pipeline_has_four_fixed_stages_in_order():
    view = build_ai_pipeline_view()
    ids = [s["id"] for s in view["stages"]]
    assert ids == list(STAGE_ORDER)
    assert ids == ["rules", "fin_sentiment", "local_reasoning", "premium_fallback"]
    assert view["stage_count"] == 4
    # Every stage carries its escalation order and is never deletable.
    for i, s in enumerate(view["stages"], start=1):
        assert s["order"] == i
        assert s["can_delete"] is False


def test_ai_pipeline_rules_is_core_and_locked():
    view = build_ai_pipeline_view()
    rules = view["stages"][0]
    assert rules["id"] == "rules"
    assert rules["core"] is True
    assert rules["can_disable"] is False


def test_can_disable_rules_always_false():
    ok, reason = can_disable_ai_stage("rules")
    assert ok is False
    assert "core" in reason.lower()


def test_can_disable_finbert_blocked_when_sole_sentiment_provider():
    # In the shipped config FinBERT is the only sentiment_classifier.
    ok, reason = can_disable_ai_stage("fin_sentiment")
    assert ok is False
    assert "sentiment" in reason.lower()


def test_can_disable_local_and_premium_allowed():
    assert can_disable_ai_stage("local_reasoning")[0] is True
    assert can_disable_ai_stage("premium_fallback")[0] is True


def test_ai_pipeline_finbert_carries_version():
    view = build_ai_pipeline_view()
    finbert = view["stages"][1]
    assert finbert["id"] == "fin_sentiment"
    assert finbert["model"]["model_name"] == "ProsusAI/finbert"
    assert finbert["model"]["version"]            # a logical version label is present


def test_can_disable_unknown_stage():
    ok, reason = can_disable_ai_stage("not_a_stage")
    assert ok is False
    assert "unknown" in reason.lower()
