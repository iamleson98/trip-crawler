# -*- coding: utf-8 -*-
"""run_migration.py — Push the prepared migration plan into the
rust-be-template service (https://datxevui.com) through its admin API.

Phases (run in this order; each is resumable via the state file):

  brands     POST /api/admin/brands               (306)
             → after this, run the db-layouts phase (see below)
  db-layouts Emit layouts_seats.sql — apply it to the server DB while the
             backend container is briefly stopped (bus_layout + seat rows;
             no API exists for them). Requires the brand map from `brands`.
  data       POST addresses → routes → schedules(+points) → pickup points
  pictures   POST /api/admin/routes/{id}/pictures  (multipart)
  verify     Count-check every table via the admin API + smoke test.

Idempotency / resume:
  * brands   — existing slugs are skipped (list fetched at start)
  * everything else — the state file records seed-id → API-id maps and a
    per-phase item cursor; re-running skips completed work.

Usage:
  export DXV_EMAIL=... DXV_PASSWORD=...
  python3 run_migration.py --base https://datxevui.com --phase brands
  python3 run_migration.py --base https://datxevui.com --phase db-layouts
  #   ↓ apply scripts/migration/layouts_seats.sql on the server (see README)
  python3 run_migration.py --base https://datxevui.com --phase data
  python3 run_migration.py --base https://datxevui.com --phase pictures
  python3 run_migration.py --base https://datxevui.com --phase verify
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api_client import Client

HERE = Path(__file__).resolve().parent
PLAN_PATH = HERE / "migration_plan.json"
STATE_PATH = HERE / "migration_state.json"
GEOCODE_CACHE_PATH = HERE / "geocode_cache.json"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = load_json(path, {})
        self.dirty = 0
        self.lock = threading.Lock()

    def save(self, force=False):
        with self.lock:
            self.dirty += 1
            if force or self.dirty >= 50:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, ensure_ascii=False),
                              encoding="utf-8")
                tmp.replace(self.path)
                self.dirty = 0

    # brand slug → api id
    def brand_id(self, slug):
        return self.data.setdefault("brands", {}).get(slug)

    def set_brand(self, slug, api_id):
        self.data.setdefault("brands", {})[slug] = api_id
        self.save()

    def map_get(self, kind, seed_id):
        return self.data.setdefault(kind, {}).get(str(seed_id))

    def map_set(self, kind, seed_id, api_id):
        self.data.setdefault(kind, {})[str(seed_id)] = api_id
        self.save()




# ── Phase: brands ───────────────────────────────────────────────────
def phase_brands(c: Client, plan, st: State, dry: bool):
    existing = {}
    # NOTE: GET /api/admin/brands returns every row (no server-side
    # pagination) — one fetch is enough.
    for b in c.get_json("/api/admin/brands").get("items", []):
        existing[b["slug"]] = b["id"]
    print(f"[brands] {len(existing)} existing on server")

    created, skipped = 0, 0
    for b in plan["brands"]:
        if st.brand_id(b["slug"]) and b["slug"] in existing:
            skipped += 1
            continue
        if b["slug"] in existing:
            st.set_brand(b["slug"], existing[b["slug"]])
            skipped += 1
            continue
        body = {"name": b["name"], "slug": b["slug"]}
        if b.get("logo_url"):
            body["logoUrl"] = b["logo_url"]
        if b.get("description"):
            body["description"] = b["description"]
        if b.get("contact_phone"):
            body["contactPhone"] = b["contact_phone"]
        if b.get("contact_email"):
            body["contactEmail"] = b["contact_email"]
        if b.get("rating") is not None:
            body["rating"] = b["rating"]
        if b.get("status"):
            body["status"] = b["status"]
        if b.get("accent_color"):
            body["accentColor"] = b["accent_color"]
        if dry:
            print("  would create", b["slug"])
            continue
        r = c.post_json("/api/admin/brands", body)
        st.set_brand(b["slug"], r["id"])
        created += 1
        if created % 25 == 0:
            print(f"  [brands] {created} created "
                  f"({len(plan['brands']) - created - skipped} left)")
    st.save(force=True)
    print(f"[brands] created={created} skipped={skipped} "
          f"totalMapped={len(st.data.get('brands', {}))}")


# ── Phase: db-layouts (emit SQL) ─────────────────────────────────────
def phase_db_layouts(c: Client, plan, st: State, out: Path):
    """Emit the bus_layout + seat INSERT script.

    Applies only after the `brands` phase (needs the API brand ids) and
    BEFORE the `data` phase (schedules reference the layout ids). Run it
    on the server with the backend stopped — see README.md.
    """
    missing = [b["slug"] for b in plan["brands"] if not st.brand_id(b["slug"])]
    if missing:
        raise SystemExit(f"{len(missing)} brands unmapped — run `brands` first")

    now = plan["generated_at"]
    lines = [
        "-- layouts_seats.sql — generated by run_migration.py (db-layouts)",
        f"-- generated_at: {now}",
        "-- Insert bus_layout + seat rows for the migrated fleet.",
        "-- SAFE ORDER: stop backend → backup DB → apply → start backend.",
        "-- Engine: rustqlite; UUIDs are 16-byte BLOBs (X'hex') because the",
        "-- app binds Uuid values as binary via sqlx — TEXT ids break decode.",
        "BEGIN;",
    ]
    n_lay = n_seat = 0
    for lay in plan["layouts"]:
        brand_slug = lay.get("brand_slug")
        brand_id = st.brand_id(brand_slug) if brand_slug else None
        if not brand_id:
            print(f"  !! layout {lay['id']} has no brand — skipping")
            continue
        name = lay["name"].replace("'", "''")
        lines.append(
            f"INSERT INTO bus_layout (id, brand_id, name, vehicle_type, "
            f"total_seats, layout_data, created_at, updated_at) VALUES ("
            f"{uuid_blob(lay['id'])}, {uuid_blob(brand_id)}, '{name}', "
            f"'{lay['vehicle_code']}', {lay['total_seats']}, NULL, "
            f"'{now}', '{now}');")
        n_lay += 1
        for s in lay["seat_rows"]:
            lines.append(
                f"INSERT INTO seat (id, bus_layout_id, seat_label, seat_class, "
                f"row_num, col_num, is_window, floor, created_at) VALUES ("
                f"{uuid4_blob()}, {uuid_blob(lay['id'])}, '{s['label']}', "
                f"'{s['class']}', {s['row_num']}, {s['col_num']}, "
                f"{1 if s['is_window'] else 0}, {s['floor']}, '{now}');")
            n_seat += 1
    lines.append("COMMIT;")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[db-layouts] wrote {out.name}: {n_lay} layouts, {n_seat} seats")


def uuid_blob(u: str) -> str:
    """36-char UUID string → X'...' 16-byte blob literal (sqlx Uuid bind form)."""
    h = str(u).replace("-", "").strip().lower()
    if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"bad uuid: {u!r}")
    return f"X'{h}'"


def uuid4_blob() -> str:
    import uuid
    return uuid_blob(str(uuid.uuid4()))


# ── Geocode helper for zero-coord addresses ─────────────────────────
def geocode_missing(c: Client, plan, cache_path: Path):
    cache = load_json(cache_path, {})
    todo = [a for a in plan["addresses"] if a["lat"] == 0 and a["lon"] == 0]
    print(f"[geocode] {len(todo)} addresses without coordinates")
    for i, a in enumerate(todo, 1):
        key = a["name"]
        if key in cache:
            hit = cache[key]
            a["lat"], a["lon"] = hit
            continue
        q = urllib.parse.quote(a["name"])
        try:
            r = c.get_json(f"/api/places/search?q={q}&limit=1")
            items = r.get("items") or []
        except Exception as e:
            items = []
            print(f"  [geocode] search failed for {a['name']}: {e}")
        if items:
            it = items[0]
            hit = [it.get("lat", 0.0), it.get("lon", 0.0)]
        else:
            hit = None
        cache[key] = hit
        if hit:
            a["lat"], a["lon"] = hit
        if i % 20 == 0:
            print(f"  [geocode] {i}/{len(todo)}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                          encoding="utf-8")
    ok = sum(1 for a in todo if a["lat"] or a["lon"])
    print(f"[geocode] resolved {ok}/{len(todo)}")


def _run_stage(name: str, items, worker, workers: int, total: int):
    """Run `worker(item)` over `items` with a thread pool; log progress.
    worker returns the created API id (or None). Failures are logged and
    simply left for the next resumable run."""
    done = fail = 0
    t0 = time.time()
    if workers <= 1:
        for it in items:
            try:
                worker(it)
            except Exception as e:
                fail += 1
                print(f"  [{name}] FAIL {it.get('slug', it.get('seed_id', '?'))}: {e}")
            done += 1
            if done % 200 == 0:
                print(f"  [{name}] {done}/{total} "
                      f"({done / max(time.time() - t0, 1):.1f}/s)")
        return fail
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, it) for it in items]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                fail += 1
                msg = str(e)
                if fail <= 5 or fail % 50 == 0:
                    print(f"  [{name}] FAIL ({fail}): {msg[:200]}")
            done += 1
            if done % 200 == 0:
                rate = done / max(time.time() - t0, 1)
                eta = (total - done) / max(rate, 0.01) / 60
                print(f"  [{name}] {done}/{total} ({rate:.1f}/s, "
                      f"ETA {eta:.0f}m, fail {fail})")
    print(f"  [{name}] stage done: {done} attempts, {fail} failures")
    return fail


# ── Phase: data (addresses → routes → schedules → pickup points) ─────
def phase_data(c: Client, plan, st: State, dry: bool, workers: int = 1):
    brand_by_seed = {b["seed_id"]: b for b in plan["brands"]}

    # vehicle type ids by code
    vt_ids = {v["code"]: v["id"] for v in
              c.get_json("/api/admin/vehicle-types").get("items", [])}
    print(f"[data] vehicle types: {vt_ids}")

    # 1. addresses ---------------------------------------------------------
    if not st.data.get("geocoded"):
        if not dry:
            geocode_missing(c, plan, GEOCODE_CACHE_PATH)
        st.data["geocoded"] = True
        st.save(force=True)

    def addr_worker(a):
        brand = brand_by_seed[a["brand_seed_id"]]
        brand_id = st.brand_id(brand["slug"])
        if not brand_id:
            return None
        body = {
            "brandId": brand_id, "name": a["name"],
            "lat": a["lat"], "lon": a["lon"],
        }
        for k in ("address", "province", "district", "ward"):
            if a.get(k):
                body[k] = a[k]
        if dry:
            return None
        r = c.post_json("/api/admin/addresses", body)
        st.map_set("addresses", a["seed_id"], r["id"])

    todo = [a for a in plan["addresses"]
            if not st.map_get("addresses", a["seed_id"])]
    _run_stage("addresses", todo, addr_worker, workers, len(todo))
    st.save(force=True)
    print(f"[addresses] mapped "
          f"{len(st.data.get('addresses', {}))}/{len(plan['addresses'])}")

    # 2. routes --------------------------------------------------------------
    def route_worker(r_):
        brand = brand_by_seed[r_["brand_seed_id"]]
        body = {
            "brandId": st.brand_id(brand["slug"]),
            "name": r_["name"],
            "startLocationId": r_["start_location_id"],
            "endLocationId": r_["end_location_id"],
            "status": r_["status"],
        }
        if dry:
            return None
        r = c.post_json("/api/admin/routes", body)
        st.map_set("routes", r_["seed_id"], r["id"])

    todo = [r for r in plan["routes"]
            if not st.map_get("routes", r["seed_id"])]
    _run_stage("routes", todo, route_worker, workers, len(todo))
    st.save(force=True)
    print(f"[routes] mapped "
          f"{len(st.data.get('routes', {}))}/{len(plan['routes'])}")

    # 3. schedules (+ points) -------------------------------------------------
    route_id_by_seed = st.data.get("routes", {})

    def sched_worker(s):
        route_id = route_id_by_seed.get(s["route_seed_id"])
        if not route_id:
            return None
        body = {
            "routeId": route_id,
            "departureTime": s["departure_time"],
            "basePriceAdult": s["base_price_adult"],
        }
        if s["base_price_child"] is not None:
            body["basePriceChild"] = s["base_price_child"]
        if s["layout_id"]:
            body["busLayoutId"] = s["layout_id"]
        vt = vt_ids.get(s["vehicle_code"]) if s["vehicle_code"] else None
        if vt:
            body["vehicleTypeId"] = vt
        pts = []
        for p in s["points"]:
            aid = st.map_get("addresses", p["address_seed_id"])
            if aid:
                pt = {"addressId": aid}
                if p["arrival_time"]:
                    pt["arrivalTime"] = p["arrival_time"]
                pts.append(pt)
        if len(pts) >= 2:
            body["points"] = pts
        if dry:
            return None
        r = c.post_json("/api/admin/schedules", body)
        st.map_set("schedules", s["seed_id"], r["id"])

    todo = [s for s in plan["schedules"]
            if not st.map_get("schedules", s["seed_id"])]
    _run_stage("schedules", todo, sched_worker, workers, len(todo))
    st.save(force=True)
    print(f"[schedules] mapped "
          f"{len(st.data.get('schedules', {}))}/{len(plan['schedules'])}")

    # 4. pickup points --------------------------------------------------------
    def pickup_worker(p):
        route_id = route_id_by_seed.get(p["route_seed_id"])
        if not route_id:
            return None
        body = {
            "routeId": route_id, "name": p["name"],
            "stopOrder": p["stop_order"],
            "kind": p["kind"], "lat": p["lat"], "lon": p["lon"],
        }
        if p.get("address"):
            body["address"] = p["address"]
        if dry:
            return None
        r = c.post_json("/api/admin/pickup-points", body)
        st.map_set("pickups", f"{p['route_seed_id']}:{p['address_seed_id']}",
                   r["id"])

    todo = [p for p in plan["pickup_points"]
            if not st.map_get("pickups",
                              f"{p['route_seed_id']}:{p['address_seed_id']}")]
    _run_stage("pickup-points", todo, pickup_worker, workers, len(todo))
    st.save(force=True)
    print(f"[pickup-points] mapped "
          f"{len(st.data.get('pickups', {}))}/{len(plan['pickup_points'])}")


# ── Phase: pictures ──────────────────────────────────────────────────
def phase_pictures(c: Client, plan, st: State, dry: bool, limit: int | None,
                   workers: int = 1):
    route_id_by_seed = st.data.get("routes", {})
    todo = []
    for pic in plan["pictures"]:
        key = f"{pic['route_seed_id']}:{pic['file']}"
        if st.map_get("pictures", key):
            continue
        if not route_id_by_seed.get(pic["route_seed_id"]):
            continue
        todo.append(pic)
        if limit and len(todo) >= limit:
            break
    counter = {"dedup": 0}

    def pic_worker(pic):
        key = f"{pic['route_seed_id']}:{pic['file']}"
        route_id = route_id_by_seed.get(pic["route_seed_id"])
        if dry:
            return None
        r = c.upload_picture(route_id, pic["file"], pic.get("alt"))
        st.map_set("pictures", key, r.get("picture", {}).get("id", "?"))
        if r.get("deduped"):
            with st.lock:
                counter["dedup"] += 1

    fails = _run_stage("pictures", todo, pic_worker, workers, len(todo))
    st.save(force=True)
    print(f"[pictures] attempted {len(todo)} (fails {fails}, "
          f"deduped {counter['dedup']}), mapped "
          f"{len(st.data.get('pictures', {}))}/{len(plan['pictures'])}")


# ── Phase: verify ────────────────────────────────────────────────────
def phase_verify(c: Client, plan, st: State):
    ok = True
    brands = c.get_json("/api/admin/brands")["items"]
    print(f"[verify] brands on server: {len(brands)} (plan: {len(plan['brands'])})")
    if len(brands) < len(plan["brands"]) * 0.99:
        ok = False

    # paginated route count
    total = c.get_json("/api/admin/routes?limit=1").get("total")
    print(f"[verify] routes on server: {total} (plan: {len(plan['routes'])})")
    if (total or 0) < len(plan["routes"]) * 0.99:
        ok = False

    # schedules: count via a sample of routes
    import random
    from collections import Counter
    random.seed(7)
    exp_per_route = Counter(s["route_seed_id"] for s in plan["schedules"])
    sample = random.sample(list(st.data.get("routes", {}).items()), 25)
    sched_total = sched_expected = 0
    for seed_id, api_id in sample:
        scheds = c.get_json(f"/api/admin/schedules?routeId={api_id}")["items"]
        sched_total += len(scheds)
        sched_expected += exp_per_route.get(seed_id, 0)
    print(f"[verify] schedules on 25-route sample: {sched_total} "
          f"(plan expects {sched_expected})")
    if sched_total < sched_expected:
        print("[verify] !! some schedules are missing on the sample")

    # spot-check a schedule with points (price must match the plan)
    with_pts = [s for s in plan["schedules"] if s["points"]]
    s = with_pts[len(with_pts) // 2]
    api_id = st.data["schedules"][s["seed_id"]]
    route_api_id = st.data["routes"][s["route_seed_id"]]
    d = c.get_json(f"/api/admin/schedules?routeId={route_api_id}")
    sched = next((x for x in d["items"] if x["id"] == api_id), None)
    if sched:
        print(f"[verify] sample schedule {api_id[:8]}: dep={sched['departureTime']} "
              f"price={sched['basePriceAdult']} points={len(sched.get('points', []))} "
              f"layout={bool(sched.get('busLayoutId'))} vt={sched.get('vehicleTypeId') is not None}")
        assert sched["basePriceAdult"] == s["base_price_adult"], "PRICE MISMATCH"
    else:
        print("[verify] !! sample schedule not found")

    # pictures on one route
    some_route = next(iter(st.data.get("routes", {}).values()))
    pics = c.get_json(f"/api/admin/routes/{some_route}/pictures")
    print(f"[verify] pictures on sample route: {len(pics.get('items', []))}")

    print(f"[verify] {'OK' if ok else 'MISMATCHES FOUND'} — "
          f"open https://datxevui.com/search to smoke-test.")


PHASES = {
    "brands": phase_brands,
    "db-layouts": phase_db_layouts,
    "data": phase_data,
    "pictures": phase_pictures,
    "verify": phase_verify,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("DXV_BASE",
                                                     "https://datxevui.com"))
    ap.add_argument("--email", default=os.environ.get("DXV_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("DXV_PASSWORD"))
    ap.add_argument("--phase", required=True, choices=list(PHASES))
    ap.add_argument("--rps", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel requests inside a stage (data/pictures)")
    ap.add_argument("--plan", default=str(PLAN_PATH))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="pictures: cap the number of uploads this run")
    ap.add_argument("--sql-out", default=str(HERE / "layouts_seats.sql"))
    args = ap.parse_args()

    if not (args.email and args.password):
        raise SystemExit("set DXV_EMAIL / DXV_PASSWORD")

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    st = State(STATE_PATH)
    c = Client(args.base, args.email, args.password, rps=args.rps)
    c.login()

    if args.phase == "db-layouts":
        phase_db_layouts(c, plan, st, Path(args.sql_out))
        return
    if args.phase == "pictures":
        phase_pictures(c, plan, st, args.dry, args.limit, args.workers)
        return
    if args.phase == "data":
        phase_data(c, plan, st, args.dry, args.workers)
        return
    if args.phase == "verify":
        phase_verify(c, plan, st)
    else:
        PHASES[args.phase](c, plan, st, args.dry)


if __name__ == "__main__":
    main()
