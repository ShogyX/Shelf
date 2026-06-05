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
        self._capture_context = None  # hi-DPI context used only for canvas page capture
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
        scroll: int = 0,
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

            # Lazy-loaded content (e.g. a manga reader's pages, or an infinite chapter list)
            # only attaches to the DOM as you scroll. Scroll to the bottom a few times so those
            # <img>/links are present in the captured HTML.
            if scroll > 0:
                for _ in range(scroll):
                    try:
                        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        break
                    await page.wait_for_timeout(700)
                try:
                    await page.evaluate("() => window.scrollTo(0, 0)")
                except Exception:
                    pass

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

    # ----------------------------------------------------------------- canvas capture
    # Some readers (e.g. comix.to) serve a subset of page images pre-scrambled and reassemble
    # them client-side onto a <canvas> (normal pages stay plain <img>). The scramble key is in
    # obfuscated WASM we don't replicate; instead we let the page's own code descramble, then
    # screenshot the resulting canvas. This is gated to operator-permitted sources, same as render().
    _CAPTURE_PROBE = """(n) => {
      const el = document.querySelector(`[data-page="${n}"]`);
      if (!el) return {present:false};
      const c = el.querySelector('canvas');
      const img = el.querySelector('img');
      if (c && c.width > 50) return {present:true, kind:'CANVAS'};
      if (img && img.naturalWidth > 50) return {present:true, kind:'IMG'};
      return {present:true, kind:'none'};
    }"""

    # The reader floats a fixed/sticky page-nav toolbar over the bottom of the page; hide such
    # overlays before screenshotting so a captured page is pure content. Keeps the page containers
    # (and their ancestors) visible; navigation is driven by keyboard/scroll, not the toolbar.
    _HIDE_OVERLAYS = """() => {
      for (const el of document.querySelectorAll('body *')) {
        const pos = getComputedStyle(el).position;
        if ((pos === 'fixed' || pos === 'sticky') && !el.querySelector('[data-page]')) {
          el.style.setProperty('visibility', 'hidden', 'important');
        }
      }
    }"""

    async def _capture_ctx(self):
        """A second context used only for descramble capture. A TALL viewport (not a high device
        pixel ratio) is what gives a high-resolution screenshot here: the reader fits one page to
        the viewport height, so height 1700 renders the page at ~1150–1650px — matching the source.
        device_scale_factor stays 1 on purpose: the reader sizes its <canvas> by devicePixelRatio
        but draws the descrambled image at CSS size, so dsf=2 left the bottom-right of every canvas
        black (a half-rendered page — the 'zoom' bug). Reuses any clearance cookies the main context
        earned so it isn't re-challenged from scratch."""
        if self._capture_context is not None:
            return self._capture_context
        await self._ensure()  # guarantees the browser is launched
        async with self._lock:
            if self._capture_context is not None:
                return self._capture_context
            ua = self.user_agent or (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            self._capture_context = await self._browser.new_context(
                user_agent=ua, viewport={"width": 1280, "height": 1700},
                device_scale_factor=1, locale="en-US",
            )
            await self._capture_context.add_init_script(_STEALTH_INIT)
            try:  # carry over clearance cookies the main context may already hold
                cookies = await self._context.cookies()
                if cookies:
                    await self._capture_context.add_cookies(cookies)
            except Exception:
                pass
            return self._capture_context

    async def _render_reader_page(self, page, n: int, tries: int = 36) -> str:
        """Drive the virtualized reader until [data-page=n] renders a real canvas/img.
        Returns the rendered kind ('CANVAS' | 'IMG' | 'none')."""
        for _ in range(tries):
            info = await page.evaluate(self._CAPTURE_PROBE, n)
            if info.get("present") and info.get("kind") in ("CANVAS", "IMG"):
                return info["kind"]
            # Navigation combo (validated against the live reader): bring the page's container into
            # view, nudge with reader hotkeys + a click, and scroll — until it lazy-renders.
            await page.evaluate(
                "(n) => { const el = document.querySelector(`[data-page=\"${n}\"]`);"
                " if (el) el.scrollIntoView({block:'center'}); window.scrollBy(0, 600); }", n)
            for key in ("ArrowRight", "ArrowDown", "PageDown"):
                try:
                    await page.keyboard.press(key)
                except Exception:
                    pass
            try:
                await page.mouse.click(950, 750)
            except Exception:
                pass
            await page.wait_for_timeout(250)
        info = await page.evaluate(self._CAPTURE_PROBE, n)
        return info.get("kind", "none")

    async def capture_canvas_pages(
        self, url: str, *, want: set[int] | None = None, stop_after: int | None = None,
        nav_timeout_s: float = 45.0, hydrate_timeout_s: float = 30.0,
    ) -> tuple[int, dict[int, bytes]]:
        """Open a reader page, render each page in turn, and return
        ``(total_pages, {1-based page index: PNG bytes})`` for every page that rendered as a
        <canvas> (i.e. was scrambled and got descrambled client-side). ``want`` optionally limits
        which page indices are captured (others are still walked so the reader advances).
        ``stop_after`` stops walking once past that page index — used when the scrambled pages are
        known to be early, to avoid driving the reader through the whole (mostly-normal) tail."""
        ctx = await self._capture_ctx()
        page = await ctx.new_page()
        out: dict[int, bytes] = {}
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_s * 1000)
            # Wait for the reader to hydrate its page containers (and clear any CF interstitial).
            waited, total = 0.0, 0
            while waited < hydrate_timeout_s:
                total = await page.eval_on_selector_all("[data-page]", "els => els.length")
                if total > 0:
                    break
                await page.wait_for_timeout(750)
                waited += 0.75
            if total <= 0:
                return 0, out
            last = min(total, stop_after) if stop_after else total
            for n in range(1, last + 1):
                kind = await self._render_reader_page(page, n)
                if kind != "CANVAS":
                    continue  # normal pages are already correct via the image CDN
                if want is not None and n not in want:
                    continue
                el = await page.query_selector(f'[data-page="{n}"] canvas')
                if el is None:
                    continue
                try:
                    await page.evaluate(self._HIDE_OVERLAYS)
                except Exception:
                    pass
                try:
                    out[n] = await el.screenshot(type="png")
                except Exception:
                    continue
            return total, out
        finally:
            await page.close()

    async def aclose(self) -> None:
        try:
            if self._capture_context:
                await self._capture_context.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._capture_context = self._context = self._browser = self._pw = None
