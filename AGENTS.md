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
  helpers. Add favorites helper tests there in the same style.
* venv python: `.venv/bin/python` (python 3.12). The deployed process may run
  under system python3.13 site-packages; **restart the backend** to pick up code
  changes (it has run over HTTPS on :7010).
* Current cross-repo task: a global "favorites" feature. The UI side
  (`srst-harken`) holds the design spec:
  `srst-harken/docs/specs/2026-06-27-favorites-design.md`. Backend work for it:
  a NEW separate **SQLite** DB (stdlib `sqlite3`, opened/closed per request via a
  context-manager helper), configured by a new `--favorites-db` arg (default
  `~/.cache/srst-uttale/favorites.db`), plus `Favorite`/`Favorites` models and
  `Favorites` Add/List/Update/Delete/Export endpoints. See the spec for the exact
  schema, endpoints, and semantics.
