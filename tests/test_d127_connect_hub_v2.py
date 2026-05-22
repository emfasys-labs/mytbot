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


# ── P4: Local LLM machine probe + catalogue fitness ───────────────────────────

from connectors.machine_probe import probe_machine  # noqa: E402
from connectors import local_llm as lllm  # noqa: E402


def _probe(*, cpu=8, ram=16.0, gpu=False, vram=0.0, disk=200.0, ollama=True):
    return {
        "cpu_count": cpu, "ram_gb": ram, "gpu_present": gpu, "gpu_name": "test" if gpu else None,
        "vram_gb": vram, "disk_free_gb": disk, "ollama_available": ollama,
        "ollama_url": "http://localhost:11434", "accelerated": gpu and vram > 0,
    }


def test_machine_probe_returns_all_fields():
    p = probe_machine()
    for key in ("cpu_count", "ram_gb", "gpu_present", "vram_gb", "disk_free_gb",
                "ollama_available", "accelerated"):
        assert key in p


def test_catalogue_loads_and_is_nonempty():
    cat = lllm.load_catalogue()
    assert len(cat) >= 3
    assert all("id" in e and "disk_gb" in e for e in cat)


def test_fitness_unsupported_when_disk_too_small():
    entry = {"id": "qwen2.5:7b", "disk_gb": 4.7, "min_ram_gb": 8, "min_vram_gb": 6, "params": "7B"}
    f = lllm.compute_fitness(_probe(disk=2.0), entry)
    assert f == lllm.UNSUPPORTED


def test_fitness_available_on_capable_gpu():
    entry = {"id": "qwen2.5:7b", "disk_gb": 4.7, "min_ram_gb": 8, "min_vram_gb": 6, "params": "7B"}
    f = lllm.compute_fitness(_probe(gpu=True, vram=12.0, disk=200.0), entry)
    assert f == lllm.AVAILABLE


def test_fitness_unsupported_when_ram_too_small_cpu_only():
    entry = {"id": "llama3.1:8b", "disk_gb": 4.9, "min_ram_gb": 10, "min_vram_gb": 6, "params": "8B"}
    f = lllm.compute_fitness(_probe(ram=4.0, gpu=False, disk=200.0), entry)
    assert f == lllm.UNSUPPORTED


def test_fitness_large_model_cpu_only_is_too_slow():
    entry = {"id": "qwen2.5:14b", "disk_gb": 9.0, "min_ram_gb": 16, "min_vram_gb": 11, "params": "14B"}
    f = lllm.compute_fitness(_probe(ram=32.0, gpu=False, disk=200.0), entry)
    assert f == lllm.TOO_SLOW


def test_recommend_picks_highest_quality_available():
    cat = lllm.load_catalogue()
    # A strong GPU machine — the 14B model should be the recommendation.
    rec = lllm.recommend_model(_probe(gpu=True, vram=16.0, ram=64.0, disk=500.0), cat)
    assert rec == "qwen2.5:14b"


def test_recommend_none_when_nothing_fits():
    cat = lllm.load_catalogue()
    rec = lllm.recommend_model(_probe(ram=2.0, gpu=False, disk=1.0), cat)
    assert rec is None


def test_availability_false_without_ollama():
    cat = lllm.load_catalogue()
    available, reason = lllm.resolve_local_llm_availability(_probe(ollama=False), cat)
    assert available is False
    assert "ollama" in reason.lower()


def test_availability_false_on_weak_machine():
    cat = lllm.load_catalogue()
    available, reason = lllm.resolve_local_llm_availability(
        _probe(ram=2.0, gpu=False, disk=1.0, ollama=True), cat
    )
    assert available is False
    assert "machine" in reason.lower() or "hardware" in reason.lower()


def test_availability_true_on_capable_machine():
    cat = lllm.load_catalogue()
    available, _ = lllm.resolve_local_llm_availability(
        _probe(gpu=True, vram=12.0, ram=32.0, disk=300.0), cat
    )
    assert available is True


def test_build_view_marks_recommendation_and_fitness():
    view = lllm.build_local_llm_view(probe=_probe(gpu=True, vram=16.0, ram=64.0, disk=500.0))
    assert view["local_llm_available"] is True
    assert view["recommended_model"] == "qwen2.5:14b"
    rec_rows = [m for m in view["models"] if m["fitness"] == lllm.RECOMMENDED]
    assert len(rec_rows) == 1 and rec_rows[0]["id"] == "qwen2.5:14b"


