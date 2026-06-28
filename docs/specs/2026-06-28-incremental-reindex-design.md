# Incremental reindex of recent podcasts

Date: 2026-06-28
Status: approved (design)

## Problem

The search database (`lines` + `scopes` in DuckDB) is populated by `reindex()`,
which **wipes the whole table** (`DELETE FROM lines` / `DELETE FROM scopes`) and
rebuilds everything matched by the current `fd` scan. There is no way to add only
the latest episodes (e.g. the last 7/30 days, or the last 6 months) without
rebuilding the entire 8.8M-row table, and a pattern-filtered reindex is dangerous:
it still wipes the whole `lines` table but only re-inserts the filtered subset,
destroying every other podcast's rows.

We want to reindex only podcasts whose **episode date** falls within a time
window, triggered **live over HTTP** (no service restart), **idempotently**
(re-running never duplicates rows; a podcast is never added twice).

## Prerequisite bug fix

`process_vtt(vtt, root)` — the VTT→rows parser — is **broken in the committed
tree** (HEAD `2fe6152`). Commit `8abf473` deleted its `def` line, leaving an
orphaned function body at `server.py:432-442`; the name is undefined and the call
site at `server.py:450` raises `NameError` on *any* reindex (CLI or
`POST /uttale/Reindex`). The deployed server is an editable install of this exact
tree, so reindex is currently non-functional in production.

Restore the signature above the orphaned body:

```python
def process_vtt(vtt: str, root: str) -> List[tuple]:
```

This un-breaks the existing full reindex and is a prerequisite for the new
incremental path.

## What "latest" means

The **episode date encoded in the path** drives the filter, e.g.
`48k/<podcast>/<YYYYMMDD>/by10m/<file>.vtt` → `20260623`. This matches how
episodes are organized; no new tracking table or mtime reading is needed.

- Parse the date as the first path segment that is exactly 8 digits (robust to
  the `48k/` prefix; in practice segment index 2).
- A file whose path has no parseable 8-digit date is **skipped** by a window
  reindex (it cannot be "recent").

## Selecting files

Discovery is unchanged: `fd --type f --extension vtt --base-directory <root>`
lists every VTT relative to `--root`. The new date filter is applied to that list.

New helpers (pure functions, easy to unit-test):

- `episode_date_of(filename: str) -> str` → the 8-digit date string, or `""` if
  none.
- `since_from_days(days: int, today: date) -> str` → `(today - days)` formatted
  `YYYYMMDD`. `today` is injectable for tests; production passes
  `date.today()`.
- `within_window(filename: str, since: str) -> bool` → `True` iff
  `episode_date_of(filename) >= since` (string comparison is correct for
  zero-padded `YYYYMMDD`) and a date was parseable.

`days` is converted to a `since` string; `since` may also be passed directly.

## Idempotent per-file replace (core)

For a window reindex, replace the whole-table wipe with a **per-file replace**.
After parsing the matched files into rows, under the write lock:

1. For each affected `filename`: `DELETE FROM lines WHERE filename = ?`.
2. `INSERT` that file's freshly-parsed rows (`filename, start, end_time, text`).

This guarantees:

- **No duplicates** on re-run — a file's old rows are removed before re-insert,
  even though `lines` has no primary key.
- **Edits are picked up** — a re-transcribed VTT refreshes its rows.
- Files **outside the window are never touched**.

### Scopes reconciliation

`scopes` is the distinct-filename list backing the scope dropdown. In the same
locked section, after updating `lines` for the affected filenames:

- Delete `scopes` rows for the affected filenames.
- Re-insert `DISTINCT filename` among the affected set that still has lines.

Net effect: new episodes appear in the scope list (no duplicates); an episode
whose VTT vanished drops out. Search never observes a half-updated state because
`lines` and `scopes` for the affected files are updated under one lock.

### Parsing happens off-lock

VTT parsing (the slow part) reuses the existing multiprocessing workers to parse
the **matched subset** into rows **before** taking the lock. Only the quick
`DELETE`+`INSERT`+scopes reconcile runs under the lock, so live search is not
blocked for the duration of parsing.

## Endpoint (backward compatible)

Extend the existing `Reindex` pydantic model and `POST /uttale/Reindex`:

```
POST /uttale/Reindex
  { "days": 30 }            # episodes from the last 30 days (incremental)
  { "since": "20260101" }   # episodes on/after this date (incremental)
  { "pattern": "homsen" }   # existing filename filter (full rebuild of subset)
  { }                       # no window -> full rebuild (today's behavior, fixed)
```

Rules:

