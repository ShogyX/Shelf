"""Minimal sd_notify(3) client for systemd ``Type=notify`` readiness + watchdog (O4).

A no-op when the process isn't run under a notify-enabled systemd unit (``NOTIFY_SOCKET``
unset), so importing/calling it is safe in every environment (tests, dev, Type=simple).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger("shelf.sdnotify")


def notify(state: str) -> bool:
    """Send a service state line to systemd (e.g. ``READY=1``, ``WATCHDOG=1``, ``STOPPING=1``).
    Returns False (no-op) when not run under systemd or on any socket error."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # An '@'-prefixed address is the Linux abstract namespace (leading NUL byte).
    target = ("\0" + addr[1:]) if addr.startswith("@") else addr
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(target)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError as exc:  # not fatal — readiness/watchdog is best-effort
        log.debug("sd_notify(%r) failed: %s", state, exc)
        return False


def watchdog_interval_s() -> float | None:
    """Half the systemd WatchdogSec deadline (from ``WATCHDOG_USEC``), i.e. how often to ping —
    or None when no watchdog is configured."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return max(1.0, int(usec) / 1_000_000.0 / 2.0)
    except ValueError:
        return None


async def watchdog_loop() -> None:
    """Ping ``WATCHDOG=1`` at half the configured WatchdogSec until cancelled. Returns immediately
    (never loops) when no watchdog is configured, so it's safe to always create the task."""
    interval = watchdog_interval_s()
    if interval is None:
        return
    while True:
        await asyncio.sleep(interval)
        notify("WATCHDOG=1")
