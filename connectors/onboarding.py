"""
connectors/onboarding.py
=========================
D127 Connect Hub v2 — Phase 6: first-run onboarding wizard.

The wizard walks a new operator through the minimum setup:

    1. Connect a trading platform   (required — ≥1 broker)
    2. Add news & data feeds        (optional — enriches signals)
    3. AI pipeline                  (auto — Rules + FinBERT always on;
                                     Local LLM auto-installs if the
                                     machine probe passes; Premium optional)
    4. Treasury account             (optional)

The wizard state is almost entirely *derived* from the live Connect Hub
snapshot, the AI pipeline view, and the machine probe. The only
persisted bit is whether the operator has finished/dismissed the wizard
(`control_state` key ``connect_hub.onboarding``), so it does not reappear
every launch.

Design rule honoured here: the system must be launchable with a single
paper broker — `can_launch` is true as soon as one broker is configured.
"""

from __future__ import annotations

from typing import Any

from connectors.install_profiles import recommend_profile

# Step ids.
STEP_BROKER = "broker"
STEP_FEEDS = "feeds"
STEP_AI = "ai"
STEP_TREASURY = "treasury"

# Step status values.
DONE = "done"          # satisfied
ATTENTION = "attention"  # required + not satisfied — blocks launch
OPTIONAL = "optional"  # not satisfied, but skippable

ONBOARDING_STATE_KEY = "connect_hub.onboarding"

_STEP_DEFS: tuple[dict[str, Any], ...] = (
    {
        "id": STEP_BROKER,
        "label": "Connect a trading platform",
        "required": True,
        "summary": "Pick at least one supported broker. The system can run on a single paper broker.",
    },
    {
        "id": STEP_FEEDS,
        "label": "Add news & data feeds",
        "required": False,
        "summary": "Optional. More feeds enrich signals; the system works without them.",
    },
    {
        # The AI core (Rules + FinBERT) is required — but it is on by
        # default and FinBERT cannot be disabled while it is the only
        # sentiment provider (P3), so this step is normally auto-satisfied.
        # Local LLM and Premium *within* this step remain optional.
        "id": STEP_AI,
        "label": "AI pipeline",
        "required": True,
        "summary": "Rules + FinBERT are always on. Local LLM auto-installs if your machine supports it; Premium LLM is optional.",
    },
    {
        "id": STEP_TREASURY,
        "label": "Treasury account",
        "required": False,
        "summary": "Optional. One read-only treasury account can be connected as a capital reference.",
    },
)


def _category_rows(connect_hub: dict[str, Any] | None, category: str) -> list[dict[str, Any]]:
    cats = (connect_hub or {}).get("categories", {})
    rows = cats.get(category, []) if isinstance(cats, dict) else []
    return [r for r in rows if isinstance(r, dict)]


def _count_configured(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("enabled") and r.get("configured"))


def _count_connected(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("connected"))


def build_onboarding_view(
    *,
    connect_hub: dict[str, Any] | None = None,
    ai_pipeline: dict[str, Any] | None = None,
    machine_probe: dict[str, Any] | None = None,
    persisted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the onboarding wizard descriptor.

    All inputs are optional — a missing input degrades that step to a
    safe "needs attention / optional" rather than raising.
    """
    persisted = persisted or {}
    brokers = _category_rows(connect_hub, "brokers")
    feeds = _category_rows(connect_hub, "information_feeds")
    treasury = _category_rows(connect_hub, "treasury_accounts")

    brokers_ready = _count_configured(brokers)
    brokers_connected = _count_connected(brokers)
    feeds_ready = _count_configured(feeds)
    treasury_ready = _count_configured(treasury)

    # AI step — Rules + FinBERT must be enabled (they are on by default).
    ai_stages = {s.get("id"): s for s in (ai_pipeline or {}).get("stages", [])}
    rules_on = bool(ai_stages.get("rules", {}).get("enabled"))
    finbert_on = bool(ai_stages.get("fin_sentiment", {}).get("enabled"))
    ai_core_ready = rules_on and finbert_on

    steps: list[dict[str, Any]] = []
    for defn in _STEP_DEFS:
        sid = defn["id"]
        detail: dict[str, Any] = {}
        if sid == STEP_BROKER:
            satisfied = brokers_ready >= 1
            status = DONE if satisfied else ATTENTION
            detail = {"configured": brokers_ready, "connected": brokers_connected}
        elif sid == STEP_FEEDS:
            satisfied = feeds_ready >= 1
            status = DONE if satisfied else OPTIONAL
            detail = {"configured": feeds_ready}
        elif sid == STEP_AI:
            satisfied = ai_core_ready
            status = DONE if satisfied else ATTENTION
            detail = {
                "rules_enabled": rules_on,
                "finbert_enabled": finbert_on,
                "local_llm_available": bool((machine_probe or {}).get("accelerated"))
                if machine_probe else None,
            }
        else:  # STEP_TREASURY
            satisfied = treasury_ready >= 1
            status = DONE if satisfied else OPTIONAL
            detail = {"configured": treasury_ready}
        steps.append(
            {
                "id": sid,
                "label": defn["label"],
                "required": defn["required"],
                "summary": defn["summary"],
                "status": status,
                "satisfied": satisfied,
                "detail": detail,
            }
        )

    required_outstanding = [s for s in steps if s["required"] and not s["satisfied"]]
    # First step that still needs the operator's attention (required
    # first, then optional pending), else None — nothing left to do.
    current = next((s["id"] for s in steps if s["status"] == ATTENTION), None)
    if current is None:
        current = next((s["id"] for s in steps if not s["satisfied"]), None)

    can_launch = brokers_ready >= 1
    ready_to_finish = not required_outstanding
    completed = bool(persisted.get("completed"))

    # M11 — recommend the install profile the machine supports (Lite / Standard
    # / Local AI). Best-effort: only when a probe is available.
    install_profile = recommend_profile(machine_probe) if machine_probe else None

    return {
        "steps": steps,
        "current_step": current,
        "can_launch": can_launch,
        "ready_to_finish": ready_to_finish,
        "completed": completed,
        "completed_at": persisted.get("completed_at"),
        # Show the wizard until it is explicitly completed.
        "show_wizard": not completed,
        "install_profile": install_profile,
    }
