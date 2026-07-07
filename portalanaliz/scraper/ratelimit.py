"""Global politeness throttle for all outgoing forum requests."""

from __future__ import annotations

import random
import threading
import time


class RateLimiter:
    """Enforces a minimum interval (plus jitter) between requests.

    Thread-safe; a single instance must be shared by every component that
    talks to the forum (API calls and media downloads alike).
    """

    def __init__(self, min_interval: float = 1.0, jitter: float = 0.75) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        """Block until a request slot is available, then claim it."""
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + (
                self.min_interval + random.uniform(0, self.jitter)
            )
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """Push the next slot further out (used after 429/5xx responses)."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)
