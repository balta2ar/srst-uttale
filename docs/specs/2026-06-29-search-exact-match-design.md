# Fast exact-match path in /uttale/Search (download speedup)

Date: 2026-06-29
Status: approved (design)

## Problem

The offline PWA downloads an episode by fetching transcript lines per segment
file via `/api/lines` → `/uttale/Search` (sent as `q=""`, `scope=<full vtt path>`,
`limit=1000`), then the audio. The `Search` query is:

```sql
SELECT filename, start, end_time, text FROM lines
WHERE LOWER(text) LIKE LOWER(?) AND LOWER(filename) LIKE LOWER(?) LIMIT ?
```

For a download this becomes `LOWER(text) LIKE '%%'` (matches every row) `AND
LOWER(filename) LIKE '%<path>%'` — a **full scan of ~8.8M rows** with two
`LOWER()` calls per row. Measured cost: **~2 seconds per segment** over HTTP
(~690ms in-process). A download issues this sequentially for every segment, so a
~15-segment episode spends ~30s just on line queries.

This is **pre-existing** behavior, not caused by the recent reindex feature
(verified: the `search()` SQL is unchanged by that work, the live DB is untouched
since before it, and no reindex ever ran on it). But it is very fixable.

## Change

In `search(q, scope, limit)` (`uttale/backend/server.py:614`), add one branch:

- **When `q` is empty (after `.strip()`) AND `scope` is non-empty** → exact-match
  path:
  ```sql
  SELECT filename, start, end_time, text FROM lines
  WHERE filename = ? ORDER BY start LIMIT ?
  ```
  binding `scope` as the exact filename. Measured **~40–100ms** vs ~2s — a
  ~17–31× speedup.
- **Otherwise** (any non-empty text query, or empty scope) → the **existing**
  `LIKE` query, unchanged.

The `Search` response model and the `results` row mapping are unchanged. This is
the only structural change to the endpoint.

### Ordering

`ORDER BY start` is added to the exact-match path. Verified on the live data: the
current query already returns a single file's rows in `start` order, and the
exact-match-with-`ORDER BY start` query returns the **identical** rows in the
**identical** order. So this does not reorder what downloads receive — it only
makes the ordering explicit and deterministic (VTT `HH:MM:SS.mmm` strings sort
lexically = chronologically; the 10-minute buckets never exceed the `HH` field).

## Why it is safe

- The **only** caller that sends `q=""` together with a `scope` is the offline
  `/api/lines` proxy (the download path), which always sends a full VTT path —
  so an exact `filename = scope` match is correct for it.
- The **search box** sends `q=<text>` with an empty or partial `scope`, so it
  never enters the branch; full-text search behavior is unchanged.
- Empirically: same rows, same order, ~17× faster.

## Data flow (callers unchanged)

`downloadEpisode` → `Api.lines(vtt)` → `/api/lines` (offline proxy: `q=""`,
`scope=vtt`, `limit=1000`) → `/uttale/Search` → exact-match branch → the file's
lines in ~100ms instead of ~2s. No offline-app or proxy changes are required.

## Error handling

Unchanged. The branch only selects which SQL runs inside the existing `try`; a
query failure still raises `HTTPException(500)`.

## Testing (uttale AGENTS.md: stdlib unittest, no pytest)

Add `TestSearchExactMatch` to `uttale/backend/test_server.py`, using a temp
DuckDB (set `server.args` + `server.init_database()`, the existing pattern):

- Seed two files' lines, inserting some rows **out of `start` order** for one
  file.
- `search(q="", scope="<file A path>")` returns **only** file A's lines, **ordered
  by `start`**, excludes file B, and the out-of-order rows come back sorted.
- `search(q="", scope="")` returns the LIKE-path result (empty-scope behavior
  preserved — matches all, since both `LIKE` operands are `%%`).
- `search(q="<text>", scope="<partial>")` still uses the LIKE path (text search
  unaffected) and finds the expected text match.
- New code adds **zero** new ruff issues (pre-existing E722/F401 out of scope).
- Run via the uv test env: `/tmp/opencode/uttale-test/bin/python -m unittest
  uttale.backend.test_server -v`.

### Live smoke

Against a throwaway server (uttale `127.0.0.1:7011`, temp DBs under
`/tmp/opencode`): time `/uttale/Search?q=&scope=<a real vtt>&limit=1000` and
confirm it drops from ~seconds to tens of ms; confirm a text query
(`q=<word>&scope=`) still returns matches. Kill the throwaway by saved PID; do
not touch the real :7010 server.

## Scope / non-goals

- No new endpoint, no offline proxy change, no DuckDB index (the in-`Search`
  branch was the chosen approach).
- The full-text search box path is untouched.
- Deploying (restarting :7010 to pick up the change) remains the user's action.

## Files touched

- `uttale/backend/server.py`: the exact-match branch in `search()`.
- `uttale/backend/test_server.py`: `TestSearchExactMatch`.
- `docs/specs/2026-06-29-search-exact-match-design.md`: this document.
