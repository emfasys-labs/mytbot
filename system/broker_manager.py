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
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from brokers.base import BrokerAdapter
from brokers.registry import BROKER_REGISTRY, get_broker


@dataclass
class BrokerStatus:
    name: str
    configured: bool = False
    connected: bool = False
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

    def to_dict(self) -> dict[str, Any]:
        return {
            name: {
                "configured": s.configured,
                "connected": s.connected,
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

    _BROKER_TIMEOUTS: dict[str, float] = {
        "ibkr": 120,
        "kraken": 30,
        "binance": 30,
        "bybit": 30,
        "alpaca": 30,
    }

    async def discover_and_connect(self) -> BrokerReport:
        """
        Two-phase discovery:
          Phase 1 — connect all fast brokers (REST APIs) in parallel.
          Phase 2 — IBKR gets a TCP probe first; if Gateway is listening,
                    connect happens in the background so it doesn't block startup.
        """
        self.report = BrokerReport()
        fast_tasks = []
        ibkr_status: BrokerStatus | None = None
        ibkr_cfg: dict[str, Any] = {}

        for name in BROKER_REGISTRY:
            cfg = self.configs.get(name, {})
            status = BrokerStatus(name=name)
            self.report.brokers[name] = status

            if not _is_configured(name, cfg):
                status.configured = False
                status.error = "Missing API keys in .env"
                logger.info("broker | {} | skipped (not configured)", name)
                continue

            status.configured = True

            if name == "ibkr":
                ibkr_status = status
                ibkr_cfg = cfg
                continue

            timeout = self._BROKER_TIMEOUTS.get(name, 30)
            fast_tasks.append(self._try_connect(name, cfg, status, timeout))

        # Phase 1: all non-IBKR brokers in parallel (typically < 5s each)
        if fast_tasks:
            await asyncio.gather(*fast_tasks)

        # Phase 2: IBKR — probe first, then connect (background if slow)
        if ibkr_status is not None and ibkr_status.configured:
            await self._handle_ibkr(ibkr_cfg, ibkr_status)

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
                    self.adapters["ibkr"] = adapter
                    self._ibkr_fail_count = 0
                    logger.info("broker | ibkr | connected (background)")
                else:
                    self._ibkr_fail_count += 1
                    status.error = "connect() returned False"
                    logger.warning("broker | ibkr | connect failed (attempt {})", self._ibkr_fail_count)
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
        try:
            adapter = get_broker(name, paper_mode=self.paper_mode, **cfg)
            connected = await asyncio.wait_for(adapter.connect(), timeout=timeout)
            if connected:
                status.connected = True
                status.error = None
                self.adapters[name] = adapter
                logger.info("broker | {} | connected", name)
            else:
                status.error = "connect() returned False"
                logger.warning("broker | {} | connect failed (returned False)", name)
        except asyncio.TimeoutError:
            status.error = f"Connection timed out ({timeout}s)"
            logger.warning("broker | {} | {}", name, status.error)
        except Exception as exc:
            status.error = str(exc)[:200]
            logger.warning("broker | {} | connect error: {}", name, exc)

    _RECONNECT_BASE = 60
    _RECONNECT_MAX = 300

    def start_reconnect_loop(self) -> None:
        """Start a background task that retries failed broker connections."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="broker-reconnect-loop"
        )

    def _ibkr_backoff(self) -> float:
        """Exponential backoff: 60 → 120 → 240 → 300 (capped)."""
        return min(self._RECONNECT_BASE * (2 ** self._ibkr_fail_count), self._RECONNECT_MAX)

    async def _reconnect_loop(self) -> None:
        await asyncio.sleep(self._RECONNECT_BASE)
        while True:
            failed = [
                name for name, s in self.report.brokers.items()
                if s.configured and not s.connected and name not in self.adapters
            ]
            for name in failed:
                cfg = self.configs.get(name, {})
                status = self.report.brokers[name]
                if name == "ibkr":
                    backoff = self._ibkr_backoff()
                    elapsed = time.monotonic() - self._ibkr_last_attempt if self._ibkr_last_attempt else backoff
                    if elapsed < backoff:
                        logger.debug(
                            "broker | ibkr | backoff {:.0f}s (next in {:.0f}s)",
                            backoff, backoff - elapsed,
                        )
                        continue
                    await self._handle_ibkr(cfg, status)
                    if self._late_connect_task:
                        try:
                            await self._late_connect_task
                        except Exception:
                            pass
                else:
                    timeout = self._BROKER_TIMEOUTS.get(name, 30)
                    await self._try_connect(name, cfg, status, timeout)

            newly = [n for n in failed if n in self.adapters]
            if newly:
                logger.info("broker | reconnected: {}", ", ".join(newly))

            await asyncio.sleep(self._RECONNECT_BASE)

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
        for name, adapter in self.adapters.items():
            try:
                await adapter.disconnect()
                logger.info("broker | {} | disconnected", name)
            except Exception as exc:
                logger.warning("broker | {} | disconnect error: {}", name, exc)
        self.adapters.clear()

    def get_adapter(self, name: str) -> BrokerAdapter | None:
        return self.adapters.get(name)
