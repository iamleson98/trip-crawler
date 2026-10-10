#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prepare_v2.py — Build the v2 migration plan for the fresh-datxevui rebuild.

Sources
-------
  * booking seed DB (built by seed_booking_db.py from the latest N×N crawl)
  * data/brand-research/final_brands.json   (ratings / phones / offices)
  * data/vehicle-type-media/vehicle_type_media.json  (route pictures)
  * the RAW N×N crawl (/tmp/nxn_out/*/trips.jsonl) — per-seat-class fares
  * preset_cache.json — the backend's own bus-layout presets (fetched once
    via scripts/fetch_presets.py)

What v2 adds over the original prepare.py
-----------------------------------------
  1. **Seat-class fares** — the crawl's `available_seat_info.seat_group`
     gives each seat class the operator actually sells ("VIP",
     "CABIN ĐƠN TẦNG 2", …) with its price. They are mapped onto the
     backend's canonical classes and emitted as `class_fares` per
     schedule → `schedule_fare` rows via the admin API.
  2. **Layouts carry a full seat PLAN** (the backend's own preset when the
     capacity matches, else a locally generated plan in the same style).
     Seat rows are created by the BACKEND (`POST /api/admin/bus-layouts`
     with `plan`) — the direct-SQL `db-layouts` phase is gone.
  3. days_of_week is null (daily recurring) — same policy as v1: the
     3-day crawl window is a crawl artifact, not an operating calendar.

Canonical seat classes (backend `dto/seat_plan.rs` + `fares.rs`):
  bed_lower  — berths/cabins on deck 1
  bed_upper  — berths/cabins on deck 2
  vip        — premium seats (explicit seatClass on limousine plans)
  standard   — everything without a class (schedule.base_price_adult)

Class mapping from Vexere's names (case/diacritic-insensitive substring):
  contains 'vip'                          → vip
  contains 'tầng 2' / 'tầng trên'          → bed_upper
  contains 'tầng 1' / 'tầng dưới'          → bed_lower
  anything else                            → standard (base price; no row)

Usage:
    python3 prepare_v2.py \
        [--seed PATH] [--research PATH] [--media PATH] \
        [--crawl-root /tmp/nxn_out] [--presets preset_cache.json] \
        [--out migration_plan_v2.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vi import (one_space, restore_district, restore_province, restore_ward)
from prepare import (
    CITY_NAMES, VALID_CITY_SLUGS, build_description, extract_phone,
    map_vehicle_code, norm_label, now_iso, offset_to_hhmm, hhmm_valid,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


# ── Canonical class mapping ──────────────────────────────────────────
def strip_tones(s: str) -> str:
    """Lowercase + drop Vietnamese tones for fuzzy matching."""
    s = s.replace("Đ", "D").replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def canon_class(name: str) -> str | None:
    """Vexere seat-group name → canonical class (None = standard)."""
    n = strip_tones((name or "").strip().lower())
    if not n:
        return None
    if "vip" in n:
        return "vip"
    if "tang 2" in n or "tang tren" in n or n == "tren" or "upper" in n:
        return "bed_upper"
    if "tang 1" in n or "tang duoi" in n or n == "duoi" or "lower" in n:
        return "bed_lower"
    return None


# ── Seat-plan generation (mirrors src/service/seat_plan_presets.rs) ──
def _deck(rows: int, cols: int, cells: list[dict], name: str | None = None) -> dict:
    return {"rows": rows, "cols": cols, "cells": cells, **({"name": name} if name else {})}


def _cell(row: int, col: int, kind: str, label: str | None = None,
          seat_class: str | None = None) -> dict:
    c = {"row": row, "col": col, "kind": kind}
    if label:
        c["label"] = label
    if seat_class:
        c["seatClass"] = seat_class
    return c


def berth_plan(total: int, wc: bool = False) -> dict:
    """Two-deck 3-lane sleeper (6·5·6 style): driver+door deck 1, stairs
    deck 2, berths in columns 1/3/5, rear bench widened to reach `total`.
    Odd totals put the extra berth on the lower deck. Mirrors
    `sleeper(per_deck, wc)` in the backend."""
    upper_n = total // 2
    lower_n = total - upper_n

    def deck_cells(upper: bool, n: int, rows_hint: int):
        letter = "B" if upper else "A"
        cells = []
        if upper:
            cells.append(_cell(1, 5, "stairs"))
        else:
            cells.append(_cell(1, 1, "driver"))
            cells.append(_cell(1, 5, "door"))
        rear = n % 3
        if rear == 1:
            rear = 4
        elif rear == 2:
            rear = 5
        full = (n - rear) // 3 if rear else n // 3
        bench_row = 2 + full
        idx = 0
        for r in range(full):
            for col in (1, 3, 5):
                idx += 1
                cells.append(_cell(2 + r, col, "bed", f"{letter}{idx:02d}"))
        if rear:
            cols = [1, 2, 3, 4, 5] if rear == 5 else [1, 2, 4, 5]
            for col in cols:
                idx += 1
                if wc and not upper and col == 3 and rear == 4:
                    cells.append(_cell(bench_row, 3, "wc"))
                    continue
                cells.append(_cell(bench_row, col, "bed", f"{letter}{idx:02d}"))
        rows = bench_row if rear else 1 + full
        return _deck(rows, 5, cells)

    # a shared row count keeps both decks visually aligned
    d1 = deck_cells(False, lower_n, 0)
    d2 = deck_cells(True, upper_n, 0)
    rows = max(d1["rows"], d2["rows"])
    d1["rows"] = d2["rows"] = rows
    return {"decks": [d1, d2]}


def cabin_plan(total: int, double_rear: bool = False, wc: bool = False) -> dict:
    """Two-deck cabin limousine: two lanes of single cabins (cols 1/3),
    an odd remainder becomes the rear centre cabin (a double when
    requested). Driver+door on deck 1, stairs on deck 2; a WC is a rear
    fixture cell that costs no sellable seat. Capacity is EXACT."""
    upper = (total + 1) // 2          # cabins on the upper deck
    lower = total - upper             # cabins on the lower deck

    def rows_for(n: int) -> int:
        # n cabins → rows of 2 lanes (+ a rear centre cell when odd)
        return (n + 1) // 2

    rows = 1 + max(rows_for(upper), rows_for(lower)) + (1 if wc else 0)

    def deck(upper_deck: bool, n: int) -> dict:
        letter = "B" if upper_deck else "A"
        cells = []
        if upper_deck:
            cells.append(_cell(1, 3, "stairs"))
        else:
            cells.append(_cell(1, 1, "driver"))
            cells.append(_cell(1, 3, "door"))
        n_rows = n // 2
        odd = n % 2 == 1
        idx = 0
        for r in range(n_rows):
            for col in (1, 3):
                idx += 1
                cells.append(_cell(2 + r, col, "cabin", f"{letter}{idx:02d}"))
        if odd:
            idx += 1
            kind = ("cabin_double" if (upper_deck and double_rear) else "cabin")
            cells.append(_cell(2 + n_rows, 2, kind, f"{letter}{idx:02d}"))
        if wc and not upper_deck:
            # rear-centre WC fixture (its own row; nothing sellable lost)
            cells.append(_cell(2 + n_rows + (1 if odd else 0), 2, "wc"))
        return _deck(rows, 3, cells)

    return {"decks": [deck(False, lower), deck(True, upper)]}


def coach_plan(total: int) -> dict:
    """Seated coach: rows of 2+2 (cols 1/2/4/5), rear row 5-wide.
    Mirrors `coach(n_rows)`; capacity is made exact by trimming the rear."""
    rows = (total + 3) // 4
    cells = [_cell(1, 3, "driver")]
    n = 0
    for r in range(1, rows + 1):
        cols = [1, 2, 3, 4, 5] if r == rows else [1, 2, 4, 5]
        for col in cols:
            if n >= total:
                break
            if r == 1 and col == 3:
                continue  # driver
            n += 1
            cells.append(_cell(r, col, "seat", f"{n:02d}"))
    return {"decks": [_deck(rows, 5, cells)]}


def limo_seat_plan(total: int) -> dict:
    """VIP limousine seats: rows of 2 with aisle (cols 1/3), rear row
    3-wide (cols 1/2/3). Every seat carries the `vip` class — same as the
    v1 migration's limousine seat maps. Mirrors `limo_11`."""
    rows = (total + 1) // 2
    cells = [_cell(1, 1, "driver")]
    n = 0
    for r in range(1, rows + 1):
        cols = [1, 2, 3] if r == rows else [1, 3]
        for col in cols:
            if n >= total:
                break
            if r == 1 and col == 1:
                continue  # driver
            n += 1
            cells.append(_cell(r, col, "seat", f"{n:02d}", "vip"))
    return {"decks": [_deck(rows, 3, cells)]}


def car_plan(total: int) -> dict:
    """Small car/MPV: driver + front passenger, then rows of 3 seats."""
    cells = [_cell(1, 1, "driver"), _cell(1, 3, "seat", "01")]
    n = 1
    row = 2
    while n < total and row <= 8:
        for col in (1, 2, 3):
            if n >= total:
                break
            n += 1
            cells.append(_cell(row, col, "seat", f"{n:02d}"))
        row += 1
    return {"decks": [_deck(max(2, row - 1), 3, cells)]}


def plan_sellable(plan: dict) -> int:
    kinds = {"seat", "bed", "cabin", "cabin_double"}
    return sum(1 for d in plan["decks"] for c in d["cells"] if c["kind"] in kinds)


def plan_labels_unique(plan: dict) -> bool:
    labels = [c["label"] for d in plan["decks"] for c in d["cells"]
              if c.get("label")]
    return len(labels) == len(set(l.lower() for l in labels))


# ── Preset inference (mirrors backend `infer_id` + `find`) ───────────
PRESET_CAPACITY = {
    "car_4": 4, "car_7": 6, "limo_9": 9, "limo_11": 11, "minibus_15": 15,
    "seat_29": 29, "seat_45": 45, "sleeper_38_wc": 38, "sleeper_40": 40,
    "sleeper_42": 42, "sleeper_44": 44, "sleeper_46": 46,
    "limo_bed_34": 34, "cabin_24": 24, "cabin_22": 22, "cabin_22_double": 22,
}


def layout_plan(label: str, code: str, seats: int, presets: dict) -> tuple[dict, str]:
    """(plan, origin) for one layout: a backend preset when the capacity
    matches exactly (with WC/double hints), else a locally generated plan
    of the same family. Limousine-seat plans carry the `vip` class."""
    s = strip_tones((label or "").lower())
    says = lambda *words: any(w in s for w in words)
    # family classification (mirror of the backend's infer_id):
    #   cabin  - explicit phong/cabin in the label
    #   berth  - sleeper code, or giuong/bed/sleeper words (incl. cabins)
    #   seated - everything else (limousine seats / coach / car)
    cabinish = says("phong", "cabin")
    berthy = cabinish or says("giuong", "bed", "sleeper") \
        or (code in ("sleeper", "semi_sleeper")
            and not re.match(r"^xe\s*\d+\s*cho", s))
    wc = says("wc", "ve sinh")
    double = says("doi", "double")
    # "Xe N chỗ" with no berth vocabulary is a seated coach whatever the
    # heuristic code says — keep the public filter honest too.
    if re.match(r"^xe\s*\d+\s*cho", s) and not (cabinish or berthy):
        code = "standard"

    # exact preset matches
    cands = [pid for pid, cap in PRESET_CAPACITY.items() if cap == seats and pid in presets]
    if cabinish:
        cands = [p for p in cands if p.startswith("cabin") or p == "limo_bed_34"]
    elif berthy:
        cands = [p for p in cands if p.startswith("sleeper") or p == "limo_bed_34"]
    elif code == "limousine":
        cands = [p for p in cands if p.startswith("limo") and p not in ("limo_bed_34",)]
    elif code == "minivan":
        cands = [p for p in cands if p in ("minibus_15", "car_7")]
    else:
        cands = [p for p in cands if p.startswith("seat") or p.startswith("car")]

    # variant hints
    def fits(pid: str) -> bool:
        if "wc" in pid and pid != "cabin_22" and not wc:
            return False
        if pid == "cabin_22" and wc:
            return False
        if "double" in pid and not double:
            return False
        if pid == "sleeper_38_wc" and not wc:
            return False
        return True

    cands = [p for p in cands if fits(p)]
    if len(cands) == 1:
        pid = cands[0]
        plan = json.loads(json.dumps(presets[pid]["plan"]))
        # limousine seated presets get the vip class (v1 semantics)
        if pid.startswith("limo_") and pid != "limo_bed_34":
            for d in plan["decks"]:
                for c in d["cells"]:
                    if c["kind"] == "seat":
                        c["seatClass"] = "vip"
        return plan, f"preset:{pid}"

    # generated plans
    if cabinish:
        plan = cabin_plan(seats, double_rear=double, wc=wc)
    elif berthy:
        plan = berth_plan(seats, wc=wc)
    elif code == "limousine":
        plan = limo_seat_plan(seats)
    elif code in ("minivan",):
        plan = car_plan(seats) if seats <= 8 else coach_plan(seats)
    else:
        plan = coach_plan(seats)
    assert plan_sellable(plan) == seats, f"plan capacity {plan_sellable(plan)} != {seats} ({label!r})"
    assert plan_labels_unique(plan), f"duplicate labels for {label!r}"
    return plan, "generated"


# ── Seat-group fare extraction from the raw crawl ────────────────────
def extract_class_fares(crawl_root: Path) -> dict:
    """(operator, from-slug, to-slug, dep-hh:mm, vehicle-label) →
    {canonical_class: min observed group price}."""
    # area id (vexere) → city slug, mirroring nxn_crawl AREAS
    AREA_TO_SLUG = {
        24: "ha-noi", 29: "ho-chi-minh", 15: "da-nang", 27: "hai-phong",
        705: "hue", 457: "lam-dong", 482: "lao-cai", 417: "khanh-hoa",
        278: "ha-giang",
    }

    key = (lambda op, f, t, dep, vt:
           (op.strip().lower(), AREA_TO_SLUG.get(int(f)), AREA_TO_SLUG.get(int(t)),
            dep, one_space(vt)))
    fares = defaultdict(lambda: defaultdict(list))
    n = 0
    for jf in sorted(crawl_root.glob("*/trips.jsonl")):
        from_id = int(jf.parent.name.split("_")[0])
        to_id = int(jf.parent.name.split("_")[1])
        for line in open(jf, encoding="utf-8"):
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            op = (t.get("company") or {}).get("name") or ""
            groups = ((t.get("available_seat_info") or {}).get("seat_group")) or {}
            if not groups:
                continue
            for s in t.get("route", {}).get("schedules") or []:
                dep = f"{int(s.get('hour', 0)):02d}:{int(s.get('minute', 0)):02d}"
                vt = s.get("vehicle_type") or ""
                k = key(op, from_id, to_id, dep, vt)
                for gname, info in groups.items():
                    cls = canon_class(gname)
                    if not cls:
                        continue
                    for price in (info.get("fare") or []):
                        if price and int(price) > 0:
                            fares[k][cls].append(int(price))
    out = {}
    for k, classes in fares.items():
        out[k] = {cls: min(prices) for cls, prices in classes.items()}
    print(f"[class-fares] scanned {n} trips → {len(out)} schedule keys with fares")
    return out


# ── Main ─────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="/home/z/my-project/download/booking_seed_new.sqlite")
    ap.add_argument("--research", default=str(REPO / "data/brand-research/final_brands.json"))
    ap.add_argument("--media", default=str(REPO / "data/vehicle-type-media/vehicle_type_media.json"))
    ap.add_argument("--crawl-root", default=Path("/tmp/nxn_out"))
    ap.add_argument("--presets", default="/home/z/my-project/scripts/preset_cache.json")
    ap.add_argument("--out", default=str(HERE / "migration_plan_v2.json"))
    args = ap.parse_args()

    db = sqlite3.connect(args.seed)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    research = {one_space(b["name"]): b
                for b in json.load(open(args.research, encoding="utf-8"))}
    media = json.load(open(args.media, encoding="utf-8"))
    media_comps = media["companies"]
    media_root = Path(args.media).parent
    presets = json.load(open(args.presets, encoding="utf-8"))

    class_fares = extract_class_fares(Path(args.crawl_root))

    stats = defaultdict(int)
    now = now_iso()

    # ── Brands ─────────────────────────────────────────────────────
    brands = []
    brand_by_seed_id = {}
    for r in cur.execute("SELECT * FROM brand ORDER BY name"):
        name = one_space(r["name"])
        res = research.get(name) or {}
        phone = extract_phone(res.get("own_phones"), res.get("web_phones"))
        logo = (r["logo_url"] or "").strip() or None
        b = {
            "seed_id": r["id"],
            "slug": r["slug"].strip(),
            "name": name,
            "logo_url": logo,
            "description": build_description(name, res),
            "contact_phone": phone,
            "contact_email": None,
            "rating": (round(float(res["rating"]), 1)
                       if res.get("rating") is not None else None),
            "status": "active",
            "accent_color": None,
            "total_trips": r["total_trips"],
        }
        brands.append(b)
        brand_by_seed_id[r["id"]] = b
        if phone:
            stats["brand_phone"] += 1
        if b["rating"]:
            stats["brand_rating"] += 1

    # ── Bus layouts (+ plans) ──────────────────────────────────────
    layouts = []
    layout_by_seed_id = {}
    for r in cur.execute("SELECT * FROM bus_layout"):
        seats = r["total_seats"] or 0
        if seats <= 0:
            stats["layout_skipped_zero_seats"] += 1
            continue
        label = one_space(r["vehicle_type"]) or one_space(r["name"]) or ""
        code = map_vehicle_code(r["vehicle_type"] or "", seats)
        lid = str(uuid.uuid4())
        brand = brand_by_seed_id.get(r["brand_id"])
        try:
            plan, origin = layout_plan(label, code, seats, presets)
        except AssertionError as e:
            stats["layout_plan_failed"] += 1
            print(f"  !! plan failed: {e}")
            continue
        lay = {
            "id": lid,
            "seed_id": r["id"],
            "brand_seed_id": r["brand_id"],
            "brand_slug": brand["slug"] if brand else None,
            "name": label or code,
            "vehicle_code": code,
            "total_seats": seats,
            "plan": plan,
            "plan_origin": origin,
        }
        layouts.append(lay)
        layout_by_seed_id[r["id"]] = lay
        stats[f"layout_{origin.split(':')[0]}"] += 1

    # ── Addresses ──────────────────────────────────────────────────
    addresses = []
    addr_by_seed_id = {}
    for r in cur.execute("SELECT * FROM address ORDER BY brand_id, name"):
        name = one_space(r["name"])
        province = restore_province(r["province"]) or restore_district(r["district"])
        ward = restore_ward(r["ward"])
        district = restore_district(r["district"])
        admin = " / ".join(x for x in (ward or district, province) if x)
        a = {
            "seed_id": r["id"],
            "brand_seed_id": r["brand_id"],
            "name": name,
            "address": admin or None,
            "lat": r["lat"],
            "lon": r["lon"],
            "province": province,
            "district": district,
            "ward": ward,
        }
        addresses.append(a)
        addr_by_seed_id[r["id"]] = a
        if r["lat"] == 0 and r["lon"] == 0:
            stats["addr_zero_coords"] += 1

    # ── Routes ─────────────────────────────────────────────────────
    routes = []
    route_by_seed_id = {}
    for r in cur.execute(
            "SELECT * FROM route ORDER BY brand_id, start_location_id, end_location_id"):
        start, end = r["start_location_id"], r["end_location_id"]
        assert start in VALID_CITY_SLUGS and end in VALID_CITY_SLUGS, (start, end)
        route = {
            "seed_id": r["id"],
            "brand_seed_id": r["brand_id"],
            "name": f"{CITY_NAMES[start]} → {CITY_NAMES[end]}",
            "start_location_id": start,
            "end_location_id": end,
            "status": "active",
        }
        routes.append(route)
        route_by_seed_id[r["id"]] = route

    # ── Schedules (+ points + class fares) ─────────────────────────
    # route seed id → (from-slug, to-slug) for the class-fare join
    pair_by_route = {rid: (r["start_location_id"], r["end_location_id"])
                     for rid, r in route_by_seed_id.items()}
    brand_name_by_seed = {b["seed_id"]: b["name"] for b in brands}

    points_by_sched = defaultdict(list)
    for r in cur.execute(
            "SELECT * FROM schedule_point ORDER BY schedule_id, stop_order"):
        points_by_sched[r["schedule_id"]].append(r)

    schedules = []
    for r in cur.execute("SELECT * FROM schedule"):
        dep = (r["departure_time"] or "").strip()
        if not hhmm_valid(dep):
            m = re.match(r"^(\d{1,2}):(\d{1,2})", dep)
            dep = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" if m else None
        lay = layout_by_seed_id.get(r["bus_layout_id"])
        pts = []
        for p in points_by_sched.get(r["id"], []):
            hhmm = offset_to_hhmm(dep or "00:00", p["arrival_time"] or "")
            pts.append({
                "address_seed_id": p["address_id"],
                "stop_order": p["stop_order"],
                "kind": p["kind"],
                "arrival_time": hhmm,
            })

        # class-fare join: (operator, from, to, dep, vehicle label)
        cfs = []
        if lay:
            pair = pair_by_route.get(r["route_id"])
            op = brand_name_by_seed.get(
                route_by_seed_id[r["route_id"]]["brand_seed_id"]) if r["route_id"] in route_by_seed_id else None
            if pair and op:
                k = (op.strip().lower(), pair[0], pair[1], dep, one_space(lay["name"]))
                for cls, price in (class_fares.get(k) or {}).items():
                    if cls and price and price > 0:
                        cfs.append({"seat_class": cls, "price_adult": int(price)})
                # class fares never undercut the schedule's base price by
                # accident of promo floors: keep them >= base only when the
                # base came from the same class family (leave raw otherwise)
            if cfs:
                stats["sched_with_class_fares"] += 1
            stats["class_fare_rows"] += len(cfs)

        s = {
            "seed_id": r["id"],
            "route_seed_id": r["route_id"],
            "layout_id": lay["id"] if lay else None,
            "vehicle_code": lay["vehicle_code"] if lay else None,
            "departure_time": dep,
            "days_of_week": None,       # recurring daily (v1 policy)
            "effective_from": None,
            "effective_to": None,
            "base_price_adult": r["base_price_adult"],
            "base_price_child": None,
            "amenities": None,
            "points": pts,
            "class_fares": cfs,
        }
        schedules.append(s)
        if pts:
            stats["sched_with_points"] += 1
        if dep is None:
            stats["sched_bad_time"] += 1
        stats["points_total"] += len(pts)

    # ── Route-level pickup points ──────────────────────────────────
    pickups = []
    for route in routes:
        seen = {}
        for s in schedules:
            if s["route_seed_id"] != route["seed_id"]:
                continue
            for p in s["points"]:
                key = p["address_seed_id"]
                if key not in seen:
                    seen[key] = {"address_seed_id": key,
                                 "order": p["stop_order"],
                                 "kind": p["kind"]}
        for key, info in sorted(seen.items(), key=lambda kv: kv[1]["order"]):
            name = addr_by_seed_id[key]["name"]
            kind = "station"
            low = name.casefold()
            if any(w in low for w in ("dọc", "quốc lộ", " ql", "cây số", "cổng số")):
                kind = "curb"
            pickups.append({
                "route_seed_id": route["seed_id"],
                "address_seed_id": key,
                "stop_order": 0,
                "kind": kind,
                "name": name,
                "address": addr_by_seed_id[key]["address"],
                "lat": addr_by_seed_id[key]["lat"],
                "lon": addr_by_seed_id[key]["lon"],
            })
    per_route = defaultdict(list)
    for p in pickups:
        per_route[p["route_seed_id"]].append(p)
    for rid, plist in per_route.items():
        for i, p in enumerate(plist, start=1):
            p["stop_order"] = i

    # ── Route pictures ─────────────────────────────────────────────
    media_by_slug = {}
    for cid, c in media_comps.items():
        media_by_slug[c["brand_slug"]] = {
            "cover": c.get("cover_url"),
            "cover_file": None,
            "vts": {norm_label(vt["label"]): vt for vt in c["vehicle_types"]},
        }
    covers_dir = media_root / "covers"
    for slug, m in media_by_slug.items():
        if covers_dir.exists():
            for ext in (".jpeg", ".jpg", ".png", ".webp"):
                f = covers_dir / f"{slug}{ext}"
                if f.exists():
                    m["cover_file"] = str(f)
                    break

    pictures = []
    for route in routes:
        brand = brand_by_seed_id[route["brand_seed_id"]]
        m = media_by_slug.get(brand["slug"])
        if not m:
            continue
        files = []
        if m["cover_file"]:
            files.append((m["cover_file"], f"Ảnh đại diện nhà xe {brand['name']}"))
        labels = {}
        for s in schedules:
            if s["route_seed_id"] == route["seed_id"] and s["vehicle_code"]:
                lay = next((l for l in layouts if l["id"] == s["layout_id"]), None)
                if lay:
                    labels[lay["name"]] = True
        picked = 0
        for label in labels:
            vt = m["vts"].get(norm_label(label))
            if not vt:
                continue
            for img in vt["images"]:
                f = media_root / img["file"]
                if f.exists():
                    files.append((str(f), (img.get("caption_vi")
                                           or img.get("caption_en") or label)[:255]))
                    picked += 1
        if picked == 0:
            for label, vt in m["vts"].items():
                for img in vt["images"]:
                    f = media_root / img["file"]
                    if f.exists():
                        files.append((str(f), (img.get("caption_vi")
                                               or img.get("caption_en") or label)[:255]))
        seenf, uniq = set(), []
        for f, alt in files:
            if f not in seenf:
                seenf.add(f)
                uniq.append({"route_seed_id": route["seed_id"],
                             "file": f, "alt": alt})
        for u in uniq[:12]:
            pictures.append(u)
        stats["routes_with_pictures"] += 1 if uniq else 0

    plan = {
        "generated_at": now,
        "counts": {
            "brands": len(brands), "layouts": len(layouts),
            "addresses": len(addresses), "routes": len(routes),
            "schedules": len(schedules),
            "schedule_points": stats["points_total"],
            "pickup_points": len(pickups),
            "picture_uploads": len(pictures),
            "class_fare_rows": stats["class_fare_rows"],
        },
        "stats": dict(stats),
        "brands": brands, "layouts": layouts, "addresses": addresses,
        "routes": routes, "schedules": schedules, "pickup_points": pickups,
        "pictures": pictures,
    }
    Path(args.out).write_text(json.dumps(plan, ensure_ascii=False),
                              encoding="utf-8")
    print(json.dumps(plan["counts"], indent=2, ensure_ascii=False))
    print("stats:", json.dumps(dict(stats), ensure_ascii=False))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
