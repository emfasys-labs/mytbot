from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from storage.models import DailyPnL, OrderLog, RiskLog, SignalLog
from system.live_arming import (
    IBKR_LIVE_ARMING_PHRASE,
    LIVE_ARMING_PHRASE,
    is_ibkr_live_armed,
    is_live_armed,
)

DEPLOYMENT_STATE_KEY = "deployment.stage"
DEPLOYMENT_HISTORY_KEY = "deployment.stage_history"

STAGES = ("paper", "micro_live", "live")
FULL_LIVE_ARMING_ENV = "MYTBOT_FULL_LIVE_ARMED"
FULL_LIVE_ARMING_PHRASE = "I_UNDERSTAND_FULL_LIVE_RISK"


@dataclass(frozen=True)
class DeploymentCheck:
    key: str
    label: str
    passed: bool
    required: bool = True
    detail: str = ""
    current: float | int | str | None = None
    target: float | int | str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "required": self.required,
            "detail": self.detail,
            "current": self.current,
            "target": self.target,
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return raw if isinstance(raw, dict) else {}
    except OSError:
        return {}


def load_deployment_config(path: str | Path = "config/deployment.yaml") -> dict[str, Any]:
    raw = _load_yaml(path)
    req = raw.get("requirements")
    if not isinstance(req, dict):
        req = {}
    return {
        "stage": _normalise_stage(raw.get("stage", "paper")),
        "promotion_locked": bool(raw.get("promotion_locked", True)),
        "requirements": {
            "paper_soak_days": int(req.get("paper_soak_days", 14)),
            "paper_min_signals": int(req.get("paper_min_signals", 50)),
            "paper_min_risk_decisions": int(req.get("paper_min_risk_decisions", 50)),
            "paper_max_drawdown_pct": float(req.get("paper_max_drawdown_pct", 3.0)),
            "paper_max_reject_rate_pct": float(req.get("paper_max_reject_rate_pct", 85.0)),
            "micro_live_soak_days": int(req.get("micro_live_soak_days", 7)),
            "micro_live_min_fills": int(req.get("micro_live_min_fills", 10)),
            "micro_live_max_drawdown_pct": float(req.get("micro_live_max_drawdown_pct", 1.5)),
            "micro_live_max_reject_rate_pct": float(req.get("micro_live_max_reject_rate_pct", 75.0)),
            "micro_live_max_order_notional_usd": float(
                req.get("micro_live_max_order_notional_usd", 250.0)
            ),
        },
    }


def _normalise_stage(value: Any) -> str:
    stage = str(value or "paper").strip().lower().replace("-", "_")
    return stage if stage in STAGES else "paper"


def _days_since_iso(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)


async def get_configured_stage(bus: Any | None = None) -> str:
    if bus is not None:
        try:
            raw = await bus.get_state(DEPLOYMENT_STATE_KEY, None)
            if isinstance(raw, dict) and raw.get("stage"):
                return _normalise_stage(raw.get("stage"))
            if isinstance(raw, str):
                return _normalise_stage(raw)
        except Exception:  # noqa: BLE001
            pass
    return str(load_deployment_config().get("stage", "paper"))


async def set_stage_override(bus: Any, stage: str, *, source: str = "api") -> dict[str, Any]:
    next_stage = _normalise_stage(stage)
    now = _utcnow_iso()
    raw_hist = await bus.get_state(DEPLOYMENT_HISTORY_KEY, None)
    history = raw_hist if isinstance(raw_hist, dict) else {}
    entry = history.get(next_stage)
    if not isinstance(entry, dict):
        entry = {}
    if not entry.get("started_at"):
        entry["started_at"] = now
    entry["last_selected_at"] = now
    entry["source"] = source
    history[next_stage] = entry
    await bus.set_state(DEPLOYMENT_HISTORY_KEY, history)
    payload = {"stage": next_stage, "updated_at": now, "source": source}
    await bus.set_state(DEPLOYMENT_STATE_KEY, payload)
    return payload


