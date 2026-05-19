"""Source adapter interface for the D116 instrument registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from instruments.registry import SourceContribution


class SourceFetchError(RuntimeError):
    """Raised by a source when its fetch failed in a recoverable way.

    Builder isolates each source, so raising this is just a structured signal
    that this run failed; other sources continue.
    """


@dataclass
class SourceContext:
    """Per-run context passed to ``Source.fetch()``."""

    started_at: datetime
    cache_dir: Optional[str] = None
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SourceFetchResult:
    """What a source returned from one ``fetch()`` invocation."""

    source_id: str
    source_version: str
    contributions: Sequence[SourceContribution]
    notes: Optional[str] = None
    partial: bool = False
    fetched_at: Optional[datetime] = None


@runtime_checkable
class Source(Protocol):
    """Adapter contract. Implementations may be sync or async."""

    source_id: str
    source_version: str
    cadence_sec: int

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult: ...
