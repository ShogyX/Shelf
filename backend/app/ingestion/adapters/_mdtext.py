"""Tiny shared helper to turn plain text / light markdown into paragraph HTML."""
from __future__ import annotations

import re


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_md(s: str) -> str:
    s = _escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"_(.+?)_", r"<em>\1</em>", s)
    return s


def text_to_paragraph_html(text: str) -> str:
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    out: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = (line.strip() for line in block.splitlines() if line.strip())
        inner = "<br/>".join(_inline_md(line) for line in lines)
        out.append(f"<p>{inner}</p>")
    return "\n".join(out)
