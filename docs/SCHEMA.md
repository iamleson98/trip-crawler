# Output Schemas

This document is the **authoritative field reference** for every file a
`vexere` crawl run produces. Use it to build your downstream tables, write
`COPY` statements, or sanity-check a dataset you've been handed.

> **TL;DR** — A single `vexere crawl-trips` run produces six files. One raw
> nested source-of-truth (`trips.jsonl`), two flat dimension tables
> (`brands.*`), two flat fact tables (`fares.*`), and two pre-aggregated
> per-route summaries (`summaries.*` / `routes.*`). The flat tables are
> derived views — you can always rebuild them from `trips.jsonl` alone via
> `vexere brands` / `vexere fares` / `vexere routes`.

---

## File inventory

| File | Format | Grain | Produced by | Purpose |
|------|--------|-------|-------------|---------|
| `trips.jsonl` | JSONL only | one row per `Trip` | `crawl-trips`, `crawl-all` | Raw, full-nested VeXeRe record. Source of truth — preserve for re-processing. |
| `brands.jsonl` + `brands.csv` | both | one row per unique operator | `crawl-trips`, `crawl-all`, `brands`, `summary` | `dim_brands` — deduplicated by `id`. Joins to `fares.operator_id`. |
| `fares.jsonl` + `fares.csv` | both | one row per `(trip, schedule)` | `crawl-trips`, `crawl-all`, `fares`, `summary` | `fact_fares` — every bookable departure with all price + seat info flattened. **The main analysis table.** |
| `summaries.jsonl` + `summaries.csv` | both | one row per `(from, to, date)` | `crawl-trips`, `crawl-all`, `summary` | Pre-computed per-route aggregates. Stamped from the live search filter. |
| `routes.jsonl` + `routes.csv` | both | one row per `(from, to, date)` | `routes` | Same grain as `summaries` but derived from `id_index` parsing. Useful for inspecting a `--raw-only` dataset. |
| `areas.jsonl` + `areas.csv` | both | one row per area | `crawl-all` | Area detail records for the seed list of popular routes. |

> Why does `trips.jsonl` only come as JSONL? Because each `Trip` carries a
> `route.schedules[].extra` and `Trip.extra` catch-all `serde_json::Value`
> field — flattening that to CSV would either lose data or produce
> unpredictable columns. The flat `fares` table is the clean CSV view of
> the same data.

---

## `trips.jsonl` — raw nested source of truth

One `Trip` per line. Schema (abbreviated — see `src/models/trip.rs` for the
authoritative Rust types):

```jsonc
{
  "idIndex": "401932250_2026-09-01_23:00_24_38",   // composite key
  "route": {
    "id": 34608615,
    "name": " Bắc Giang-Sapa ",
    "pickup_points": [{ /* PickupPoint */ }],
    "dropoff_points": [{ /* PickupPoint */ }],
    "distance": 320.5,                              // km
    "duration": 360,                                // minutes (sometimes null)
    "schedules": [
      {
        "trip_code": "abc123",                      // ★ schedule token (= fares.schedule_id)
        "hour": "23", "minute": "00",
        "pickup_date": "2026-09-01T23:00:00+07:00",  // ISO 8601 departure
        "arrival_time": "2026-09-02T05:00:00+07:00", // ISO 8601 arrival
        "fare": {
          "original": 450000,                        // ★ sticker price VND
          "discount": -1,                             // ★ discount amount (-1 = none)
          "max":      780000                         // ★ ceiling for sorting
        },
        "available_seats": 12,
        "total_seats": 24,
        "vehicle_type": "Limousine 24 chỗ",
        "seat_type": 2,                               // 1=seat, 2=bed, ...
        "seat_template_name": "Limousine 24",
        "...": "many more fields, see Schedule struct"
      }
    ]
  },
  "company": {
    "id": 53096,                                     // ★ operator ID (= brands.id)
    "name": "FUTA HÀ SƠN",
    "name_slug": "futa-ha-son",
    "logo": "https://...",
    "rating": 4.5,
    "rating_count": 200,
    "phone": "...", "hotline": "...", "address": "..."
  },
  "score": 0.5,                                      // search-ranking score
  "discount_score": 0.1,
  "coupon_score": 0.0,
  "is_deal_1111": false,
  "speaking_english_utility": false,
  "...": "many more ranking + diversity + commission fields"
}
```

### The `id_index` composite key

Shape: `"<route_id>_<YYYY-MM-DD>_<HH:MM>_<from_area_id>_<to_area_id>"`.

| Segment | Example | Meaning |
|---------|---------|---------|
| 1 | `401932250` | Internal route ID |
| 2 | `2026-09-01` | Departure date |
| 3 | `23:00` | Departure time `HH:MM` |
| 4 (penultimate) | `24` | From-area ID |
| 5 (last) | `38` | To-area ID |

