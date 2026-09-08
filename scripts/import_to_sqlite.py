#!/usr/bin/env python3
"""Import crawled VeXeRe trip data into the booking-app SQLite database.

Reads one or more `trips.jsonl` files (one Trip object per line, as produced
by `vexere crawl-trips`) and idempotently populates the dimension and
inventory tables of the booking schema. Booking / payment / user / chat
tables are NEVER touched — those are operational and reserved for the live
app.

Field mapping (VeXeRe → booking schema)
---------------------------------------
  company                       → brand            (dim, dedup by company.id)
  route.from / route.to         → place            (dim, dedup by area_id)
  (brand, from, to)             → route           (dim, dedup by trio)
  (brand, vehicle_type, seats)  → bus_layout      (dim, dedup by trio)
  1..total_seats per layout     → seat             (dim, synthesized)
  route.pickup_points[]         → pickup_point    (dim, dedup by point.id
                                                  within a route)
  route.dropoff_points[]        → pickup_point    (dim, kind='dropoff')
  (vexere_route_id, sched_idx) → schedule        (dim, recurring template)
  (schedule, date)             → trip_session    (fact, one per departure)
  (trip_session, seat)         → seat_inventory  (fact, status='available')

ID strategy
-----------
VeXeRe IDs are preserved as strings with a `vx_*` prefix to avoid collision
with future app-generated UUIDs:

  brand.id          = f"vx_brand_{company_id}"
  place.id          = f"vx_place_{area_id}"         (e.g. "vx_place_24")
  place.osm_id      = 1_000_000_000 + area_id       (sentinel range — real
                                                     OSM IDs are << 1B)
  route.id          = f"vx_route_{brand}_{from}_{to}"
  bus_layout.id     = f"vx_bl_{brand}_{slug}_{seats}"
  seat.id           = f"vx_seat_{layout}_{row}{col_letter}"
  pickup_point.id   = f"vx_pp_{route}_{point_id}"
  schedule.id       = f"vx_sch_{vexere_route_id}_{idx}"
  trip_session.id   = f"vx_ts_{vexere_route_id}_{idx}_{date_ymd}"
  seat_inventory.id = f"vx_si_{trip_session}_{seat}"

Idempotency
-----------
- All dimension tables use `INSERT OR IGNORE` — re-runs don't duplicate.
- `trip_session` uses `INSERT OR REPLACE` keyed by id — re-runs refresh
  available_seats / total_seats without touching bookings.
- `seat_inventory` uses `INSERT OR IGNORE` keyed by (trip_session_id, seat_id).
  Existing rows (e.g. ones already marked 'held' or 'sold' by the app) are
  preserved. To force a full refresh, pass `--drop-existing`.

Usage
-----
Single pair:

    python scripts/import_to_sqlite.py \\
        --db     /path/to/app.db \\
        --trips  /tmp/nxn_out/HaNoi_to_SaPa/trips.jsonl

Whole N×N crawl (using the manifest written by nxn_crawl.py):

    python scripts/import_to_sqlite.py \\
        --db       /path/to/app.db \\
        --manifest /tmp/nxn_out/manifest.json \\
        --batch-size 5000

Glob multiple pairs:

    python scripts/import_to_sqlite.py \\
        --db   /path/to/app.db \\
        --dir  /tmp/nxn_out

Dry run (parses + prints, no DB writes):

    python scripts/import_to_sqlite.py \\
        --db      /tmp/test.db \\
        --trips   /tmp/foo/trips.jsonl \\
        --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    """Vietnamese-aware slugify: strip diacritics, lowercase, hyphenate."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def strip_diacritics(s: str) -> str:
    """Strip Vietnamese tone marks (used for name_ascii / name_no_tones)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def now_iso() -> str:
    """SQLite-friendly UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sanitize(s: Any) -> Any:
    """Strip SQLite-illegal control chars from strings before binding."""
    if isinstance(s, str):
        # Remove NUL + other control chars except tab/newline/CR
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)
    return s


# Seat layout synthesis: 4 seats per row (A B C D), 2+aisle+2.
SEAT_COLS = ["A", "B", "C", "D"]


