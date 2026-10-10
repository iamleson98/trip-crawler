#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_migration_v2.py — Push the v2 migration plan into a FRESH
datxevui database through the admin API (no direct-DB steps).

Phases (run in this order; each is resumable via the state file):

  bootstrap   register the first admin account on an empty DB + login
              (the first registered user is promoted to admin by the
              backend). Skipped automatically when login already works.
  brands      POST /api/admin/brands                        (312)
  layouts     POST /api/admin/bus-layouts with a full seat
              `plan` — the backend generates the seat rows itself
              (replaces the old direct-SQL db-layouts phase)
  data        POST addresses → routes → schedules(+points+classFares)
              → pickup points
  pictures    POST /api/admin/routes/{id}/pictures  (multipart)
  verify      count-check every table + smoke test

Differences vs v1 (run_migration.py):
  * `layouts` goes through the admin API — the backend now has full
    write endpoints for bus layouts (v1 had to stop the service and
    INSERT bus_layout/seat rows via rustqlite-cli).
  * schedules carry `classFares` → `schedule_fare` rows (per-seat-class
    pricing from the crawl).
  * schedules are created with daysOfWeek = null (daily), no effective
    window — same recurring policy as v1.

Usage:
  export DXV_EMAIL=... DXV_PASSWORD=...   # admin account
  python3 run_migration_v2.py --base https://datxevui.com --phase bootstrap
  python3 run_migration_v2.py --base https://datxevui.com --phase brands
  python3 run_migration_v2.py --base https://datxevui.com --phase layouts
  python3 run_migration_v2.py --base https://datxevui.com --phase data --workers 6 --rps 40
  python3 run_migration_v2.py --base https://datxevui.com --phase pictures --workers 6 --rps 40
  python3 run_migration_v2.py --base https://datxevui.com --phase verify
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import threading
import time
import urllib.error
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api_client import Client

HERE = Path(__file__).resolve().parent
PLAN_PATH = HERE / "migration_plan_v2.json"
STATE_PATH = HERE / "migration_state_v2.json"


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


def _run_stage(name: str, items, worker, workers: int, total: int):
    """Run `worker(item)` over `items` with a thread pool; log progress.
    Failures are logged and left for the next resumable run."""
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


# ── Phase: bootstrap (fresh DB admin) ────────────────────────────────
def phase_bootstrap(c: Client, st: State, plan, dry: bool, full_name: str):
    """Register the first admin account on an empty DB (the backend
    promotes the first registrant to admin). No-op when login works."""
    try:
        c.login()
        print("[bootstrap] login already works — DB is not fresh, skipping")
        return
    except Exception as e:
        print(f"[bootstrap] login failed ({str(e)[:120]}) — registering admin")
    body = {
        "email": c.email,
        "password": c.password,
        "fullName": full_name,
    }
    if dry:
        print("  would POST /api/auth/register", body["email"])
        return
    data = json.loads(c.request("POST", "/api/auth/register", body=body))
    tok = (data.get("tokens") or {}).get("access_token")
    if tok:
        c.token = tok
        c.token_at = time.time()
    print(f"[bootstrap] admin registered + logged in ("
          f"user {(data.get('user') or {}).get('id', '?')[:8]})")


# ── Phase: brands ───────────────────────────────────────────────────
def phase_brands(c: Client, plan, st: State, dry: bool):
    existing = {}
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
        if b.get("rating") is not None:
            body["rating"] = b["rating"]
        if b.get("status"):
            body["status"] = b["status"]
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


