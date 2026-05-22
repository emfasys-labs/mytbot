"""
connectors/certification.py
============================
D127 Connect Hub v2 — Phase 2: certification tiers + live-mode guard.

The strong product rule:

    Certified connectors may execute (place trades / move treasury cash).
    Experimental connectors may only inform (advisory scoring, balance reads).
    Anything not explicitly certified is treated as experimental.

This module is pure resolution logic. The actual *veto* is enforced by
the risk engine (`RiskEngine._check_broker_certification`) — the
unconditional rail — not here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

CERTIFIED = "certified"
EXPERIMENTAL = "experimental"


def resolve_tier(manifest: Any | None) -> str:
    """Return the certification tier of a connector manifest.

    Anything missing, malformed, or not literally ``certified`` resolves
    to ``experimental`` — fail-closed for execution privileges.
    """
    if manifest is None:
        return EXPERIMENTAL
    raw = str(getattr(manifest, "certification", "") or "").strip().lower()
    return CERTIFIED if raw == CERTIFIED else EXPERIMENTAL


def manifest_is_paper_only(manifest: Any | None) -> bool:
    """True when the connector supports paper trading but not live."""
    if manifest is None:
        return False
    caps = dict(getattr(manifest, "capabilities", {}) or {})
    if not caps:
        return False
    supports_paper = bool(caps.get("supports_paper", False))
    supports_live = bool(caps.get("supports_live", True))
    return supports_paper and not supports_live


def may_execute(
    manifest: Any | None,
    *,
    system_live_mode: bool = False,
) -> tuple[bool, str]:
    """Decide whether a connector is permitted to execute (trade / move cash).

    Returns ``(allowed, reason)``. Two gates, in order:

    1. **Certification** — only ``certified`` connectors may execute.
    2. **Live-mode guard** — a paper-only connector may not execute while
       the system is in live mode (it would be ``unsupported_in_live``).

    Reduce-only / closing intent is *not* considered here — exits must
    always be allowed and the caller (risk engine) exempts them before
    calling this function.
    """
    tier = resolve_tier(manifest)
    if tier != CERTIFIED:
        return (False, "broker_not_certified")
    if system_live_mode and manifest_is_paper_only(manifest):
        return (False, "broker_unsupported_in_live")
    return (True, "certified")


def certified_broker_ids(manifests: list[Any] | None) -> frozenset[str]:
    """Return the set of broker connector ids whose tier is ``certified``."""
    out: set[str] = set()
    for m in manifests or []:
        if str(getattr(m, "category", "")).strip().lower() != "brokers":
            continue
        if resolve_tier(m) == CERTIFIED:
            out.add(str(getattr(m, "id", "")).strip().lower())
    return frozenset(out)


# ── cached catalogue access for the risk-engine gate ─────────────────────────
# The risk engine calls this per-signal; loading the YAML catalogue every time
# would be wasteful. Cache for the process lifetime — certification is a
# myTbot-team manifest edit, applied on restart, never a runtime toggle.

_BROKER_MANIFEST_CACHE: dict[str, Any] = {"loaded": False, "by_id": {}, "ok": False}


def _ensure_broker_manifests(refresh: bool = False) -> tuple[dict[str, Any], bool]:
    if _BROKER_MANIFEST_CACHE["loaded"] and not refresh:
        return _BROKER_MANIFEST_CACHE["by_id"], _BROKER_MANIFEST_CACHE["ok"]
    by_id: dict[str, Any] = {}
    ok = False
    try:
        from system.connect_hub import load_connector_manifests

        for m in load_connector_manifests():
            if str(getattr(m, "category", "")).strip().lower() == "brokers":
                by_id[str(getattr(m, "id", "")).strip().lower()] = m
        ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("certification | could not load connector catalogue: %s", exc)
        ok = False
    _BROKER_MANIFEST_CACHE.update({"loaded": True, "by_id": by_id, "ok": ok})
    return by_id, ok


def refresh_certification_cache() -> None:
    """Force the next ``broker_execution_decision`` to reload the catalogue."""
    _BROKER_MANIFEST_CACHE["loaded"] = False


def broker_execution_decision(
    broker_id: str, *, system_live_mode: bool = False
) -> tuple[bool, str]:
    """Risk-engine entry point: may this broker place trades?

    Fail-open policy on the two infrastructure edge cases:

    * **catalogue failed to load** — allow + warn; never halt trading on a
      config-read glitch.
    * **broker absent from the catalogue** — allow + warn; a broker with no
      manifest also has no adapter and cannot route an order anyway. The
      genuine certification risk — an *in-catalogue but experimental*
      connector — is still caught.
    """
    bid = (broker_id or "").strip().lower()
    if not bid:
        return (True, "no_broker_id")
    by_id, ok = _ensure_broker_manifests()
    if not ok:
        logger.warning("certification | catalogue unavailable — allowing %s", bid)
        return (True, "catalogue_unavailable")
    manifest = by_id.get(bid)
    if manifest is None:
        logger.warning("certification | broker '%s' not in catalogue — allowing", bid)
        return (True, "broker_not_in_catalogue")
    return may_execute(manifest, system_live_mode=system_live_mode)
