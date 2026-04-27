"""Small Telegram helpers for operator lifecycle notifications."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any

import httpx
from loguru import logger

from system.portfolio_equity import live_portfolio_snapshot


def _telegram_configured() -> tuple[str, str] | None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return None
    disabled = (os.getenv("MYTBOT_DISABLE_TELEGRAM_ALERTS", "") or "").strip().lower()
    if disabled in ("1", "true", "yes", "on"):
        return None
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def _money(v: Decimal) -> str:
    q = v.quantize(Decimal("0.01"))
    return f"{q:,.2f}"


async def _send_telegram(message: str) -> None:
    cfg = _telegram_configured()
    if cfg is None:
        return
    token, chat_id = cfg
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": f"[mytbot] {message}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram lifecycle notification failed | {}", exc)


def _coverage_lines(report: Any) -> list[str]:
    if report is None:
        return ["Coverage: unavailable"]
    try:
        cov = report.coverage()
    except Exception:  # noqa: BLE001
        return ["Coverage: unavailable"]
    included = cov.get("included") or []
    excluded = cov.get("excluded") or []
    lines = [
        f"Included: {', '.join(included) if included else 'none'}",
    ]
    if excluded:
        names = ", ".join(str(x.get("name", "?")) for x in excluded if isinstance(x, dict))
        lines.append(f"Excluded: {names or 'unknown'}")
    return lines


async def send_lifecycle_notification(
    event: str,
    *,
    broker_manager: Any | None,
    broker_report: Any | None,
    paper_mode: bool,
    require_full_coverage: bool = False,
    wait_timeout_sec: float = 0.0,
) -> None:
    """Send the only default Telegram messages: system start and stop."""
    label = event.strip().upper()
    mode = "PAPER" if paper_mode else "LIVE"
    nav_line = "NAV: unavailable"
    deadline = asyncio.get_running_loop().time() + max(0.0, float(wait_timeout_sec))
    if broker_manager is not None:
        while True:
            try:
                snap = await live_portfolio_snapshot(broker_manager)
                cov = broker_report.coverage() if broker_report is not None else {}
                coverage_full = bool(cov.get("full"))
                nav_ready = snap.complete and snap.value > 0
                if nav_ready and (coverage_full or not require_full_coverage):
                    nav_line = f"NAV: {_money(snap.value)}"
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    if require_full_coverage:
                        logger.info(
                            "Telegram lifecycle notification skipped | event={} nav_ready={} coverage_full={} missing={}",
                            label,
                            nav_ready,
                            coverage_full,
                            list(snap.missing),
                        )
                        return
                    if snap.missing:
                        nav_line = f"NAV: verifying ({', '.join(snap.missing)})"
                    else:
                        nav_line = "NAV: 0.00"
                    break
            except Exception as exc:  # noqa: BLE001
                if asyncio.get_running_loop().time() >= deadline:
                    nav_line = f"NAV: unavailable ({exc})"
                    if require_full_coverage:
                        logger.info("Telegram lifecycle notification skipped | event={} error={}", label, exc)
                        return
                    break
            await asyncio.sleep(2.0)

    lines = [
        f"SYSTEM {label} ({mode})",
        nav_line,
        *_coverage_lines(broker_report),
    ]
    await _send_telegram("\n".join(lines))
