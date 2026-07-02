"""
brokers/ibkr/ibinsync_patches.py
=================================
Defensive monkey-patches for ``ib_insync`` (0.9.86).

D128 — crash fix. ``ib_insync.wrapper.Wrapper.updateMktDepthL2`` indexes
the DOM (Level-2 order book) list without a bounds check on the
``update`` operation:

    elif operation == 1:                       # update
        dom[position] = DOMLevel(price, size, marketMaker)   # <-- IndexError

When IBKR streams a market-depth ``update`` for a ``position`` index
beyond the current list length (it does — partial/unentitled depth
feeds, or out-of-order messages), this raises
``IndexError: list assignment index out of range`` inside the asyncio
socket-read callback. ib_insync's decoder catches most occurrences, but
under a burst it escapes and kills the whole event loop — the four
``run.py`` crashes observed on 2026-05-22 (13:07–13:24).

Note the original code already guards the ``delete`` branch with
``if position < len(dom)`` — only ``update`` was left unguarded.

This module replaces ``updateMktDepthL2`` with a bounds-safe version
that grows the list instead of crashing, and can never raise out of the
event loop. Idempotent — calling ``apply_ibinsync_patches`` twice is a
no-op.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_mytbot_d128_mktdepth_patch"
_EXPECTED_DISCOVERY_FILTER_FLAG = "_mytbot_expected_discovery_filter"


class _ExpectedIbkrDiscoveryFilter(logging.Filter):
    """Suppress only IBKR's normal contract-qualification miss diagnostics."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not (
            "error 200" in message
            and "no security definition has been found for the request" in message
        )


def _install_expected_discovery_filter() -> None:
    wrapper_logger = logging.getLogger("ib_insync.wrapper")
    if getattr(wrapper_logger, _EXPECTED_DISCOVERY_FILTER_FLAG, False):
        return
    wrapper_logger.addFilter(_ExpectedIbkrDiscoveryFilter())
    setattr(wrapper_logger, _EXPECTED_DISCOVERY_FILTER_FLAG, True)


def _safe_update_mkt_depth_l2(
    self,
    reqId: int,
    position: int,
    marketMaker: str,
    operation: int,
    side: int,
    price: float,
    size: float,
    isSmartDepth: bool = False,
) -> None:
    """Bounds-safe replacement for ``Wrapper.updateMktDepthL2``.

    Preserves the original semantics for in-range indices; out-of-range
    ``update`` operations grow the DOM list (padding with empty levels
    if needed) instead of raising ``IndexError``. Never raises.
    """
    from ib_insync.objects import DOMLevel, MktDepthData

    try:
        ticker = self.reqId2Ticker.get(reqId)
        if ticker is None:
            return
        dom = ticker.domBids if side else ticker.domAsks

        if operation == 0:  # insert
            idx = max(0, min(int(position), len(dom)))
            dom.insert(idx, DOMLevel(price, size, marketMaker))
        elif operation == 1:  # update
            if 0 <= position < len(dom):
                dom[position] = DOMLevel(price, size, marketMaker)
            elif position >= len(dom):
                # Out-of-range update — IBKR referenced a level we have
                # not seen. Grow the book rather than crash.
                while len(dom) < position:
                    dom.append(DOMLevel(0.0, 0.0, ""))
                dom.append(DOMLevel(price, size, marketMaker))
        elif operation == 2:  # delete
            if 0 <= position < len(dom):
                level = dom.pop(position)
                price = level.price
                size = 0

        tick = MktDepthData(
            self.lastTime, position, marketMaker, operation, side, price, size
        )
        ticker.domTicks.append(tick)
        self.pendingTickers.add(ticker)
    except Exception as exc:  # noqa: BLE001 — must never escape the event loop
        logger.debug("ib_insync mkt-depth patch swallowed error: %s", exc)


def apply_ibinsync_patches() -> bool:
    """Install the defensive ib_insync patches. Idempotent.

    Returns True when the patch was applied (or already present), False
    when ib_insync could not be patched (logged, non-fatal).
    """
    _install_expected_discovery_filter()
    try:
        from ib_insync.wrapper import Wrapper
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_insync patch | could not import Wrapper: %s", exc)
        return False

    if getattr(Wrapper, _PATCH_FLAG, False):
        return True
    try:
        Wrapper.updateMktDepthL2 = _safe_update_mkt_depth_l2
        setattr(Wrapper, _PATCH_FLAG, True)
        logger.info("ib_insync patch | D128 bounds-safe updateMktDepthL2 installed")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_insync patch | failed to install: %s", exc)
        return False
