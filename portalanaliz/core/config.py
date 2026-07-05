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
    # Global focus list (FOCUS_TICKERS="SNT,VOT"): post sync and scoring only
    # touch topics whose ticker_hint is in the list. Empty = everything.
    focus_tickers: tuple[str, ...] = ()


def _parse_tickers(raw: str) -> tuple[str, ...]:
    return tuple(t.strip().upper() for t in raw.split(",") if t.strip())


@dataclass(frozen=True)
class ScoringSettings:
    """LLM scoring config. Model specs are "provider:model", provider one of:

    - anthropic — Anthropic API (needs ANTHROPIC_API_KEY)
    - local    — any OpenAI-compatible server (Ollama, LM Studio, vLLM)
                 at LOCAL_LLM_BASE_URL
    """

    filter_model: str = "local:qwen3.6:27b"
    extract_model: str = "local:qwen3.6:27b"
    # Named prompt set from portalanaliz/scoring/prompts.py:PROMPTS.
    prompt: str = "uv1"
    # Same semantics as Settings.focus_tickers; shares the FOCUS_TICKERS var.
    tickers: tuple[str, ...] = ()
    anthropic_api_key: str = ""
    local_base_url: str = "http://localhost:11434/v1"
    local_api_key: str = "ollama"  # most local servers ignore it but require a value


def load_scoring_settings() -> ScoringSettings:
    load_dotenv()
    return ScoringSettings(
        filter_model=os.environ.get("SCORING_FILTER_MODEL", ScoringSettings.filter_model),
        extract_model=os.environ.get("SCORING_EXTRACT_MODEL", ScoringSettings.extract_model),
        prompt=os.environ.get("SCORING_PROMPT", ScoringSettings.prompt),
        tickers=_parse_tickers(os.environ.get("FOCUS_TICKERS", "")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        local_base_url=os.environ.get("LOCAL_LLM_BASE_URL", ScoringSettings.local_base_url),
        local_api_key=os.environ.get("LOCAL_LLM_API_KEY", ScoringSettings.local_api_key),
    )


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
        focus_tickers=_parse_tickers(os.environ.get("FOCUS_TICKERS", "")),
    )
