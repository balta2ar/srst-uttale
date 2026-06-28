# Query-driven reindex ("reindex what I searched")

Date: 2026-06-28
Status: approved (design)

## Problem

The search database (`lines` + `scopes` in DuckDB) is populated by `reindex()`,
which **wipes the whole table** (`DELETE FROM lines` / `DELETE FROM scopes`) and
rebuilds everything matched by the current `fd` scan. There is no way to add only
some podcasts incrementally, and a pattern-filtered reindex is dangerous: it still
wipes the whole `lines` table but only re-inserts the filtered subset, destroying
every other podcast's rows.

In practice the user finds gaps from the **Find tab**: searching a filename
pattern such as `"idioti 202606"` lists indexed episodes via `/uttale/Scopes`. If
the user expects 3 episodes but sees 1, two are unindexed. We want a **Reindex**
button next to the search results that re-uses **that same query string** to index
exactly those podcasts (one podcast + one month), **idempotently** (re-running
never duplicates rows), **live over HTTP** (no service restart).

The selector is a **filename/path pattern**, never transcript text and never
filesystem mtime (stat-ing the whole tree is slow). `/uttale/Search` (full-text)
is irrelevant to this feature; the relevant matcher is the same one `/uttale/Scopes`
uses.

## Prerequisite bug fix

`process_vtt(vtt, root)` — the VTT→rows parser — is **broken in the committed
tree** (HEAD `2fe6152`). Commit `8abf473` deleted its `def` line, leaving an
orphaned function body at `server.py:432-442`; the name is undefined and the call
site at `server.py:450` raises `NameError` on *any* reindex. The deployed server
is an editable install of this exact tree, so reindex is currently non-functional
in production.

Restore the signature above the orphaned body:

```python
def process_vtt(vtt: str, root: str) -> List[tuple]:
```

This un-breaks reindex and is a prerequisite for the new path.

## The query is the selector

The Find-tab box already holds a filename/scope pattern (e.g. `"idioti 202606"`,
`"Marianne 20210316"`, `"2026"`) and sends it to `/uttale/Scopes`, which matches
with `q.replace(" ", "%")` → `LIKE %idioti%202606%` (case-insensitive, tokens in
order, substring on the path). The Reindex button reuses the **same string** as
the `pattern`.

A bare/empty pattern is **rejected over HTTP** (`status: "no pattern"`). A full
corpus rebuild remains a **CLI-only** operation (`--reindex` with no pattern); it
is never reachable from the HTTP endpoint, removing the whole-table-wipe footgun
from the network surface.

## CLI (preserved)

The existing `--reindex` flag (`server.py:889-897`) is kept and now shares the
same matcher and write logic as the HTTP path:

- `--reindex` (no arg, `const=""`) → **full rebuild**: whole-table wipe + reindex
  the entire corpus (this is the only path that clears rows for files deleted from
  disk). **No limit.**
- `--reindex PATTERN` (e.g. `--reindex 2026`, `--reindex '202510 kontakt'`) →
  **filtered, per-file replace** (safe; only matched files' rows are
  deleted+reinserted, everything else untouched). **No limit.**

So the CLI can still reindex everything *and* filter, unbounded; the HTTP endpoint
is the limited, pattern-required surface.

## One write rule everywhere

The mode is chosen by **whether a pattern is present**, identically for CLI and
HTTP:

- **Non-empty pattern → per-file replace** (scoped `DELETE WHERE filename=?` +
  `INSERT`; non-destructive to unmatched files).
- **Empty pattern → full rebuild** (whole-table `DELETE FROM lines/scopes` +
  reinsert everything; CLI-only, since HTTP rejects empty).

The discovery matcher is the same in both surfaces (see next section); only the
limit differs (CLI unbounded, HTTP default 2000).

## Pushing the filter into `fd`

`fd` is fast; the date/podcast filter is pushed **into `fd`** rather than listing
all ~36k VTTs and filtering in Python.

`fd`'s `--glob` matches the **basename only**, so a directory-level token (a month
like `202606`) does not match via glob (verified: `--glob --full-path
'*idioti*202606*'` → 0 results). Instead use **`--full-path` with a regex** built
from the query tokens:

- Split the pattern on whitespace into tokens.
- `re.escape` each token (so `.`, `+`, etc. in a query can't break or distort the
  regex), then join with `.*`, case-insensitive.
- e.g. `"idioti 202606"` → `(?i)idioti.*202606`.

This mirrors `/uttale/Scopes` semantics exactly (same tokens, same order,
case-insensitive substring), so the reindex match set is the same kind of set the
search showed — except `fd` reads the **live filesystem**, surfacing the
not-yet-indexed files (verified: `fd` found 25 `idioti 202606` VTTs on disk while
`scopes` had 17 — the 8 extra are precisely what a reindex would add).

