"""
connectors/lifecycle.py
========================
D127 Connect Hub v2 — connector lifecycle state machine.

Every connector instance (a broker, a news feed, an AI stage, a treasury
account) is in exactly one lifecycle state. This module is the single
authority for what those states are, which transitions are legal, and
how to *derive* the correct state from observable inputs.

State diagram
-------------
    not_configured ─Configure─▶ needs_credentials ─creds saved─▶ testing
    testing ─pass─▶ connected         testing ─partial─▶ connected_limited
    testing ─fail─▶ error             connected ─Disable─▶ disabled
    disabled ─Enable─▶ testing        connected ─live, paper-only─▶ unsupported_in_live

The module is pure (no I/O) so it is trivially testable and can be
imported anywhere — API layer, snapshot builder, orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── States ───────────────────────────────────────────────────────────────────

NOT_CONFIGURED = "not_configured"
NEEDS_CREDENTIALS = "needs_credentials"
TESTING = "testing"
CONNECTED = "connected"
CONNECTED_LIMITED = "connected_limited"
DISABLED = "disabled"
ERROR = "error"
UNSUPPORTED_IN_LIVE = "unsupported_in_live"

ALL_STATES: frozenset[str] = frozenset(
    {
        NOT_CONFIGURED,
        NEEDS_CREDENTIALS,
        TESTING,
        CONNECTED,
        CONNECTED_LIMITED,
        DISABLED,
        ERROR,
        UNSUPPORTED_IN_LIVE,
    }
)

# States in which a connector is usable by the running system.
USABLE_STATES: frozenset[str] = frozenset({CONNECTED, CONNECTED_LIMITED})

# ── Legal transitions ────────────────────────────────────────────────────────
# Maps a source state to the set of states it may move to. Used as a guard;
# `resolve_status` is the primary derivation path.

_TRANSITIONS: dict[str, frozenset[str]] = {
    NOT_CONFIGURED: frozenset({NEEDS_CREDENTIALS, TESTING, DISABLED}),
    NEEDS_CREDENTIALS: frozenset({TESTING, NOT_CONFIGURED, DISABLED}),
    TESTING: frozenset(
        {CONNECTED, CONNECTED_LIMITED, ERROR, NEEDS_CREDENTIALS, UNSUPPORTED_IN_LIVE}
    ),
    CONNECTED: frozenset({DISABLED, TESTING, ERROR, UNSUPPORTED_IN_LIVE, CONNECTED_LIMITED}),
    CONNECTED_LIMITED: frozenset({DISABLED, TESTING, ERROR, UNSUPPORTED_IN_LIVE, CONNECTED}),
    DISABLED: frozenset({TESTING, NOT_CONFIGURED, NEEDS_CREDENTIALS}),
    ERROR: frozenset({TESTING, NEEDS_CREDENTIALS, DISABLED, NOT_CONFIGURED}),
    UNSUPPORTED_IN_LIVE: frozenset({TESTING, DISABLED, CONNECTED, CONNECTED_LIMITED}),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """Return True when `from_state -> to_state` is a legal lifecycle move.

    A no-op transition (`from == to`) is always legal.
    """
    f = (from_state or "").strip().lower()
    t = (to_state or "").strip().lower()
    if f == t and t in ALL_STATES:
        return True
    return t in _TRANSITIONS.get(f, frozenset())


# ── State derivation ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StatusInputs:
    """Observable facts used to derive a connector's lifecycle state."""

    enabled: bool
    credentials_complete: bool   # all *required* secrets present in .env
    has_any_credential: bool     # at least one required secret present
    test_ok: Optional[bool]      # None = never tested; True/False = last test outcome
    test_partial: bool = False   # test authenticated but some capability missing
    paper_only: bool = False     # connector supports paper but not live
    system_live_mode: bool = False
    auth_required: bool = True    # False for auth_type == none (e.g. rules engine)


def resolve_status(inputs: StatusInputs) -> str:
    """Derive the canonical lifecycle state from observable inputs.

    This is the single source of truth — the API, the snapshot builder, and
    the test endpoint all call this rather than hand-rolling state logic.
    """
    if not inputs.enabled:
        return DISABLED

    # A paper-only connector cannot participate while the system is live.
    if inputs.paper_only and inputs.system_live_mode:
        return UNSUPPORTED_IN_LIVE

    # Credential gate (skipped for connectors that need no auth).
    if inputs.auth_required and not inputs.credentials_complete:
        return NEEDS_CREDENTIALS if inputs.has_any_credential else NOT_CONFIGURED

    # Credentials are present (or not required) — interpret the last test.
    if inputs.test_ok is None:
        # Configured but never tested. Treat as testing-pending: the UI shows
        # a "Run test" next-action; it is not yet usable.
        return TESTING
    if inputs.test_ok is False:
        return ERROR
    if inputs.test_partial:
        return CONNECTED_LIMITED
    return CONNECTED


def is_usable(state: str) -> bool:
    """True when the connector may be used by the running system."""
    return (state or "").strip().lower() in USABLE_STATES
