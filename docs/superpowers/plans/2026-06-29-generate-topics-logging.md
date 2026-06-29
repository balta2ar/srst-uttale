# GenerateTopics server-log instrumentation — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface GenerateTopics outcomes (start / success / failure-with-reason) to the uttale server log so failures aren't silent (only in `/tmp/vtt-topics/` today).

**Architecture:** Add `logging` calls in `run_vtt_topics` (uttale/backend/server.py) using the module logger already configured at server.py:157. Log start (info) before the subprocess; after the existing publish/discard decision, log success (info) or a single error (error, keyed by failure mode: OSError/not-found, non-zero exit, or empty output). Additive — no change to control flow, return value, the per-run file, or the API.

**Tech Stack:** Python 3.12, stdlib `logging`, stdlib `unittest` (`assertLogs`).

## Global Constraints

- Repo: `/home/bz/share/btsync/prg/srst-uttale` (== `/mnt/payload/share/msi/prg/srst-uttale`, same tree). Edit only `uttale/backend/server.py` and `uttale/backend/test_server.py` (+ spec/plan). Commit to **master**; stage only the named files (never `git add -A`).
- Style (`STYLE.md`/`AGENTS.md`): no comments unless they explain *why*; compact; imports at top; mimic surrounding code.
- Testing: **no pytest.** stdlib `unittest`. Run via the uv test env from the repo root: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v`. Syntax: `/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py`.
- New code must add **ZERO** new ruff issues. Pre-existing E722 (bare except) + F401 (`sys`,`Dict`) are accepted/out of scope (policy: "zero new issues other than E722").
- `logging` is already imported and configured at the top of server.py (`logging.basicConfig(level=logging.DEBUG)` at server.py:157). Do NOT add a new logger or re-configure logging — use the module-level `logging.info`/`logging.error` (root logger). `assertLogs()` with no logger arg captures the root logger.
- Do NOT change `run_vtt_topics`'s signature or return value (`int`), the per-run file writes, or `start_topics_generation`/the endpoint.

## File structure

- `uttale/backend/server.py` — `logging` calls inside `run_vtt_topics` (currently lines 380-407).
- `uttale/backend/test_server.py` — `assertLogs` assertions added as new methods in the existing `TestGenerateTopics` class.

---

### Task 1: log GenerateTopics outcomes

**Files:**
- Modify: `uttale/backend/server.py` (`run_vtt_topics`, lines 380-407)
- Test: `uttale/backend/test_server.py` (new methods in `TestGenerateTopics`)

**Interfaces:**
- Produces: behavior unchanged (`run_vtt_topics(topic_dir, log_dir=...) -> int`). Adds root-logger records: one `INFO` "vtt-topics start", then either one `INFO` "vtt-topics published" (success) or one `ERROR` (failure, message keyed by mode).

- [ ] **Step 1: Write the failing tests**

Add these methods to the existing `TestGenerateTopics` class in `uttale/backend/test_server.py` (it already has `self.stub(...)`, `self.root`, `self.logs`, `self.episode_dir`, `self.topics_path`, `self.filename`, and the PATH save/restore in setUp/tearDown). Place them after `test_run_does_not_publish_empty_output` (around line 334).

```python
    def test_logs_start_and_published_on_success(self):
        self.stub('#!/bin/sh\nprintf "00:00:10 Intro\\n"\n')
        with self.assertLogs(level='INFO') as cm:
            run_vtt_topics(self.episode_dir, log_dir=self.logs)
        out = "\n".join(cm.output)
        self.assertIn('vtt-topics start', out)
        self.assertIn('vtt-topics published', out)
        self.assertIn(self.episode_dir, out)

    def test_logs_error_on_nonzero_exit(self):
        self.stub('#!/bin/sh\necho boom >&2\nexit 3\n')
        with self.assertLogs(level='ERROR') as cm:
            run_vtt_topics(self.episode_dir, log_dir=self.logs)
        out = "\n".join(cm.output)
        self.assertIn('exit=3', out)
        self.assertIn(self.logs, out)

    def test_logs_error_on_empty_output(self):
        self.stub('#!/bin/sh\nexit 0\n')
        with self.assertLogs(level='ERROR') as cm:
            run_vtt_topics(self.episode_dir, log_dir=self.logs)
        out = "\n".join(cm.output)
        self.assertIn('no output', out)

    def test_logs_error_when_binary_not_found(self):
        # Do NOT stub vtt-topics; ensure PATH cannot resolve it.
        os.environ['PATH'] = self.bindir
        with self.assertLogs(level='ERROR') as cm:
            run_vtt_topics(self.episode_dir, log_dir=self.logs)
        out = "\n".join(cm.output)
        self.assertIn('vtt-topics', out)
        self.assertIn(self.episode_dir, out)
```

Note: `setUp` saves `self._orig_path = os.environ.get('PATH', '')` and `tearDown` restores it (existing), so mutating `os.environ['PATH']` in the not-found test is safe. `self.bindir` is the (empty-by-default) stub dir created in setUp, which does not contain `vtt-topics` unless `self.stub(...)` was called.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestGenerateTopics -v`
Expected: the 4 new tests FAIL — `assertLogs` raises `AssertionError: no logs of level INFO or higher triggered on root` (because `run_vtt_topics` currently emits no logging records). (The pre-existing `TestGenerateTopics` tests still pass.)

- [ ] **Step 3: Add the logging to `run_vtt_topics`**

