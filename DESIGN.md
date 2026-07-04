# PortalAnaliz Forum Intelligence — Design Doc

**Status:** Draft v0.1 (2026-07-04)
**Goal:** Archive posts from the portalanaliz.pl forum, then mine that archive to surface potentially undervalued stocks based on other members' analyses. Visualize results locally.

## 1. Overview

portalanaliz.pl is a Polish stock-analysis forum accessible through the Tapatalk mobile API. Many stocks have dedicated forum topics. We want a local, modular pipeline:

```
[Tapatalk API] → [Scraper] → [Storage (DB + media)] → [Scoring (LLM)] → [Web UI (localhost)]
```

Out of scope for now: the forum's chat/shoutbox, real-time alerting, any trading automation.

## 2. Data source: Tapatalk API

Tapatalk exposes forums via an XML-RPC endpoint, typically at `https://portalanaliz.pl/forum/mobiquo/mobiquo.php` (exact path to be confirmed during discovery). Key methods:

- `login(username, password)` — session auth (credentials supplied by user later; stored only in local `.env`, never committed).
- `get_forum()` — full forum/subforum tree.
- `get_topic(forum_id, start, end, mode)` — topic listing, supports pagination.
- `get_thread(topic_id, start, end)` — posts within a topic, paginated.
- Responses are XML-RPC with base64-encoded text fields (BBCode); posts may embed images as attachments or external links.

Fallback: if the Tapatalk endpoint is unavailable or crippled, scrape the phpBB/vBulletin HTML directly (same politeness rules). Keep the fetch layer behind an interface so the transport can be swapped.

### Rate limiting (hard requirement)

- Single worker, global token bucket, default ~1 request / 2–3 s with jitter.
- Exponential backoff on errors; hard stop on repeated 403/429.
- Conditional fetching: only re-fetch topics whose `last_post_time` changed since our last sync (Tapatalk topic listings include this), so incremental syncs cost a handful of requests, not thousands.

## 3. Architecture — modules

Monorepo, one Python package per module, communicating only through the database (no direct imports across module boundaries except shared `core`).

```
portalanaliz/
├── core/        # shared models, DB session, config, logging
├── scraper/     # Tapatalk client, sync scheduler, media downloader
├── scoring/     # LLM-based analysis extraction & sentiment/quality scoring
├── web/         # FastAPI + frontend served on localhost
├── data/        # SQLite DB + media/ blob store (gitignored)
└── DESIGN.md
```

### 3.1 core
- SQLAlchemy models, Alembic migrations.
- Config via `.env` (pydantic-settings): credentials, rate limits, LLM API key.
- Structured logging.

### 3.2 scraper
- **Full backfill mode:** walk forum tree → all topics → all posts, oldest first, resumable (cursor stored in DB so a crash never re-downloads).
- **Incremental mode:** poll topic listings, fetch only new/edited posts. Intended to run on a schedule (cron/launchd) a few times per day.
- **Media pipeline:** parse post BBCode/HTML for images and attachments; download to `data/media/<sha256>.<ext>`, deduplicated by content hash; store reference rows in DB. Media downloads count against the same rate limiter.
- Stores raw API payloads (compressed) alongside parsed rows, so parsing bugs can be fixed by re-parsing, not re-downloading.

### 3.3 scoring
- Runs independently of scraping; consumes unscored posts from DB.
- Pipeline per post (LLM calls, batched):
  1. **Relevance filter** — is this an actual stock analysis vs. chit-chat? (cheap model / keyword prefilter first to save tokens).
  2. **Extraction** — tickers/companies mentioned, thesis direction (bullish/bearish), key claims (valuation multiples, catalysts, price targets).
  3. **Quality score** — depth of analysis (numbers cited? sources? reasoning?), 0–100.
- Author track record: aggregate per-user historical calls to weight future signals (later phase).
- Stock-level rollup: per company, aggregate recent post sentiment × quality × author weight → "attention/undervaluation" score.
- All prompts versioned in repo; scores stored with `prompt_version` so rescoring is possible.

### 3.4 web (localhost)
- FastAPI backend + simple frontend (server-rendered Jinja or lightweight React — decide at implementation).
- Views:
  - **Dashboard:** top-scored stocks, recent high-quality analyses, scraper health (last sync, request counts).
  - **Stock page:** score history chart, linked forum topics, best posts.
  - **Post browser:** full-text search (SQLite FTS5), rendered posts with local images.
  - **Authors:** leaderboard by historical analysis quality.

## 4. Data model (initial)

- `forums` (id, tapatalk_id, name, parent_id)
- `topics` (id, forum_id, title, ticker_hint, last_post_at, post_count, last_synced_at)
- `posts` (id, topic_id, author_id, posted_at, raw_bbcode, rendered_text, edited_at, raw_payload_ref)
- `authors` (id, username, post_count, track_record_score)
- `media` (id, post_id, source_url, local_path, sha256, mime, fetched_at)
- `stocks` (id, ticker, name, gpw_symbol) + `topic_stocks` mapping (a topic may cover one stock; a post may mention many)
- `post_scores` (post_id, prompt_version, is_analysis, direction, quality, extracted_json, scored_at)
- `stock_scores` (stock_id, as_of, composite_score, inputs_json)
- `sync_state` (cursor bookkeeping for resumable scraping)

SQLite to start (single user, local). Schema kept Postgres-compatible in case of migration.

## 5. Tech stack

- Python 3.12, `httpx`, `xmltodict`/custom XML-RPC client, SQLAlchemy + Alembic, SQLite (FTS5), FastAPI, Anthropic API for scoring (model configurable; cheap model for filtering, stronger model for extraction).
- Git from day one; `data/`, `.env`, media all gitignored.

## 6. Phases

1. **P0 — Skeleton:** repo, core models, config, migrations.
2. **P1 — Scraper:** Tapatalk client, backfill + incremental sync, media download, rate limiting. *Milestone: full local archive.*
3. **P2 — Browser UI:** post search/reading on localhost (useful even before scoring).
4. **P3 — Scoring:** LLM pipeline, stock rollups.
5. **P4 — Dashboard:** scores, charts, author leaderboard.
6. **Later:** author track-record backtesting against actual GPW price data, alerting.

## 7. Risks / open questions

- Tapatalk endpoint path/availability on portalanaliz.pl — verify first (needs credentials).
- Forum ToS: personal archival for private use; keep request rate polite, no redistribution.
- Post volume unknown → backfill duration unknown; resumability makes this a non-issue beyond time.
- Polish-language content: LLM prompts must handle Polish; ticker extraction needs GPW ticker/company alias list.
- Images containing the actual analysis (screenshots of tables): scoring phase may need vision-capable model — deferred, but media is archived from day one so nothing is lost.