New helper `pattern_to_fd_regex(pattern: str) -> str` (pure, unit-testable)
performs the token split / escape / join. Discovery is wrapped in a shared helper
`discover_vtts(root, pattern, limit) -> list[str]` used by **both** the CLI and
HTTP paths, so the matcher is identical. This **replaces** the legacy
`pattern_to_wildcard` + Python `fnmatch` filtering (`server.py:473-498`); the old
list-everything-then-`fnmatch` approach is removed.

The `fd` invocation:

```
fd --type f --extension vtt --base-directory <root> --full-path <regex> [--max-results <limit>]
```

When `pattern` is empty (CLI full rebuild), the regex is empty/omitted so `fd`
lists every VTT. `--max-results` is included only when `limit` is set.

### Candidate limit

`--max-results <limit>` bounds the candidate set (the "good limit" requested) and
applies to the **HTTP path only**: default **2000**, overridable via a `limit`
field in the POST body. A targeted podcast+month query matches a handful of files;
the limit is a guardrail against an over-broad query (e.g. `"2026"` matching
thousands). When `fd` returns exactly `limit` results the response flags it
(`truncated: true`) so the caller knows the set was capped.

The **CLI is unbounded** (`limit=None` → no `--max-results`), so `--reindex` (full
or filtered) always processes every match.

## Idempotent per-file replace (core)

This is the write path for a **non-empty pattern** (all HTTP calls, filtered CLI).
After `fd` yields the matched relative paths and the workers parse them into rows,
under the write lock, for each affected `filename`:

1. `DELETE FROM lines WHERE filename = ?`
2. `INSERT` that file's freshly-parsed rows (`filename, start, end_time, text`).

This guarantees:

- **No duplicates** on re-run — a file's old rows are removed before re-insert,
  even though `lines` has no primary key.
- **Edits are picked up** — a re-transcribed VTT refreshes its rows.
- Files **not matched by the query are never touched**.

The **empty-pattern full rebuild** (CLI-only) keeps the existing whole-table path:
`DELETE FROM lines` / `DELETE FROM scopes` then reinsert everything `fd` returned
(unbounded). Both paths share the same parse step; they differ only in the
delete scope.

### Scopes reconciliation

`scopes` is the distinct-filename list backing the scope dropdown. In the same
locked section, for the affected filenames: delete their `scopes` rows, then
re-insert `DISTINCT filename` among them that still has lines. New episodes appear
in the scope list (no duplicates); a vanished VTT drops out. Search never observes
a half-updated state because `lines` and `scopes` for the affected files update
under one lock.

### Parsing happens off-lock

VTT parsing (the slow part) reuses the existing multiprocessing workers to parse
the matched subset **before** taking the lock. Only the quick
`DELETE`+`INSERT`+scopes-reconcile runs under the lock, so live search is not
blocked while files are parsed.

## Endpoint (extends the existing one)

Extend the existing `Reindex` pydantic model and `POST /uttale/Reindex`:

```
POST /uttale/Reindex
  { "pattern": "idioti 202606" }            # reindex matches of this query
  { "pattern": "idioti 202606", "limit": 500 }
  { "pattern": "" } / { }                   # rejected -> status "no pattern"
```

Rules:

- `pattern` is **required** (non-empty after trim); empty → immediate
  `status: "no pattern"`, no work.
- `limit` optional, default 2000.
- **Discovery is synchronous, parsing/writing is threaded.** The `fd` scan is
  millisecond-scale, so the handler runs it inline and the immediate response
  carries a real `matched` count: `0` → `status: "nothing matched"`; otherwise
  spawn the worker thread and return `status: "started"`. The worker re-uses the
  already-computed file list.
- Response (returned immediately):
  `{ "pattern": "...", "status": "...", "matched": <int>, "limit": <int>,
     "truncated": <bool> }`.
  `status` ∈ `"no pattern"`, `"nothing matched"`, `"started"`, `"already running"`.

## Concurrency

Mirror the `GenerateTopics` dedup pattern (`server.py:370-371, 408-428`):

- Module globals: `_reindex_lock = threading.Lock()` and a `_reindex_running`
  flag (bool, guarded by the lock).
- On request, after synchronous discovery yields a non-empty match: under the
  lock, if `_reindex_running` is set → return `"already running"`. Otherwise set
  the flag, spawn a daemon `threading.Thread`, return `"started"`. (The
  already-running check happens *after* discovery so a no-op query still reports
  `"nothing matched"`.)
- The worker parses the matched files, performs the DB write section guarded by
  `_reindex_lock`, and clears `_reindex_running` in a `finally`.
