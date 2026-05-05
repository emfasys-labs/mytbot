"""Read-only IBKR Gateway/TWS connectivity diagnostic.

This script does not place orders. It checks the configured TWS API socket and,
when possible, attempts a read-only ib_insync connection with a diagnostic
client id so we can tell apart "port closed", "API prompt/disclaimer blocked",
duplicate client IDs, and slow Gateway startup.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.split("#", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


@dataclass
class TcpResult:
    host: str
    port: int
    ok: bool
    error: str | None = None


def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> TcpResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return TcpResult(host, port, True)
    except Exception as exc:  # noqa: BLE001
        return TcpResult(host, port, False, f"{type(exc).__name__}: {exc}")


def _classify_error(text: str) -> str:
    s = text.lower()
    if "paper trading disclaimer" in s:
        return "gateway_blocked_by_paper_disclaimer"
    if "client id" in s and ("use" in s or "duplicate" in s):
        return "duplicate_client_id"
    if "connectionrefused" in s or "actively refused" in s or "refused" in s:
        return "port_refused_or_api_listener_not_accepting"
    if "timeout" in s or "timed out" in s:
        return "api_handshake_timeout"
    if "readonly" in s or "read-only" in s:
        return "api_readonly_or_permission_issue"
    return "unknown"


async def _ib_probe(host: str, port: int, client_id: int, timeout: float) -> tuple[bool, str, list[tuple[int, int, str]]]:
    try:
        from ib_insync import IB, util
    except Exception as exc:  # noqa: BLE001
        return False, f"ib_insync import failed: {exc}", []

    util.patchAsyncio()
    ib = IB()
    events: list[tuple[int, int, str]] = []

    def on_error(req_id: int, error_code: int, error_string: str, contract: object) -> None:
        events.append((req_id, error_code, error_string))

    ib.errorEvent += on_error
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=timeout, readonly=True)
        accounts = ib.managedAccounts()
        return True, f"connected read-only; accounts={accounts or '(none yet)'}", events
    except Exception as exc:  # noqa: BLE001
        details = "; ".join(f"{code}:{msg}" for _, code, msg in events)
        suffix = f" | events={details}" if details else ""
        return False, f"{type(exc).__name__}: {exc}{suffix}", events
    finally:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    _load_dotenv()
    configured_host = os.getenv("IBKR_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("IBKR_PORT", "7497"))
    client_id = int(os.getenv("IBKR_DIAG_CLIENT_ID", "31"))
    timeout = float(os.getenv("IBKR_DIAG_TIMEOUT", "12"))

    candidates = []
    for h in (configured_host, "127.0.0.1", "localhost", "::1"):
        if h and h not in candidates:
            candidates.append(h)

    print(f"IBKR diagnostic | configured={configured_host}:{port} | diag_client_id={client_id}")
    print("TCP probes:")
    tcp_results = [_tcp_probe(h, port) for h in candidates]
    for r in tcp_results:
        status = "ok" if r.ok else f"fail ({r.error})"
        print(f"  - {r.host}:{r.port}: {status}")

    probe_hosts = [r.host for r in tcp_results if r.ok] or [configured_host]
    print("Read-only API probes:")
    any_ok = False
    for h in probe_hosts:
        ok, msg, events = await _ib_probe(h, port, client_id, timeout)
        any_ok = any_ok or ok
        classification = "ok" if ok else _classify_error(msg)
        print(f"  - {h}:{port}: {classification} | {msg}")
        if events:
            for req_id, code, event_msg in events[-8:]:
                print(f"      event reqId={req_id} code={code} msg={event_msg}")

    if any_ok:
        print("Diagnosis: IBKR API is reachable with a read-only client.")
        return 0
    print(
        "Diagnosis: IBKR API is not usable from this process. If Gateway is visibly logged in, "
        "accept the paper trading disclaimer/API client prompt, verify trusted IP/API settings, "
        "and check that IBKR_HOST matches the interface actually accepting socket clients."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
