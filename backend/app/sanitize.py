"""Reader-content sanitization (Stage 3).

Takes arbitrary stored chapter HTML and produces a clean, safe, semantic subset:
allowlisted tags only, scripts/styles/handlers stripped, structure normalized.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# Tags we keep. Everything else is unwrapped (children preserved) or dropped.
ALLOWED_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "em", "i", "strong", "b", "u", "s", "small", "sub", "sup",
    "blockquote", "q", "cite",
    "ul", "ol", "li",
    "a", "img",
    "figure", "figcaption",
    "pre", "code",
    "div", "span",
}
# Tags whose entire subtree is removed.
DROP_SUBTREE = {"script", "style", "noscript", "iframe", "object", "embed", "svg", "form", "head"}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
}

_WS_RE = re.compile(r"[ \t ]+")


# Layout-only class names we preserve so comic/image chapters render correctly
# (full-width, gapless pages). Any other class value is still stripped.
ALLOWED_CLASSES = {"comic", "comic-page"}

# --- Advertisement image filtering -----------------------------------------------------
# Some reader sites inject ad/banner <img> into the chapter body; these get fetched + cached
# into the work. Drop them at sanitize time (before image localization, so they never download).
# Heuristics are deliberately conservative to avoid stripping real illustrations/comic pages.
_AD_URL_RE = re.compile(
    r"(?:doubleclick|googlesyndication|googleadservices|google_ads|adservice|amazon-adsystem|"
    r"adsystem|adnxs|moatads|media\.net|taboola|outbrain|popads|popcash|propellerads|exoclick|"
    r"juicyads|adsterra|/ads?[/_-]|[/_?&-]ads?\.|[/_-]banner[/_.-]|sponsor)",
    re.I,
)
_AD_CLASS_TOKENS = {
    "ad", "ads", "adv", "advert", "advertisement", "adsbygoogle", "ad-banner", "ad-container",
    "ad-slot", "ad-wrapper", "banner", "banner-ad", "sponsor", "sponsored", "promo",
}


def _is_ad_image(tag: Tag) -> bool:
    """Heuristic: is this <img> an advertisement/banner rather than story content?"""
    src = " ".join(
        str(tag.get(a) or "")
        for a in ("src", "data-src", "data-original", "data-lazy-src")
    )
    if _AD_URL_RE.search(src):
        return True
    classes = {c.lower() for c in (tag.get("class") or [])}
    if classes & _AD_CLASS_TOKENS:
        return True
    idv = (tag.get("id") or "").lower()
    if idv in _AD_CLASS_TOKENS or idv.startswith(("ad-", "ads-", "ad_", "banner", "sponsor")):
        return True
    alt = (tag.get("alt") or "").lower()
    return "advertis" in alt or "sponsored" in alt


def _clean_attrs(tag: Tag) -> None:
    allowed = ALLOWED_ATTRS.get(tag.name, set())
    for attr in list(tag.attrs.keys()):
        if attr == "class":
            kept = [c for c in tag.get("class", []) if c in ALLOWED_CLASSES]
            if kept:
                tag["class"] = kept
            else:
                del tag["class"]
        elif attr not in allowed:
            del tag[attr]
        elif attr in ("href", "src"):
            # Allowlist URI schemes on BOTH links and image sources. img/src can't run script in a
            # modern browser, but a data:-URI src enables tracking/exfil if CSP is ever relaxed — only
            # http(s), protocol-relative, and site-relative refs are allowed.
            # Strip ALL ASCII whitespace/control chars first: a browser ignores them when resolving a
            # URL, so "java\tscript:alert(1)" would slip past a plain startswith("javascript:") check
            # (SEC-L2). Then reject any EXPLICIT scheme that isn't http/https; schemeless (relative /
            # protocol-relative) refs have no scheme and are kept.
            low = re.sub(r"[\x00-\x20]+", "", str(tag.get(attr, ""))).lower()
            scheme = re.match(r"^([a-z][a-z0-9+.\-]*):", low)
            if scheme and scheme.group(1) not in ("http", "https"):
                del tag[attr]


def sanitize_html(raw: str) -> str:
    """Return a sanitized, normalized HTML string safe to render in the reader."""
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "lxml")

    # Strip comments.
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    # Drop dangerous subtrees outright.
    for tag in soup.find_all(DROP_SUBTREE):
        tag.decompose()

    # Walk all tags; drop ad images, unwrap disallowed, clean attrs on allowed.
    for tag in soup.find_all(True):
        if tag.name == "img" and _is_ad_image(tag):
            tag.decompose()  # advertisement/banner — never part of the story
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
        else:
            _clean_attrs(tag)

    body = soup.body or soup
    html = body.decode_contents() if isinstance(body, Tag) else str(soup)
    # Collapse runs of whitespace but keep tag structure.
    html = _WS_RE.sub(" ", html)
    html = re.sub(r"(\s*<br\s*/?>\s*){3,}", "<br/><br/>", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def count_words(html_or_text: str) -> int:
    soup = BeautifulSoup(html_or_text or "", "lxml")
    text = soup.get_text(" ", strip=True)
    return len([w for w in text.split() if w])


def text_to_html(text: str) -> str:
    """Convert plain text into paragraph HTML (used by TXT/MD-ish imports)."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    parts = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Preserve single newlines inside a block as <br/>.
        inner = "<br/>".join(_escape(line.strip()) for line in block.splitlines() if line.strip())
        parts.append(f"<p>{inner}</p>")
    return "\n".join(parts)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["sanitize_html", "count_words", "text_to_html"]
# silence unused import warnings for NavigableString in some linters
_ = NavigableString
