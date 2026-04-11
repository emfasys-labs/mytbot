"""
ai/providers/rules_provider.py
==============================
Deterministic, zero-cost provider: ticker extraction, keyword event
classification, source credibility, duplicate detection, materiality.
Always runs first in the provider chain.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

from ai.providers.base import AIProvider
from ai.schemas import ProviderResult

# ── Ticker extraction ───────────────────────────────────────────────────────

_DOLLAR_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")

_KNOWN_TICKERS: dict[str, str] = {
    "APPLE": "AAPL", "MICROSOFT": "MSFT", "GOOGLE": "GOOGL", "ALPHABET": "GOOGL",
    "AMAZON": "AMZN", "NVIDIA": "NVDA", "META": "META", "FACEBOOK": "META",
    "TESLA": "TSLA", "NETFLIX": "NFLX", "AMD": "AMD", "INTEL": "INTC",
    "BOEING": "BA", "JPMORGAN": "JPM", "JP MORGAN": "JPM", "GOLDMAN": "GS",
    "GOLDMAN SACHS": "GS", "MORGAN STANLEY": "MS", "BANK OF AMERICA": "BAC",
    "WELLS FARGO": "WFC", "CITIGROUP": "C", "DISNEY": "DIS", "WALMART": "WMT",
    "COSTCO": "COST", "BERKSHIRE": "BRK.B", "VISA": "V", "MASTERCARD": "MA",
    "PAYPAL": "PYPL", "SALESFORCE": "CRM", "ORACLE": "ORCL", "IBM": "IBM",
    "QUALCOMM": "QCOM", "BROADCOM": "AVGO", "PALANTIR": "PLTR",
    "COINBASE": "COIN", "ROBINHOOD": "HOOD", "SNOWFLAKE": "SNOW",
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "POLKADOT": "DOT",
    "CHAINLINK": "LINK", "LITECOIN": "LTC", "AVALANCHE": "AVAX",
    "S&P 500": "SPY", "S&P500": "SPY", "NASDAQ": "QQQ", "DOW JONES": "DIA",
    "RUSSELL 2000": "IWM", "TREASURY": "TLT", "GOLD": "GLD", "CRUDE": "USO",
    "OIL": "USO", "DOLLAR": "DXY", "VIX": "VIX",
}

_STANDALONE_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "INTC", "BA", "JPM", "GS", "MS", "BAC", "WFC",
    "DIS", "WMT", "COST", "V", "MA", "PYPL", "CRM", "ORCL", "IBM",
    "QCOM", "AVGO", "PLTR", "COIN", "HOOD", "SNOW",
    "SPY", "QQQ", "DIA", "IWM", "TLT", "GLD", "USO", "DXY", "VIX",
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "DOT", "LINK", "LTC",
    "AVAX", "BNB", "MATIC",
}

# ── Source credibility ──────────────────────────────────────────────────────

_SOURCE_CREDIBILITY: dict[str, str] = {
    "reuters": "high", "bloomberg": "high", "associated press": "high",
    "ap news": "high", "financial times": "high",
    "wall street journal": "high", "wsj": "high", "cnbc": "high",
    "bbc": "high", "the guardian": "high", "nytimes": "high",
    "new york times": "high", "washington post": "high",
    "yahoo finance": "medium", "marketwatch": "medium",
    "seeking alpha": "medium", "investopedia": "medium",
    "barrons": "medium", "the economist": "medium", "cnn": "medium",
    "business insider": "medium", "fortune": "medium", "forbes": "medium",
    "benzinga": "medium", "zacks": "medium",
}

# ── Emergency keywords (always escalate) ────────────────────────────────────

_DEFAULT_EMERGENCY_KEYWORDS = [
    "emergency rate cut", "unscheduled fed", "exchange hack",
    "war declared", "market halt", "flash crash", "bank failure",
    "sovereign default", "nuclear", "pandemic declared",
]

# ── Decay-hours lookup ──────────────────────────────────────────────────────

_EVENT_DECAY: dict[str, int] = {
    "earnings": 24, "macro": 48, "regulatory": 36, "geopolitical": 72,
    "sector": 12, "company": 12, "crypto": 6, "mna": 48, "other": 12,
}


class RulesProvider(AIProvider):
    """Fast deterministic rules: tickers, events, materiality, dedup."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._keyword_map: dict[str, list[str]] = cfg.get("keyword_event_map", {
            "earnings": ["earnings", "guidance", "revenue", "eps", "forecast", "profit", "loss", "quarterly", "dividend"],
            "macro": ["cpi", "inflation", "payrolls", "fomc", "fed", "rates", "gdp", "unemployment", "nonfarm", "pce", "ppi"],
            "regulatory": ["sec", "lawsuit", "approval", "ban", "etf", "regulation", "investigation", "fine", "antitrust"],
            "geopolitical": ["war", "sanctions", "tariff", "embargo", "nato", "summit", "ceasefire"],
            "crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "stablecoin", "binance", "coinbase"],
            "mna": ["acquire", "merger", "deal", "takeover", "buyout", "acquisition"],
        })
        self._dedup_enabled = bool(cfg.get("deduplicate_headlines", True))
        self._dedup_window_min = int(cfg.get("deduplicate_window_minutes", 120))
        self._dedup_cache: OrderedDict[str, float] = OrderedDict()
        self._max_cache = 2000
        self._emergency_keywords = cfg.get("emergency_keywords") or _DEFAULT_EMERGENCY_KEYWORDS

    @property
    def name(self) -> str:
        return "rules"

    async def score_headline(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> ProviderResult:
        t0 = time.monotonic()
        text = f"{headline} {body or ''}".strip()
        lower = text.lower()
        headline_lower = headline.lower()

        is_dup = self._check_duplicate(headline)
        tickers = self._extract_tickers(text)
        event_type = self._classify_event(lower)
        credibility = self._source_credibility(source)
        materiality = self._assess_materiality(event_type, credibility, headline_lower)
        decay = _EVENT_DECAY.get(event_type, 12)

        basic_sentiment = self._keyword_sentiment(lower)

        elapsed = int((time.monotonic() - t0) * 1000)
        return ProviderResult(
            provider_name=self.name,
            sentiment=basic_sentiment,
            confidence=0.3 if basic_sentiment != 0.0 else 0.1,
            directional_bias=("bullish" if basic_sentiment > 0.1 else "bearish" if basic_sentiment < -0.1 else "neutral"),
            affected_symbols=tickers,
            event_type=event_type,
            decay_hours=decay,
            rationale=f"Rules: {event_type} event | {len(tickers)} ticker(s) | {credibility} source",
            materiality=materiality,
            novelty_score=0.0,
            is_duplicate=is_dup,
            source_credibility=credibility,
            latency_ms=elapsed,
            cost_estimate_gbp=0.0,
            success=True,
        )

    async def startup_check(self) -> bool:
        logger.info("rules_provider | ready | keywords={} event_types", len(self._keyword_map))
        return True

    def is_emergency(self, headline: str) -> bool:
        lower = headline.lower()
        return any(kw in lower for kw in self._emergency_keywords)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _extract_tickers(self, text: str) -> list[str]:
        found: set[str] = set()
        for m in _DOLLAR_TICKER_RE.finditer(text):
            found.add(m.group(1))
        upper = text.upper()
        for name, ticker in _KNOWN_TICKERS.items():
            if name in upper:
                found.add(ticker)
        words = re.findall(r"\b([A-Z]{2,5})\b", text)
        for w in words:
            if w in _STANDALONE_TICKERS:
                found.add(w)
        return sorted(found)

    def _classify_event(self, lower_text: str) -> str:
        scores: dict[str, int] = {}
        for event_type, keywords in self._keyword_map.items():
            hits = sum(1 for kw in keywords if kw in lower_text)
            if hits > 0:
                scores[event_type] = hits
        if not scores:
            return "other"
        return max(scores, key=scores.get)  # type: ignore[arg-type]

    def _source_credibility(self, source: str) -> str:
        if not source:
            return "low"
        key = source.strip().lower()
        for known, level in _SOURCE_CREDIBILITY.items():
            if known in key:
                return level
        return "medium"

    def _assess_materiality(self, event_type: str, credibility: str, headline_lower: str) -> str:
        if any(kw in headline_lower for kw in self._emergency_keywords):
            return "high"
        if event_type in ("macro", "geopolitical", "mna") and credibility == "high":
            return "high"
        if event_type in ("macro", "geopolitical") or credibility == "high":
            return "medium"
        if event_type == "other" and credibility == "low":
            return "low"
        return "medium"

    def _keyword_sentiment(self, lower_text: str) -> float:
        positive = ["surge", "soar", "rally", "beat", "strong", "upgrade",
                     "bullish", "record high", "outperform", "growth",
                     "breakout", "approval", "deal", "partnership"]
        negative = ["crash", "plunge", "miss", "weak", "downgrade",
                     "bearish", "selloff", "sell-off", "layoff", "bankrupt",
                     "default", "investigation", "fraud", "lawsuit", "ban"]
        pos = sum(1 for w in positive if w in lower_text)
        neg = sum(1 for w in negative if w in lower_text)
        if pos == 0 and neg == 0:
            return 0.0
        total = pos + neg
        return round((pos - neg) / total, 2)

    def _check_duplicate(self, headline: str) -> bool:
        if not self._dedup_enabled:
            return False
        h = hashlib.md5(headline.lower().encode()).hexdigest()
        now = time.time()
        cutoff = now - (self._dedup_window_min * 60)
        if h in self._dedup_cache:
            if self._dedup_cache[h] >= cutoff:
                return True
        self._dedup_cache[h] = now
        while len(self._dedup_cache) > self._max_cache:
            self._dedup_cache.popitem(last=False)
        return False
