#!/usr/bin/env python3
"""Build the SCHEDULES + SEAT PRICING workbook from the N×N crawl output.

User requirement (2026-10-10): "I don't want trip data for next days, I just
want schedule information, pricing for seats, that's it."

This workbook is therefore a SCHEDULE-LEVEL catalog — no per-date trip
instances. The 38,818 dated trips crawled for 2026-10-10 → 2026-10-12 are
collapsed (deduplicated) into 15.7k unique recurring schedules; the 3-day
window is used ONLY as evidence of how often a schedule runs ("3/3 days =
daily") and to capture the observed selling-price range per seat class.

Reads  /home/z/my-project/nxn_out/manifest.json + each pair's trips.jsonl
Writes /home/z/my-project/download/vexere_nxn_trips_enriched.xlsx
  Sheet 1  Summary            — KPIs, top routes, seat-class overview, notes
  Sheet 2  Schedules          — master catalog (1 row per unique schedule x seat class)
  Sheet 3  Routes             — per city pair
  Sheet 4  Seat Class Pricing — per (city pair x seat family)
  Sheet 5  Operators          — per operator
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

XLSX_SKILL_DIR = "/home/z/my-project/skills/xlsx"
for sub in (XLSX_SKILL_DIR, XLSX_SKILL_DIR + "/templates"):
    if sub not in sys.path:
        sys.path.insert(0, sub)

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
import base as B  # xlsx skill design tokens & style helpers

NXN = Path("/home/z/my-project/nxn_out")
OUT = Path("/home/z/my-project/download/vexere_nxn_trips_enriched.xlsx")
CRAWL_DATES = ["2026-10-10", "2026-10-11", "2026-10-12"]
CRAWLED_ON = "2026-10-10"

# ---------------------------------------------------------------- helpers --
FAM_SLEEPER = "Giường nằm (sleeper)"
FAM_LIMO = "Limousine"
FAM_CABIN = "Cabin"
FAM_SEATER = "Ghế ngồi (seater)"
FAM_SMALL = "Xe nhỏ ≤ 9 chỗ (car)"
FAM_MINIBUS = "Minibus 10–20 chỗ"
FAM_BIG = "Xe khách lớn 29+ chỗ"
FAM_SHARED = "Xe ghép (shared)"
FAM_OTHER = "Khác"

_CAR_BRANDS = ("fortuner", "fortune", "vinfast", "vf8", "kia", "carnival",
               "custin", "sedona", "hyundai", "dcar", "toyota", "gaz")


def nfc(s):
    """NFC-normalize + strip; collapse inner whitespace. None-safe."""
    if not isinstance(s, str):
        return s
    s = unicodedata.normalize("NFC", s).strip()
    return re.sub(r"\s+", " ", s)


def classify_family(vt_raw: str):
    v = nfc(vt_raw or "").lower()
    if not v:
        return FAM_OTHER
    n = None
    m = (re.search(r"(\d+)\s*(?:chỗ|ghế|cho)\b", v)
         or re.search(r"^sơ đồ\s*(\d+)", v))
    if m:
        n = int(m.group(1))
    if "giường nằm" in v or "giuong nam" in v:
        return FAM_SLEEPER
    if "cabin" in v:
        return FAM_CABIN
    if "limousine" in v:
        return FAM_LIMO
    if "ghế ngồi" in v or "ghe ngoi" in v:
        return FAM_SEATER
    if "ghép" in v or "ghep" in v:
        return FAM_SHARED
    if n is not None and n <= 9:
        return FAM_SMALL
    if any(b in v for b in _CAR_BRANDS) or "limo green" in v:
        return FAM_SMALL if (n is None or n <= 9) else FAM_MINIBUS
    if n is not None and n <= 20:
        return FAM_MINIBUS
    if n is not None:
        return FAM_BIG
    if "phòng" in v or "phong" in v:
        return FAM_CABIN
    if "limo" in v:
        return FAM_LIMO
    return FAM_OTHER


def hhmm_to_min(t: str):
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def fmt_dur(minutes):
    if minutes is None or minutes <= 0:
        return ""
    return f"{minutes // 60}h{minutes % 60:02d}"


def fmt_arrival(dep_time: str, minutes):
    dm = hhmm_to_min(dep_time)
    if dm is None or not minutes or minutes <= 0:
        return ""
    total = dm + minutes
    days, clock = divmod(total, 1440)
    suffix = f" (+{days}d)" if days else ""
    return f"{clock // 60:02d}:{clock % 60:02d}{suffix}"


def infer_city(end: dict, cmap: dict):
    name = nfc(end.get("city_name") or "")
    if name:
        return name
    cid = end.get("city_id")
    if cid in cmap:
        return cmap[cid]
    pn = nfc(end.get("name") or "")
    m = re.match(r"^TP\.?\s+(.+?)(?:\s*\(|$)", pn)
    if m:
        return m.group(1).strip()
    # last resort: a known city name embedded in the point name
    # (e.g. 'Vincom Đà Nẵng', 'BXTT Đà Nẵng') — longest names first
    for cn in sorted(set(cmap.values()), key=len, reverse=True):
        if cn and cn in pn:
            return cn
    return "?"


# ------------------------------------------------------------- data load --
def load_catalog():
    manifest = json.load(open(NXN / "manifest.json"))
    # pass 1: city_id -> city_name (majority vote)
    city_votes = {}
    files = []
    for p in manifest:
        f = Path(p["pair_dir"]) / "trips.jsonl"
        if f.exists():
            files.append(f)
    for f in files:
        for line in open(f, encoding="utf-8"):
            rec = json.loads(line)
            for end in (rec["route"]["from"], rec["route"]["to"]):
                cid, cn = end.get("city_id"), nfc(end.get("city_name") or "")
                if cid is not None and cn:
                    city_votes.setdefault(cid, Counter())[cn] += 1
    cmap = {cid: c.most_common(1)[0][0] for cid, c in city_votes.items()}

    catalog = {}
    n_trips = n_fares = 0
    for f in files:
        for line in open(f, encoding="utf-8"):
            rec = json.loads(line)
            n_trips += 1
            r = rec["route"]
            date = r["departure_date"][:10]
            base = {
                "route_id": r["id"],
                "from_city": infer_city(r["from"], cmap),
                "to_city": infer_city(r["to"], cmap),
                "operator": nfc(rec["company"]["name"]) or "?",
                "route_name": nfc(r.get("name") or ""),
                "dep_point": nfc(r["from"].get("name") or ""),
                "arr_point": nfc(r["to"].get("name") or ""),
                "dep_time": nfc(r.get("departure_time") or ""),
                "duration": r.get("duration"),
                "distance": r.get("distance") or None,
                "rating": (rec["company"].get("ratings") or {}).get("overall"),
                "reviews": (rec["company"].get("ratings") or {}).get("comments"),
            }
            for s in r.get("schedules", []):
                n_fares += 1
                vt = nfc(s.get("vehicle_type") or "")
                tpl = s.get("seat_template_id") or vt
                key = (r["id"], base["dep_time"], r["from"]["id"],
                       r["to"]["id"], tpl)
                v = catalog.get(key)
                if v is None:
                    v = dict(base)
                    v["seat_class"] = vt
                    v["family"] = classify_family(vt)
                    v["seats"] = s.get("total_seats")
                    v["orig"] = []
                    v["sell"] = []
                    v["dates"] = set()
                    catalog[key] = v
                v["dates"].add(date)
                fare = s.get("fare") or {}
                if fare.get("original"):
                    v["orig"].append(fare["original"])
                if fare.get("discount"):
                    v["sell"].append(fare["discount"])
                if s.get("total_seats"):
                    v["seats"] = max(v["seats"] or 0, s["total_seats"])

    rows = []
    for v in catalog.values():
        n_days = len(v["dates"])
        orig = max(v["orig"]) if v["orig"] else None
        sell_min = min(v["sell"]) if v["sell"] else None
        sell_max = max(v["sell"]) if v["sell"] else None
        disc = (round((1 - sell_min / orig) * 100, 1)
                if orig and sell_min and orig >= sell_min else None)
        freq = f"{n_days}/3 {'(daily)' if n_days == 3 else 'days'}"
        rows.append({
            **{k: v[k] for k in
               ("route_id", "from_city", "to_city", "operator", "route_name",
                "dep_point", "arr_point", "dep_time", "duration", "distance",
                "seat_class", "family", "seats")},
            "orig": orig, "sell_min": sell_min, "sell_max": sell_max,
            "disc": disc, "n_days": n_days, "freq": freq,
        })
    rows.sort(key=lambda x: (x["from_city"], x["to_city"],
                             hhmm_to_min(x["dep_time"]) or 0,
                             x["operator"], x["seat_class"]))
    return rows, n_trips, n_fares


# ----------------------------------------------------------- aggregations --
def pair_aggregate(rows):
    agg = {}
    for r in rows:
        k = (r["from_city"], r["to_city"])
        a = agg.setdefault(k, {"ops": set(), "n": 0, "daily": 0, "deps": [],
                               "durs": [], "dists": [], "pmin": None,
                               "pmax": None, "fams": set()})
        _acc(a, r)
    return agg


def pair_family_aggregate(rows):
    agg = {}
    for r in rows:
        k = (r["from_city"], r["to_city"], r["family"])
        a = agg.setdefault(k, {"ops": set(), "n": 0, "daily": 0, "deps": [],
                               "durs": [], "dists": [], "pmin": None,
                               "pmax": None, "fams": set()})
        _acc(a, r)
    return agg


def _acc(a, r):
    a["ops"].add(r["operator"])
    a["n"] += 1
    if r["n_days"] == 3:
        a["daily"] += 1
    a["deps"].append(r["dep_time"])
    if r["duration"]:
        a["durs"].append(r["duration"])
    if r["distance"]:
        a["dists"].append(r["distance"])
    a["fams"].add(r["family"])
    if r["sell_min"]:
        a["pmin"] = min(a["pmin"] if a["pmin"] else 10 ** 12, r["sell_min"])
    if r["sell_max"]:
        a["pmax"] = max(a["pmax"] or 0, r["sell_max"])


def operator_aggregate(rows):
    agg = {}
    for r in rows:
        a = agg.setdefault(r["operator"], {"routes": set(), "n": 0, "daily": 0,
                                           "cities": set(), "pmin": None,
                                           "pmax": None, "rating": None,
                                           "reviews": 0, "route_names": []})
        a["routes"].add(r["route_id"])
        a["route_names"].append(r["route_name"] or "?")
        a["n"] += 1
        if r["n_days"] == 3:
            a["daily"] += 1
        a["cities"].update((r["from_city"], r["to_city"]))
        if r["sell_min"]:
            a["pmin"] = min(a["pmin"] if a["pmin"] else 10 ** 12, r["sell_min"])
        if r["sell_max"]:
            a["pmax"] = max(a["pmax"] or 0, r["sell_max"])
    # rating/reviews come from rows — attach via second pass
    for r in rows:
        a = agg[r["operator"]]
        if a["rating"] is None and r.get("rating") is not None:
            a["rating"] = r["rating"]
        if r.get("reviews"):
            a["reviews"] = max(a["reviews"], r["reviews"])
    for a in agg.values():
        top = Counter(a["route_names"]).most_common(1)[0][0]
        a["top_route"] = top
    return agg


# --------------------------------------------------------------- builders --
INT = B.FORMATS["integer"]
PCT = B.FORMATS["percentage"]
DEC1 = B.FORMATS["decimal_1"]


def _write_table(ws, title, headers, data, fmts, aligns, caption=None,
                 total=None, freeze="C5", autofilter=True, widths=None):
    """Generic styled table at origin B2/B4 per design.md."""
    last_col = len(headers) + 1
    B.setup_sheet(ws, title=title, last_col=last_col)
    if caption:
        ws.cell(row=3, column=2, value=caption).font = B.font_caption()
        ws.row_dimensions[3].height = 14
    for ci, h in enumerate(headers, 2):
        ws.cell(row=4, column=ci, value=h)
    B.style_header_row(ws, 4, 2, last_col)

    def _align_row(rn):
        for ci in range(2, last_col + 1):
            k = aligns[ci - 2]
            if k == "r":
                ws.cell(row=rn, column=ci).alignment = B.align_number()
            elif k == "c":
                ws.cell(row=rn, column=ci).alignment = B.align_date()

    for i, row in enumerate(data):
        rn = 5 + i
        for ci, val in enumerate(row, 2):
            c = ws.cell(row=rn, column=ci, value=val)
            f = fmts[ci - 2]
            if f and isinstance(val, (int, float)) and not isinstance(val, bool):
                c.number_format = f
        B.style_data_row(ws, rn, 2, last_col, i)
        _align_row(rn)
    last_row = 4 + len(data)
    if total is not None:
        trn = last_row + 1
        for ci, val in enumerate(total, 2):
            c = ws.cell(row=trn, column=ci, value=val)
            f = fmts[ci - 2]
            if f and isinstance(val, (int, float)) and not isinstance(val, bool):
                c.number_format = f
        B.style_total_row(ws, trn, 2, last_col)
        for ci in range(2, last_col + 1):
            k = aligns[ci - 2]
            if k == "r":
                ws.cell(row=trn, column=ci).alignment = B.align_number()
            elif k == "c":
                ws.cell(row=trn, column=ci).alignment = B.align_date()
        last_row = trn
    if autofilter and data:
        ws.auto_filter.ref = f"B4:{get_column_letter(last_col)}{4 + len(data)}"
    if freeze:
        ws.freeze_panes = freeze
    if widths:
        for ci, w in enumerate(widths, 2):
            ws.column_dimensions[get_column_letter(ci)].width = w
    else:
        B.auto_fit_columns(ws, min_width=8, max_width=30, header_row=4,
                           data_start_row=5)
    return last_row


def build_schedules(wb, rows):
    headers = ["From City", "To City", "Operator", "Route", "Departure Point",
               "Arrival Point", "Departure", "Arrival", "Duration (min)",
               "Duration", "Distance (km)", "Seat Class", "Seat Family",
               "Seats", "List Price (VND)", "Min Price (VND)",
               "Max Price (VND)", "Discount", "Frequency"]
    fmts = [None] * 8 + [INT, None, INT, None, None, INT, INT, INT, INT,
                         PCT, None]
    aligns = ["t", "t", "t", "t", "t", "t", "c", "c", "r", "c", "r", "t",
              "t", "r", "r", "r", "r", "r", "c"]
    data = []
    for r in rows:
        data.append([
            r["from_city"], r["to_city"], r["operator"], r["route_name"],
            r["dep_point"], r["arr_point"], r["dep_time"],
            fmt_arrival(r["dep_time"], r["duration"]),
            r["duration"], fmt_dur(r["duration"]), r["distance"],
            r["seat_class"], r["family"], r["seats"], r["orig"],
            r["sell_min"], r["sell_max"],
            (r["disc"] / 100) if r["disc"] is not None else None, r["freq"],
        ])
    ws = wb.create_sheet("Schedules")
    _write_table(
        ws, "Bus Schedules & Seat Pricing — Master Catalog", headers, data,
        fmts, aligns,
        caption=("One row per unique recurring schedule × seat class "
                 "(deduplicated from dated trips). Frequency = on how many "
                 "of the 3 verified days (2026-10-10 → 12) the schedule "
                 "operates; 3/3 means daily service."),
        freeze="D5", widths=[15, 15, 22, 20, 28, 28, 11, 12, 11, 9, 11, 24,
                             20, 8, 14, 14, 14, 10, 12])
    # self-check
    assert ws.max_row == 4 + len(data), "Schedules row count mismatch"
    assert ws.cell(row=5, column=2).value == rows[0]["from_city"]
    for rn in (5, ws.max_row):
        for ci in range(2, 9):
            assert ws.cell(row=rn, column=ci).value not in (None, ""), \
                f"empty key cell at row {rn} col {ci}"
    return ws


def build_routes(wb, agg):
    items = sorted(agg.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    headers = ["From City", "To City", "Operators", "Schedules", "Daily (3/3)",
               "First Departure", "Last Departure", "Min Duration (h)",
               "Max Duration (h)", "Distance (km)", "Price Min (VND)",
               "Price Max (VND)", "Seat Families"]
    fmts = [None, None, INT, INT, INT, None, None, DEC1, DEC1, INT, INT, INT,
            None]
    aligns = ["t", "t", "r", "r", "r", "c", "c", "r", "r", "r", "r", "r", "t"]
    data = []
    for (fc, tc), a in items:
        durs = sorted(a["durs"]) or [None, None]
        dist = int(statistics.median(a["dists"])) if a["dists"] else None
        data.append([
            fc, tc, len(a["ops"]), a["n"], a["daily"],
            min(a["deps"]) if a["deps"] else None,
            max(a["deps"]) if a["deps"] else None,
            round(durs[0] / 60, 1) if durs[0] else None,
            round(durs[-1] / 60, 1) if durs[-1] else None,
            dist, a["pmin"], a["pmax"], ", ".join(sorted(a["fams"])),
        ])
    total = ["Total", "", None, sum(a["n"] for a in agg.values()),
             sum(a["daily"] for a in agg.values()), "", "", None, None, None,
             None, None, ""]
    ws = wb.create_sheet("Routes")
    _write_table(
        ws, "Routes — City Pair Overview", headers, data, fmts, aligns,
        caption=("Per city pair: operators, unique schedules, daily (3/3) "
                 "schedules, span of departures, duration and seat-price "
                 "range. Prices are per seat in VND."),
        freeze="D5")
    # self-check
    col_e = [ws.cell(row=rn, column=5).value for rn in range(5, 5 + len(data))]
    assert sum(col_e) == sum(a["n"] for a in agg.values())
    return ws, 4 + len(data)


def main():
    rows, n_trips, n_fares = load_catalog()
    n_daily = sum(1 for r in rows if r["n_days"] == 3)
    pair_agg = pair_aggregate(rows)
    pf_agg = pair_family_aggregate(rows)
    op_agg = operator_aggregate(rows)
    fam_agg = {}
    for r in rows:
        f = fam_agg.setdefault(r["family"], {"n": 0, "daily": 0, "pmin": None,
                                            "pmax": None})
        f["n"] += 1
        if r["n_days"] == 3:
            f["daily"] += 1
        if r["sell_min"]:
            f["pmin"] = min(f["pmin"] if f["pmin"] else 10 ** 12, r["sell_min"])
        if r["sell_max"]:
            f["pmax"] = max(f["pmax"] or 0, r["sell_max"])

    n_rows = len(rows)
    n_ops = len(op_agg)
    n_routes = len({r["route_id"] for r in rows})
    n_pairs = len(pair_agg)
    n_fams = len(fam_agg)
    print(f"catalog={n_rows} daily={n_daily} ops={n_ops} routes={n_routes} "
          f"pairs={n_pairs} fams={n_fams} (from {n_trips} trips/{n_fares} fares)")

    wb = Workbook()

    # ------------------------------------------------------- Sheet 1: Summary
    ws = wb.active
    ws.title = "Summary"
    B.setup_sheet(ws, title="Vexere Schedules & Seat Pricing — 9-City Network",
                  last_col=7)
    ws.cell(row=3, column=2,
            value=(f"Source: vexere.com · crawled {CRAWLED_ON} · "
                   "72 ordered city pairs across 9 cities · schedule "
                   "frequency verified 2026-10-10 → 2026-10-12")).font = \
        B.font_caption()
    ws.row_dimensions[3].height = 14

    kpis = [("Unique Schedules", n_rows), ("Daily (3/3 days)", n_daily),
            ("Operators", n_ops), ("Routes", n_routes),
            ("City Pairs", n_pairs), ("Seat Families", n_fams)]
    for i, (label, val) in enumerate(kpis):
        col = 2 + i
        c = ws.cell(row=5, column=col, value=val)
        c.font = B.font_kpi()
        c.alignment = B.align_date()
        c.number_format = INT
        lc = ws.cell(row=6, column=col, value=label)
        lc.font = B.font_kpi_label()
        lc.alignment = B.align_date()
    ws.row_dimensions[5].height = 30
    ws.row_dimensions[6].height = 14
    for col, w in zip("BCDEFG", (26, 16, 14, 15, 14, 14)):
        ws.column_dimensions[col].width = w

    rn = 8
    ws.cell(row=rn, column=2,
            value="Top Routes by Daily Departures").font = B.font_subheader()
    ws.row_dimensions[rn].height = 20
    rn += 1
    top = sorted(pair_agg.items(), key=lambda kv: (-kv[1]["daily"], kv[0]))[:12]
    headers = ["Route", "Operators", "Daily (3/3)", "Schedules",
               "Price Min (VND)", "Price Max (VND)"]
    for ci, h in enumerate(headers, 2):
        ws.cell(row=rn, column=ci, value=h)
    B.style_header_row(ws, rn, 2, 7)
    for i, ((fc, tc), a) in enumerate(top):
        r2 = rn + 1 + i
        vals = [f"{fc} → {tc}", len(a["ops"]), a["daily"], a["n"],
                a["pmin"], a["pmax"]]
        for ci, v in enumerate(vals, 2):
            c = ws.cell(row=r2, column=ci, value=v)
            if ci >= 3:
                c.number_format = INT
                c.alignment = B.align_number()
        B.style_data_row(ws, r2, 2, 7, i)
        for ci in range(3, 8):
            ws.cell(row=r2, column=ci).alignment = B.align_number()
    trn = rn + 1 + len(top)
    tvals = ["Top 12 total", None, sum(a["daily"] for _, a in top),
             sum(a["n"] for _, a in top), None, None]
    for ci, v in enumerate(tvals, 2):
        c = ws.cell(row=trn, column=ci, value=v)
        if ci >= 3 and v is not None:
            c.number_format = INT
    B.style_total_row(ws, trn, 2, 7)
    for ci in range(3, 8):
        ws.cell(row=trn, column=ci).alignment = B.align_number()

    rn = trn + 2
    ws.cell(row=rn, column=2, value="Seat Class Overview").font = \
        B.font_subheader()
    ws.row_dimensions[rn].height = 20
    rn += 1
    fam_items = sorted(fam_agg.items(), key=lambda kv: -kv[1]["n"])
    headers = ["Seat Family", "Schedules", "Share", "Daily (3/3)",
               "Price Min (VND)", "Price Max (VND)"]
    for ci, h in enumerate(headers, 2):
        ws.cell(row=rn, column=ci, value=h)
    B.style_header_row(ws, rn, 2, 7)
    for i, (fam, f) in enumerate(fam_items):
        r2 = rn + 1 + i
        vals = [fam, f["n"], f["n"] / n_rows, f["daily"], f["pmin"],
                f["pmax"]]
        for ci, v in enumerate(vals, 2):
            c = ws.cell(row=r2, column=ci, value=v)
            if ci == 4:
                c.number_format = PCT
            elif ci >= 3:
                c.number_format = INT
        B.style_data_row(ws, r2, 2, 7, i)
        for ci in range(3, 8):
            ws.cell(row=r2, column=ci).alignment = B.align_number()
    trn = rn + 1 + len(fam_items)
    tvals = ["Total", n_rows, 1.0, n_daily, None, None]
    for ci, v in enumerate(tvals, 2):
        c = ws.cell(row=trn, column=ci, value=v)
        if ci == 4:
            c.number_format = PCT
        elif ci >= 3:
            c.number_format = INT
    B.style_total_row(ws, trn, 2, 7)
    for ci in range(3, 8):
        ws.cell(row=trn, column=ci).alignment = B.align_number()

    rn = trn + 2
    ws.cell(row=rn, column=2, value="Notes").font = B.font_subheader()
    ws.row_dimensions[rn].height = 20
    notes = [
        "Schedule catalog: one row per unique recurring schedule (route + "
        "departure time + seat class), deduplicated from dated trips crawled "
        "for 2026-10-10 → 2026-10-12. No per-date trip instances are listed.",
        "Frequency: '3/3 days' = the schedule operates on every verified day "
        "(daily service). '2/3 days' and '1/3 days' = observed on fewer days "
        "(may be non-daily, seasonal, or sold out on the missing days).",
        "Prices are per seat in VND as listed on vexere.com at crawl time. "
        "List Price = operator list price; Min/Max Price = observed selling "
        "price range after discounts across the verified days.",
        "Arrival times show the day offset (+1d / +2d) computed from the "
        "scheduled duration. Durations and distances come from the operator's "
        "route data.",
        "Data source: internal-vroute.vexere.com (Vexere) trip search over 72 "
        "ordered city pairs across 9 Vietnamese cities, crawled "
        f"{CRAWLED_ON}.",
    ]
    for i, note in enumerate(notes):
        r2 = rn + 1 + i
        ws.merge_cells(start_row=r2, start_column=2, end_row=r2, end_column=7)
        c = ws.cell(row=r2, column=2, value=note)
        c.font = B.font_caption()
        c.alignment = Alignment(horizontal="left", vertical="top",
                                wrap_text=True)
        ws.row_dimensions[r2].height = 30

    # ------------------------------------------------- Sheet 2: Schedules
    build_schedules(wb, rows)

    # ---------------------------------------------------- Sheet 3: Routes
    routes_ws, routes_last = build_routes(wb, pair_agg)

    # ------------------------------------------ Sheet 4: Seat Class Pricing
    # (build via pair-family aggregation incl. median of per-row min price)
    sell_by_group = {}
    for r in rows:
        if r["sell_min"]:
            sell_by_group.setdefault(
                (r["from_city"], r["to_city"], r["family"]), []
            ).append(r["sell_min"])
    headers = ["From City", "To City", "Seat Family", "Schedules",
               "Daily (3/3)", "Operators", "Price Min (VND)",
               "Price Median (VND)", "Price Max (VND)"]
    fmts = [None, None, None, INT, INT, INT, INT, INT, INT]
    aligns = ["t", "t", "t", "r", "r", "r", "r", "r", "r"]
    items = sorted(pf_agg.items(),
                   key=lambda kv: (kv[0][0], kv[0][1], -kv[1]["n"]))
    data = []
    for (fc, tc, fam), a in items:
        prices = sorted(p for p in sell_by_group[(fc, tc, fam)] if p)
        med = int(statistics.median(prices)) if prices else None
        data.append([fc, tc, fam, a["n"], a["daily"], len(a["ops"]),
                     a["pmin"], med, a["pmax"]])
    total = ["Total", "", "", sum(x[3] for x in data),
             sum(x[4] for x in data), None, None, None, None]
    ws = wb.create_sheet("Seat Class Pricing")
    _write_table(
        ws, "Seat Pricing by Route & Seat Class", headers, data, fmts, aligns,
        total=total,
        caption=("Per city pair × seat class family: unique schedules, daily "
                 "(3/3) schedules, operators and the per-seat price range in "
                 "VND (median of observed selling prices)."),
        freeze="E5")
    assert sum(x[3] for x in data) == n_rows, "pricing sheet schedule sum"

    # -------------------------------------------------- Sheet 5: Operators
    headers = ["Operator", "Schedules", "Daily (3/3)", "Routes",
               "Cities Served", "Price Min (VND)", "Price Max (VND)",
               "Rating", "Reviews", "Top Route"]
    fmts = [None, INT, INT, INT, INT, INT, INT, DEC1, INT, None]
    aligns = ["t", "r", "r", "r", "r", "r", "r", "r", "r", "t"]
    ops = sorted(op_agg.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    data = []
    for name, a in ops:
        data.append([name, a["n"], a["daily"], len(a["routes"]),
                     len(a["cities"]), a["pmin"], a["pmax"], a["rating"],
                     a["reviews"], a["top_route"]])
    total = ["Total", sum(x[1] for x in data), sum(x[2] for x in data),
             sum(x[3] for x in data), None, None, None, None, None, ""]
    ws = wb.create_sheet("Operators")
    _write_table(
        ws, "Operators — Coverage & Pricing", headers, data, fmts, aligns,
        caption=("Per operator: unique schedules, daily (3/3) schedules, "
                 "routes, cities served, seat-price range, Vexere rating and "
                 "most-scheduled route. Sorted by schedules."),
        freeze="C5")
    ops_data_last = 4 + len(data)  # excludes totals row
    assert sum(x[1] for x in data) == n_rows, "operators schedule sum"
    assert len(data) == n_ops

    # ---------------------------------------------------- Sheet 6: Review
    rws = wb.create_sheet("Review")
    rws.sheet_properties.tabColor = "FFC000"
    sched_last = 4 + n_rows
    checks = [
        ["Schedules row count", n_rows,
         f"=COUNTA(Schedules!B5:B{sched_last})"],
        ["Routes schedule total", n_rows,
         f"=SUM(Routes!E5:E{routes_last})"],
        ["Seat pricing schedule total", n_rows,
         f"=SUM('Seat Class Pricing'!E5:E{4 + len(items)})"],
        ["Operators schedule total", n_rows,
         f"=SUM(Operators!C5:C{ops_data_last})"],
        ["Operators row count", n_ops,
         f"=COUNTA(Operators!B5:B{ops_data_last})"],
    ]
    hdrs = ["Check", "Expected", "Actual", "Status"]
    B.setup_sheet(rws, title="Cross-Validation Review", last_col=5)
    for ci, h in enumerate(hdrs, 2):
        rws.cell(row=4, column=ci, value=h)
    B.style_header_row(rws, 4, 2, 5)
    for i, (label, exp, actual) in enumerate(checks):
        rn = 5 + i
        rws.cell(row=rn, column=2, value=label)
        rws.cell(row=rn, column=3, value=exp).number_format = INT
        rws.cell(row=rn, column=4, value=actual)
        rws.cell(row=rn, column=5,
                 value=f'=IF(C{rn}=D{rn},"PASS","FAIL")')
        B.style_data_row(rws, rn, 2, 5, i)
        rws.cell(row=rn, column=3).alignment = B.align_number()
        rws.cell(row=rn, column=4).alignment = B.align_number()
        rws.cell(row=rn, column=5).alignment = B.align_date()
    for col, w in zip("BCDE", (30, 14, 14, 10)):
        rws.column_dimensions[col].width = w

    # KPI cross-checks (semantic inspection)
    assert ws.max_row >= 5
    assert kpis[0][1] == n_rows and kpis[1][1] == n_daily

    wb.properties.creator = "Z.ai"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(OUT))
    print(f"saved {OUT} ({OUT.stat().st_size / 1e6:.2f} MB) "
          f"sheets={wb.sheetnames}")


if __name__ == "__main__":
    main()
