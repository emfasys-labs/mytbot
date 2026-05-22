"""D128 — ib_insync market-depth crash-fix patch.

`ib_insync.wrapper.Wrapper.updateMktDepthL2` indexed the DOM list on the
`update` operation with no bounds check, so a malformed IBKR Level-2
update raised `IndexError` inside the asyncio socket callback and crashed
`run.py` (four crashes 2026-05-22 13:07-13:24). The patch makes the
handler bounds-safe — it can never raise out of the event loop.
"""
from __future__ import annotations

from ib_insync.objects import DOMLevel
from ib_insync.wrapper import Wrapper

from brokers.ibkr.ibinsync_patches import _PATCH_FLAG, apply_ibinsync_patches


class _FakeTicker:
    def __init__(self):
        self.domBids: list = []
        self.domAsks: list = []
        self.domTicks: list = []


class _FakeWrapper:
    """Minimal stand-in supplying the attributes the handler touches."""

    def __init__(self):
        self.reqId2Ticker = {1: _FakeTicker()}
        self.lastTime = None
        self.pendingTickers: set = set()


def setup_function(_):
    apply_ibinsync_patches()


def _depth(w, *args):
    """Invoke the *currently patched* Wrapper.updateMktDepthL2 on a fake."""
    return Wrapper.updateMktDepthL2(w, *args)


def test_apply_is_idempotent():
    assert apply_ibinsync_patches() is True
    assert apply_ibinsync_patches() is True
    assert getattr(Wrapper, _PATCH_FLAG, False) is True


def test_out_of_range_update_does_not_raise():
    """The exact crash scenario: an `update` (op=1) for a position past
    the end of an empty DOM list. Old code → IndexError → process crash."""
    w = _FakeWrapper()
    t = w.reqId2Ticker[1]
    # operation=1 update at position 5 on an empty bid book
    _depth(w, 1, 5, "MM", 1, 1, 100.0, 10.0)
    assert len(t.domBids) == 6                 # list grown, not crashed
    assert t.domBids[5].price == 100.0


def test_insert_and_update_in_range_preserved():
    w = _FakeWrapper()
    t = w.reqId2Ticker[1]
    _depth(w, 1, 0, "MM", 0, 0, 99.0, 5.0)   # insert ask
    assert t.domAsks[0].price == 99.0
    _depth(w, 1, 0, "MM", 1, 0, 99.5, 6.0)   # update ask
    assert t.domAsks[0].price == 99.5
    assert t.domAsks[0].size == 6.0


def test_delete_in_and_out_of_range_safe():
    w = _FakeWrapper()
    t = w.reqId2Ticker[1]
    _depth(w, 1, 0, "MM", 0, 1, 99.0, 5.0)   # insert bid
    _depth(w, 1, 0, "MM", 2, 1, 0.0, 0.0)    # delete bid
    assert len(t.domBids) == 0
    # delete out of range — must not raise
    _depth(w, 1, 9, "MM", 2, 1, 0.0, 0.0)
    assert len(t.domBids) == 0


def test_unknown_reqid_is_safe():
    w = _FakeWrapper()
    # reqId not in reqId2Ticker — must not raise
    _depth(w, 999, 0, "MM", 1, 1, 100.0, 10.0)


def test_negative_position_insert_is_clamped():
    w = _FakeWrapper()
    t = w.reqId2Ticker[1]
    _depth(w, 1, -3, "MM", 0, 1, 100.0, 10.0)
    assert len(t.domBids) == 1                 # clamped to index 0, no crash
