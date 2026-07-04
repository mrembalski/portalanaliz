# Scoring module notes

Pipeline per post: free prefilter (length >= 200 chars stripped, Polish analysis
keywords) -> LLM relevance filter -> LLM extraction (tickers, direction, claims,
quality 0-100) -> per-ticker rollup into `stock_scores`.

## Invariants

- A post is "done" when a `post_scores` row exists for `prompts.PROMPT_VERSION`.
  Bump the version on ANY prompt change; old rows stay for comparison — never
  delete or rewrite them.
- One commit per post — a killed run loses at most the in-flight post.
- `--limit N` counts only posts that reach the LLM; prefilter skips are free
  and unlimited.
- Provider-agnostic: model specs are `provider:model` where provider is
  `anthropic` or `local` (OpenAI-compatible /chat/completions — Ollama,
  LM Studio, vLLM at `LOCAL_LLM_BASE_URL`). Everything after the first colon
  is the model name, so Ollama tags like `local:qwen3:8b` work. New providers
  go in `llm.py:make_client`.
- Quoted BBCode blocks are stripped before scoring — quotes are other
  people's words.
- Cost is logged per post (`post_scores.cost_usd`); local models log $0.
  Anthropic prices live in `llm.py:ANTHROPIC_PRICING` (USD/MTok, sticker).
- Rollup anchors `as_of` to the newest scored post (not today) so a
  historical backfill still produces a meaningful window; rows are
  upserted per (ticker, as_of-date), keeping history for charts.

## Config (.env)

`SCORING_FILTER_MODEL` (default `anthropic:claude-haiku-4-5`),
`SCORING_EXTRACT_MODEL` (default `anthropic:claude-sonnet-5`),
`ANTHROPIC_API_KEY`, `LOCAL_LLM_BASE_URL` (default `http://localhost:11434/v1`),
`LOCAL_LLM_API_KEY`.

## Gotchas

- JSON is requested via prompt (not provider-specific structured-output
  APIs) so any provider works; `parse_json_response()` tolerates code
  fences, `<think>` blocks, and surrounding prose.
- Small local models (gemma3:4b) hallucinate company names in `tickers[].name`
  and non-GPW tickers; treat `name` as decoration, join on `ticker`.
- Direction of a post = its primary ticker's direction (topic `ticker_hint`
  as fallback match).
