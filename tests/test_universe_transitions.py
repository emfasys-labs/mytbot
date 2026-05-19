"""D118 — Tests for the universe tier-transition ring buffer."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from data.universe_transitions import (
    DEFAULT_RING_CAPACITY,
    TierTransition,
    TransitionBuffer,
    build_previous_tier_map,
    diff_tiers,
    load_transitions,
    record_transitions,
    save_transitions,
)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. build_previous_tier_map normalises + de-dupes
# ---------------------------------------------------------------------------


def test_build_previous_tier_map():
    out = build_previous_tier_map(
        core=["AAPL", "msft"],
        scan=["GOOG", "AAPL"],  # AAPL already in core, must not flip
        light=["TLT"],
    )
    assert out == {"AAPL": "core", "MSFT": "core", "GOOG": "scan", "TLT": "light"}


# ---------------------------------------------------------------------------
# 2. diff_tiers — promotion, demotion, fall-off, new entry
# ---------------------------------------------------------------------------


def test_diff_promotes_from_scan_to_core():
    prev = {"AAPL": "scan"}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    assert len(rows) == 1
    assert rows[0].from_tier == "scan"
    assert rows[0].to_tier == "core"
    assert rows[0].reason == "promoted_within_watching"


def test_diff_demotes_from_core_to_light():
    prev = {"AAPL": "core"}
    rows = diff_tiers(
        previous=prev,
        new_core=[],
        new_scan=[],
        new_light=["AAPL"],
        now=_utc(2026, 5, 19),
    )
    assert rows[0].reason == "demoted_to_light"


def test_diff_falls_off_universe():
    prev = {"AAPL": "core"}
    rows = diff_tiers(
        previous=prev,
        new_core=[],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    assert rows[0].from_tier == "core"
    assert rows[0].to_tier == "absent"
    assert rows[0].reason == "fell_off_universe"


def test_diff_brand_new_entry():
    prev: dict[str, str] = {}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    assert rows[0].from_tier == "absent"
    assert rows[0].to_tier == "core"
    assert rows[0].reason == "new_from_priority_rule"


def test_diff_unchanged_symbol_emits_no_row():
    prev = {"AAPL": "core"}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    assert rows == []


def test_diff_attaches_score_delta_when_available():
    prev = {"AAPL": "scan"}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        scores_previous={"AAPL": 10.0},
        scores_new={"AAPL": 12.5},
        now=_utc(2026, 5, 19),
    )
    assert rows[0].score_delta == pytest.approx(2.5)


def test_diff_omits_delta_when_one_side_missing():
    prev = {"AAPL": "scan"}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        scores_previous={},
        scores_new={"AAPL": 12.5},
        now=_utc(2026, 5, 19),
    )
    assert rows[0].score_delta is None


def test_diff_normalises_symbol_case_consistently():
    prev = {"aapl": "core"}
    rows = diff_tiers(
        previous=prev,
        new_core=["AAPL"],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    # Same symbol (case-insensitive) in the same tier -> no row emitted,
    # and crucially no duplicate "absent -> core" appended for AAPL.
    assert rows == []


def test_diff_sorts_rows_by_symbol():
    prev: dict[str, str] = {}
    rows = diff_tiers(
        previous=prev,
        new_core=["MSFT", "AAPL", "TLT"],
        new_scan=[],
        new_light=[],
        now=_utc(2026, 5, 19),
    )
    assert [r.symbol for r in rows] == ["AAPL", "MSFT", "TLT"]


# ---------------------------------------------------------------------------
# 3. Ring buffer cap
# ---------------------------------------------------------------------------


def test_buffer_caps_at_capacity():
    buf = TransitionBuffer(capacity=3)
    for i in range(5):
        buf.append(
            TierTransition(
                ts="t",
                symbol=f"S{i}",
                from_tier="scan",
                to_tier="core",
                reason="promoted",
            )
        )
    assert len(buf) == 3
    assert [r.symbol for r in buf.rows] == ["S2", "S3", "S4"]


def test_buffer_recent_truncates_to_limit():
    buf = TransitionBuffer()
    for i in range(10):
        buf.append(
            TierTransition(
                ts="t",
                symbol=f"S{i}",
                from_tier="scan",
                to_tier="core",
                reason="promoted",
            )
        )
    out = buf.recent(3)
    assert len(out) == 3
    assert out[-1].symbol == "S9"


# ---------------------------------------------------------------------------
# 4. Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_preserves_rows(tmp_path):
    buf = TransitionBuffer(capacity=2)
    buf.append(
        TierTransition(
            ts="2026-05-19T21:00:00+00:00",
            symbol="AAPL",
            from_tier="scan",
            to_tier="core",
            reason="promoted",
            score_delta=1.5,
        )
    )
    p = tmp_path / "transitions.json"
    save_transitions(buf, path=p)
    loaded = load_transitions(p)
    assert len(loaded) == 1
    assert loaded.rows[0].symbol == "AAPL"
    assert loaded.rows[0].score_delta == pytest.approx(1.5)
    assert loaded.capacity == 2


def test_load_corrupt_file_returns_empty_buffer(tmp_path):
    p = tmp_path / "transitions.json"
    p.write_text("not json", encoding="utf-8")
    buf = load_transitions(p)
    assert len(buf) == 0


def test_load_missing_file_returns_empty_buffer(tmp_path):
    buf = load_transitions(tmp_path / "nope.json")
    assert len(buf) == 0
    assert buf.capacity == DEFAULT_RING_CAPACITY


def test_save_atomic_no_tmp_files(tmp_path):
    buf = TransitionBuffer()
    save_transitions(buf, path=tmp_path / "subdir" / "transitions.json")
    tmp_files = [pp for pp in (tmp_path / "subdir").iterdir() if pp.suffix == ".tmp"]
    assert tmp_files == []


def test_record_transitions_appends_to_existing(tmp_path):
    p = tmp_path / "transitions.json"
    record_transitions(
        [
            TierTransition(
                ts="t1", symbol="A", from_tier="scan", to_tier="core", reason="r1"
            )
        ],
        path=p,
    )
    record_transitions(
        [
            TierTransition(
                ts="t2", symbol="B", from_tier="scan", to_tier="core", reason="r2"
            )
        ],
        path=p,
    )
    buf = load_transitions(p)
    assert [r.symbol for r in buf.rows] == ["A", "B"]


def test_load_ignores_garbage_rows(tmp_path):
    p = tmp_path / "transitions.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "capacity": 10,
                "rows": [
                    {
                        "ts": "x",
                        "symbol": "VALID",
                        "from_tier": "scan",
                        "to_tier": "core",
                        "reason": "r",
                        "score_delta": None,
                    },
                    "not-a-dict",
                    {"symbol": "", "from_tier": "scan", "to_tier": "core"},  # empty sym
                    {"symbol": "BADDELTA", "from_tier": "scan", "to_tier": "core", "reason": "r", "score_delta": "abc"},
                ],
            }
        ),
        encoding="utf-8",
    )
    buf = load_transitions(p)
    syms = [r.symbol for r in buf.rows]
    assert "VALID" in syms
    assert "" not in syms
    assert "BADDELTA" not in syms
