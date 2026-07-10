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
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..config import get_settings
from .. import config_store

log = logging.getLogger("shelf.zendriver")


async def kill_solver_subprocess(proc) -> None:
    """SIGKILL a timed-out xvfb-run solver and REAP it. ``proc.kill()`` alone hits only the
    ``xvfb-run`` wrapper — its child Xvfb + python + headful Chrome are a separate part of the
    process tree and survive as orphans (each Chrome is hundreds of MB; under a multi-hour crawl
    they pile up and exhaust memory+swap). The launchers start the subprocess with
    ``start_new_session=True`` so it leads its own process group; kill the WHOLE group, then
    ``await proc.wait()`` so the wrapper doesn't linger as a zombie."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # already gone
    except PermissionError:
        try:
            proc.kill()  # not a group leader — fall back to the direct child
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except ProcessLookupError:
        pass

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
    cp = (config_store.effective("solver_chrome_path") or "").strip()
    if cp:
        env["SHELF_SOLVER_CHROME_PATH"] = cp
    timeout = float(timeout_s if timeout_s is not None else (config_store.effective("flaresolverr_timeout_s") + 90))
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24",
           sys.executable, "-m", "app.ingestion.cf_browser", url]
    repo_root = str(Path(__file__).resolve().parents[2])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=repo_root, env=env, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await kill_solver_subprocess(proc)
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
    # cf_browser prints its result as one JSON line, but the headful browser stack (zendriver's
    # Cloudflare helper) logs benign diagnostics to stdout ahead of it — e.g. "no Cloudflare
    # challenge appeared". A whole-stdout json.loads then chokes and discards a SUCCESSFUL solve.
    # Scan for the JSON payload line (the object carrying html/body_text) instead of trusting stdout
    # to be pristine. [Same hazard, same fix, as comix_catalog._browser_crawl.]
    data = None
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and (obj.get("html") or obj.get("body_text")):
            data = obj
            break
    if data is None:
        _note_fail(url)
        return None
    _fail_at.pop(_host(url), None)
    return data
