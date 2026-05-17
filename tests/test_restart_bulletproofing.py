"""Restart bulletproofing — regression tests.

These lock in the three fixes that stop a process (re)start from bleeding
money via the close→reopen churn, and that let the system self-heal after a
machine wake without a human pressing START:

  FIX 2  TradingLoop boot warmup churn-guard — position-reducing coordinator
         actions (cull / recycle / shed / trim / close / flatten) are
         suppressed for the first cycle(s) after start; opens / arbitrage and
         all risk/stop-loss exits are NEVER gated.
  FIX 3  Durable ReplacementContext mirror — cull / re-entry cooldowns
         survive a DB-empty / DB-outage window at boot (a bus read that
         returns nothing or raises must NOT silently reset the brakes).
  FIX 1  Patient infra wait — the boot wait budget is generous + tunable.

Pure / offline. No DB, no brokers, no network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from system.trading_loop.loop import TradingLoop
import system.d015_escalation as esc
from portfolio.d015_replacement_context import ReplacementContext
from system.dependency_manager import _infra_wait_sec


# ───────────────────────── FIX 2: warmup churn-guard ─────────────────────


def _act(kind: str, *, strategy: str = "x", metadata: dict | None = None):
    return SimpleNamespace(
        kind=kind,
        strategy_name=strategy,
        symbol="BTC-USD",
        metadata=metadata or {},
    )


def _loop() -> TradingLoop:
    return TradingLoop(broker_configs={}, available_brokers=[], paper_mode=True)


def test_action_classification_reducing_vs_open() -> None:
    L = _loop()
    # Reducing surface
    assert L._action_is_position_reducing(_act("trim_symbol"))
    assert L._action_is_position_reducing(_act("close_symbol"))
    assert L._action_is_position_reducing(_act("flatten_symbol"))
    assert L._action_is_position_reducing(
        _act("open_strategy", strategy="capital_recycle")
    )
    assert L._action_is_position_reducing(
        _act("open_strategy", metadata={"sizing_path": "adaptive_shed_to_target"})
    )
    assert L._action_is_position_reducing(
        _act("open_strategy", metadata={"capital_recycle_reason": "dead_edge"})
    )
    # Non-reducing — must always pass
    assert not L._action_is_position_reducing(_act("open_strategy"))
    assert not L._action_is_position_reducing(_act("cross_exchange_arbitrage"))


def test_warmup_active_until_window_and_one_iteration_elapse() -> None:
    L = _loop()
    L._warmup_min_sec = 120.0
    # Not started yet → not in warmup (loop owns the gate only once running).
    assert L._in_boot_warmup() is False
    # Just started, no iteration completed → warmup active.
    L._loop_started_monotonic = __import__("time").monotonic()
    L.iterations = 0
    assert L._in_boot_warmup() is True
    # One iteration done but still inside the wall-clock window → still warm.
    L.iterations = 1
    assert L._in_boot_warmup() is True
    # Window elapsed AND an iteration completed → warmup cleared.
    L._loop_started_monotonic -= 121.0
    assert L._in_boot_warmup() is False


def test_warmup_suppresses_only_reducing_actions() -> None:
    L = _loop()
    L._warmup_min_sec = 120.0
    L._loop_started_monotonic = __import__("time").monotonic()
    L.iterations = 0  # warmup active

    actions = [
        _act("open_strategy"),
        _act("trim_symbol"),
        _act("open_strategy", strategy="capital_recycle"),
        _act("cross_exchange_arbitrage"),
        _act("close_symbol"),
    ]
    kept = L._suppress_reducing_actions_during_warmup(actions)
    kinds = sorted((a.kind, a.strategy_name) for a in kept)
    assert kinds == [
        ("cross_exchange_arbitrage", "x"),
        ("open_strategy", "x"),
    ]
    # Idempotent + logs at most once.
    assert L._warmup_suppress_logged is True


def test_after_warmup_all_actions_pass() -> None:
    L = _loop()
    L._warmup_min_sec = 0.0
    L._loop_started_monotonic = __import__("time").monotonic() - 10.0
    L.iterations = 5  # warmup cleared
    actions = [_act("open_strategy"), _act("trim_symbol"), _act("close_symbol")]
    kept = L._suppress_reducing_actions_during_warmup(actions)
    assert len(kept) == 3  # nothing suppressed once warm


# ───────────────────────── FIX 3: durable mirror ─────────────────────────


class _FakeBus:
    """Minimal CommandBus stand-in; optionally raises on read (DB outage)."""

    def __init__(self, *, raise_on_get: bool = False):
        self._store: dict[str, object] = {}
        self._raise_on_get = raise_on_get

    async def get_state(self, key: str, default=None):
        if self._raise_on_get:
            raise RuntimeError("simulated Postgres outage")
        return self._store.get(key, default)

    async def set_state(self, key: str, value) -> None:
        self._store[key] = value


@pytest.fixture()
def _mirror(tmp_path, monkeypatch):
    p = tmp_path / "replacement_context.json"
    monkeypatch.setattr(esc, "_REPL_CTX_MIRROR", Path(p))
    return p


def test_ctx_value_is_empty_semantics() -> None:
    assert esc._ctx_value_is_empty(None)
    assert esc._ctx_value_is_empty("nope")
    assert esc._ctx_value_is_empty({})
    assert esc._ctx_value_is_empty(
        {"last_event_at_by_symbol": {}, "recent_events": []}
    )
    assert not esc._ctx_value_is_empty(
        {"last_cull_at_by_symbol": {"BTC-USD": "2026-05-17T00:00:00+00:00"}}
    )


def test_save_writes_mirror_and_bus(_mirror) -> None:
    bus = _FakeBus()
    ctx = ReplacementContext()
    from datetime import datetime, timezone

    ctx.last_cull_at_by_symbol["BTC-USD"] = datetime(2026, 5, 17, tzinfo=timezone.utc)

    asyncio.run(esc.save_replacement_context_to_bus(bus, ctx))

    assert _mirror.exists(), "durable mirror file must be written"
    assert esc.D015_REPLACEMENT_STATE_KEY in bus._store


def test_load_restores_from_mirror_when_bus_empty(_mirror) -> None:
    from datetime import datetime, timezone

    # Persist a cooldown via a healthy bus (also writes the mirror).
    bus = _FakeBus()
    ctx = ReplacementContext()
    ctx.last_cull_at_by_symbol["BTC-USD"] = datetime(2026, 5, 17, tzinfo=timezone.utc)
    asyncio.run(esc.save_replacement_context_to_bus(bus, ctx))

    # Simulate a restart where the DB row is gone / not yet readable.
    empty_bus = _FakeBus()
    restored = asyncio.run(esc.load_replacement_context_from_bus(empty_bus))
    assert "BTC-USD" in restored.last_cull_at_by_symbol, (
        "cull cooldown MUST survive a DB-empty restart via the mirror"
    )


def test_load_survives_bus_read_exception(_mirror) -> None:
    from datetime import datetime, timezone

    bus = _FakeBus()
    ctx = ReplacementContext()
    ctx.last_cull_at_by_symbol["ETH-USD"] = datetime(2026, 5, 17, tzinfo=timezone.utc)
    asyncio.run(esc.save_replacement_context_to_bus(bus, ctx))

    # Bus.get_state raises (Postgres down) — must NOT propagate, must fall
    # back to the durable mirror so the brakes stay on.
    outage_bus = _FakeBus(raise_on_get=True)
    restored = asyncio.run(esc.load_replacement_context_from_bus(outage_bus))
    assert "ETH-USD" in restored.last_cull_at_by_symbol


def test_load_empty_everywhere_is_safe(_mirror) -> None:
    # No bus value, no mirror file → empty context, no exception.
    restored = asyncio.run(esc.load_replacement_context_from_bus(_FakeBus()))
    assert isinstance(restored, ReplacementContext)
    assert restored.last_cull_at_by_symbol == {}


# ───────────────────────── FIX 1: patient infra wait ─────────────────────


def test_infra_wait_sec_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("MYTBOT_INFRA_WAIT_SEC", raising=False)
    assert _infra_wait_sec() >= 240.0
    monkeypatch.setenv("MYTBOT_INFRA_WAIT_SEC", "300")
    assert _infra_wait_sec() == 300.0
    # Garbage falls back to the default, never raises.
    monkeypatch.setenv("MYTBOT_INFRA_WAIT_SEC", "not-a-number")
    assert _infra_wait_sec(default=111.0) == 111.0
    # Floor enforced (never an impatient sub-20s budget).
    monkeypatch.setenv("MYTBOT_INFRA_WAIT_SEC", "1")
    assert _infra_wait_sec() == 20.0
