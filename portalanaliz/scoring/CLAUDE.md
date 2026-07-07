# Scoring module notes

Goal: binary UNDERVALUATION SIGNAL per post — not company scoring. Pipeline per
post: ONE LLM undervaluation call (`undervalued` 0/1) -> per-ticker rollup
counting undervalued posts vs posts analyzed into `stock_scores`. The old free
prefilter (length/keywords) is gone — every post goes to the LLM, in full
(no truncation).

There is a SINGLE LLM stage. The old two-stage "relevance filter -> extract"
design is gone: chit-chat just scores 0, decided by the same call. Each post
gets its own call (batching removed): the model receives one post and answers
with a bare digit `0` or `1`. The call returns only the binary signal, so the
undervaluation signal lands on the topic's own `ticker_hint` (no per-post
ticker/reason extraction anymore).

## Invariants

- A scoring config = (prompt set, model). It's still recorded on every
  `post_scores` row using the existing (prompt_version, filter_model,
  extract_model) columns: with the filter stage removed, new rows store
  `filter_model=""` and `extract_model=<model>`. This keeps them distinct from
  the pre-refactor two-stage rows (which have a real filter_model), so old
  experiments stay comparable. A post is "done" when a row exists for the
  ACTIVE config — changing prompt or model starts a fresh pass; old rows stay.
  Rollup and web UI only read the active config's rows.
- Prompts: built-in in `prompts.py:PROMPTS` (`uv4` = requires the thesis to
  come from the author's OWN analysis/research; same criteria as the retired
  batched `uv3`, whose rows stay in the DB) plus user files in
  `prompts/<name>.prompt`
  (single `[scoring]` section — legacy `[extract]` accepted, `[filter]`
  ignored; see `prompts/README.md`). Each set is just the CRITERIA; the
  single-post framing + bare-0/1 output contract are added by `prompts._compose()`.
  Selected via SCORING_PROMPT / `--prompt`; list with the `prompts` subcommand.
  To experiment, ADD a new set/file — never edit one that has scored rows.
- Reruns are explicit and config-scoped: `--rerun all` or
  `--rerun <status,...>` drops the ACTIVE config's matching rows (honoring
  the ticker scope) before scoring; other configs are never touched.
  `--retry-errors` = `--rerun error`. Statuses: `scored`, `error`
  (`chit_chat`, `skipped_short`, `skipped_keywords` are legacy — no longer
  produced).
- `FOCUS_TICKERS` (shared with the scraper) narrows scoring to topics with
  those ticker_hints; empty = whole archive. CLI `--tickers` overrides.
- One commit per POST — a killed run loses at most the in-flight posts (one
  per worker). This holds under `--workers N` too: N concurrent workers, each
  with its own DB session and client; each DB fetch of 500 unscored posts is
  drained before the next, so no post is handed out twice. Default
  `--workers 1` = sequential.
- `--limit N` caps posts sent to the LLM.
- Per-post token/cost is the call's real usage (one call = one post).
- Provider-agnostic: model specs are `provider:model`. Providers:
  `anthropic` (API, needs ANTHROPIC_API_KEY); `local` (OpenAI-compatible
  /chat/completions — Ollama, LM Studio, vLLM at `LOCAL_LLM_BASE_URL`);
  `ollama` (Ollama native /api/chat with `think:false` — same
  `LOCAL_LLM_BASE_URL`, strips a trailing `/v1`; use for BULK local scoring:
  disabling thinking collapses reasoning models' output from thousands of
  tokens to the single digit, a 10-100x throughput win the /v1 endpoint
  can't get because it ignores the think toggle);
  `claude` (headless `claude -p` CLI); `codex` (headless `codex exec` CLI,
  OpenAI GPT-5.x, read-only sandbox — no token usage exposed so cost logs $0).
  Everything after the first colon is the model name, so Ollama tags like
  `local:qwen3:8b` and `codex:gpt-5.5` work. New providers go in
  `llm.py:make_client`.
- Quoted BBCode blocks are stripped before scoring — quotes are other
  people's words.
- Cost is logged per post (`post_scores.cost_usd`); local models log $0.
  Anthropic prices live in `llm.py:ANTHROPIC_PRICING` (USD/MTok, sticker).
- Rollup counts: `posts_analyzed` = scored posts in the ticker's topics
  (denominator via `topic.ticker_hint`); `undervalued_posts` = posts whose
  signal names the ticker (`tickers_json`, which under the current pipeline is
  the topic's hint). `as_of` anchors to the newest scored post; rows upserted
  per (ticker, as_of-date), keeping history for charts.
- `post_scores.is_analysis/quality/direction/claims_json/summary` are dead
  columns from the pre-pivot (`v1`) and two-stage eras — the current pipeline
  never writes them, and all rows that used them have been deleted. Kept in the
  schema only to avoid a table rebuild.

## Config (.env, all overridable per run via CLI flags)

`SCORING_MODEL` (default `local:qwen3.6:27b`; old `SCORING_EXTRACT_MODEL` is
read as a fallback), `SCORING_PROMPT` (default `uv4`), `FOCUS_TICKERS`
(e.g. `SNT,VOT`), `ANTHROPIC_API_KEY`, `LOCAL_LLM_BASE_URL`
(default `http://localhost:11434/v1`), `LOCAL_LLM_API_KEY`. CLI: `--model
--prompt --tickers --rerun --limit --workers`. `stats` prints a per-config
breakdown and marks the active one; `prompts` lists prompt sets and origin.

## Gotchas

- The answer is a bare digit requested via prompt (no provider-specific
  structured-output APIs) so any provider works; `parse_score()` tolerates
  code fences, `<think>` blocks, and stray prose — first standalone 0/1 wins,
  no digit = error row (rescore with `--rerun error`).
- Reasoning models (qwen3+, deepseek-r1) burn completion tokens on thinking
  before the digit — `MAX_TOKENS` (=2000) leaves headroom.
- `tickers_json` is `[topic.ticker_hint]` for an undervalued post, else `[]`.
