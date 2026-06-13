"""Generic Cloudflare solve via a headful zendriver browser, run as a SUBPROCESS under Xvfb.

This is the strongest solver tier — zendriver drives a real, evasion-hardened browser whose
``Tab.verify_cf()`` passes the Turnstile / managed challenges that FlareSolverr and a plain headless
render can't. It returns the fully-rendered page so the caller gets real content even when the origin
keeps challenging cheaper clients.

Run standalone (the app invokes it out-of-process so the heavy headful browser never touches the
event loop), under an X server:

    xvfb-run -a -s "-screen 0 1280x1024x24" python -m app.ingestion.cf_browser <url>

Prints ``{"status", "html", "body_text", "cookies", "user_agent"}`` as JSON on stdout.
``body_text`` is ``document.body.innerText`` — clean JSON for an API URL rendered in the browser.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# Shared Chromium-locator (defined in comix_browser, kept in one place).
from .comix_browser import _chrome_path


async def solve_url(url: str, *, settle_s: float = 3.0) -> dict:
    """Navigate to ``url``, clear any Cloudflare challenge, and return the rendered page + cookies."""
    import zendriver as zd

    browser = await zd.start(
        headless=False, sandbox=False, browser_executable_path=_chrome_path(),
        browser_args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                      "--disable-software-rasterizer"],
    )
    try:
        tab = await browser.get(url)
        try:
            await tab.verify_cf()
        except Exception as exc:  # noqa: BLE001 — verify_cf is noisy; clearance often lands anyway
            print(f"verify_cf: {exc!r}", file=sys.stderr)
        await tab.sleep(settle_s)
        html = await tab.get_content() or ""
        body_text = ""
        try:
            body_text = await tab.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            pass
        ua = ""
        try:
            ua = await tab.evaluate("navigator.userAgent") or ""
        except Exception:  # noqa: BLE001
            pass
        cookies: list[dict] = []
        try:
            for c in await browser.cookies.get_all():
                cookies.append({"name": c.name, "value": c.value, "domain": c.domain or ""})
        except Exception:  # noqa: BLE001
            pass
        return {"status": 200, "html": html, "body_text": body_text,
                "cookies": cookies, "user_agent": ua}
    finally:
        try:
            await browser.stop()
        except Exception:  # noqa: BLE001
            pass


async def _main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.ingestion.cf_browser <url>", file=sys.stderr)
        return 2
    result = await solve_url(sys.argv[1],
                             settle_s=float(os.environ.get("SHELF_SOLVER_SETTLE_S", "3")))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
