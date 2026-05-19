from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


CONNECTORS_CONFIG_PATH = Path("config/connectors.yaml")
ENV_PATH = Path(".env")


def _clean_env(name: str) -> str:
    value = os.getenv(str(name or "").strip(), "").strip()
    if not value or value.startswith("#"):
        return ""
    return value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _load_yaml(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else CONNECTORS_CONFIG_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    return data


@dataclass(frozen=True)
class ConnectorSecret:
    env: str
    label: str = ""
    required: bool = True

    @property
    def configured(self) -> bool:
        return bool(_clean_env(self.env))

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "label": self.label or self.env,
            "required": self.required,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class ConnectorManifest:
    id: str
    label: str
    category: str
    enabled: bool = True
    adapter: str | None = None
    auth_type: str = "api_key"
    required_secrets: tuple[ConnectorSecret, ...] = ()
    capabilities: dict[str, bool] = field(default_factory=dict)
    roles: tuple[str, ...] = ()
    safety: dict[str, Any] = field(default_factory=dict)
    docs_url: str | None = None
    notes: str | None = None

    @property
    def configured(self) -> bool:
        required = [s for s in self.required_secrets if s.required]
        if not required:
            return bool(self.enabled)
        return all(s.configured for s in required)

    def to_dict(self, *, status: dict[str, Any] | None = None) -> dict[str, Any]:
        row_status = dict(status or {})
        connected = bool(row_status.get("connected", False))
        healthy = bool(row_status.get("healthy", connected))
        state = "off"
        if self.enabled:
            state = "ready" if self.configured else "needs_credentials"
            if row_status.get("state"):
                state = str(row_status["state"])
            elif connected:
                state = "connected"
        missing = [s.env for s in self.required_secrets if s.required and not s.configured]
        next_actions: list[dict[str, str]] = []
        if not self.enabled:
            next_actions.append({"kind": "enable_manifest", "label": "Enable connector in config/connectors.yaml"})
        elif missing:
            next_actions.append({"kind": "set_env", "label": f"Set {', '.join(missing)} in .env"})
        elif not connected and self.category == "brokers":
            next_actions.append({"kind": "start_system", "label": "Start the system to connect this broker"})
        elif not connected and self.category == "information_feeds":
            next_actions.append({"kind": "run_pipeline", "label": "Run the data pipeline to ingest this feed"})
        elif not connected and self.category == "ai_providers":
            next_actions.append({"kind": "check_ai_runtime", "label": "Check model/runtime availability"})
        elif not connected and self.category == "treasury_accounts":
            next_actions.append({"kind": "approval_required", "label": "Treasury transfer execution requires a future approval workflow"})
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "enabled": self.enabled,
            "configured": self.configured,
            "connected": connected,
            "healthy": healthy,
            "state": state,
            "adapter": self.adapter,
            "auth_type": self.auth_type,
            "required_secrets": [s.to_dict() for s in self.required_secrets],
            "capabilities": dict(self.capabilities),
            "roles": list(self.roles),
            "safety": dict(self.safety),
            "docs_url": self.docs_url,
            "notes": self.notes,
            "status": row_status,
            "next_actions": next_actions,
        }


def _parse_secret(row: Any) -> ConnectorSecret | None:
    if isinstance(row, str):
        env = row.strip()
        return ConnectorSecret(env=env) if env else None
    if not isinstance(row, dict):
        return None
    env = str(row.get("env") or "").strip()
    if not env:
        return None
    return ConnectorSecret(
        env=env,
        label=str(row.get("label") or env).strip(),
        required=bool(row.get("required", True)),
    )


