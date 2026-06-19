from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from loguru import logger

from data.universe import UniverseManager
from data.universe_prefilter import PriorityBreakdown, top_n_by_priority
from data.universe_tiers import UniverseTiers, save_universe_tiers
from data.yfinance_fetch import fetch_history


@dataclass
class BuildTelemetry:
    """D118 — cycle observation surfaced to the budget controller.

    The orchestrator passes this back into the budget controller so the
    next cycle's target_budget can adapt to actual measured throughput
    and the deepest priority rank that entered the watching tier.
    """

    measured_duration_sec: float = 0.0
    candidates_considered: int = 0
    picked: int = 0
    scored: int = 0
    timed_out: list[str] = field(default_factory=list)
    max_watching_rank: int | None = None
    picks_breakdowns: dict[str, PriorityBreakdown] = field(default_factory=dict)


def _to_yf_symbol(symbol: str, broker: str) -> str | None:
    """Translate a broker-native symbol to its yfinance-canonical form.

    D116 delegates to :mod:`instruments.canonical`, which centralises the
    multi-broker translation logic. The legacy behaviour of returning
    ``None`` for unrecognised dotted/underscored tickers is preserved.
    """
    from instruments.canonical import to_canonical

    s = (symbol or "").strip().upper()
    if not s:
        return None
    if "_" in s:
        return None
    parsed = to_canonical(s, broker=broker)
    return parsed.symbol if parsed is not None else None


def liquidity_score_for_symbol(symbol: str) -> float:
    """
    Proxy for liquidity + tradability using recent dollar volume and intraday range (spread proxy).
    Higher is better. Returns 0.0 when yfinance has no usable bars.
    """
    sym = symbol.strip()
    if not sym:
        return 0.0
    try:
        df = fetch_history(sym, period="5d", interval="1d")
        if df is None or df.empty:
            return 0.0
        row = df.iloc[-1]
        c = float(row["Close"])
        v = float(row["Volume"])
        if c <= 0 or v < 0 or math.isnan(c) or math.isnan(v):
            return 0.0
        dollar = c * v
        hi, lo = float(row["High"]), float(row["Low"])
        spread_proxy = (hi - lo) / c if c > 0 else 1.0
        if math.isnan(spread_proxy):
            spread_proxy = 1.0
        return max(0.0, math.log1p(dollar) * (1.0 / (1.0 + 3.0 * spread_proxy)))
    except Exception:  # noqa: BLE001
        return 0.0


