# PortalAnaliz Forum Intelligence

Local archive + analysis of the portalanaliz.pl stock forum (Polish, GPW-focused),
fetched via its Tapatalk XML-RPC endpoint. Goal: mine members' analyses to surface
potentially undervalued stocks.

## Layout

- `portalanaliz/core/` — SQLAlchemy models, SQLite setup (incl. FTS5), config from `.env`
- `portalanaliz/scraper/` — Tapatalk client, rate limiter, resumable sync CLI
- `portalanaliz/web/` — FastAPI + Jinja2 UI (dashboard, browser, search, authors)
- `portalanaliz/scoring/` — LLM pipeline for binary per-post undervaluation signals (free prefilter → one batched undervalued 0/1 call, 5 posts/call → per-ticker signal counts), provider-agnostic (Anthropic or local OpenAI-compatible)
- `data/` — gitignored: `portalanaliz.db` (SQLite)

## Commands

```bash
.venv/bin/pip install -e .                                    # setup
python -m portalanaliz.scraper.probe                          # connectivity check
python -m portalanaliz.scraper.sync all --forum-id 3 --budget 500   # sync (resumable)
python -m portalanaliz.web                                    # UI on :8000 (preview config uses :8321)
python -m portalanaliz.scoring all --limit 100                # LLM scoring (resumable)
python -m portalanaliz.scoring stats                          # scoring progress + cost
python -m portalanaliz.scoring prompts                        # list prompt sets
python -m portalanaliz.scoring score --rerun all              # fresh pass of active config
```

Sync subcommands: `forums | topics | posts | all`. Forum 3 = GPW stocks (main target).

## Config

`.env` (never commit): `LOGIN`, `PASSWORD` — forum credentials. Optional:
`TAPATALK_URL`, `MIN_REQUEST_INTERVAL`, `REQUEST_TIMEOUT`. Scoring:
`ANTHROPIC_API_KEY`, `SCORING_MODEL` (`anthropic:<model>` or `local:<model>`;
single model — no separate filter stage), `SCORING_PROMPT` (built-in set in
`scoring/prompts.py` or a `prompts/<name>.prompt` file), `LOCAL_LLM_BASE_URL`
(any OpenAI-compatible server; Ollama default).
`FOCUS_TICKERS` (e.g. `SNT,VOT`) — global limiter: post sync and scoring only
touch topics with those ticker hints; empty = everything.

## Hard rules

- **Every outgoing request** goes through `TapatalkClient.call()`, which applies one
  `RateLimiter` (~2.5s + jitter). Never bypass it; never lower the interval without the
  user asking.
- Sync is resumable and budget-capped (`--budget`); never re-fetch stored data.
  Full raw API payload lives in `posts.raw_json` (zlib JSON) — parsing bugs are fixed
  by re-parsing, not re-downloading.
- No tests yet; verify scraper changes with a small `--budget` live run.

## Gotchas

- Server emits non-standard XML-RPC dateTime (`+00:00` suffix); `tapatalk.py`
  decodes Binary/DateTime manually. Don't switch to stdlib builtin-type parsing.
- Post search = FTS5 external-content table + triggers on `posts`; `init_db()`
  auto-rebuilds when row counts drift.
- Forum holds ~150k posts; full backfill takes hours by design. Be polite.
