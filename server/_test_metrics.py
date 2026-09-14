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

conn.close()
print("\nALL METRICS TESTS PASSED")