> Note: the from/to in `id_index` are the **point IDs** VeXeRe uses
> internally — these may differ from the original search filter `from`/`to`
> (e.g. searching `to=482` "Sa Pa" returns trips whose `id_index` ends in
> `_38` because 38 is the Lào Cai city ID). Use `route.from.id` /
> `route.to.id` for the actual point the bus serves.

### Type-tolerance notes

VeXeRe's JSON has drifted across microservices. The deserializer handles:

| Field pattern | VeXeRe sends | Rust type |
|--------------|--------------|-----------|
| `parent_id`, `country_id`, `state_id`, `area_id`, `x_migrate_key`, `duplicated_parent_id` | `string \| number \| null` | `FlexString` newtype → normalized `String` |
| `is_deal_1111`, `speaking_english_utility` | `int \| bool` | `bool` |
| `discount_score`, `coupon_score` | `int \| float` | `f64` |
| `commission_score_normalization`, `penalty_score_normalization`, etc. | `int \| float \| null` | `Option<f64>` |

---

## `brands.csv` — operator dimension

One row per unique operator. Deduplicated by `id` with a **merge-with-richest**
strategy: if the same operator appears multiple times with different fields
filled in, we keep the first non-empty value seen for each field.

```csv
id,name,name_slug,logo,description,rating,rating_count,phone,hotline,address
53096,FUTA HÀ SƠN,futa-ha-son,https://...,Hãng xe...,4.5,200,+84...,1900...,Số 1...
```

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGINT PK | VeXeRe operator ID. Joins to `fares.operator_id`. |
| `name` | TEXT | Vietnamese display name (e.g. "FUTA HÀ SƠN"). |
| `name_slug` | TEXT NULL | URL-safe slug. |
| `logo` | TEXT NULL | Logo URL. |
| `description` | TEXT NULL | Short tagline. |
| `rating` | DOUBLE NULL | Average rating 0–5. |
| `rating_count` | INT NULL | Number of ratings. |
| `phone` | TEXT NULL | Office phone. |
| `hotline` | TEXT NULL | Hotline. |
| `address` | TEXT NULL | Office address. |

---

## `fares.csv` — the price fact table

One row per `(trip, schedule)` — i.e. one bookable departure. This is the
table you'll actually analyze. Each row carries the operator (denormalized
for convenience) and all price + seat info flattened from the schedule.

```csv
schedule_id,trip_id,route_id,operator_id,operator_name,from_area_id,to_area_id,date,departure_time,departure_iso,arrival_iso,duration_minutes,vehicle_type,seat_type,total_seats,available_seats,original_price_vnd,discount_vnd,final_price_vnd,max_price_vnd
abc123,401932250_2026-09-01_23:00_24_38,34608615,53096,FUTA HÀ SƠN,24,38,2026-09-01,23:00,2026-09-01T23:00:00+07:00,2026-09-02T05:00:00+07:00,360,Limousine 24 chỗ,2,24,12,450000,-1,450000,780000
```

| Column | Type | Notes |
|--------|------|-------|
| `schedule_id` | TEXT PK | The opaque `trip_code` token VeXeRe uses for booking. Unique per departure. |
| `trip_id` | TEXT | Composite ID — matches `Trip.id_index`. |
| `route_id` | BIGINT | VeXeRe internal route ID. |
| `operator_id` | BIGINT FK | Joins to `brands.id`. |
| `operator_name` | TEXT | Denormalized for convenience (avoid a join when exploring). |
| `from_area_id` | BIGINT | Origin point ID (from the search filter or `id_index` parsing). |
| `to_area_id` | BIGINT | Destination point ID. |
| `date` | DATE | Departure date `YYYY-MM-DD`. |
| `departure_time` | TEXT | `HH:MM` (24h, zero-padded). |
| `departure_iso` | TIMESTAMPTZ | ISO 8601 departure with timezone. |
| `arrival_iso` | TIMESTAMPTZ | ISO 8601 arrival with timezone. |
| `duration_minutes` | INT NULL | `arrival_iso - departure_iso` in minutes. NULL if either timestamp is missing/unparseable. |
| `vehicle_type` | TEXT | e.g. "Limousine 24 chỗ", "Giường nằm 44 chỗ". |
| `seat_type` | SMALLINT | 1 = seat, 2 = bed, etc. |
| `total_seats` | INT | Vehicle capacity. |
| `available_seats` | INT | Currently bookable seats. |
| `original_price_vnd` | BIGINT | Sticker price in đồng. |
| `discount_vnd` | BIGINT | Discount amount. `-1` means "no discount available". |
| `final_price_vnd` | BIGINT | **★ Computed**: `original_price_vnd - max(0, discount_vnd)`. Use this for price analysis. |
| `max_price_vnd` | BIGINT | Ceiling price VeXeRe uses for sorting (usually == `original`). |

