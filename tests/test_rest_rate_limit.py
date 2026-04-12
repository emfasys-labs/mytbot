from __future__ import annotations

import asyncio
import time

import pytest

from brokers.rest_rate_limit import AsyncRestGap


@pytest.mark.asyncio
async def test_rest_gap_spaces_calls() -> None:
    gap = AsyncRestGap(0.08)
    t0 = time.monotonic()
    await gap.wait()
    await gap.wait()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.075