def test_set_local_llm_model_rejects_non_catalogue(tmp_path):
    ai_yaml = tmp_path / "ai.yaml"
    ai_yaml.write_text("providers:\n  local_reasoning:\n    model_name: qwen2.5:7b\n", encoding="utf-8")
    assert lllm.set_local_llm_model("not-a-real-model", ai_config_path=ai_yaml) is False


def test_set_local_llm_model_writes_catalogue_model(tmp_path):
    ai_yaml = tmp_path / "ai.yaml"
    ai_yaml.write_text("providers:\n  local_reasoning:\n    model_name: qwen2.5:7b\n", encoding="utf-8")
    assert lllm.set_local_llm_model("llama3.1:8b", ai_config_path=ai_yaml) is True
    import yaml
    cfg = yaml.safe_load(ai_yaml.read_text(encoding="utf-8"))
    assert cfg["providers"]["local_reasoning"]["model_name"] == "llama3.1:8b"


# ── P5: Premium LLM provider picker + cert ────────────────────────────────────

from connectors import premium_llm as pllm  # noqa: E402


def test_premium_catalogue_has_five_providers():
    cat = pllm.load_provider_catalogue()
    ids = {e["id"] for e in cat}
    assert ids == {"anthropic", "openai", "gemini", "azure_openai", "custom_openai"}


def test_premium_endpoint_types_cover_two_shapes():
    cat = pllm.load_provider_catalogue()
    types = {e["endpoint_type"] for e in cat}
    assert types == {"anthropic_native", "openai_compatible"}


def test_find_provider():
    assert pllm.find_provider("anthropic")["label"] == "Anthropic Claude"
    assert pllm.find_provider("nonexistent") is None


def test_premium_view_reports_configuration(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    view = pllm.build_premium_llm_view()
    by_id = {p["id"]: p for p in view["providers"]}
    assert by_id["openai"]["api_key_configured"] is True
    assert by_id["openai"]["configured"] is True            # openai needs no base_url
    assert by_id["anthropic"]["api_key_configured"] is False
    # Azure/custom need a base_url too — key alone is not "configured".
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    view2 = pllm.build_premium_llm_view()
    azure = {p["id"]: p for p in view2["providers"]}["azure_openai"]
    assert azure["api_key_configured"] is True
    assert azure["configured"] is False                      # missing endpoint


def test_set_premium_provider_rejects_non_catalogue(tmp_path):
    ai_yaml = tmp_path / "ai.yaml"
    ai_yaml.write_text("providers:\n  premium_fallback:\n    provider: anthropic\n", encoding="utf-8")
    assert pllm.set_premium_provider("not-a-provider", "x", ai_config_path=ai_yaml) is False


def test_set_premium_provider_writes_catalogue_provider(tmp_path):
    ai_yaml = tmp_path / "ai.yaml"
    ai_yaml.write_text(
        "providers:\n  premium_fallback:\n    provider: anthropic\n    model_name: old\n",
        encoding="utf-8",
    )
    assert pllm.set_premium_provider("openai", "gpt-4o", ai_config_path=ai_yaml) is True
    import yaml
    cfg = yaml.safe_load(ai_yaml.read_text(encoding="utf-8"))
    assert cfg["providers"]["premium_fallback"]["provider"] == "openai"
    assert cfg["providers"]["premium_fallback"]["model_name"] == "gpt-4o"


@pytest.mark.asyncio
async def test_cert_premium_unknown_provider():
    r = await pllm.cert_premium_provider("nonexistent", model="x")
    assert r.passed is False
    assert "catalogue" in r.reason.lower()


@pytest.mark.asyncio
async def test_cert_premium_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = await pllm.cert_premium_provider("anthropic", model="claude-sonnet-4-5")
    assert r.passed is False
    assert "api key" in r.reason.lower()


def test_premium_cert_text_evaluation():
    # Valid structured reply within budget → passes.
    good = pllm._evaluate_text('{"sentiment": "positive", "confidence": 0.8}', 500)
    assert good.passed is True and good.schema_ok is True
    # Non-JSON reply → fails.
    bad = pllm._evaluate_text("not json at all", 500)
    assert bad.passed is False and bad.json_mode_ok is False
    # Missing schema keys → fails.
    partial = pllm._evaluate_text('{"foo": "bar"}', 500)
    assert partial.passed is False and partial.schema_ok is False
