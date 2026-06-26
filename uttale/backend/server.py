import argparse
import fnmatch
import logging
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import threading
import time
from os.path import exists, join, relpath, splitext
from pathlib import Path
from typing import Dict, List

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


def parse_time(t: str) -> float:
    h, m, s = t.split(":")
    s, ms = s.split(".")
    return int(h) * 3600 + int(m) * 60 + float(s) + int(ms) / 1000


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
        return proc.stdout, {"Cache-Control": "max-age=86400"}

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
    filename: str, start: str, end: str, range_header: str = Header(None)
) -> Response:
    """Extract audio segment"""
    audio_data, headers = get_audio_segment(filename, start, end, range_header)
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
        "--reindex",
        nargs="?",
        const="",
        default=None,
        metavar="PATTERN",
        help="Reindex VTT files and exit. Optional PATTERN for case-insensitive wildcard filtering "
        "(e.g., '202510 kontakt' matches files containing both terms, spaces act as wildcards)",
    )
    args = parser.parse_args()
    init_database()
    if args.reindex is not None:
        reindex(args.root, args.reindex)
    try:
        iface, port = args.iface.split(":")
    except:
        exit(1)
    uvicorn.run(app, host=iface, port=int(port))


if __name__ == "__main__":
    main()
