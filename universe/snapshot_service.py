from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

import yaml

from brokers.ibkr.universe import load_ibkr_universe
from core.instrument_profiles import crypto_display_name, logo_url_for_symbol
from data.universe import UniverseManager
from data.universe_builder import _to_yf_symbol
from data.universe_tiers import load_universe_tiers
from universe.persistence import DEFAULT_INTELLIGENCE_PATH, load_intelligence_state
from universe.universe_tiers import UniverseIntelligenceState

# Curated human names / sectors (data/universe.py). Pipeline symbols outside this set
# still get a generated description from class + stage.
_CATALOG_BY_SYMBOL: dict[str, tuple[str, str | None]] = {
    inst.symbol.upper(): (inst.name, inst.sector) for inst in UniverseManager.INITIAL_UNIVERSE
}
for _entry in load_ibkr_universe():
    _CATALOG_BY_SYMBOL.setdefault(_entry.symbol.upper(), (_entry.name, _entry.sector))
    _CATALOG_BY_SYMBOL.setdefault(_entry.broker_symbol.upper(), (_entry.name, _entry.sector))


def _catalog_lookup(sym: str) -> tuple[str | None, str | None]:
    """Match pipeline/tier tickers to UniverseInstrument (handles EUR.USD, BTC-USD, etc.)."""
    u = sym.upper().strip()
    if u in _CATALOG_BY_SYMBOL:
        return _CATALOG_BY_SYMBOL[u]
    if u.endswith("=X") and len(u) == 8:
        pair = u[:6]
        dotted = f"{pair[:3]}.{pair[3:]}"
        underscored = f"{pair[:3]}_{pair[3:]}"
        if dotted in _CATALOG_BY_SYMBOL:
            return _CATALOG_BY_SYMBOL[dotted]
        if underscored in _CATALOG_BY_SYMBOL:
            return _CATALOG_BY_SYMBOL[underscored]
    norm = u.replace(".", "_")
    if norm in _CATALOG_BY_SYMBOL:
        return _CATALOG_BY_SYMBOL[norm]
    for sep in ("-", "/"):
        if sep in u:
            base = u.split(sep, 1)[0].strip()
            if base in _CATALOG_BY_SYMBOL:
                return _CATALOG_BY_SYMBOL[base]
    return None, None


def _fmt_sector_label(sector: str | None) -> str | None:
    if not sector or str(sector).lower() == "general":
        return None
    return str(sector).replace("_", " ")


def _instrument_presentation(sym: str, klass: str, name: str | None, sector: str | None) -> dict[str, Any]:
    su = str(sym or "").strip().upper()
    k = str(klass or "").strip().lower()
    if su == "USO":
        return {
            "name": "United States Oil Fund",
            "description": "ETF-style commodity fund linked to WTI crude oil futures.",
            "category": "Commodity fund",
            "logo_kind": "commodity",
            "exchange": None,
            "currency": "USD",
            "industry": None,
        }
    if su == "XLE":
        return {
            "name": "Energy Select Sector SPDR Fund",
            "description": "ETF tracking large US energy-sector equities.",
            "category": "Sector ETF",
            "logo_kind": "fund",
            "exchange": None,
            "currency": "USD",
            "industry": "Energy",
        }
    if su == "CL=F":
        return {
            "name": "WTI Crude Oil",
            "description": "WTI crude oil front-month futures contract.",
            "category": "Commodity future",
            "logo_kind": "commodity",
            "exchange": "CME",
            "currency": "USD",
            "industry": "Energy",
        }
    if k == "fx" or su.endswith("=X"):
        pair = su.replace("=X", "")
        display = f"{pair[:3]}/{pair[3:6]}" if len(pair) >= 6 else su
        return {
            "name": display,
            "description": f"{pair[:3]} versus {pair[3:6]} foreign exchange pair." if len(pair) >= 6 else f"{su} foreign exchange pair.",
            "category": "Forex pair",
            "logo_kind": "forex",
            "exchange": "IDEALPRO",
            "currency": None,
            "industry": None,
        }
    if k == "crypto":
        display = crypto_display_name(su) or su.replace("-USD", "").replace("USD", "") or su
        return {
            "name": display,
            "description": f"{display} digital asset.",
            "category": "Crypto",
            "logo_kind": "crypto",
            "exchange": None,
            "currency": "USD" if "USD" in su else None,
            "industry": None,
        }
    sec_lbl = _fmt_sector_label(sector)
    if k == "etf":
        return {
            "name": name or su,
            "description": name or (f"{su} · ETF" + (f" · {sec_lbl}" if sec_lbl else "")),
            "category": "ETF",
            "logo_kind": "fund",
            "exchange": None,
            "currency": "USD",
            "industry": sec_lbl,
        }
    return {
        "name": name or None,
        "description": name or (f"{su} · {k}" + (f" · {sec_lbl}" if sec_lbl else "")),
        "category": "Equity" if k == "equity" else k,
        "logo_kind": k or "equity",
        "exchange": None,
        "currency": "USD" if k in {"equity", "etf"} else None,
        "industry": sec_lbl,
    }