### Key invariants

- `final_price_vnd = original_price_vnd - max(0, discount_vnd)` — this is
  enforced at extraction time, so the CSV is always self-consistent.
- `discount_vnd = -1` is treated as "no discount" → `max(0, -1) = 0` →
  `final = original`. This matches VeXeRe's frontend behavior.
- `duration_minutes` is derived; if either ISO timestamp is missing or
  unparseable, the column is NULL (not 0).

---

## `routes.csv` + `summaries.csv` — per-route aggregates

One row per `(from_area_id, to_area_id, date)`. Two files, same shape —
`summaries.*` is produced during a live crawl (stamped from the search
filter values); `routes.*` is derived from `id_index` parsing on disk
(useful for inspecting a `--raw-only` dataset).

```csv
from_area_id,to_area_id,date,n_trips,n_schedules,n_operators,min_price_vnd,max_price_vnd,avg_price_vnd,median_price_vnd,total_available_seats
24,38,2026-09-01,27,27,1,10000,450000,438518.5,450000,658
```

| Column | Type | Notes |
|--------|------|-------|
| `from_area_id` | BIGINT | Origin point ID. |
| `to_area_id` | BIGINT | Destination point ID. |
| `date` | DATE | Departure date. |
| `n_trips` | INT | Number of parent trips (one trip = one row in `trips.jsonl`). |
| `n_schedules` | INT | Number of bookable departures (one per `(trip, schedule)`). |
| `n_operators` | INT | Distinct operators serving this route-day. |
| `min_price_vnd` | BIGINT NULL | Lowest `final_price_vnd` across all schedules. |
| `max_price_vnd` | BIGINT NULL | Highest `final_price_vnd` across all schedules. |
| `avg_price_vnd` | DOUBLE NULL | Arithmetic mean of `final_price_vnd`. |
| `median_price_vnd` | BIGINT NULL | Median of `final_price_vnd` — robust against outliers. |
| `total_available_seats` | BIGINT | Sum of `available_seats` across all schedules. |

### Why both `min_price_vnd` AND `median_price_vnd`?

VeXeRe occasionally lists "teaser" departures at VND 10,000 (a deposit
floor) that distort the min. The median is the more useful "typical price"
for a route; the min tells you whether a teaser exists.

---

## `areas.csv` — area reference

Only produced by `vexere crawl-all` (which seeds a fixed list of popular
areas). One row per area ID. Useful as a `dim_areas` join target for the
`from_area_id` / `to_area_id` columns in `fares` and `routes`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Area ID (sometimes numeric-as-string — the FlexString normalizes it). |
| `name` | TEXT | Vietnamese display name (e.g. "Hà Nội"). |
| `code` | TEXT NULL | IATA-style code (e.g. "HNI"). |
| `parent_id` | TEXT NULL | Parent area ID (e.g. province for a district). |
| `country_id` | TEXT NULL | Country ID (always "1" for Vietnam). |
| `type` | INT | 1 = city/province, 2 = station/stop, 3 = country, etc. |
| `latitude` / `longitude` | DOUBLE NULL | Centroid coordinates. |

---

## Importing into a SQL database

### Postgres / MySQL / DuckDB

