"""
CLI metrics report over the SQLite classification_log.

Usage:
    python analyze_metrics.py [--db /data/metrics.sqlite3] [--days N]

Reports:
  - cache hit rate + Hamming-distance distribution for actual hits
  - OCR fallback-to-VLM rate (manual mode: how often OCR confidence was too
    low and the VLM had to generate the slug instead)
  - latency p50/p95/p99 per stage (ocr, vlm, total)
  - false-negative rate for is_meme wherever manual_override_is_meme is set
  - correction rate overall, and broken out by cache_hit vs fresh-call
    scenario (a skewed rate on one side points at a specific fix: tighten
    the Hamming threshold if cache-hit corrections dominate, work on the
    prompt/model if fresh-call corrections dominate)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def fetch_rows(db_path: str, since_ts: float | None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM classification_log"
    params: tuple = ()
    if since_ts is not None:
        q += " WHERE ts >= ?"
        params = (since_ts,)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def report(rows: list[sqlite3.Row]) -> None:
    n = len(rows)
    print(f"Total requests analyzed: {n}")
    if n == 0:
        print("No data — nothing to report.")
        return

    # --- Cache hit rate + Hamming distance distribution for actual hits ---
    hits = [r for r in rows if r["cache_hit"]]
    misses = [r for r in rows if not r["cache_hit"]]
    print(f"\n== Cache ==")
    print(f"Hit rate: {len(hits)}/{n} ({100*len(hits)/n:.1f}%)")
    dists = [r["cache_hamming_distance"] for r in hits if r["cache_hamming_distance"] is not None]
    if dists:
        print(f"Hamming distance on hits: min={min(dists)} p50={percentile([float(d) for d in dists],0.5):.1f} "
              f"max={max(dists)} (threshold currently in effect at write time varies; "
              f"check server/config.py PHASH_HAMMING_THRESHOLD for the current value)")
        buckets = {}
        for d in dists:
            buckets[d] = buckets.get(d, 0) + 1
        print("Distance histogram:", dict(sorted(buckets.items())))
    else:
        print("No cache hits recorded yet.")

    # --- OCR fallback-to-VLM rate (manual mode) ---
    manual_rows = [r for r in rows if r["mode"] == "manual" and not r["cache_hit"]]
    if manual_rows:
        vlm_fallback = [r for r in manual_rows if r["vlm_ran"]]
        print(f"\n== OCR fast-path vs VLM fallback (manual mode, cache misses only) ==")
        print(f"Manual-mode requests: {len(manual_rows)}")
        print(f"Fell back to VLM: {len(vlm_fallback)} ({100*len(vlm_fallback)/len(manual_rows):.1f}%)")
    else:
        print("\n== OCR fast-path vs VLM fallback ==\nNo manual-mode, non-cached requests recorded.")

    # --- Latency percentiles per stage ---
    print(f"\n== Latency (seconds) ==")
    for stage in ("ocr_seconds", "vlm_seconds", "total_seconds"):
        vals = [r[stage] for r in rows if r[stage] is not None]
        if not vals:
            print(f"{stage}: no data")
            continue
        p50, p95, p99 = percentile(vals, 0.5), percentile(vals, 0.95), percentile(vals, 0.99)
        print(f"{stage}: n={len(vals)} p50={p50:.2f} p95={p95:.2f} p99={p99:.2f} "
              f"mean={statistics.mean(vals):.2f}")

    # --- False-negative rate for is_meme where hand-labeled ---
    labeled = [r for r in rows if r["manual_override_is_meme"] is not None and r["is_meme"] is not None]
    print(f"\n== Accuracy (hand-labeled subset) ==")
    if labeled:
        false_negatives = [r for r in labeled if r["manual_override_is_meme"] == 1 and r["is_meme"] == 0]
        false_positives = [r for r in labeled if r["manual_override_is_meme"] == 0 and r["is_meme"] == 1]
        print(f"Labeled rows: {len(labeled)}")
        print(f"False negatives (missed real memes): {len(false_negatives)} "
              f"({100*len(false_negatives)/len(labeled):.1f}%)")
        print(f"False positives (flagged non-memes): {len(false_positives)} "
              f"({100*len(false_positives)/len(labeled):.1f}%)")
    else:
        print("No rows have manual_override_is_meme set yet — label some with "
              "metrics.set_manual_override(row_id, is_meme) to populate this.")

    # --- Correction rate, overall and by scenario ---
    corrections = [r for r in rows if r["was_correction"]]
    print(f"\n== Corrections (\"Rename last\") ==")
    print(f"Overall correction rate: {len(corrections)}/{n} ({100*len(corrections)/n:.1f}%)")
    if corrections:
        cache_hit_corrections = [r for r in corrections if r["correction_was_cache_hit"] == 1]
        fresh_corrections = [r for r in corrections if r["correction_was_cache_hit"] == 0]
        print(f"Scenario A (was a cache hit): {len(cache_hit_corrections)} "
              f"({100*len(cache_hit_corrections)/len(corrections):.1f}% of corrections)")
        print(f"Scenario B (was a fresh model call): {len(fresh_corrections)} "
              f"({100*len(fresh_corrections)/len(corrections):.1f}% of corrections)")
        # Rate relative to each scenario's own volume, not just share of corrections.
        total_cache_hits = len([r for r in rows if r["cache_hit"]])
        total_fresh = len([r for r in rows if not r["cache_hit"] and not r["was_correction"]])
        if total_cache_hits:
            print(f"Correction rate AMONG cache hits: {len(cache_hit_corrections)}/{total_cache_hits} "
                  f"({100*len(cache_hit_corrections)/total_cache_hits:.1f}%) "
                  f"— high here means PHASH_HAMMING_THRESHOLD is too loose (false cache matches)")
        if total_fresh:
            print(f"Correction rate AMONG fresh calls: {len(fresh_corrections)}/{total_fresh} "
                  f"({100*len(fresh_corrections)/total_fresh:.1f}%) "
                  f"— high here means the base prompt/model needs work, not the cache")
    else:
        print("No corrections recorded yet.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("METRICS_DB_PATH", "/data/metrics.sqlite3"))
    ap.add_argument("--days", type=float, default=None, help="only rows from the last N days")
    args = ap.parse_args()

    since_ts = time.time() - args.days * 86400 if args.days else None
    try:
        rows = fetch_rows(args.db, since_ts)
    except sqlite3.OperationalError as e:
        print(f"Could not read {args.db}: {e}", file=sys.stderr)
        sys.exit(1)

    report(rows)


if __name__ == "__main__":
    main()
