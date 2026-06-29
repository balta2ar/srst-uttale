# Surface GenerateTopics outcomes to the uttale server log

Date: 2026-06-29
Status: approved (design)

## Problem

`POST /uttale/GenerateTopics` runs `vtt-topics` in a fire-and-forget daemon
thread (`run_vtt_topics`). When it fails — e.g. the server can't find `vtt-topics`
on its PATH (`FileNotFoundError`), the script exits non-zero, or it produces no
output — the failure is written **only** to the per-run file
`/tmp/vtt-topics/<dir>-<ts>.log`. It never reaches the server's own
stderr/journal, so `journalctl --user -u uttale` shows nothing and the UI just
says "started". The failure is effectively silent.

(The recent real-world instance: the systemd unit's minimal PATH lacked
`vtt-topics`, so every run logged `error [Errno 2] No such file or directory:
'vtt-topics'` to the per-run file only. A separate dotfiles fix forwards PATH;
this change makes such failures visible in the server log going forward.)

## Change

Add `logging` calls in `run_vtt_topics` (`uttale/backend/server.py:380-407`) using
the module logger already configured at `server.py:157`
(`logging.basicConfig(level=logging.DEBUG)`). This is additive — only log lines;
no change to control flow, the return value (`code`), the per-run file logging, or
the API response.

Events and levels:

- **Start** → `logging.info("vtt-topics start dir=%s log=%s", topic_dir, log_path)`
  (emitted right after `log_path` is known, before launching the subprocess).
- **Binary not found / OSError** (the existing `except OSError as e:` at
  `server.py:400`) → `logging.error("vtt-topics failed: %s (dir=%s log=%s)", e,
  topic_dir, log_path)`.
- **Non-zero exit** (`code != 0` after the subprocess returns; the case where
  nothing is published) → `logging.error("vtt-topics exit=%d (dir=%s log=%s)",
  code, topic_dir, log_path)`.
- **Exit 0 but empty output** (publish guard fails: exit 0 yet the temp file is
  missing/empty, so no `topics` is written) → `logging.error("vtt-topics produced
  no output (dir=%s log=%s)", topic_dir, log_path)`.
- **Success** (exit 0 + non-empty temp file → `topics` published) →
  `logging.info("vtt-topics published dir=%s", topic_dir)`.

These are hooked into the **existing** publish/discard decision
(`server.py:403-406`): `if code == 0 and exists(tmp_path) and getsize(tmp_path) >
0:` is the success branch (publish + info); the `elif`/failure paths carry the
error logs. The `OSError` handler carries the binary-not-found error.

### Implementation note: one error log per run (no duplicates)

The `OSError`/not-found case sets `code = -1` and still creates an (empty)
`tmp_path`, so it would also fall into the discard `elif`. To emit exactly **one**
server-log line per failed run, the error logging is centralized at the
publish/discard decision rather than split across the `except` and the branch:

- In the `except OSError as e:` block, keep the existing per-run-file write and
  **capture the reason** into a local (e.g. `err = str(e)`); do not call
  `logging.error` there.
- After the `with` block, branch on the outcome:
  - success (exit 0 + non-empty temp) → `logging.info(... published ...)`.
  - otherwise → a single `logging.error(...)` whose message is chosen by the
    failure mode: a captured `OSError` reason if present (e.g. "vtt-topics not
    found: <err>"); else `code != 0` → "exit=<code>"; else (`code == 0`, empty)
    → "produced no output". Include `dir` and the per-run `log` path.

This keeps one clear error line per failure while preserving the existing
per-run-file detail.

The reason + the per-run log path appear in the journal, so a reader immediately
sees *what* failed and *where* the full subprocess output lives.

## Data flow / structure

`run_vtt_topics` already computes `topic_dir`, `log_path`, `code`, and `tmp_path`,
and already branches on publish-vs-discard. The new log lines attach to those
existing points — no new functions, ~5 added lines. The function's signature,
return value (`int` exit code), and the per-run file remain unchanged, so
`start_topics_generation`, the worker thread, and the endpoint are untouched.

## Error handling

The `logging` calls cannot meaningfully fail and do not alter control flow or the
return value. The endpoint still returns `"started"` synchronously — it runs
before the thread produces an outcome, so it cannot report success/failure; that
is out of scope (the journal + per-run log are the outcome record).

## Testing (uttale AGENTS.md: stdlib unittest, no pytest)

Extend `TestGenerateTopics` in `uttale/backend/test_server.py` (it already builds
a temp tree, stubs a fake `vtt-topics` on PATH via `self.stub(...)`, and exercises
`run_vtt_topics`). Use `self.assertLogs(level=...)`:

- **Success**: stub a `vtt-topics` that prints non-empty output → assert an
  `INFO` record matching "published" is emitted (and `topics` is written, as
  existing tests already check).
- **Non-zero exit**: stub one that exits 3 → assert an `ERROR` record containing
  `exit=3` and the per-run log path.
- **Empty output**: stub one that exits 0 with no stdout → assert an `ERROR`
  record matching "no output".
- **Binary not found**: temporarily set `PATH` to a dir without `vtt-topics`
  (or unset the stub) and run `run_vtt_topics` → assert an `ERROR` record is
  emitted mentioning the failure (this is the exact real-world case).
- New code must add **zero** new ruff issues (pre-existing E722/F401 accepted).
- Run via the uv test env: `/tmp/opencode/uttale-test/bin/python -m unittest
  uttale.backend.test_server -v`.

## Scope / non-goals

- No change to the API response, the per-run file logging, the thread/dedup
  logic, or the PATH (the PATH fix lives in the dotfiles `supervisor` script,
  applied separately).
- No new log file, rotation, or log-level config — uses the existing module
  logger → journald.

## Files touched

- `uttale/backend/server.py`: the `logging` calls in `run_vtt_topics`.
- `uttale/backend/test_server.py`: `assertLogs` assertions in `TestGenerateTopics`.
- `docs/specs/2026-06-29-generate-topics-logging-design.md`: this document.
