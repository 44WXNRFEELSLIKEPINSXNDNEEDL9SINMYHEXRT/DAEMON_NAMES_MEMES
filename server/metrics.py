"""
Append-only metrics log — SQLite, not Redis (see README "Why SQLite here").

One row per /classify request (cache hit or miss), covering: mode, provider,
per-stage latency, isMeme/filenameSlug, cache_hit + hamming distance, error,
and the correction-flow columns (was_correction, previous_wrong_slug) plus a
nullable manual_override_is_meme for later hand-labeled accuracy tracking.

Single-writer, WAL mode, one connection per call (SQLite handles concurrent
readers fine; writes are serialized by SQLite itself under WAL — acceptable
for this write pattern, which is "one INSERT per classify request", not a
high-concurrency OLTP workload).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
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


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.METRICS_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.METRICS_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with contextlib.closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def log_classification(
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
) -> int | None:
    """Insert one row. Never raises — a metrics-log failure must not break
    the actual classification response; logged and swallowed."""
    try:
        with contextlib.closing(_connect()) as conn:
            cur = conn.execute(
                """INSERT INTO classification_log (
                    ts, provider, mode, client_key, phash, cache_hit,
                    cache_hamming_distance, ocr_used, vlm_ran, ocr_seconds,
                    vlm_seconds, total_seconds, is_meme, filename_slug, error,
                    was_correction, previous_wrong_slug, correction_was_cache_hit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(), provider, mode, client_key, phash, int(cache_hit),
                    cache_hamming_distance, int(ocr_used), int(vlm_ran), ocr_seconds,
                    vlm_seconds, total_seconds,
                    None if is_meme is None else int(is_meme),
                    filename_slug, error, int(was_correction), previous_wrong_slug,
                    None if correction_was_cache_hit is None else int(correction_was_cache_hit),
                ),
            )
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
