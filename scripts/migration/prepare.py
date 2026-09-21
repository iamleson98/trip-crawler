# -*- coding: utf-8 -*-
"""prepare.py — Build a clean, fully-transformed migration plan JSON from:

  * data/booking_seed.sqlite      (deduped brands/routes/schedules/points/
                                   addresses/bus_layouts — booking-app schema)
  * data/brand-research/final_brands.json   (ratings / phones / addresses)
  * data/vehicle-type-media/vehicle_type_media.json  (picture files)

Transformations applied (the "clean formatting" pass):
  brands     — names collapsed to single spaces; slug kept (already unique);
               logo from vexere CDN; rating/phone/address from brand research;
               Vietnamese description composed from research facts.
  addresses  — province/ward/district diacritics restored; `address` composed
               as "<ward|district>, <province>"; names collapsed to 1 space.
  routes     — name "<City A> → <City B>" from the app's own city table.
  schedules  — arrival_time "T±Nmin" → absolute "HH:MM" (departure + offset,
               mod 24h); days_of_week ISO list → 7-char Mon-first bitmask;
               recurring semantics: no effective window, daily (the crawl
               window was only 3 days — the mask is a crawl artifact);
               vehicle_type mapped from the bus-layout label; price exact.
  bus_layouts— kept with exact total_seats; seat rows generated for the
               seat map (2-col berths for sleeper classes, 4-col for seated).
  pictures   — per route: brand cover first, then the images of the vehicle
               types that serve that route (label-matched), capped at 12.

Usage:
    python3 prepare.py [--seed PATH] [--research PATH] [--media PATH]
                        [--out PATH] [--cities-from rust-be-template]

Output: migration_plan.json (everything the API pusher + DB writer need).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vi import (DISTRICT_VI, PROVINCE_VI, WARD_VI, one_space, restore_district,
                restore_province, restore_ward)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# City slug → display name (mirrors rust-be-template src/cities.rs).
CITY_NAMES = {
    "ha-noi": "Hà Nội", "ho-chi-minh": "TP. Hồ Chí Minh", "hai-phong": "Hải Phòng",
    "da-nang": "Đà Nẵng", "can-tho": "Cần Thơ", "ha-giang": "Hà Giang",
    "cao-bang": "Cao Bằng", "bac-kan": "Bắc Kạn", "tuyen-quang": "Tuyên Quang",
    "lao-cai": "Lào Cai", "dien-bien": "Điện Biên", "lai-chau": "Lai Châu",
    "son-la": "Sơn La", "yen-bai": "Yên Bái", "hoa-binh": "Hòa Bình",
    "thai-nguyen": "Thái Nguyên", "lang-son": "Lạng Sơn", "quang-ninh": "Quảng Ninh",
    "bac-giang": "Bắc Giang", "vinh-phuc": "Vĩnh Phúc", "bac-ninh": "Bắc Ninh",
    "phu-tho": "Phú Thọ", "ha-nam": "Hà Nam", "thai-binh": "Thái Bình",
    "nam-dinh": "Nam Định", "ninh-binh": "Ninh Bình", "ninh-thuan": "Ninh Thuận",
    "thanh-hoa": "Thanh Hóa", "nghe-an": "Nghệ An", "ha-tinh": "Hà Tĩnh",
    "quang-binh": "Quảng Bình", "quang-tri": "Quảng Trị", "hue": "Huế",
    "quang-nam": "Quảng Nam", "quang-ngai": "Quảng Ngãi", "binh-dinh": "Bình Định",
    "phu-yen": "Phú Yên", "khanh-hoa": "Khánh Hòa", "binh-thuan": "Bình Thuận",
    "kon-tum": "Kon Tum", "gia-lai": "Gia Lai", "dak-lak": "Đắk Lắk",
    "dak-nong": "Đắk Nông", "lam-dong": "Lâm Đồng", "ba-ria-vung-tau": "Bà Rịa - Vũng Tàu",
    "binh-duong": "Bình Dương", "binh-phuoc": "Bình Phước", "dong-nai": "Đồng Nai",
    "tay-ninh": "Tây Ninh", "long-an": "Long An", "tien-giang": "Tiền Giang",
    "ben-tre": "Bến Tre", "tra-vinh": "Trà Vinh", "vinh-long": "Vĩnh Long",
    "dong-thap": "Đồng Tháp", "an-giang": "An Giang", "kien-giang": "Kiên Giang",
    "hau-giang": "Hậu Giang", "soc-trang": "Sóc Trăng", "bac-lieu": "Bạc Liêu",
    "ca-mau": "Cà Mau",
}

VALID_CITY_SLUGS = set(CITY_NAMES)

# ── Vehicle-class mapping (bus_layout label → catalog code) ─────────
BED_WORDS = ("giường", "phòng", "cabin", "khoang", "bed")


def map_vehicle_code(label: str, total_seats: int | None) -> str:
    """Map a VeXeRe vehicle label to one of the 5 catalog codes.

    limousine    — seated VIP van (Limousine 9-19 chỗ)
    sleeper      — berth vehicle (giường nằm / cabin / phòng)
    semi_sleeper — single-berth vehicle (giường nằm đơn / phòng đơn)
    standard     — seated coach (ghế ngồi)
    minivan      — small car (Xe 7 chỗ, Fortuner, Vinfast …)
    """
    s = one_space(label).casefold()
    seats = total_seats or 0
    if "limo" in s:
        if any(w in s for w in BED_WORDS):
            return "semi_sleeper" if "đơn" in s else "sleeper"
        return "limousine"
    if any(w in s for w in BED_WORDS):
        return "semi_sleeper" if "đơn" in s else "sleeper"
    if "ghế ngồi" in s or "ghe ngoi" in s:
        return "standard"
    if "7 chỗ" in s or "7 cho" in s or "fortuner" in s or "vinfast" in s \
            or "xc 7" in s or (0 < seats <= 8):
        return "minivan"
    if seats >= 30:
        return "sleeper"
    if seats >= 17:
        return "standard"
    return "minivan"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_label(s: str) -> str:
    return one_space(s).casefold()


# ── Schedule helpers ────────────────────────────────────────────────
def iso_days_to_mask(spec: str) -> str:
    """'2,3,4' (ISO Mon=1..Sun=7) → 7-char Mon-first bitmask '0111000'.

    NOTE: the observed days only cover the 3-day crawl window, so the
    migration pushes schedules as DAILY recurring services (mask=None);
    this helper is kept for reference/verification use.
    """
    days = {int(x) for x in spec.split(",") if x.strip()}
    return "".join("1" if (i + 1) in days else "0" for i in range(7))


def offset_to_hhmm(dep_hhmm: str, t_label: str) -> str | None:
    """'T+540min' relative to departure → absolute 'HH:MM' (mod 24h)."""
    m = re.match(r"^T([+-]?\d+)(?:min)?$", (t_label or "").strip())
    if not m:
        return None
    mins = int(m.group(1))
    h, mi = dep_hhmm.split(":")
    total = (int(h) * 60 + int(mi) + mins) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def hhmm_valid(t: str) -> bool:
    return bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", (t or "").strip()))


# ── Phone extraction ────────────────────────────────────────────────
def extract_phone(*cands) -> str | None:
    """First valid VN number (0xxxxxxxxx, 9-20 digits) from research strings."""
    for cand in cands:
        if not cand:
            continue
        for raw in re.split(r"[;,/]| và | hoac ", str(cand)):
            digits = re.sub(r"[^\d+]", "", raw)
            digits = digits.lstrip("+")
            if digits.startswith("0") and 9 <= len(digits) <= 11 \
                    and digits.isdigit():
                return digits
    return None


def build_description(name: str, r: dict) -> str:
    parts = []
    n_routes = r.get("routes_n")
    if n_routes:
        parts.append(f"Nhà xe {name} hiện khai thác {n_routes} tuyến đường"
                     f" tại các thành phố lớn.")
    mp = r.get("min_price")
    xp = r.get("max_price")
    if mp and xp and mp < xp:
        parts.append(f"Giá vé từ {mp:,}đ đến {xp:,}đ.".replace(",", "."))
    rating = r.get("rating")
    rc = r.get("rating_count")
    if rating:
        txt = f"Đánh giá {rating}/5"
        if rc:
            txt += f" ({rc:,} lượt)".replace(",", ".")
        parts.append(txt + ".")
    addr = r.get("address")
    if addr:
        parts.append(f"Văn phòng: {one_space(addr)}.")
    return " ".join(parts)[:4999] or None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=str(REPO / "data/booking_seed.sqlite"))
    ap.add_argument("--research", default=str(REPO / "data/brand-research/final_brands.json"))
    ap.add_argument("--media", default=str(REPO / "data/vehicle-type-media/vehicle_type_media.json"))
    ap.add_argument("--out", default=str(HERE / "migration_plan.json"))
    args = ap.parse_args()

    db = sqlite3.connect(args.seed)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    research = {one_space(b["name"]): b
                for b in json.load(open(args.research, encoding="utf-8"))}
    media = json.load(open(args.media, encoding="utf-8"))
    media_comps = media["companies"]
    media_root = (Path(args.media)).parent  # …/vehicle-type-media/

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
        if b["description"]:
            stats["brand_desc"] += 1

    # ── Bus layouts (+ generated seat maps) ─────────────────────────
    layouts = []
    layout_by_seed_id = {}
    for r in cur.execute("SELECT * FROM bus_layout"):
        seats = r["total_seats"] or 0
        code = map_vehicle_code(r["vehicle_type"] or "", seats)
        lid = str(uuid.uuid4())
        brand = brand_by_seed_id.get(r["brand_id"])
        lay = {
            "id": lid,
            "seed_id": r["id"],
            "brand_seed_id": r["brand_id"],
            "brand_slug": brand["slug"] if brand else None,
            "name": one_space(r["vehicle_type"]) or one_space(r["name"]) or code,
            "vehicle_code": code,
            "total_seats": seats,
            "seat_rows": gen_seat_rows(code, seats),
        }
        layouts.append(lay)
        layout_by_seed_id[r["id"]] = lay

    # ── Addresses ──────────────────────────────────────────────────
    addresses = []
    addr_by_seed_id = {}
    for r in cur.execute("SELECT * FROM address ORDER BY brand_id, name"):
        name = one_space(r["name"])
        province = restore_province(r["province"]) or restore_district(r["district"])
        ward = restore_ward(r["ward"])
        district = restore_district(r["district"])
        # Human-readable admin line: prefer ward, then district.
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
        if province:
            stats["addr_province_ok"] += 1

    # ── Routes ─────────────────────────────────────────────────────
    routes = []
    route_by_seed_id = {}
    for r in cur.execute("SELECT * FROM route ORDER BY brand_id, start_location_id, end_location_id"):
        start = r["start_location_id"]
        end = r["end_location_id"]
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

    # ── Schedules (+ points) ────────────────────────────────────────
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
        s = {
            "seed_id": r["id"],
            "route_seed_id": r["route_id"],
            "layout_id": lay["id"] if lay else None,
            "vehicle_code": lay["vehicle_code"] if lay else None,
            "departure_time": dep,
            "days_of_week": None,       # recurring daily — see module docstring
            "effective_from": None,
            "effective_to": None,
            "base_price_adult": r["base_price_adult"],
            "base_price_child": r["base_price_child"],
            "amenities": None,
            "points": pts,
        }
        schedules.append(s)
        if pts:
            stats["sched_with_points"] += 1
        if dep is None:
            stats["sched_bad_time"] += 1
        stats["points_total"] += len(pts)

    # ── Route-level pickup points (booking flow needs them) ─────────
    # Union of every address a route's schedules stop at, ordered by the
    # most common stop_order position, kind derived from vexere segment.
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
                "stop_order": len(pickups) % 1000 or 1,
                "kind": kind,
                "name": name,
                "address": addr_by_seed_id[key]["address"],
                "lat": addr_by_seed_id[key]["lat"],
                "lon": addr_by_seed_id[key]["lon"],
            })
    # renumber stop_order within each route
    per_route = defaultdict(list)
    for p in pickups:
        per_route[p["route_seed_id"]].append(p)
    for rid, plist in per_route.items():
        for i, p in enumerate(plist, start=1):
            p["stop_order"] = i

    # ── Route pictures ──────────────────────────────────────────────
    # Media lookup: brand_slug → {normalized label → [image files]}
    media_by_slug = {}
    for cid, c in media_comps.items():
        media_by_slug[c["brand_slug"]] = {
            "cover": c.get("cover_url"),
            "cover_file": None,
            "vts": {norm_label(vt["label"]): vt for vt in c["vehicle_types"]},
        }
    # Locate cover files on disk (covers/<slug>.<ext>)
    covers_dir = media_root / "covers"
    for slug, m in media_by_slug.items():
        if covers_dir.exists():
            for ext in (".jpeg", ".jpg", ".png", ".webp"):
                f = covers_dir / f"{slug}{ext}"
                if f.exists():
                    m["cover_file"] = str(f)
                    break

    pictures = []  # [{route_seed_id, file, alt}]
    for route in routes:
        brand = brand_by_seed_id[route["brand_seed_id"]]
        m = media_by_slug.get(brand["slug"])
        if not m:
            continue
        files = []
        if m["cover_file"]:
            files.append((m["cover_file"],
                          f"Ảnh đại diện nhà xe {brand['name']}"))
        # vehicle labels that serve this route (via its schedules' layouts)
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
        # No vehicle-label match → use every image of the brand.
        if picked == 0:
            for label, vt in m["vts"].items():
                for img in vt["images"]:
                    f = media_root / img["file"]
                    if f.exists():
                        files.append((str(f), (img.get("caption_vi")
                                               or img.get("caption_en") or label)[:255]))
        # dedupe by path, cap at 12 (MAX_PICTURES_PER_ROUTE)
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
            "seats": sum(len(l["seat_rows"]) for l in layouts),
            "addresses": len(addresses), "routes": len(routes),
            "schedules": len(schedules),
            "schedule_points": stats["points_total"],
            "pickup_points": len(pickups),
            "picture_uploads": len(pictures),
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


def gen_seat_rows(code: str, total_seats: int):
    """Generate a seat map for a layout.

    sleeper/semi_sleeper — berths: column A (bed_lower, window) + column B
    (bed_upper); labels A01…, B01…  limousine seats get class 'vip'.
    seated classes — 4 columns A–D (A/D window); labels A1…
    """
    rows = []
    n = max(int(total_seats or 0), 0)
    if n == 0:
        return rows
    if code in ("sleeper", "semi_sleeper"):
        n_rows = (n + 1) // 2
        for r in range(1, n_rows + 1):
            for col, (letter, cls, window) in enumerate(
                    (("A", "bed_lower", 1), ("B", "bed_upper", 0)), start=1):
                if len(rows) >= n:
                    break
                rows.append({
                    "label": f"{letter}{r:02d}", "class": cls,
                    "row_num": r, "col_num": col, "is_window": bool(window),
                    "floor": 1,
                })
    else:
        cls = "vip" if code == "limousine" else "standard"
        cols = (("A", 1, 1), ("B", 2, 0), ("C", 3, 0), ("D", 4, 1))
        r = 1
        i = 0
        while len(rows) < n:
            for letter, col, window in cols:
                if len(rows) >= n:
                    break
                rows.append({
                    "label": f"{letter}{r}", "class": cls,
                    "row_num": r, "col_num": col, "is_window": bool(window),
                    "floor": 1,
                })
                i += 1
            r += 1
    return rows


if __name__ == "__main__":
    main()
