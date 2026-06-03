"""Headless-browser fetch path (per-source `render_js`).

Renders JavaScript-heavy pages and waits out passive anti-bot JS challenges
(e.g. Cloudflare's "Just a moment…") that a plain HTTP client cannot handle.
A single browser + context is reused so that any clearance cookie obtained on the
first navigation is carried to subsequent chapter fetches.

This is an opt-in capability, only usable on sources the operator has marked
permitted — it is not a tool for evading access controls on sites you may not read.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("shelf.browser")

# Markers that indicate an anti-bot interstitial is still being shown.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "cf-challenge",
    "challenge-platform",
)

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


class RenderedPage:
    def __init__(self, status: int, text: str, url: str, body_text: str = "") -> None:
        self.status_code = status
        self.text = text          # full rendered HTML
        self.body_text = body_text  # document.body.innerText — clean JSON for API responses
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"rendered fetch returned HTTP {self.status_code} for {self.url}")


class BrowserFetcher:
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._context is not None:
            return self._context
        async with self._lock:
            if self._context is not None:
                return self._context
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # optional 'render' extra not installed
                raise RuntimeError(
                    "JS rendering needs the optional 'render' extra. Install it with "
                    "`pip install -e .[render] && playwright install chromium` "
                    "(only needed for sources with render_js enabled)."
                ) from exc

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=self.user_agent
                or (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            await self._context.add_init_script(_STEALTH_INIT)
            log.info("browser fetcher launched")
            return self._context

    async def render(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        headers: dict[str, str] | None = None,
        challenge_timeout_s: float = 25.0,
        nav_timeout_s: float = 45.0,
    ) -> RenderedPage:
        context = await self._ensure()
        page = await context.new_page()
        try:
            if headers:
                # e.g. an Authorization bearer for an authed JSON API behind Cloudflare.
                await page.set_extra_http_headers(headers)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_s * 1000)
            status = resp.status if resp else 0

            # Wait out a passive challenge: poll until a content selector appears
            # or the challenge markers disappear from the document.
            deadline = challenge_timeout_s
            interval = 0.75
            waited = 0.0
            while waited < deadline:
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=int(interval * 1000))
                        break
                    except Exception:
                        pass
                else:
                    title = (await page.title() or "").lower()
                    body = (await page.content())[:4000].lower()
                    if not any(m in title or m in body for m in _CHALLENGE_MARKERS):
                        # Give the real content a beat to hydrate.
                        await page.wait_for_timeout(400)
                        break
                await page.wait_for_timeout(int(interval * 1000))
                waited += interval

            html = await page.content()
            final_url = page.url
            try:
                body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                body_text = ""
            # If an anti-bot *challenge* cleared, the navigation status (e.g. 403/503 from the
            # interstitial) is stale — trust the content and report 200. Only do this for the
            # codes Cloudflare uses for challenges; a genuine 401/404/418 (e.g. J-Novel's
            # members-only "BLITZ" 418) is a real origin response and MUST be preserved so the
            # caller can classify it (members-only → unavailable, not a failed/garbage fetch).
            if status in (403, 429, 503) and not any(
                m in html[:4000].lower() for m in _CHALLENGE_MARKERS
            ):
                status = 200
            return RenderedPage(status=status or 200, text=html, url=final_url, body_text=body_text)
        finally:
            await page.close()

    async def aclose(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._context = self._browser = self._pw = None
