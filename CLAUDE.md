# PortalAnaliz Forum Intelligence

Local archive + analysis of the portalanaliz.pl stock forum (Polish, GPW-focused),
fetched via its Tapatalk XML-RPC endpoint. Goal: mine members' analyses to surface
potentially undervalued stocks.

## Layout

- `portalanaliz/core/` — SQLAlchemy models, SQLite setup (incl. FTS5), config from `.env`
- `portalanaliz/scraper/` — Tapatalk client, rate limiter, resumable sync CLI, media downloader
- `portalanaliz/web/` — FastAPI + Jinja2 UI (dashboard, browser, search, authors)
- `portalanaliz/scoring/` — LLM pipeline (prefilter → relevance → extraction → rollup), provider-agnostic (Anthropic or local OpenAI-compatible)
- `data/` — gitignored: `portalanaliz.db` (SQLite) and `media/` (sha256-named files)

## Commands

```bash
.venv/bin/pip install -e .                                    # setup
python -m portalanaliz.scraper.probe                          # connectivity check
python -m portalanaliz.scraper.sync all --forum-id 3 --budget 500   # sync (resumable)
python -m portalanaliz.web                                    # UI on :8000 (preview config uses :8321)
python -m portalanaliz.scoring all --limit 100                # LLM scoring (resumable)
python -m portalanaliz.scoring stats                          # scoring progress + cost
```

Sync subcommands: `forums | topics | posts | media | all`. Forum 3 = GPW stocks (main target).

## Config

`.env` (never commit): `LOGIN`, `PASSWORD` — forum credentials. Optional:
`TAPATALK_URL`, `MIN_REQUEST_INTERVAL`, `REQUEST_TIMEOUT`. Scoring:
`ANTHROPIC_API_KEY`, `SCORING_FILTER_MODEL` / `SCORING_EXTRACT_MODEL`
(`anthropic:<model>` or `local:<model>`), `LOCAL_LLM_BASE_URL` (Ollama default).

## Hard rules

- **Every outgoing request** (API and media) goes through `TapatalkClient.call()` or
  `MediaDownloader`, both sharing one `RateLimiter` (~2.5s + jitter). Never bypass it;
  never lower the interval without the user asking.
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
