# Crawl Datasets & Excel Workbooks

This directory holds the published crawl outputs and Excel workbooks
produced by the Python scripts in `../scripts/`.

## Files

| File | Size | Description |
|------|------|-------------|
| `vexere_nxn_manifest.json` | ~16 KB | Inventory of all 72 (from, to) pairs attempted: from/to city IDs, names, trip count, fare count, output paths. |
| `vexere_nxn_trips.xlsx` | ~6.6 MB | **Flat fares workbook** — 62 sheets: 1 summary + 61 per-pair (skips the 11 pairs that had zero direct service). 102,715 fare rows, 13 columns each. |
| `vexere_nxn_trips_enriched.xlsx` | ~13 MB | **Enriched workbook** — 63 sheets: 1 summary + 61 per-pair + 1 master "Pickup & Dropoff Points" sheet. 49,648 enriched fares with 25 columns each, plus 10,742 distinct pickup/dropoff points. |

## `trips-sample/` — single-route sample dataset

A complete crawl output for one route (Hà Nội → Sa Pa, 3 days,
2026-09-01 to 2026-09-03). Useful as a small, self-contained fixture
for testing downstream scripts without downloading the full N×N set.

| File | Description |
|------|-------------|
| `trips.jsonl` | Raw trips JSONL (968 trips, 7.3 MB). |
| `trips_sample.jsonl` | 10-trip sample of the above. |
| `fares.csv` / `fares.jsonl` | Derived flat fares table (968 rows). |
| `brands.csv` / `brands.jsonl` | 26 unique operators with trip counts. |
| `summaries.csv` / `summaries.jsonl` | Per-route-day summary (3 rows). |

## Coverage of the N×N dataset

- **9 cities, 72 ordered pairs** (each city paired with every other city).
- **3-day window**: 2026-09-01 → 2026-09-03.
- **61 pairs** have direct service (49,446 trips total).
- **11 pairs** have no direct service — mostly cross-region pairs that
  are geographically far (e.g. Hà Giang → Nha Trang, Đà Lạt → Hà Giang).
- **26 operators** serve the network; top operator by departure count
  is Sao Việt with 191 departures.
- **46 unique routes** across the 61 active pairs.

Top 5 routes by trip count:

| Route | Trips |
|-------|------|
| Hải Phòng → Hà Nội | 7,018 |
| Huế → Đà Nẵng | 6,418 |
| Hà Nội → Hải Phòng | 6,028 |
| Đà Lạt → HCM | 4,034 |
| HCM → Đà Lạt | 3,509 |

## Field schemas

See [`../docs/SCHEMA.md`](../docs/SCHEMA.md) for the authoritative field
reference for every output file (`trips.jsonl`, `fares.csv`, `brands.csv`,
`routes.csv`, `summaries.csv`, `areas.csv`) plus SQL DDL for
Postgres/MySQL/DuckDB and Python pandas recipes.

## Reproducing

See [`../scripts/README.md`](../scripts/README.md) for full end-to-end
reproduction instructions.
