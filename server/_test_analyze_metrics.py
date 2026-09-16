"""Populate a richer test DB and run analyze_metrics.py against it end-to-end."""
import sys, os, tempfile, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "rich_metrics.sqlite3")

import config
config.METRICS_DB_PATH = db_path
import metrics
import importlib
importlib.reload(metrics)
metrics.init_db()

random.seed(42)

# 50 cache misses (fresh classify calls), varying latency + occasional error
for i in range(50):
    metrics.log_classification(
        provider="daemon2", mode="auto" if i % 3 else "manual",
        client_key=f"1.2.3.{i%5}", phash=f"hash{i:04d}",
        cache_hit=False, ocr_used=(i % 2 == 0), vlm_ran=(i % 3 != 0),
        ocr_seconds=random.uniform(0.1, 1.0),
        vlm_seconds=random.uniform(10, 30) if i % 3 != 0 else None,
        total_seconds=random.uniform(10, 31),
        is_meme=(i % 4 != 0), filename_slug=f"slug-{i}",
        error="bad_model_output" if i == 7 else None,
    )

# 20 cache hits, varying hamming distance
for i in range(20):
    metrics.log_classification(
        provider="daemon2", mode="auto", client_key=f"5.5.5.{i%3}",
        phash=f"hash{i:04d}", cache_hit=True,
        cache_hamming_distance=random.choice([0, 1, 2, 3, 5, 8]),
        is_meme=True, filename_slug=f"slug-{i}", total_seconds=0.05,
    )

# 5 corrections: 3 from cache-hit scenario, 2 from fresh-call scenario
for i in range(3):
    metrics.log_classification(
        provider="daemon2", mode="manual", was_correction=True,
        correction_was_cache_hit=True, previous_wrong_slug=f"wrong-{i}",
        is_meme=True, filename_slug=f"fixed-{i}", total_seconds=15.0,
    )
for i in range(2):
    metrics.log_classification(
        provider="daemon2", mode="auto", was_correction=True,
        correction_was_cache_hit=False, previous_wrong_slug=f"wrongfresh-{i}",
        is_meme=True, filename_slug=f"fixedfresh-{i}", total_seconds=18.0,
    )

# hand-label a few rows for the accuracy section
import sqlite3
conn = sqlite3.connect(db_path)
ids = [r[0] for r in conn.execute("SELECT id FROM classification_log WHERE cache_hit=0 AND was_correction=0 LIMIT 10")]
conn.close()
for i, rid in enumerate(ids):
    metrics.set_manual_override(rid, is_meme=(i % 5 != 0))  # a few disagreements

print(f"Populated {db_path} with test rows.")
print("---- Running analyze_metrics.py ----")

import subprocess
result = subprocess.run(
    [sys.executable, "analyze_metrics.py", "--db", db_path],
    capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
)
print(result.stdout)
if result.returncode != 0:
    print("STDERR:", result.stderr)
    sys.exit(1)
assert "== Cache ==" in result.stdout
assert "== Latency (seconds) ==" in result.stdout
assert "== Corrections" in result.stdout
assert "Scenario A" in result.stdout and "Scenario B" in result.stdout
assert "== Accuracy" in result.stdout
print("\nanalyze_metrics.py END-TO-END TEST PASSED")
