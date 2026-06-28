import argparse
import fnmatch
import hashlib
import logging
import multiprocessing as mp
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from os.path import dirname, exists, join, relpath, splitext
from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import polars as pl
import uvicorn
import webvtt
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tqdm import tqdm


class Scopes(BaseModel):
    q: str = ""
    limit: int = 100
    results_count: int = 0
    results: list[str] = []


class Search(BaseModel):
    q: str
    scope: str = ""
    limit: int = 100
    results_count: int = 0
    results: list[dict] = []


class Play(BaseModel):
    filename: str
    start: str
    end: str
    status: str = ""


class Reindex(BaseModel):
    pattern: str = ""
    status: str = ""


class StatusResponse(BaseModel):
    status: str


class Favorite(BaseModel):
    filename: str
    start: str
    end: str = ""
    text: str = ""
    comment: str = ""
    created_at: str = ""
    updated_at: str = ""
    exported_at: Optional[str] = None


class Favorites(BaseModel):
    filename: str = ""
    results_count: int = 0
    results: list[Favorite] = []


class FavoriteAdd(BaseModel):
    filename: str
    start: str
    end: str = ""
    text: str = ""
    comment: str = ""


class FavoriteUpdate(BaseModel):
    filename: str
    start: str
    comment: Optional[str] = None
    set_exported: bool = False


class Topic(BaseModel):
    title: str
    start: str

class Topics(BaseModel):
    filename: str = ""
    results_count: int = 0
    results: list[Topic] = []


class GenerateTopicsRequest(BaseModel):
    filename: str


class GenerateTopics(BaseModel):
    filename: str = ""
    status: str = ""


class Listen(BaseModel):
    filename: str
    position: str
    updated_at: str


class Listens(BaseModel):
    results_count: int = 0
    results: list[Listen] = []


class ListenAdd(BaseModel):
    filename: str
    position: str


class ArgumentParserWithDefaults(argparse.ArgumentParser):
    def format_help(self):
        help_text = super().format_help()
        help_text += "\nCurrent argument values:\n"
        for action in self._actions:
            if (
                action.dest != "help"
                and hasattr(action, "default")
                and action.default is not None
                and action.default != argparse.SUPPRESS
            ):
                help_text += f"  {action.dest}: {action.default}\n"
        return help_text


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)
db_duckdb = None
args = None

logging.basicConfig(level=logging.DEBUG)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    headers = dict(request.headers)
    logging.debug(f"Incoming Request: {request.method} {request.url}")
    logging.debug(f"Headers: {headers}")
    return await call_next(request)


def resolve_db_path(db_arg: str) -> str:
    """Resolve database path based on argument rules"""
    if "/" not in db_arg and "\\" not in db_arg and not db_arg.startswith("~"):
        cache_dir = Path.home() / ".cache" / "srst-uttale"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir / db_arg)
    return db_arg


def init_database():
    """Initialize the database and create tables"""
    global db_duckdb
    db_path = resolve_db_path(args.db)
    db_duckdb = duckdb.connect(db_path)
    db_duckdb.execute(
        "CREATE TABLE IF NOT EXISTS lines (filename VARCHAR, start VARCHAR, end_time VARCHAR, text VARCHAR)"
    )
    db_duckdb.execute("CREATE TABLE IF NOT EXISTS scopes (scope VARCHAR)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def favorites_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS favorites ("
            "filename TEXT, start TEXT, end TEXT, text TEXT, comment TEXT DEFAULT '', "
            "created_at TEXT, updated_at TEXT, exported_at TEXT, "
            "PRIMARY KEY (filename, start))"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def favorites_get(db_path: str, filename: str, start: str) -> Optional[dict]:
    with favorites_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM favorites WHERE filename = ? AND start = ?", (filename, start)
        ).fetchone()
        return dict(row) if row else None


FAVORITES_SORTS = {
    "created_desc": "created_at DESC",
    "created_asc": "created_at ASC",
    "name_asc": "filename ASC, start ASC",
    "name_desc": "filename DESC, start DESC",
}


