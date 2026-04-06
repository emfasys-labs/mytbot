from __future__ import annotations

from types import SimpleNamespace

import pytest

from control.runner_control import apply_control_commands


class _Bus:
    def __init__(self, rows):
        self._rows = rows
        self.done = []
        self.failed = []
        self.events = []

    async def claim_pending(self, limit=20):  # noqa: ANN001
        _ = limit
        return self._rows

    async def mark_done(self, command_id: int):
        self.done.append(command_id)

    async def mark_failed(self, command_id: int, error: str):
        self.failed.append((command_id, error))

    async def append_dashboard_event(self, event_type: str, payload=None):  # noqa: ANN001
        self.events.append((event_type, payload))

    async def merge_risk_override_state(self, *args, **kwargs):  # noqa: ANN001
        pass


class _Risk:
    def __init__(self):
        self.killed = False
        self.reset = False
        self.is_killed = False
        self._parameters = SimpleNamespace(
            apply_regime_override=lambda name, value, reason, source: (name, value, reason, source),
            persist_regime_overrides_to_yaml=lambda: None,
        )

    def kill(self):
        self.killed = True
        self.is_killed = True

    def reset_kill(self):
        self.reset = True
        self.is_killed = False


class _Exec:
    def __init__(self):
        self.cancelled = False

    async def cancel_all(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_apply_control_commands_kill_and_toggle():
    rows = [
        SimpleNamespace(id=1, command_type="kill", payload={}),
        SimpleNamespace(id=2, command_type="toggle_strategy", payload={"name": "momentum_breakout", "enabled": False}),
    ]
    bus = _Bus(rows)
    risk = _Risk()
    exe = _Exec()
    strat = SimpleNamespace(enabled=True, strategy="momentum_breakout")
    strategies = {"momentum_breakout": strat}

    await apply_control_commands(bus, risk_engine=risk, execution_engine=exe, strategies=strategies)
    assert risk.killed is True
    assert exe.cancelled is True
    assert strat.enabled is False
    assert bus.done == [1, 2]
    assert bus.failed == []
