#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supplement_pairs.py — reconcile production with the enriched excel.

The 2026-10-10 N×N recrawl widened the catalog from 60 to 130 city pairs,
but the imported seed database was built from the earlier 60-pair crawl,
so production is missing ~70 pairs (~2,600 schedule-class rows) plus a
handful of services on shared pairs. This script diffs the LIVE site
against `data/vexere_nxn_trips_enriched.xlsx` (the authoritative catalog)
and pushes only what is missing, through the admin API:

  plan    build the diff from the live site (brands/routes/layouts/
          schedules) + the excel; prints a summary and caches it in
          the state file
  push    addresses → routes → schedules (points + class fares)
          → pickup points; resumable via supplement_state.json
  verify  recount after the push

Design notes
------------
* Services are compared at (from, to, operator, departure) grain — the
  excel collapses layouts into one row per seat class, the site keeps
  one schedule per layout, so ANY schedule at a departure marks the
  service as present (never duplicated).
* Seat-class labels map to the global canonical layout catalog by
  family + parsed seat count, nearest count when there is no exact
  match — the same trade-off `layouts-canonicalize` already made.
* Class fares follow the crawl's own seat classes: VIP → vip,
  tầng 2/tầng trên → bed_upper, tầng 1/tầng dưới → bed_lower, anything
  else is the standard base price.
