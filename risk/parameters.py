"""
Parameter manager with layered overrides:
regime override > AI recommendation > proven default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import os
import json

import yaml
from loguru import logger
from sqlalchemy import create_engine, text


@dataclass
class ParameterRecommendation:
    parameter: str
    current_value: float
    recommended_value: float
    confidence: float
    rationale: str
    duration_hours: int
    evidence: list[str] = field(default_factory=list)
    approved: bool = False


@dataclass
class _Override:
    value: Decimal
    layer: str
    reason: str
    source: str
    confidence: Decimal | None = None
    evidence: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None


class ParameterManager:
    def __init__(self, config_path: str = "config/fundamentals.yaml", enable_db_logging: bool = True):
        self.config_path = Path(config_path)
        self._cfg = self.load_fundamentals()
        self._regime_overrides: dict[str, _Override] = {}
        self._ai_overrides: dict[str, _Override] = {}
        self._history: list[dict[str, Any]] = []
        self._db_logging_enabled = enable_db_logging
        self._db_persist_warned = False
        self._engine = self._init_engine() if enable_db_logging else None
        self._merge_overrides_file()
        logger.info("parameters | loaded fundamentals from {}", self.config_path)

    def _merge_overrides_file(self) -> None:
        """Apply persisted regime overrides from config/risk_parameter_overrides.yaml (if present)."""
        path = os.getenv("RISK_PARAMETER_OVERRIDES_PATH", "config/risk_parameter_overrides.yaml")
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            return
        try:
            with p.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except OSError as exc:
            logger.warning("parameters | overrides file unreadable | {} | {}", p, exc)
            return
        for name, val in (data.get("overrides") or {}).items():
            if name not in self._cfg["risk_parameters"]:
                logger.warning("parameters | skip unknown override key | {}", name)
                continue
            try:
                v = self._validate_bounded(name, Decimal(str(val)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("parameters | skip invalid override | {}={} | {}", name, val, exc)
                continue
            self._regime_overrides[name] = _Override(
                value=v,
                layer="regime",
                reason="persisted override file",
                source="disk",
            )
        logger.info("parameters | merged {} regime overrides from {}", len(self._regime_overrides), p)

    def persist_regime_overrides_to_yaml(self) -> None:
        """Write current regime overrides for restart survival (runner-only)."""
        path = os.getenv("RISK_PARAMETER_OVERRIDES_PATH", "config/risk_parameter_overrides.yaml")
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            overrides = {k: float(v.value) for k, v in self._regime_overrides.items()}
            with p.open("w", encoding="utf-8") as f:
                f.write("# Auto-generated — regime overrides from dashboard/API. Safe to edit.\n")
                yaml.safe_dump({"overrides": overrides}, f, sort_keys=True, default_flow_style=False)
            logger.info("parameters | persisted {} overrides to {}", len(overrides), p)
        except OSError as exc:
            logger.warning("parameters | overrides file write failed | {}", exc)

    def load_fundamentals(self) -> dict[str, Any]:
        with self.config_path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if "risk_parameters" not in raw:
            raise ValueError("fundamentals config missing risk_parameters")
        return raw

    def get_value(self, parameter: str) -> Decimal:
        self.check_expiries()
        self._assert_known(parameter)
        if parameter in self._regime_overrides:
            return self._regime_overrides[parameter].value
        if parameter in self._ai_overrides:
            return self._ai_overrides[parameter].value
        return Decimal(str(self._cfg["risk_parameters"][parameter]["proven_default"]))

    def apply_regime_override(self, parameter: str, value: float, reason: str, source: str) -> bool:
        self._assert_known(parameter)
        v = self._validate_bounded(parameter, Decimal(str(value)))
        old = self.get_value(parameter)
        self._regime_overrides[parameter] = _Override(
            value=v,
            layer="regime",
            reason=reason,
            source=source,
        )
        self._append_history(parameter, "regime", old, v, reason, source, None, None, [])
        logger.warning("parameters | regime override | {}: {} -> {} | {}", parameter, old, v, reason)
        return True

    def apply_ai_recommendation(self, recommendation: ParameterRecommendation) -> bool:
        p = recommendation.parameter
        self._assert_known(p)
        pol = self._cfg.get("ai_recommendation_policy", {})
        min_conf = Decimal(str(pol.get("min_confidence_to_apply", 0.8)))
        max_dev = Decimal(str(pol.get("max_deviation_from_default_pct", 0.40)))
        max_dur = int(pol.get("max_duration_hours", 168))

        proposed = Decimal(str(recommendation.recommended_value))
        default = Decimal(str(self._cfg["risk_parameters"][p]["proven_default"]))
        deviation = abs(proposed - default) / default if default != 0 else Decimal("0")

        approved = True
        reason = recommendation.rationale
        if Decimal(str(recommendation.confidence)) < min_conf:
            approved = False
            reason = f"confidence below threshold ({recommendation.confidence} < {min_conf})"
        elif deviation > max_dev:
            approved = False
            reason = f"deviation exceeds policy ({deviation:.4f} > {max_dev})"
        elif p in self._regime_overrides:
            approved = False
            reason = "conflicts with active regime override"
        else:
            try:
                proposed = self._validate_bounded(p, proposed)
            except Exception as exc:  # noqa: BLE001
                approved = False
                reason = str(exc)

        old = self.get_value(p)
        recommendation.approved = approved
        if not approved:
            self._append_history(
                p,
                "ai_rejected",
                old,
                old,
                reason,
                "ai",
                Decimal(str(recommendation.confidence)),
                None,
                recommendation.evidence,
            )
            logger.warning("parameters | ai recommendation rejected | {} | {}", p, reason)
            return False

        duration = min(max_dur, max(1, int(recommendation.duration_hours)))
        expires = datetime.now(timezone.utc) + timedelta(hours=duration)
        self._ai_overrides[p] = _Override(
            value=proposed,
            layer="ai",
            reason=recommendation.rationale,
            source="ai",
            confidence=Decimal(str(recommendation.confidence)),
            evidence=list(recommendation.evidence),
            expires_at=expires,
        )
        self._append_history(
            p,
            "ai",
            old,
            proposed,
            recommendation.rationale,
            "ai",
            Decimal(str(recommendation.confidence)),
            expires,
            recommendation.evidence,
        )
        logger.info("parameters | ai recommendation applied | {}: {} -> {}", p, old, proposed)
        return True

    def check_expiries(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, ov in self._ai_overrides.items() if ov.expires_at is not None and ov.expires_at <= now]
        for p in expired:
            old = self._ai_overrides[p].value
            del self._ai_overrides[p]
            new = self.get_value(p)
            self._append_history(p, "expiry", old, new, "override expired", "system", None, None, [])
            logger.info("parameters | override expired | {}: {} -> {}", p, old, new)

    def get_parameter_history(self, parameter: str, days: int = 30) -> list[dict[str, Any]]:
        self._assert_known(parameter)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        return [x for x in self._history if x["parameter"] == parameter and x["timestamp"] >= cutoff]

    def _assert_known(self, parameter: str) -> None:
        if parameter not in self._cfg["risk_parameters"]:
            raise KeyError(f"Unknown parameter: {parameter}")

    def _validate_bounded(self, parameter: str, value: Decimal) -> Decimal:
        spec = self._cfg["risk_parameters"][parameter]
        lo = Decimal(str(spec["absolute_min"]))
        hi = Decimal(str(spec["absolute_max"]))
        if value < lo or value > hi:
            raise ValueError(f"{parameter}={value} outside bounds [{lo}, {hi}]")
        return value

    def _append_history(
        self,
        parameter: str,
        layer: str,
        old_value: Decimal,
        new_value: Decimal,
        reason: str,
        source: str,
        confidence: Decimal | None,
        expiry_time: datetime | None,
        evidence: list[str],
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc),
            "parameter": parameter,
            "layer": layer,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "source": source,
            "confidence": confidence,
            "expiry_time": expiry_time,
            "evidence": list(evidence),
        }
        self._history.append(row)
        self._persist_history_row(row)

    def _sync_database_url(self) -> str:
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db = os.getenv("POSTGRES_DB", "mytbot")
        user = quote_plus(os.getenv("POSTGRES_USER", "mytbot"))
        password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
        return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"

    def _init_engine(self):
        try:
            return create_engine(self._sync_database_url(), future=True, pool_pre_ping=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("parameters | db logging disabled | failed to init engine: {}", exc)
            self._db_logging_enabled = False
            return None

    def _persist_history_row(self, row: dict[str, Any]) -> None:
        if not self._db_logging_enabled or self._engine is None:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO parameter_log
                        (timestamp, parameter, layer, old_value, new_value, reason, source, confidence, expiry_time, evidence)
                        VALUES
                        (:timestamp, :parameter, :layer, :old_value, :new_value, :reason, :source, :confidence, :expiry_time, :evidence::jsonb)
                        """
                    ),
                    {
                        "timestamp": row["timestamp"],
                        "parameter": row["parameter"],
                        "layer": row["layer"],
                        "old_value": row["old_value"],
                        "new_value": row["new_value"],
                        "reason": row["reason"],
                        "source": row["source"],
                        "confidence": row["confidence"],
                        "expiry_time": row["expiry_time"],
                        "evidence": json.dumps(row["evidence"]),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            if not self._db_persist_warned:
                logger.warning("parameters | db persist skipped: {}", exc)
                self._db_persist_warned = True