def favorites_list(db_path: str, filename: Optional[str] = None, sort: str = "created_desc") -> List[dict]:
    order_by = FAVORITES_SORTS.get(sort, FAVORITES_SORTS["created_desc"])
    with favorites_db(db_path) as conn:
        if filename:
            rows = conn.execute(
                f"SELECT * FROM favorites WHERE LOWER(filename) LIKE LOWER(?) ORDER BY {order_by}",
                (f"%{filename}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM favorites ORDER BY {order_by}"
            ).fetchall()
        return [dict(row) for row in rows]


def favorites_add(
    db_path: str, filename: str, start: str, end: str = "", text: str = "", comment: str = ""
) -> dict:
    now = now_iso()
    with favorites_db(db_path) as conn:
        conn.execute(
            "INSERT INTO favorites (filename, start, end, text, comment, created_at, updated_at, exported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(filename, start) DO UPDATE SET "
            "end = excluded.end, text = excluded.text, comment = excluded.comment, updated_at = excluded.updated_at",
            (filename, start, end, text, comment, now, now),
        )
    return favorites_get(db_path, filename, start)


def favorites_update(
    db_path: str, filename: str, start: str, comment: Optional[str] = None, set_exported: bool = False
) -> Optional[dict]:
    now = now_iso()
    sets = ["updated_at = ?"]
    values = [now]
    if comment is not None:
        sets.append("comment = ?")
        values.append(comment)
    if set_exported:
        sets.append("exported_at = ?")
        values.append(now)
    values.extend([filename, start])
    with favorites_db(db_path) as conn:
        cur = conn.execute(
            f"UPDATE favorites SET {', '.join(sets)} WHERE filename = ? AND start = ?",
            values,
        )
        if cur.rowcount == 0:
            return None
    return favorites_get(db_path, filename, start)


def favorites_delete(db_path: str, filename: str, start: str) -> bool:
    with favorites_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM favorites WHERE filename = ? AND start = ?", (filename, start)
        )
        return cur.rowcount > 0


LISTENS_LIMIT = 10


@contextmanager
def listens_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listens ("
            "filename TEXT PRIMARY KEY, position TEXT, updated_at TEXT)"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def listens_list(db_path: str) -> List[dict]:
    with listens_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM listens ORDER BY updated_at DESC LIMIT ?", (LISTENS_LIMIT,)
        ).fetchall()
        return [dict(row) for row in rows]


def listens_upsert(db_path: str, filename: str, position: str) -> dict:
    now = now_iso()
    with listens_db(db_path) as conn:
        conn.execute(
            "INSERT INTO listens (filename, position, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET "
            "position = excluded.position, updated_at = excluded.updated_at",
            (filename, position, now),
        )
        conn.execute(
            "DELETE FROM listens WHERE filename NOT IN "
            "(SELECT filename FROM listens ORDER BY updated_at DESC LIMIT ?)",
            (LISTENS_LIMIT,),
        )
        row = conn.execute(
            "SELECT * FROM listens WHERE filename = ?", (filename,)
        ).fetchone()
        return dict(row)


def parse_time(t: str) -> float:
    h, m, s = t.split(":")
    s, ms = s.split(".")
    return int(h) * 3600 + int(m) * 60 + float(s) + int(ms) / 1000


TOPIC_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]?\d):([0-5]?\d)(\.\d{1,3})?$")


def parse_topic_time(token: str) -> Optional[str]:
    m = TOPIC_TIME_RE.match(token)
    if not m:
        return None
    h, mm, ss, frac = m.groups()
    return f"{int(h):02d}:{int(mm):02d}:{int(ss):02d}{frac or '.000'}"


def read_topics(root: str, filename: str) -> List[Topic]:
    path = join(dirname(join(root, filename)), "topics")
    if not exists(path):
        return []
    topics = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2:
                    continue
                start = parse_topic_time(parts[0])
                if start is None:
                    continue
                topics.append(Topic(title=parts[1].strip(), start=start))
    except OSError:
        return []
    return topics


TOPICS_LOG_DIR = "/tmp/vtt-topics"
_topics_running: set = set()
_topics_lock = threading.Lock()


def topics_dir_for(root: str, filename: str) -> str:
    return dirname(join(root, filename))


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