* Everything runs gently (default 12 rps, single worker) — rust-sql's
  write path is not to be trusted under concurrency (see the migration
  README's engine lessons).

Usage:
  export DXV_EMAIL=... DXV_PASSWORD=...
  python3 supplement_pairs.py --phase plan
  python3 supplement_pairs.py --phase push [--dry]
  python3 supplement_pairs.py --phase verify
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
from collections import defaultdict
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from api_client import Client  # noqa: E402

# App city slugs (source of truth: rust-be-template src/cities.rs —
# kept in sync by that repo's has_all_cities_in_sync_with_frontend test;
# exported verbatim to app_cities.json next to this script).
CITY_NAMES = json.loads((HERE / "app_cities.json").read_text(encoding="utf-8"))
NAME_TO_SLUG = {}
for _s, _n in CITY_NAMES.items():
    NAME_TO_SLUG[_n] = _s
    if _n.startswith("TP. "):
        NAME_TO_SLUG[_n[4:]] = _s
    NAME_TO_SLUG[_n.replace(" - ", "-")] = _s
NAME_TO_SLUG["Bà Rịa-Vũng Tàu"] = "ba-ria-vung-tau"

EXCEL = HERE.parent.parent / "data" / "vexere_nxn_trips_enriched.xlsx"
SEED_DB = HERE.parent.parent / "data" / "booking_seed.sqlite"
STATE_PATH = HERE / "supplement_state.json"


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def strip_tones(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D").lower()


# ── excel side ──────────────────────────────────────────────────────

def load_excel_services():
    """(from, to, operator) → {departure: service} — service carries every
    seat-class row observed at that departure."""
    wb = openpyxl.load_workbook(EXCEL, read_only=True, data_only=True)
    ws = wb["Schedules"]
    it = ws.iter_rows(values_only=True)
    for _ in range(3):
        next(it)
    header = [str(h).strip() if h else "" for h in next(it)]
    col = {name: i for i, name in enumerate(header)}

    services = defaultdict(dict)
    skipped = 0
    for r in it:
        if not r or all(v is None for v in r):
            continue
        dep = str(r[col["Departure"]] or "")
        if "--:--" in dep or not re.match(r"^\d{1,2}:\d{2}$", dep):
            skipped += 1
            continue
        frm = NAME_TO_SLUG.get(str(r[col["From City"]] or ""))
        to = NAME_TO_SLUG.get(str(r[col["To City"]] or ""))
        op = nfc(str(r[col["Operator"]] or "").strip())
        if not (frm and to and op):
            skipped += 1
            continue
        hh, mm = dep.split(":")
        dep = f"{int(hh):02d}:{mm}"
        bundle = services[(frm, to, op)].setdefault(dep, {
            "rows": [], "dep_point": None, "arr_point": None,
            "arrival": None, "duration": None,
        })
        bundle["rows"].append({
            "seat_class": str(r[col["Seat Class"]] or "").strip(),
            "list_price": int(r[col["List Price (VND)"]] or 0),
        })
        if bundle["dep_point"] is None:
            bundle["dep_point"] = str(r[col["Departure Point"]] or "").strip()
            bundle["arr_point"] = str(r[col["Arrival Point"]] or "").strip()
            arr = str(r[col["Arrival"]] or "")
            bundle["arrival"] = arr if re.match(r"^\d{1,2}:\d{2}$", arr) else None
            try:
                bundle["duration"] = int(r[col["Duration (min)"]] or 0) or None
            except (TypeError, ValueError):
                bundle["duration"] = None
    return services, skipped


# ── canonical layout picker ─────────────────────────────────────────

def layout_family(label: str) -> str:
    s = strip_tones(label)
    want = layout_count(label)
    if "phong" in s or "cabin" in s:
        return "cabin"
    if any(w in s for w in ("giuong", "bed", "sleeper")):
        # small "limousine giường nằm đơn/đôi" vehicles are cabin limos
        if ("limo" in s) and want and want <= 24:
            return "cabin"
        return "berth"
    if "limousine" in s or "limo" in s:
        return "limo"
    if re.match(r"^xe\s*\d+\s*cho", s) or any(
            w in s for w in ("fortuner", "xpander", "carnival", "sedan")):
        return "van"
    return "seat"


def layout_count(label: str):
    m = re.search(r"(\d+)\s*(cho|phong|giuong|ghe|seats?)", strip_tones(label))
    return int(m.group(1)) if m else None


class LayoutCatalog:
    """Nearest global canonical layout per (family, seat count)."""

    def __init__(self, layouts):
        self.globals = [l for l in layouts if not l.get("brandId")]
        self.by_family = defaultdict(list)
        for l in self.globals:
            name = strip_tones(l.get("name") or "")
            fam = ("cabin" if "phong" in name else
                   "berth" if "giuong" in name else
                   "limo" if "limousine" in name else
                   "van" if "xe 7" in name or "xe 4" in name else "seat")
            self.by_family[fam].append(l)

    def pick(self, label: str):
        fam = layout_family(label)
        want = layout_count(label)
        vip = "vip" in strip_tones(label)
        # a "Limousine N chỗ" with more than 16 seats is a bed limo;
        # one with ≤ 8 seats is a VIP van (Xe 7 chỗ canonical)
        if fam == "limo" and want and want >= 17:
            fam = "berth"
        elif fam == "limo" and want and want <= 8:
            vans = [l for l in self.by_family.get("van", [])
                    if l.get("totalSeats") in (want, want - 1)]
            if vans:
                return vans[0]
        cands = self.by_family.get(fam) or []
        if not cands:  # family fallback: limo beds sit in berth/limo
            cands = self.by_family.get("berth" if fam == "limo" else "limo", [])
        if not cands:
            return None
        exact = [l for l in cands if want and l.get("totalSeats") == want]
        if exact:
            best = exact[0]
            if vip:
                for l in exact:
                    if "vip" in strip_tones(l.get("name") or ""):
                        best = l
                        break
            return best
        return min(cands, key=lambda l: abs((l.get("totalSeats") or 0) -
                                            (want or 999)))


def class_fares(rows):
    """Crawl seat classes → canonical class fares (standard = base price)."""
    by_class = {}
    for r in rows:
        s = strip_tones(r["seat_class"])
        if "vip" in s:
            cls = "vip"
        elif "tang 2" in s or "tang tren" in s:
            cls = "bed_upper"
        elif "tang 1" in s or "tang duoi" in s:
            cls = "bed_lower"
        else:
            continue
        p = r["list_price"]
        if cls not in by_class or p < by_class[cls]:
            by_class[cls] = p
    return [{"seatClass": c, "priceAdult": p} for c, p in by_class.items()]


# ── state ───────────────────────────────────────────────────────────

class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else {}

    def save(self):
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1),
            encoding="utf-8")


# ── live prod snapshot ──────────────────────────────────────────────

