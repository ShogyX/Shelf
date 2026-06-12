"""Shared anti-bot / Cloudflare challenge detection — ONE marker set, ONE decision function.

Before this module each consumer (fetcher, browser, libgen, engine) had its own slightly-
different marker list and scanned only the first ~4 KB of the body. Real Cloudflare managed-
challenge pages are verbose: their markers (and the literal <title>Just a moment…</title>)
regularly sit past 4 KB, and challenges are frequently served with HTTP 200 — so a missed scan
returned the interstitial as "real content" and could even store it as a chapter, permanently
poisoning the work. Every consumer now calls :func:`is_challenge` over the FULL body.

False-positive discipline (a chapter of fiction may legitimately contain "just a moment" or
"captcha"): the STRUCTURAL markers below are CF/anti-bot implementation artifacts that cannot
appear in prose, and the broader TEXT markers are only consulted on suspicious statuses
(403/503) or when the response provably transited Cloudflare (cf-ray / server: cloudflare).
"""
from __future__ import annotations

from collections.abc import Mapping

# Implementation artifacts of CF (and common anti-bot) challenge pages — never present in real
# content. Safe to trust at ANY status, including a 200-status challenge.
STRUCTURAL_MARKERS = (
    "_cf_chl",                  # window._cf_chl_opt / __cf_chl_* challenge bootstrap
    "challenge-platform",       # /cdn-cgi/challenge-platform/ script path
    "cf-browser-verification",
    "cf-error-details",
    'id="challenge-form"',
    "cf_challenge",
    "turnstile",                # CF Turnstile captcha widget
    "captcha-delivery",         # DataDome
    "ddos protection by",
)

# Human-readable interstitial text. Only trusted alongside a suspicious status or CF headers —
# real prose can quote these.
TEXT_MARKERS = (
    "<title>just a moment",
    "just a moment...",
    "checking your browser",
    "verifying you are human",
    "attention required! | cloudflare",
    "you have been blocked",
    "enable javascript and cookies to continue",
)


def _header(headers, name: str) -> str:
    try:
        if isinstance(headers, Mapping):
            return (headers.get(name) or headers.get(name.title()) or "").strip()
        get = getattr(headers, "get", None)
        if callable(get):
            return (get(name) or "").strip()
    except Exception:  # noqa: BLE001 — odd header containers must never break detection
        pass
    return ""


def via_cloudflare(headers) -> bool:
    """Did this response transit Cloudflare? (gates the broad text markers on 200s)."""
    return bool(_header(headers, "cf-ray") or "cloudflare" in _header(headers, "server").lower())


def is_challenge(status: int, headers, body: str | bytes | None) -> bool:
    """True when (status, headers, body) is an anti-bot challenge/block page, not real content.

    * ANY non-empty ``cf-mitigated`` header is a challenge (``challenge``, ``managed_challenge``,
      ``block`` — older code matched only the exact string "challenge" and missed the rest).
    * The FULL body is scanned (the historical [:4000] slice missed verbose challenge pages).
    * Structural markers convict at any status — CF serves challenges with HTTP 200 too.
    * Text markers convict only on suspicious statuses (403/503/429) or when headers prove the
      response transited Cloudflare, so prose quoting "just a moment" is never flagged.
    """
    if _header(headers, "cf-mitigated"):
        return True
    if body is None:
        text = ""
    elif isinstance(body, bytes):
        text = body.decode("utf-8", "replace").lower()
    else:
        text = body.lower()
    if not text:
        return False
    if any(m in text for m in STRUCTURAL_MARKERS):
        return True
    if status in (403, 429, 503) or via_cloudflare(headers):
        return any(m in text for m in TEXT_MARKERS)
    return False


# Visible-word ceiling for an interstitial: challenge pages are short; real chapters run
# hundreds of words. Used by the store-time backstop only.
_MAX_CHALLENGE_WORDS = 300


def looks_like_challenge_page(html: str | None) -> bool:
    """Store-time backstop: does this (raw, pre-sanitize) HTML look like a challenge page?

    Deliberately conservative — per the regression guardrails, prose containing challenge-y
    phrases must NEVER be rejected, so this requires a STRUCTURAL anti-bot artifact to co-occur
    with a short, image-less page. The structural markers live in <script>/attributes, which is
    why this must run BEFORE sanitization strips them."""
    if not html:
        return False
    low = html.lower()
    if "<img" in low:
        return False                      # challenge pages carry no content images
    if not any(m in low for m in STRUCTURAL_MARKERS):
        return False
    # Rough visible-text word count: strip tags/scripts crudely (good enough for a ceiling).
    import re
    visible = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", low,
                     flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    return len(visible.split()) <= _MAX_CHALLENGE_WORDS
