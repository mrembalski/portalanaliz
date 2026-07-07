"""Minimal BBCode -> HTML renderer for phpBB/Tapatalk post content.

Not a full BBCode implementation; covers the tags that actually occur in
the archive and strips the rest. Input is escaped first, so the output is
safe to mark as trusted HTML.
"""

from __future__ import annotations

import html
import re

QUOTE_RE = re.compile(
    r'\[quote(?:[=\s][^\]]*)?\](?P<body>(?:(?!\[/?quote).)*?)\[/quote\]',
    re.IGNORECASE | re.DOTALL,
)
QUOTE_NAME_RE = re.compile(r'\[quote(?:=| [^\]]*?name=)&quot;?([^&\]]+?)&quot;?[\]\s]',
                           re.IGNORECASE)
QUOTE_OPEN_RE = re.compile(r'\[quote[^\]]*\]', re.IGNORECASE)
QUOTE_CLOSE_RE = re.compile(r'\[/quote\]', re.IGNORECASE)
IMG_RE = re.compile(r'\[img[^\]]*\](.*?)\[/img\]', re.IGNORECASE | re.DOTALL)
URL_WITH_TEXT_RE = re.compile(r'\[url=(?:&quot;)?([^\]&]+?)(?:&quot;)?\](.*?)\[/url\]',
                              re.IGNORECASE | re.DOTALL)
URL_BARE_RE = re.compile(r'\[url\](.*?)\[/url\]', re.IGNORECASE | re.DOTALL)
CODE_RE = re.compile(r'\[code[^\]]*\](.*?)\[/code\]', re.IGNORECASE | re.DOTALL)
SIMPLE_TAGS = {"b": "strong", "i": "em", "u": "u", "s": "del"}
# Formatting tags we drop while keeping their inner text.
STRIP_TAGS = r"size|color|center|left|right|justify|font|highlight|list|\*|attachment|youtube|media|video|spoiler|hr|table|tr|td|sub|sup|email"
STRIP_RE = re.compile(rf'\[/?(?:{STRIP_TAGS})[^\]]*\]', re.IGNORECASE)
BARE_URL_RE = re.compile(r'(?<![">=])(https?://[^\s<\[]+)')


def render(content: str, attachments: list[dict] | None = None) -> str:
    """Render BBCode to HTML.

    Tapatalk empties inline ``[img][/img]`` tags and moves the real file URLs
    into a separate ``attachments`` array; pass it so those images stay
    clickable.
    """
    text = html.escape(content or "", quote=True)
    placeholders: list[str] = []
    # URLs for empty [img] tags, consumed in document order.
    attach_urls = [a["url"] for a in (attachments or [])
                   if a.get("content_type") == "image" and a.get("url")]

    def stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    def media_link(src: str) -> str:
        return stash(
            f'<a href="{html.escape(src)}" class="media-placeholder" '
            f'target="_blank" rel="noopener">image</a>'
        )

    def img_sub(m: re.Match) -> str:
        # Media is no longer downloaded/hosted, so show a placeholder where an
        # image used to be. Link to the original URL when it looks like one.
        src = html.unescape(m.group(1).strip())
        if src.startswith(("http://", "https://")):
            return media_link(src)
        if attach_urls:  # empty tag -> next attachment URL
            return media_link(attach_urls.pop(0))
        return stash('<span class="media-placeholder">image</span>')

    def url_text_sub(m: re.Match) -> str:
        href = m.group(1).strip()
        return stash(f'<a href="{href}" target="_blank" rel="noopener">') + m.group(2) + stash("</a>")

    def url_bare_sub(m: re.Match) -> str:
        href = m.group(1).strip()
        return stash(f'<a href="{href}" target="_blank" rel="noopener">{href}</a>')

    def code_sub(m: re.Match) -> str:
        return stash(f"<pre>{m.group(1)}</pre>")

    text = CODE_RE.sub(code_sub, text)
    text = IMG_RE.sub(img_sub, text)
    text = URL_WITH_TEXT_RE.sub(url_text_sub, text)
    text = URL_BARE_RE.sub(url_bare_sub, text)

    # Quotes, innermost first so nesting works.
    def quote_cite(tag: str) -> str:
        name_match = QUOTE_NAME_RE.match(tag)
        return f"<cite>{name_match.group(1)}</cite>" if name_match else ""

    def quote_sub(m: re.Match) -> str:
        return (stash("<blockquote>") + quote_cite(m.group(0))
                + m.group("body") + stash("</blockquote>"))

    for _ in range(6):
        text, n = QUOTE_RE.subn(quote_sub, text)
        if n == 0:
            break

    # Unbalanced quote tags (Tapatalk truncates long posts mid-quote):
    # treat a stray opener as a blockquote running to the end of the post,
    # and drop stray closers.
    def quote_open_sub(m: re.Match) -> str:
        return stash("<blockquote>") + quote_cite(m.group(0))

    text, n_open = QUOTE_OPEN_RE.subn(quote_open_sub, text)
    text = QUOTE_CLOSE_RE.sub("", text)
    text += stash("</blockquote>") * n_open

    for tag, repl in SIMPLE_TAGS.items():
        text = re.sub(rf'\[{tag}\]', stash(f"<{repl}>"), text, flags=re.IGNORECASE)
        text = re.sub(rf'\[/{tag}\]', stash(f"</{repl}>"), text, flags=re.IGNORECASE)

    text = STRIP_RE.sub("", text)
    text = BARE_URL_RE.sub(lambda m: stash(
        f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'), text)
    text = text.replace("\n", stash("<br>"))

    # Attachments not referenced by an inline [img] tag: append as links.
    for src in attach_urls:
        text += stash("<br>") + media_link(src)

    for i, fragment in enumerate(placeholders):
        text = text.replace(f"\x00{i}\x00", fragment)
    return text
