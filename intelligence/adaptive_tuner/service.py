from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intelligence.adaptive_tuner.ai_advisor import TunerAIAdvisor
from intelligence.adaptive_tuner.optimizer import (
    attribute_and_propose,
    current_overrides,
    empty_state,
)
from intelligence.adaptive_tuner.registry import load_tuner_config
from intelligence.adaptive_tuner.schema import TunerConfig
from storage.models import FillLog, ParameterTuningLog


def _resolve_dotted(block: dict[str, Any], dotted: str) -> Any:
    cur: Any = block
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _load_param_defaults(
    params: tuple,
    strategies_path: str = "config/strategies.yaml",
    trade_admission_path: str = "config/trade_admission.yaml",
) -> dict[str, Decimal]:
    """Seed defaults from the live YAML so the optimizer starts at config values."""
    out: dict[str, Decimal] = {}
    try:
        strategies_raw = yaml.safe_load(Path(strategies_path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        strategies_raw = {}
    try:
        admission_raw = yaml.safe_load(Path(str(trade_admission_path)).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        admission_raw = {}
    for p in params:
        if p.namespace == "trade_admission":
            block = admission_raw
        else:
            block = strategies_raw.get(p.namespace) or {}
        val = _resolve_dotted(block, p.name)
        if val is not None:
            try:
                out[p.key] = p.clamp(Decimal(str(val)))
            except Exception:  # noqa: BLE001
                pass
    return out


class AdaptiveTunerService:
    """Live, bounded, regime-conditioned parameter self-tuning."""

    def __init__(self, cfg: TunerConfig | None = None):
        self.cfg = cfg or load_tuner_config()
        self.defaults = _load_param_defaults(self.cfg.params)
        self.state = self._load_state()
        self.advisor = TunerAIAdvisor() if self.cfg.ai_advisor_enabled else None
        self._last_ai_cycle = -(10**9)

    # ── persistence ───────────────────────────────────────────────────────
    def _load_state(self) -> dict[str, Any]:
        p = Path(self.cfg.state_path)
        if not p.exists():
            return empty_state()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "params" in data:
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("adaptive_tuner | corrupt state, starting fresh | {}", exc)
        return empty_state()

    def _persist_state(self) -> None:
        p = Path(self.cfg.state_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", delete=False, dir=str(p.parent), encoding="utf-8", suffix=".tmp"
            ) as tmp:
                json.dump(self.state, tmp, ensure_ascii=False)
            os.replace(tmp.name, str(p))
        except Exception as exc:  # noqa: BLE001
            logger.debug("adaptive_tuner | state persist failed | {}", exc)

    # ── live override surface (consumed by the trading loop) ──────────────
    def overrides_for(self, namespace: str, regime: str) -> dict[str, Decimal]:
        """Resolved {param_name: value} (dotted names preserved) for a namespace."""
        if not self.cfg.enabled:
            return {}
        all_ns = current_overrides(self.state, self.cfg.params, regime, self.cfg)
        return all_ns.get(namespace, {})

    # ── reward signal ─────────────────────────────────────────────────────
    async def _recent_reward(
        self, session_factory: async_sessionmaker[AsyncSession] | None, nav: Decimal
    ) -> tuple[Decimal, dict[str, Any]]:
        """Net realized P&L over the attribution window, normalized by NAV."""
        if session_factory is None or nav <= 0:
            return Decimal("0"), {"fills": 0, "net_pnl": "0"}
        since = datetime.now(timezone.utc) - timedelta(hours=self.cfg.attribution_window_hours)
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(FillLog.realised_pnl - FillLog.fee), 0),
                        func.count(FillLog.id),
                    ).where(FillLog.timestamp >= since)
                )
            ).one()
        net = Decimal(str(row[0] or 0))
        fills = int(row[1] or 0)
        reward = (net / nav) if nav > 0 else Decimal("0")
        return reward, {"fills": fills, "net_pnl": str(net)}

    # ── main cycle ────────────────────────────────────────────────────────
    async def maybe_run_cycle(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        regime: str,
        nav: Decimal,
        loop_iteration: int,
    ) -> dict[str, Any] | None:
        if not self.cfg.enabled or not self.cfg.params:
            return None
        if self.cfg.apply_every_n_cycles > 0 and (loop_iteration % self.cfg.apply_every_n_cycles) != 0:
            return None

        reward, fills_summary = await self._recent_reward(session_factory, nav)

        # Reward trend for AI context.
        trend = list(self.state.get("reward_trend", []))[-8:]
        trend.append(float(reward))
        self.state["reward_trend"] = trend[-8:]

        # Optional AI advisor (rate-limited, advisory only).
        ai_hints: dict[str, str] = {}
        ai_rationale = ""
        if (
            self.advisor is not None
            and self.advisor.available
            and (self.state.get("cycles", 0) - self._last_ai_cycle) >= self.cfg.ai_min_cycles_between_calls
        ):
            try:
                ctx = self._ai_context(regime, reward, trend, fills_summary)
                ai_hints = await self.advisor.advise(ctx)
                ai_rationale = ai_hints.pop("_rationale", "")
                self._last_ai_cycle = int(self.state.get("cycles", 0))
            except Exception as exc:  # noqa: BLE001
                logger.debug("adaptive_tuner | ai advisor skipped | {}", exc)

        self.state, proposals = attribute_and_propose(
            self.state,
            self.cfg,
            self.cfg.params,
            reward=reward,
            regime=regime,
            defaults=self.defaults,
            ai_hints=ai_hints,
        )
        self._persist_state()

        applied = [p for p in proposals if p.new_value != p.old_value]
        if applied:
            logger.info(
                "adaptive_tuner | regime={} reward={:.5f} applied={} | {}",
                regime, float(reward), len(applied),
                ", ".join(f"{p.param_key}:{p.old_value}->{p.new_value}" for p in applied[:6]),
            )
            await self._log_applied(session_factory, applied, regime, reward, ai_rationale)

        return {
            "regime": regime,
            "reward": float(reward),
            "applied": len(applied),
            "ai_used": bool(ai_hints),
            "fills_summary": fills_summary,
        }

    def _ai_context(self, regime: str, reward: Decimal, trend: list, fills_summary: dict) -> dict[str, Any]:
        params_ctx = []
        for p in self.cfg.params:
            reg = regime if (self.cfg.regime_conditioned and p.regime_conditioned) else "all"
            ps = self.state.get("params", {}).get(p.key, {})
            cur = ps.get("current", {}).get(reg, float(self.defaults.get(p.key, p.min_value)))
            buckets = ps.get("buckets", {}).get(reg, {})
            best = None
            best_mean = float("-inf")
            for vstr, st in buckets.items():
                n = int(st.get("n", 0))
                if n >= self.cfg.min_samples_to_exploit:
                    mean = float(st.get("sum", 0.0)) / n
                    if mean > best_mean:
                        best_mean, best = mean, float(vstr)
            params_ctx.append(
                {
                    "key": p.key,
                    "current": round(float(cur), 4),
                    "min": float(p.min_value),
                    "max": float(p.max_value),
                    "best": best,
                }
            )
        return {
            "regime": regime,
            "reward": float(reward),
            "reward_trend": [round(x, 5) for x in trend],
            "fills_summary": fills_summary,
            "params": params_ctx,
        }

    async def _log_applied(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        applied: list,
        regime: str,
        reward: Decimal,
        ai_rationale: str,
    ) -> None:
        if session_factory is None:
            return
        try:
            async with session_factory() as session:
                for p in applied:
                    session.add(
                        ParameterTuningLog(
                            timestamp=datetime.now(timezone.utc),
                            parameter=p.param_key[:96],
                            regime=str(regime)[:32],
                            old_value=p.old_value,
                            new_value=p.new_value,
                            source=p.source[:24],
                            reward=reward,
                            rationale=(ai_rationale or p.rationale)[:2000],
                            evidence={k: _json_safe(v) for k, v in (p.evidence or {}).items()},
                        )
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("adaptive_tuner | tuning-log persist failed | {}", exc)

    # ── diagnostics ───────────────────────────────────────────────────────
    def diagnostics(self) -> dict[str, Any]:
        live: dict[str, Any] = {}
        for p in self.cfg.params:
            entry = {"namespace": p.namespace, "bounds": [float(p.min_value), float(p.max_value)]}
            ps = self.state.get("params", {}).get(p.key, {})
            entry["current"] = ps.get("current", {})
            live[p.key] = entry
        return {
            "enabled": self.cfg.enabled,
            "cycles": self.state.get("cycles", 0),
            "regime": self.state.get("last_regime", "unknown"),
            "reward_trend": self.state.get("reward_trend", []),
            "ai_advisor": bool(self.advisor and self.advisor.available),
            "tunable_count": len(self.cfg.params),
            "parameters": live,
            "recent_proposals": list(self.state.get("recent_proposals", []))[-20:][::-1],
        }


def _json_safe(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
