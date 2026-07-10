"""The timed-out xvfb-run solver must kill its WHOLE process tree, not just the wrapper.

Regression for the memory/swap balloon: a bare ``proc.kill()`` SIGKILLs only the directly-launched
``xvfb-run`` wrapper, leaving its child Xvfb + headful Chrome running as orphans (hundreds of MB each)
that pile up over a multi-hour crawl. ``kill_solver_subprocess`` kills the process GROUP (the launchers
use ``start_new_session=True``) so the grandchild dies too, and reaps the wrapper.
"""
import asyncio
import os
import signal

from app.ingestion.zendriver_solver import kill_solver_subprocess


def _grandchild_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_kill_solver_subprocess_kills_whole_group(tmp_path):
    async def run() -> None:
        pidfile = tmp_path / "grandchild.pid"
        # Wrapper (stands in for xvfb-run) backgrounds a long-lived grandchild (stands in for Chrome),
        # records its PID, then blocks. proc.kill() alone would orphan the grandchild.
        script = f"sleep 300 & echo $! > {pidfile}; wait"
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", script, start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(50):  # wait for the grandchild to register its PID
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        gpid = int(pidfile.read_text().strip())
        assert _grandchild_alive(gpid)

        await kill_solver_subprocess(proc)

        assert proc.returncode is not None, "wrapper not reaped (would zombie)"
        for _ in range(40):  # group-kill propagation is async
            if not _grandchild_alive(gpid):
                break
            await asyncio.sleep(0.05)
        assert not _grandchild_alive(gpid), "grandchild orphaned — only the wrapper was killed"

    asyncio.run(run())


def test_solve_recovers_json_from_polluted_stdout(monkeypatch):
    """cf_browser prints its result as one JSON line, but zendriver logs a benign "no Cloudflare
    challenge appeared" line to stdout ahead of it. solve() must recover the payload — a whole-stdout
    json.loads used to fail and discard a SUCCESSFUL solve (returning None), which silently defeated
    the zendriver escalation tier for Turnstile-gated sources like comix.to reader pages."""
    import json as _json
    from app.ingestion import zendriver_solver as z

    payload = {"status": 200, "html": "<a href='/title/31z3-kingdom/1-chapter-1'>c1</a>",
               "body_text": "", "cookies": [], "user_agent": "UA"}
    polluted = (b"Timeout: Cloudflare challenge elements not found or not visible within 15 seconds.\n"
                + _json.dumps(payload).encode() + b"\n")

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return polluted, b""

    async def _fake_exec(*a, **k):
        return _FakeProc()

    monkeypatch.setattr(z, "available", lambda: True)
    monkeypatch.setattr(z, "in_cooldown", lambda url: False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    out = asyncio.run(z.solve("https://comix.to/title/31z3-kingdom?page=1"))
    assert out is not None and out["status"] == 200 and "chapter-1" in out["html"]


def test_kill_solver_subprocess_survives_dead_process():
    """Already-exited proc → no raise (the timeout path must never throw out of the solver)."""
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            "true", start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await kill_solver_subprocess(proc)  # must not raise

    asyncio.run(run())


if __name__ == "__main__":  # ponytail: runnable without pytest
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        test_kill_solver_subprocess_kills_whole_group(pathlib.Path(d))
    test_kill_solver_subprocess_survives_dead_process()
    print("ok")
