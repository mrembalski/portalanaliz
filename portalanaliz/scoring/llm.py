"""Provider-agnostic LLM client for the scoring pipeline.

Model specs are "provider:model", e.g. "anthropic:claude-haiku-4-5" or
"local:qwen3:8b" (everything after the first colon is the model name, so
Ollama tags with colons work). "local" talks to any OpenAI-compatible
chat-completions endpoint (Ollama, LM Studio, vLLM).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from portalanaliz.core.config import ScoringSettings

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Local models cost nothing.
# Sonnet 5 sticker is $3/$15 (intro $2/$10 through 2026-08-31) — we log the
# sticker price as an upper bound.
ANTHROPIC_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}


@dataclass
class LLMResponse:
    text: str
    model: str  # full spec, e.g. "anthropic:claude-haiku-4-5"
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLMError(Exception):
    pass


class LLMClient:
    """One provider+model. complete() returns the raw text response."""

    def __init__(self, spec: str):
        self.spec = spec

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        raise NotImplementedError

    def close(self) -> None:
        pass


class AnthropicClient(LLMClient):
    def __init__(self, spec: str, model: str, api_key: str):
        super().__init__(spec)
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY not set (needed for anthropic:* models)")
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        inp, out = response.usage.input_tokens, response.usage.output_tokens
        in_rate, out_rate = ANTHROPIC_PRICING.get(self.model, (5.0, 25.0))
        cost = (inp * in_rate + out * out_rate) / 1_000_000
        return LLMResponse(text=text, model=self.spec, input_tokens=inp,
                           output_tokens=out, cost_usd=cost)

    def close(self) -> None:
        self._client.close()


class OpenAICompatClient(LLMClient):
    """Chat-completions client for local servers (Ollama, LM Studio, vLLM)."""

    def __init__(self, spec: str, model: str, base_url: str, api_key: str):
        super().__init__(spec)
        self.model = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300.0,  # local inference can be slow
        )

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        resp = self._http.post("/chat/completions", json={
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })
        if resp.status_code != 200:
            raise LLMError(f"local LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected local LLM response shape: {data}") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text, model=self.spec,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=0.0,
        )

    def close(self) -> None:
        self._http.close()


def make_client(spec: str, settings: ScoringSettings) -> LLMClient:
    provider, _, model = spec.partition(":")
    if not model:
        raise LLMError(f"model spec {spec!r} must be 'provider:model'")
    if provider == "anthropic":
        return AnthropicClient(spec, model, settings.anthropic_api_key)
    if provider == "local":
        return OpenAICompatClient(spec, model, settings.local_base_url, settings.local_api_key)
    raise LLMError(f"unknown provider {provider!r} in {spec!r} (use anthropic: or local:)")


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_response(text: str) -> dict:
    """Extract a JSON object from a model response (tolerates code fences,
    <think> blocks from local reasoning models, and surrounding prose)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text)
    m = _JSON_RE.search(text)
    if not m:
        raise LLMError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(m.group(0))
