# Personal preferences

## Commands
- Don't run dev server commands - assume it's already running 

## Code style 
- Always strive for consise, simple solutions. 
- If a problem can be solved in a simpler way, propose it. 

## General preferences
- If asked to do too much work at once, stop and state that clearly. 
- If computer use is helpful for completing or verifying work, shell out to gpt-5.5 with Codex for it.
- Keep the documentation (CLAUDE.md files) updated and concise. 

## Picking the right models for workflows and subagents
Rankings, higher = better. Cost reflects what I actually pay, not list price. Intelligence is how hard a problem you can hand the model unsupervised. Taste covers UI/UX, code quality, API design. 

| Model    | Cost | Intelligence | Taste |
| -------- | :--: | :----------: | :---: |
| GPT-5.5  |   9  |       8      |   5   |
| Sonnet-5 |   5  |       5      |   7   |
| Opus-4.8 |   4  |       7      |   8   |
| Fable-5  |   2  |       9      |   9   |

How to apply: 
- These are the defaults, not limits.
- Cost is a tie-breaker only. 
- Bulk/mechanical work (clear-spec implementation, data analysis, migrations): gpt-5.5 - it's effectively free.
- Anything user-facing (UI, API design) needs taste >= 7.
- Reviews of plans/implementations: Fable-5 or Opus-4.8, optionally GPT-5.5 as an extra independent perspective. 
- Never use Haiku.
- Mechanics: gpt-5.5 is only reachable through the Codex CLI - `codex exec` or `codex review`. 

Using gpt-5.5 inside workflows and subagents (the model parameter only takes Claude models, so use a wrapper): 
- Spawn a thin Claude wrapper agent with `model: 'sonnet', effort: 'low'` whose prompt instructs it to write a self-contained codex prompt, run `codex exec` via Bash, and return 

# PortalAnaliz Forum Intelligence

Local archive + analysis of the portalanaliz.pl stock forum (Polish, GPW-focused),
fetched via its Tapatalk XML-RPC endpoint. 

Ultimate goal: mine members' analyses to surface potentially undervalued stocks.

## Layout

- `portalanaliz/core/` — SQLAlchemy models, SQLite setup (incl. FTS5), config from `.env`
- `portalanaliz/scraper/` — Tapatalk client, rate limiter, resumable sync CLI
- `portalanaliz/web/` — FastAPI + Jinja2 UI (dashboard, browser, search, authors)
- `portalanaliz/scoring/` — LLM pipeline for binary per-post provider-agnostic undervaluation signals
- `data/` — gitignored: `portalanaliz.db` (SQLite)

## Commands
Remember to use `.venv/bin/python` instead of `python`.

```bash
.venv/bin/pip install -e .                                    # setup
.venv/bin/python -m portalanaliz.scraper.probe                                # connectivity check
.venv/bin/python -m portalanaliz.scraper.sync all --forum-id 3 --budget 500   # sync (resumable)
.venv/bin/python -m portalanaliz.web                                          # UI on :8000 (preview config uses :8321)
.venv/bin/python -m portalanaliz.scoring all --limit 100                      # LLM scoring (resumable)
.venv/bin/python -m portalanaliz.scoring stats                                # scoring progress + cost
.venv/bin/python -m portalanaliz.scoring prompts                              # list prompt sets
.venv/bin/python -m portalanaliz.scoring score --rerun all                    # fresh pass of active config
```

Sync subcommands: `forums | topics | posts | all`. Forum 3 = GPW stocks (main target).

Scoring runs are long-lived and network-bound (each batch waits on the remote LLM), so the
process shows near-zero local CPU while it works; that's normal, not a hang.

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
  `RateLimiter` (~1s + jitter). Never bypass it; never lower the interval without the
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