def fetch_prod(c: Client):
    def paged(path):
        out, off = [], 0
        while True:
            d = c.get_json(f"{path}?limit=200&offset={off}")
            items = d.get("items") or []
            out += items
            off += len(items)
            if not items or len(items) < 200:
                break
        return out

    brands = c.get_json("/api/admin/brands")["items"]
    return brands, paged("/api/admin/routes"), paged("/api/admin/bus-layouts")


def build_plan(c: Client, services):
    """Diff the live site against the excel services → push todo list."""
    brands, routes, layouts = fetch_prod(c)
    brand_by_name = {nfc(b["name"]): b for b in brands}
    cat = LayoutCatalog(layouts)
    name_by_id = {b["id"]: nfc(b["name"]) for b in brands}

    route_by_key = {}
    for r in routes:
        key = (r["startLocationId"], r["endLocationId"],
               name_by_id.get(r["brandId"], ""))
        route_by_key[key] = r

    todo_scheds, missing_brands = [], []
    for (frm, to, op), deps in sorted(services.items()):
        brand = brand_by_name.get(op)
        if not brand:
            missing_brands.append(op)
            continue
        key = (frm, to, op)
        route = route_by_key.get(key)
        present = set()
        if route:
            for s in (c.get_json(f"/api/admin/schedules?routeId={route['id']}")
                      .get("items") or []):
                present.add(s.get("departureTime"))
        for dep, bundle in sorted(deps.items()):
            if dep in present:
                continue
            row = bundle["rows"][0]
            layout = cat.pick(row["seat_class"])
            todo_scheds.append({
                "key": list(key), "dep": dep, "brandId": brand["id"],
                "routeId": route["id"] if route else None,
                "brand_name": op,
                "dep_point": bundle["dep_point"], "arr_point": bundle["arr_point"],
                "arrival": bundle["arrival"], "duration": bundle["duration"],
                "base_price": min(r["list_price"] for r in bundle["rows"]),
                "class_fares": class_fares(bundle["rows"]),
                "layout_id": layout["id"] if layout else None,
                "layout_name": (layout or {}).get("name"),
                "label": row["seat_class"],
            })
    return todo_scheds, missing_brands


# ── address / route / pickup helpers ────────────────────────────────

def geocode(c: Client, st: State, name: str):
    cache = st.data.setdefault("geocode", {})
    if name in cache:
        return tuple(cache[name]) if cache[name] else (0.0, 0.0)
    hit = None
    try:
        r = c.get_json(
            f"/api/places/search?q={urllib.parse.quote(name)}&limit=1")
        items = r.get("items") or []
        if items:
            hit = [items[0].get("lat") or 0.0, items[0].get("lon") or 0.0]
    except Exception:
        pass
    cache[name] = hit
    return tuple(hit) if hit else (0.0, 0.0)


