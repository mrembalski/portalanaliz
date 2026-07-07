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
    if provider == "claude":
        return ClaudeCLIClient(spec, model)
    if provider == "codex":
        return CodexCLIClient(spec, model)
    raise LLMError(f"unknown provider {provider!r} in {spec!r} "
                   "(use anthropic:, local:, claude: or codex:)")


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


def _coerce_binary(value) -> bool:
    """0/1, "0"/"1", true/false, "true"/"false" -> bool (tolerant of what
    small local models emit for the batch scoring array)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "tak"):
        return True
    if s in ("0", "false", "no", "nie", ""):
        return False
    raise LLMError(f"not a 0/1 value: {value!r}")


def parse_scores(text: str, expected: int) -> list[bool]:
    """Parse a batch response {"scores": [0, 1, ...]} into `expected` bools.

    Raises LLMError if the array is missing or its length doesn't match the
    batch size — so a malformed batch becomes error rows, not silent misalign-
    ment of signals to posts."""
    data = parse_json_response(text)
    scores = data.get("scores")
    if not isinstance(scores, list):
        raise LLMError(f"response has no 'scores' array: {str(data)[:200]!r}")
    if len(scores) != expected:
        raise LLMError(f"expected {expected} scores, got {len(scores)}: {scores!r}")
    return [_coerce_binary(s) for s in scores]