# ── Phase: layouts (bus_layout + seat rows, all via the API) ─────────
def phase_layouts(c: Client, plan, st: State, dry: bool, workers: int = 1):
    missing = [b["slug"] for b in plan["brands"] if not st.brand_id(b["slug"])]
    if missing:
        raise SystemExit(f"{len(missing)} brands unmapped — run `brands` first")

    def lay_worker(lay):
        if st.map_get("layouts", lay["seed_id"]):
            return None
        brand_id = st.brand_id(lay["brand_slug"]) if lay.get("brand_slug") else None
        body = {
            "name": lay["name"],
            "brandId": brand_id,
            "vehicleType": lay["vehicle_code"],
            "totalSeats": lay["total_seats"],
            "plan": lay["plan"],
        }
        if dry:
            return None
        r = c.post_json("/api/admin/bus-layouts", body)
        st.map_set("layouts", lay["seed_id"], r["id"])

    todo = [l for l in plan["layouts"] if not st.map_get("layouts", l["seed_id"])]
    _run_stage("layouts", todo, lay_worker, workers, len(todo))
    st.save(force=True)
    print(f"[layouts] mapped {len(st.data.get('layouts', {}))}/{len(plan['layouts'])}")


# ── Phase: data ──────────────────────────────────────────────────────
def phase_data(c: Client, plan, st: State, dry: bool, workers: int = 1):
    brand_by_seed = {b["seed_id"]: b for b in plan["brands"]}
    layout_seed_by_new_id = {l["id"]: l["seed_id"] for l in plan["layouts"]}

    vt_ids = {v["code"]: v["id"] for v in
              c.get_json("/api/admin/vehicle-types").get("items", [])}
    print(f"[data] vehicle types: {list(vt_ids)}")

    # 0. geocode addresses without coordinates (district-wide zones) ────
    import urllib.parse
    todo_geo = [a for a in plan["addresses"] if a["lat"] == 0 and a["lon"] == 0]
    if todo_geo and not dry:
        cache = load_json(HERE / "geocode_cache_v2.json", {})
        print(f"[geocode] {len(todo_geo)} addresses without coordinates")
        for i, a in enumerate(todo_geo, 1):
            key = a["name"]
            if key in cache:
                hit = cache[key]
                if hit:
                    a["lat"], a["lon"] = hit
                continue
            q = urllib.parse.quote(a["name"])
            try:
                r = c.get_json(f"/api/places/search?q={q}&limit=1")
                items = r.get("items") or []
            except Exception as e:
                items = []
                print(f"  [geocode] search failed for {a['name']}: {e}")
            hit = None
            if items:
                it = items[0]
                hit = [it.get("lat", 0.0), it.get("lon", 0.0)]
            cache[key] = hit
            if hit:
                a["lat"], a["lon"] = hit
            if i % 20 == 0:
                print(f"  [geocode] {i}/{len(todo_geo)}")
        (HERE / "geocode_cache_v2.json").write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        ok = sum(1 for a in todo_geo if a["lat"] or a["lon"])
        print(f"[geocode] resolved {ok}/{len(todo_geo)}")

    # 1. addresses ---------------------------------------------------------
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

    # 3. schedules (+ points + class fares) ----------------------------------
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
        if s.get("base_price_child") is not None:
            body["basePriceChild"] = s["base_price_child"]
        if s["layout_id"]:
            seed_lid = layout_seed_by_new_id.get(s["layout_id"])
            lid = st.map_get("layouts", seed_lid) if seed_lid else None
            if lid:
                body["busLayoutId"] = lid
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
        if s.get("class_fares"):
            body["classFares"] = [
                {"seatClass": cf["seat_class"], "priceAdult": cf["price_adult"]}
                for cf in s["class_fares"]
            ]
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

    total = c.get_json("/api/admin/routes?limit=1").get("total")
    print(f"[verify] routes on server: {total} (plan: {len(plan['routes'])})")
    if (total or 0) < len(plan["routes"]) * 0.99:
        ok = False

    lay_total = c.get_json("/api/admin/bus-layouts?limit=1").get("total")
    print(f"[verify] bus layouts on server: {lay_total} (plan: {len(plan['layouts'])})")

    # schedules: count via a sample of routes
    random.seed(7)
    exp_per_route = Counter(s["route_seed_id"] for s in plan["schedules"])
    sample = random.sample(list(st.data.get("routes", {}).items()), 25)
    sched_total = sched_expected = fare_rows = 0
    for seed_id, api_id in sample:
        scheds = c.get_json(f"/api/admin/schedules?routeId={api_id}")["items"]
        sched_total += len(scheds)
        sched_expected += exp_per_route.get(seed_id, 0)
        fare_rows += sum(len(s.get("classFares") or []) for s in scheds)
    print(f"[verify] schedules on 25-route sample: {sched_total} "
          f"(plan expects {sched_expected}), classFare rows: {fare_rows}")
    if sched_total < sched_expected:
        print("[verify] !! some schedules are missing on the sample")

    # spot-check a schedule with points + class fares (price must match)
    with_fares = [s for s in plan["schedules"] if s["class_fares"]]
    if with_fares:
        s = with_fares[len(with_fares) // 2]
        api_id = st.data["schedules"][s["seed_id"]]
        route_api_id = st.data["routes"][s["route_seed_id"]]
        d = c.get_json(f"/api/admin/schedules?routeId={route_api_id}")
        sched = next((x for x in d["items"] if x["id"] == api_id), None)
        if sched:
            print(f"[verify] sample schedule {api_id[:8]}: "
                  f"dep={sched['departureTime']} "
                  f"price={sched['basePriceAdult']} "
                  f"classFares={[(f['seatClass'], f['priceAdult']) for f in sched.get('classFares') or []]} "
                  f"points={len(sched.get('points', []))} "
                  f"layout={bool(sched.get('busLayoutId'))} "
                  f"vt={sched.get('vehicleTypeId') is not None}")
            assert sched["basePriceAdult"] == s["base_price_adult"], "PRICE MISMATCH"
        else:
            print("[verify] !! sample schedule not found")

    # public smoke test: search a popular pair
    try:
        search = c.get_json("/api/trips?from=ha-noi&to=lao-cai&date=2026-10-11")
        items = search.get("items") or search.get("trips") or []
        print(f"[verify] public search ha-noi→lao-cai 2026-10-11: "
              f"{len(items)} trips")
        if items:
            t0 = items[0]
            print(f"[verify]   sample trip: {t0.get('brandName') or (t0.get('brand') or {}).get('name')} "
                  f"| {t0.get('departureTime')} | "
                  f"{(t0.get('priceRange') or {}).get('min') or t0.get('minPrice')} VND")
    except Exception as e:
        print(f"[verify] public search failed: {str(e)[:150]}")

    print(f"[verify] {'OK' if ok else 'MISMATCHES FOUND'} — "
          f"open https://datxevui.com/search to smoke-test.")


PHASES = {
    "bootstrap": phase_bootstrap,
    "brands": phase_brands,
    "layouts": phase_layouts,
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
    ap.add_argument("--full-name", default=os.environ.get("DXV_FULL_NAME",
                                                          "DatXeVui Admin"))
    ap.add_argument("--phase", required=True, choices=list(PHASES))
    ap.add_argument("--rps", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel requests inside a stage (data/pictures/layouts)")
    ap.add_argument("--plan", default=str(PLAN_PATH))
    ap.add_argument("--state", default=str(STATE_PATH))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="pictures: cap the number of uploads this run")
    args = ap.parse_args()

    if not (args.email and args.password):
        raise SystemExit("set DXV_EMAIL / DXV_PASSWORD")

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    st = State(Path(args.state))
    c = Client(args.base, args.email, args.password, rps=args.rps)

    if args.phase == "bootstrap":
        phase_bootstrap(c, st, plan, args.dry, args.full_name)
        return
    c.login()
    if args.phase == "brands":
        phase_brands(c, plan, st, args.dry)
    elif args.phase == "layouts":
        phase_layouts(c, plan, st, args.dry, args.workers)
    elif args.phase == "data":
        phase_data(c, plan, st, args.dry, args.workers)
    elif args.phase == "pictures":
        phase_pictures(c, plan, st, args.dry, args.limit, args.workers)
    elif args.phase == "verify":
        phase_verify(c, plan, st)


if __name__ == "__main__":
    main()
