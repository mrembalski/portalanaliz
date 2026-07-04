"""Plain-text extraction and free keyword prefilter for scoring.

Both stages are free (no tokens): posts shorter than MIN_CHARS after
stripping, or with no analysis-flavoured vocabulary, never reach the LLM.
"""

from __future__ import annotations

import html
import re

MIN_CHARS = 200

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


# Polish analysis vocabulary (substring match on lowercased text). Broad on
# purpose — the LLM filter does the precise call; this only skips posts with
# zero analytical signal.
ANALYSIS_KEYWORDS = [
    "wycen", "zysk", "strat", "przychod", "przychód", "ebitda", "ebit",
    "marż", "marza", "c/z", "c/wk", "p/e", "ev/", "dywidend", "raport",
    "prognoz", "wskaźnik", "wskazni", "dług", "dlug", "zadłuż", "zadluz",
    "bilans", "przepływ", "przeplyw", "cash flow", "kapitalizac",
    "rekomendac", "kurs docelowy", "cena docelowa", "target", "akcjonari",
    "emisj", "skup akcji", "buyback", "wynik", "kwartał", "kwartal",
    "rentown", "sprzedaż", "sprzedaz", "kontrakt", "backlog", "portfel",
    "niedowartościowan", "niedowartosciowan", "przewartościowan",
    "przewartosciowan", "fundamental", "spółk", "spolk", "mln", "tys.",
    "segment", "inwestyc", "amortyzac",
]


def has_analysis_signal(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ANALYSIS_KEYWORDS)