- `days` and `since` are optional. If both are present, **`days` wins**.
- `pattern` (existing) **composes** with the date filter: when a window is
  present, a file must match *both* the window and the pattern.
- **Mode selection:** when a window (`days`/`since`) is present, the engine uses
  **per-file replace** (incremental, scoped DELETE). When **absent**, it uses the
  **full rebuild** (current whole-table behavior), preserving existing semantics
  exactly — so a bare `{}` or a `{ "pattern": ... }` call behaves as before
  (now that `process_vtt` is fixed).
- Response (returned immediately):
  `{ "status": "...", "since": "<YYYYMMDD or empty>", "matched": <int> }`.
  `status` ∈ `"started"`, `"already running"`, `"nothing matched"`.

**Discovery is synchronous, parsing/writing is threaded.** The `fd` scan plus
date/pattern filtering over ~36k relative paths is a millisecond-scale string
operation, so the request handler runs it inline. This lets the immediate
response carry a real `matched` count and return `"nothing matched"` (with
`matched: 0`) when the window selects no files — *before* spawning anything. Only
when `matched > 0` does the handler spawn the worker thread (parse + DB write) and
return `"started"`. The worker re-uses the already-computed file list.

## Concurrency

Mirror the `GenerateTopics` dedup pattern (`server.py:370-371, 408-428`):

- Module globals: `_reindex_lock = threading.Lock()` and a `_reindex_running`
  flag (bool, guarded by the lock).
- On request, after synchronous discovery yields a non-empty match: under the
  lock, if `_reindex_running` is set → return `"already running"` (no second
  run). Otherwise set the flag, spawn a daemon `threading.Thread`, and return
  `"started"` immediately. (The `"already running"` check happens *after*
  discovery so a no-op window still reports `"nothing matched"` rather than
  masking it.)
- The worker parses the matched files, then performs the DB write section guarded
  by `_reindex_lock`; it clears `_reindex_running` in a `finally`.
- Reads (search) use the same single global `db_duckdb` connection; worst case a
  search momentarily waits behind an in-flight write batch.

This also serializes against the full-rebuild path, so the two modes cannot
clobber each other.

## Observability

- Synchronous status string in the response (`started` / `already running` /
  `nothing matched`).
- Per-run detail (matched count, parse failures, elapsed) is appended to a log
  file under `/tmp/srst-reindex/<timestamp>.log`, mirroring topics-generation
  logging, so a run can be inspected without a progress endpoint.
- A full job-status / progress endpoint is **out of scope** (YAGNI).

## Explicit non-goals

- A window reindex **never removes** rows for episodes that fell *out* of the
  window — it only touches files matching the window. This is the correct, safe
  behavior for "add recent podcasts"; removing stale data remains the job of a
  full rebuild.
- No mtime-based selection (episode-date only, per the decision above).
- No second DuckDB connection — DuckDB holds an exclusive file lock; all writes
  go through the existing in-process `db_duckdb` handle.

## Testing (unittest, no pytest — per AGENTS.md)

In `uttale/backend/test_server.py`, using the existing temp-dir fixture style:

- **Date helpers:** `episode_date_of` (with/without an 8-digit segment),
  `since_from_days` (deterministic with an injected `today`), `within_window`
  (in-window, out-of-window, undated path).
- **Idempotency:** build a temp `48k/Pod/<date>/by10m/x.vtt`, point
  `server.args.root`/`server.args.db` at temp paths, run the incremental reindex
  twice, assert the `lines` row count is identical after the 2nd run (no
  duplication).
- **Edit refresh:** modify the VTT between runs, assert rows reflect the new
  content (count/text changes), not a mix of old+new.
- **Scopes reconcile:** assert a newly indexed filename appears in `scopes`
  exactly once.
- **`process_vtt` regression:** parse a temp VTT and assert the returned tuples
  `(filename, start, end_time, text)`.
- New code must add **zero** new ruff issues. Verify via the uv test env
  (`uv venv` + duckdb/polars/uvicorn/webvtt-py/fastapi/pydantic/tqdm/httpx) and
  `make test`. Pre-existing ruff issues are out of scope.

## Files touched

- `uttale/backend/server.py`: restore `process_vtt`; add `episode_date_of` /
  `since_from_days` / `within_window`; add the per-file replace + scopes-reconcile
  write path; add `_reindex_lock` / `_reindex_running`; extend the `Reindex`
  model and `POST /uttale/Reindex` (mode selection + threaded run + status).
- `uttale/backend/test_server.py`: the tests above.
- `docs/specs/2026-06-28-incremental-reindex-design.md`: this document.