def seed_address(brand_name: str, point_name: str):
    """The seed DB has geocoded coordinates for crawled point pools."""
    try:
        con = sqlite3.connect(f"file:{SEED_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return con.execute(
            "SELECT a.lat, a.lon, a.address, a.province, a.district, a.ward "
            "FROM address a JOIN brand b ON a.brand_id = b.id "
            "WHERE b.name = ? AND a.name = ? COLLATE NOCASE",
            (brand_name, point_name)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def ensure_address(c: Client, st: State, brand_id, brand_name, point_name):
    """Get-or-create a brand address for a crawl point; returns
    (api_id, lat, lon, address_line)."""
    key = f"{brand_id}:{nfc(point_name)}"
    cached = st.data.setdefault("addresses", {}).get(key)
    if cached:
        return cached["id"], cached["lat"], cached["lon"], cached.get("address")
    row = seed_address(brand_name, point_name)
    if row:
        lat, lon, address = row[0] or 0.0, row[1] or 0.0, row[2]
        extra = dict(zip(("address", "province", "district", "ward"), row[2:]))
    else:
        lat, lon = geocode(c, st, point_name)
        address, extra = None, {}
    body = {"brandId": brand_id, "name": point_name,
            "lat": lat, "lon": lon}
    for k in ("address", "province", "district", "ward"):
        if extra.get(k):
            body[k] = extra[k]
    r = c.post_json("/api/admin/addresses", body)
    st.data["addresses"][key] = {"id": r["id"], "lat": lat, "lon": lon,
                                 "address": address}
    st.save()
    return r["id"], lat, lon, address


def ensure_route(c: Client, st: State, key, brand_id, name):
    rk = ":".join(key)
    rid = st.data.setdefault("routes", {}).get(rk)
    if rid:
        return rid
    r = c.post_json("/api/admin/routes", {
        "brandId": brand_id, "name": name,
        "startLocationId": key[0], "endLocationId": key[1],
        "status": "active",
    })
    st.data["routes"][rk] = r["id"]
    st.save()
    return r["id"]


def ensure_pickup(c: Client, st: State, route_id, name, addr, lat, lon,
                  stop_order):
    pk = f"{route_id}:{name}"
    if pk in st.data.setdefault("pickups", {}):
        return
    body = {"routeId": route_id, "name": name, "stopOrder": stop_order,
            "kind": "station", "lat": lat, "lon": lon}
    if addr:
        body["address"] = addr
    c.post_json("/api/admin/pickup-points", body)
    st.data["pickups"][pk] = True
    st.save()


def plus_minutes(hhmm: str, minutes: int) -> str:
    hh, mm = map(int, hhmm.split(":"))
    total = (hh * 60 + mm + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


# ── phases ──────────────────────────────────────────────────────────

def phase_plan(c: Client, st: State):
    services, skipped = load_excel_services()
    todo_scheds, missing_brands = build_plan(c, services)
    pairs = sorted({tuple(t["key"][:2]) for t in todo_scheds})
    new_routes = {tuple(t["key"]) for t in todo_scheds if not t["routeId"]}
    no_layout = [t for t in todo_scheds if not t["layout_id"]]
    print(f"[plan] excel services: {sum(len(v) for v in services.values())} "
          f"({skipped} rows skipped: unmapped city / no departure time)")
    print(f"[plan] routes to create : {len(new_routes)}")
    print(f"[plan] schedules to push: {len(todo_scheds)} "
          f"across {len(pairs)} pairs")
    if missing_brands:
        print(f"[plan] !! operators with no prod brand: "
              f"{sorted(set(missing_brands))[:5]}")
    print(f"[plan] services without a canonical layout: {len(no_layout)}")
    approx = [t for t in todo_scheds if t["layout_name"] and
              layout_count(t["label"]) and
              layout_count(t["layout_name"] or "") != layout_count(t["label"])]
    print(f"[plan] layout approximations (nearest count): {len(approx)}")
    for t in approx[:8]:
        print(f"        {t['label']!r} -> {t['layout_name']!r}")
    st.data["plan"] = todo_scheds
    st.save()
    return todo_scheds


def phase_push(c: Client, st: State, dry: bool, limit: int | None):
    todo = st.data.get("plan")
    if not todo:
        todo = phase_plan(c, st)
    if limit:
        todo = todo[:limit]
    print(f"[push] {len(todo)} schedules todo — "
          f"{'DRY RUN — nothing is written' if dry else 'live run'}")
    if dry:
        return

    brands = c.get_json("/api/admin/brands")["items"]
    brand_by_name = {nfc(b["name"]): b for b in brands}

    # 1. routes --------------------------------------------------------------
    route_ids = {}
    for t in todo:
        key = tuple(t["key"])
        if t["routeId"]:
            route_ids[key] = t["routeId"]
        elif key not in route_ids:
            route_ids[key] = ensure_route(
                c, st, t["key"], t["brandId"],
                f"{CITY_NAMES[key[0]]} → {CITY_NAMES[key[1]]}")
    created = sum(1 for t in todo if not t["routeId"])
    print(f"[push] routes: {created} created, "
          f"{len(route_ids) - created} existing")

    # 2. addresses (deduped per brand+point) --------------------------------
    addr_cache = {}
    points = {(t["brand_name"], t["dep_point"]) for t in todo} | \
             {(t["brand_name"], t["arr_point"]) for t in todo}
    for op, point in sorted(p for p in points if p[1]):
        brand = brand_by_name.get(op)
        if not brand:
            continue
        addr_cache[(op, point)] = ensure_address(
            c, st, brand["id"], op, point)
    print(f"[push] addresses ready: {len(addr_cache)}")

    # 3. schedules -----------------------------------------------------------
    pushed = skipped_pts = 0
    for t in todo:
        key = tuple(t["key"])
        route_id = route_ids[key]
        sk = f"{':'.join(key)}@{t['dep']}"
        if st.data.setdefault("schedules", {}).get(sk):
            pushed += 1
            continue
        dep = addr_cache.get((t["brand_name"], t["dep_point"]))
        arr = addr_cache.get((t["brand_name"], t["arr_point"]))
        body = {"routeId": route_id, "departureTime": t["dep"],
                "basePriceAdult": t["base_price"]}
        if t["layout_id"]:
            body["busLayoutId"] = t["layout_id"]
        if t["class_fares"]:
            body["classFares"] = t["class_fares"]
        if dep and arr:
            arr_time = t["arrival"] or (
                plus_minutes(t["dep"], t["duration"]) if t["duration"]
                else None)
            body["points"] = [{"addressId": dep[0], "arrivalTime": t["dep"]},
                              {"addressId": arr[0], **(
                                  {"arrivalTime": arr_time} if arr_time
                                   else {})}]
        else:
            skipped_pts += 1
        r = c.post_json("/api/admin/schedules", body)
        st.data["schedules"][sk] = r["id"]
        st.save()
        pushed += 1
        # route-level boarding/drop points (the booking flow needs them)
        if dep:
            ensure_pickup(c, st, route_id, t["dep_point"] or "Điểm đón",
                          dep[3], dep[1], dep[2], 1)
        if arr:
            ensure_pickup(c, st, route_id, t["arr_point"] or "Điểm trả",
                          arr[3], arr[1], arr[2], 2)
        if pushed % 25 == 0:
            print(f"[push] schedules: {pushed}/{len(todo)}", flush=True)
    print(f"[push] schedules pushed: {pushed} "
          f"({skipped_pts} without a full point pair)")


def phase_verify(c: Client):
    services, _ = load_excel_services()
    brands, routes, layouts = fetch_prod(c)
    pairs = {(r["startLocationId"], r["endLocationId"]) for r in routes}
    excel_pairs = {k[:2] for k in services}
    print(f"[verify] prod pairs now : {len(pairs)} (excel: {len(excel_pairs)})")
    missing = excel_pairs - pairs
    if missing:
        print(f"[verify] !! still missing: {sorted(missing)}")
    else:
        print("[verify] all excel pairs present")
    sched_total = sum(r.get("scheduleCount") or 0 for r in routes)
    print(f"[verify] prod schedules : {sched_total}")

    import random
    random.seed(3)
    for key in random.sample(sorted(services), 5):
        frm, to, op = key
        route = next((r for r in routes if r["startLocationId"] == frm
                      and r["endLocationId"] == to), None)
        if not route:
            print(f"[verify] {frm}->{to} ({op[:24]!r}): NO ROUTE")
            continue
        items = c.get_json(f"/api/admin/schedules?routeId={route['id']}") \
            .get("items") or []
        dep = next(iter(services[key]))
        m = next((s for s in items if s.get("departureTime") == dep), None)
        print(f"[verify] {frm}->{to} {op[:24]!r} @{dep}: "
              f"{('OK price ' + str(m.get('basePriceAdult'))) if m else 'MISSING'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("DXV_BASE",
                                                     "https://datxevui.com"))
    ap.add_argument("--email", default=os.environ.get("DXV_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("DXV_PASSWORD"))
    ap.add_argument("--phase", required=True,
                    choices=["plan", "push", "verify"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="push only the first N schedules (staged rollout)")
    ap.add_argument("--rps", type=float, default=12.0)
    args = ap.parse_args()

    c = Client(args.base, args.email, args.password, rps=args.rps)
    c.login()
    st = State(STATE_PATH)
    if args.phase == "plan":
        phase_plan(c, st)
    elif args.phase == "push":
        phase_push(c, st, args.dry, args.limit)
    else:
        phase_verify(c)


if __name__ == "__main__":
    main()
