"""
tests/test_wave0_safety_lock.py
================================
Wave 0 safety lock — protects the architectural invariants the rest of the
roadmap depends on:

1. ``brokers/base.py`` ``BrokerAdapter`` ABC public method signatures are
   frozen. Any change must be deliberate (update the golden snapshot below).
2. ``BrokerAdapter.paper_mode`` defaults to ``True``.
3. The ``ai/`` package never imports ``brokers.*`` (AI cannot reach
   broker adapters).
4. Every ``execution_engine.execute(`` call site in
   ``system/trading_loop/loop.py`` is preceded by a
   ``risk_engine.evaluate_and_persist(`` call within a small window
   (D015 path always routes through the risk engine).
5. ``RiskEngine`` vetoes a signal once the kill switch is engaged.

These tests are intentionally tight. Failures here usually mean a refactor
crossed a line the roadmap forbids.
"""

from __future__ import annotations

import ast
import inspect
import re
from decimal import Decimal
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 1. BrokerAdapter public interface snapshot ──────────────────────────────

# Golden snapshot of the BrokerAdapter abstract API. Each entry is
# (method_name, tuple_of_parameter_names_excluding_self). Update this only
# when the architectural decision to change the broker interface has been
# explicitly made and recorded in docs/DECISIONS.md.
BROKER_ADAPTER_ABC_SNAPSHOT: dict[str, tuple[str, ...]] = {
    "connect": (),
    "disconnect": (),
    "is_connected": (),
    "get_balance": (),
    "get_positions": (),
    "place_order": ("order",),
    "cancel_order": ("broker_order_id",),
    "get_order": ("broker_order_id",),
    "get_open_orders": (),
    "get_candles": ("symbol", "timeframe", "limit"),
    "get_order_book": ("symbol", "depth"),
    "get_last_price": ("symbol",),
    "stream_prices": ("symbols",),
    "get_supported_symbols": (),
    "get_asset_class": ("symbol",),
}


def test_broker_adapter_abstract_methods_match_snapshot() -> None:
    from brokers.base import BrokerAdapter

    abstract_methods = set(BrokerAdapter.__abstractmethods__)
    snapshot_methods = set(BROKER_ADAPTER_ABC_SNAPSHOT.keys())
    assert abstract_methods == snapshot_methods, (
        "BrokerAdapter abstract method set drifted from Wave 0 snapshot. "
        f"Added: {abstract_methods - snapshot_methods}. "
        f"Removed: {snapshot_methods - abstract_methods}. "
        "Update BROKER_ADAPTER_ABC_SNAPSHOT only after a deliberate "
        "architectural decision recorded in docs/DECISIONS.md."
    )

    for name, expected_params in BROKER_ADAPTER_ABC_SNAPSHOT.items():
        method = getattr(BrokerAdapter, name)
        sig = inspect.signature(method)
        actual_params = tuple(
            p for p in sig.parameters.keys() if p != "self"
        )
        # `expected_params` may be a string for single-arg methods due to a
        # tuple-literal slip; normalise.
        if isinstance(expected_params, str):
            expected_params = (expected_params,)
        assert actual_params == expected_params, (
            f"BrokerAdapter.{name} parameters drifted: "
            f"expected {expected_params}, got {actual_params}."
        )


def test_broker_adapter_paper_mode_default_is_true() -> None:
    from brokers.base import BrokerAdapter

    assert BrokerAdapter.paper_mode is True, (
        "BrokerAdapter.paper_mode default must remain True. "
        "Paper-by-default is a Wave 0 invariant."
    )


# ── 2. AI must not import brokers.* ─────────────────────────────────────────

AI_DIR = REPO_ROOT / "ai"


def _iter_py_files(root: Path):
    for p in root.rglob("*.py"):
        # Skip generated / cache directories.
        if "__pycache__" in p.parts:
            continue
        yield p


def test_ai_package_does_not_import_brokers() -> None:
    assert AI_DIR.exists(), "ai/ package missing — repo layout changed?"

    offenders: list[tuple[str, int, str]] = []
    for path in _iter_py_files(AI_DIR):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - hard fail
            pytest.fail(f"Could not parse {path}: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "brokers" or alias.name.startswith("brokers."):
                        offenders.append((str(path), node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "brokers" or module.startswith("brokers."):
                    offenders.append((str(path), node.lineno, module))

    assert not offenders, (
        "AI layer must not import broker adapters. Offenders:\n"
        + "\n".join(f"  {p}:{ln} -> {mod}" for p, ln, mod in offenders)
    )


# ── 3. D015 path: every execute() in trading loop is preceded by risk eval ──

LOOP_PATH = REPO_ROOT / "system" / "trading_loop" / "loop.py"
RISK_BEFORE_EXEC_WINDOW_LINES = 80


def test_trading_loop_routes_every_execute_through_risk_engine() -> None:
    assert LOOP_PATH.exists(), f"missing {LOOP_PATH}"
    text = LOOP_PATH.read_text(encoding="utf-8").splitlines()

    exec_pat = re.compile(r"\bexecution_engine\.execute\s*\(")
    risk_pat = re.compile(r"\brisk_engine\.evaluate_and_persist\s*\(")

    exec_lines = [i for i, l in enumerate(text) if exec_pat.search(l)]
    risk_lines = [i for i, l in enumerate(text) if risk_pat.search(l)]

    assert exec_lines, "no execution_engine.execute( call site found — refactor?"
    assert risk_lines, "no risk_engine.evaluate_and_persist( call site found — refactor?"

    unguarded: list[int] = []
    for ex_ln in exec_lines:
        # Find the most recent risk evaluate above this execute call.
        recent_risk = [r for r in risk_lines if r < ex_ln]
        if not recent_risk:
            unguarded.append(ex_ln + 1)
            continue
        if (ex_ln - recent_risk[-1]) > RISK_BEFORE_EXEC_WINDOW_LINES:
            unguarded.append(ex_ln + 1)

    assert not unguarded, (
        "execution_engine.execute( call sites without a nearby preceding "
        "risk_engine.evaluate_and_persist( in system/trading_loop/loop.py: "
        f"lines {unguarded}. The risk engine is the final veto and must be "
        "called immediately before every execute()."
    )


# ── 4. RiskEngine vetoes when kill switch is engaged ────────────────────────

@pytest.mark.asyncio
async def test_risk_engine_vetoes_when_kill_switch_engaged() -> None:
    from risk.engine import RiskEngine, RiskVerdict, Signal

    engine = RiskEngine(
        config={
            "fundamentals_path": "config/fundamentals.yaml",
            "allocator_d015_enabled": True,
            "allocator_d015_primary": True,
        }
    )
    engine.kill()

    sig = Signal(
        signal_id="wave0-safety-lock-1",
        symbol="AAPL",
        side="buy",
        strategy="momentum",
        confidence=0.9,
        suggested_quantity=Decimal("1"),
        suggested_price=Decimal("100"),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-04-27T00:00:00+00:00",
        metadata={},
    )
    decision = engine.evaluate(sig, portfolio_state={})

    assert decision.verdict is RiskVerdict.REJECTED, (
        "Kill switch must produce REJECTED verdict. "
        f"Got {decision.verdict} | reason={decision.reason}"
    )
    assert any("kill" in c.lower() for c in decision.checks_failed), (
        f"Expected kill-switch check to fail; got {decision.checks_failed}"
    )
