import pytest

from system.deployment import build_deployment_readiness, validate_deployment_startup


@pytest.mark.asyncio
async def test_default_paper_stage_is_start_ready(monkeypatch):
    monkeypatch.setenv("APP_ENV", "paper")
    monkeypatch.setenv("IBKR_PORT", "7497")

    readiness = await build_deployment_readiness(bus=None, session_factory=None)

    assert readiness["stage"] == "paper"
    assert readiness["paper_mode"] is True
    blocker_keys = {b["key"] for b in readiness["blockers"]}
    assert "paper_days" in blocker_keys
    assert "app_env_live" in blocker_keys


@pytest.mark.asyncio
async def test_micro_live_readiness_requires_explicit_live_arming(monkeypatch):
    monkeypatch.setenv("APP_ENV", "paper")
    monkeypatch.delenv("MYTBOT_LIVE_ARMED", raising=False)
    monkeypatch.delenv("DASHBOARD_READ_TOKEN", raising=False)
    monkeypatch.delenv("API_CONTROL_TOKEN", raising=False)

    readiness = await build_deployment_readiness(
        bus=None,
        session_factory=None,
        requested_stage="micro_live",
    )

    blocker_keys = {b["key"] for b in readiness["blockers"]}
    assert "app_env_live" in blocker_keys
    assert "live_armed" in blocker_keys
    assert "dashboard_tokens" in blocker_keys
    assert "paper_days" in blocker_keys


def test_startup_validation_refuses_paper_with_ibkr_live_port(monkeypatch):
    monkeypatch.setenv("APP_ENV", "paper")
    monkeypatch.setenv("IBKR_PORT", "7496")

    with pytest.raises(RuntimeError, match="deployment startup validation failed"):
        validate_deployment_startup()
