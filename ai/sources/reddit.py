from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class RedditMentionEvent:
    symbol: str
    subreddit: str
    post_id: str
    posted_at: datetime
    title: str
    url: str
    score: int
    payload: dict[str, Any]


class RedditSource:
    """M6 scaffold: Reddit source interface (disabled by default)."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))

    async def fetch_recent(self, symbols: list[str], lookback_hours: int = 24) -> list[RedditMentionEvent]:
        # Intentionally no live ingestion in this milestone.
        _ = symbols, lookback_hours
        return []