Replace `run_vtt_topics` (`server.py:380-407`). Current:

```python
def run_vtt_topics(topic_dir: str, log_dir: str = TOPICS_LOG_DIR) -> int:
    os.makedirs(log_dir, exist_ok=True)
    safe = topic_dir.strip("/").replace("/", "_") or "root"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = join(log_dir, f"{safe}-{stamp}.log")
    target = join(topic_dir, "topics")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=topic_dir, prefix=".topics-", delete=False
    )
    tmp_path = tmp.name
    tmp.close()
    code = -1
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"vtt-topics {topic_dir}\nstarted {stamp}\n")
        log.flush()
        try:
            with open(tmp_path, "w", encoding="utf-8") as out:
                code = subprocess.run(
                    ["vtt-topics", topic_dir], stdout=out, stderr=log
                ).returncode
        except OSError as e:
            log.write(f"error {e}\n")
        log.write(f"exit={code}\n")
    if code == 0 and exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        os.replace(tmp_path, target)
    elif exists(tmp_path):
        os.remove(tmp_path)
    return code
```

New (capture the OSError reason into `err`; log start before the subprocess; one success/error log after the publish/discard decision):

```python
def run_vtt_topics(topic_dir: str, log_dir: str = TOPICS_LOG_DIR) -> int:
    os.makedirs(log_dir, exist_ok=True)
    safe = topic_dir.strip("/").replace("/", "_") or "root"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = join(log_dir, f"{safe}-{stamp}.log")
    target = join(topic_dir, "topics")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=topic_dir, prefix=".topics-", delete=False
    )
    tmp_path = tmp.name
    tmp.close()
    logging.info("vtt-topics start dir=%s log=%s", topic_dir, log_path)
    code = -1
    err = ""
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"vtt-topics {topic_dir}\nstarted {stamp}\n")
        log.flush()
        try:
            with open(tmp_path, "w", encoding="utf-8") as out:
                code = subprocess.run(
                    ["vtt-topics", topic_dir], stdout=out, stderr=log
                ).returncode
        except OSError as e:
            err = str(e)
            log.write(f"error {e}\n")
        log.write(f"exit={code}\n")
    if code == 0 and exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        os.replace(tmp_path, target)
        logging.info("vtt-topics published dir=%s", topic_dir)
    else:
        if exists(tmp_path):
            os.remove(tmp_path)
        if err:
            reason = f"vtt-topics not found: {err}"
        elif code != 0:
            reason = f"exit={code}"
        else:
            reason = "produced no output"
        logging.error("vtt-topics failed (%s) dir=%s log=%s", reason, topic_dir, log_path)
    return code
```

Notes for the implementer:
- The publish/discard logic is preserved: success branch publishes (and now logs info); the `else` covers all failure modes (removes the temp file if present, exactly as the old `elif exists(tmp_path)` did, then logs one error).
- `reason` priority: OSError (`err`) first (since the not-found case also has `code == -1`), then non-zero exit, then exit-0-empty. This yields exactly **one** error line per failed run.
- Do not add comments; the code is self-explanatory and house style is comment-free.

- [ ] **Step 4: Run tests + syntax + ruff**

Run: `/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server.TestGenerateTopics -v`
Expected: PASS (all `TestGenerateTopics` tests, including the 4 new).

Then the whole suite + syntax + ruff:
```bash
/tmp/opencode/uttale-test/bin/python -m unittest uttale.backend.test_server -v
/tmp/opencode/uttale-test/bin/python -m py_compile uttale/backend/server.py
/tmp/opencode/uttale-test/bin/ruff check uttale/backend/server.py
```
Expected: all tests pass (note: an unrelated pre-existing flake, `TestReindexEndpoint.test_started_reports_matched_and_runs`, may run slowly but was de-flaked earlier to a 30s budget — it should pass). ruff shows no NEW issues vs the accepted baseline (7 E722 + 2 F401). The change adds no new bare-except and no new import (`logging` already imported), so ruff is unchanged.

- [ ] **Step 5: Commit**

```bash
git add uttale/backend/server.py uttale/backend/test_server.py
git commit -m "backend: log GenerateTopics outcomes to server log

run_vtt_topics now emits start (info), success 'published' (info), and a single
failure (error, keyed by OSError/exit-code/empty-output with the per-run log
path) to the module logger -> journald, so GenerateTopics failures are visible in
journalctl, not just /tmp/vtt-topics. Additive; per-run file + return value
unchanged."
```

---

## Self-review checklist (completed by plan author)

- **Spec coverage:** start (info) — Step 3 `logging.info("vtt-topics start...")`; success (info) — Step 3 success branch; single error keyed by mode (OSError/exit/empty) — Step 3 `else` branch with `reason` priority; per-run file unchanged — Step 3 keeps all `log.write(...)`; tests for success/non-zero/empty/not-found — Step 1; zero new ruff — Step 4. All spec sections mapped.
- **Placeholder scan:** none — full before/after code + exact commands.
- **Type/consistency:** `run_vtt_topics(topic_dir, log_dir=...) -> int` unchanged; `logging.info`/`logging.error` are the root-logger calls captured by `assertLogs()`; `err`/`reason`/`code` locals consistent across the Step-3 block.
- **Edge note:** the not-found test sets `PATH=self.bindir` (empty stub dir) so `vtt-topics` can't resolve; `setUp` saved the original PATH and `tearDown` restores it (existing fixture behavior).
