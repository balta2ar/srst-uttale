# Agents instructions

* mimic the existing code style and conventions
* do not add any comments unless necessary
* strive for conciseness, code reuse, and separate abstractions where appropriate
* do not use local imports, keep all imports at the top of the file

See also `STYLE.md` (no comments, compact, short imports).

## Architecture & context (so you don't rediscover)

* Backend is a single-file FastAPI app: `uttale/backend/server.py`. `app =
  FastAPI()`. Endpoints are namespaced under `/uttale/...`: `Scopes`, `Search`,
  `Play`, `Audio` (GET) and `Reindex` (POST). Models are pydantic `BaseModel`s
  (`Scopes`, `Search`, `Play`, ...) returning `results: list[...]` +
  `results_count`. Mirror these shapes for new endpoints.
* Main store is **DuckDB**, opened once globally in `init_database()` into
  `db_duckdb` (a module global) from a path computed by `resolve_db_path(db_arg)`.
  Tables `lines` and `scopes` are `CREATE TABLE IF NOT EXISTS`. Do NOT put new
  unrelated data (e.g. favorites) in this DuckDB.
* A "line" = `(filename, start, end_time, text)` from the `lines` table;
  timestamps are VTT strings (`00:00:26.240`). `Search` filters with `LIKE`.
* `get_audio_segment(...)` extracts/【de】codes audio; `/uttale/Audio` streams it
  and honors HTTP Range (the standard `Range` header alias was fixed so range
  requests work). harken proxies audio so the browser stays single-origin.
* CLI is argparse in `main()`. Listen default `0.0.0.0:7010`. `--db` selects the
  DuckDB. `--ssl` (+ `--ssl-cert`/`--ssl-key`, self-signed under
  `~/.cache/srst-uttale/`) serves HTTPS. CORS + `Vary: Origin` are configured for
  harken cross-origin during dev.
* Tests: `uttale/backend/test_server.py` uses `unittest` + temp dirs to test
  helpers. Add favorites helper tests there in the same style. Run with
  `make test` (`python3 -m unittest uttale.backend.test_server -v`).
* Env: there is NO working in-repo venv here — `.venv/bin/python` is a broken
  symlink to system python3.14, and system pythons lack `polars`/`webvtt`, so
  `server.py` can't even import. To RUN tests in a sandbox, use uv:
  `uv venv /tmp/opencode/uttale-test --python 3.12` then
  `uv pip install --python /tmp/opencode/uttale-test/bin/python duckdb polars
  uvicorn webvtt-py fastapi pydantic tqdm httpx` (httpx only needed for
  starlette TestClient). The deployed server is an **editable uv tool**
  (`uv tool list` shows `uttale`; installed from this repo with `editable:true`)
  that imports `server.py` directly from this working tree, so a plain
  **restart** picks up code changes — no reinstall needed. It runs over HTTPS on
  :7010, e.g. `srst-uttale-backend-api --db root.db --root <audio_root> --ssl`.
* Cross-repo "favorites" feature: the backend side is **implemented** in
  `server.py` — a separate **SQLite** store (stdlib `sqlite3`, opened/closed per
  request via the `favorites_db()` context manager), `--favorites-db` arg
  (default `~/.cache/srst-uttale/favorites.db`), `Favorite`/`Favorites` models,
  helpers `favorites_add/get/list/update/delete`, and endpoints under
  `/uttale/Favorites` (GET list, POST add/upsert, POST `/Update` comment,
  DELETE by `?filename=&start=`, POST `/Export` no-op stub). Tests in
  `test_server.py::TestFavorites`. The UI side (`srst-harken`) and the design
  spec live at `srst-harken/docs/specs/2026-06-27-favorites-design.md`.