class UniverseBuilder:
    """
    Build a dynamic ingestion universe from connected brokers.
    Optional tiered ranking (liquidity / spread proxy via yfinance) for core vs scan vs light.
    """

    def __init__(self, *, max_symbols: int = 300, ranking: dict[str, Any] | None = None) -> None:
        self.max_symbols = max(25, int(max_symbols))
        self.ranking_cfg = ranking or {}
        self._cache: list[str] = []

    def ranking_enabled(self) -> bool:
        return bool(self.ranking_cfg.get("enabled", False))

    def update_caps(
        self,
        *,
        max_symbols: int | None = None,
        core_max: int | None = None,
        scan_max: int | None = None,
        max_candidates_to_score: int | None = None,
    ) -> None:
        """D117 — retune caps without reconstructing the builder.

        Only fields explicitly supplied are changed; the others retain
        their previous value so the orchestrator can update one axis at
        a time. The ranking config dict is updated in-place because
        :meth:`build_tiered_universe` reads from it at call time.
        """
        if max_symbols is not None:
            self.max_symbols = max(25, int(max_symbols))
        if not isinstance(self.ranking_cfg, dict):
            # Preserve the historical contract: ranking may be a plain
            # dict-like; we want a mutable dict to update.
            self.ranking_cfg = dict(self.ranking_cfg)
        if core_max is not None:
            self.ranking_cfg["core_max"] = max(1, int(core_max))
        if scan_max is not None:
            self.ranking_cfg["scan_max"] = max(0, int(scan_max))
        if max_candidates_to_score is not None:
            self.ranking_cfg["max_candidates_to_score"] = max(50, int(max_candidates_to_score))

    async def build_symbols(self, broker_manager: Any | None) -> list[str]:
        """Flat symbol list (legacy): half equities / half crypto cap, or tier flatten when ranking on."""
        if self.ranking_enabled():
            tiers = await self.build_tiered_universe(broker_manager)
            merged = list(dict.fromkeys(list(tiers.core) + list(tiers.scan)))
            cap = self.max_symbols
            out = merged[:cap]
            if not out:
                out = self._cache
            if out:
                self._cache = out
            logger.info("universe_builder | dynamic tiered flat symbols={}", len(out))
            return out

        symbols: list[str] = []
        seen: set[str] = set()

        if broker_manager is not None:
            tasks = []
            for name, adapter in broker_manager.adapters.items():
                tasks.append(self._fetch_for_broker(name, adapter))
            if tasks:
                rows = await asyncio.gather(*tasks, return_exceptions=True)
                for row in rows:
                    if isinstance(row, Exception):
                        continue
                    for sym in row:
                        if sym not in seen:
                            seen.add(sym)
                            symbols.append(sym)

        if not symbols:
            universe = UniverseManager()
            for inst in universe.get_all():
                sym = _to_yf_symbol(inst.broker_symbol or inst.symbol, inst.broker)
                if sym and sym not in seen:
                    seen.add(sym)
                    symbols.append(sym)

        equities = [s for s in symbols if "-" not in s]
        crypto = [s for s in symbols if "-" in s]
        out = equities[: self.max_symbols // 2] + crypto[: self.max_symbols // 2]
        if not out:
            out = self._cache
        if out:
            self._cache = out
        logger.info("universe_builder | dynamic symbols={}", len(out))
        return out

    async def build_tiered_universe(
        self,
        broker_manager: Any | None,
        *,
        priority_scores: Mapping[str, PriorityBreakdown] | None = None,
        target_budget: int | None = None,
        anchors: list[str] | None = None,
        telemetry: BuildTelemetry | None = None,
    ) -> UniverseTiers:
        """Collect broker symbols, score with yfinance, assign core / scan / light tiers.

        D118: when ``priority_scores`` is supplied, candidate selection
        switches from the legacy fixed-seed stratified sample to a
        deterministic top-N pick by ``priority_score``. ``target_budget``
        overrides the static ``max_candidates_to_score`` cap. The
        legacy stratified sampler remains as a safety fallback when
        no priority scores are available (e.g. cycle 1 before any
        registry data is ingested or when the kill switch is engaged).
        """
        cycle_start = time.perf_counter()
        by_broker = await self._collect_candidates_by_broker(broker_manager)
        candidates = list(dict.fromkeys([s for rows in by_broker.values() for s in rows]))
        candidates_total = len(candidates)

        # D118 — pick the actual candidate set for scoring.
        priority_used = False
        active_pins: list[str] = []
        if priority_scores:
            budget = (
                int(target_budget)
                if target_budget is not None and target_budget > 0
                else max(50, int(self.ranking_cfg.get("max_candidates_to_score", 450)))
            )
            # Anchors are intersected with the discovered candidate set so
            # an anchor that no broker exposes does not become a phantom row.
            candidate_set = set(candidates)
            pin = [a for a in (anchors or []) if a in candidate_set]
            active_pins = list(dict.fromkeys(pin))
            selected = top_n_by_priority(
                priority_scores,
                budget=budget,
                pinned=pin,
            )
            # Restrict to symbols the brokers actually expose; some prefilter
            # rows may carry symbols from a previous-cycle registry snapshot
            # that no broker returned this cycle.
            candidates = [s for s in selected if s in candidate_set]
            if not candidates:
                # Defensive: priority pre-filter eliminated everything (e.g.
                # broker outage). Fall back to the legacy sampler so we at
                # least scan curated anchors instead of stalling.
                candidates = list(dict.fromkeys(
                    [s for rows in by_broker.values() for s in rows]
                ))
                candidates = self._stratified_sample_candidates(by_broker, max_cand=budget)
            else:
                priority_used = True
        else:
            max_cand = max(50, int(self.ranking_cfg.get("max_candidates_to_score", 450)))
            if len(candidates) > max_cand:
                candidates = self._stratified_sample_candidates(by_broker, max_cand=max_cand)

        if telemetry is not None:
            telemetry.candidates_considered = candidates_total
            telemetry.picked = len(candidates)
            if priority_used and priority_scores:
                telemetry.picks_breakdowns = {
                    s: priority_scores[s] for s in candidates if s in priority_scores
                }

        core_max = max(1, int(self.ranking_cfg.get("core_max", 50)))
        scan_max = max(0, int(self.ranking_cfg.get("scan_max", 250)))
        total_cap = max(core_max, min(self.max_symbols, core_max + scan_max))

        concurrency = max(1, min(20, int(self.ranking_cfg.get("yf_concurrency", 10))))
        timeout_s = max(1.0, float(self.ranking_cfg.get("score_timeout_sec", 4.0)))
        sem = asyncio.Semaphore(concurrency)
        timeouts: list[str] = []

        async def score_one(sym: str) -> tuple[str, float, bool]:
            timed_out = False
            async with sem:
                try:
                    val = await asyncio.wait_for(
                        asyncio.to_thread(liquidity_score_for_symbol, sym),
                        timeout=timeout_s,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    val = 0.0
                except Exception:  # noqa: BLE001
                    val = 0.0
            return (sym, float(val), timed_out)

        scored = await asyncio.gather(*[score_one(s) for s in candidates])
        scored_list: list[tuple[str, float]] = []
        for sym, val, did_timeout in scored:
            if did_timeout:
                timeouts.append(sym)
            else:
                scored_list.append((sym, val))
        # Symbols that timed out keep their previous score (if any) outside
        # this map. score_map only contains symbols that completed; the
        # orchestrator's score-ages persistence handles the "do not update"
        # contract for timeouts.
        score_map = {s: v for s, v in scored_list}

        scored_symbol_set = {s for s, _ in scored_list}
        pinned_scored = [s for s in active_pins if s in scored_symbol_set]
        pinned_set = set(pinned_scored)
        ordered_syms = pinned_scored + [
            s
            for s, _ in sorted(scored_list, key=lambda x: x[1], reverse=True)
            if s not in pinned_set
        ]
        top = ordered_syms[:total_cap]
        rest = ordered_syms[total_cap:]
        core = top[:core_max]
        scan = top[core_max:]
        light = rest

        updated_at = datetime.now(timezone.utc).isoformat()
        tiers = UniverseTiers(
            core=tuple(core),
            scan=tuple(scan),
            light=tuple(light),
            scores=score_map,
            updated_at=updated_at,
        )
        tiers_path = self.ranking_cfg.get("tiers_path")
        path = None
        if isinstance(tiers_path, str) and tiers_path.strip():
            from pathlib import Path

            path = Path(tiers_path.strip())
        save_universe_tiers(tiers, path=path)

        # D118 telemetry — wall-clock duration and deepest watching rank.
        if telemetry is not None:
            telemetry.measured_duration_sec = max(
                0.0, time.perf_counter() - cycle_start
            )
            telemetry.scored = len(scored_list)
            telemetry.timed_out = list(dict.fromkeys(timeouts))
            watching_set = set(core) | set(scan)
            # The rank of a watching member within ``candidates`` (the
            # priority-ranked pre-filter output) — this is the value the
            # utility-saturation controller saturates on.
            deepest = -1
            for idx, sym in enumerate(candidates):
                if sym in watching_set:
                    if idx > deepest:
                        deepest = idx
            telemetry.max_watching_rank = (deepest + 1) if deepest >= 0 else None

        logger.info(
            "universe_builder | tiers | core={} scan={} light={} priority_used={} timeouts={}",
            len(tiers.core),
            len(tiers.scan),
            len(tiers.light),
            priority_used,
            len(timeouts),
        )
        return tiers

    async def _collect_candidates(self, broker_manager: Any | None) -> list[str]:
        by_broker = await self._collect_candidates_by_broker(broker_manager)
        return list(dict.fromkeys([s for rows in by_broker.values() for s in rows]))

    async def _collect_candidates_by_broker(self, broker_manager: Any | None) -> dict[str, list[str]]:
        symbols: list[str] = []
        seen: set[str] = set()
        by_broker: dict[str, list[str]] = {}

        if broker_manager is not None:
            tasks = []
            for name, adapter in broker_manager.adapters.items():
                tasks.append((name, self._fetch_for_broker(name, adapter)))
            if tasks:
                rows = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
                for (name, _), row in zip(tasks, rows):
                    if isinstance(row, Exception):
                        continue
                    broker_rows: list[str] = []
                    for sym in row:
                        u = sym.strip().upper()
                        if u not in seen:
                            seen.add(u)
                            symbols.append(u)
                            broker_rows.append(u)
                    by_broker[name] = broker_rows

        if not symbols:
            universe = UniverseManager()
            for inst in universe.get_all():
                sym = _to_yf_symbol(inst.broker_symbol or inst.symbol, inst.broker)
                if sym:
                    u = sym.strip().upper()
                    if u not in seen:
                        seen.add(u)
                        symbols.append(u)
                        by_broker.setdefault(inst.broker, []).append(u)
        return by_broker

    def _stratified_sample_candidates(self, by_broker: dict[str, list[str]], *, max_cand: int) -> list[str]:
        """Deterministic broker-balanced sample with curated anchors pinned first."""
        rng = random.Random(int(self.ranking_cfg.get("sample_seed", 42)))
        anchor_syms: list[str] = []
        for inst in UniverseManager.INITIAL_UNIVERSE:
            sym = _to_yf_symbol(inst.broker_symbol or inst.symbol, inst.broker)
            if sym:
                anchor_syms.append(sym.strip().upper())
        out = list(dict.fromkeys(anchor_syms))[:max_cand]
        remaining_slots = max(0, max_cand - len(out))
        if remaining_slots <= 0:
            return out[:max_cand]

        pools = {
            broker: [s for s in list(dict.fromkeys(rows)) if s not in set(out)]
            for broker, rows in by_broker.items()
            if rows
        }
        total = sum(len(v) for v in pools.values())
        if total <= 0:
            return out[:max_cand]

        selected: list[str] = []
        brokers = sorted(pools)
        min_per_broker = min(25, max(1, remaining_slots // max(1, len(brokers) * 2)))
        for broker in brokers:
            pool = pools[broker]
            take = min(len(pool), min_per_broker, remaining_slots - len(selected))
            if take <= 0:
                break
            selected.extend(rng.sample(pool, take) if len(pool) > take else pool)

        remaining_slots = max(0, max_cand - len(out) - len(selected))
        if remaining_slots > 0:
            weighted: list[str] = []
            already = set(out + selected)
            for broker in brokers:
                pool = [s for s in pools[broker] if s not in already]
                if not pool:
                    continue
                share = max(1, round(remaining_slots * (len(pool) / total)))
                take = min(len(pool), share)
                weighted.extend(rng.sample(pool, take) if len(pool) > take else pool)
                already.update(weighted)
            if len(weighted) < remaining_slots:
                tail = [s for rows in pools.values() for s in rows if s not in already]
                take = min(len(tail), remaining_slots - len(weighted))
                weighted.extend(rng.sample(tail, take) if len(tail) > take else tail)
            selected.extend(weighted[:remaining_slots])

        return list(dict.fromkeys(out + selected))[:max_cand]

    async def _fetch_for_broker(self, name: str, adapter: Any) -> list[str]:
        try:
            supported = await asyncio.wait_for(adapter.get_supported_symbols(), timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.debug("universe_builder | {} symbols fetch failed: {}", name, exc)
            return []
        out: list[str] = []
        for raw in supported or []:
            sym = _to_yf_symbol(str(raw), name)
            if sym:
                out.append(sym)
        return out