CONFIG_PATH = Path("config/universe_selection.yaml")


def load_universe_selection_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    if not p.is_file():
        return {"enabled": False, "fallback_to_pipeline": True}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"enabled": False}
    except (OSError, yaml.YAMLError):
        return {"enabled": False, "fallback_to_pipeline": True}


def _load_pipeline_symbols() -> list[str]:
    p = Path("config/data_pipeline.yaml")
    if not p.is_file():
        return []
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return []
        return [str(s).strip().upper() for s in cfg.get("symbols") or [] if str(s).strip()]
    except (OSError, yaml.YAMLError):
        return []


def _pipeline_caps() -> dict[str, int]:
    """Resolve the active universe-tier caps.

    The static YAML in ``config/data_pipeline.yaml::dynamic_universe`` is
    the *neutral anchor*. When D117 adaptive caps have been resolved in
    the most recent pipeline tick (see :mod:`universe.adaptive_state`),
    we overlay the persisted resolved values so the dashboard and any
    consumer of ``_pipeline_caps()`` see the same numbers that the
    UniverseBuilder actually used.
    """
    p = Path("config/data_pipeline.yaml")
    out = {"max_symbols": 300, "core_max": 50, "scan_max": 250, "candidates": 400}
    if p.is_file():
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
            du = (cfg or {}).get("dynamic_universe") or {}
            rk = du.get("ranking") or {}
            out["max_symbols"] = int(du.get("max_symbols") or out["max_symbols"])
            out["core_max"] = int(rk.get("core_max") or out["core_max"])
            out["scan_max"] = int(rk.get("scan_max") or out["scan_max"])
            out["candidates"] = int(rk.get("max_candidates_to_score") or out["candidates"])
        except (OSError, yaml.YAMLError, TypeError, ValueError):
            pass
    # D117 overlay — only when adaptive resolved a non-empty state.
    try:
        from universe.adaptive_state import load_adaptive_state

        state = load_adaptive_state()
        resolved = state.resolved if state.enabled else {}
        if resolved:
            if resolved.get("candidates"):
                out["candidates"] = int(resolved["candidates"])
            if resolved.get("watching"):
                out["max_symbols"] = int(resolved["watching"])
            if resolved.get("core"):
                out["core_max"] = int(resolved["core"])
            if resolved.get("scan"):
                out["scan_max"] = int(resolved["scan"])
    except Exception:  # noqa: BLE001
        pass
    return out


def _adaptive_caps_block() -> dict[str, Any] | None:
    """Surface the persisted adaptive-caps state for the dashboard.

    Returns ``None`` when adaptive caps are disabled or the runtime
    state file is missing/empty; otherwise returns a structured block
    with the resolved caps, base anchor, multiplier, regime/pressure
    context, and the most recent grace-extended symbols.
    """
    try:
        from universe.adaptive_state import load_adaptive_state
    except Exception:  # noqa: BLE001
        return None
    state = load_adaptive_state()
    if not state.enabled or not state.resolved:
        return None
    return {
        "enabled": True,
        "updated_at": state.updated_at,
        "resolved": dict(state.resolved),
        "context": dict(state.context),
        "consecutive_misses_count": len(state.consecutive_misses),
        "last_grace_extended": list(state.last_grace_extended)[:50],
    }


