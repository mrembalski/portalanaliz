# Scraper module notes

Endpoint: phpBB 3.1, Tapatalk plugin `pb31_2.3.0`, api_level 4, login required
(`guest_okay: false`). Methods used: `login` (credentials as XML-RPC Binary),
`get_config`, `get_forum`, `get_topic` (modes `""` and `"TOP"` for stickies),
`get_thread`. Response strings arrive base64-encoded; `_decode()` converts to str.

## Invariants

- Cursor: `Topic.posts_fetched` = number of post positions (0-based) already stored.
  Expected total = `total_post_num` (authoritative, from `get_thread`) falling back to
  `reply_number + 1`. Post sync fetches `[posts_fetched, posts_fetched+49]` pages and
  commits after each page — safe to kill anytime.
- Partially-fetched topics are synced before untouched ones (don't leave threads half-archived).
- Empty page while cursor < expected ⇒ deleted posts; set cursor to server total, move on.
- Media rows are created at post ingest (from `[img]` tags + `attachments`/`inlineattachments`);
  downloading is a separate step (`sync media`), deduped by content sha256.
- `RequestBudgetExceeded` is the clean exit path for budgeted runs — progress must already
  be committed when it propagates.
- HTTP 403 ⇒ raise immediately, no retry (we may be blocked; don't hammer).
- Incremental sync = rerun `topics` (refreshes `reply_number`) then `posts`; only tails
  get fetched. Post edits are not re-fetched (accepted limitation).
