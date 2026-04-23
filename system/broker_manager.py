"""
system/broker_manager.py
========================
Auto-discover and connect available brokers.
Never crash if a broker is unavailable — just skip it.
Reports which brokers are live and which failed.

IBKR gets special handling:
  - Quick TCP probe (3s) before attempting the slow ib_insync handshake
  - API health check (send handshake, verify response) before full connect
  - Single-attempt guard (only one IBKR connect at a time)
  - Exponential backoff on repeated failures (60s → 120s → 300s max)
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from brokers.base import BrokerAdapter
from brokers.registry import BROKER_REGISTRY, get_broker


def _balance_rows_mean_ready(balances: list[Any]) -> bool:
    """True when snapshot looks complete (not [] while account summary is still loading)."""
    if not balances:
        return False
    for b in balances:
        cy = str(getattr(b, "currency", "") or "").strip()
        if cy:
            return True
    return False


def _balance_poll_mean_ready(name: str, balances: list[Any]) -> bool:
    """
    Venue-specific readiness from a successful get_balance() poll.

    IBKR can return transient empty rows while account summary warms up, so
    keep the strict non-empty check there. Other venues should be considered
    ready once the authenticated balance call succeeds, even when the wallet
    currently has zero funded assets.
    """
    if name == "ibkr":
        return _balance_rows_mean_ready(balances)
    return True


@dataclass
class BrokerStatus:
    """Per-broker row for status; balance_ready after first non-empty get_balance snapshot."""

    name: str
    configured: bool = False
    connected: bool = False
    balance_ready: bool = False
    error: str | None = None


@dataclass
class BrokerReport:
    brokers: dict[str, BrokerStatus] = field(default_factory=dict)

    @property
    def active_names(self) -> list[str]:
        return [n for n, s in self.brokers.items() if s.connected]

    @property
    def any_connected(self) -> bool:
        return any(s.connected for s in self.brokers.values())

    @property
    def included_names(self) -> list[str]:
        """Brokers whose balances are trustworthy and reflected in NAV."""
        return [n for n, s in self.brokers.items() if s.connected and s.balance_ready]

    @property
    def excluded(self) -> list[BrokerStatus]:
        """Configured brokers that are NOT contributing to NAV right now."""
        return [
            s for s in self.brokers.values()
            if s.configured and not (s.connected and s.balance_ready)
        ]

    def coverage(self) -> dict[str, Any]:
        """
        Machine-readable portfolio coverage summary.

        ``full`` is true when every configured broker is both connected and
        has produced a balance snapshot — i.e. the NAV reported by the
        dashboard reflects 100% of the wallets the operator asked for.
        When ``full`` is false, ``excluded`` lists the missing brokers with
        their last known error, so the UI can render "partial coverage"
        honestly instead of silently truncating NAV.
        """
        configured = [s for s in self.brokers.values() if s.configured]
        included = self.included_names
        excluded = [
            {
                "name": s.name,
                "connected": bool(s.connected),
                "balance_ready": bool(s.balance_ready),
                "reason": (s.error or "not ready").strip(),
            }
            for s in self.excluded
        ]
        return {
            "full": bool(configured) and not excluded,
            "configured": [s.name for s in configured],
            "included": included,
            "excluded": excluded,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            name: {
                "configured": s.configured,
                "connected": s.connected,
                "balance_ready": s.balance_ready,
                "error": s.error,
            }
            for name, s in self.brokers.items()
        }


def _broker_configs_from_env() -> dict[str, dict[str, Any]]:
    return {
        "ibkr": {
            "host": os.getenv("IBKR_HOST", "127.0.0.1"),
            "port": int(os.getenv("IBKR_PORT", "7497")),
            "client_id": int(os.getenv("IBKR_CLIENT_ID", "1")),
            "account_id": os.getenv("IBKR_ACCOUNT_ID", "").strip(),
        },
        "kraken": {
            "api_key": os.getenv("KRAKEN_API_KEY", "").strip(),
            "api_secret": os.getenv("KRAKEN_API_SECRET", "").strip(),
        },
        "binance": {
            "api_key": os.getenv("BINANCE_API_KEY", "").strip(),
            "api_secret": os.getenv("BINANCE_API_SECRET", "").strip(),
            "testnet": os.getenv("BINANCE_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
        },
        "bybit": {
            "api_key": os.getenv("BYBIT_API_KEY", "").strip(),
            "api_secret": os.getenv("BYBIT_API_SECRET", "").strip(),
            "testnet": os.getenv("BYBIT_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
            "category": (os.getenv("BYBIT_CATEGORY", "linear") or "linear").strip().lower(),
        },
        "alpaca": {
            "api_key": os.getenv("ALPACA_API_KEY", "").strip(),
            "api_secret": os.getenv("ALPACA_API_SECRET", "").strip(),
            "base_url": os.getenv("ALPACA_BASE_URL", "").strip() or None,
        },
    }


def _is_configured(name: str, cfg: dict[str, Any]) -> bool:
    if name == "ibkr":
        return True
    if name in {"kraken", "binance", "bybit", "alpaca"}:
        return bool(cfg.get("api_key") and cfg.get("api_secret"))
    return True


# Shown in dashboard even when optional deps are missing (see BROKER_REGISTRY try/import).
_MISSING_ADAPTER_HINT: dict[str, str] = {
    "bybit": "Install pybit (pip install pybit) to enable the Bybit adapter",
}


async def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    """Quick TCP connect to check if a service is listening."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def _ibkr_api_alive(host: str, port: int, timeout: float = 5.0) -> bool:
    """Send IB API handshake and check if Gateway actually responds.

    Returns True only if the Gateway sends back protocol bytes.
    This catches the case where the Gateway is listening on the port
    but its API handler is dead (zombie state).
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        msg = b"API\x00" + struct.pack("!I", 9) + b"v100..176"
        sock.sendall(msg)
        sock.settimeout(timeout)
        data = sock.recv(4096)
        return len(data) > 0
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class BrokerManager:
    """Attempts to connect every configured broker, skipping those that fail."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.configs = _broker_configs_from_env()
        self.adapters: dict[str, BrokerAdapter] = {}
        self.report = BrokerReport()
        self._late_connect_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._ibkr_connecting = asyncio.Lock()
        self._ibkr_fail_count: int = 0
        self._ibkr_last_attempt: float = 0
        self._broker_fail_count: dict[str, int] = {}
        self._broker_last_attempt: dict[str, float] = {}
        # Consecutive rate-limit fails per broker. Resets on success or on any
        # non-rate-limit failure. Used to escalate backoff and error text when a
        # venue is persistently throttled server-side (e.g. Kraken key punishment).
        self._broker_rate_limit_streak: dict[str, int] = {}

    _BROKER_TIMEOUTS: dict[str, float] = {
        "ibkr": 120,
        "kraken": 90,
        "binance": 45,
        "bybit": 45,
        "alpaca": 30,
    }

    _STARTUP_TIMEOUT = 15.0  # max seconds to wait for any broker during startup

    async def discover_and_connect(self) -> BrokerReport:
        """
        All brokers connect in parallel with a short startup timeout.
        Whatever connects within STARTUP_TIMEOUT is ready immediately.
        Anything slower (Kraken rate-limits, slow IBKR handshake) is
        left for the reconnect loop — never blocks system start.
        """
        self.report = BrokerReport()
        all_tasks: list[asyncio.Task] = []

        # Every venue in .env config gets a row in /system/status (dashboard badges).
        # Optional adapters (e.g. Bybit) may be absent from BROKER_REGISTRY if pybit is not installed.
        for name in sorted(self.configs.keys()):
            cfg = self.configs.get(name, {})
            status = BrokerStatus(name=name)
            self.report.brokers[name] = status

            if name not in BROKER_REGISTRY:
                if _is_configured(name, cfg):
                    status.configured = True
                    status.connected = False
                    status.error = _MISSING_ADAPTER_HINT.get(
                        name,
                        f"No adapter registered for {name}",
                    )
                    logger.warning("broker | {} | {}", name, status.error)
                else:
                    status.configured = False
                    status.error = "Missing API keys in .env"
                    logger.info("broker | {} | skipped (not configured)", name)
                continue

            if not _is_configured(name, cfg):
                status.configured = False
                status.error = "Missing API keys in .env"
                logger.info("broker | {} | skipped (not configured)", name)
                continue

            status.configured = True

            if name == "ibkr":
                all_tasks.append(asyncio.create_task(
                    self._handle_ibkr(cfg, status), name=f"connect-{name}",
                ))
            else:
                timeout = min(self._BROKER_TIMEOUTS.get(name, 30), self._STARTUP_TIMEOUT)
                all_tasks.append(asyncio.create_task(
                    self._try_connect(name, cfg, status, timeout), name=f"connect-{name}",
                ))

        if all_tasks:
            done, pending = await asyncio.wait(all_tasks, timeout=self._STARTUP_TIMEOUT)
            for task in pending:
                broker_name = task.get_name().replace("connect-", "")
                status = self.report.brokers.get(broker_name)
                if status and not status.connected:
                    status.error = "Still connecting (will retry in background)"
                    logger.info("broker | {} | slow connect — will retry in background", broker_name)

        if self._late_connect_task and not self._late_connect_task.done():
            pass  # IBKR background connect continues independently

        active = self.report.active_names
        if active:
            logger.info("broker | connected: {}", ", ".join(active))
        else:
            logger.warning("broker | no brokers connected — running in observation mode")

        return self.report

    async def _handle_ibkr(self, cfg: dict[str, Any], status: BrokerStatus) -> None:
        """
        IBKR connection with three safety layers:
          1. TCP probe — is the port open at all?
          2. API health check — does the Gateway actually respond to protocol?
          3. Background connect with single-attempt guard
        """
        if self._ibkr_connecting.locked():
            logger.debug("broker | ibkr | connection attempt already in progress, skipping")
            return

        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 7497)

        logger.info("broker | ibkr | probing {}:{}...", host, port)
        reachable = await _tcp_probe(host, port, timeout=3.0)

        if not reachable:
            status.error = (
                f"IB Gateway/TWS not reachable on {host}:{port} — "
                f"start TWS/Gateway with API enabled (paper=7497, live=7496)"
            )
            logger.warning("broker | ibkr | {}", status.error)
            return

        api_ok = await asyncio.get_event_loop().run_in_executor(
            None, _ibkr_api_alive, host, port, 5.0
        )
        if not api_ok:
            status.error = (
                "Gateway listening but API not responding — "
                "restart Gateway (zombie state)"
            )
            logger.warning("broker | ibkr | {}", status.error)
            self._ibkr_fail_count += 1
            return

        self._ibkr_fail_count = 0
        logger.info(
            "broker | ibkr | Gateway API alive — connecting in background"
        )
        self._late_connect_task = asyncio.create_task(
            self._background_ibkr_connect(cfg, status),
            name="ibkr-background-connect",
        )

    async def _mark_balance_ready(self, name: str, adapter: BrokerAdapter, status: BrokerStatus) -> None:
        """Set balance_ready after a successful account snapshot (may lag connect for IBKR)."""
        if not status.connected:
            return
        timeout = 60.0 if name == "ibkr" else 35.0
        try:
            balances = await asyncio.wait_for(adapter.get_balance(), timeout=timeout)
            if balances is not None and _balance_poll_mean_ready(name, list(balances)):
                status.balance_ready = True
                logger.info("broker | {} | balance snapshot ready ({} rows)", name, len(balances))
            else:
                status.balance_ready = False
        except Exception as exc:  # noqa: BLE001
            status.balance_ready = False
            logger.debug("broker | {} | balance snapshot not ready: {}", name, exc)

    async def _background_ibkr_connect(
        self, cfg: dict[str, Any], status: BrokerStatus
    ) -> None:
        """Connect IBKR in the background with a single-attempt guard."""
        async with self._ibkr_connecting:
            adapter = None
            try:
                self._ibkr_last_attempt = time.monotonic()
                adapter = get_broker("ibkr", paper_mode=self.paper_mode, **cfg)
                connected = await asyncio.wait_for(
                    adapter.connect(), timeout=self._BROKER_TIMEOUTS["ibkr"]
                )
                if connected:
                    status.connected = True
                    status.error = None
                    status.balance_ready = False
                    self.adapters["ibkr"] = adapter
                    self._ibkr_fail_count = 0
                    logger.info("broker | ibkr | connected (background)")
                    await self._mark_balance_ready("ibkr", adapter, status)
                else:
                    self._ibkr_fail_count += 1
                    adapter_hint = getattr(adapter, "_last_connect_error", None)
                    status.error = str(adapter_hint)[:200] if adapter_hint else "connect() returned False"
                    logger.warning(
                        "broker | ibkr | connect failed (attempt {}): {}",
                        self._ibkr_fail_count,
                        status.error,
                    )
            except asyncio.TimeoutError:
                self._ibkr_fail_count += 1
                status.error = f"Timed out ({self._BROKER_TIMEOUTS['ibkr']}s)"
                logger.warning("broker | ibkr | connect timed out (attempt {})", self._ibkr_fail_count)
                if adapter is not None:
                    try:
                        await adapter.disconnect()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                status.error = "Cancelled (system stopping)"
                logger.info("broker | ibkr | background connect cancelled")
                if adapter is not None:
                    try:
                        await adapter.disconnect()
                    except Exception:
                        pass
            except Exception as exc:
                self._ibkr_fail_count += 1
                status.error = str(exc)[:200]
                logger.warning("broker | ibkr | connect error (attempt {}): {}", self._ibkr_fail_count, exc)

    async def _try_connect(
        self, name: str, cfg: dict[str, Any], status: BrokerStatus, timeout: float
    ) -> None:
        self._broker_last_attempt[name] = time.monotonic()
        try:
            adapter = get_broker(name, paper_mode=self.paper_mode, **cfg)
            connected = await asyncio.wait_for(adapter.connect(), timeout=timeout)
            if connected:
                status.connected = True
                status.error = None
                status.balance_ready = False
                self.adapters[name] = adapter
                self._broker_fail_count[name] = 0
                self._broker_rate_limit_streak[name] = 0
                logger.info("broker | {} | connected", name)
                await self._mark_balance_ready(name, adapter, status)
            else:
                self._broker_fail_count[name] = self._broker_fail_count.get(name, 0) + 1
                adapter_hint = getattr(adapter, "_last_connect_error", None)
                if adapter_hint == "rate_limit":
                    streak = self._broker_rate_limit_streak.get(name, 0) + 1
                    self._broker_rate_limit_streak[name] = streak
                    if name == "kraken" and streak >= 3:
                        status.error = (
                            "Kraken API key persistently blocked by exchange "
                            "(rate-limit or temporary lockout across consecutive attempts) — "
                            "likely a server-side throttle on the key or account. "
                            "Fix: rotate KRAKEN_API_KEY in Kraken account settings, "
                            "or wait for Kraken's penalty window (often 15–60 min) to clear. "
                            "Reconnect will back off to reduce the chance of re-triggering it."
                        )
                    else:
                        status.error = (
                            "Startup connect deferred (transient exchange throttle/retry)"
                            if name == "kraken"
                            else "connect() returned False"
                        )
                else:
                    self._broker_rate_limit_streak[name] = 0
                    if adapter_hint == "invalid_nonce":
                        status.error = (
                            "Invalid nonce (another client is using this API key with "
                            "a higher nonce) — rotate the key or increase the nonce "
                            "window in the exchange's API settings"
                        )
                    elif adapter_hint:
                        status.error = str(adapter_hint)[:200]
                    else:
                        status.error = (
                            "Startup connect deferred (transient exchange throttle/retry)"
                            if name == "kraken"
                            else "connect() returned False"
                        )
                logger.warning("broker | {} | connect failed (attempt {}): {}", name, self._broker_fail_count[name], status.error)
        except asyncio.TimeoutError:
            self._broker_fail_count[name] = self._broker_fail_count.get(name, 0) + 1
            if name in {"binance", "bybit"}:
                status.error = f"Connection timed out ({timeout}s) — will retry in background"
            else:
                status.error = f"Connection timed out ({timeout}s)"
            logger.warning("broker | {} | {}", name, status.error)
        except Exception as exc:
            self._broker_fail_count[name] = self._broker_fail_count.get(name, 0) + 1
            status.error = str(exc)[:200]
            logger.warning("broker | {} | connect error: {}", name, exc)

    _RECONNECT_BASE = 30
    _RECONNECT_MAX = 300
    _HEALTH_POLL_SEC = 10

    def start_reconnect_loop(self) -> None:
        """Start a background task that retries failed broker connections."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="broker-reconnect-loop"
        )

    def _ibkr_backoff(self) -> float:
        """Exponential backoff: 60 -> 120 -> 240 -> 300 (capped)."""
        base = min(self._RECONNECT_BASE * (2 ** self._ibkr_fail_count), self._RECONNECT_MAX)
        return base + random.uniform(0, min(5.0, base * 0.2))

    def _broker_backoff(self, name: str) -> float:
        """Exponential backoff for any broker: 60 -> 120 -> 240 -> 300 (capped)."""
        fails = self._broker_fail_count.get(name, 0)
        if name == "kraken":
            # Kraken uses a counter-based private-API limiter. Short bursts drain
            # in seconds, so start aggressively; but once we've seen ≥3 consecutive
            # "EAPI:Rate limit exceeded" responses, Kraken has applied a persistent
            # server-side throttle to the key itself (verified empirically: the
            # counter does not drain even with 3+ minutes of zero traffic). Back
            # off hard to let the penalty expire and to avoid retriggering it.
            streak = self._broker_rate_limit_streak.get(name, 0)
            if streak >= 3:
                # 10m → 20m → 30m → 30m (cap). Jitter stays modest.
                persistent_base = min(600 * max(1, streak - 2), 1800)
                return persistent_base + random.uniform(0, min(15.0, persistent_base * 0.05))
            base = min(10 * (2 ** fails), 120)
            return base + random.uniform(0, min(3.0, base * 0.2))
        if name == "binance":
            # Binance private endpoints can intermittently stall/throttle; retry sooner than default.
            base = min(15 * (2 ** fails), 120)
            return base + random.uniform(0, min(3.0, base * 0.2))
        if name == "bybit":
            # Bybit private wallet probes may time out transiently; keep reconnect cadence tighter.
            base = min(20 * (2 ** fails), 180)
            return base + random.uniform(0, min(4.0, base * 0.2))
        base = min(self._RECONNECT_BASE * (2 ** fails), self._RECONNECT_MAX)
        return base + random.uniform(0, min(5.0, base * 0.2))

    def _connect_timeout(self, name: str) -> float:
        """
        Per-attempt connect timeout used by reconnect loop.

        Slow private auth checks on some venues may require progressively longer
        windows after repeated failures.
        """
        base = float(self._BROKER_TIMEOUTS.get(name, 30))
        fails = self._broker_fail_count.get(name, 0)
        if name == "bybit":
            return min(90.0, base + (fails * 15.0))
        if name == "binance":
            return min(75.0, base + (fails * 10.0))
        return base

    async def _reconnect_loop(self) -> None:
        await asyncio.sleep(self._HEALTH_POLL_SEC)
        while True:
            await self._prune_disconnected_adapters()
            failed = [
                name for name, s in list(self.report.brokers.items())
                if s.configured
                and not s.connected
                and name not in self.adapters
                and name in BROKER_REGISTRY
            ]
            attempted: list[str] = []
            for name in failed:
                now = time.monotonic()
                if name == "ibkr":
                    backoff = self._ibkr_backoff()
                    last = self._ibkr_last_attempt
                else:
                    backoff = self._broker_backoff(name)
                    last = self._broker_last_attempt.get(name, 0)
                elapsed = now - last
                if last > 0 and elapsed < backoff:
                    continue

                cfg = self.configs.get(name, {})
                status = self.report.brokers[name]
                attempted.append(name)

                if name == "ibkr":
                    await self._handle_ibkr(cfg, status)
                    if self._late_connect_task:
                        try:
                            await self._late_connect_task
                        except Exception:
                            pass
                else:
                    fails = self._broker_fail_count.get(name, 0)
                    logger.info(
                        "broker | {} | reconnect attempt (fails={} backoff={:.0f}s)",
                        name, fails, backoff,
                    )
                    timeout = self._connect_timeout(name)
                    await self._try_connect(name, cfg, status, timeout)

            newly = [n for n in attempted if n in self.adapters]
            if newly:
                logger.info("broker | reconnected: {}", ", ".join(newly))

            await asyncio.sleep(self._HEALTH_POLL_SEC)

    async def _prune_disconnected_adapters(self) -> None:
        """
        Actively verify adapter connectivity and immediately mark/report disconnects.
        Without this, a broker can stay "green" in UI after dropping.
        """
        for name, adapter in list(self.adapters.items()):
            alive = False
            try:
                alive = bool(await asyncio.wait_for(adapter.is_connected(), timeout=3))
            except Exception:
                alive = False

            if alive:
                continue

            self.adapters.pop(name, None)
            status = self.report.brokers.get(name)
            if status is not None:
                status.connected = False
                status.balance_ready = False
                status.error = "Disconnected"
            self._broker_fail_count[name] = max(1, self._broker_fail_count.get(name, 0))
            logger.warning("broker | {} | disconnected (health poll)", name)

        # Connected but still waiting for first usable account snapshot (common for IBKR).
        refresh_tasks = []
        for name, adapter in list(self.adapters.items()):
            st = self.report.brokers.get(name)
            if st is not None and st.connected and not st.balance_ready:
                refresh_tasks.append(self._mark_balance_ready(name, adapter, st))
        if refresh_tasks:
            await asyncio.gather(*refresh_tasks, return_exceptions=True)

    async def disconnect_all(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._late_connect_task is not None and not self._late_connect_task.done():
            self._late_connect_task.cancel()
            try:
                await self._late_connect_task
            except (asyncio.CancelledError, Exception):
                pass
        disconnected_names: list[str] = []
        for name, adapter in list(self.adapters.items()):
            try:
                await adapter.disconnect()
                logger.info("broker | {} | disconnected", name)
            except Exception as exc:
                logger.warning("broker | {} | disconnect error: {}", name, exc)
            disconnected_names.append(name)
        self.adapters.clear()
        for name in disconnected_names:
            st = self.report.brokers.get(name)
            if st is not None:
                st.connected = False
                st.balance_ready = False

    def get_adapter(self, name: str) -> BrokerAdapter | None:
        return self.adapters.get(name)