async def _fetch_stage_history(bus: Any | None) -> dict[str, Any]:
    if bus is None:
        return {}
    try:
        raw = await bus.get_state(DEPLOYMENT_HISTORY_KEY, None)
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _evidence_from_db(session_factory: Any | None, history: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "paper": {"days_observed": 0, "signals": 0, "risk_decisions": 0, "fills": 0},
        "micro_live": {"days_observed": 0, "fills": 0, "orders": 0, "risk_decisions": 0},
    }
    for stage in ("paper", "micro_live"):
        hist = history.get(stage)
        if isinstance(hist, dict):
            evidence[stage]["started_at"] = hist.get("started_at")
            evidence[stage]["days_observed"] = _days_since_iso(hist.get("started_at"))

    if session_factory is None:
        return evidence

    async with session_factory() as session:
        sig_count = await session.scalar(select(func.count(SignalLog.id)))
        risk_count = await session.scalar(select(func.count(RiskLog.id)))
        paper_orders = await session.scalar(
            select(func.count(OrderLog.id)).where(OrderLog.paper_mode.is_(True))
        )
        live_orders = await session.scalar(
            select(func.count(OrderLog.id)).where(OrderLog.paper_mode.is_(False))
        )
        paper_first = await session.scalar(select(func.min(SignalLog.timestamp)))
        if paper_first is None:
            paper_first = await session.scalar(
                select(func.min(OrderLog.timestamp)).where(OrderLog.paper_mode.is_(True))
            )
        live_first = await session.scalar(
            select(func.min(OrderLog.timestamp)).where(OrderLog.paper_mode.is_(False))
        )
        rejected = await session.scalar(
            select(func.count(RiskLog.id)).where(func.lower(RiskLog.verdict) == "rejected")
        )
        approved = await session.scalar(
            select(func.count(RiskLog.id)).where(func.lower(RiskLog.verdict) == "approved")
        )
        pnl_rows = list((await session.execute(select(DailyPnL).order_by(DailyPnL.date.asc()))).scalars().all())

    evidence["paper"]["signals"] = int(sig_count or 0)
    evidence["paper"]["risk_decisions"] = int(risk_count or 0)
    evidence["paper"]["fills"] = int(paper_orders or 0)
    evidence["micro_live"]["orders"] = int(live_orders or 0)
    evidence["micro_live"]["fills"] = int(live_orders or 0)
    evidence["micro_live"]["risk_decisions"] = int(risk_count or 0)

    if not evidence["paper"].get("started_at") and paper_first is not None:
        evidence["paper"]["started_at"] = paper_first.isoformat()
        evidence["paper"]["days_observed"] = max(
            evidence["paper"]["days_observed"], _days_since_iso(paper_first.isoformat())
        )
    if not evidence["micro_live"].get("started_at") and live_first is not None:
        evidence["micro_live"]["started_at"] = live_first.isoformat()
        evidence["micro_live"]["days_observed"] = max(
            evidence["micro_live"]["days_observed"], _days_since_iso(live_first.isoformat())
        )

    total_risk = int(approved or 0) + int(rejected or 0)
    reject_rate = (float(rejected or 0) / total_risk * 100.0) if total_risk else 0.0
    dd = _max_drawdown_pct([Decimal(str(getattr(r, "portfolio_value", 0) or 0)) for r in pnl_rows])
    evidence["paper"]["reject_rate_pct"] = round(reject_rate, 2)
    evidence["paper"]["max_drawdown_pct"] = round(dd, 2)
    evidence["micro_live"]["reject_rate_pct"] = round(reject_rate, 2)
    evidence["micro_live"]["max_drawdown_pct"] = round(dd, 2)
    return evidence


def _max_drawdown_pct(values: list[Decimal]) -> float:
    peak = Decimal("0")
    worst = Decimal("0")
    for v in values:
        if v <= 0:
            continue
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * Decimal("100")
            if dd > worst:
                worst = dd
    return float(worst)


def _m8_profile() -> dict[str, Any]:
    return _load_yaml("config/m8_micro_live.yaml")