def _priority_rule_block() -> dict[str, Any] | None:
    """D118 — surface the self-tuning priority pre-filter telemetry.

    Returns ``None`` when none of the D118 state files exist yet (first
    boot pre-cycle-1). Otherwise the block carries the current learned
    weights, the recent weight-history sparkline, the budget controller
    state with binding-constraint label, and the score-age summary.
    """
    try:
        from data.universe_budget_controller import load_budget_state
        from data.universe_score_ages import load_score_ages
        from data.universe_weight_learner import (
            COMPONENT_NAMES,
            load_weight_learner_state,
        )
    except Exception:  # noqa: BLE001
        return None
    weights_state = load_weight_learner_state()
    budget_state = load_budget_state()
    ages_state = load_score_ages()
    if (
        weights_state.cycle_count == 0
        and budget_state.cycle_count == 0
        and len(ages_state) == 0
    ):
        return None
    # Surface clamped + renormalised live weights (matches what the
    # learner exposes to the pre-filter).
    from data.universe_weight_learner import WeightLearner as _WL

    live_weights = _WL(state=weights_state).current_weights()
    return {
        "enabled": True,
        "weights": {name: float(live_weights.get(name, 0.0)) for name in COMPONENT_NAMES},
        "weights_history": list(weights_state.history[-30:]),
        "weights_cycle_count": int(weights_state.cycle_count),
        "weights_last_update_at": weights_state.last_update_at,
        "budget": {
            "target_budget": int(budget_state.target_budget),
            "binding_constraint": str(budget_state.binding_constraint),
            "cycle_count": int(budget_state.cycle_count),
            "last_observation": dict(budget_state.last_observation),
            "last_update_at": budget_state.last_update_at,
        },
        "score_age_summary": ages_state.summary(),
    }


def _transitions_block(limit: int = 100) -> list[dict[str, Any]]:
    """D118 — surface the most recent tier-transition rows."""
    try:
        from data.universe_transitions import load_transitions
    except Exception:  # noqa: BLE001
        return []
    buf = load_transitions()
    return [row.to_dict() for row in buf.recent(int(limit))]


def _score_ages_by_symbol() -> dict[str, dict[str, Any]]:
    """D118 — load per-symbol score-age + last_score for the UI grid."""
    try:
        from data.universe_score_ages import load_score_ages
    except Exception:  # noqa: BLE001
        return {}
    state = load_score_ages()
    out: dict[str, dict[str, Any]] = {}
    for sym, row in state.items():
        out[sym] = {
            "last_scored_at": row.last_scored_at,
            "last_score": row.last_score,
            "score_count": int(row.score_count),
            "first_seen_at": row.first_seen_at,
        }
    return out


