# Python Orchestration & Excel Build Scripts

This directory contains the Python scripts used to drive the N×N crawl
(all major Vietnamese cities paired with each other), build flat Excel
workbooks from the raw `trips.jsonl`, and produce an enriched workbook
with full pickup/dropoff point detail.

These scripts are NOT part of the Rust crate — they orchestrate the
pre-built `vexere` binary (`cargo build --release` produces
`target/release/vexere`).

## Requirements

```bash
pip install openpyxl pandas
```

You also need the `vexere` binary on your `PATH` (or edit the
`VEXERE_BIN` constant at the top of each script):

```bash
cd /path/to/trip-crawler
cargo build --release
export PATH="$PWD/target/release:$PATH"
```

## Scripts

### `nxn_crawl.py` — N×N orchestrator

Iterates every ordered (from, to) pair of the 9 major Vietnamese cities
(72 pairs total) and runs `vexere crawl-trips` + `vexere fares` for
each pair across a 3-day window.

```bash
python scripts/nxn_crawl.py \
    --from-date 2026-09-01 \
    --to-date   2026-09-03 \
    --out       /tmp/nxn_out
```

Resumable: pairs with a non-empty `trips.jsonl` are skipped on re-run.
Manifest is written incrementally after every pair — safe to interrupt.

### `build_excel.py` — flat fares workbook

Reads each pair's `fares.csv` (or derives it from `trips.jsonl` if
missing) and produces a single workbook with one sheet per (from, to)
pair plus a summary sheet.

```bash
python scripts/build_excel.py \
    --in-dir   /tmp/nxn_out \
    --manifest /tmp/nxn_out/manifest.json \
    --out      data/vexere_nxn_trips.xlsx
```

Each per-pair sheet has 13 columns: date, departure_time, operator_name,
vehicle_type, original_price_vnd, discount_vnd, final_price_vnd,
available_seats, total_seats, duration_minutes, departure_iso,
arrival_iso, schedule_id.

### `build_excel_enriched.py` — enriched workbook with pickup/dropoff

Reads each pair's raw `trips.jsonl` directly (NOT the derived fares.csv)
so it can attach route-level `pickup_points[]` and `dropoff_points[]`
context to every fare row.

```bash
python scripts/build_excel_enriched.py \
    --in-dir   /tmp/nxn_out \
    --manifest /tmp/nxn_out/manifest.json \
    --out      data/vexere_nxn_trips_enriched.xlsx
```

Each per-pair sheet has 25 columns (vs. 13 in the flat workbook):
- Original 13 fare columns
- `n_pickup_points`, `n_dropoff_points`
- `first_pickup_name`, `first_pickup_lat`, `first_pickup_lon`
- `last_dropoff_name`, `last_dropoff_lat`, `last_dropoff_lon`
- `pickup_points_summary` (multi-line cell with `<name> (<offset>) @ <lat>, <lon> [district]` per line, capped at 12 points + "+N more" suffix)
- `dropoff_points_summary`
- `trip_id`, `route_id`

Also adds a master "Pickup & Dropoff Points" sheet with one row per
distinct point across all 72 pairs (~10,740 rows), deduplicated by
(point_type, name, lat, lon) and sorted by usage count.

## Reproducibility

The two `.xlsx` files in `data/` were produced by running these
scripts against the 72-pair, 3-day crawl from 2026-09-01 to
2026-09-03. To reproduce end-to-end:

```bash
# 1. Build the binary
cargo build --release

# 2. Run the N×N crawl (~25 min, resumable)
python scripts/nxn_crawl.py --out /tmp/nxn_out

# 3. Build the flat workbook
python scripts/build_excel.py \
    --in-dir   /tmp/nxn_out \
    --manifest /tmp/nxn_out/manifest.json \
    --out      data/vexere_nxn_trips.xlsx

# 4. Build the enriched workbook
python scripts/build_excel_enriched.py \
    --in-dir   /tmp/nxn_out \
    --manifest /tmp/nxn_out/manifest.json \
    --out      data/vexere_nxn_trips_enriched.xlsx
```