def _parse_manifest(category: str, key: str, row: Any) -> ConnectorManifest | None:
    if not isinstance(row, dict):
        return None
    cid = str(row.get("id") or key).strip().lower()
    if not cid:
        return None
    secrets = tuple(s for s in (_parse_secret(x) for x in row.get("required_secrets") or []) if s is not None)
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    return ConnectorManifest(
        id=cid,
        label=str(row.get("label") or cid).strip(),
        category=category,
        enabled=bool(row.get("enabled", True)),
        adapter=str(row.get("adapter") or cid).strip().lower() or None,
        auth_type=str(row.get("auth_type") or "api_key").strip(),
        required_secrets=secrets,
        capabilities={str(k): bool(v) for k, v in caps.items()},
        roles=tuple(_as_list(row.get("roles"))),
        safety=row.get("safety") if isinstance(row.get("safety"), dict) else {},
        docs_url=str(row.get("docs_url")).strip() if row.get("docs_url") else None,
        notes=str(row.get("notes")).strip() if row.get("notes") else None,
    )


def load_connector_manifests(path: str | Path | None = None) -> list[ConnectorManifest]:
    cfg = _load_yaml(path)
    manifests: list[ConnectorManifest] = []
    for category in ("brokers", "information_feeds", "ai_providers", "treasury_accounts"):
        rows = cfg.get(category)
        if not isinstance(rows, dict):
            continue
        for key, row in rows.items():
            parsed = _parse_manifest(category, str(key), row)
            if parsed is not None:
                manifests.append(parsed)
    return manifests


def find_connector_manifest(
    *,
    category: str,
    connector_id: str,
    path: str | Path | None = None,
) -> ConnectorManifest | None:
    cat = str(category or "").strip()
    cid = str(connector_id or "").strip().lower()
    for manifest in load_connector_manifests(path):
        if manifest.category == cat and manifest.id == cid:
            return manifest
    return None


def _quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if re.search(r"\s|#|=|['\"]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_file(
    updates: dict[str, str],
    *,
    env_path: str | Path = ENV_PATH,
) -> list[str]:
    """
    Upsert allowed environment values into ``.env`` without returning secrets.

    The caller is responsible for validating that keys are allowed for the
    selected connector. Values are also copied into ``os.environ`` for immediate
    status rebuilding, although long-lived adapters may still need a restart.
    """

    clean: dict[str, str] = {}
    for key, value in updates.items():
        env = str(key or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env):
            raise ValueError(f"Invalid environment variable name: {env}")
        clean[env] = str(value or "").strip()
    if not clean:
        return []

    p = Path(env_path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) in clean:
            key = m.group(1)
            out.append(f"{key}={_quote_env_value(clean[key])}")
            seen.add(key)
        else:
            out.append(line)
    if out and out[-1].strip():
        out.append("")
    for key in clean:
        if key not in seen:
            out.append(f"{key}={_quote_env_value(clean[key])}")
    text = "\n".join(out).rstrip() + "\n"

    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(p.parent), newline="\n") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(p)
    for key, value in clean.items():
        os.environ[key] = value
    return sorted(clean)


def set_connector_enabled(
    *,
    category: str,
    connector_id: str,
    enabled: bool,
    path: str | Path = CONNECTORS_CONFIG_PATH,
) -> None:
    p = Path(path)
    cfg = _load_yaml(p)
    rows = cfg.get(category)
    if not isinstance(rows, dict) or connector_id not in rows or not isinstance(rows[connector_id], dict):
        raise ValueError(f"Unknown connector: {category}/{connector_id}")
    rows[connector_id]["enabled"] = bool(enabled)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def delete_connector_manifest(
    *,
    category: str,
    connector_id: str,
    path: str | Path = CONNECTORS_CONFIG_PATH,
) -> dict[str, Any]:
    p = Path(path)
    cfg = _load_yaml(p)
    rows = cfg.get(category)
    cid = str(connector_id or "").strip().lower()
    if not isinstance(rows, dict) or cid not in rows:
        raise ValueError(f"Unknown connector: {category}/{cid}")
    row = rows.pop(cid)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return {
        "category": category,
        "id": cid,
        "label": str(row.get("label") or cid) if isinstance(row, dict) else cid,
    }


def set_ai_provider_enabled(
    *,
    provider_id: str,
    enabled: bool,
    path: str | Path = "config/ai.yaml",
) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    cfg = _load_yaml(p)
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return False
    row = providers.get(provider_id)
    if not isinstance(row, dict):
        return False
    row["enabled"] = bool(enabled)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return True