def _env_checks(stage: str, req: dict[str, Any]) -> list[DeploymentCheck]:
    app_env = os.getenv("APP_ENV", "paper").strip().lower()
    checks: list[DeploymentCheck] = []
    live_stage = stage in {"micro_live", "live"}
    if stage == "paper":
        checks.append(
            DeploymentCheck(
                "paper_env",
                "Runtime is paper-safe",
                app_env != "live",
                detail=f"APP_ENV={app_env or 'paper'}",
                current=app_env or "paper",
                target="not live",
            )
        )
    else:
        checks.extend(
            [
                DeploymentCheck(
                    "app_env_live",
                    "APP_ENV is live",
                    app_env == "live",
                    detail=f"APP_ENV={app_env or 'paper'}",
                    current=app_env or "paper",
                    target="live",
                ),
                DeploymentCheck(
                    "live_armed",
                    "Real-order arming phrase is set",
                    is_live_armed(),
                    detail=f"Set MYTBOT_LIVE_ARMED={LIVE_ARMING_PHRASE}",
                    current="set" if is_live_armed() else "missing",
                    target="set",
                ),
                DeploymentCheck(
                    "dashboard_tokens",
                    "Dashboard read and mutation tokens are configured",
                    bool(os.getenv("DASHBOARD_READ_TOKEN", "").strip())
                    and bool(os.getenv("API_CONTROL_TOKEN", "").strip()),
                    detail="Live stages require DASHBOARD_READ_TOKEN and API_CONTROL_TOKEN.",
                    current="set"
                    if os.getenv("DASHBOARD_READ_TOKEN", "").strip()
                    and os.getenv("API_CONTROL_TOKEN", "").strip()
                    else "missing",
                    target="set",
                ),
            ]
        )

    ibkr_port = os.getenv("IBKR_PORT", "").strip()
    if ibkr_port:
        if live_stage:
            checks.append(
                DeploymentCheck(
                    "ibkr_live_port",
                    "IBKR live route is explicitly armed",
                    ibkr_port == "7496" and is_ibkr_live_armed(),
                    detail=f"Use IBKR_PORT=7496 and IBKR_LIVE_ARMED={IBKR_LIVE_ARMING_PHRASE}.",
                    current=f"port {ibkr_port}, armed={'yes' if is_ibkr_live_armed() else 'no'}",
                    target="7496 armed",
                )
            )
        else:
            checks.append(
                DeploymentCheck(
                    "ibkr_paper_port",
                    "IBKR is not pointed at the live port",
                    ibkr_port != "7496",
                    detail="Paper stage refuses the standard IBKR live port 7496.",
                    current=ibkr_port,
                    target="not 7496",
                )
            )

    if stage == "micro_live":
        m8 = _m8_profile()
        caps = []
        for key in ("max_notional_usd_per_order", "max_notional_gbp_per_order"):
            raw = m8.get(key)
            if raw is not None:
                try:
                    val = float(raw)
                    if key.endswith("gbp_per_order"):
                        val *= float(os.getenv("M8_GBP_USD_RATE", "1.25"))
                    caps.append(val)
                except (TypeError, ValueError):
                    pass
        cap = min(caps) if caps else 0.0
        cap_target = float(req.get("micro_live_max_order_notional_usd", 250.0))
        checks.extend(
            [
                DeploymentCheck(
                    "m8_enabled",
                    "Micro-live risk profile is enabled",
                    bool(m8.get("enabled")),
                    detail="config/m8_micro_live.yaml must have enabled: true.",
                    current=str(bool(m8.get("enabled"))).lower(),
                    target="true",
                ),
                DeploymentCheck(
                    "m8_symbol_whitelist",
                    "Micro-live symbol whitelist is non-empty",
                    bool(m8.get("symbol_whitelist")),
                    detail="Only whitelisted symbols may trade during micro-live.",
                    current=len(m8.get("symbol_whitelist") or []),
                    target="> 0",
                ),
                DeploymentCheck(
                    "m8_strategy_whitelist",
                    "Micro-live strategy whitelist is non-empty",
                    bool(m8.get("strategy_whitelist")),
                    detail="Only reviewed strategies may trade during micro-live.",
                    current=len(m8.get("strategy_whitelist") or []),
                    target="> 0",
                ),
                DeploymentCheck(
                    "m8_order_cap",
                    "Micro-live order cap is tiny",
                    cap > 0 and cap <= cap_target,
                    detail="Keep real-money rollout small and visibly bounded.",
                    current=round(cap, 2),
                    target=f"<= {cap_target:g}",
                ),
            ]
        )

    if stage == "live":
        cfg = load_deployment_config()
        checks.extend(
            [
                DeploymentCheck(
                    "promotion_unlocked",
                    "Full-live promotion lock is open",
                    not bool(cfg.get("promotion_locked", True)),
                    detail="config/deployment.yaml promotion_locked must be false.",
                    current="locked" if cfg.get("promotion_locked", True) else "open",
                    target="open",
                ),
                DeploymentCheck(
                    "full_live_armed",
                    "Full-live arming phrase is set",
                    os.getenv(FULL_LIVE_ARMING_ENV, "").strip() == FULL_LIVE_ARMING_PHRASE,
                    detail=f"Set {FULL_LIVE_ARMING_ENV}={FULL_LIVE_ARMING_PHRASE}.",
                    current="set"
                    if os.getenv(FULL_LIVE_ARMING_ENV, "").strip() == FULL_LIVE_ARMING_PHRASE
                    else "missing",
                    target="set",
                ),
            ]
        )
    return checks