def start_topics_generation(
    root: str, filename: str, log_dir: str = TOPICS_LOG_DIR
) -> str:
    topic_dir = topics_dir_for(root, filename)
    if not os.path.isdir(topic_dir):
        return "not found"
    key = os.path.realpath(topic_dir)
    with _topics_lock:
        if key in _topics_running:
            return "already running"
        _topics_running.add(key)

    def worker():
        try:
            run_vtt_topics(topic_dir, log_dir)
        finally:
            with _topics_lock:
                _topics_running.discard(key)

    threading.Thread(target=worker, daemon=True).start()
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


def reindex_worker_duckdb(
    vtt_files: List[str], root: str, return_dict, idx: int, counter, lock: mp.Lock
):
    rows = []
    for vtt in vtt_files:
        captions = process_vtt(vtt, root)
        rows.extend(captions)
        with lock:
            counter.value += 1
    return_dict[idx] = rows


def update_progress(
    total: int, counter, lock: mp.Lock, stop_event: threading.Event, description: str
):
    with tqdm(total=total, desc=description) as pbar:
        while not stop_event.is_set():
            with lock:
                current = counter.value
            pbar.n = current
            pbar.refresh()
            if current >= total:
                break
            time.sleep(0.5)
        pbar.n = total
        pbar.refresh()


def pattern_to_wildcard(pattern: str) -> str:
    """Convert user pattern to wildcard expression"""
    if not pattern:
        return "*"
    parts = pattern.strip().split()
    if not parts:
        return "*"
    wildcard = "*" + "*".join(parts) + "*"
    return wildcard.lower()


def reindex(root: str, pattern: str = ""):
    try:
        fd = subprocess.run(
            ["fd", "--type", "f", "--extension", "vtt", "--base-directory", root],
            capture_output=True,
            text=True,
            check=True,
        )
        vtt_files = fd.stdout.splitlines()
    except:
        vtt_files = []

    if pattern:
        wildcard = pattern_to_wildcard(pattern)
        vtt_files = [f for f in vtt_files if fnmatch.fnmatch(f.lower(), wildcard)]
    total_files = len(vtt_files)
    if not vtt_files:
        return
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
    if all_rows:
        df = pl.DataFrame(all_rows, schema=["filename", "start", "end_time", "text"])
        db_duckdb.register("df", df)
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


