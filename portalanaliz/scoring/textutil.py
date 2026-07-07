"""Plain-text extraction (BBCode stripping) for scoring."""

from __future__ import annotations

import html
import re

_QUOTE_RE = re.compile(
    r"\[quote(?:[=\s][^\]]*)?\](?:(?!\[/?quote).)*?\[/quote\]",
    re.IGNORECASE | re.DOTALL,
)
_IMG_RE = re.compile(r"\[img[^\]]*\].*?\[/img\]", re.IGNORECASE | re.DOTALL)
_URL_TEXT_RE = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"\[/?[a-zA-Z*][^\]]*\]")
_WS_RE = re.compile(r"[ \t]+")


def strip_bbcode(content: str) -> str:
    """BBCode -> plain text. Quoted blocks are dropped entirely — quotes are
    other people's words and shouldn't influence this post's score."""
    text = content or ""
    for _ in range(6):  # innermost-first so nested quotes unwind
        text, n = _QUOTE_RE.subn("", text)
        if n == 0:
            break
    text = _IMG_RE.sub("", text)
    text = _URL_TEXT_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