def _evidence_checks(stage: str, evidence: dict[str, Any], req: dict[str, Any]) -> list[DeploymentCheck]:
    paper = evidence.get("paper", {})
    micro = evidence.get("micro_live", {})
    checks: list[DeploymentCheck] = []
    if stage in {"micro_live", "live"}:
        days = int(paper.get("days_observed") or 0)
        target = int(req.get("paper_soak_days", 14))
        checks.extend(
            [
                DeploymentCheck("paper_days", "Paper soak days complete", days >= target, current=days, target=target),
                DeploymentCheck(
                    "paper_signals",
                    "Paper signal sample is large enough",
                    int(paper.get("signals") or 0) >= int(req.get("paper_min_signals", 50)),
                    current=int(paper.get("signals") or 0),
                    target=int(req.get("paper_min_signals", 50)),
                ),
                DeploymentCheck(
                    "paper_risk_decisions",
                    "Paper risk decisions are observable",
                    int(paper.get("risk_decisions") or 0) >= int(req.get("paper_min_risk_decisions", 50)),
                    current=int(paper.get("risk_decisions") or 0),
                    target=int(req.get("paper_min_risk_decisions", 50)),
                ),
                DeploymentCheck(
                    "paper_drawdown",
                    "Paper drawdown is inside limit",
                    float(paper.get("max_drawdown_pct") or 0) <= float(req.get("paper_max_drawdown_pct", 3.0)),
                    current=float(paper.get("max_drawdown_pct") or 0),
                    target=f"<= {float(req.get('paper_max_drawdown_pct', 3.0)):g}%",
                ),
                DeploymentCheck(
                    "paper_reject_rate",
                    "Paper rejection rate is understood",
                    float(paper.get("reject_rate_pct") or 0) <= float(req.get("paper_max_reject_rate_pct", 85.0)),
                    current=float(paper.get("reject_rate_pct") or 0),
                    target=f"<= {float(req.get('paper_max_reject_rate_pct', 85.0)):g}%",
                ),
            ]
        )
    if stage == "live":
        days = int(micro.get("days_observed") or 0)
        target = int(req.get("micro_live_soak_days", 7))
        checks.extend(
            [
                DeploymentCheck("micro_live_days", "Micro-live days complete", days >= target, current=days, target=target),
                DeploymentCheck(
                    "micro_live_fills",
                    "Micro-live fill sample is large enough",
                    int(micro.get("fills") or 0) >= int(req.get("micro_live_min_fills", 10)),
                    current=int(micro.get("fills") or 0),
                    target=int(req.get("micro_live_min_fills", 10)),
                ),
                DeploymentCheck(
                    "micro_live_drawdown",
                    "Micro-live drawdown is inside limit",
                    float(micro.get("max_drawdown_pct") or 0) <= float(req.get("micro_live_max_drawdown_pct", 1.5)),
                    current=float(micro.get("max_drawdown_pct") or 0),
                    target=f"<= {float(req.get('micro_live_max_drawdown_pct', 1.5)):g}%",
                ),
                DeploymentCheck(
                    "micro_live_reject_rate",
                    "Micro-live rejection rate is understood",
                    float(micro.get("reject_rate_pct") or 0)
                    <= float(req.get("micro_live_max_reject_rate_pct", 75.0)),
                    current=float(micro.get("reject_rate_pct") or 0),
                    target=f"<= {float(req.get('micro_live_max_reject_rate_pct', 75.0)):g}%",
                ),
            ]
        )
    return checks


