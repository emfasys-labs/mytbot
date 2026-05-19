from __future__ import annotations

from decimal import Decimal

import pytest

from connectors.base import TreasuryAdapter


class _Treasury(TreasuryAdapter):
    id = "demo_treasury"
    label = "Demo Treasury"

    async def health(self):
        raise NotImplementedError

    async def balances(self):
        return {"GBP": Decimal("1000")}


@pytest.mark.asyncio
async def test_treasury_default_transfer_quote_is_approval_only() -> None:
    quote = await _Treasury().quote_transfer(
        source_account="treasury",
        destination_account="ibkr",
        currency="GBP",
        amount=Decimal("25000"),
    )

    assert quote.route_allowed is False
    assert quote.requires_manual_approval is True
    assert quote.reason == "transfer_execution_not_enabled"