def _normalize_connector_id(raw: str) -> str:
    cid = str(raw or "").strip().lower()
    cid = re.sub(r"[^a-z0-9_]+", "_", cid)
    cid = re.sub(r"_+", "_", cid).strip("_")
    if not cid or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", cid):
        raise ValueError("Connector id must start with a letter and contain only letters, numbers, or underscores")
    return cid


def _secret_rows(env_names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for env in env_names:
        key = str(env or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid environment variable name: {key}")
        rows.append({"env": key, "label": key.replace("_", " ").title()})
    return rows


def scaffold_broker_adapter(
    *,
    connector_id: str,
    label: str,
    path: str | Path = "brokers",
) -> Path:
    """
    Create a broker adapter scaffold from ``brokers/_template``.

    The scaffold is intentionally not registered automatically as live trading
    code. It gives the developer the exact implementation surface while Connect
    Hub can show the connector as declared/awaiting adapter implementation.
    """

    cid = _normalize_connector_id(connector_id)
    base = Path(path)
    target = base / cid
    if target.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    template = base / "_template" / "adapter.py"
    text = template.read_text(encoding="utf-8") if template.exists() else ""
    class_name = "".join(part.capitalize() for part in cid.split("_")) + "Adapter"
    text = text.replace("NewExchangeAdapter", class_name)
    text = text.replace('broker_name = "newexchange"', f'broker_name = "{cid}"')
    text = text.replace("[Exchange Name]", label)
    text = text.replace("https://docs.newexchange.com/api", "TODO: add API docs URL")
    text = text.replace("pip install newexchange-python", "TODO: add Python SDK if one exists")
    (target / "adapter.py").write_text(text, encoding="utf-8")
    (target / "__init__.py").write_text("", encoding="utf-8")
    return target


def add_connector_manifest(
    *,
    category: str,
    connector_id: str,
    label: str,
    auth_type: str = "api_key",
    required_env: list[str] | None = None,
    capabilities: dict[str, bool] | None = None,
    roles: list[str] | None = None,
    docs_url: str | None = None,
    notes: str | None = None,
    scaffold_adapter: bool = False,
    path: str | Path = CONNECTORS_CONFIG_PATH,
) -> dict[str, Any]:
    cid = _normalize_connector_id(connector_id)
    cat = str(category or "").strip()
    if cat not in {"brokers", "information_feeds", "ai_providers", "treasury_accounts"}:
        raise ValueError("Unknown connector category")
    p = Path(path)
    cfg = _load_yaml(p)
    rows = cfg.setdefault(cat, {})
    if not isinstance(rows, dict):
        raise ValueError(f"Invalid manifest category: {cat}")
    if cid in rows:
        raise ValueError(f"Connector already exists: {cid}")
    row: dict[str, Any] = {
        "label": str(label or cid).strip() or cid,
        "adapter": cid,
        "enabled": True,
        "auth_type": str(auth_type or "api_key").strip(),
        "required_secrets": _secret_rows(required_env or []),
        "capabilities": capabilities or {},
    }
    if roles:
        row["roles"] = [str(r).strip() for r in roles if str(r).strip()]
    if docs_url:
        row["docs_url"] = str(docs_url).strip()
    if notes:
        row["notes"] = str(notes).strip()
    rows[cid] = row
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    scaffolded_path: Path | None = None
    if cat == "brokers" and scaffold_adapter:
        scaffolded_path = scaffold_broker_adapter(connector_id=cid, label=row["label"])
    return {
        "category": cat,
        "id": cid,
        "label": row["label"],
        "scaffolded_adapter_path": str(scaffolded_path) if scaffolded_path else None,
    }


def _broker_statuses(orchestrator: Any | None) -> dict[str, dict[str, Any]]:
    if orchestrator is None:
        return {}
    try:
        status = orchestrator.status()
    except Exception:  # noqa: BLE001
        return {}
    brokers = status.get("brokers") if isinstance(status, dict) else {}
    if not isinstance(brokers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, row in brokers.items():
        if not isinstance(row, dict):
            continue
        connected = bool(row.get("connected"))
        balance_ready = bool(row.get("balance_ready"))
        out[str(name).strip().lower()] = {
            "connected": connected,
            "healthy": connected and balance_ready,
            "balance_ready": balance_ready,
            "state": "connected" if connected else ("needs_credentials" if not row.get("configured") else "unavailable"),
            "error": row.get("error"),
        }
    return out


def _feed_statuses(news_data_providers: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in news_data_providers or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id") or "").strip().lower()
        if not pid:
            continue
        state = str(row.get("state") or "off")
        out[pid] = {
            "connected": state in {"live", "stale"},
            "healthy": state == "live",
            "state": state,
            "last_ingest_at": row.get("last_ingest_at"),
            "age_label": row.get("age_label"),
            "error": row.get("error"),
        }
    return out


def _ai_provider_statuses(ai_config_path: str | Path = "config/ai.yaml") -> dict[str, dict[str, Any]]:
    cfg = _load_yaml(ai_config_path)
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for pid, row in providers.items():
        if not isinstance(row, dict):
            continue
        enabled = bool(row.get("enabled", False))
        env_names = _as_list(row.get("required_env"))
        configured = enabled and all(bool(_clean_env(e)) for e in env_names) if env_names else enabled
        out[str(pid).strip().lower()] = {
            "connected": configured,
            "healthy": configured,
            "state": "ready" if configured else ("needs_credentials" if enabled else "off"),
            "provider": row.get("provider"),
            "model_name": row.get("model_name"),
        }
    return out


def _category_summary(connectors: list[dict[str, Any]]) -> dict[str, Any]:
    configured = [c for c in connectors if c.get("configured")]
    connected = [c for c in connectors if c.get("connected")]
    healthy = [c for c in connectors if c.get("healthy")]
    return {
        "total": len(connectors),
        "enabled": sum(1 for c in connectors if c.get("enabled")),
        "configured": len(configured),
        "connected": len(connected),
        "healthy": len(healthy),
        "ids": [str(c.get("id")) for c in connectors],
        "connected_ids": [str(c.get("id")) for c in connected],
    }


def build_connect_hub_snapshot(
    *,
    orchestrator: Any | None = None,
    news_data_providers: list[dict[str, Any]] | None = None,
    config_path: str | Path | None = None,
    ai_config_path: str | Path = "config/ai.yaml",
) -> dict[str, Any]:
    """
    Build the adaptive Connect Hub view.

    This is read-only. It exposes what can be connected and what is usable now;
    it never initiates OAuth, credential writes, broker transfers, or treasury
    movement. Treasury rows are policy metadata only until a future approval
    workflow explicitly enables transfer execution.
    """

    manifests = load_connector_manifests(config_path)
    status_by_category = {
        "brokers": _broker_statuses(orchestrator),
        "information_feeds": _feed_statuses(news_data_providers),
        "ai_providers": _ai_provider_statuses(ai_config_path),
        "treasury_accounts": {},
    }

    categories: dict[str, list[dict[str, Any]]] = {
        "brokers": [],
        "information_feeds": [],
        "ai_providers": [],
        "treasury_accounts": [],
    }
    for manifest in manifests:
        statuses = status_by_category.get(manifest.category, {})
        status = statuses.get(manifest.id) or statuses.get(str(manifest.adapter or "").lower()) or {}
        categories.setdefault(manifest.category, []).append(manifest.to_dict(status=status))

    summaries = {name: _category_summary(rows) for name, rows in categories.items()}
    return {
        "generated_at": _utc_now_iso(),
        "categories": categories,
        "summary": summaries,
        "capability_flags": {
            "can_trade": any(c.get("connected") and c.get("capabilities", {}).get("can_trade") for c in categories["brokers"]),
            "has_information_feed": any(c.get("configured") for c in categories["information_feeds"]),
            "has_ai_provider": any(c.get("configured") for c in categories["ai_providers"]),
            "has_treasury_account": any(c.get("configured") for c in categories["treasury_accounts"]),
            "can_auto_transfer": any(
                c.get("configured") and c.get("capabilities", {}).get("can_initiate_transfer")
                for c in categories["treasury_accounts"]
            ),
        },
    }
