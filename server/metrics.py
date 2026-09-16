"""
Append-only metrics log — SQLite, not Redis (see README "Why SQLite here").

One row per /classify or /correct request (cache hit or miss), covering:
mode, provider, per-stage latency, isMeme/filenameSlug, cache_hit + hamming
distance, error, the correction-flow columns (was_correction,
previous_wrong_slug, correction_was_cache_hit) plus a nullable
manual_override_is_meme for later hand-labeled accuracy tracking.

Request path: MetricsWriter.record() only enqueues; one daemon thread per
process owns one connection and writes rows in batches (WAL mode, busy
timeout), so disk latency never reaches the event loop and several uvicorn
workers can share the same database file. A full queue drops rows (logged)
instead of applying backpressure to requests.

Scripts and tests can still call log_classification() for a synchronous
single-row insert that returns the row id.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import random
import sqlite3
import threading
import time

import config

log = logging.getLogger("meme-classifier.metrics")

SCHEMA = """
CREATE TABLE IF NOT EXISTS classification_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                          REAL NOT NULL,
    provider                    TEXT NOT NULL,
    mode                        TEXT,
    client_key                  TEXT,
    phash                       TEXT,
    cache_hit                   INTEGER NOT NULL DEFAULT 0,
    cache_hamming_distance      INTEGER,
    ocr_used                    INTEGER NOT NULL DEFAULT 0,
    vlm_ran                     INTEGER NOT NULL DEFAULT 0,
    ocr_seconds                 REAL,
    vlm_seconds                 REAL,
    total_seconds               REAL,
    is_meme                     INTEGER,
    filename_slug               TEXT,
    error                       TEXT,
    was_correction              INTEGER NOT NULL DEFAULT 0,
    previous_wrong_slug         TEXT,
    correction_was_cache_hit    INTEGER,
    manual_override_is_meme     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_classification_log_ts ON classification_log(ts);
CREATE INDEX IF NOT EXISTS idx_classification_log_provider ON classification_log(provider);
CREATE INDEX IF NOT EXISTS idx_classification_log_cache_hit ON classification_log(cache_hit);
CREATE INDEX IF NOT EXISTS idx_classification_log_was_correction ON classification_log(was_correction);
"""

_INSERT = """INSERT INTO classification_log (
    ts, provider, mode, client_key, phash, cache_hit,
    cache_hamming_distance, ocr_used, vlm_ran, ocr_seconds,
    vlm_seconds, total_seconds, is_meme, filename_slug, error,
    was_correction, previous_wrong_slug, correction_was_cache_hit
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.METRICS_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.METRICS_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(attempts: int = 50) -> None:
    """Create the schema and switch the file to WAL (persistent, so plain
    connections don't repeat it). Several uvicorn workers start at once and
    SQLite answers a contended journal_mode change with an immediate "database
    is locked" instead of waiting on busy_timeout — hence the retry."""
    for attempt in range(attempts):
        try:
            with contextlib.closing(_connect()) as conn:
                if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(0.05 + random.random() * 0.1)


def _row(
    *,
    provider: str,
    mode: str | None = None,
    client_key: str | None = None,
    phash: str | None = None,
    cache_hit: bool = False,
    cache_hamming_distance: int | None = None,
    ocr_used: bool = False,
    vlm_ran: bool = False,
    ocr_seconds: float | None = None,
    vlm_seconds: float | None = None,
    total_seconds: float | None = None,
    is_meme: bool | None = None,
    filename_slug: str | None = None,
    error: str | None = None,
    was_correction: bool = False,
    previous_wrong_slug: str | None = None,
    correction_was_cache_hit: bool | None = None,
) -> tuple:
    return (
        time.time(), provider, mode, client_key, phash, int(cache_hit),
        cache_hamming_distance, int(ocr_used), int(vlm_ran), ocr_seconds,
        vlm_seconds, total_seconds,
        None if is_meme is None else int(is_meme),
        filename_slug, error, int(was_correction), previous_wrong_slug,
        None if correction_was_cache_hit is None else int(correction_was_cache_hit),
    )


def log_classification(**fields) -> int | None:
    """Synchronous single-row insert (scripts/tests). Never raises."""
    try:
        with contextlib.closing(_connect()) as conn:
            cur = conn.execute(_INSERT, _row(**fields))
            conn.commit()
            return cur.lastrowid
    except Exception:  # noqa: BLE001
        log.exception("Failed to write metrics row (non-fatal)")
        return None


def set_manual_override(row_id: int, is_meme: bool) -> bool:
    """Hand-label a row for accuracy tracking (analyze_metrics.py reads this)."""
    try:
        with contextlib.closing(_connect()) as conn:
            cur = conn.execute(
                "UPDATE classification_log SET manual_override_is_meme = ? WHERE id = ?",
                (int(is_meme), row_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        log.exception("Failed to set manual override for row %s", row_id)
        return False


_STOP = object()


class MetricsWriter:
    """Batched background writer used on the request path."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=max(1, config.METRICS_QUEUE_SIZE))
        self._thread: threading.Thread | None = None
        self._dropped = 0

    def start(self) -> None:
        if not config.METRICS_ENABLED or self._thread is not None:
            return
        try:
            init_db()
        except Exception:  # noqa: BLE001
            log.exception("Metrics DB init failed at %s; metrics disabled", config.METRICS_DB_PATH)
            return
        self._thread = threading.Thread(target=self._run, name="metrics-writer", daemon=True)
        self._thread.start()

    def record(self, **fields) -> None:
        """Enqueue one row. Never blocks, never raises."""
        if self._thread is None:
            return
        try:
            self._q.put_nowait(_row(**fields))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning("Metrics queue full; dropped %d rows so far", self._dropped)
        except Exception:  # noqa: BLE001
            log.exception("Invalid metrics row (non-fatal)")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued row has been written (tests, shutdown)."""
        if self._thread is None:
            return True
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._q.unfinished_tasks

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self.flush(timeout)
        self._q.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def _run(self) -> None:
        conn = None
        while True:
            item = self._q.get()
            batch, stop = [], item is _STOP
            if not stop:
                batch.append(item)
            while not stop and len(batch) < config.METRICS_BATCH_SIZE:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is _STOP:
                    stop = True
                else:
                    batch.append(item)
            if batch:
                try:
                    if conn is None:
                        conn = _connect()
                    with conn:
                        conn.executemany(_INSERT, batch)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to write %d metrics rows (non-fatal)", len(batch))
                    if conn is not None:
                        with contextlib.suppress(Exception):
                            conn.close()
                    conn = None
            for _ in range(len(batch) + (1 if stop else 0)):
                self._q.task_done()
            if stop:
                if conn is not None:
                    conn.close()
                return
