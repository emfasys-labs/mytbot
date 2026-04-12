"""
FRED series observations (JSON API).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from data.http_retry import httpx_get_with_retry


@dataclass
class FredObservation:
    obs_date: str
    value: str


def fetch_series_observations(
    api_key: str,
    series_id: str,
    *,
    observation_start: date | None = None,
    timeout_sec: float = 60.0,
) -> list[FredObservation]:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params: dict[str, str] = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    if observation_start is not None:
        params["observation_start"] = observation_start.isoformat()

    out: list[FredObservation] = []
    r = httpx_get_with_retry(url, params=params, timeout_sec=timeout_sec)
    r.raise_for_status()
    data = r.json()
    for row in data.get("observations") or []:
        d = row.get("date")
        v = row.get("value")
        if not d or v is None or v == ".":
            continue
        out.append(FredObservation(obs_date=str(d), value=str(v)))
    return out


def fred_fetch_wallclock() -> datetime:
    return datetime.now(timezone.utc)
