# Search exact-match path — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/uttale/Search` use a fast exact-match query (`WHERE filename = ? ORDER BY start`) when the request is a download (empty `q` + a `scope`), instead of the ~2s full-table `LIKE` scan.

**Architecture:** Add one branch inside the existing `search()` handler. When `q` is empty (after `.strip()`) and `scope` is non-empty, run `SELECT ... WHERE filename = ? ORDER BY start LIMIT ?` binding `scope` as the exact filename. Otherwise keep the existing `LIKE` query verbatim. Response model and row mapping unchanged.

**Tech Stack:** Python 3.12, FastAPI, DuckDB, pydantic; stdlib `unittest`.

## Global Constraints

- Repo: `/home/bz/share/btsync/prg/srst-uttale` (same tree as `/mnt/payload/share/msi/prg/srst-uttale`). Edit only `uttale/backend/server.py` and `uttale/backend/test_server.py` (+ the spec/plan). Commit to **master**; stage only the named files (never `git add -A`).
- Style (`STYLE.md`/`AGENTS.md`): no comments unless they explain *why*; compact; all imports at top; mimic surrounding code.
- Testing: **no pytest.** stdlib `unittest`. Run via the uv test env from the repo root: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v`. Syntax: `/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py`.
- New code must add **ZERO** new ruff issues. Pre-existing E722 (bare except) + F401 (`sys`,`Dict`) are accepted/out of scope (project policy: "zero new issues other than E722").
- Do NOT start/stop/restart the live :7010 server; its DuckDB `~/.cache/srst-uttale/root.db` is locked — never open it. Tests use a temp DuckDB under `/tmp/opencode`.
- The shared DuckDB connection is the module global `server.db_duckdb` (set by `server.init_database()` from `server.args.db`). Tests set `server.args = SimpleNamespace(db=<tempfile>)` then call `server.init_database()`.
- `server.search(q, scope="", limit=100)` returns a `Search` pydantic model whose `.results` is a list of `{"filename","text","start","end"}` dicts, and `.results_count` is their count.

## File structure

- `uttale/backend/server.py` — the branch inside `search()` (currently lines 613-631).
- `uttale/backend/test_server.py` — a new `TestSearchExactMatch` class appended at the end (before `if __name__ == '__main__':`).

---

### Task 1: exact-match branch in `search()`

**Files:**
- Modify: `uttale/backend/server.py` (`search()`, lines 613-631)
- Test: `uttale/backend/test_server.py` (new `TestSearchExactMatch`)

**Interfaces:**
- Produces: behavior change only — `search(q, scope, limit)`'s contract is unchanged (same `Search` model). When `q.strip()` is empty and `scope` is non-empty, it returns exactly the rows of the file named `scope`, ordered by `start`.

- [ ] **Step 1: Write the failing tests**

Append to `uttale/backend/test_server.py` (before the final `if __name__ == '__main__':`). The fixture seeds `lines` rows directly (controlling `start` order) — no file tree needed.

```python
class TestSearchExactMatch(unittest.TestCase):
    def setUp(self):
        self.dbfile = os.path.join(tempfile.mkdtemp(), 'lines.db')
        self._saved_args = server.args
        self._saved_db = server.db_duckdb
        server.args = SimpleNamespace(db=self.dbfile)
        server.init_database()
        rows = [
            # file A, inserted OUT of start order on purpose
            ('48k/Pod/20260601/by10m/a.vtt', '00:00:02.000', '00:00:03.000', 'a-third'),
            ('48k/Pod/20260601/by10m/a.vtt', '00:00:00.000', '00:00:01.000', 'a-first'),
            ('48k/Pod/20260601/by10m/a.vtt', '00:00:01.000', '00:00:02.000', 'a-second'),
            # file B (must never appear for an A-scoped exact match)
            ('48k/Pod/20260601/by10m/b.vtt', '00:00:00.000', '00:00:01.000', 'b-only'),
        ]
        server.db_duckdb.executemany("INSERT INTO lines VALUES (?, ?, ?, ?)", rows)

    def tearDown(self):
        try:
            server.db_duckdb.close()
        except Exception:
            pass
        server.args = self._saved_args
        server.db_duckdb = self._saved_db
        shutil.rmtree(os.path.dirname(self.dbfile), ignore_errors=True)

    def test_exact_match_returns_only_that_file_ordered_by_start(self):
        res = server.search(q="", scope="48k/Pod/20260601/by10m/a.vtt", limit=1000)
        files = {r["filename"] for r in res.results}
        self.assertEqual(files, {"48k/Pod/20260601/by10m/a.vtt"})
        self.assertEqual([r["text"] for r in res.results], ["a-first", "a-second", "a-third"])
        self.assertEqual(res.results_count, 3)

    def test_whitespace_only_q_still_uses_exact_match(self):
        res = server.search(q="   ", scope="48k/Pod/20260601/by10m/a.vtt", limit=1000)
        self.assertEqual(res.results_count, 3)
        self.assertNotIn("b-only", [r["text"] for r in res.results])

    def test_empty_scope_uses_like_path_matches_all(self):
        res = server.search(q="", scope="", limit=1000)
        self.assertEqual(res.results_count, 4)

    def test_text_query_still_uses_like_path(self):
        res = server.search(q="a-second", scope="", limit=1000)
        self.assertEqual([r["text"] for r in res.results], ["a-second"])

    def test_exact_match_limit_applies(self):
        res = server.search(q="", scope="48k/Pod/20260601/by10m/a.vtt", limit=2)
        self.assertEqual(res.results_count, 2)
        self.assertEqual([r["text"] for r in res.results], ["a-first", "a-second"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestSearchExactMatch -v`
Expected: FAIL — `test_exact_match_returns_only_that_file_ordered_by_start` fails because the current `LIKE` path returns file A's rows in storage (insert) order `['a-third','a-first','a-second']`, not `['a-first','a-second','a-third']`. (Other tests may pass under the current code; the ordering one is the proof the branch is needed.)

- [ ] **Step 3: Add the exact-match branch**

Replace the body of `search()` (`server.py:613-631`). Current:

```python
@app.get("/uttale/Search", response_model=Search)
def search(q: str, scope: str = "", limit: int = 100) -> Search:
    """Search for text in the database given a scope"""
    result = Search(q=q, scope=scope, limit=limit)
    try:
        query = q.replace(" ", "%")
        scope_query = scope.replace(" ", "%")
        cursor = db_duckdb.execute(
            "SELECT filename, start, end_time, text FROM lines WHERE LOWER(text) LIKE LOWER(?) AND LOWER(filename) LIKE LOWER(?) LIMIT ?",
            (f"%{query}%", f"%{scope_query}%", limit),
        ).fetchall()
        result.results = [
            {"filename": row[0], "text": row[3], "start": row[1], "end": row[2]}
            for row in cursor
        ]
        result.results_count = len(result.results)
    except:
        raise HTTPException(status_code=500, detail="DuckDB search query failed")
    return result
```

New (add the `if not q.strip() and scope:` branch; the `else` keeps the existing LIKE query unchanged):

```python
@app.get("/uttale/Search", response_model=Search)
def search(q: str, scope: str = "", limit: int = 100) -> Search:
    """Search for text in the database given a scope"""
    result = Search(q=q, scope=scope, limit=limit)
    try:
        if not q.strip() and scope:
            cursor = db_duckdb.execute(
                "SELECT filename, start, end_time, text FROM lines WHERE filename = ? ORDER BY start LIMIT ?",
                (scope, limit),
            ).fetchall()
        else:
            query = q.replace(" ", "%")
            scope_query = scope.replace(" ", "%")
            cursor = db_duckdb.execute(
                "SELECT filename, start, end_time, text FROM lines WHERE LOWER(text) LIKE LOWER(?) AND LOWER(filename) LIKE LOWER(?) LIMIT ?",
                (f"%{query}%", f"%{scope_query}%", limit),
            ).fetchall()
        result.results = [
            {"filename": row[0], "text": row[3], "start": row[1], "end": row[2]}
            for row in cursor
        ]
        result.results_count = len(result.results)
    except:
        raise HTTPException(status_code=500, detail="DuckDB search query failed")
    return result
```

- [ ] **Step 4: Run tests + syntax + ruff**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestSearchExactMatch -v`
Expected: PASS (5 tests).

Then the whole suite + syntax + ruff:
```bash
/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v
/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py
/tmp/opencode/uttale-test/bin/ruff check uttale/backend/server.py
```
Expected: all tests pass; ruff shows no NEW issues vs the accepted baseline (7 E722 + 2 F401). (The branch adds one `if`/`else`, no new bare-except, no new import.)

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: exact-match Search path for downloads (filename = ? ORDER BY start)

When q is empty and scope is set (the offline /api/lines download path), query
WHERE filename = ? ORDER BY start instead of the ~2s LOWER()-LIKE full scan
(~17-31x faster). Text-search path unchanged."
```

---

### Task 2: Live smoke (throwaway server)

**Files:** none (verification only).

- [ ] **Step 1: Build a temp tree + start a throwaway uttale server**

```bash
mkdir -p /tmp/opencode/srch-smoke/48k/Pod/20260601/by10m
printf 'WEBVTT\n\n00:00:02.000 --> 00:00:03.000\nthird\n\n00:00:00.000 --> 00:00:01.000\nfirst\n\n00:00:01.000 --> 00:00:02.000\nsecond\n' \
  > /tmp/opencode/srch-smoke/48k/Pod/20260601/by10m/a.vtt
PYTHONPATH=/home/bz/share/btsync/prg/srst-uttale /tmp/opencode/uttale-test/bin/python -m uttale.backend.server \
  --root /tmp/opencode/srch-smoke --db /tmp/opencode/srch-smoke/lines.db \
  --favorites-db /tmp/opencode/srch-smoke/fav.db --listens-db /tmp/opencode/srch-smoke/listens.db \
  --iface 127.0.0.1:7011 --reindex >/tmp/opencode/srch-smoke/utt.log 2>&1 &
echo $! > /tmp/opencode/utt.pid
for i in $(seq 1 20); do
  c=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:7011/uttale/Scopes?q=Pod" 2>/dev/null || echo 000)
  [ "$c" = "200" ] && { echo "ready"; break; }; sleep 0.5
done
```
(`--reindex` with no pattern builds the index from the temp tree on startup. Note: `--iface host:port`, never `--port`.)

- [ ] **Step 2: Verify exact-match returns the file's lines ordered by start**

```bash
curl -sk "https://127.0.0.1:7011/uttale/Search?q=&scope=48k/Pod/20260601/by10m/a.vtt&limit=1000" \
  -w '\n[time %{time_total}s]\n'
echo "--- text search still works (q=second, no scope) ---"
curl -sk "https://127.0.0.1:7011/uttale/Search?q=second&scope=&limit=1000"
echo
```
Expected: the exact-match call returns 3 results with `text` in order `first, second, third` (ordered by start, not insertion order), fast. The text query returns the `second` line.

- [ ] **Step 3: Tear down (by PID) + confirm real :7010 untouched**

```bash
kill "$(cat /tmp/opencode/utt.pid)" 2>/dev/null && rm -f /tmp/opencode/utt.pid
rm -rf /tmp/opencode/srch-smoke
curl -sk https://127.0.0.1:7010/uttale/Scopes?q=verna -o /dev/null -w 'real :7010: %{http_code}\n'
```
Expected: real :7010 still `200` (we never restarted it; it runs the new code only after the user restarts it).

No commit (verification task).

---

## Post-implementation note

The speedup only reaches the live server **after the user restarts :7010**. This plan never restarts it.

## Self-review checklist (completed by plan author)

- **Spec coverage:** exact-match branch on `q.strip()` empty + scope (T1 Step 3); ORDER BY start (T1 Step 3, asserted T1 Step 1); text-search path unchanged (T1 Step 1 `test_text_query_still_uses_like_path`); empty-scope LIKE preserved (T1 `test_empty_scope_uses_like_path_matches_all`); limit applies (T1 `test_exact_match_limit_applies`); live smoke (T2). All spec sections mapped.
- **Placeholder scan:** none — full code + exact commands in every step.
- **Type consistency:** uses `server.search(q, scope, limit)` → `Search` model with `.results` (list of `{filename,text,start,end}`) and `.results_count`, matching the current endpoint.
