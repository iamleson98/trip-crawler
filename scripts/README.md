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

### `import_to_sqlite.py` — load trips.jsonl into the booking-app SQLite DB

Reads one or more `trips.jsonl` files (one Trip object per line, as
produced by `vexere crawl-trips`) and idempotently populates the
**dimension** and **inventory** tables of the booking schema.

```bash
# Single pair
python scripts/import_to_sqlite.py \
    --db     path/to/app.db \
    --trips  /tmp/nxn_out/HaNoi_to_SaPa/trips.jsonl

# Whole N×N crawl (using the manifest written by nxn_crawl.py)
python scripts/import_to_sqlite.py \
    --db       path/to/app.db \
    --manifest /tmp/nxn_out/manifest.json \
    --batch-size 5000

# Glob a directory of pair subdirs
python scripts/import_to_sqlite.py \
    --db   path/to/app.db \
    --dir  /tmp/nxn_out

# Dry-run (parses + collects dimensions + prints stats; no DB writes)
python scripts/import_to_sqlite.py \
    --db      /tmp/test.db \
    --trips   /tmp/foo/trips.jsonl \
    --dry-run

# Create tables in a fresh DB (also creates a minimal `booking` stub so
# seat_inventory's FK to `booking.id` resolves)
python scripts/import_to_sqlite.py \
    --db            path/to/fresh.db \
    --trips         /tmp/foo/trips.jsonl \
    --init-schema

# Force a full refresh (drops all rows with vx_* IDs, then re-imports)
python scripts/import_to_sqlite.py \
    --db            path/to/app.db \
    --trips         /tmp/foo/trips.jsonl \
    --drop-existing
```

#### Field mapping

| VeXeRe source field        | Destination table     | Notes |
|----------------------------|-----------------------|-------|
| `company`                  | `brand`               | dedup by `company.id`; `slug` auto-derived from name |
| `route.from` / `route.to` | `place`               | dedup by `area_id`; `osm_id` = 1B + area_id (sentinel range, avoids collision with real OSM IDs) |
| `(brand, from, to)`        | `route`               | dedup by trio; one route per brand's from-to offer |
| `(brand, vehicle_type, total_seats)` | `bus_layout` | dedup by trio |
| `1..total_seats` per layout | `seat`               | synthesized labels `1A 1B 1C 1D 2A ...`; 4-wide, 2+aisle+2; window seats flagged |
| `route.pickup_points[]`    | `pickup_point`        | `kind='pickup'`; dedup by `(route_id, point_id)` |
| `route.dropoff_points[]`   | `pickup_point`        | `kind='dropoff'` |
| `(vexere_route_id, dep_time, seat_template_id)` | `schedule` | recurring template; effective_from/to derived from observed dates |
| `(schedule, departure_date)` | `trip_session`      | one specific departure; VeXeRe returns same departure multiple times with different pickup/dropoff combos — these collapse to one `trip_session` |
| `(trip_session, seat)`     | `seat_inventory`      | pre-populated `status='available'`; `base_price` from schedule fare; `final_price = base - max(0, discount)` |

#### ID strategy

All imported rows use VeXeRe IDs prefixed with `vx_*` to avoid collision
with future app-generated UUIDs:

| Table          | ID format                                            |
|----------------|------------------------------------------------------|
| `place`        | `vx_place_<area_id>` (e.g. `vx_place_24`)            |
| `brand`        | `vx_brand_<company_id>`                              |
| `route`        | `vx_route_<brand>_<from>_<to>`                       |
| `bus_layout`   | `vx_bl_<brand>_<vehicle_slug>_<seats>`              |
| `seat`         | `vx_seat_<layout>_<row><col_letter>`                |
| `pickup_point` | `vx_pp_<route>_<point_id>`                           |
| `schedule`     | `vx_sch_<vexere_route>_<dep_time_slug>_<seat_tpl>`  |
| `trip_session` | `vx_ts_<vexere_route>_<dep_time_slug>_<seat_tpl>_<date>` |
| `seat_inventory` | `vx_si_<trip_session>_<seat_label>`               |

This makes it trivial to distinguish imported rows from app-generated
rows: `WHERE id LIKE 'vx_%'`. The `--drop-existing` flag uses this to
wipe imported data without touching operational rows (bookings,
payments, reviews, etc.).

#### Idempotency

- Dimension tables use `INSERT OR IGNORE` — re-runs don't duplicate.
- `trip_session` uses `INSERT OR REPLACE` keyed by id — re-runs refresh
  `available_seats` / `total_seats` without touching bookings.
- `seat_inventory` uses `INSERT OR IGNORE` keyed by `(trip_session_id, seat_id)`.
  Existing rows (e.g. ones already marked 'held' or 'sold' by the app)
  are preserved. To force a full refresh, pass `--drop-existing`.
- `brand.total_trips` is recomputed and `UPDATE`d on every run.

#### What it does NOT touch

The importer only writes to: `place`, `brand`, `route`, `bus_layout`,
`seat`, `pickup_point`, `schedule`, `trip_session`, `seat_inventory`.

It does NOT write to (and never will): `user`, `booking`, `booking_seat`,
`payment`, `campaign`, `discount_program`, `review`, `wishlist_item`,
`price_alert`, `notification`, `chat_*`, `null_claw_exchange`,
`audit_log`, `posts`, `roles`, `permissions`, `role_permissions`,
`user_roles`, `user_verification`, `refresh_tokens`, `seaql_migrations`.

Those tables are operational and reserved for the live app.

#### Verified against the sample dataset

Tested end-to-end on `data/trips-sample/trips.jsonl` (Hà Nội → Sa Pa,
968 trips, 4 days):

- 968 trips parsed → 700 unique `trip_session` rows (268 duplicates
  collapsed: same physical departure, different boarding/dropoff point
  combos in VeXeRe's API response).
- 26 brands, 51 routes, 44 bus_layouts, 1,040 seats, 318 pickup_points,
  243 schedules.
- 16,562 `seat_inventory` rows (sum of total_seats across trip_sessions).
- Re-run preserves all row counts (idempotent).
- `--drop-existing` followed by re-import produces identical counts.

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

# 5. (Optional) Populate a booking-app SQLite DB with the crawled catalog
python scripts/import_to_sqlite.py \
    --db       path/to/app.db \
    --manifest /tmp/nxn_out/manifest.json \
    --init-schema  # only if the DB is empty
```