```sql
-- Dimension: operators
CREATE TABLE dim_brands (
  id           BIGINT PRIMARY KEY,
  name         TEXT NOT NULL,
  name_slug    TEXT, logo TEXT, description TEXT,
  rating       DOUBLE PRECISION, rating_count INTEGER,
  phone        TEXT, hotline TEXT, address TEXT
);

-- Dimension: areas (only if you've run crawl-all; otherwise derive from
-- route.from / route.to in trips.jsonl)
CREATE TABLE dim_areas (
  id           TEXT PRIMARY KEY,
  name         TEXT, code TEXT,
  parent_id    TEXT, country_id TEXT,
  type         SMALLINT,
  latitude     DOUBLE PRECISION, longitude DOUBLE PRECISION
);

-- Fact: fares (the table you'll actually query)
CREATE TABLE fact_fares (
  schedule_id        TEXT PRIMARY KEY,
  trip_id            TEXT NOT NULL,
  route_id           BIGINT NOT NULL,
  operator_id        BIGINT NOT NULL REFERENCES dim_brands(id),
  from_area_id       BIGINT NOT NULL,
  to_area_id         BIGINT NOT NULL,
  date               DATE NOT NULL,
  departure_time     TEXT,
  departure_iso      TIMESTAMPTZ,
  arrival_iso        TIMESTAMPTZ,
  duration_minutes   INTEGER,
  vehicle_type       TEXT,
  seat_type          SMALLINT,
  total_seats        INTEGER,
  available_seats    INTEGER,
  original_price_vnd BIGINT,
  discount_vnd       BIGINT,
  final_price_vnd    BIGINT,   -- enforced = original - max(0, discount)
  max_price_vnd      BIGINT
);

-- Aggregate: per-route-day summaries
CREATE TABLE agg_route_day (
  from_area_id          BIGINT NOT NULL,
  to_area_id            BIGINT NOT NULL,
  date                  DATE NOT NULL,
  n_trips               INTEGER,
  n_schedules           INTEGER,
  n_operators           INTEGER,
  min_price_vnd         BIGINT,
  max_price_vnd         BIGINT,
  avg_price_vnd         DOUBLE PRECISION,
  median_price_vnd      BIGINT,
  total_available_seats BIGINT,
  PRIMARY KEY (from_area_id, to_area_id, date)
);

-- Bulk load (Postgres syntax)
\copy dim_brands     FROM 'brands.csv'  WITH (FORMAT csv, HEADER true);
\copy dim_areas      FROM 'areas.csv'   WITH (FORMAT csv, HEADER true);
\copy fact_fares     FROM 'fares.csv'   WITH (FORMAT csv, HEADER true);
\copy agg_route_day  FROM 'routes.csv'  WITH (FORMAT csv, HEADER true);
```

### DuckDB (zero-server, fast analytics)

```sql
-- Run directly on the CSVs — no ingestion step needed
INSTALL httpfs;
ATTACH 'fares.csv'    AS fares  (TYPE csv, HEADER true);
ATTACH 'brands.csv'   AS brands (TYPE csv, HEADER true);
ATTACH 'routes.csv'   AS routes (TYPE csv, HEADER true);

-- Top 5 cheapest routes next weekend
SELECT from_area_id, to_area_id, date, median_price_vnd, n_operators
FROM routes
WHERE date >= '2026-09-06'
ORDER BY median_price_vnd ASC
LIMIT 5;

-- Operator market share by departures
SELECT b.name, COUNT(*) AS n_departures, AVG(f.final_price_vnd) AS avg_price
FROM fares f JOIN brands b ON f.operator_id = b.id
GROUP BY b.name
ORDER BY n_departures DESC;
```

### Python (pandas / polars)

```python
import pandas as pd

brands = pd.read_csv('brands.csv')
fares  = pd.read_csv('fares.csv')
routes = pd.read_csv('routes.csv')

# Top 5 operators by number of departures
print(fares.groupby('operator_name').size().sort_values(ascending=False).head())

# Cheapest 5 routes next weekend
print(routes.nsmallest(5, 'median_price_vnd')[
    ['from_area_id', 'to_area_id', 'date', 'median_price_vnd', 'n_operators']
])

# Price distribution for a specific operator
op = fares[fares.operator_name == 'Sao Việt']
print(op.final_price_vnd.describe())
```

---

## Re-deriving tables from `trips.jsonl` alone

If you crawled with `--raw-only` (or only kept `trips.jsonl` to save space),
the flat tables can be rebuilt at any time:

```bash
# All three at once (with summary table):
vexere summary --dir ./trips-out --force

# Granular — just one table at a time:
vexere brands --dir ./trips-out --force
vexere fares  --dir ./trips-out --force
vexere routes --dir ./trips-out --force
```

The `--force` flag overwrites existing files. Without it, the subcommand
no-ops if the target file already exists (so you can re-run safely).

> **Note on `fares` extracted from disk**: When extracted from a raw-only
> dataset, the `(from, to, date)` columns on each `FareRow` are parsed from
> the trip's `id_index`. If `id_index` doesn't match the expected
> `<route_id>_<YYYY-MM-DD>_<HH:MM>_<from>_<to>` shape, the row is stamped
> with `(0, 0, "unknown")` — still emitted, just under the catch-all key.

---

## Real-world dataset sizes

A 3-day crawl of Hà Nội (24) → Sa Pa (482), 2026-09-01 → 2026-09-03,
produced:

| File | Rows | Size |
|------|------|------|
| `trips.jsonl` | 968 trips | 7.6 MB |
| `brands.csv` | 26 unique operators | 905 B |
| `brands.jsonl` | 26 | 4.3 KB |
| `fares.csv` | 968 departures | 281 KB |
| `fares.jsonl` | 968 | 588 KB |
| `routes.csv` | 142 route-days | 9.2 KB |

A 7-day single-route crawl produces ~2,150 trips / ~13 MB `trips.jsonl`.
A full `crawl-all` (10 routes × 7 days) would produce ~20,000 trips.
