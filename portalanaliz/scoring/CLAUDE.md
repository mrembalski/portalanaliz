# Scoring module notes

Goal: binary UNDERVALUATION SIGNAL per post — not company scoring. Pipeline per
post: free prefilter (length >= 200 chars stripped, Polish analysis keywords)
-> LLM relevance filter (chit-chat gate) -> LLM undervaluation call
(`undervalued` 0/1 + tickers it applies to + one-line reason) -> per-ticker
rollup counting undervalued posts vs posts analyzed into `stock_scores`.

## Invariants

- A scoring config = (prompt set, filter_model, extract_model); all three are
  recorded on every `post_scores` row (even free skips) and form the unique
  key with post_id. A post is "done" when a row exists for the ACTIVE config —
  changing prompt or model starts a fresh comparable pass; old rows stay.
  Rollup and web UI only read the active config's rows.
- Prompts: built-ins in `prompts.py:PROMPTS` (`uv1`; `uv2` = stricter, requires
  a numeric valuation argument) plus user files in `prompts/<name>.prompt`
  ([filter] + [extract] sections; see `prompts/README.md`). Selected via
  SCORING_PROMPT / `--prompt`; list with the `prompts` subcommand. To
  experiment, ADD a new set/file — never edit one that has scored rows.
- Reruns are explicit and config-scoped: `--rerun all` or
  `--rerun <status,...>` drops the ACTIVE config's matching rows (honoring
  the ticker scope) before scoring; other configs are never touched.
  `--retry-errors` = `--rerun error`.
- `FOCUS_TICKERS` (shared with the scraper) narrows scoring to topics with
  those ticker_hints; empty = whole archive. CLI `--tickers` overrides.
- One commit per post — a killed run loses at most the in-flight post.
- `--limit N` counts only posts that reach the LLM; prefilter skips are free
  and unlimited.
- Provider-agnostic: model specs are `provider:model` where provider is
  `anthropic`, `local`, or `claude` (headless `claude -p` CLI — uses the
  Claude subscription quota, e.g. `claude:opus`; cost_usd logs the
  API-equivalent price) (OpenAI-compatible /chat/completions — Ollama,
  LM Studio, vLLM at `LOCAL_LLM_BASE_URL`). Everything after the first colon
  is the model name, so Ollama tags like `local:qwen3:8b` work. New providers
  go in `llm.py:make_client`.
- Quoted BBCode blocks are stripped before scoring — quotes are other
  people's words.
- Cost is logged per post (`post_scores.cost_usd`); local models log $0.
  Anthropic prices live in `llm.py:ANTHROPIC_PRICING` (USD/MTok, sticker).
- Rollup counts: `posts_analyzed` = scored posts in the ticker's topics
  (denominator via `topic.ticker_hint`); `undervalued_posts` = posts whose
  signal names the ticker (`tickers_json`; falls back to the topic's hint when
  the model returns none). `as_of` anchors to the newest scored post (not
  today); rows upserted per (ticker, as_of-date), keeping history for charts.
- `post_scores.quality/direction/claims_json` are legacy columns from the
  pre-pivot quality-scoring era (prompt_version "v1") — readable, not written.

## Config (.env, all overridable per run via CLI flags)

`SCORING_FILTER_MODEL` (default `anthropic:claude-haiku-4-5`),
`SCORING_EXTRACT_MODEL` (default `anthropic:claude-sonnet-5`),
`SCORING_PROMPT` (default `uv1`), `FOCUS_TICKERS` (e.g. `SNT,VOT`),
`ANTHROPIC_API_KEY`, `LOCAL_LLM_BASE_URL` (default `http://localhost:11434/v1`),
`LOCAL_LLM_API_KEY`. CLI: `--filter-model --extract-model --prompt --tickers
--rerun --limit`. `stats` prints a per-config breakdown and marks the active
one; `prompts` lists prompt sets and their origin.

## Gotchas

- JSON is requested via prompt (not provider-specific structured-output
  APIs) so any provider works; `parse_json_response()` tolerates code
  fences, `<think>` blocks, and surrounding prose.
- Reasoning models (qwen3+, deepseek-r1) burn completion tokens on thinking
  before the JSON — keep max_tokens generous (2000/3000 in pipeline.py) or
  responses truncate to empty content.
- Small local models (gemma3:4b) sometimes invent non-GPW tickers; the
  rollup only trusts `tickers_json` for the numerator and the topic's
  `ticker_hint` for the denominator.
- `tickers_json` is a plain list of ticker strings under "uv*" prompt sets;
  legacy "v1" rows hold a list of dicts.
