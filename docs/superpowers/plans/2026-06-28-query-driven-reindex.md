# Query-driven reindex — backend implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a filename/path query (e.g. `"idioti 202606"`) reindex exactly the podcasts it matches — idempotently (no duplicate rows), live over HTTP (no restart) — while keeping the CLI able to reindex everything or a filtered subset, unbounded.

**Architecture:** A user-supplied pattern is translated to an `fd --full-path` regex and discovered against the live filesystem (`discover_vtts`). A **non-empty** pattern writes via **per-file replace** (`DELETE FROM lines WHERE filename=?` + `INSERT`, plus scoped `scopes` reconcile) so unmatched podcasts are never touched; an **empty** pattern (CLI-only) keeps the existing whole-table rebuild. The HTTP endpoint requires a pattern, caps candidates with `fd --max-results` (default 2000), discovers synchronously, then parses+writes in a daemon thread guarded by a module lock (mirroring the `GenerateTopics` dedup pattern).

**Tech Stack:** Python 3.12, FastAPI, pydantic, DuckDB, polars, webvtt-py, `fd` (external binary), stdlib `unittest`.

## Global Constraints

- Repo: `/mnt/payload/share/msi/prg/srst-uttale`. Edit `uttale/backend/server.py` and `uttale/backend/test_server.py` only (plus this plan/spec). Commit to **master**; stage only the named files (never `git add -A`).
- Style (`STYLE.md` / `AGENTS.md`): **no comments** unless they explain *why*; compact but readable; **all imports at top of file** (no local imports); short imports; extract external calls into helpers.
- Testing: **no pytest**. Use stdlib `unittest` in `test_server.py`. Run the suite with the uv test env python: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v` (run from the repo root). `fd`, `ffmpeg`, `ffprobe` are on PATH in that env.
- New code must add **ZERO** new ruff issues. Pre-existing ruff issues (`sys`/`Dict`/`Path` unused, bare `except`) are out of scope — do not "fix" them. If `ruff` is not installed in the uv env: `uv pip install --python /tmp/opencode/uttale-test/bin/python ruff`, then run `/tmp/opencode/uttale-test/bin/ruff check uttale/backend/server.py` (or a system `ruff`).
- Verify syntax with `/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py` after edits.
- The live server on :7010 is an editable install of this tree. **Do NOT start, stop, or restart it.** Its DuckDB `~/.cache/srst-uttale/root.db` is **locked** — never open it. All tests use temp DBs/trees under `/tmp/opencode`.
- Audio/VTT path layout: `48k/<podcast>/<YYYYMMDD>/by10m/<file>.vtt`; `filename` (a "line"ّs key) is the path **relative to `--root`**.
- Module globals in `server.py`: `db_duckdb` (the shared DuckDB connection, set by `init_database()`), `args` (the parsed argparse namespace; tests set it to a `SimpleNamespace`).

---

## Pre-flight: ensure the uv test env exists

If `/tmp/opencode/uttale-test/bin/python` is missing, create it once (the implementer of Task 1 does this before running tests):

```bash
cd /tmp/opencode
uv venv uttale-test
/tmp/opencode/uttale-test/bin/python -m ensurepip >/dev/null 2>&1 || true
uv pip install --python /tmp/opencode/uttale-test/bin/python duckdb polars pyarrow uvicorn webvtt-py fastapi pydantic tqdm httpx
```

Confirm `fd --version` prints (it is required by `discover_vtts`). `pyarrow` is required because `reindex` registers a polars DataFrame into DuckDB (`db_duckdb.register("df", df)`), which converts via Arrow — without it the reindex tests raise `ModuleNotFoundError: No module named 'pyarrow'`.

---

## File structure

- `uttale/backend/server.py` — all production changes (helpers, write path, model, endpoint, CLI wiring). One file by existing convention.
- `uttale/backend/test_server.py` — all new tests, appended as new `unittest.TestCase` classes; extend the import block as needed.

---

### Task 1: Restore `process_vtt` (prerequisite bug fix)

The `def` line for `process_vtt` was deleted in commit `8abf473`, leaving an orphaned body at `server.py:432-442`; `reindex()` calls it at `server.py:450` and currently raises `NameError`. Restore the signature.

**Files:**
- Modify: `uttale/backend/server.py` (around line 431-432)
- Test: `uttale/backend/test_server.py` (new class `TestProcessVtt`)

**Interfaces:**
- Produces: `process_vtt(vtt: str, root: str) -> List[tuple]` — parses a VTT (path relative to `root`) and returns `(filename, start, end_time, text)` tuples; `[]` if the file is missing or unreadable.

- [ ] **Step 1: Write the failing test**

Append to `uttale/backend/test_server.py`:

```python
class TestProcessVtt(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.rel = os.path.join('48k', 'Pod', '20260623', 'by10m', 'by10m_00.vtt')
        self.abs = os.path.join(self.root, self.rel)
        os.makedirs(os.path.dirname(self.abs))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_vtt(self, body):
        with open(self.abs, 'w', encoding='utf-8') as f:
            f.write(body)

    def test_parses_captions_to_tuples(self):
        self.write_vtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhei der\n\n"
            "00:00:01.000 --> 00:00:02.500\nandre linje\n"
        )
        rows = server.process_vtt(self.rel, self.root)
        self.assertEqual(rows[0], (self.rel, '00:00:00.000', '00:00:01.000', 'hei der'))
        self.assertEqual(rows[1][3], 'andre linje')

    def test_missing_file_returns_empty(self):
        rows = server.process_vtt(os.path.join('48k', 'X', '20200101', 'a', 'b.vtt'), self.root)
        self.assertEqual(rows, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestProcessVtt -v`
Expected: FAIL — `AttributeError: module 'uttale.backend.server' has no attribute 'process_vtt'` (the name is undefined).

- [ ] **Step 3: Restore the signature**

In `uttale/backend/server.py`, the orphaned body currently looks like:

```python
    return "started"



    abs_vtt = join(root, vtt)
```

Insert the `def` line so it reads:

```python
    return "started"


def process_vtt(vtt: str, root: str) -> List[tuple]:
    abs_vtt = join(root, vtt)
    rel_vtt = relpath(abs_vtt, root)
    if not exists(abs_vtt):
        return []
    try:
        captions = []
        for c in webvtt.read(abs_vtt):
            captions.append((rel_vtt, c.start, c.end, c.text))
        return captions
    except:
        return []
```

(Only the `def process_vtt(vtt: str, root: str) -> List[tuple]:` line and the blank-line normalization are new; the body already exists.)

- [ ] **Step 4: Run test to verify it passes**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestProcessVtt -v`
Expected: PASS (2 tests).

Also: `/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py` (no output = ok).

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: restore process_vtt def (un-break reindex)"
```

---

### Task 2: `pattern_to_fd_regex` helper

Translate a space-separated pattern into a case-insensitive `fd --full-path` regex matching the tokens in order, with each token `re.escape`d.

**Files:**
- Modify: `uttale/backend/server.py` (add helper near `pattern_to_wildcard`, ~line 482)
- Test: `uttale/backend/test_server.py` (new class `TestPatternToFdRegex`)

**Interfaces:**
- Produces: `pattern_to_fd_regex(pattern: str) -> str` — returns `(?i)tok1.*tok2` with tokens `re.escape`d; returns `""` for empty/whitespace-only input.

- [ ] **Step 1: Write the failing test**

Append to `test_server.py`:

```python
class TestPatternToFdRegex(unittest.TestCase):
    def test_single_token(self):
        self.assertEqual(server.pattern_to_fd_regex('idioti'), '(?i)idioti')

    def test_multiple_tokens_in_order(self):
        self.assertEqual(server.pattern_to_fd_regex('idioti 202606'), '(?i)idioti.*202606')

    def test_escapes_regex_special_chars(self):
        self.assertEqual(server.pattern_to_fd_regex('c++ a.b'), r'(?i)c\+\+.*a\.b')

    def test_empty_and_whitespace_return_empty(self):
        self.assertEqual(server.pattern_to_fd_regex(''), '')
        self.assertEqual(server.pattern_to_fd_regex('   '), '')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestPatternToFdRegex -v`
Expected: FAIL — `AttributeError: ... has no attribute 'pattern_to_fd_regex'`.

- [ ] **Step 3: Implement the helper**

In `server.py`, immediately after `pattern_to_wildcard` (ends ~line 481), add:

```python
def pattern_to_fd_regex(pattern: str) -> str:
    parts = pattern.strip().split()
    if not parts:
        return ""
    return "(?i)" + ".*".join(re.escape(p) for p in parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestPatternToFdRegex -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: add pattern_to_fd_regex (query -> fd full-path regex)"
```

---

### Task 3: `discover_vtts` (fd discovery, replaces fnmatch)

Push the filter into `fd`. An empty pattern lists every VTT; a non-empty pattern filters via the regex; `limit` (when set) caps with `--max-results`.

**Files:**
- Modify: `uttale/backend/server.py` (add `discover_vtts`; remove the `fnmatch` block from `reindex` in Task 4)
- Test: `uttale/backend/test_server.py` (new class `TestDiscoverVtts`)

**Interfaces:**
- Consumes: `pattern_to_fd_regex` (Task 2).
- Produces: `discover_vtts(root: str, pattern: str = "", limit=None) -> list[str]` — relative VTT paths under `root` matching `pattern` (all if empty), at most `limit` if `limit` is not None. Returns `[]` if `fd` fails.

- [ ] **Step 1: Write the failing test**

Append to `test_server.py`:

```python
class TestDiscoverVtts(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.made = []
        for rel in [
            os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
            os.path.join('48k', 'idioti', '20260601', 'by10m', 'b.vtt'),
            os.path.join('48k', 'kontakt', '20260515', 'by10m', 'c.vtt'),
        ]:
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write('WEBVTT\n')
            self.made.append(rel)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_pattern_lists_all(self):
        found = server.discover_vtts(self.root, '')
        self.assertEqual(sorted(found), sorted(self.made))

    def test_pattern_filters(self):
        found = server.discover_vtts(self.root, 'idioti 202606')
        self.assertEqual(sorted(found),
                         sorted(m for m in self.made if 'idioti' in m))

    def test_limit_caps_count(self):
        found = server.discover_vtts(self.root, 'idioti', limit=1)
        self.assertEqual(len(found), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestDiscoverVtts -v`
Expected: FAIL — `AttributeError: ... has no attribute 'discover_vtts'`.

- [ ] **Step 3: Implement `discover_vtts`**

In `server.py`, add immediately after `pattern_to_fd_regex` (Task 2):

```python
def discover_vtts(root: str, pattern: str = "", limit=None) -> list:
    cmd = ["fd", "--type", "f", "--extension", "vtt", "--base-directory", root]
    if limit is not None:
        cmd += ["--max-results", str(limit)]
    regex = pattern_to_fd_regex(pattern)
    if regex:
        cmd += ["--full-path", regex]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout.splitlines()
    except:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestDiscoverVtts -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: add discover_vtts (fd push-down discovery)"
```

---

### Task 4: Per-file-replace write path in `reindex`

Refactor `reindex` to use `discover_vtts`, add a `limit` param, and branch the DB write: non-empty pattern → per-file replace (scoped delete + scoped scopes reconcile); empty pattern → existing whole-table rebuild. Keep the multiprocessing parse exactly as-is.

**Files:**
- Modify: `uttale/backend/server.py` — `reindex` (lines 484-542)
- Test: `uttale/backend/test_server.py` (new class `TestReindexWrite`)

**Interfaces:**
- Consumes: `discover_vtts` (Task 3), `process_vtt` (Task 1), module globals `db_duckdb`, `args`.
- Produces: `reindex(root: str, pattern: str = "", limit=None) -> int` — runs the reindex and **returns the number of files processed** (0 if none). Existing callers that ignore the return value still work.

- [ ] **Step 1: Write the failing tests**

Append to `test_server.py`. This fixture sets up a temp DuckDB via `init_database()` and a temp tree.

```python
class TestReindexWrite(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dbfile = os.path.join(tempfile.mkdtemp(), 'lines.db')
        self._saved_args = server.args
        self._saved_db = server.db_duckdb
        server.args = SimpleNamespace(db=self.dbfile, root=self.root)
        server.init_database()

    def tearDown(self):
        try:
            server.db_duckdb.close()
        except Exception:
            pass
        server.args = self._saved_args
        server.db_duckdb = self._saved_db
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.dbfile), ignore_errors=True)

    def make_vtt(self, rel, lines):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        body = "WEBVTT\n\n"
        t = 0
        for text in lines:
            body += f"00:00:0{t}.000 --> 00:00:0{t+1}.000\n{text}\n\n"
            t += 1
        with open(p, 'w', encoding='utf-8') as f:
            f.write(body)
        return rel

    def line_count(self):
        return server.db_duckdb.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    def scopes_for(self, like):
        return server.db_duckdb.execute(
            "SELECT scope FROM scopes WHERE scope LIKE ?", (like,)).fetchall()

    def test_pattern_reindex_is_idempotent(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
                      ['one', 'two', 'three'])
        n1 = server.reindex(self.root, 'idioti')
        c1 = self.line_count()
        n2 = server.reindex(self.root, 'idioti')
        c2 = self.line_count()
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)
        self.assertEqual(c1, 3)
        self.assertEqual(c2, 3)

    def test_pattern_reindex_picks_up_edits(self):
        rel = self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
                            ['one', 'two'])
        server.reindex(self.root, 'idioti')
        self.assertEqual(self.line_count(), 2)
        self.make_vtt(rel, ['one', 'two', 'three', 'four'])
        server.reindex(self.root, 'idioti')
        self.assertEqual(self.line_count(), 4)

    def test_pattern_reindex_does_not_touch_unmatched(self):
        server.db_duckdb.execute(
            "INSERT INTO lines VALUES ('48k/other/20200101/by10m/z.vtt','00:00:00.000','00:00:01.000','keep')")
        server.db_duckdb.execute("INSERT INTO scopes VALUES ('48k/other/20200101/by10m/z.vtt')")
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'), ['x'])
        server.reindex(self.root, 'idioti')
        kept = server.db_duckdb.execute(
            "SELECT COUNT(*) FROM lines WHERE filename = '48k/other/20200101/by10m/z.vtt'").fetchone()[0]
        self.assertEqual(kept, 1)
        self.assertEqual(len(self.scopes_for('%idioti%')), 1)

    def test_full_rebuild_clears_stale_rows(self):
        server.db_duckdb.execute(
            "INSERT INTO lines VALUES ('48k/gone/20200101/by10m/z.vtt','00:00:00.000','00:00:01.000','stale')")
        server.db_duckdb.execute("INSERT INTO scopes VALUES ('48k/gone/20200101/by10m/z.vtt')")
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'), ['x'])
        server.reindex(self.root, '')
        gone = server.db_duckdb.execute(
            "SELECT COUNT(*) FROM lines WHERE filename = '48k/gone/20200101/by10m/z.vtt'").fetchone()[0]
        self.assertEqual(gone, 0)
        self.assertEqual(self.line_count(), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestReindexWrite -v`
Expected: FAIL — the idempotency/scoped tests fail because the current `reindex` does `DELETE FROM lines` (whole table) and `INSERT` without scoping, and returns `None` (so `n1 == 1` fails). (The current code wipes everything then inserts only matched rows, so `test_pattern_reindex_does_not_touch_unmatched` fails.)

- [ ] **Step 3: Rewrite `reindex`**

Replace the entire `reindex` function (`server.py:484-542`) with:

```python
def reindex(root: str, pattern: str = "", limit=None) -> int:
    vtt_files = discover_vtts(root, pattern, limit)
    total_files = len(vtt_files)
    if not vtt_files:
        return 0
    manager = mp.Manager()
    return_dict = manager.dict()
    counter = manager.Value("i", 0)
    lock = manager.Lock()
    num_processes = min(mp.cpu_count(), 8)
    chunk_size = (total_files + num_processes - 1) // num_processes
    chunks = [vtt_files[i : i + chunk_size] for i in range(0, total_files, chunk_size)]
    jobs = []
    for idx, chunk in enumerate(chunks):
        p = mp.Process(
            target=reindex_worker_duckdb,
            args=(chunk, root, return_dict, idx, counter, lock),
        )
        jobs.append(p)
        p.start()
    stop_event = threading.Event()
    progress_thread = threading.Thread(
        target=update_progress,
        args=(total_files, counter, lock, stop_event, "Reindexing DuckDB"),
    )
    progress_thread.start()
    for p in jobs:
        p.join()
    stop_event.set()
    progress_thread.join()
    all_rows = []
    for idx in range(len(chunks)):
        all_rows.extend(return_dict.get(idx, []))
    df = pl.DataFrame(all_rows, schema=["filename", "start", "end_time", "text"])
    db_duckdb.register("df", df)
    if pattern:
        db_duckdb.execute(
            "DELETE FROM lines WHERE filename IN (SELECT DISTINCT filename FROM df)"
        )
        db_duckdb.execute(
            "INSERT INTO lines SELECT filename, start, end_time, text FROM df"
        )
        db_duckdb.execute(
            "DELETE FROM scopes WHERE scope IN (SELECT DISTINCT filename FROM df)"
        )
        db_duckdb.execute(
            "INSERT INTO scopes SELECT DISTINCT filename FROM df WHERE filename IN (SELECT DISTINCT filename FROM lines)"
        )
    else:
        db_duckdb.execute("DELETE FROM lines")
        db_duckdb.execute(
            "INSERT INTO lines SELECT filename, start, end_time, text FROM df"
        )
        db_duckdb.execute("DELETE FROM scopes")
        db_duckdb.execute(
            "INSERT INTO scopes SELECT DISTINCT filename AS scope FROM lines ORDER BY scope"
        )
    db_duckdb.unregister("df")
    db_duckdb.commit()
    return total_files
```

Notes for the implementer:
- `discover_vtts` (Task 3) replaces the old inline `fd` call + `fnmatch` block — the `fnmatch` import may now be unused in `server.py`; **leave the `import fnmatch` line as-is** (removing it is out of scope and `fnmatch` is still imported by the test file independently; do not touch unrelated imports). [If ruff flags `fnmatch` as newly unused, see Task 4 Step 4 note.]
- The per-file replace deletes by the set of filenames present in the freshly-parsed `df`, so a matched file with zero captions (empty `df` subset) won't delete anything for it — acceptable (an unreadable/empty VTT keeps its prior rows). Matched files that parsed to rows are fully replaced.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestReindexWrite -v`
Expected: PASS (4 tests).

Then run the **whole** suite and a ruff check:

```bash
/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v
ruff check uttale/backend/server.py
```

Expected: all tests pass. For ruff: compare against the pre-existing baseline — if `fnmatch` now shows as `F401 imported but unused` and it was NOT in the baseline, remove **only** the `import fnmatch` line from `server.py` (line 2) to keep "zero new issues", then re-run `py_compile` and the suite. Do not touch other flagged lines.

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: per-file-replace reindex (idempotent, non-destructive)"
```

---

### Task 5: HTTP endpoint — require pattern, limit, threaded run, dedup

Extend the `Reindex` model and `POST /uttale/Reindex`: require a non-empty pattern, accept `limit` (default 2000), discover synchronously to report `matched`/`truncated`, and run parse+write in a daemon thread guarded by a module lock. Wire the CLI to pass `limit=None` (unbounded).

**Files:**
- Modify: `uttale/backend/server.py` — `Reindex` model (lines 53-55); add module globals + a runner; replace `trigger_reindex` (lines 732-738); CLI call (line 904).
- Test: `uttale/backend/test_server.py` (new class `TestReindexEndpoint`)

**Interfaces:**
- Consumes: `discover_vtts` (Task 3), `reindex` (Task 4).
- Produces:
  - `Reindex` model fields: `pattern: str = ""`, `status: str = ""`, `limit: int = 2000`, `matched: int = 0`, `truncated: bool = False`.
  - `REINDEX_LIMIT = 2000` (module constant).
  - `_reindex_lock` (`threading.Lock`), `_reindex_running` (bool, module global).
  - `start_reindex(root, pattern, limit) -> dict` — synchronous discovery + threaded run; returns `{"status","matched","truncated"}`. `status` ∈ `"no pattern"`, `"nothing matched"`, `"already running"`, `"started"`.

- [ ] **Step 1: Write the failing tests**

Append to `test_server.py`. These test the `start_reindex` orchestration (synchronous parts) directly; the thread is joined via a brief wait where needed.

```python
class TestReindexEndpoint(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dbfile = os.path.join(tempfile.mkdtemp(), 'lines.db')
        self._saved_args = server.args
        self._saved_db = server.db_duckdb
        server.args = SimpleNamespace(db=self.dbfile, root=self.root)
        server.init_database()
        with server._reindex_lock:
            server._reindex_running = False

    def tearDown(self):
        try:
            server.db_duckdb.close()
        except Exception:
            pass
        server.args = self._saved_args
        server.db_duckdb = self._saved_db
        with server._reindex_lock:
            server._reindex_running = False
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.dbfile), ignore_errors=True)

    def make_vtt(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n")
        return rel

    def test_empty_pattern_rejected(self):
        res = server.start_reindex(self.root, '   ', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'no pattern')
        self.assertEqual(res['matched'], 0)

    def test_nothing_matched(self):
        res = server.start_reindex(self.root, 'doesnotexist', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'nothing matched')
        self.assertEqual(res['matched'], 0)

    def test_started_reports_matched_and_runs(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'))
        res = server.start_reindex(self.root, 'idioti', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'started')
        self.assertEqual(res['matched'], 1)
        for _ in range(50):
            with server._reindex_lock:
                running = server._reindex_running
            if not running:
                break
            time.sleep(0.1)
        n = server.db_duckdb.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        self.assertEqual(n, 1)

    def test_truncated_flag_when_capped(self):
        for i in range(3):
            self.make_vtt(os.path.join('48k', 'idioti', '2026060%d' % i, 'by10m', 'a.vtt'))
        res = server.start_reindex(self.root, 'idioti', 2)
        self.assertEqual(res['matched'], 2)
        self.assertTrue(res['truncated'])

    def test_already_running_guard(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'))
        with server._reindex_lock:
            server._reindex_running = True
        res = server.start_reindex(self.root, 'idioti', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'already running')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestReindexEndpoint -v`
Expected: FAIL — `AttributeError` on `server._reindex_lock` / `server.start_reindex` / `server.REINDEX_LIMIT`.

- [ ] **Step 3a: Extend the `Reindex` model**

Replace `server.py:53-55`:

```python
class Reindex(BaseModel):
    pattern: str = ""
    status: str = ""
    limit: int = 2000
    matched: int = 0
    truncated: bool = False
```

- [ ] **Step 3b: Add module globals + `start_reindex`**

Add directly **above** the `reindex` function (just before `def reindex(` from Task 4):

```python
REINDEX_LIMIT = 2000
_reindex_lock = threading.Lock()
_reindex_running = False


def start_reindex(root: str, pattern: str, limit: int) -> dict:
    global _reindex_running
    if not pattern.strip():
        return {"status": "no pattern", "matched": 0, "truncated": False}
    vtt_files = discover_vtts(root, pattern, limit)
    matched = len(vtt_files)
    truncated = matched >= limit
    if matched == 0:
        return {"status": "nothing matched", "matched": 0, "truncated": False}
    with _reindex_lock:
        if _reindex_running:
            return {"status": "already running", "matched": matched, "truncated": truncated}
        _reindex_running = True

    def worker():
        global _reindex_running
        try:
            reindex(root, pattern, limit)
        finally:
            with _reindex_lock:
                _reindex_running = False

    threading.Thread(target=worker, daemon=True).start()
    return {"status": "started", "matched": matched, "truncated": truncated}
```

- [ ] **Step 3c: Replace the endpoint**

Replace `server.py:732-738` (`trigger_reindex`):

```python
@app.post("/uttale/Reindex", response_model=Reindex)
def trigger_reindex(request: Reindex) -> Reindex:
    """Reindex VTTs matching a filename pattern (per-file replace)"""
    limit = request.limit if request.limit and request.limit > 0 else REINDEX_LIMIT
    res = start_reindex(args.root, request.pattern, limit)
    return Reindex(
        pattern=request.pattern,
        limit=limit,
        status=res["status"],
        matched=res["matched"],
        truncated=res["truncated"],
    )
```

Note: `BackgroundTasks` is no longer used by this endpoint. The `from fastapi import BackgroundTasks, ...` line stays (it is still used by the audio `play` endpoint at `server.py:692`). Do not remove it.

- [ ] **Step 3d: CLI stays unbounded**

The CLI call at `server.py:904` is `reindex(args.root, args.reindex)`. It already omits `limit`, so it defaults to `None` (unbounded). **No change needed** — confirm the line reads:

```python
    if args.reindex is not None:
        reindex(args.root, args.reindex)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestReindexEndpoint -v`
Expected: PASS (5 tests).

Then the whole suite + syntax + ruff:

```bash
/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v
/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py
ruff check uttale/backend/server.py
```

Expected: all pass; zero new ruff issues vs baseline.

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: query-driven Reindex endpoint (require pattern, limit, dedup thread)"
```

---

### Task 6: Live smoke test (throwaway server) + final verification

Verify end-to-end against a throwaway server with a temp DB, without touching the live :7010 instance.

**Files:** none (verification only).

- [ ] **Step 1: Build a tiny temp tree + start a throwaway server**

```bash
mkdir -p /tmp/opencode/reindex-smoke/48k/idioti/20260601/by10m
printf 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhei\n' > /tmp/opencode/reindex-smoke/48k/idioti/20260601/by10m/a.vtt
mkdir -p /tmp/opencode/reindex-smoke/48k/idioti/20260601/by10m2
printf 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhalla\n' > /tmp/opencode/reindex-smoke/48k/idioti/20260601/by10m2/b.vtt
/tmp/opencode/uttale-test/bin/python -m uttale.backend.server \
  --root /tmp/opencode/reindex-smoke \
  --db /tmp/opencode/reindex-smoke/lines.db \
  --favorites-db /tmp/opencode/reindex-smoke/fav.db \
  --listens-db /tmp/opencode/reindex-smoke/listens.db \
  --iface 127.0.0.1:7011 &
echo $! > /tmp/opencode/utt.pid
sleep 2
```

(Run from the repo root so `uttale.backend.server` imports.)

- [ ] **Step 2: Verify empty Scopes, then reindex by pattern, then Scopes populated**

```bash
curl -s 'http://127.0.0.1:7011/uttale/Scopes?q=idioti'
echo
curl -s -X POST 'http://127.0.0.1:7011/uttale/Reindex' \
  -H 'Content-Type: application/json' -d '{"pattern":"idioti 202606"}'
echo
sleep 2
curl -s 'http://127.0.0.1:7011/uttale/Scopes?q=idioti'
echo
```

Expected: first Scopes `results` empty (`results_count: 0`); Reindex returns `{"pattern":"idioti 202606", ..., "status":"started","matched":2,"truncated":false}`; second Scopes lists both VTT paths (`results_count: 2`).

- [ ] **Step 3: Verify idempotency + empty-pattern rejection**

```bash
curl -s -X POST 'http://127.0.0.1:7011/uttale/Reindex' -H 'Content-Type: application/json' -d '{"pattern":"idioti"}'; echo
sleep 1
curl -s 'http://127.0.0.1:7011/uttale/Scopes?q=idioti'; echo
curl -s -X POST 'http://127.0.0.1:7011/uttale/Reindex' -H 'Content-Type: application/json' -d '{"pattern":""}'; echo
```

Expected: re-run still `results_count: 2` (no duplication); empty-pattern POST returns `"status":"no pattern"`, `"matched":0`.

- [ ] **Step 4: Stop the throwaway server (by saved PID)**

```bash
kill "$(cat /tmp/opencode/utt.pid)" && rm -f /tmp/opencode/utt.pid
```

Confirm the live server is untouched (still responds):

```bash
curl -sk https://localhost:7010/uttale/Scopes?q=idioti -o /dev/null -w '%{http_code}\n'
```

Expected: `200` (the real server, unaffected — we never restarted it; it still runs the old code until *you* restart it to pick up the new endpoint).

- [ ] **Step 5: Final full-suite + ruff gate**

```bash
/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v
ruff check uttale/backend/server.py
```

Expected: all tests pass; zero new ruff issues. No commit (verification task).

---

## Post-implementation note

The new `POST /uttale/Reindex` shape (and the fixed `process_vtt`) only take effect on the live server **after you restart it** (`srst-uttale-backend-api ... --ssl`). The plan never restarts it. The offline-PWA Reindex button + `/api/reindex` proxy (harken repo) is deferred per the spec and is a separate plan.

## Self-review checklist (completed by plan author)

- Spec coverage: prerequisite `process_vtt` (T1), `pattern_to_fd_regex` (T2), `discover_vtts`/fd push-down/limit (T3), per-file replace + scopes reconcile + empty-pattern rebuild (T4), require-pattern + threaded dedup + matched/truncated + CLI unbounded (T5), live smoke (T6). All spec sections mapped.
- Type consistency: `reindex(root, pattern="", limit=None) -> int`; `discover_vtts(root, pattern="", limit=None) -> list`; `start_reindex(root, pattern, limit) -> dict` with keys `status/matched/truncated`; `Reindex` fields `pattern/status/limit/matched/truncated` — used consistently across T4/T5 and tests.
- No placeholders: every code/edit step shows full code and exact commands.
