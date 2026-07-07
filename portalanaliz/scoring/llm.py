"""Provider-agnostic LLM client for the scoring pipeline.

Model specs are "provider:model", e.g. "anthropic:claude-haiku-4-5",
"local:qwen3:8b" (everything after the first colon is the model name, so
Ollama tags with colons work) or "claude:opus". Providers:

- anthropic — Anthropic API (needs ANTHROPIC_API_KEY)
- local     — any OpenAI-compatible chat-completions endpoint (Ollama, ...)
- claude    — the local `claude` CLI in headless mode: burns the user's
              Claude subscription quota instead of API credits.
- codex     — the local `codex exec` CLI (OpenAI GPT-5.x), read-only sandbox.
              No per-call token usage exposed, so cost is logged as $0.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
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
            timeout=600.0,  # local inference can be slow (reasoning models especially)
        )

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        try:
            resp = self._http.post("/chat/completions", json={
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            })
        except httpx.HTTPError as exc:
            # Timeouts/disconnects become an error ROW for the post, not a
            # crashed run (rescore later with --rerun error).
            raise LLMError(f"local LLM request failed: {exc!r}") from exc
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


class OllamaClient(LLMClient):
    """Ollama's native /api/chat with thinking DISABLED (`think: false`).

    The OpenAI-compat /v1 endpoint ignores the think toggle, so reasoning
    models (qwen3+, gemma-reasoning) burn thousands of completion tokens
    thinking before the tiny answer — 10-100x slower. Hitting /api/chat
    with `think: false` collapses output to just the single digit,
    which is all the pipeline needs. Use for bulk local scoring where
    the model's own chain-of-thought isn't worth the throughput hit.
    """

    def __init__(self, spec: str, model: str, base_url: str, api_key: str):
        super().__init__(spec)
        self.model = model
        # LOCAL_LLM_BASE_URL usually ends in /v1 (OpenAI path); native API is
        # at the server root.
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        self._http = httpx.Client(
            base_url=root,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=600.0,
        )

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        try:
            resp = self._http.post("/api/chat", json={
                "model": self.model,
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": max_tokens},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            })
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama request failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise LLMError(f"ollama HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected ollama response shape: {data}") from exc
        return LLMResponse(
            text=text, model=self.spec,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            cost_usd=0.0,
        )

    def close(self) -> None:
        self._http.close()


class ClaudeCLIClient(LLMClient):
    """Headless `claude -p` — one subprocess per call, subscription quota.

    --tools "" and a replaced --system-prompt keep the per-call context small
    (~2.5k tokens instead of the full ~22k Claude Code system prompt);
    MAX_THINKING_TOKENS=0 disables thinking. total_cost_usd from the CLI is
    the API-equivalent price, recorded for comparison even though a
    subscription isn't billed per call.
    """

    def __init__(self, spec: str, model: str):
        super().__init__(spec)
        if shutil.which("claude") is None:
            raise LLMError("`claude` CLI not found on PATH (needed for claude:* models)")
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        proc = subprocess.run(
            ["claude", "-p", user, "--system-prompt", system,
             "--model", self.model, "--output-format", "json", "--tools", ""],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "MAX_THINKING_TOKENS": "0"},
        )
        if proc.returncode != 0:
            raise LLMError(f"claude CLI exit {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:300]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude CLI non-JSON output: {proc.stdout[:300]}") from exc
        if data.get("is_error"):
            raise LLMError(f"claude CLI error result: {str(data)[:300]}")
        usage = data.get("usage") or {}
        inp = (int(usage.get("input_tokens") or 0)
               + int(usage.get("cache_creation_input_tokens") or 0)
               + int(usage.get("cache_read_input_tokens") or 0))
        return LLMResponse(
            text=data.get("result") or "", model=self.spec,
            input_tokens=inp,
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(data.get("total_cost_usd") or 0.0),
        )


class CodexCLIClient(LLMClient):
    """Headless `codex exec` — one subprocess per call (OpenAI GPT-5.x).

    Codex has no separate system role, so system+user are concatenated. Runs
    read-only, ephemeral (no session files), with the final message written to
    a temp file via -o. Token usage isn't exposed on stdout, so cost is $0."""

    def __init__(self, spec: str, model: str):
        super().__init__(spec)
        if shutil.which("codex") is None:
            raise LLMError("`codex` CLI not found on PATH (needed for codex:* models)")
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResponse:
        import tempfile

        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as f:
            out_path = f.name
        try:
            proc = subprocess.run(
                ["codex", "exec", "-m", self.model, "-s", "read-only",
                 "--skip-git-repo-check", "--ephemeral", "-o", out_path,
                 f"{system}\n\n{user}"],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                raise LLMError(f"codex CLI exit {proc.returncode}: "
                               f"{(proc.stderr or proc.stdout)[:300]}")
            with open(out_path) as fh:
                text = fh.read()
        finally:
            os.unlink(out_path)
        return LLMResponse(text=text, model=self.spec, input_tokens=0,
                           output_tokens=0, cost_usd=0.0)


def make_client(spec: str, settings: ScoringSettings) -> LLMClient:
    provider, _, model = spec.partition(":")
    if not model:
        raise LLMError(f"model spec {spec!r} must be 'provider:model'")
    if provider == "anthropic":
        return AnthropicClient(spec, model, settings.anthropic_api_key)
    if provider == "local":
        return OpenAICompatClient(spec, model, settings.local_base_url, settings.local_api_key)
    if provider == "ollama":
        return OllamaClient(spec, model, settings.local_base_url, settings.local_api_key)
    if provider == "claude":
        return ClaudeCLIClient(spec, model)
    if provider == "codex":
        return CodexCLIClient(spec, model)
    raise LLMError(f"unknown provider {provider!r} in {spec!r} "
                   "(use anthropic:, local:, ollama:, claude: or codex:)")


def parse_score(text: str) -> bool:
    """Parse a single-post response into a bool.

    The prompt demands a bare digit, but tolerates code fences, <think>
    blocks from local reasoning models, and stray prose: after stripping
    those, the first standalone 0 or 1 wins. No digit = LLMError (error row,
    rescore later with --rerun error)."""
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    t = re.sub(r"```(?:json)?", "", t).strip()
    m = re.search(r"(?<!\d)[01](?!\d)", t)
    if not m:
        raise LLMError(f"no 0/1 answer in response: {text[:200]!r}")
    return m.group(0) == "1"
