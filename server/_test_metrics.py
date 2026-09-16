"""Unit tests for server/metrics.py — real SQLite, tmp file, no mocks needed."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "test_metrics.sqlite3")

import config
config.METRICS_DB_PATH = db_path

import metrics
import importlib
importlib.reload(metrics)

metrics.init_db()

# --- basic insert + row exists ---
rid = metrics.log_classification(
    provider="daemon2", mode="auto", client_key="1.2.3.4", phash="abc123",
    cache_hit=False, ocr_used=True, vlm_ran=True,
    ocr_seconds=0.5, vlm_seconds=20.1, total_seconds=20.6,
    is_meme=True, filename_slug="test-slug",
)
assert rid is not None and rid > 0
print("basic insert OK, row id:", rid)

import sqlite3
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM classification_log WHERE id=?", (rid,)).fetchone()
assert row["provider"] == "daemon2"
assert row["cache_hit"] == 0
assert row["is_meme"] == 1
assert row["filename_slug"] == "test-slug"
assert row["manual_override_is_meme"] is None
print("row contents OK:", dict(row))

# --- cache hit row ---
rid2 = metrics.log_classification(
    provider="daemon2", mode="auto", phash="abc123", cache_hit=True,
    cache_hamming_distance=3, is_meme=True, filename_slug="test-slug",
)
row2 = conn.execute("SELECT * FROM classification_log WHERE id=?", (rid2,)).fetchone()
assert row2["cache_hit"] == 1 and row2["cache_hamming_distance"] == 3
print("cache-hit row OK")

# --- correction row ---
rid3 = metrics.log_classification(
    provider="daemon2", mode="manual", was_correction=True,
    previous_wrong_slug="wrong-old-slug", correction_was_cache_hit=True,
    is_meme=True, filename_slug="corrected-slug",
)
row3 = conn.execute("SELECT * FROM classification_log WHERE id=?", (rid3,)).fetchone()
assert row3["was_correction"] == 1
assert row3["previous_wrong_slug"] == "wrong-old-slug"
assert row3["correction_was_cache_hit"] == 1
print("correction row OK")

# --- manual override ---
ok = metrics.set_manual_override(rid, True)
assert ok is True
row4 = conn.execute("SELECT * FROM classification_log WHERE id=?", (rid,)).fetchone()
assert row4["manual_override_is_meme"] == 1
print("manual override set OK")

ok2 = metrics.set_manual_override(999999, True)  # non-existent row
assert ok2 is False
print("manual override on missing row returns False OK")

# --- never raises even on a bad path ---
config.METRICS_DB_PATH = "/definitely/does/not/exist/metrics.sqlite3"
importlib.reload(metrics)
result = metrics.log_classification(provider="daemon2")
assert result is None  # swallowed, logged, didn't raise
print("failure path swallowed OK (no exception propagated)")

# --- background writer (request path): batched, non-blocking, never raises ---
config.METRICS_DB_PATH = db_path
config.METRICS_BATCH_SIZE = 50
importlib.reload(metrics)
before = conn.execute("SELECT COUNT(*) FROM classification_log").fetchone()[0]
writer = metrics.MetricsWriter()
writer.start()
assert writer.running
for i in range(500):
    writer.record(provider="worker", mode="auto", cache_hit=bool(i % 2), filename_slug=f"w{i}")
assert writer.flush(10)
after = conn.execute("SELECT COUNT(*) FROM classification_log").fetchone()[0]
assert after - before == 500, (before, after)
writer.record(provider="worker", bogus_field=1)  # invalid row: logged, not raised
writer.stop()
assert not writer.running
writer.record(provider="worker")  # after stop: silently ignored
print("MetricsWriter batched 500 rows, tolerant of bad rows OK")

# queue full -> rows dropped, record() never blocks
config.METRICS_QUEUE_SIZE = 1
importlib.reload(metrics)
tiny = metrics.MetricsWriter()
tiny._thread = object()  # pretend running without a consumer
tiny.record(provider="x"); tiny.record(provider="x"); tiny.record(provider="x")
assert tiny._dropped == 2
print("full queue drops instead of blocking OK")

# unwritable path -> writer disabled, record() is a no-op
config.METRICS_DB_PATH = "/definitely/does/not/exist/metrics.sqlite3"
config.METRICS_QUEUE_SIZE = 10000
importlib.reload(metrics)
dead = metrics.MetricsWriter()
dead.start()
assert not dead.running
dead.record(provider="x")
print("unwritable DB disables writer without raising OK")

conn.close()
print("\nALL METRICS TESTS PASSED")