def _promotion_action_blockers(stage: str, next_stage: str | None, blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Blockers that must clear before the stage selection can be persisted.

    Some checks are restart/runtime checks. Example: while currently running
    paper, APP_ENV is expected to be paper; after promotion the operator edits
    env and restarts into micro-live, where startup validation enforces APP_ENV
    and broker live-port correctness before broker connect.
    """
    if stage == "paper" and next_stage == "micro_live":
        restart_keys = {"app_env_live", "ibkr_live_port"}
        return [b for b in blockers if str(b.get("key", "")) not in restart_keys]
    return blockers


async def build_deployment_readiness(
    *,
    bus: Any | None = None,
    session_factory: Any | None = None,
    requested_stage: str | None = None,
) -> dict[str, Any]:
    cfg = load_deployment_config()
    current_stage = _normalise_stage(await get_configured_stage(bus))
    stage = _normalise_stage(requested_stage or current_stage)
    req = cfg["requirements"]
    history = await _fetch_stage_history(bus)
    evidence = await _evidence_from_db(session_factory, history)
    current_next_stage = "micro_live" if current_stage == "paper" else "live" if current_stage == "micro_live" else None
    check_stage = _normalise_stage(requested_stage or current_next_stage or current_stage)
    checks = _env_checks(check_stage, req) + _evidence_checks(check_stage, evidence, req)
    required = [c for c in checks if c.required]
    passed = sum(1 for c in required if c.passed)
    blockers = [c.as_dict() for c in required if not c.passed]
    next_stage = "micro_live" if stage == "paper" else "live" if stage == "micro_live" else None
    action_blockers = _promotion_action_blockers(stage, next_stage, blockers)
    if stage == "paper":
        days_left = max(0, int(req["paper_soak_days"]) - int(evidence["paper"].get("days_observed") or 0))
    elif stage == "micro_live":
        days_left = max(
            0, int(req["micro_live_soak_days"]) - int(evidence["micro_live"].get("days_observed") or 0)
        )
    else:
        days_left = 0
    return {
        "stage": stage,
        "runtime_env": os.getenv("APP_ENV", "paper").strip().lower() or "paper",
        "paper_mode": os.getenv("APP_ENV", "paper").strip().lower() != "live",
        "next_stage": next_stage,
        "promotion_ready": not blockers and next_stage is not None,
        "promotion_action_ready": not action_blockers and next_stage is not None,
        "checks_passed": passed,
        "checks_total": len(required),
        "days_left": days_left,
        "requirements": req,
        "evidence": evidence,
        "checks": [c.as_dict() for c in checks],
        "blockers": blockers,
        "promotion_action_blockers": action_blockers,
        "updated_at": _utcnow_iso(),
    }


def validate_deployment_startup() -> None:
    """Fail fast for env/config-only deployment mistakes before broker connect."""
    stage = str(load_deployment_config().get("stage", "paper"))
    checks = _env_checks(stage, load_deployment_config()["requirements"])
    blockers = [c for c in checks if c.required and not c.passed]
    if blockers:
        joined = "; ".join(f"{c.key}: {c.detail or c.label}" for c in blockers)
        raise RuntimeError(f"deployment startup validation failed for stage={stage}: {joined}")
