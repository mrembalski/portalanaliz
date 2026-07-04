"""Tapatalk (Mobiquo) XML-RPC client.

Every outgoing request to the forum goes through TapatalkClient.call(),
which applies the shared rate limiter and retry/backoff policy. Higher
layers (sync jobs, media downloader) must never talk to the forum directly.
"""

from __future__ import annotations

import logging
import xmlrpc.client
from datetime import datetime
from typing import Any

import httpx

from portalanaliz.core.config import Settings
from portalanaliz.scraper.ratelimit import RateLimiter

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; Tapatalk client; portalanaliz-archiver/0.1)"

# Statuses that mean "slow down / try again later".
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class TapatalkError(Exception):
    """A Tapatalk method returned result=false or the transport failed for good."""


class RequestBudgetExceeded(Exception):
    """Raised when the per-run request budget is used up; sync exits cleanly."""


class TapatalkClient:
    def __init__(self, settings: Settings, limiter: RateLimiter | None = None) -> None:
        self.settings = settings
        self.limiter = limiter or RateLimiter(min_interval=settings.min_request_interval)
        self._http = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "text/xml",
                "Accept-Encoding": "gzip",
            },
            timeout=settings.request_timeout,
            follow_redirects=True,
        )
        self._logged_in = False
        self.requests_made = 0
        # Optional per-run cap on outgoing requests; None = unlimited.
        self.max_requests: int | None = None

    # ------------------------------------------------------------------ core

    def call(self, method: str, *params: Any, max_tries: int = 4) -> Any:
        """Perform one XML-RPC call with rate limiting and backoff.

        Strings destined for the API must be passed as str (converted to
        base64 automatically where Tapatalk expects it via Binary params
        handled by callers); responses have base64 payloads decoded to str.
        """
        body = xmlrpc.client.dumps(params, methodname=method, encoding="utf-8")
        backoff = 5.0
        last_error: Exception | None = None

        for attempt in range(1, max_tries + 1):
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise RequestBudgetExceeded(f"request budget ({self.max_requests}) exhausted")
            self.limiter.wait()
            self.requests_made += 1
            try:
                resp = self._http.post(self.settings.tapatalk_url, content=body)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("%s: transport error (attempt %d): %s", method, attempt, exc)
                self.limiter.penalize(backoff)
                backoff *= 2
                continue

            if resp.status_code in RETRYABLE_STATUSES:
                last_error = TapatalkError(f"{method}: HTTP {resp.status_code}")
                log.warning("%s: HTTP %d (attempt %d), backing off", method, resp.status_code, attempt)
                self.limiter.penalize(backoff * 4)
                backoff *= 2
                continue

            if resp.status_code == 403:
                # Repeated 403 means we're blocked; do not hammer the server.
                raise TapatalkError(f"{method}: HTTP 403 — access denied, stopping")

            resp.raise_for_status()
            # No builtin types: the server emits dateTime values with a
            # timezone suffix (e.g. 20260704T12:00:00+00:00) that the stdlib
            # datetime parsing rejects, so we decode Binary/DateTime ourselves.
            parsed, _ = xmlrpc.client.loads(resp.content)
            return _decode(parsed[0])

        raise TapatalkError(f"{method}: giving up after {max_tries} attempts") from last_error

    # ----------------------------------------------------------------- auth

    def login(self) -> dict:
        result = self.call(
            "login",
            xmlrpc.client.Binary(self.settings.login.encode()),
            xmlrpc.client.Binary(self.settings.password.encode()),
        )
        if not result.get("result"):
            raise TapatalkError(f"login failed: {result.get('result_text', '')!r}")
        self._logged_in = True
        return result

    def ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def call_authed(self, method: str, *params: Any) -> Any:
        """Call a method that requires a session; re-login once if it expired."""
        self.ensure_login()
        result = self.call(method, *params)
        if isinstance(result, dict) and result.get("result") is False:
            log.info("%s returned result=false, re-logging in and retrying once", method)
            self._logged_in = False
            self.ensure_login()
            result = self.call(method, *params)
            if isinstance(result, dict) and result.get("result") is False:
                raise TapatalkError(f"{method} failed: {result.get('result_text', '')!r}")
        return result

    # -------------------------------------------------------------- methods

    def get_config(self) -> dict:
        return self.call("get_config")

    def get_forum(self) -> list:
        return self.call_authed("get_forum")

    def get_topics(self, forum_id: str, start: int, last: int, mode: str = "") -> dict:
        params: tuple[Any, ...] = (str(forum_id), start, last)
        if mode:
            params += (mode,)
        return self.call_authed("get_topic", *params)

    def get_thread(self, topic_id: str, start: int, last: int, return_html: bool = False) -> dict:
        return self.call_authed("get_thread", str(topic_id), start, last, return_html)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TapatalkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _decode(value: Any) -> Any:
    """Recursively decode Tapatalk payloads: base64 -> str, dateTime -> datetime."""
    if isinstance(value, xmlrpc.client.Binary):
        return value.data.decode("utf-8", errors="replace")
    if isinstance(value, xmlrpc.client.DateTime):
        return _parse_datetime(value.value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decode(v) for v in value]
    return value


def _parse_datetime(raw: str) -> Any:
    """Parse Tapatalk dateTime strings, tolerating timezone suffixes."""
    for fmt in ("%Y%m%dT%H:%M:%S%z", "%Y%m%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return raw