- Reads (search) use the same single global `db_duckdb` connection; worst case a
  search momentarily waits behind an in-flight write batch.

## Observability

- Synchronous status string in the response.
- Per-run detail (pattern, matched count, parse failures, elapsed) is appended to
  a log file under `/tmp/srst-reindex/<timestamp>.log`, mirroring
  topics-generation logging.
- No progress/job-status endpoint (YAGNI).

## UI (offline PWA — separate harken repo, follow-up work)

Recorded here for context; the backend is usable independently.

- The Find tab `search()` already renders results from `Api.scopes(query)` into
  `resultsBox` (`offline/static/app.js`). Add a **Reindex** button in the results
  header (next to "Search results") that, when online, POSTs the current box value
  as `pattern`.
- New `Api.reindex(pattern)` → `POST /api/reindex`; new `offline.py` route
  `/api/reindex` → `_proxy_post("/uttale/Reindex", raw, ...)` (copy the
  `/api/topics` → `GenerateTopics` pattern; add `"/api/reindex"` to the POST
  allow-list).
- Gate the button on `navigator.onLine` (mirror `renderTopicsEmpty`), show
  progress text ("Reindexing… re-search shortly"), and re-run the search after a
  short delay so the new episodes appear.

## Explicit non-goals

- **No date math / no mtime.** Selection is the query string only, matched against
  the path. ("last 30 days" is expressed by the user as a date token in the query,
  e.g. `"202606"`, not a server-side date window.)
- A query reindex **never removes** rows for files the query doesn't match —
  removing stale data remains a full-rebuild (CLI) concern.
- **No second DuckDB connection** — DuckDB holds an exclusive file lock; all
  writes go through the existing in-process `db_duckdb` handle.
- The HTTP endpoint **cannot** trigger a whole-corpus rebuild (empty pattern is
  rejected); that stays CLI-only.

## Testing (unittest, no pytest — per AGENTS.md)

In `uttale/backend/test_server.py`, using the existing temp-dir fixture style:

- **`pattern_to_fd_regex`:** single token, multiple tokens (order preserved),
  regex-special chars escaped (`c++`, `a.b`), empty/whitespace → empty.
- **`process_vtt` regression:** parse a temp VTT, assert the returned tuples
  `(filename, start, end_time, text)`.
- **`discover_vtts`:** in a temp tree, a pattern returns only matching relative
  paths; an empty pattern returns all; a `limit` caps the count.
- **Idempotency (per-file replace):** build a temp `48k/Pod/<date>/by10m/x.vtt`,
  point `server.args.root`/`server.args.db` at temp paths, run a *pattern* reindex
  twice, assert `lines` row count is identical after the 2nd run (no duplication).
- **Edit refresh:** modify the VTT between runs, assert rows reflect the new
  content, not a mix of old+new.
- **Scopes reconcile:** assert a newly indexed filename appears in `scopes`
  exactly once, and an unrelated existing scope row is untouched.
- **Scoped (non-destructive) write:** seed `lines`/`scopes` with an unrelated
  podcast, run a *pattern* reindex that doesn't match it, assert its rows survive
  (guards against the old whole-table wipe).
- **CLI full rebuild (empty pattern):** seed an unrelated/stale `lines` row whose
  file is absent from the temp tree, run the empty-pattern rebuild, assert the
  stale row is gone and the on-disk files are present (whole-table rebuild
  semantics, unbounded).
- **HTTP empty pattern rejected:** assert the endpoint returns
  `status == "no pattern"` and makes no DB change (distinct from the CLI rebuild).
- New code must add **zero** new ruff issues. Verify via the uv test env
  (`uv venv` + duckdb/polars/uvicorn/webvtt-py/fastapi/pydantic/tqdm/httpx) and
  `make test`. Pre-existing ruff issues are out of scope.

## Files touched

- `uttale/backend/server.py`: restore `process_vtt`; add `pattern_to_fd_regex`
  and the shared `discover_vtts(root, pattern, limit)` (`fd --full-path`),
  replacing the legacy `pattern_to_wildcard` + `fnmatch` path; add the per-file
  replace + scopes-reconcile write path (non-empty pattern) alongside the
  empty-pattern whole-table rebuild; add `_reindex_lock` / `_reindex_running`;
  wire the CLI `--reindex` (unbounded) and extend the `Reindex` model +
  `POST /uttale/Reindex` (require pattern, `limit` default 2000, synchronous
  discovery + threaded run + status/matched/truncated).
- `uttale/backend/test_server.py`: the tests above.
- `docs/specs/2026-06-28-incremental-reindex-design.md`: this document.
- (Follow-up, harken repo) `offline/static/{app.js,api.js}`, `offline/offline.py`:
  the Reindex button + `/api/reindex` proxy.
