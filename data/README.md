# Crawl Datasets & Excel Workbooks

This directory holds the published crawl outputs and Excel workbooks
produced by the Python scripts in `../scripts/`.

> **Data accuracy note (hidden pickup points).** The `/v2/route/` search API
> attaches each operator's *entire* point pool to every trip — e.g. trips of
> "Anh Huy Đất Cảng" (company 12847) carry 1,953 pickup points, while route
> 7275428 (Hải Phòng → Hà Nội) actually serves exactly 1 ("Bến xe Thượng Lý").
> The website only renders points whose top-level `hidden` flag is `0`.
> The crawler now filters hidden points at the API layer
> (`vexerers::api::trips::strip_hidden_points`) and the Excel/SQLite builders
> apply the same filter defensively, so every artifact here matches what
> vexere.com shows.

## Files

| File | Size | Description |
|------|------|-------------|
| `vexere_nxn_manifest.json` | ~16 KB | Inventory of all 72 (from, to) pairs attempted: from/to city IDs, names, trip count, fare count, output paths. |
| `vexere_nxn_trips.xlsx` | ~5.4 MB | **Flat fares workbook** — 62 sheets: 1 summary + 60 per-pair (skips the 12 pairs that had zero direct service). 84,034 fare rows, 13 columns each. |
| `vexere_nxn_trips_enriched.xlsx` | ~10.7 MB | **Enriched workbook** — 62 sheets: 1 summary + 60 per-pair + 1 master "Pickup & Dropoff Points" sheet. 42,017 enriched fares with 25 columns each, plus 6,873 distinct pickup/dropoff points (website-visible only). |

## `trips-sample/` — single-route sample dataset

A complete crawl output for one route (Hà Nội → Sa Pa, 3 days,
2026-09-09 to 2026-09-11). Useful as a small, self-contained fixture
for testing downstream scripts without downloading the full N×N set.

| File | Description |
|------|------|-------------|
| `trips.jsonl` | Raw trips JSONL (777 trips). |
| `trips_sample.jsonl` | 10-trip sample of the above. |
| `fares.csv` / `fares.jsonl` | Derived flat fares table (777 rows). |
| `brands.csv` / `brands.jsonl` | 20 unique operators with trip counts. |
| `summaries.csv` / `summaries.jsonl` | Per-route-day summary (3 rows). |

## Coverage of the N×N dataset

- **9 cities, 72 ordered pairs** (each city paired with every other city).
- **3-day window**: 2026-09-09 → 2026-09-11.
- **60 pairs** have direct service (41,993 trips total).
- **12 pairs** have no direct service — mostly cross-region pairs that
  are geographically far (e.g. Hà Giang → Nha Trang, Đà Lạt → Hà Giang).

Top 5 routes by trip count:

| Route | Trips |
|-------|------|
| Hà Nội → Hải Phòng | 8,067 |
| Hải Phòng → Hà Nội | 5,575 |
| Đà Lạt → Hồ Chí Minh | 3,310 |
| Huế → Đà Nẵng | 3,022 |
| Đà Nẵng → Huế | 2,970 |

## Field schemas

See [`../docs/SCHEMA.md`](../docs/SCHEMA.md) for the authoritative field
reference for every output file (`trips.jsonl`, `fares.csv`, `brands.csv`,
`routes.csv`, `summaries.csv`, `areas.csv`) plus SQL DDL for
Postgres/MySQL/DuckDB and Python pandas recipes.

## Reproducing

See [`../scripts/README.md`](../scripts/README.md) for full end-to-end
reproduction instructions.
