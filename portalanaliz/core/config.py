"""Application settings loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_TAPATALK_URL = "https://portalanaliz.pl/forum/mobiquo/mobiquo.php"


@dataclass(frozen=True)
class Settings:
    login: str
    password: str
    tapatalk_url: str = DEFAULT_TAPATALK_URL
    # Politeness: minimum seconds between outgoing requests (jitter added on top).
    min_request_interval: float = 2.5
    request_timeout: float = 30.0


def load_settings() -> Settings:
    load_dotenv()
    login = os.environ.get("LOGIN", "")
    password = os.environ.get("PASSWORD", "")
    if not login or not password:
        raise RuntimeError("LOGIN and PASSWORD must be set in the environment or .env")
    return Settings(
        login=login,
        password=password,
        tapatalk_url=os.environ.get("TAPATALK_URL", DEFAULT_TAPATALK_URL),
        min_request_interval=float(os.environ.get("MIN_REQUEST_INTERVAL", "2.5")),
        request_timeout=float(os.environ.get("REQUEST_TIMEOUT", "30")),
    )
