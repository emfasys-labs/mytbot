import textwrap

from brokers.permissions import BrokerPermissions
from execution.router import SmartOrderRouter


def _write_permissions(path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def test_router_fallback_when_preferred_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        ibkr:
          equity: {enabled: false, reason: pending}
        alpaca:
          equity: {enabled: true, reason: null}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["ibkr", "alpaca"])
    rmap = r.route("equity", "SPY")
    assert rmap == "alpaca"


def test_router_no_broker_when_all_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        ibkr:
          option: {enabled: false, reason: pending}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["ibkr"])
    assert r.route("option", "AAPL 202601 C100") is None


def test_router_permission_reload_runtime(tmp_path, monkeypatch):
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        ibkr:
          equity: {enabled: true, reason: null}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["ibkr"])
    assert r.route("equity", "SPY") == "ibkr"

    _write_permissions(
        cfg,
        """
        ibkr:
          equity: {enabled: false, reason: temporary-restriction}
        """,
    )
    r.reload_permissions()
    assert r.route("equity", "SPY") is None

