from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal


ConnectorCategory = Literal["broker", "information_feed", "ai_provider", "treasury_account"]


@dataclass(frozen=True)
class ConnectorHealth:
    connected: bool
    healthy: bool
    state: str
    checked_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorCapability:
    key: str
    enabled: bool
    description: str = ""


@dataclass(frozen=True)
class InformationEvent:
    source: str
    title: str
    published_at: datetime
    affected_symbols: tuple[str, ...] = ()
    url: str | None = None
    raw_text: str | None = None
    credibility_score: Decimal | None = None
    materiality_score: Decimal | None = None
    latency_ms: int | None = None
    usage_cost: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TreasuryTransferQuote:
    source_account: str
    destination_account: str
    currency: str
    amount: Decimal
    estimated_fee: Decimal = Decimal("0")
    estimated_arrival_seconds: int | None = None
    requires_manual_approval: bool = True
    route_allowed: bool = False
    reason: str | None = None


class ConnectHubAdapter(ABC):
    """
    Common runtime contract for Connect Hub integrations.

    Existing broker adapters keep using ``brokers.base.BrokerAdapter``. This
    interface is for future onboarding/status wrappers around all connector
    categories, including feeds, AI providers, and treasury accounts.
    """

    id: str
    label: str
    category: ConnectorCategory

    @abstractmethod
    async def health(self) -> ConnectorHealth:
        """Return current connection state without mutating credentials or funds."""

    async def capabilities(self) -> tuple[ConnectorCapability, ...]:
        return ()


class InformationFeedAdapter(ConnectHubAdapter):
    category: ConnectorCategory = "information_feed"

    @abstractmethod
    async def fetch_events(self, *, symbols: tuple[str, ...], limit: int = 100) -> tuple[InformationEvent, ...]:
        """Fetch normalized events/headlines for the AI and signal pipelines."""


class AIProviderConnector(ConnectHubAdapter):
    category: ConnectorCategory = "ai_provider"

    @abstractmethod
    async def score(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return advisory-only model output. Implementations must never place orders."""


class TreasuryAdapter(ConnectHubAdapter):
    category: ConnectorCategory = "treasury_account"

    @abstractmethod
    async def balances(self) -> dict[str, Decimal]:
        """Read available treasury balances by currency."""

    async def quote_transfer(
        self,
        *,
        source_account: str,
        destination_account: str,
        currency: str,
        amount: Decimal,
    ) -> TreasuryTransferQuote:
        return TreasuryTransferQuote(
            source_account=source_account,
            destination_account=destination_account,
            currency=currency,
            amount=amount,
            route_allowed=False,
            requires_manual_approval=True,
            reason="transfer_execution_not_enabled",
        )
