# DatXeVui Data Migration — trip-crawler → rust-be-template

One-time migration of the crawled VeXeRe dataset (brands, routes,
schedules, stops, prices, pictures) into the production booking service
at **https://datxevui.com** through its admin REST API, plus one
direct-DB step for the seat maps and one bulk pre-warm for trips.

## What it migrated

| Target table | Rows | Source |
|---|---|---|
| `brand` | 306 | `booking_seed.sqlite` (deduped operators) + `brand-research/final_brands.json` (ratings, phones, offices → description) |
| `address` | 3,832 | per-brand stop points (2 km geo-clustered), diacritics restored, `address` = "ward, province" |
| `route` | 1,093 | (brand, from-city, to-city); names "Đà Nẵng → Hà Nội" from the app's own city table |
| `schedule` | 9,676 | recurring services (route, dep-time, layout); **exact list prices** from the crawl |
| `schedule_point` | 63,786 | ordered pickup/drop sequence with absolute `HH:MM` arrival times |
| `pickup_point` | 7,222 | route-level boarding points (booking flow needs them) |
| `bus_layout` | 525 | vehicle layouts with exact seat counts (direct DB step) |
| `seat` | 12,996 | generated seat maps (A/B berths for sleepers, A–D for seated) |
| `route_picture` | 9,783 | de-watermarked vehicle images + brand covers (multipart upload → RustFS CDN) |
| `trip_session` (warm) | 67,679 | pre-materialized trips for the next 7 days |
| `seat_inventory` (warm) | 1,520,688 | bookable seats for the warm trips |

Everything above was verified through the public API: search returns
trips with exact prices, trip detail shows the seat map + stops +
pictures, and a full booking hold (2 seats → `AB-HSN8GZ`) round-trips
correctly.

## Files

| File | Purpose |
|---|---|
| `vi.py` | Vietnamese diacritics restoration tables (25 provinces, 199 wards) + text helpers |
| `prepare.py` | Builds `migration_plan.json` from the seed DB + research + media manifest — every formatting fix lives here |
| `api_client.py` | Admin-API client: login (`X-Client: mobile` bypasses the UA/origin gates), pacing, retries, multipart upload |
| `run_migration.py` | Phased runner — see below |
| `warm_trips.py` | Generates bulk SQL that pre-materializes trips for the next N days |
| `ssh_run.py` | Tiny paramiko wrapper used for the server-side steps |
| `cleanup_brands.py` | Emergency: delete every brand on the server + reset state |

## Run order

```bash
cd scripts/migration
export DXV_EMAIL='…' DXV_PASSWORD='…'   # admin account

python3 prepare.py                       # → migration_plan.json
python3 run_migration.py --phase brands   # 306 brands via POST /api/admin/brands
python3 run_migration.py --phase db-layouts   # → layouts_seats.sql

# ── direct-DB step (bus layouts + seats have no admin API) ──────────
# 1. copy rustqlite-cli (built from the rust-sql repo, target/release/)
#    + layouts_seats.sql to the server, e.g. /opt/migration/
# 2. stop the backend, backup, import, fix ownership, restart:
python3 ssh_run.py "docker service scale datxevui_backend=0 --detach"
python3 ssh_run.py --put layouts_seats.sql /opt/migration/layouts_seats.sql
python3 ssh_run.py "/opt/migration/rustqlite-cli --import /opt/migration/layouts_seats.sql /var/lib/docker/volumes/datxevui_vexevn-data/_data/app.db"
python3 ssh_run.py "chown 1000:999 /var/lib/docker/volumes/datxevui_vexevn-data/_data/app.db* && docker service scale datxevui_backend=1 --detach"

python3 run_migration.py --phase data --workers 6 --rps 40   # addresses → routes → schedules → pickup points
python3 run_migration.py --phase pictures --workers 6 --rps 40
python3 run_migration.py --phase verify

# ── optional but recommended: pre-warm trips (see warm_trips.py) ────
python3 warm_trips.py --days 7            # → warm_trips.sql, apply like layouts_seats.sql
```

## Design decisions

* **Schedules are recurring.** The crawl window was only 3 days
  (Tue–Thu); the observed weekday set is a crawl artifact, so schedules
  are pushed with `daysOfWeek = null` (daily) and no effective window —
  exactly how a booking site must behave.
* **Prices are exact.** `base_price_adult` = the operator's list price
  from the crawl (min over the observed dates of the same recurring
  service, which the seed builder had already collapsed).
* **Arrival times** `T±Nmin` (relative to departure) were converted to
  absolute `HH:MM` (mod 24 h) — the only format the API accepts.
* **Vehicle classes**: each bus-layout label ("Limousine 24 phòng",
  "Giường nằm 40 chỗ"…) maps to one of the 5 catalog codes
  (`limousine`, `minivan`, `standard`, `semi_sleeper`, `sleeper`) via
  `prepare.py::map_vehicle_code`; the schedule also carries
  `vehicleTypeId` so the public filter works.
* **Bus layouts + seats had to be inserted directly into the DB** — the
  admin API is read-only for them, and booking requires per-seat rows.
  The DB is the rustqlite engine's own file format (`RSQLDB05` magic),
  so the import runs through `rustqlite-cli` (built from the rust-sql
  repo) — plain `sqlite3` cannot open it. UUIDs are written as 16-byte
  `X'…'` blobs because sqlx binds `Uuid` as binary.
* **Trip pre-warming**: trips materialize lazily per search; routes with
  6 000+ schedules exceed any sane request timeout on the first search
  per date. `warm_trips.py` bulk-inserts the exact rows the app would
  generate for the next N days. `SERVER_REQUEST_TIMEOUT_SECS=120` was
  also added to the stack env as a safety net.
* **Idempotency**: every phase records seed-id → API-id maps in
  `migration_state.json` and skips completed work; re-running any phase
  is safe. Brands are matched by slug against the server state.
* **Ownership trap**: files created in the data volume by root (the CLI)
  crash-loop the container (uid 1000 `app` cannot write) — always
  `chown 1000:999 app.db*` after any direct-DB step.

## Bugs found in rust-be-template while migrating

1. `slugify()` in `admin_service.rs` is missing the double-diacritic
   vowels `ấ ế ố ớ ứ` (+ uppercase) from its strip table, so
   "Đất Cảng" slugifies to `d-t-cang` (the vowel disappears entirely).
   Fixed in the rust-be-template repo.
2. `SERVER_REQUEST_TIMEOUT_SECS` was not wired through `deploy/stack.yml`
   (default 30 s) — added as a passthrough with default 120.

## Server-side artifacts

* `/opt/migration/rustqlite-cli` — engine CLI used for the direct-DB steps
* `/opt/migration/layouts_seats.sql`, `warm_trips.sql` — applied imports
* `/opt/migration/app.db.bak-*` — DB backups taken before each direct step
