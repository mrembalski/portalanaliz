"""Media handling: extract image/attachment URLs from posts, download them.

Downloads share the forum rate limiter — a media fetch is an outgoing
request like any other.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from portalanaliz.core.util import utcnow
from portalanaliz.core.db import MEDIA_DIR
from portalanaliz.core.models import Media
from portalanaliz.scraper.ratelimit import RateLimiter
from portalanaliz.scraper.tapatalk import USER_AGENT, RequestBudgetExceeded

log = logging.getLogger(__name__)

IMG_TAG_RE = re.compile(r"\[img[^\]]*\](.*?)\[/img\]", re.IGNORECASE | re.DOTALL)

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def extract_media_urls(post_payload: dict) -> list[tuple[str, str]]:
    """Return (url, kind) pairs found in a raw post payload."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, kind: str) -> None:
        url = url.strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            found.append((url, kind))

    for match in IMG_TAG_RE.finditer(post_payload.get("post_content") or ""):
        add(match.group(1), "inline")

    for key in ("attachments", "inlineattachments"):
        for att in post_payload.get(key) or []:
            if isinstance(att, dict):
                url = att.get("url") or att.get("thumbnail_url") or ""
                if url:
                    add(url, "attachment")

    return found


class MediaDownloader:
    def __init__(self, limiter: RateLimiter, timeout: float = 60.0,
                 max_requests: int | None = None) -> None:
        self.limiter = limiter
        self.max_requests = max_requests
        self.requests_made = 0
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )

    def download_pending(self, session: Session, max_attempts: int = 3) -> int:
        """Download all pending media rows; returns number completed."""
        pending = session.scalars(
            select(Media).where(Media.status == "pending", Media.attempts < max_attempts)
        ).all()
        done = 0
        for item in pending:
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise RequestBudgetExceeded(f"media request budget ({self.max_requests}) exhausted")
            try:
                self._download_one(session, item)
                done += 1
            except httpx.HTTPError as exc:
                item.attempts += 1
                if item.attempts >= max_attempts:
                    item.status = "failed"
                log.warning("media %s failed (attempt %d): %s", item.source_url, item.attempts, exc)
            session.commit()
        return done

    def _download_one(self, session: Session, item: Media) -> None:
        self.limiter.wait()
        self.requests_made += 1
        resp = self._http.get(item.source_url)
        if resp.status_code >= 400:
            item.attempts += 1
            if resp.status_code == 404 or item.attempts >= 3:
                item.status = "failed"
            return

        data = resp.content
        sha = hashlib.sha256(data).hexdigest()
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
        ext = MIME_EXT.get(mime) or mimetypes.guess_extension(mime) or _ext_from_url(item.source_url)

        filename = f"{sha}{ext}"
        path = MEDIA_DIR / filename
        if not path.exists():
            path.write_bytes(data)

        item.local_path = filename
        item.sha256 = sha
        item.mime = mime or None
        item.size = len(data)
        item.status = "done"
        item.fetched_at = utcnow()

    def close(self) -> None:
        self._http.close()


def _ext_from_url(url: str) -> str:
    tail = url.split("?")[0].rsplit(".", 1)
    if len(tail) == 2 and len(tail[1]) <= 5 and tail[1].isalnum():
        return "." + tail[1].lower()
    return ".bin"