@app.get("/uttale/Scopes", response_model=Scopes)
def scopes(q: str = "", limit: int = 100) -> Scopes:
    """Search for scopes in the database"""
    result = Scopes(q=q, limit=limit)
    try:
        query = q.replace(" ", "%")
        cursor = db_duckdb.execute(
            "SELECT DISTINCT scope FROM scopes WHERE LOWER(scope) LIKE LOWER(?) ORDER BY scope LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        result.results = [row[0] for row in cursor]
        result.results_count = len(result.results)
    except:
        pass
    return result


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


@app.get("/uttale/Topics", response_model=Topics)
def topics(filename: str) -> Topics:
    """Return background-generated topic markers for a podcast"""
    results = read_topics(args.root, filename)
    return Topics(filename=filename, results=results, results_count=len(results))


@app.post("/uttale/GenerateTopics", response_model=GenerateTopics)
def generate_topics(request: GenerateTopicsRequest) -> GenerateTopics:
    """Generate topic markers for an episode in the background (fire and forget)"""
    status = start_topics_generation(args.root, request.filename)
    return GenerateTopics(filename=request.filename, status=status)


def audio_etag(filename: str, start: str, end: str) -> str:
    digest = hashlib.sha1(f"{filename}|{start}|{end}".encode("utf-8")).hexdigest()
    return f'"{digest}"'


def get_audio_segment(
    filename: str, start: str, end: str, range_header: str = None
) -> tuple[bytes, dict]:
    o = splitext(join(args.root, filename))[0] + ".ogg"
    if not exists(o):
        raise HTTPException(status_code=404, detail=f"File not found: {o}")

    if range_header and (start or end):
        raise HTTPException(
            status_code=400,
            detail="Cannot use both range header and start/end parameters",
        )

    try:
        if range_header:
            try:
                bytes_range = range_header.split("=")[1]
                start_byte, end_byte = map(
                    lambda x: int(x) if x else None, bytes_range.split("-")
                )
            except:
                raise HTTPException(status_code=400, detail="Invalid range header")

            file_size = os.path.getsize(o)
            if end_byte is None:
                end_byte = file_size - 1
            if start_byte is None:
                start_byte = 0

            if (
                start_byte >= file_size
                or end_byte >= file_size
                or start_byte > end_byte
            ):
                raise HTTPException(status_code=416, detail="Range Not Satisfiable")

            with open(o, "rb") as f:
                f.seek(start_byte)
                data = f.read(end_byte - start_byte + 1)

            headers = {
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end_byte - start_byte + 1),
                "Cache-Control": "max-age=86400",
            }
            return data, headers

        if not start and not end:
            with open(o, "rb") as f:
                return f.read(), {"Cache-Control": "max-age=86400"}

        start_sec = parse_time(start)
        end_sec = parse_time(end)
        duration = end_sec - start_sec
        if duration <= 0:
            raise HTTPException(
                status_code=400, detail="End time must be greater than start time"
            )
        proc = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                str(start_sec),
                "-t",
                str(duration),
                "-i",
                o,
                "-f",
                "ogg",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
        return proc.stdout, {
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": audio_etag(filename, start, end),
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid time format") from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail="Audio processing failed") from e
    except subprocess.SubprocessError as e:
        raise HTTPException(status_code=500, detail="Audio processing failed") from e


@app.get("/uttale/Play", response_model=Play)
def play(
    filename: str, start: str, end: str, background_tasks: BackgroundTasks
) -> Play:
    """Play audio segment"""
    result = Play(filename=filename, start=start, end=end)
    audio_data, _ = get_audio_segment(filename, start, end)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
        tmp.write(audio_data)
        tmp_path = tmp.name
    subprocess.Popen(["play", tmp_path])

    def cleanup(tmp_file):
        try:
            time.sleep(5)
            if exists(tmp_file):
                os.remove(tmp_file)
        except:
            pass

    background_tasks.add_task(cleanup, tmp_path)
    result.status = "playing"
    return result


@app.head("/uttale/Audio")
@app.get("/uttale/Audio")
def audio_endpoint(
    filename: str, start: str, end: str, range_header: str = Header(None, alias="Range")
) -> Response:
    """Extract audio segment"""
    audio_data, headers = get_audio_segment(filename, start, end, range_header)
    headers["Vary"] = "Origin"
    status_code = 206 if range_header else 200
    return Response(
        content=audio_data,
        media_type="audio/ogg",
        headers=headers,
        status_code=status_code,
    )


@app.post("/uttale/Reindex", response_model=Reindex)
def trigger_reindex(request: Reindex, background_tasks: BackgroundTasks) -> Reindex:
    """Trigger reindexing of subtitle files"""
    result = Reindex(pattern=request.pattern)
    background_tasks.add_task(reindex, args.root, request.pattern)
    result.status = "Reindexing started in background"
    return result


def favorites_db_path() -> str:
    return resolve_db_path(args.favorites_db)


def listens_db_path() -> str:
    return resolve_db_path(args.listens_db)


@app.get("/uttale/Favorites", response_model=Favorites)
def favorites_index(filename: str = "", sort: str = "created_desc") -> Favorites:
    """List favorites, optionally filtered by filename and sorted"""
    try:
        rows = favorites_list(favorites_db_path(), filename or None, sort)
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Favorites query failed")
    return Favorites(
        filename=filename,
        results=[Favorite(**row) for row in rows],
        results_count=len(rows),
    )


@app.post("/uttale/Favorites", response_model=Favorite)
def favorites_create(fav: FavoriteAdd) -> Favorite:
    """Add (upsert) a favorite"""
    try:
        row = favorites_add(
            favorites_db_path(), fav.filename, fav.start, fav.end, fav.text, fav.comment
        )
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Favorite add failed")
    return Favorite(**row)


@app.post("/uttale/Favorites/Update", response_model=Favorite)
def favorites_set_comment(fav: FavoriteUpdate) -> Favorite:
    """Update a favorite's comment and/or stamp exported_at"""
    try:
        row = favorites_update(
            favorites_db_path(), fav.filename, fav.start, fav.comment, fav.set_exported
        )
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Favorite update failed")
    if row is None:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return Favorite(**row)


@app.delete("/uttale/Favorites", response_model=StatusResponse)
def favorites_remove(filename: str, start: str) -> StatusResponse:
    """Delete a favorite by (filename, start)"""
    try:
        deleted = favorites_delete(favorites_db_path(), filename, start)
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Favorite delete failed")
    if not deleted:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return StatusResponse(status="deleted")


@app.post("/uttale/Favorites/Export", response_model=StatusResponse)
def favorites_export() -> StatusResponse:
    """Export favorites (stub; real export lands later)"""
    return StatusResponse(status="not implemented")


@app.get("/uttale/Listens", response_model=Listens)
def listens_index() -> Listens:
    """List the most recent listens, newest first"""
    try:
        rows = listens_list(listens_db_path())
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Listens query failed")
    return Listens(results=[Listen(**row) for row in rows], results_count=len(rows))


@app.post("/uttale/Listens", response_model=Listen)
def listens_create(listen: ListenAdd) -> Listen:
    """Upsert a listen position for an episode (keeps only LISTENS_LIMIT most recent)"""
    try:
        row = listens_upsert(listens_db_path(), listen.filename, listen.position)
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Listen upsert failed")
    return Listen(**row)


def detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def ensure_cert(cert_path: Path, key_path: Path):
    if cert_path.exists() and key_path.exists():
        return
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    sans = ["DNS:localhost", "IP:127.0.0.1"]
    ip = detect_lan_ip()
    if ip:
        sans.append(f"IP:{ip}")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key_path), "-out", str(cert_path),
        "-days", "3650", "-subj", "/CN=uttale",
        "-addext", f"subjectAltName={','.join(sans)}",
    ], check=True)
    logging.info(f"Generated self-signed cert for {sans} at {cert_path}")


