"""
connectors/capability_probe.py
===============================
D127 Connect Hub v2 — live capability detection.

The "Test connection" action runs a probe that turns a connector's
*declared* capabilities (the `connectors.yaml` manifest) into *detected*
capabilities — what the connector verifiably can do right now.

Phase 1 covers brokers and information feeds. Brokers are probed against
the live `BrokerManager` status the orchestrator already maintains;
feeds against the ingest telemetry. AI providers and treasury accounts
get probe support in later phases (P3/P5/P7) — `probe_connector` returns
an explicit "not yet probed in this phase" result for them so callers
never crash.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class ProbeResult:
    """Outcome of a live capability probe."""

    ok: bool                                # auth/connection verified
    partial: bool                           # reachable but a capability is missing/degraded
    reason: str                             # human-readable summary
    detected_capabilities: dict[str, bool] = field(default_factory=dict)
    credentials_complete: bool = False      # all required .env secrets present
    has_any_credential: bool = False        # at least one required secret present
    latency_ms: Optional[int] = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "partial": self.partial,
            "reason": self.reason,
            "detected_capabilities": dict(self.detected_capabilities),
            "credentials_complete": self.credentials_complete,
            "has_any_credential": self.has_any_credential,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at.isoformat(),
            "detail": dict(self.detail),
        }


def _credential_state(manifest: Any) -> tuple[bool, bool, bool]:
    """Return (auth_required, credentials_complete, has_any_credential)."""
    auth_type = str(getattr(manifest, "auth_type", "api_key") or "").strip().lower()
    # `none` (rules engine) and `gateway` (IBKR — uses TWS/Gateway, not API
    # keys) need no .env secrets.
    auth_required = auth_type not in {"none", "gateway", "local_model", "local_endpoint"}
    secrets = list(getattr(manifest, "required_secrets", ()) or ())
    required = [s for s in secrets if getattr(s, "required", True)]
    if not required:
        return (auth_required, True, True)
    configured = [bool(getattr(s, "configured", False)) for s in required]
    return (auth_required, all(configured), any(configured))


def _broker_probe(manifest: Any, orchestrator: Any | None) -> ProbeResult:
    auth_required, creds_complete, has_any = _credential_state(manifest)
    started = time.monotonic()

    if auth_required and not creds_complete:
        return ProbeResult(
            ok=False,
            partial=False,
            reason="missing required credentials",
            credentials_complete=False,
            has_any_credential=has_any,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    broker_status: dict[str, Any] = {}
    if orchestrator is not None:
        try:
            status = orchestrator.status()
            brokers = status.get("brokers", {}) if isinstance(status, dict) else {}
            if isinstance(brokers, dict):
                broker_status = brokers.get(manifest.id) or {}
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                ok=False, partial=False,
                reason=f"could not read live broker status: {exc}",
                credentials_complete=creds_complete, has_any_credential=has_any,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    if not broker_status:
        return ProbeResult(
            ok=False, partial=False,
            reason="broker not connected — start the system to verify a live connection",
            credentials_complete=creds_complete, has_any_credential=has_any,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    connected = bool(broker_status.get("connected"))
    balance_ready = bool(broker_status.get("balance_ready"))
    err = broker_status.get("error")
    declared = dict(getattr(manifest, "capabilities", {}) or {})
    latency = int((time.monotonic() - started) * 1000)

    if not connected:
        return ProbeResult(
            ok=False, partial=False,
            reason=str(err) if err else "broker configured but not connected",
            credentials_complete=creds_complete, has_any_credential=has_any,
            latency_ms=latency, detail={"broker_status": broker_status},
        )

    detected = {
        "can_read_balance": balance_ready and bool(declared.get("can_read_balance", True)),
        "can_trade": connected and bool(declared.get("can_trade", False)),
        "can_withdraw": False,  # never — hard-wired off for every broker
    }
    for key in ("supports_paper", "supports_live", "supports_options",
                "supports_forex", "supports_equities", "supports_crypto_spot",
                "supports_crypto_derivatives"):
        if key in declared:
            detected[key] = bool(declared[key])

    partial = not balance_ready
    return ProbeResult(
        ok=True,
        partial=partial,
        reason="connected (balance not yet ready)" if partial else "connected",
        detected_capabilities=detected,
        credentials_complete=creds_complete,
        has_any_credential=has_any,
        latency_ms=latency,
        detail={"broker_status": broker_status},
    )


def _feed_probe(
    manifest: Any, news_provider_statuses: list[dict[str, Any]] | None
) -> ProbeResult:
    auth_required, creds_complete, has_any = _credential_state(manifest)
    started = time.monotonic()

    if auth_required and not creds_complete:
        return ProbeResult(
            ok=False, partial=False,
            reason="missing required credentials",
            credentials_complete=False, has_any_credential=has_any,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    row: dict[str, Any] = {}
    for r in news_provider_statuses or []:
        if isinstance(r, dict) and str(r.get("id") or "").strip().lower() == manifest.id:
            row = r
            break

    declared = dict(getattr(manifest, "capabilities", {}) or {})
    detected = {k: bool(v) for k, v in declared.items()}
    latency = int((time.monotonic() - started) * 1000)

    if not row:
        return ProbeResult(
            ok=False, partial=False,
            reason="feed has not ingested yet — run the data pipeline to verify",
            credentials_complete=creds_complete, has_any_credential=has_any,
            latency_ms=latency,
        )

    state = str(row.get("state") or "off").strip().lower()
    if state == "live":
        return ProbeResult(
            ok=True, partial=False, reason="feed live",
            detected_capabilities=detected,
            credentials_complete=creds_complete, has_any_credential=has_any,
            latency_ms=latency, detail={"feed_status": row},
        )
    if state == "stale":
        return ProbeResult(
            ok=True, partial=True,
            reason="feed reachable but last ingest is stale",
            detected_capabilities=detected,
            credentials_complete=creds_complete, has_any_credential=has_any,
            latency_ms=latency, detail={"feed_status": row},
        )
    return ProbeResult(
        ok=False, partial=False,
        reason=str(row.get("error") or "feed not ingesting"),
        credentials_complete=creds_complete, has_any_credential=has_any,
        latency_ms=latency, detail={"feed_status": row},
    )


def probe_connector(
    *,
    category: str,
    manifest: Any,
    orchestrator: Any | None = None,
    news_provider_statuses: list[dict[str, Any]] | None = None,
) -> ProbeResult:
    """Dispatch a capability probe for a connector.

    Phase 1 handles `brokers` and `information_feeds`. AI providers and
    treasury accounts return an explicit not-yet-supported result.
    """
    cat = (category or "").strip().lower()
    if cat == "brokers":
        return _broker_probe(manifest, orchestrator)
    if cat == "information_feeds":
        return _feed_probe(manifest, news_provider_statuses)

    auth_required, creds_complete, has_any = _credential_state(manifest)
    return ProbeResult(
        ok=False, partial=False,
        reason=f"capability probing for '{cat}' arrives in a later Connect Hub phase",
        credentials_complete=creds_complete, has_any_credential=has_any,
    )
