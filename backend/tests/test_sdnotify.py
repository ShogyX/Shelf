"""O4: sd_notify(3) client — readiness/watchdog messages reach systemd, no-op otherwise."""
from __future__ import annotations

import socket

from app import sdnotify


def test_notify_is_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False  # not under systemd → no-op, never raises


def test_notify_sends_to_unix_socket(monkeypatch, tmp_path):
    sock_path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        assert sdnotify.notify("READY=1") is True
        assert srv.recv(64) == b"READY=1"
        assert sdnotify.notify("WATCHDOG=1") is True
        assert srv.recv(64) == b"WATCHDOG=1"
    finally:
        srv.close()


def test_watchdog_interval_is_half_the_deadline(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.watchdog_interval_s() is None             # no watchdog configured
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")          # 120s deadline
    assert sdnotify.watchdog_interval_s() == 60.0             # ping at half
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert sdnotify.watchdog_interval_s() is None             # malformed → no ping