def synthesize_seats(total_seats: int) -> list[tuple[str, int, int, bool, int]]:
    """Return a list of (seat_label, row_num, col_num, is_window, floor)
    for a bus with `total_seats` seats. Layout is 4-wide, 2+aisle+2; window
    seats are A and D. Floor is always 1 (single-deck); caller can override
    for double-decker buses via bus_layout.layout_data later."""
    seats: list[tuple[str, int, int, bool, int]] = []
    if total_seats <= 0:
        return seats
    for i in range(total_seats):
        row = (i // len(SEAT_COLS)) + 1
        col_idx = i % len(SEAT_COLS)
        col_letter = SEAT_COLS[col_idx]
        label = f"{row}{col_letter}"
        is_window = col_letter in ("A", "D")
        seats.append((label, row, col_idx + 1, is_window, 1))
    return seats


# ---------------------------------------------------------------------------
# Schema (used to create the destination tables if --init-schema is passed;
# otherwise the script assumes the user's app has already applied migrations.)
# ---------------------------------------------------------------------------

# Order matters: parents before children (FK enforcement is deferred).
SCHEMA_DDL: list[str] = [
    # place (geo)
    """CREATE TABLE IF NOT EXISTS "place" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "osm_id" bigint NOT NULL UNIQUE,
        "name" varchar(255) NOT NULL,
        "name_ascii" varchar(255) NULL,
        "name_no_tones" varchar(255) NULL,
        "type" varchar(30) NOT NULL,
        "province" varchar(255) NULL,
        "district" varchar(255) NULL,
        "ward" varchar(255) NULL,
        "lat" double NOT NULL,
        "lon" double NOT NULL,
        "population" integer NOT NULL DEFAULT 0,
        "created_at" text NOT NULL
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS "Place_osm_id_uniq" ON "place" ("osm_id")',
    'CREATE INDEX IF NOT EXISTS "Place_name_idx" ON "place" ("name")',
    'CREATE INDEX IF NOT EXISTS "Place_nameAscii_idx" ON "place" ("name_ascii")',
    'CREATE INDEX IF NOT EXISTS "Place_nameNoTones_idx" ON "place" (name_no_tones)',
    'CREATE INDEX IF NOT EXISTS "Place_province_idx" ON "place" ("province")',
    'CREATE INDEX IF NOT EXISTS "Place_type_idx" ON "place" ("type")',

    # brand
    """CREATE TABLE IF NOT EXISTS "brand" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "slug" varchar(120) NOT NULL UNIQUE,
        "name" varchar(255) NOT NULL,
        "logo_url" varchar(500) NULL,
        "description" text NULL,
        "contact_phone" varchar(20) NULL,
        "contact_email" varchar(255) NULL,
        "rating" double NULL,
        "status" varchar(30) NOT NULL DEFAULT 'active',
        "accent_color" varchar(9) NULL,
        "total_trips" integer NOT NULL DEFAULT 0,
        "created_at" text NOT NULL,
        "updated_at" text NOT NULL
    )""",
    'CREATE INDEX IF NOT EXISTS "Brand_status_idx" ON "brand" ("status")',

    # route
    """CREATE TABLE IF NOT EXISTS "route" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "brand_id" uuid_text NULL,
        "name" varchar(255) NOT NULL,
        "start_location_id" varchar(20) NOT NULL,
        "end_location_id" varchar(20) NOT NULL,
        "status" varchar(30) NOT NULL DEFAULT 'active',
        "created_at" text NOT NULL,
        "updated_at" text NOT NULL,
        FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE,
        FOREIGN KEY ("start_location_id") REFERENCES "place" ("id") ON DELETE RESTRICT ON UPDATE CASCADE,
        FOREIGN KEY ("end_location_id") REFERENCES "place" ("id") ON DELETE RESTRICT ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "Route_brandId_idx" ON "route" ("brand_id")',
    'CREATE INDEX IF NOT EXISTS "Route_startEnd_idx" ON "route" ("start_location_id", "end_location_id")',
    'CREATE INDEX IF NOT EXISTS "Route_status_idx" ON "route" ("status")',

    # bus_layout
    """CREATE TABLE IF NOT EXISTS "bus_layout" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "brand_id" uuid_text NULL,
        "name" varchar(255) NULL,
        "vehicle_type" varchar(30) NULL,
        "total_seats" smallint NULL,
        "layout_data" text NULL,
        "created_at" text NOT NULL,
        "updated_at" text NOT NULL,
        FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "BusLayout_brandId_idx" ON "bus_layout" ("brand_id")',

    # seat
    """CREATE TABLE IF NOT EXISTS "seat" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "bus_layout_id" uuid_text NOT NULL,
        "seat_label" varchar(10) NOT NULL,
        "seat_class" varchar(30) NULL,
        "row_num" smallint NULL,
        "col_num" smallint NULL,
        "is_window" boolean NOT NULL DEFAULT FALSE,
        "floor" smallint NOT NULL DEFAULT 1,
        "created_at" text NOT NULL,
        FOREIGN KEY ("bus_layout_id") REFERENCES "bus_layout" ("id") ON DELETE CASCADE ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "Seat_busLayoutId_idx" ON "seat" ("bus_layout_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "Seat_layout_label_uniq" ON "seat" ("bus_layout_id", "seat_label")',

    # pickup_point (per route)
    """CREATE TABLE IF NOT EXISTS "pickup_point" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "route_id" uuid_text NOT NULL,
        "name" varchar(255) NULL,
        "address" text NULL,
        "lat" double NULL,
        "lon" double NULL,
        "stop_order" integer NOT NULL DEFAULT 0,
        "kind" varchar(20) NULL,
        "created_at" text NOT NULL,
        FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE CASCADE ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "PickupPoint_routeId_idx" ON "pickup_point" ("route_id")',
    'CREATE INDEX IF NOT EXISTS "PickupPoint_route_order_idx" ON "pickup_point" ("route_id", "stop_order")',

    # schedule (recurring template)
    """CREATE TABLE IF NOT EXISTS "schedule" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "route_id" uuid_text NOT NULL,
        "departure_time" text NOT NULL,
        "effective_from" text NULL,
        "effective_to" text NULL,
        "days_of_week" text NULL,
        "bus_layout_id" text NULL,
        "base_price_adult" bigint NOT NULL DEFAULT 0,
        "base_price_child" bigint NULL,
        "amenities" text NULL,
        "created_at" text NOT NULL,
        FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE CASCADE ON UPDATE CASCADE,
        FOREIGN KEY ("bus_layout_id") REFERENCES "bus_layout" ("id") ON DELETE RESTRICT ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "Schedule_routeId_idx" ON "schedule" ("route_id")',
    'CREATE INDEX IF NOT EXISTS "Schedule_route_departure_idx" ON "schedule" ("route_id", "departure_time")',
    'CREATE INDEX IF NOT EXISTS "Schedule_effective_range_idx" ON "schedule" ("effective_from", "effective_to")',

    # trip_session (one specific departure)
    """CREATE TABLE IF NOT EXISTS "trip_session" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "schedule_id" uuid_text NOT NULL,
        "departure_date" text NOT NULL,
        "actual_departure_at" text NULL,
        "driver_name" varchar(255) NULL,
        "driver_phone" varchar(20) NULL,
        "status" varchar(30) NOT NULL DEFAULT 'scheduled',
        "total_seats" bigint NOT NULL DEFAULT 0,
        "available_seats" bigint NOT NULL DEFAULT 0,
        "created_at" text NOT NULL,
        "updated_at" text NOT NULL,
        FOREIGN KEY ("schedule_id") REFERENCES "schedule" ("id") ON DELETE CASCADE ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "TripSession_scheduleId_idx" ON "trip_session" ("schedule_id")',
    'CREATE INDEX IF NOT EXISTS "TripSession_departureDate_idx" ON "trip_session" ("departure_date")',
    'CREATE INDEX IF NOT EXISTS "TripSession_date_status_idx" ON "trip_session" ("departure_date", "status")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "TripSession_schedule_date_uniq" ON "trip_session" ("schedule_id", "departure_date")',

    # seat_inventory (per-trip per-seat)
    """CREATE TABLE IF NOT EXISTS "seat_inventory" (
        "id" uuid_text NOT NULL PRIMARY KEY,
        "trip_session_id" uuid_text NOT NULL,
        "seat_id" uuid_text NOT NULL,
        "status" varchar(20) NOT NULL DEFAULT 'available',
        "base_price" integer NOT NULL DEFAULT 0,
        "final_price" integer NOT NULL DEFAULT 0,
        "currency" varchar(3) NOT NULL DEFAULT 'VND',
        "held_until" text NULL,
        "held_by_booking_id" uuid_text NULL,
        "created_at" text NOT NULL,
        "updated_at" text NOT NULL,
        FOREIGN KEY ("trip_session_id") REFERENCES "trip_session" ("id") ON DELETE CASCADE ON UPDATE CASCADE,
        FOREIGN KEY ("seat_id") REFERENCES "seat" ("id") ON DELETE RESTRICT ON UPDATE CASCADE,
        FOREIGN KEY ("held_by_booking_id") REFERENCES "booking" ("id") ON DELETE SET NULL ON UPDATE CASCADE
    )""",
    'CREATE INDEX IF NOT EXISTS "SeatInventory_tripSessionId_idx" ON "seat_inventory" ("trip_session_id")',
    'CREATE INDEX IF NOT EXISTS "SeatInventory_seatId_idx" ON "seat_inventory" ("seat_id")',
    'CREATE INDEX IF NOT EXISTS "SeatInventory_heldByBookingId_idx" ON "seat_inventory" ("held_by_booking_id")',
    'CREATE INDEX IF NOT EXISTS "SeatInventory_tripSessionId_status_idx" ON "seat_inventory" ("trip_session_id", "status")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "SeatInventory_trip_seat_uniq" ON "seat_inventory" ("trip_session_id", "seat_id")',
]


# ---------------------------------------------------------------------------
# Discovery: which trips.jsonl files should we read?
# ---------------------------------------------------------------------------

def discover_trips_files(args: argparse.Namespace) -> list[Path]:
    """Resolve `--trips` / `--dir` / `--manifest` into a flat list of
    trips.jsonl paths. Deduplicated, sorted."""
    found: set[Path] = set()

    for p in args.trips or []:
        path = Path(p)
        if not path.exists():
            print(f"  ! --trips path not found: {p}", file=sys.stderr)
            continue
        found.add(path.resolve())

    if args.dir:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"  ! --dir not a directory: {args.dir}", file=sys.stderr)
        else:
            # /tmp/nxn_out/<pair>/trips.jsonl — single pair dir
            direct = d / "trips.jsonl"
            if direct.exists():
                found.add(direct.resolve())
            # /tmp/nxn_out — N×N root, look in each subdir
            for child in sorted(d.iterdir()):
                if child.is_dir():
                    cand = child / "trips.jsonl"
                    if cand.exists():
                        found.add(cand.resolve())

    if args.manifest:
        mpath = Path(args.manifest)
        if not mpath.exists():
            print(f"  ! --manifest not found: {args.manifest}", file=sys.stderr)
        else:
            try:
                with mpath.open("r", encoding="utf-8") as f:
                    manifest = json.load(f)
                # manifest shape: { "pairs": [ { "out_dir": "...", "trips_path": "...", ... }, ... ] }
                # or: [ { ... }, ... ]
                pairs = manifest.get("pairs", manifest) if isinstance(manifest, dict) else manifest
                for pair in pairs:
                    if not isinstance(pair, dict):
                        continue
                    tp = pair.get("trips_path") or pair.get("trips_jsonl")
                    if not tp:
                        od = (pair.get("out_dir") or pair.get("dir")
                              or pair.get("pair_dir"))
                        if od:
                            tp = os.path.join(od, "trips.jsonl")
                    if tp and os.path.exists(tp):
                        found.add(Path(tp).resolve())
            except (OSError, json.JSONDecodeError) as e:
                print(f"  ! failed to read manifest: {e}", file=sys.stderr)

    return sorted(found)


# ---------------------------------------------------------------------------
# Streaming trips.jsonl
# ---------------------------------------------------------------------------

def iter_trips(path: Path) -> Iterable[dict]:
    """Yield parsed Trip objects from a trips.jsonl file, line by line."""
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  ! {path.name}:{lineno}: bad JSON ({e}); skipping",
                      file=sys.stderr)


# ---------------------------------------------------------------------------
# The Importer
# ---------------------------------------------------------------------------

class Importer:
    def __init__(self, db_path: Path, batch_size: int = 5000,
                 dry_run: bool = False, verbose: bool = False):
        self.db_path = db_path
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.verbose = verbose

        # In-memory dedup tables for dimensions (populated during pass 1).
        self.places: dict[str, dict] = {}          # key = area_id (str)
        self.brands: dict[int, dict] = {}           # key = company.id (int)
        self.routes: dict[str, dict] = {}           # key = (brand, from, to)
        self.bus_layouts: dict[str, dict] = {}      # key = (brand, vtype, seats)
        self.seats: dict[str, list[dict]] = {}      # key = bus_layout.id
        self.pickup_points: dict[str, dict] = {}     # key = (route, point_id)
        self.schedules: dict[str, dict] = {}         # key = (vexere_route_id, idx)
        self.route_pickups: dict[str, list[dict]] = {}  # route_id → pickups

        # Per-schedule min/max date (for effective_from / effective_to)
        self.schedule_dates: dict[str, list[str]] = {}

        # Counters (for summary)
        self.counters = {
            "trips_seen": 0,
            "trips_with_multi_schedule": 0,
            "places": 0,
            "brands": 0,
            "routes": 0,
            "bus_layouts": 0,
            "seats": 0,
            "pickup_points": 0,
            "schedules": 0,
            "trip_sessions": 0,
            "seat_inventory": 0,
        }

    # -- connection setup -------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.Connection(str(self.db_path))
        # WAL mode for concurrent reader/writer (the app may be running).
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            pass  # WAL may be disallowed on network FS — silently fall back.
        conn.execute("PRAGMA synchronous=NORMAL;")
        # FK OFF during bulk import — we insert parents before children so
        # referential integrity is preserved by ordering, and turning FK
        # off avoids edge cases where a child table's FK references a parent
        # table that the importer doesn't populate (e.g. seat_inventory
        # references booking, which is the app's runtime responsibility).
        # The app turns FK back ON at runtime via its own connection.
        conn.execute("PRAGMA foreign_keys=OFF;")
        # Lower SQLite's busy timeout so concurrent writes don't hang forever.
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_schema(self, conn: sqlite3.Connection) -> None:
        """Create the destination tables if they don't yet exist. Useful when
        the destination DB is empty. Creates minimal stubs for parent tables
        the importer doesn't populate (e.g. `booking`) so FK references
        resolve cleanly."""
        cur = conn.cursor()
        # Minimal stub for `booking` — only `id` PK; needed because
        # `seat_inventory.held_by_booking_id` references it. The app's
        # real schema will REPLACE this with the full table when its
        # migrations run.
        cur.execute(
            'CREATE TABLE IF NOT EXISTS "booking" ('
            '"id" uuid_text NOT NULL PRIMARY KEY'
            ')'
        )
        for ddl in SCHEMA_DDL:
            cur.execute(ddl)
        conn.commit()

    # -- pass 1: collect dimensions from a single trip --------------------

    def _safe_area_name(self, area_id: str, fallback: str) -> str:
        """Try to find a clean city-level name for the area_id."""
        # Hardcode a few well-known area_ids for nicer display.
        KNOWN = {
            "24": "Hà Nội", "29": "Hồ Chí Minh", "15": "Đà Nẵng",
            "27": "Hải Phòng", "705": "Huế", "457": "Đà Lạt",
            "482": "Sa Pa", "417": "Nha Trang", "278": "Hà Giang",
            "38": "Lào Cai",
        }
        return KNOWN.get(str(area_id), fallback or f"Area {area_id}")

    def _first_point_coords(self, points: list[dict]) -> tuple[float, float]:
        """Pull lat/lon from the first point with a usable location."""
        for p in points or []:
            loc = p.get("location") or {}
            lat = loc.get("lat")
            lon = loc.get("lon")
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon)
                except (TypeError, ValueError):
                    continue
        return 0.0, 0.0

    @staticmethod
    def _visible_points(points: list[dict] | None) -> list[dict]:
        """Keep only points the VeXeRe website renders for this route.

        The /v2/route/ search API attaches the operator's ENTIRE point pool
        to every trip; inactive points carry `hidden: 1`. E.g. route 7275428
        (Hải Phòng → Hà Nội, Anh Huy Đất Cảng) returns 1,953 raw points but
        serves exactly 1 (Bến xe Thượng Lý). Points missing the `hidden` key
        are treated as visible (matches the FE's tolerant rendering).
        """
        return [p for p in (points or []) if not p.get("hidden")]

    def collect_trip(self, trip: dict) -> None:
        """Pass 1: harvest dimensions from one trip into the in-memory
        dedup tables. Does NOT write to DB."""
        self.counters["trips_seen"] += 1
        company = trip.get("company") or {}
        route = trip.get("route") or {}
        schedules = route.get("schedules") or []

        if len(schedules) > 1:
            self.counters["trips_with_multi_schedule"] += 1

        # -- place (from / to) --
        from_area = route.get("from") or {}
        to_area = route.get("to") or {}
        from_id = str(from_area.get("id") or "")
        to_id = str(to_area.get("id") or "")
        from_name = self._safe_area_name(from_id, from_area.get("name") or "")
        to_name = self._safe_area_name(to_id, to_area.get("name") or "")

        # Use first pickup_point coords as a representative lat/lon for the
        # 'from' place; first dropoff coords for the 'to' place.
        # NOTE: filter to visible points only — the /v2/route/ API returns the
        # operator's ENTIRE point pool on every trip; the website only renders
        # points with `hidden == 0` (route 7275428: 1,953 raw → 1 visible).
        pickups = self._visible_points(route.get("pickup_points"))
        dropoffs = self._visible_points(route.get("dropoff_points"))
        from_lat, from_lon = self._first_point_coords(pickups)
        to_lat, to_lon = self._first_point_coords(dropoffs)

        for area_id, name, lat, lon in [
            (from_id, from_name, from_lat, from_lon),
            (to_id, to_name, to_lat, to_lon),
        ]:
            if not area_id:
                continue
            if area_id not in self.places:
                self.places[area_id] = {
                    "id": f"vx_place_{area_id}",
                    "osm_id": 1_000_000_000 + int(area_id),
                    "name": sanitize(name),
                    "name_ascii": sanitize(strip_diacritics(name)),
                    "name_no_tones": sanitize(strip_diacritics(name)),
                    "type": "city",
                    "lat": lat,
                    "lon": lon,
                }

        # -- brand --
        comp_id = company.get("id")
        if comp_id is not None and comp_id not in self.brands:
            ratings = company.get("ratings") or {}
            overall = ratings.get("overall") if isinstance(ratings, dict) else None
            name = sanitize(company.get("name") or f"Brand {comp_id}")
            self.brands[comp_id] = {
                "id": f"vx_brand_{comp_id}",
                "slug": slugify(name) or f"brand-{comp_id}",
                "name": name,
                "logo_url": sanitize(company.get("logo")),
                "description": None,
                "contact_phone": sanitize(company.get("hotline") or company.get("phone")),
                "contact_email": None,
                "rating": float(overall) if overall is not None else None,
                "status": "active",
                "accent_color": None,
                "total_trips": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

        # -- route (dedup by brand + from + to) --
        brand_id = f"vx_brand_{comp_id}" if comp_id is not None else None
        route_key = f"{brand_id}|{from_id}|{to_id}"
        if route_key not in self.routes:
            name_parts = []
            if brand_id:
                bname = self.brands[comp_id]["name"] if comp_id is not None else ""
                name_parts.append(bname)
            name_parts.append(from_name)
            name_parts.append(to_name)
            rname = sanitize(" → ".join(name_parts))
            self.routes[route_key] = {
                "id": f"vx_route_{comp_id}_{from_id}_{to_id}",
                "brand_id": brand_id,
                "name": rname,
                "start_location_id": f"vx_place_{from_id}",
                "end_location_id": f"vx_place_{to_id}",
                "status": "active",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

        route_id = self.routes[route_key]["id"]

        # -- bus_layout (dedup by brand + vehicle_type + total_seats) --
        # Take vehicle type + total_seats from the first schedule (most common).
        if schedules:
            sched0 = schedules[0]
            vtype = sanitize(sched0.get("vehicle_type") or "unknown")
            tseats = int(sched0.get("total_seats") or 0) or 0
        else:
            vtype = "unknown"
            tseats = 0

        bl_key = f"{comp_id}|{vtype}|{tseats}"
        if bl_key not in self.bus_layouts:
            bl_id = f"vx_bl_{comp_id}_{slugify(vtype) or 'v'}_{tseats}"
            self.bus_layouts[bl_key] = {
                "id": bl_id,
                "brand_id": brand_id,
                "name": vtype,
                "vehicle_type": vtype,
                "total_seats": tseats,
                "layout_data": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            # synthesize seats for this layout (one-time)
            self.seats[bl_id] = [
                {
                    "id": f"vx_seat_{bl_id}_{label}",
                    "bus_layout_id": bl_id,
                    "seat_label": label,
                    "seat_class": "standard",
                    "row_num": rn,
                    "col_num": cn,
                    "is_window": 1 if win else 0,
                    "floor": fl,
                    "created_at": now_iso(),
                }
                for (label, rn, cn, win, fl) in synthesize_seats(tseats)
            ]

        # -- pickup_points (per route — VeXeRe IDs are unique within route) --
        # We dedup by (route_id, point_id) so the same physical point used by
        # two different brand routes becomes two pickup_point rows.
        seen_pp_ids: set[tuple[str, int]] = set()
        for kind, points in (("pickup", pickups), ("dropoff", dropoffs)):
            for idx, p in enumerate(points or []):
                pid = p.get("id")
                if pid is None:
                    continue
                key = (route_id, pid)
                if key in seen_pp_ids:
                    continue
                seen_pp_ids.add(key)
                if key not in [(route_id, pp["vexere_id"]) for pp in self.route_pickups.get(route_id, [])]:
                    loc = p.get("location") or {}
                    lat = loc.get("lat")
                    lon = loc.get("lon")
                    pp_row = {
                        "id": f"vx_pp_{route_id}_{pid}",
                        "route_id": route_id,
                        "name": sanitize(p.get("name")),
                        "address": sanitize(p.get("display_name_vex")),
                        "lat": float(lat) if lat is not None else None,
                        "lon": float(lon) if lon is not None else None,
                        "stop_order": int(p.get("duration") or idx),
                        "kind": kind,
                        "created_at": now_iso(),
                        "vexere_id": pid,
                    }
                    self.route_pickups.setdefault(route_id, []).append(pp_row)
                    self.pickup_points[pp_row["id"]] = pp_row

        # -- schedule (per vexere_route_id + departure_time + seat_template_id)
        # VeXeRe's `route.id` is a per-(brand, from, to, vehicle-template)
        # grouping — multiple trips share the same `route.id` but have
        # different `departure_time`s. Each unique (route.id, departure_time,
        # seat_template_id) is one recurring schedule template; each (schedule,
        # date) is one trip_session.
        vexere_route_id = route.get("id")
        if vexere_route_id is None:
            return
        departure_date = route.get("departure_date") or ""
        date_ymd = departure_date[:10] if departure_date else ""
        # The route-level departure_time is the actual bus departure; the
        # schedule's hour:minute is the pickup time (often 15 min earlier).
        route_dep_time = sanitize(route.get("departure_time") or "00:00")
        # Slug for IDs (colons aren't ID-friendly).
        dep_slug = route_dep_time.replace(":", "-")

        for idx, sched in enumerate(schedules):
            seat_tpl_id = sched.get("seat_template_id") or "default"
            sch_key = f"{vexere_route_id}|{route_dep_time}|{seat_tpl_id}"
            if sch_key not in self.schedules:
                vtype = sanitize(sched.get("vehicle_type") or "unknown")
                tseats = int(sched.get("total_seats") or 0) or 0
                bl_key = f"{comp_id}|{vtype}|{tseats}"
                bus_layout_id = self.bus_layouts.get(bl_key, {}).get("id")
                fare = sched.get("fare") or {}
                base_adult = int(fare.get("original") or 0)
                amen = {
                    "seat_template_id": sched.get("seat_template_id"),
                    "seat_template_name": sched.get("seat_template_name"),
                    "seat_type": sched.get("seat_type"),
                    "vehicle_quality": sched.get("vehicle_quality"),
                    "config": sched.get("config"),
                    "refundable": sched.get("refundable"),
                    "schedule_idx": idx,
                }
                self.schedules[sch_key] = {
                    "id": f"vx_sch_{vexere_route_id}_{dep_slug}_{seat_tpl_id}",
                    "route_id": route_id,
                    "departure_time": route_dep_time,
                    "effective_from": None,
                    "effective_to": None,
                    "days_of_week": None,
                    "bus_layout_id": bus_layout_id,
                    "base_price_adult": base_adult,
                    "base_price_child": None,
                    "amenities": json.dumps(amen, ensure_ascii=False),
                    "created_at": now_iso(),
                    "_vexere_route_id": vexere_route_id,
                    "_dep_time": route_dep_time,
                    "_seat_tpl_id": seat_tpl_id,
                    "_idx": idx,
                    "_bus_layout_key": bl_key,
                    "_base_fare": base_adult,
                    "_discount": int(fare.get("discount") or 0),
                }
                self.schedule_dates[sch_key] = []

            # track min/max date for effective_from / effective_to
            if date_ymd:
                self.schedule_dates.setdefault(sch_key, []).append(date_ymd)

        # bump brand.total_trips
        if comp_id is not None:
            self.brands[comp_id]["total_trips"] += 1

    # -- pass 1 finalize: compute effective_from / effective_to -----------

    def finalize_dimensions(self) -> None:
        """After all trips are collected, compute per-schedule effective
        date range and per-schedule bus_layout_id (already set above)."""
        for sch_key, dates in self.schedule_dates.items():
            if not dates:
                continue
            sch = self.schedules.get(sch_key)
            if not sch:
                continue
            sch["effective_from"] = min(dates)
            sch["effective_to"] = max(dates)

    # -- pass 2: write dimensions -----------------------------------------

    def write_dimensions(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        ts = now_iso()

        # places
        rows = []
        for p in self.places.values():
            rows.append((
                p["id"], p["osm_id"], p["name"], p["name_ascii"],
                p["name_no_tones"], p["type"], None, None, None,
                p["lat"], p["lon"], 0, ts,
            ))
        if rows:
            cur.executemany(
                "INSERT OR IGNORE INTO place "
                "(id, osm_id, name, name_ascii, name_no_tones, type, "
                "province, district, ward, lat, lon, population, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.counters["places"] = cur.rowcount if cur.rowcount > 0 else len(rows)
        else:
            self.counters["places"] = 0

        # brands
        rows = [(b["id"], b["slug"], b["name"], b["logo_url"], b["description"],
                 b["contact_phone"], b["contact_email"], b["rating"], b["status"],
                 b["accent_color"], b["total_trips"], b["created_at"], b["updated_at"])
                for b in self.brands.values()]
        if rows:
            cur.executemany(
                "INSERT OR IGNORE INTO brand "
                "(id, slug, name, logo_url, description, contact_phone, contact_email, "
                "rating, status, accent_color, total_trips, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            # UPDATE total_trips if row already existed
            cur.executemany(
                "UPDATE brand SET total_trips = ?, updated_at = ? WHERE id = ?",
                [(b["total_trips"], ts, b["id"]) for b in self.brands.values()],
            )
            self.counters["brands"] = len(rows)
        else:
            self.counters["brands"] = 0

        # routes
        rows = [(r["id"], r["brand_id"], r["name"], r["start_location_id"],
                 r["end_location_id"], r["status"], r["created_at"], r["updated_at"])
                for r in self.routes.values()]
        if rows:
            cur.executemany(
                "INSERT OR IGNORE INTO route "
                "(id, brand_id, name, start_location_id, end_location_id, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            self.counters["routes"] = len(rows)
        else:
            self.counters["routes"] = 0

        # bus_layouts
        rows = [(b["id"], b["brand_id"], b["name"], b["vehicle_type"],
                 b["total_seats"], b["layout_data"], b["created_at"], b["updated_at"])
                for b in self.bus_layouts.values()]
        if rows:
            cur.executemany(
                "INSERT OR IGNORE INTO bus_layout "
                "(id, brand_id, name, vehicle_type, total_seats, layout_data, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            self.counters["bus_layouts"] = len(rows)
        else:
            self.counters["bus_layouts"] = 0

        # seats
        seat_rows = []
        for seats in self.seats.values():
            for s in seats:
                seat_rows.append((s["id"], s["bus_layout_id"], s["seat_label"],
                                  s["seat_class"], s["row_num"], s["col_num"],
                                  s["is_window"], s["floor"], s["created_at"]))
        if seat_rows:
            cur.executemany(
                "INSERT OR IGNORE INTO seat "
                "(id, bus_layout_id, seat_label, seat_class, row_num, col_num, "
                "is_window, floor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                seat_rows,
            )
            self.counters["seats"] = len(seat_rows)
        else:
            self.counters["seats"] = 0

        # pickup_points
        pp_rows = []
        for pp in self.pickup_points.values():
            pp_rows.append((pp["id"], pp["route_id"], pp["name"], pp["address"],
                            pp["lat"], pp["lon"], pp["stop_order"], pp["kind"],
                            pp["created_at"]))
        if pp_rows:
            cur.executemany(
                "INSERT OR IGNORE INTO pickup_point "
                "(id, route_id, name, address, lat, lon, stop_order, kind, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                pp_rows,
            )
            self.counters["pickup_points"] = len(pp_rows)
        else:
            self.counters["pickup_points"] = 0

        # schedules
        sch_rows = []
        for sch in self.schedules.values():
            sch_rows.append((sch["id"], sch["route_id"], sch["departure_time"],
                             sch["effective_from"], sch["effective_to"],
                             sch["days_of_week"], sch["bus_layout_id"],
                             sch["base_price_adult"], sch["base_price_child"],
                             sch["amenities"], sch["created_at"]))
        if sch_rows:
            cur.executemany(
                "INSERT OR IGNORE INTO schedule "
                "(id, route_id, departure_time, effective_from, effective_to, "
                "days_of_week, bus_layout_id, base_price_adult, base_price_child, "
                "amenities, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                sch_rows,
            )
            # UPDATE effective_from/to + base_price_adult for existing rows
            cur.executemany(
                "UPDATE schedule SET effective_from = ?, effective_to = ?, "
                "base_price_adult = ?, amenities = ? WHERE id = ?",
                [(sch["effective_from"], sch["effective_to"],
                  sch["base_price_adult"], sch["amenities"], sch["id"])
                 for sch in self.schedules.values()],
            )
            self.counters["schedules"] = len(sch_rows)
        else:
            self.counters["schedules"] = 0

        conn.commit()

    # -- pass 3: write facts (trip_sessions + seat_inventory) ------------

    def write_facts(self, conn: sqlite3.Connection,
                    trips_files: list[Path]) -> None:
        """Second pass over the JSONL files. For each trip+schedule, write
        one trip_session row + N seat_inventory rows (status='available')."""
        cur = conn.cursor()
        ts = now_iso()
        ts_buf: list[tuple] = []
        si_buf: list[tuple] = []
        n_ts = 0
        n_si = 0

        def flush_ts() -> None:
            nonlocal ts_buf, n_ts
            if not ts_buf:
                return
            cur.executemany(
                "INSERT OR REPLACE INTO trip_session "
                "(id, schedule_id, departure_date, actual_departure_at, "
                "driver_name, driver_phone, status, total_seats, available_seats, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ts_buf,
            )
            n_ts += len(ts_buf)
            ts_buf = []

        def flush_si() -> None:
            nonlocal si_buf, n_si
            if not si_buf:
                return
            cur.executemany(
                "INSERT OR IGNORE INTO seat_inventory "
                "(id, trip_session_id, seat_id, status, base_price, final_price, "
                "currency, held_until, held_by_booking_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                si_buf,
            )
            n_si += len(si_buf)
            si_buf = []

        for path in trips_files:
            if self.verbose:
                print(f"  + writing facts from {path.name}")
            for trip in iter_trips(path):
                route = trip.get("route") or {}
                company = trip.get("company") or {}
                schedules = route.get("schedules") or []
                vexere_route_id = route.get("id")
                if vexere_route_id is None:
                    continue
                comp_id = company.get("id")
                from_id = str((route.get("from") or {}).get("id") or "")
                to_id = str((route.get("to") or {}).get("id") or "")
                brand_id = f"vx_brand_{comp_id}" if comp_id is not None else None
                route_id = f"vx_route_{comp_id}_{from_id}_{to_id}"
                departure_date = route.get("departure_date") or ""
                date_ymd = departure_date[:10] if departure_date else ""
                # Use the route-level departure_time (the actual bus departure,
                # not the pickup time inside schedule[0].hour:minute).
                route_dep_time = sanitize(route.get("departure_time") or "00:00")
                dep_slug = route_dep_time.replace(":", "-")

                for idx, sched in enumerate(schedules):
                    seat_tpl_id = sched.get("seat_template_id") or "default"
                    sch_key = f"{vexere_route_id}|{route_dep_time}|{seat_tpl_id}"
                    sch = self.schedules.get(sch_key)
                    if sch is None:
                        continue
                    ts_id = f"vx_ts_{vexere_route_id}_{dep_slug}_{seat_tpl_id}_{date_ymd}"
                    tseats = int(sched.get("total_seats") or 0) or 0
                    aseats = int(sched.get("available_seats") or
                                 sched.get("total_available_seats") or 0) or 0

                    ts_buf.append((
                        ts_id, sch["id"], date_ymd, None,
                        None, None, "scheduled",
                        tseats, aseats, ts, ts,
                    ))
                    if len(ts_buf) >= self.batch_size:
                        flush_ts()

                    # seat_inventory for each seat in the schedule's bus_layout
                    bl_id = sch["bus_layout_id"]
                    if not bl_id:
                        continue
                    seat_list = self.seats.get(bl_id, [])
                    fare = sched.get("fare") or {}
                    base = int(fare.get("original") or 0) or sch["_base_fare"]
                    discount = int(fare.get("discount") or 0) or 0
                    # VeXeRe uses -1 to mean "no discount"; treat <0 as 0.
                    discount = max(0, discount)
                    final = max(0, base - discount)

                    for s in seat_list:
                        si_buf.append((
                            f"vx_si_{ts_id}_{s['seat_label']}",
                            ts_id, s["id"], "available",
                            base, final, "VND", None, None, ts, ts,
                        ))
                        if len(si_buf) >= self.batch_size:
                            flush_si()

        flush_ts()
        flush_si()
        conn.commit()
        self.counters["trip_sessions"] = n_ts
        self.counters["seat_inventory"] = n_si

    # -- top-level run ----------------------------------------------------

    def run(self, trips_files: list[Path]) -> dict:
        if not trips_files:
            print("  ! no trips.jsonl files discovered — nothing to do.",
                  file=sys.stderr)
            return self.counters

        print(f"  discovered {len(trips_files)} trips.jsonl file(s):")
        for p in trips_files:
            print(f"    - {p}")

        # Pass 1: collect dimensions
        print(f"\n[1/3] collecting dimensions from {len(trips_files)} file(s)...")
        for path in trips_files:
            if self.verbose:
                print(f"  + scanning {path.name}")
            for trip in iter_trips(path):
                self.collect_trip(trip)
        self.finalize_dimensions()
        print(f"    trips seen:       {self.counters['trips_seen']:,}")
        print(f"    multi-schedule:   {self.counters['trips_with_multi_schedule']:,}")
        print(f"    places:           {len(self.places):,}")
        print(f"    brands:           {len(self.brands):,}")
        print(f"    routes:           {len(self.routes):,}")
        print(f"    bus_layouts:      {len(self.bus_layouts):,}")
        print(f"    seats:            {sum(len(v) for v in self.seats.values()):,}")
        print(f"    pickup_points:    {len(self.pickup_points):,}")
        print(f"    schedules:        {len(self.schedules):,}")

        if self.dry_run:
            print("\n[dry-run] no DB writes performed.")
            return self.counters

        # Connect & write
        conn = self.connect()
        try:
            print(f"\n[2/3] writing dimensions to {self.db_path}...")
            self.write_dimensions(conn)
            print(f"    places written:        {self.counters['places']:,}")
            print(f"    brands written:        {self.counters['brands']:,}")
            print(f"    routes written:       {self.counters['routes']:,}")
            print(f"    bus_layouts written:  {self.counters['bus_layouts']:,}")
            print(f"    seats written:        {self.counters['seats']:,}")
            print(f"    pickup_points written:{self.counters['pickup_points']:,}")
            print(f"    schedules written:    {self.counters['schedules']:,}")

            print(f"\n[3/3] writing facts (trip_sessions + seat_inventory)...")
            self.write_facts(conn, trips_files)
            print(f"    trip_sessions written:   {self.counters['trip_sessions']:,}")
            print(f"    seat_inventory written:  {self.counters['seat_inventory']:,}")
        finally:
            conn.close()

        return self.counters


# ---------------------------------------------------------------------------
# Arg parsing + entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="import_to_sqlite",
        description="Import crawled VeXeRe trips.jsonl into the booking-app "
                    "SQLite database (dimensions + inventory only; "
                    "booking/payment/user tables are NOT touched).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See module docstring for the full field-mapping table.",
    )
    p.add_argument("--db", required=True, type=Path,
                   help="Path to the destination SQLite .db file.")
    p.add_argument("--trips", action="append", default=[],
                   help="Path to a trips.jsonl file. Can be repeated.")
    p.add_argument("--dir", default=None,
                   help="Path to either a single pair dir (containing "
                        "trips.jsonl) or the N×N root (containing "
                        "<pair>/trips.jsonl).")
    p.add_argument("--manifest", default=None,
                   help="Path to the NxN manifest JSON written by nxn_crawl.py.")
    p.add_argument("--init-schema", action="store_true",
                   help="Create destination tables if they don't exist "
                        "(useful for fresh DBs).")
    p.add_argument("--drop-existing", action="store_true",
                   help="DELETE all rows with vx_* IDs from each table "
                        "before importing. Use only on a fresh import — "
                        "destroys existing imported data. Does NOT touch "
                        "app-generated rows (UUIDs without the vx_ prefix).")
    p.add_argument("--batch-size", type=int, default=5000,
                   help="INSERT batch size (default 5000).")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse + collect + print stats, but write nothing.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print per-file progress.")
    return p.parse_args(argv)


def drop_existing(conn: sqlite3.Connection) -> None:
    """Remove all vx_* rows from each table the importer owns. Safe with
    app-generated rows (UUIDs without vx_ prefix) — they're untouched."""
    print("\n[0/3] dropping existing vx_* rows (--drop-existing)...")
    cur = conn.cursor()
    # Order matters: children first (FK ON DELETE CASCADE will handle most,
    # but explicit delete is safer with defer_foreign_keys).
    tables = [
        "seat_inventory",
        "trip_session",
        "schedule",
        "pickup_point",
        "seat",
        "bus_layout",
        "route",
        "brand",
        "place",
    ]
    for t in tables:
        # id column is always 'id' for these tables.
        n = cur.execute(f"DELETE FROM {t} WHERE id LIKE 'vx_%'").rowcount
        print(f"    {t}: deleted {n:,} vx_* rows")
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    trips_files = discover_trips_files(args)
    if not trips_files:
        print("No trips.jsonl files discovered. Pass --trips / --dir / --manifest.",
              file=sys.stderr)
        return 2

    imp = Importer(
        db_path=args.db,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if args.dry_run:
        return imp.run(trips_files) and 0 or 0

    # Pre-flight: create schema if asked.
    if args.init_schema:
        conn = imp.connect()
        try:
            imp.init_schema(conn)
            print(f"[init-schema] created missing tables in {args.db}")
        finally:
            conn.close()

    # Pre-flight: drop existing vx_* rows if asked.
    if args.drop_existing:
        conn = imp.connect()
        try:
            drop_existing(conn)
        finally:
            conn.close()

    imp.run(trips_files)

    # Final summary
    print("\n" + "=" * 60)
    print("IMPORT COMPLETE")
    print("=" * 60)
    for k, v in imp.counters.items():
        print(f"  {k:<28} {v:>10,}")
    print(f"\nDatabase: {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
