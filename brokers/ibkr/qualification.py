from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_QUALIFICATION_CACHE_PATH = Path("data/runtime/ibkr_contract_qualifications.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class IBKRQualificationRecord:
    symbol: str
    asset_class: str | None
    status: str
    broker_symbol: str
    sec_type: str | None = None
    exchange: str | None = None
    currency: str | None = None
    con_id: int | None = None
    local_symbol: str | None = None
    trading_class: str | None = None
    primary_exchange: str | None = None
    qualified_at: str | None = None
    error: str | None = None

    @property
    def key(self) -> str:
        ac = (self.asset_class or "").strip().lower()
        return cache_key(self.symbol, ac)

    def is_qualified(self) -> bool:
        return self.status == "qualified" and bool(self.con_id)

    def to_json_obj(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_obj(cls, raw: dict[str, Any]) -> "IBKRQualificationRecord":
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            asset_class=_optional_str(raw.get("asset_class")),
            status=str(raw.get("status") or "unchecked").strip().lower(),
            broker_symbol=str(raw.get("broker_symbol") or raw.get("symbol") or "").strip().upper(),
            sec_type=_optional_str(raw.get("sec_type")),
            exchange=_optional_str(raw.get("exchange")),
            currency=_optional_str(raw.get("currency")),
            con_id=_optional_int(raw.get("con_id")),
            local_symbol=_optional_str(raw.get("local_symbol")),
            trading_class=_optional_str(raw.get("trading_class")),
            primary_exchange=_optional_str(raw.get("primary_exchange")),
            qualified_at=_optional_str(raw.get("qualified_at")),
            error=_optional_str(raw.get("error")),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cache_key(symbol: str, asset_class: str | None = None) -> str:
    s = str(symbol or "").strip().upper()
    ac = str(asset_class or "").strip().lower()
    return f"{ac}:{s}" if ac else s


class IBKRQualificationCache:
    """Persistent cache of IBKR contract qualification outcomes."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_QUALIFICATION_CACHE_PATH
        self._records: dict[str, IBKRQualificationRecord] = {}
        self.load()

    def load(self) -> None:
        self._records = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        rows = raw.get("records") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            rec = IBKRQualificationRecord.from_json_obj(row)
            if rec.symbol:
                self._records[rec.key] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utc_now_iso(),
            "records": [rec.to_json_obj() for rec in sorted(self._records.values(), key=lambda r: r.key)],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, symbol: str, asset_class: str | None = None) -> IBKRQualificationRecord | None:
        return self._records.get(cache_key(symbol, asset_class))

    def upsert(self, record: IBKRQualificationRecord) -> None:
        self._records[record.key] = record
        self.save()

    def all(self) -> list[IBKRQualificationRecord]:
        return list(self._records.values())

