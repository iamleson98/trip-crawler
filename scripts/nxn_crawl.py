#!/usr/bin/env python3
"""N×N crawl orchestrator.

For every ordered pair (from_area, to_area) — excluding self-pairs — across
9 major Vietnamese cities, call `vexere crawl-trips` for a 3-day window,
writing each pair's trips.jsonl into its own subdirectory. After all pairs
finish, call `vexere fares` on each pair's dir to derive the flat fares.csv.

Output layout:
  /tmp/nxn_out/
    24_50/
      trips.jsonl
      fares.csv
      fares.jsonl
    24_482/
      ...
    manifest.json

Manifest captures per-pair: from_id, to_id, from_name, to_name, n_trips,
n_fares, elapsed_seconds, returncode.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — 9 major Vietnamese cities + 3-day window.
# ---------------------------------------------------------------------------

AREAS = [
    (24,  "Hà Nội"),
    (29,  "Hồ Chí Minh"),
    (15,  "Đà Nẵng"),
    (27,  "Hải Phòng"),
    (705, "Huế"),
    (457, "Đà Lạt"),
    (482, "Sa Pa"),
    (417, "Nha Trang"),
    (278, "Hà Giang"),
]

DATES = ["2026-10-10", "2026-10-11", "2026-10-12"]
VEXERE_BIN = "/home/z/my-project/trip-crawler-clone/target/release/vexere"
OUT_ROOT = Path("/tmp/nxn_out")
OUT_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Pair enumeration — N×N excluding self-pairs.
# ---------------------------------------------------------------------------

PAIRS = [
    (a, b)
    for a in AREAS
    for b in AREAS
    if a[0] != b[0]
]
# 9 × 8 = 72 ordered pairs

# ---------------------------------------------------------------------------
# Crawl loop.
# ---------------------------------------------------------------------------

def run_pair(from_area: tuple[int, str], to_area: tuple[int, str]) -> dict:
    """Run `vexere crawl-trips` for one pair, then `vexere fares` on the
    output dir. Returns a manifest record."""
    from_id, from_name = from_area
    to_id, to_name = to_area
    pair_label = f"{from_id}_{to_id}"
    pair_dir = OUT_ROOT / pair_label
    pair_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n==> [{from_name} ({from_id}) -> {to_name} ({to_id})]", flush=True)
    t0 = time.time()

    # Phase 1: crawl-trips for 3 days → writes trips.jsonl in pair_dir.
    crawl_cmd = [
        VEXERE_BIN, "crawl-trips",
        "--from", str(from_id),
        "--to", str(to_id),
        "--from-date", DATES[0],
        "--to-date", DATES[-1],
        "--out", str(pair_dir),
    ]
    print(f"    crawl-trips: {' '.join(crawl_cmd[6:])}", flush=True)
    crawl = subprocess.run(
        crawl_cmd,
        capture_output=True,
        text=True,
        timeout=900,  # 15 min per pair — should be way more than enough
    )
    if crawl.returncode != 0:
        print(f"    FAIL crawl: {crawl.stderr.strip()[-300:]}", flush=True)
        return {
            "from_id": from_id, "from_name": from_name,
            "to_id": to_id, "to_name": to_name,
            "pair_dir": str(pair_dir),
            "n_trips": 0, "n_fares": 0,
            "elapsed_seconds": round(time.time() - t0, 1),
            "returncode": crawl.returncode,
            "error": crawl.stderr.strip()[-300:],
        }
    print(f"    crawl done: {crawl.stderr.strip().splitlines()[-1] if crawl.stderr else ''}",
          flush=True)

    # Phase 2: derive fares.csv from the trips.jsonl we just wrote.
    fares_cmd = [
        VEXERE_BIN, "fares",
        "--dir", str(pair_dir),
        "--force",
    ]
    fares = subprocess.run(
        fares_cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if fares.returncode != 0:
        print(f"    WARN fares: {fares.stderr.strip()[-300:]}", flush=True)

    # Count trips + fares.
    trips_path = pair_dir / "trips.jsonl"
    fares_path = pair_dir / "fares.csv"
    n_trips = sum(1 for _ in open(trips_path)) if trips_path.exists() else 0
    n_fares = 0
    if fares_path.exists():
        with open(fares_path) as f:
            n_fares = max(0, sum(1 for _ in f) - 1)  # subtract header

    elapsed = round(time.time() - t0, 1)
    print(f"    summary: {n_trips} trips, {n_fares} fares in {elapsed}s", flush=True)

    return {
        "from_id": from_id, "from_name": from_name,
        "to_id": to_id, "to_name": to_name,
        "pair_dir": str(pair_dir),
        "n_trips": n_trips,
        "n_fares": n_fares,
        "elapsed_seconds": elapsed,
        "returncode": 0,
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0,
                        help="start pair index (0-based, inclusive)")
    parser.add_argument("--end", type=int, default=None,
                        help="end pair index (0-based, exclusive)")
    args = parser.parse_args()

    pairs_slice = PAIRS[args.start:args.end]
    total = len(PAIRS)
    print(f"==> N×N crawl: {len(pairs_slice)} pairs (of {total} total) × "
          f"{len(DATES)} days = {len(pairs_slice) * len(DATES)} (route, date) combos")
    print(f"    output root: {OUT_ROOT}")
    print(f"    binary: {VEXERE_BIN}")
    print(f"    dates: {DATES[0]} → {DATES[-1]}")

    # Resume support: if manifest.json exists, skip pairs that already have
    # a non-zero trips.jsonl file.
    manifest_path = OUT_ROOT / "manifest.json"
    completed: dict[tuple, dict] = {}  # keyed by (from_id, to_id)
    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                prev = json.load(f)
            for rec in prev:
                key = (rec["from_id"], rec["to_id"])
                # Only mark as completed if trips.jsonl still exists and is non-empty.
                tpath = Path(rec["pair_dir"]) / "trips.jsonl"
                if tpath.exists() and tpath.stat().st_size > 0:
                    completed[key] = rec
            print(f"    resuming — {len(completed)} pairs already done")
        except Exception as e:
            print(f"    WARN reading manifest: {e}; starting fresh")

    results: list[dict] = list(completed.values())
    pending = [p for p in pairs_slice if (p[0][0], p[1][0]) not in completed]
    print(f"    pending: {len(pending)} pairs\n")

    for i, (from_area, to_area) in enumerate(pending, 1):
        print(f"\n========== pair {i}/{len(pending)} (global {args.start + i}/{total}) ==========",
              flush=True)
        rec = run_pair(from_area, to_area)
        results.append(rec)
        # Incremental manifest save — survive a crash.
        # Dedup by (from_id, to_id) so the latest result wins.
        by_key = {(r["from_id"], r["to_id"]): r for r in results}
        results = list(by_key.values())
        with open(manifest_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    # Final summary.
    total_trips = sum(r["n_trips"] for r in results)
    total_fares = sum(r["n_fares"] for r in results)
    total_secs = sum(r["elapsed_seconds"] for r in results)
    print(f"\n==> DONE — {len(results)} pairs crawled (cumulative)")
    print(f"    total trips: {total_trips}")
    print(f"    total fares: {total_fares}")
    print(f"    total time:  {total_secs:.0f}s ({total_secs/60:.1f}m)")
    print(f"    manifest:    {manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
