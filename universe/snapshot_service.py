from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from data.universe import UniverseManager
from data.universe_tiers import load_universe_tiers
from universe.persistence import DEFAULT_INTELLIGENCE_PATH, load_intelligence_state
from universe.universe_tiers import UniverseIntelligenceState

# Curated human names / sectors (data/universe.py). Pipeline symbols outside this set
# still get a generated description from class + stage.
_CATALOG_BY_SYMBOL: dict[str, tuple[str, str | None]] = {
    inst.symbol.upper(): (inst.name, inst.sector) for inst in UniverseManager.INITIAL_UNIVERSE
}


def _catalog_lookup(sym: str) -> tuple[str | None, str | None]:
    """Match pipeline/tier tickers to UniverseInstrument (handles EUR.USD, BTC-USD, etc.)."""
    u = sym.upper().strip()
    if u in _CATALOG_BY_SYMBOL:
        return _CATALOG_BY_SYMBOL[u]
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
    p = Path("config/data_pipeline.yaml")
    out = {"max_symbols": 300, "core_max": 50, "scan_max": 250, "candidates": 400}
    if not p.is_file():
        return out
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
    return out


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
    intelligence_path: Path | None = None,
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
    source_pool = int(sum(broker_totals.values())) if broker_totals else int(cfg.get("candidate_pool_default", 0))

    core_list = list(tiers.core) if tiers else []
    scan_list = list(tiers.scan) if tiers else []
    light_list = list(tiers.light) if tiers else []
    scores = dict(tiers.scores) if tiers else {}

    watching_count = len(set(core_list + scan_list)) or min(len(pipeline_syms), caps["max_symbols"])
    eligible_count = max(watching_count, min(source_pool, caps["candidates"]) if source_pool else watching_count)

    funnel_template = cfg.get("funnel_display") or {}
    drops_eligible = funnel_template.get("drops_eligible") or [
        {"reason": "Low liquidity (ADV)", "count": max(0, source_pool - eligible_count)},
        {"reason": "Asset class / region filter", "count": 0},
        {"reason": "Stale data", "count": 0},
    ]
    drops_watching = funnel_template.get("drops_watching") or [
        {"reason": f"Capacity cap ({caps['max_symbols']} max)", "count": max(0, eligible_count - watching_count)},
        {"reason": "Correlation overlap (representative kept)", "count": 0},
        {"reason": "Low opportunity score", "count": 0},
    ]

    if not enabled:
        promoted_n = min(12, max(0, watching_count // 10))
        active_n = min(7, max(0, watching_count // 40))
        funnel = [
            {"stage": "source", "count": max(source_pool, eligible_count, 1), "fresh": True, "drops": None},
            {"stage": "eligible", "count": max(eligible_count, 1), "fresh": True, "drops": drops_eligible},
            {
                "stage": "watching",
                "count": max(watching_count, 1),
                "fresh": True,
                "drops": drops_watching,
            },
            {"stage": "promoted", "count": promoted_n, "fresh": True, "drops": None},
            {"stage": "active", "count": active_n, "fresh": True, "drops": None},
            {"stage": "banned", "count": 0, "fresh": True, "drops": None},
        ]
        symbols_ui = _symbols_fallback(
            pipeline_syms, core_list + scan_list, scores, caps, cfg, intel_disabled=True, intel=None
        )
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
        }

    if intel is None:
        symbols_ui = _symbols_fallback(
            pipeline_syms, core_list + scan_list, scores, caps, cfg, intel_disabled=False, intel=None
        )
        return {
            "enabled": True,
            "fallback": "universe intelligence enabled but no build artifact yet",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "funnel": [
                {"stage": "source", "count": max(source_pool, eligible_count, 1), "fresh": True, "drops": None},
                {"stage": "eligible", "count": max(eligible_count, 1), "fresh": True, "drops": drops_eligible},
                {
                    "stage": "watching",
                    "count": max(watching_count, 1),
                    "fresh": True,
                    "drops": drops_watching,
                },
                {"stage": "promoted", "count": 0, "fresh": False, "drops": None},
                {"stage": "active", "count": min(32, max(0, watching_count // 40)), "fresh": False, "drops": None},
                {"stage": "banned", "count": 0, "fresh": True, "drops": None},
            ],
            "symbols": symbols_ui,
            "clusters": [],
            "promotions": [],
            "stream": [],
            "config_mirror": _config_mirror(cfg, caps),
            "build": _build_info(tiers.updated_at if tiers else None, cfg, state="missing"),
            "broker_totals": broker_totals,
            "core_intel": [],
            "cold_scan": [],
            "active_eval": [],
        }

    cold = list(intel.cold_scan)
    active_eval = list(intel.active_eval)
    core_intel = list(intel.core)
    clusters = deepcopy(intel.clusters)
    promotions = deepcopy(intel.promotions)

    funnel = [
        {"stage": "source", "count": max(intel.candidate_count, source_pool, 1), "fresh": True, "drops": None},
        {"stage": "eligible", "count": max(len(cold), eligible_count, 1), "fresh": True, "drops": drops_eligible},
        {
            "stage": "watching",
            "count": max(watching_count, len(active_eval), 1),
            "fresh": True,
            "drops": drops_watching,
        },
        {"stage": "promoted", "count": len(promotions), "fresh": True, "drops": None},
        {
            "stage": "active",
            "count": max(1, min(32, len(active_eval) // 15 + len(core_intel))),
            "fresh": True,
            "drops": None,
        },
        {"stage": "banned", "count": 0, "fresh": True, "drops": None},
    ]

    symbols_ui = _symbols_fallback(
        pipeline_syms, core_list + scan_list, scores, caps, cfg, intel_disabled=False, intel=intel
    )
    stream = _promotion_stream(symbols_ui, promotions)

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
        "core_intel": core_intel,
        "cold_scan": cold,
        "active_eval": active_eval,
    }


def _build_info(last_at: str | None, cfg: dict[str, Any], *, state: str | None = None) -> dict[str, Any]:
    rb = cfg.get("rebuild") or {}
    interval = int(rb.get("interval_sec", 120))
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

    for i, sym in enumerate(syms[: min(len(syms), 400)]):
        sc = float(scores.get(sym.upper(), 50.0 + (i % 17)))
        su = sym.upper()
        stage = "watching"
        if intel and su in promoted_syms:
            stage = "promoted"
        elif intel and su in active_syms:
            stage = "active"
        elif not intel and i < min(7, len(syms) // 15):
            stage = "active"
        elif not intel and i < min(27, len(syms) // 5):
            stage = "promoted"
        if intel_disabled:
            stage = "watching" if i >= min(7, len(syms) // 15) else stage
        tier_reason = "core" if i < core_max else "scan"
        klass = _classify_symbol(sym)
        spark = [max(0, min(100, sc + j * 2 - 10)) for j in range(12)]
        cat_name, cat_sector = _catalog_lookup(sym)
        sector_ui = cat_sector if cat_sector else "general"
        sec_lbl = _fmt_sector_label(sector_ui)
        if cat_name:
            name_ui: str | None = cat_name
            desc_parts = [cat_name, klass]
            if sec_lbl:
                desc_parts.append(sec_lbl)
            description_ui = " · ".join(desc_parts)
        else:
            name_ui = None
            if sec_lbl:
                description_ui = f"{su} · {klass} · {sec_lbl}"
            else:
                description_ui = f"{su} · {klass}"
        out.append(
            {
                "sym": su,
                "name": name_ui,
                "description": description_ui,
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
            }
        )
    return out


def _classify_symbol(sym: str) -> str:
    s = sym.upper()
    if "-" in s or s.endswith("USD"):
        return "crypto"
    if "=" in s or s.endswith("=X"):
        return "fx"
    if len(s) <= 4 and s in {"ES", "NQ", "YM", "CL", "GC"} or s.endswith("=F"):
        return "etf"
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
