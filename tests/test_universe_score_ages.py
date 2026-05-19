"""D118 — Tests for the per-symbol score-age persistence layer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from data.universe_score_ages import (
    DEFAULT_MAX_AGES_KEPT,
    ScoreAgeRow,
    ScoreAges,
    load_score_ages,
    save_score_ages,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Empty / corrupt load
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    state = load_score_ages(tmp_path / "nope.json")
    assert isinstance(state, ScoreAges)
    assert len(state) == 0


def test_load_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "ages.json"
    p.write_text("{not valid json", encoding="utf-8")
    state = load_score_ages(p)
    assert isinstance(state, ScoreAges)
    assert len(state) == 0


def test_load_non_mapping_payload_returns_empty(tmp_path):
    p = tmp_path / "ages.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    state = load_score_ages(p)
    assert len(state) == 0


# ---------------------------------------------------------------------------
# 2. Record scores updates last_scored_at + last_score + score_count
# ---------------------------------------------------------------------------


def test_record_scores_updates_three_fields():
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5}, now=_utc(2026, 5, 19, 21))
    row = state.get("AAPL")
    assert row is not None
    assert row.last_score == 18.5
    assert row.score_count == 1
    assert row.last_scored_at is not None
    assert "2026-05-19T21:00:00" in row.last_scored_at


def test_record_scores_increments_count_on_repeat():
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5}, now=_utc(2026, 5, 19, 21))
    state.record_scores({"AAPL": 19.0}, now=_utc(2026, 5, 19, 22))
    row = state.get("AAPL")
    assert row is not None
    assert row.score_count == 2
    assert row.last_score == 19.0


def test_record_scores_preserves_first_seen_at():
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5}, now=_utc(2026, 5, 19, 21))
    first_seen = state.get("AAPL").first_seen_at
    state.record_scores({"AAPL": 19.0}, now=_utc(2026, 5, 20, 21))
    assert state.get("AAPL").first_seen_at == first_seen


def test_record_scores_normalises_symbol_case():
    state = ScoreAges()
    state.record_scores({"aapl": 1.0}, now=_utc(2026, 5, 19))
    assert "AAPL" in state
    assert "aapl" in state  # __contains__ normalises


# ---------------------------------------------------------------------------
# 3. Timeouts do NOT update — freshness must persist
# ---------------------------------------------------------------------------


def test_timeout_symbols_are_not_updated():
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5}, now=_utc(2026, 5, 19, 21))
    original = state.get("AAPL")
    state.record_scores({"AAPL": 99.0}, timeouts=["AAPL"], now=_utc(2026, 5, 19, 22))
    after = state.get("AAPL")
    assert after.last_score == original.last_score
    assert after.last_scored_at == original.last_scored_at
    assert after.score_count == original.score_count


def test_timeouts_with_no_completed_score_leave_row_unchanged():
    state = ScoreAges()
    state.record_scores({}, timeouts=["MSFT"], now=_utc(2026, 5, 19, 21))
    assert state.get("MSFT") is None


# ---------------------------------------------------------------------------
# 4. age_seconds_for + last_score_for
# ---------------------------------------------------------------------------


def test_age_seconds_for_never_scored_returns_none():
    state = ScoreAges()
    state.observe_unseen(["GOOG"], now=_utc(2026, 5, 19))
    assert state.age_seconds_for("GOOG") is None
    assert state.last_score_for("GOOG") is None


def test_age_seconds_for_after_record():
    state = ScoreAges()
    state.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19, 0))
    age = state.age_seconds_for("AAPL", now=_utc(2026, 5, 19, 1))
    assert age == pytest.approx(3600.0)


def test_last_score_for_returns_float():
    state = ScoreAges()
    state.record_scores({"AAPL": 12.5}, now=_utc(2026, 5, 19))
    assert state.last_score_for("AAPL") == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# 5. observe_unseen tracks symbols without scoring them
# ---------------------------------------------------------------------------


def test_observe_unseen_only_inserts_new_symbols():
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5}, now=_utc(2026, 5, 19))
    state.observe_unseen(["AAPL", "MSFT", "GOOG"], now=_utc(2026, 5, 20))
    aapl = state.get("AAPL")
    assert aapl.last_score == 18.5
    assert state.get("MSFT").last_scored_at is None
    assert state.get("MSFT").score_count == 0
    assert state.get("GOOG").first_seen_at is not None


def test_observe_unseen_skips_empty_symbols():
    state = ScoreAges()
    state.observe_unseen(["", "  ", "AAPL"], now=_utc(2026, 5, 19))
    assert "AAPL" in state
    assert len(state) == 1


# ---------------------------------------------------------------------------
# 6. evict_to_cap
# ---------------------------------------------------------------------------


def test_evict_to_cap_preserves_recently_scored_symbols():
    state = ScoreAges()
    state.record_scores({"OLD": 1.0}, now=_utc(2026, 5, 1))
    state.record_scores({"MID": 1.0}, now=_utc(2026, 5, 15))
    state.record_scores({"NEW": 1.0}, now=_utc(2026, 5, 19))
    evicted = state.evict_to_cap(2)
    assert evicted == 1
    assert "NEW" in state
    assert "MID" in state
    assert "OLD" not in state


def test_evict_to_cap_evicts_unscored_before_scored():
    state = ScoreAges()
    state.observe_unseen(["UNSEEN1", "UNSEEN2"], now=_utc(2026, 5, 1))
    state.record_scores({"SCORED": 1.0}, now=_utc(2026, 5, 19))
    evicted = state.evict_to_cap(1)
    assert evicted == 2
    assert "SCORED" in state
    assert "UNSEEN1" not in state
    assert "UNSEEN2" not in state


def test_evict_to_cap_no_op_when_under_cap():
    state = ScoreAges()
    state.record_scores({"A": 1.0}, now=_utc(2026, 5, 19))
    evicted = state.evict_to_cap(100)
    assert evicted == 0
    assert "A" in state


def test_evict_to_cap_zero_or_negative_is_noop():
    state = ScoreAges()
    state.record_scores({"A": 1.0}, now=_utc(2026, 5, 19))
    assert state.evict_to_cap(0) == 0
    assert state.evict_to_cap(-1) == 0


# ---------------------------------------------------------------------------
# 7. summary
# ---------------------------------------------------------------------------


def test_summary_counts_never_scored_and_median_age():
    state = ScoreAges()
    state.observe_unseen(["U1", "U2"], now=_utc(2026, 5, 19))
    state.record_scores({"AAPL": 1.0}, now=_utc(2026, 5, 19, 0))
    state.record_scores({"MSFT": 2.0}, now=_utc(2026, 5, 19, 1))
    summary = state.summary(now=_utc(2026, 5, 19, 2))
    assert summary["total_tracked"] == 4
    assert summary["never_scored"] == 2
    # AAPL age = 7200s, MSFT age = 3600s, median = (3600 + 7200) / 2 = 5400
    assert summary["median_age_sec"] == pytest.approx(5400.0)


def test_summary_empty_state():
    state = ScoreAges()
    summary = state.summary(now=_utc(2026, 5, 19))
    assert summary["total_tracked"] == 0
    assert summary["never_scored"] == 0
    assert summary["median_age_sec"] == 0.0


# ---------------------------------------------------------------------------
# 8. Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_preserves_rows(tmp_path):
    state = ScoreAges()
    state.record_scores({"AAPL": 18.5, "msft": 17.0}, now=_utc(2026, 5, 19, 21))
    state.observe_unseen(["UNSEEN"], now=_utc(2026, 5, 19, 21))
    path = tmp_path / "ages.json"
    written = save_score_ages(state, path=path)
    assert written.is_file()
    loaded = load_score_ages(path)
    assert len(loaded) == 3
    assert loaded.last_score_for("AAPL") == 18.5
    assert loaded.last_score_for("MSFT") == 17.0
    assert loaded.last_score_for("UNSEEN") is None
    assert loaded.get("UNSEEN").score_count == 0


def test_save_is_atomic_in_directory(tmp_path):
    state = ScoreAges()
    state.record_scores({"AAPL": 1.0}, now=_utc(2026, 5, 19))
    path = tmp_path / "subdir" / "ages.json"
    save_score_ages(state, path=path)
    assert path.is_file()
    # No .tmp leaks
    tmp_files = [p for p in (tmp_path / "subdir").iterdir() if p.suffix == ".tmp"]
    assert tmp_files == []


def test_load_ignores_garbage_rows(tmp_path):
    p = tmp_path / "ages.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "rows": {
                    "VALID": {
                        "last_scored_at": "2026-05-19T21:00:00+00:00",
                        "last_score": 12.0,
                        "score_count": 1,
                        "first_seen_at": "2026-05-19T21:00:00+00:00",
                    },
                    "": {"last_scored_at": "x", "last_score": 1.0},  # empty symbol
                    "BAD_SCORE": {"last_scored_at": "x", "last_score": "abc", "score_count": 0},
                    "NULL": None,
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_score_ages(p)
    assert "VALID" in loaded
    assert "" not in loaded
    assert "BAD_SCORE" not in loaded
    assert "NULL" not in loaded


def test_default_max_ages_kept_is_sensible():
    # Sanity: the documented default should comfortably exceed the typical
    # unique-normalized universe count (~16k).
    assert DEFAULT_MAX_AGES_KEPT >= 20_000
