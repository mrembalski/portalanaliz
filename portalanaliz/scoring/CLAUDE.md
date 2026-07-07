# Scoring module notes

Goal: binary UNDERVALUATION SIGNAL per post — not company scoring. Pipeline per
post: free prefilter (length >= 200 chars stripped, Polish analysis keywords)
-> ONE batched LLM undervaluation call (`undervalued` 0/1 per post) -> per-ticker
rollup counting undervalued posts vs posts analyzed into `stock_scores`.

There is a SINGLE LLM stage. The old two-stage "relevance filter -> extract"
design is gone: chit-chat just scores 0, decided by the same call. Posts go to
the model in BATCHES of `BATCH_SIZE` (=5): it receives that many numbered posts
and returns `{"scores": [0,1,...]}`, one 0/1 per post in order. The batched call
returns only the binary signal, so the undervaluation signal lands on the
topic's own `ticker_hint` (no per-post ticker/reason extraction anymore).

## Invariants

- A scoring config = (prompt set, model). It's still recorded on every
  `post_scores` row using the existing (prompt_version, filter_model,
  extract_model) columns: with the filter stage removed, new rows store
  `filter_model=""` and `extract_model=<model>`. This keeps them distinct from
  the pre-refactor two-stage rows (which have a real filter_model), so old
  experiments stay comparable. A post is "done" when a row exists for the
  ACTIVE config — changing prompt or model starts a fresh pass; old rows stay.
  Rollup and web UI only read the active config's rows.
- Prompts: built-in in `prompts.py:PROMPTS` (`uv3` = requires the thesis to
  come from the author's OWN analysis/research) plus user files in
  `prompts/<name>.prompt`
  (single `[scoring]` section — legacy `[extract]` accepted, `[filter]`
  ignored; see `prompts/README.md`). Each set is just the CRITERIA; the batch
  framing + JSON-array output contract are added by `prompts._compose()`.
  Selected via SCORING_PROMPT / `--prompt`; list with the `prompts` subcommand.
  To experiment, ADD a new set/file — never edit one that has scored rows.
- Reruns are explicit and config-scoped: `--rerun all` or
  `--rerun <status,...>` drops the ACTIVE config's matching rows (honoring
  the ticker scope) before scoring; other configs are never touched.
  `--retry-errors` = `--rerun error`. Statuses: `skipped_short`,
  `skipped_keywords`, `scored`, `error` (`chit_chat` is legacy — no longer
  produced).
- `FOCUS_TICKERS` (shared with the scraper) narrows scoring to topics with
  those ticker_hints; empty = whole archive. CLI `--tickers` overrides.
- One commit per BATCH — a killed run loses at most the in-flight batch
  (<= BATCH_SIZE posts). This holds under `--workers N` too: N concurrent
  workers, each with its own DB session and client; the prefilter runs in the
  main thread and each DB batch is drained before the next is fetched, so no
  post is handed out twice. Default `--workers 1` = sequential.
- `--limit N` counts only posts that reach the LLM; prefilter skips are free
  and unlimited. A batch is trimmed so the limit isn't overshot.
- Per-post token/cost is the batch call's usage split evenly across its posts
  (remainder to the first row); the run's stat totals use the real call usage.
- Provider-agnostic: model specs are `provider:model`. Providers:
  `anthropic` (API, needs ANTHROPIC_API_KEY); `local` (OpenAI-compatible
  /chat/completions — Ollama, LM Studio, vLLM at `LOCAL_LLM_BASE_URL`);
  `ollama` (Ollama native /api/chat with `think:false` — same
  `LOCAL_LLM_BASE_URL`, strips a trailing `/v1`; use for BULK local scoring:
  disabling thinking collapses reasoning models' output from thousands of
  tokens to the ~20-token JSON, a 10-100x throughput win the /v1 endpoint
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
  signal names the ticker (`tickers_json`, which under the batched pipeline is
  the topic's hint). `as_of` anchors to the newest scored post; rows upserted
  per (ticker, as_of-date), keeping history for charts.
- `post_scores.is_analysis/quality/direction/claims_json/summary` are dead
  columns from the pre-pivot (`v1`) and two-stage eras — the batched pipeline
  never writes them, and all rows that used them have been deleted. Kept in the
  schema only to avoid a table rebuild.

## Config (.env, all overridable per run via CLI flags)

`SCORING_MODEL` (default `local:qwen3.6:27b`; old `SCORING_EXTRACT_MODEL` is
read as a fallback), `SCORING_PROMPT` (default `uv3`), `FOCUS_TICKERS`
(e.g. `SNT,VOT`), `ANTHROPIC_API_KEY`, `LOCAL_LLM_BASE_URL`
(default `http://localhost:11434/v1`), `LOCAL_LLM_API_KEY`. CLI: `--model
--prompt --tickers --rerun --limit --workers`. `stats` prints a per-config
breakdown and marks the active one; `prompts` lists prompt sets and origin.

## Gotchas

- JSON is requested via prompt (not provider-specific structured-output
  APIs) so any provider works; `parse_json_response()` tolerates code
  fences, `<think>` blocks, and surrounding prose. `parse_scores()` then
  validates the array length equals the batch size — a mismatch makes the
  whole batch error rows rather than silently misaligning signals to posts.
- Reasoning models (qwen3+, deepseek-r1) burn completion tokens on thinking
  before the JSON — `_batch_max_tokens()` scales the cap with batch size.
- `tickers_json` under the batched pipeline is `[topic.ticker_hint]` for an
  undervalued post, else `[]`.
