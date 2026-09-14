import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "m2.sqlite3")
import config
config.METRICS_DB_PATH = db_path
import metrics, importlib
importlib.reload(metrics)
metrics.init_db()

# 10 manual requests: 6 ocr-fast (vlm_ran=False), 4 fell back to vlm (vlm_ran=True)
for i in range(6):
    metrics.log_classification(provider="daemon2", mode="manual", cache_hit=False,
                               ocr_used=True, vlm_ran=False, is_meme=True, filename_slug=f"a{i}")
for i in range(4):
    metrics.log_classification(provider="daemon2", mode="manual", cache_hit=False,
                               ocr_used=False, vlm_ran=True, is_meme=True, filename_slug=f"b{i}")

import subprocess
result = subprocess.run([sys.executable, "analyze_metrics.py", "--db", db_path],
                        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
print(result.stdout)
assert "Fell back to VLM: 4 (40.0%)" in result.stdout, result.stdout
print("OCR fallback rate calculation verified correct")