def _d118_scoring_counts(
    *,
    priority_ranked_fallback: int,
    scored_fallback: int,
    budget_block: dict[str, Any] | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Resolve priority-ranked vs successfully-scored counts for the funnel.

    When the budget controller has completed at least one cycle, we prefer
    its last observation (``budget`` = symbols picked for yfinance,
    ``scored`` = symbols that actually returned a score). Otherwise we
    fall back to tier-file counts.
    """
    ranked = int(max(0, priority_ranked_fallback))
    scored = int(max(0, scored_fallback))
    drops_scored: list[dict[str, Any]] = []
    if isinstance(budget_block, dict):
        last_obs = budget_block.get("last_observation")
        if isinstance(last_obs, dict):
            obs_budget = int(last_obs.get("budget") or 0)
            obs_scored = int(last_obs.get("scored") or 0)
            if obs_budget > 0:
                ranked = obs_budget
            if obs_scored > 0:
                scored = obs_scored
        target = int(budget_block.get("target_budget") or 0)
        if ranked <= 0 and target > 0:
            ranked = target
    if ranked > 0 and scored > ranked:
        scored = ranked
    if ranked > scored:
        drops_scored.append(
            {
                "reason": "yfinance timeout / no score",
                "count": int(ranked - scored),
            }
        )
    return ranked, scored, drops_scored


def _build_d118_funnel(
    *,
    unique_source_count: int,
    priority_ranked_count: int,
    scored_count: int,
    watching_count: int,
    promoted_count: int,
    active_count: int,
    broker_listing_count: int,
    drops_eligible: list[dict[str, Any]],
    drops_watching: list[dict[str, Any]],
    drops_scored: list[dict[str, Any]] | None = None,
    budget_block: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """D118 — 4-stage discovery funnel (priority pick + yfinance are one step).

    ``unique_normalized`` is the deduped broker+registry universe.
    ``scored`` is how many symbols received a liquidity score this cycle.
    ``watching`` is core+scan; ``promoted_now`` (anomaly boosts) is
    metadata on that stage, not a separate funnel step — promotions
    overlap scan/light and are not a filter between watching and
    ``active_reps``. ``active_reps`` is the correlation-representative
    count from universe intelligence.
    """
    ranked = int(max(0, priority_ranked_count))
    scored = int(max(0, scored_count))
    if ranked <= 0 and scored > 0:
        ranked = scored
    scored_meta: dict[str, Any] | None = None
    if budget_block or ranked > 0:
        scored_meta = dict(budget_block) if budget_block else {}
        if ranked > 0:
            scored_meta["budget_attempted"] = ranked
        if ranked > scored:
            scored_meta["score_failures"] = int(ranked - scored)
    return [
        {
            "stage": "unique_normalized",
            "count": int(max(1, unique_source_count)),
            "fresh": True,
            "drops": drops_eligible,
            "meta": {
                "broker_listings": int(broker_listing_count or 0),
            },
        },
        {
            "stage": "scored",
            "count": int(max(0, scored)),
            "fresh": True,
            "drops": list(drops_scored or []),
            "meta": scored_meta,
        },
        {
            "stage": "watching",
            "count": int(max(0, watching_count)),
            "fresh": True,
            "drops": drops_watching,
            "meta": {
                "promoted_now": int(max(0, promoted_count)),
            },
        },
        {
            "stage": "active_reps",
            "count": int(max(0, active_count)),
            "fresh": True,
            "drops": None,
        },
    ]


def _asset_class_coverage_block(symbol_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """D118 — aggregate by asset class for the Coverage tab.

    Symbol rows already carry ``klass`` from :func:`_classify_symbol`,
    so we aggregate by ``klass`` for the UI chip + progress display.
    """
    counts: dict[str, int] = {}
    for row in symbol_rows:
        if not isinstance(row, dict):
            continue
        klass = str(row.get("klass") or "unknown")
        counts[klass] = counts.get(klass, 0) + 1
    total = sum(counts.values()) or 0
    return {
        "total": int(total),
        "by_asset_class": [
            {
                "klass": klass,
                "count": int(count),
                "share": (float(count) / float(total)) if total > 0 else 0.0,
            }
            for klass, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ],
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_universe_snapshot_dict(
    *,
    broker_symbol_totals: dict[str, int] | None = None,
    broker_symbols: dict[str, list[str]] | None = None,
    intelligence_path: Path | None = None,
    registry_summary_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Assemble JSON for GET /intelligence/universe.
    Safe when intelligence disabled, DB missing, or JSON corrupt — falls back to pipeline.
    """
    cfg = load_universe_selection_config()
    enabled = bool(cfg.get("enabled", False))
    caps = _pipeline_caps()
    pipeline_syms = _load_pipeline_symbols()
    tiers = load_universe_tiers()
    intel = load_intelligence_state(intelligence_path or DEFAULT_INTELLIGENCE_PATH)

    broker_totals = dict(broker_symbol_totals or {})
    broker_symbol_rows = {str(k): list(v or []) for k, v in (broker_symbols or {}).items()}
    if broker_symbol_rows and not broker_totals:
        broker_totals = {k: len(v) for k, v in broker_symbol_rows.items()}
    source_pool = int(sum(broker_totals.values())) if broker_totals else int(cfg.get("candidate_pool_default", 0))
    normalised_by_broker = _normalised_broker_symbols(broker_symbol_rows)
    unique_source_symbols = sorted({s for syms in normalised_by_broker.values() for s in syms})
    unique_source_count = len(unique_source_symbols)

    core_list = list(tiers.core) if tiers else []
    scan_list = list(tiers.scan) if tiers else []
    light_list = list(tiers.light) if tiers else []
    scores = dict(tiers.scores) if tiers else {}

    watching_count = len(set(core_list + scan_list)) or min(len(pipeline_syms), caps["max_symbols"])
    # Prefer the live scores map (symbols with a yfinance liquidity value);
    # the core+scan+light union can include stale light-tier rows from an
    # older, oversized budget cycle and inflates the funnel.
    scored_count = len(scores) or len(set(core_list + scan_list + light_list)) or min(
        unique_source_count or source_pool or len(pipeline_syms),
        caps["candidates"],
    )
    eligible_count = max(watching_count, scored_count)
    source_detail = _source_detail(
        broker_totals=broker_totals,
        normalised_by_broker=normalised_by_broker,
        unique_source_count=unique_source_count,
        scored_count=eligible_count,
        watching_count=watching_count,
        caps=caps,
        registry_summary_data=registry_summary_data or {},
    )

    funnel_template = cfg.get("funnel_display") or {}
    adaptive_block = _adaptive_caps_block()
    # D118 — priority pre-filter, transitions, and coverage blocks.
    priority_rule_block = _priority_rule_block()
    budget_block = (
        priority_rule_block.get("budget") if isinstance(priority_rule_block, dict) else None
    )
    priority_ranked_n, scored_n, drops_scored = _d118_scoring_counts(
        priority_ranked_fallback=min(unique_source_count, caps["candidates"]),
        scored_fallback=scored_count,
        budget_block=budget_block if isinstance(budget_block, dict) else None,
    )
    drops_eligible = _drop_rows(
        [
            ("Broker duplicates / unsupported symbol formats", max(0, source_pool - unique_source_count)),
            (
                "Below this cycle's priority cutoff (budget self-tune)",
                max(0, unique_source_count - priority_ranked_n),
            ),
        ],
        fallback=funnel_template.get("drops_eligible"),
    )
    light_count = len(light_list)
    drops_watching = _drop_rows(
        [
            (
                "Assigned to light tier (scored, not core+scan)",
                max(0, light_count),
            ),
            (
                f"Watch tier cap (core+scan max {caps['max_symbols']})",
                max(0, scored_n - watching_count - light_count),
            ),
            ("Correlation overlap (representative kept)", max(0, len((intel.core if intel else []) or []) - watching_count)),
        ],
        fallback=funnel_template.get("drops_watching"),
    )
    drops_scored = list(drops_scored or [])
    if light_count > 0 and scored_n > watching_count:
        drops_scored.append(
            {
                "reason": "In light tier after scoring (not actively watched)",
                "count": int(light_count),
            }
        )
    transitions_rows = _transitions_block(limit=200)
    # Score-age telemetry per symbol so the InstrumentsTab can render
    # the age stripe + last_scored_at tooltip.
    score_ages_by_symbol = _score_ages_by_symbol()
    if not enabled:
        promoted_n = min(12, max(0, watching_count // 10))
        active_n = min(7, max(0, watching_count // 40))
        funnel = _build_d118_funnel(
            unique_source_count=unique_source_count,
            priority_ranked_count=priority_ranked_n,
            scored_count=scored_n,
            watching_count=watching_count,
            promoted_count=promoted_n,
            active_count=active_n,
            broker_listing_count=source_pool,
            drops_eligible=drops_eligible,
            drops_watching=drops_watching,
            drops_scored=drops_scored,
            budget_block=budget_block if isinstance(budget_block, dict) else None,
        )
        symbols_ui = _symbols_fallback(
            pipeline_syms, core_list + scan_list, scores, caps, cfg,
            intel_disabled=True, intel=None,
            score_ages_by_symbol=score_ages_by_symbol,
        )
        coverage_d118 = _asset_class_coverage_block(symbols_ui)
        return {
            "enabled": False,
            "fallback": "data_pipeline.yaml + runtime tiers",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "funnel": funnel,
            "symbols": symbols_ui,
            "clusters": [],
            "promotions": [],
            "stream": [],
            "config_mirror": _config_mirror(cfg, caps),
            "build": _build_info(tiers.updated_at if tiers else None, cfg),
            "broker_totals": broker_totals,
            "coverage": source_detail,
            "adaptive": adaptive_block,
            "priority_rule": priority_rule_block,
            "transitions": transitions_rows,
            "asset_class_coverage": coverage_d118,
        }

    if intel is None:
        symbols_ui = _symbols_fallback(
            pipeline_syms, core_list + scan_list, scores, caps, cfg,
            intel_disabled=False, intel=None,
            score_ages_by_symbol=score_ages_by_symbol,
        )
        coverage_d118 = _asset_class_coverage_block(symbols_ui)
        return {
            "enabled": True,
            "fallback": "universe intelligence enabled but no build artifact yet",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "funnel": _build_d118_funnel(
                unique_source_count=unique_source_count,
                priority_ranked_count=priority_ranked_n,
                scored_count=scored_n,
                watching_count=watching_count,
                promoted_count=0,
                active_count=min(32, max(0, watching_count // 40)),
                broker_listing_count=source_pool,
                drops_eligible=drops_eligible,
                drops_watching=drops_watching,
                drops_scored=drops_scored,
                budget_block=budget_block if isinstance(budget_block, dict) else None,
            ),
            "symbols": symbols_ui,
            "clusters": [],
            "promotions": [],
            "stream": [],
            "config_mirror": _config_mirror(cfg, caps),
            "build": _build_info(tiers.updated_at if tiers else None, cfg, state="missing"),
            "broker_totals": broker_totals,
            "coverage": source_detail,
            "core_intel": [],
            "cold_scan": [],
            "active_eval": [],
            "adaptive": adaptive_block,
            "priority_rule": priority_rule_block,
            "transitions": transitions_rows,
            "asset_class_coverage": coverage_d118,
        }

    cold = list(intel.cold_scan)
    active_eval = list(intel.active_eval)
    core_intel = list(intel.core)
    clusters = deepcopy(intel.clusters)
    promotions = deepcopy(intel.promotions)
    active_count = len(core_intel)
    source_count = max(source_pool or intel.candidate_count, 1)

    funnel = _build_d118_funnel(
        unique_source_count=unique_source_count or source_count,
        priority_ranked_count=priority_ranked_n,
        scored_count=scored_n,
        watching_count=max(watching_count, len(active_eval), 1),
        promoted_count=len(promotions),
        active_count=active_count,
        broker_listing_count=source_pool,
        drops_eligible=drops_eligible,
        drops_watching=drops_watching,
        drops_scored=drops_scored,
        budget_block=budget_block if isinstance(budget_block, dict) else None,
    )

    symbols_ui = _symbols_fallback(
        pipeline_syms, core_list + scan_list, scores, caps, cfg,
        intel_disabled=False, intel=intel,
        score_ages_by_symbol=score_ages_by_symbol,
    )
    stream = _promotion_stream(symbols_ui, promotions)
    coverage_d118 = _asset_class_coverage_block(symbols_ui)

    return {
        "enabled": True,
        "fallback": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "funnel": funnel,
        "symbols": symbols_ui,
        "clusters": clusters,
        "promotions": promotions,
        "stream": stream,
        "config_mirror": _config_mirror(cfg, caps),
        "build": _build_info(intel.last_full_cluster_at or (tiers.updated_at if tiers else None), cfg),
        "broker_totals": broker_totals,
        "coverage": source_detail,
        "core_intel": core_intel,
        "cold_scan": cold,
        "active_eval": active_eval,
        "adaptive": adaptive_block,
        "priority_rule": priority_rule_block,
        "transitions": transitions_rows,
        "asset_class_coverage": coverage_d118,
    }


def _normalised_broker_symbols(broker_symbols: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for broker, symbols in broker_symbols.items():
        vals: list[str] = []
        for raw in symbols:
            sym = _to_yf_symbol(str(raw), broker)
            if sym:
                vals.append(sym.strip().upper())
        out[str(broker)] = list(dict.fromkeys(vals))
    return out


def _drop_rows(rows: list[tuple[str, int]], *, fallback: Any = None) -> list[dict[str, Any]]:
    out = [{"reason": reason, "count": int(max(0, count))} for reason, count in rows if int(max(0, count)) > 0]
    if out:
        return out
    if isinstance(fallback, list):
        return fallback
    return [{"reason": "No measured drops at this stage", "count": 0}]


def _source_detail(
    *,
    broker_totals: dict[str, int],
    normalised_by_broker: dict[str, list[str]],
    unique_source_count: int,
    scored_count: int,
    watching_count: int,
    caps: dict[str, int],
    registry_summary_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the ``coverage`` block for the universe snapshot.

    D116: when an ``registry_summary_data`` dict is supplied (produced by
    :func:`instruments.registry.registry_summary`) we annotate each
    broker with ``registry_known_count`` (total non-unknown rows) and
    ``registry_covered_count`` (``available`` rows).
    """
    by_broker_status = (registry_summary_data or {}).get("by_broker_status") or {}
    registry_active = int((registry_summary_data or {}).get("active") or 0)
    coverage: dict[str, Any] = {
        "broker_listing_count": int(sum(broker_totals.values())),
        "unique_normalized_count": int(unique_source_count),
        "scored_candidate_count": int(scored_count),
        "watched_count": int(watching_count),
        "registry_active_count": registry_active,
        "caps": {
            "candidates": int(caps["candidates"]),
            "watching": int(caps["max_symbols"]),
            "core": int(caps["core_max"]),
            "scan": int(caps["scan_max"]),
        },
    }
    # D116: detect whether IBKR is allowed to union its curated seed with the
    # instrument registry. Note and source labels are derived from the actual
    # raw count rather than the YAML flag alone, so the dashboard reflects the
    # adapter's true behaviour after a registry rebuild.
    ibkr_registry_union_enabled = False
    try:
        from instruments.builder import load_config as _load_registry_config

        ibkr_registry_union_enabled = bool(
            _load_registry_config().ibkr_supported_symbols_use_registry
        )
    except Exception:
        ibkr_registry_union_enabled = False

    by_broker: dict[str, Any] = {}
    for broker, symbols in normalised_by_broker.items():
        statuses = by_broker_status.get(broker) or by_broker_status.get(broker.lower()) or {}
        registry_covered = int(statuses.get("available") or 0)
        registry_known = int(
            registry_covered
            + int(statuses.get("requires_qualification") or 0)
            + int(statuses.get("unavailable") or 0)
            + int(statuses.get("blocked") or 0)
        )
        raw_count = int(broker_totals.get(broker, 0))

        if broker.lower() == "ibkr":
            if ibkr_registry_union_enabled and raw_count > 200:
                source_label = "curated_seed+registry"
                note_label = (
                    "IBKR has no list-all endpoint — union of curated YAML seed, "
                    "qualification cache, and D116 instrument registry. Orders "
                    "still call qualifyContractsAsync before submission."
                )
            else:
                source_label = "curated_seed"
                note_label = (
                    "IBKR/TWS has no practical list-all endpoint; using curated "
                    "IBKR tradable seed. Flip "
                    "ibkr_supported_symbols_use_registry in "
                    "config/instrument_registry.yaml to extend coverage."
                )
        else:
            source_label = "broker_catalog"
            note_label = None

        by_broker[broker] = {
            "raw": raw_count,
            "normalized": len(symbols),
            "source": source_label,
            "note": note_label,
            "registry_known_count": registry_known,
            "registry_covered_count": registry_covered,
        }
    coverage["by_broker"] = by_broker
    return coverage


def _build_info(last_at: str | None, cfg: dict[str, Any], *, state: str | None = None) -> dict[str, Any]:
    rb = cfg.get("rebuild") or {}
    # D118: the orchestrator now rebuilds the dynamic universe from the
    # data-pipeline loop, not the legacy universe_selection.yaml rebuild
    # timer. Surface the effective pipeline interval so the dashboard
    # countdown does not claim a 24h refresh cycle.
    interval = int(os.getenv("PIPELINE_INTERVAL_SEC", "3600"))
    now = datetime.now(timezone.utc)
    last_dt = _parse_dt(last_at)
    if state is None:
        if last_dt is None:
            resolved_state = "missing"
        else:
            age = now - last_dt.astimezone(timezone.utc)
            resolved_state = "stale" if age > timedelta(seconds=max(interval * 2, interval + 300)) else "fresh"
    else:
        resolved_state = state
    next_dt = (last_dt.astimezone(timezone.utc) + timedelta(seconds=interval)) if last_dt else now
    return {
        "state": resolved_state,
        "lastBuildAt": last_at,
        "nextBuildAt": next_dt.isoformat(),
        "loopId": 0,
        "durationMs": int(rb.get("last_duration_ms", 0)),
        "intervalSec": interval,
    }


def _config_mirror(cfg: dict[str, Any], caps: dict[str, int]) -> dict[str, Any]:
    cap = cfg.get("capacity") or {}
    return {
        "capacity": {
            "source": cap.get("source"),
            "watching": cap.get("watching", caps["max_symbols"]),
            "core": cap.get("core", caps["core_max"]),
            "scan": cap.get("scan", caps["scan_max"]),
            "candidates": cap.get("candidates", caps["candidates"]),
        },
        "filters": cfg.get("filters") or {},
        "factorWeights": cfg.get("factor_weights") or {},
        "promotion": cfg.get("promotion") or {},
        "rebuild": cfg.get("rebuild") or {},
    }


def _symbols_fallback(
    pipeline_syms: list[str],
    tier_flat: list[str],
    scores: dict[str, float],
    caps: dict[str, int],
    cfg: dict[str, Any],
    *,
    intel_disabled: bool,
    intel: UniverseIntelligenceState | None,
    score_ages_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Lightweight symbol rows for UI grid (not full book)."""
    syms = tier_flat if tier_flat else pipeline_syms
    if not syms:
        syms = ["SPY", "QQQ", "BTC-USD"]
    core_max = caps["core_max"]
    out: list[dict[str, Any]] = []
    pair_syms = set()
    promoted_syms = set()
    active_syms = set()
    if intel and intel.clusters:
        for cl in intel.clusters:
            mem = cl.get("members") or []
            if len(mem) > 1:
                pair_syms.update(str(m).upper() for m in mem)
    if intel:
        promoted_syms = {
            str(p.get("symbol", "")).upper()
            for p in (intel.promotions or [])
            if isinstance(p, dict) and str(p.get("symbol", "")).strip()
        }
        active_syms = {str(s).upper() for s in (intel.core or [])}
        # D118 dashboard tabs compare row-level counts against funnel
        # counts. Keep every active representative and promoted symbol in
        # the row payload, then append the full current watchlist. The old
        # 400-row cap made Instruments/Composition disagree with the funnel
        # whenever watching > 400.
        syms = list(
            dict.fromkeys(
                [s for s in (intel.core or []) if str(s).strip()]
                + [s for s in promoted_syms if str(s).strip()]
                + list(syms)
            )
        )

    for i, sym in enumerate(syms):
        sc = float(scores.get(sym.upper(), 50.0 + (i % 17)))
        su = sym.upper()
        stage = "watching"
        if intel and su in promoted_syms:
            stage = "promoted"
        elif intel and su in active_syms:
            stage = "active_reps"
        elif not intel and i < min(7, len(syms) // 15):
            stage = "active_reps"
        elif not intel and i < min(27, len(syms) // 5):
            stage = "promoted"
        if intel_disabled:
            stage = "watching" if i >= min(7, len(syms) // 15) else stage
        tier_reason = "core" if su in active_syms or i < core_max else "scan"
        klass = _classify_symbol(sym)
        spark = [max(0, min(100, sc + j * 2 - 10)) for j in range(12)]
        cat_name, cat_sector = _catalog_lookup(sym)
        sector_ui = cat_sector if cat_sector else "general"
        pres = _instrument_presentation(sym, klass, cat_name, sector_ui)
        name_ui: str | None = pres.get("name")
        description_ui = str(pres.get("description") or f"{su} · {klass}")
        # D118 — attach per-symbol score-age + last-score telemetry when
        # available. The UI uses ``last_scored_at`` for the age stripe
        # and ``priority_breakdown`` (when present on a future API
        # extension) for the inspector tooltip.
        age_info = (
            (score_ages_by_symbol or {}).get(su) if score_ages_by_symbol else None
        )
        row = {
            "sym": su,
            "name": name_ui,
            "description": description_ui,
            "category": pres.get("category"),
            "logo_url": logo_url_for_symbol(su, klass=klass),
            "logo_kind": pres.get("logo_kind"),
            "exchange": pres.get("exchange"),
            "currency": pres.get("currency"),
            "industry": pres.get("industry"),
            "klass": klass,
            "sector": sector_ui,
            "stage": stage,
            "conviction": int(min(100, max(0, sc + (i % 11) - 5))),
            "trend": "rising" if i % 3 else "steady",
            "factors": {
                "momentum": int(sc) % 100,
                "liquidity": int(sc + 10) % 100,
                "correlation": int(sc / 2) % 100,
                "news": int(sc / 3) % 100,
            },
            "spread": round(0.5 + (i % 20) * 0.4, 1),
            "spark": spark,
            "bookCorr": round((i % 17) / 33 - 0.5, 2),
            "tierReason": tier_reason,
            "override": None,
            "pairWatch": su in pair_syms,
            "last_scored_at": age_info.get("last_scored_at") if isinstance(age_info, dict) else None,
            "last_score": age_info.get("last_score") if isinstance(age_info, dict) else None,
            "score_count": age_info.get("score_count") if isinstance(age_info, dict) else None,
        }
        out.append(row)
    return out


def _classify_symbol(sym: str) -> str:
    s = sym.upper()
    if "-" in s or s.endswith("USD"):
        return "crypto"
    if len(s) <= 4 and s in {"ES", "NQ", "YM", "CL", "GC"} or s.endswith("=F"):
        return "etf"
    if "=" in s or s.endswith("=X"):
        return "fx"
    etfs = {"SPY", "QQQ", "IWM", "XLK", "XLE", "TLT", "GLD", "EEM"}
    if s in etfs:
        return "etf"
    return "equity"


def _promotion_stream(symbols: list[dict[str, Any]], promotions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stream: list[dict[str, Any]] = []
    by_symbol = {str(s.get("sym", "")).upper(): s for s in symbols if isinstance(s, dict)}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for p in promotions:
        sym = str(p.get("symbol", "")).upper()
        if not sym:
            continue
        row = by_symbol.get(sym) or {}
        promoted_at = _parse_dt(str(p.get("promoted_at") or "") or None)
        stream.append(
            {
                "sym": sym,
                "klass": row.get("klass") or _classify_symbol(sym),
                "why": str(p.get("reason", "promoted")),
                "conviction": int(float(p.get("conviction", row.get("conviction", 72)) or 72)),
                "trend": row.get("trend") or "rising",
                "promotedAt": int(promoted_at.timestamp() * 1000) if promoted_at else now_ms,
                "spark": row.get("spark") or [60, 62, 65, 70, 72],
                "bookCorr": row.get("bookCorr", 0.1),
                "topFactors": [
                    ("conviction", str(p.get("conviction", "")) or "n/a"),
                    ("score", str(p.get("score", "")) or "n/a"),
                ],
                "relatedNews": [],
            }
        )
    if stream:
        return stream[:12]
    return []
