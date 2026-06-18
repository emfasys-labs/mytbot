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


def test_router_cme_future_pins_to_ibkr(tmp_path, monkeypatch):
    """D165 — exchange-traded ``=F`` futures must route to IBKR, never crypto."""
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        ibkr:
          future: {enabled: true, reason: null}
        bybit:
          future: {enabled: true, reason: null}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["ibkr", "bybit"])
    assert r.route("future", "CL=F") == "ibkr"
    assert r.route("future", "ES=F") == "ibkr"


def test_router_cme_future_skips_when_ibkr_unavailable(tmp_path, monkeypatch):
    """If IBKR can't take a CME future, skip it — do NOT mis-route to Bybit
    (Bybit's ``future`` permission covers crypto perpetuals, not CME futures)."""
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        ibkr:
          future: {enabled: false, reason: Pending IBKR approval}
        bybit:
          future: {enabled: true, reason: null}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["ibkr", "bybit"])
    # IBKR not permitted for the future → must skip, never return "bybit".
    assert r.route("future", "CL=F") is None


def test_router_crypto_perp_still_uses_bybit(tmp_path, monkeypatch):
    """Crypto-style futures (no ``=F`` suffix) still use the Bybit carve-out."""
    cfg = tmp_path / "broker_permissions.yaml"
    _write_permissions(
        cfg,
        """
        bybit:
          future: {enabled: true, reason: null}
        """,
    )
    perms = BrokerPermissions(cfg)
    monkeypatch.setattr("execution.router.get_permissions", lambda: perms)
    r = SmartOrderRouter(["bybit"])
    assert r.route("future", "BTC-USD") == "bybit"


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

