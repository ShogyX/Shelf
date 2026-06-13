"""Async wrapper around the out-of-process headful zendriver Cloudflare solver (``cf_browser``).

This is the STRONGEST tier of the fetcher's auto-escalation ladder (plain HTTP → FlareSolverr → in-app
render → here). It passes Turnstile / managed challenges the cheaper tiers can't. Because the headful
browser is heavy and slow it runs in its own process under Xvfb; a per-host cooldown stops us paying a
full solve timeout on every request to a host the solver currently can't pass.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..config import get_settings

log = logging.getLogger("shelf.zendriver")

_fail_at: dict[str, float] = {}       # host -> monotonic time of last failed solve
_FAIL_COOLDOWN_S = 600.0


def available() -> bool:
    """True when a zendriver solve CAN run here: the package is importable AND xvfb-run exists (the
    headful browser needs an X server). Checked before escalating so we never block on a dead path."""
    return (importlib.util.find_spec("zendriver") is not None
            and shutil.which("xvfb-run") is not None)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def in_cooldown(url: str) -> bool:
    fa = _fail_at.get(_host(url))
    return fa is not None and (time.monotonic() - fa) < _FAIL_COOLDOWN_S


def _note_fail(url: str) -> None:
    _fail_at[_host(url)] = time.monotonic()


async def solve(url: str, *, timeout_s: float | None = None) -> dict | None:
    """Solve ``url`` in a headful zendriver subprocess. Returns
    ``{"status","html","body_text","cookies","user_agent"}`` or None (unavailable / cooling down /
    failed). Never raises."""
    if not available() or in_cooldown(url):
        return None
    s = get_settings()
    env = dict(os.environ)
    if (s.solver_chrome_path or "").strip():
        env["SHELF_SOLVER_CHROME_PATH"] = s.solver_chrome_path.strip()
    timeout = float(timeout_s if timeout_s is not None else (s.flaresolverr_timeout_s + 90))
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24",
           sys.executable, "-m", "app.ingestion.cf_browser", url]
    repo_root = str(Path(__file__).resolve().parents[2])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=repo_root, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("zendriver solve timed out after %ss for %s", timeout, url)
            _note_fail(url)
            return None
    except (FileNotFoundError, OSError) as exc:
        log.warning("zendriver solve unavailable (%s)", exc)
        _note_fail(url)
        return None
    if proc.returncode != 0:
        log.warning("zendriver solve exited %s for %s: %s", proc.returncode, url,
                    (err or b"")[-200:].decode("utf-8", "replace"))
        _note_fail(url)
        return None
    try:
        data = json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError:
        _note_fail(url)
        return None
    if not isinstance(data, dict) or not (data.get("html") or data.get("body_text")):
        _note_fail(url)
        return None
    _fail_at.pop(_host(url), None)
    return data
