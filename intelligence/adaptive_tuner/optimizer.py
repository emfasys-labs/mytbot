"""Bounded, regime-conditioned contextual hill-climber.

The decision rule is deliberately simple and inspectable: per (parameter,
regime) it keeps the mean realized reward observed at each value it has tried,
and each cycle nudges the live value ONE bounded step toward the best-observed
value (exploit) or explores a neighbouring value (explore / AI-guided). It never
jumps — single steps clamped to the registry's hard [min, max] rails — so a bad
reward signal can only ever move a parameter slowly and reversibly.

Pure functions over a JSON-serializable ``state`` dict (floats internally; the
boundary converts to Decimal). No I/O, fully unit-testable.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

from intelligence.adaptive_tuner.schema import TunableParam, TuningProposal, TunerConfig


def _q(value: float, step: float) -> str:
    """Quantize a value onto the step grid → stable bucket key."""
    if step <= 0:
        return f"{value:.6f}"
    return f"{round(value / step) * step:.6f}"


def empty_state() -> dict[str, Any]:
    return {"cycles": 0, "params": {}, "recent_proposals": [], "last_regime": "unknown"}


def _param_state(state: dict[str, Any], key: str) -> dict[str, Any]:
    return state["params"].setdefault(
        key, {"current": {}, "buckets": {}, "last_applied": {}}
    )


def _best_bucket(buckets: dict[str, dict[str, Any]], min_samples: int) -> tuple[float | None, float]:
    """Return (value, mean_reward) of the best sufficiently-sampled bucket."""
    best_val: float | None = None
    best_mean = float("-inf")
    for vstr, st in buckets.items():
        n = int(st.get("n", 0))
        if n < min_samples:
            continue
        mean = float(st.get("sum", 0.0)) / n if n else 0.0
        if mean > best_mean:
            best_mean = mean
            best_val = float(vstr)
    return best_val, (best_mean if best_val is not None else 0.0)


def attribute_and_propose(
    state: dict[str, Any],
    config: TunerConfig,
    params: tuple[TunableParam, ...],
    *,
    reward: Decimal,
    regime: str,
    defaults: dict[str, Decimal],
    ai_hints: dict[str, str] | None = None,
    rng: random.Random | None = None,
) -> tuple[dict[str, Any], list[TuningProposal]]:
    """Credit the last-applied values with ``reward``, then propose next values.

    ``ai_hints`` maps a param key → "up"/"down"/"hold" advisory direction
    (soft prior on exploration only — magnitude is always the bounded optimizer's).
    Returns ``(new_state, proposals)``.
    """
    rng = rng or random.Random()
    state.setdefault("params", {})
    state.setdefault("recent_proposals", [])
    state["cycles"] = int(state.get("cycles", 0)) + 1
    attr_regime = str(state.get("last_regime", "unknown"))
    r = float(reward)
    explore_rate = float(config.exploration_rate)
    ai_hints = ai_hints or {}

    proposals: list[TuningProposal] = []
    for p in params:
        reg = regime if (config.regime_conditioned and p.regime_conditioned) else "all"
        attr_reg = attr_regime if (config.regime_conditioned and p.regime_conditioned) else "all"
        ps = _param_state(state, p.key)
        step = float(p.step)
        lo, hi = float(p.min_value), float(p.max_value)

        # Seed current from the live YAML default the first time we see it.
        cur = ps["current"].get(reg)
        if cur is None:
            cur = float(defaults.get(p.key, Decimal(str((lo + hi) / 2))))
            cur = min(hi, max(lo, cur))
            ps["current"][reg] = cur

        # ── Attribution: the value applied LAST cycle earned this reward ──
        prev = ps["last_applied"].get(attr_reg)
        if prev is not None:
            bkey = _q(float(prev), step)
            b = ps["buckets"].setdefault(attr_reg, {}).setdefault(bkey, {"sum": 0.0, "n": 0})
            b["sum"] = float(b.get("sum", 0.0)) + r
            b["n"] = int(b.get("n", 0)) + 1

        # ── Proposal for the CURRENT regime ──
        buckets = ps["buckets"].get(reg, {})
        best_val, best_mean = _best_bucket(buckets, config.min_samples_to_exploit)
        hint = str(ai_hints.get(p.key, "")).strip().lower()

        if rng.random() < explore_rate or best_val is None:
            # Explore — direction from AI hint if present, else random.
            if hint == "up":
                direction = 1
            elif hint == "down":
                direction = -1
            elif hint == "hold":
                direction = 0
            else:
                direction = rng.choice((-1, 1))
            new = min(hi, max(lo, cur + direction * step))
            source = "ai_guided" if hint in {"up", "down", "hold"} else "explore"
        else:
            # Exploit — step one notch toward the best-observed value.
            if best_val > cur + step / 2:
                new = min(hi, cur + step)
            elif best_val < cur - step / 2:
                new = max(lo, cur - step)
            else:
                new = cur
            source = "exploit"

        ps["current"][reg] = new
        ps["last_applied"][reg] = new

        proposals.append(
            TuningProposal(
                param_key=p.key,
                regime=reg,
                old_value=Decimal(str(cur)),
                new_value=p.clamp(Decimal(str(new))),
                source=source,
                reward_attributed=Decimal(str(r)),
                rationale=(f"ai:{hint}" if source == "ai_guided" else source)
                + (f"|best={best_val:.4f}@{best_mean:.5f}" if best_val is not None else "|cold"),
                evidence={"best_value": best_val, "best_mean": best_mean, "regime": reg},
            )
        )

    state["last_regime"] = str(regime)
    # Append to the audit ring buffer (most recent last).
    for pr in proposals:
        if pr.new_value != pr.old_value:
            state["recent_proposals"].append(
                {
                    "param": pr.param_key,
                    "regime": pr.regime,
                    "old": float(pr.old_value),
                    "new": float(pr.new_value),
                    "source": pr.source,
                    "reward": float(pr.reward_attributed),
                    "rationale": pr.rationale,
                }
            )
    if len(state["recent_proposals"]) > config.max_recent_proposals:
        state["recent_proposals"] = state["recent_proposals"][-config.max_recent_proposals :]
    return state, proposals


def current_overrides(
    state: dict[str, Any],
    params: tuple[TunableParam, ...],
    regime: str,
    config: TunerConfig,
) -> dict[str, dict[str, Decimal]]:
    """Resolve the live override value per namespace for the given regime.

    Returns ``{namespace: {param_name: Decimal}}``. Dotted names are kept as-is;
    the caller expands them into the namespace's nested config.
    """
    out: dict[str, dict[str, Decimal]] = {}
    for p in params:
        reg = regime if (config.regime_conditioned and p.regime_conditioned) else "all"
        ps = state.get("params", {}).get(p.key)
        if not ps:
            continue
        val = ps.get("current", {}).get(reg)
        if val is None:
            continue
        out.setdefault(p.namespace, {})[p.name] = p.clamp(Decimal(str(val)))
    return out