def main():
    global args
    parser = ArgumentParserWithDefaults(
        description="SRST Uttale backend server for subtitle search and audio playback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Root directory containing VTT subtitle files (default: current directory)",
    )
    parser.add_argument(
        "--iface",
        default="0.0.0.0:7010",
        help="Network interface and port to bind to (format: host:port)",
    )
    parser.add_argument(
        "--db",
        default="lines_duckdb.db",
        help="Database file path. Simple filename (e.g., '202510.db') is stored in ~/.cache/srst-uttale/, "
        "otherwise path is used as-is (e.g., './test.db' or '/tmp/line.db')",
    )
    parser.add_argument(
        "--favorites-db",
        default="favorites.db",
        help="Favorites SQLite database path. Same path rules as --db "
        "(simple filename goes to ~/.cache/srst-uttale/)",
    )
    parser.add_argument(
        "--listens-db",
        default="listens.db",
        help="Listens SQLite database path (separate file, WAL mode). Same path "
        "rules as --db (simple filename goes to ~/.cache/srst-uttale/)",
    )
    parser.add_argument(
        "--reindex",
        nargs="?",
        const="",
        default=None,
        metavar="PATTERN",
        help="Reindex VTT files and exit. Optional PATTERN for case-insensitive wildcard filtering "
        "(e.g., '202510 kontakt' matches files containing both terms, spaces act as wildcards)",
    )
    parser.add_argument("--ssl", action="store_true", help="Serve over HTTPS with a self-signed cert")
    parser.add_argument("--ssl-cert", default=str(Path.home() / ".cache/srst-uttale/cert.pem"), help="TLS certificate path")
    parser.add_argument("--ssl-key", default=str(Path.home() / ".cache/srst-uttale/key.pem"), help="TLS private key path")
    args = parser.parse_args()
    init_database()
    if args.reindex is not None:
        reindex(args.root, args.reindex)
    try:
        iface, port = args.iface.split(":")
    except:
        exit(1)
    ssl_kwargs = {}
    if args.ssl:
        cert, key = Path(args.ssl_cert), Path(args.ssl_key)
        ensure_cert(cert, key)
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    uvicorn.run(app, host=iface, port=int(port), **ssl_kwargs)


if __name__ == "__main__":
    main()
