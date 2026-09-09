#!/usr/bin/env python3
"""Report part 2: Customer Feedback + Ratings Analysis (+charts) + Review sheets."""
import json
import sys
import os
from collections import Counter

XLSX_SKILL_DIR = "/home/z/my-project/skills/xlsx"
for sub in [XLSX_SKILL_DIR, os.path.join(XLSX_SKILL_DIR, "templates")]:
    if sub not in sys.path:
        sys.path.insert(0, sub)

from base import (  # noqa: E402
    FONT_NAME, PRIMARY, ROW_HEIGHTS,
    font_caption, font_subheader, font_body,
    setup_sheet, style_header_row, style_data_row, style_total_row,
    auto_fit_columns, create_bar_chart, setup_chart_titles,
    apply_chart_colors, align_number,
)
from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment  # noqa: E402
from openpyxl.chart import Reference  # noqa: E402

OUT = "/home/z/my-project/download/vexere_brands_feedback_report.xlsx"
BRANDS = json.load(open("/home/z/my-project/research/final_brands.json", encoding="utf-8"))
REVIEWS = json.load(open("/home/z/my-project/research/final_reviews.json", encoding="utf-8"))
SUPPORT = json.load(open("/home/z/my-project/research/final_features.json", encoding="utf-8"))["support"]
FEATURES = json.load(open("/home/z/my-project/research/final_features.json", encoding="utf-8"))["features"]


def safe(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "@"):
        return "'" + v
    return v


def write_table(ws, headers, rows, start_row=4, number_cols=None, col_start=2, totals=None):
    number_cols = number_cols or set()
    last_col = col_start + len(headers) - 1
    for i, h in enumerate(headers):
        ws.cell(row=start_row, column=col_start + i, value=h)
    style_header_row(ws, start_row, col_start, last_col)
    r = start_row + 1
    for idx, row in enumerate(rows):
        for i, v in enumerate(row):
            c = ws.cell(row=r, column=col_start + i, value=safe(v))
            if i in number_cols and isinstance(v, (int, float)):
                c.number_format = "#,##0"
        style_data_row(ws, r, col_start, last_col, idx)
        for i in range(len(headers)):
            cell = ws.cell(row=r, column=col_start + i)
            cell.alignment = (align_number() if i in number_cols
                              else Alignment(horizontal="left", vertical="top", wrap_text=True))
        r += 1
    if totals:
        for i, v in enumerate(totals):
            ws.cell(row=r, column=col_start + i, value=safe(v))
        style_total_row(ws, r, col_start, last_col)
        r += 1
    return r


def caption(ws, row, col, text, span=None):
    c = ws.cell(row=row, column=col, value=safe(text))
    c.font = font_caption()
    c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    if span:
        ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
    return row + 1


wb = load_workbook(OUT)

# ============================================================ 5. CUSTOMER FEEDBACK
ws = wb.create_sheet("Customer Feedback")
setup_sheet(ws, title="Customer Feedback on vexere.com — Verbatim Reviews", last_col=7)
ws.cell(row=3, column=2, value=f"{len(REVIEWS)} reviews captured from brand pages (danh-gia) · sorted by brand, then date (newest first) · as written by customers ( Vietnamese / English / other)").font = font_caption()
ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=7)

rv = sorted(REVIEWS, key=lambda r: (r["brand"].lower(), r.get("date") or "",))
rows = [[i, r["brand"], r.get("date") or "", r["author"] or "",
         r["stars"] if r["stars"] is not None else None, r["text"]]
        for i, r in enumerate(rv, 1)]
hdr = ["#", "Brand", "Review date", "Customer name", "Stars", "Customer review"]
tot = ["", f"TOTAL — {len(rows):,} reviews", None, None, f"=COUNT(G5:G{4+len(rows)})", ""]
nxt = write_table(ws, hdr, rows, number_cols={0, 4}, totals=tot)
nxt += 1
nxt = caption(ws, nxt, 2, "Each brand page exposes its latest reviews (up to 5–10) in structured data; cumulative totals per brand are in 'Brand Directory' (# Ratings). Reviews are quoted verbatim — including typos and mixed languages.", span=6)

auto_fit_columns(ws, min_width=6, max_width=22, header_row=4, data_start_row=5)
ws.column_dimensions["C"].width = 26
ws.column_dimensions["F"].width = 8
ws.column_dimensions["G"].width = 90
for rr in range(5, 5 + len(rows)):
    ws.row_dimensions[rr].height = 30
ws.freeze_panes = "C5"

# ============================================================ 6. RATINGS ANALYSIS
ws = wb.create_sheet("Ratings Analysis")
setup_sheet(ws, title="Ratings Analysis — vexere.com Brand Ratings & Review Mix", last_col=8)

rated = [b for b in BRANDS if b["rating"]]
avg = sum(b["rating"] for b in rated) / len(rated)
tot_cnt = sum(b["rating_count"] or 0 for b in BRANDS)
star_counts = Counter(r["stars"] for r in REVIEWS if r["stars"] is not None)

# ---- Block A: distribution buckets (B4:D11) + chart
buckets = [
    ("4.5 – 5.0 ★", sum(1 for b in rated if b["rating"] >= 4.5)),
    ("4.0 – 4.4 ★", sum(1 for b in rated if 4.0 <= b["rating"] < 4.5)),
    ("3.5 – 3.9 ★", sum(1 for b in rated if 3.5 <= b["rating"] < 4.0)),
    ("3.0 – 3.4 ★", sum(1 for b in rated if 3.0 <= b["rating"] < 3.5)),
    ("2.0 – 2.9 ★", sum(1 for b in rated if 2.0 <= b["rating"] < 3.0)),
    ("Below 2.0 ★", sum(1 for b in rated if b["rating"] < 2.0)),
    ("No rating yet", sum(1 for b in BRANDS if not b["rating"])),
]
r0 = 4
ws.cell(row=r0 - 1, column=2, value="Rating distribution of 306 brands").font = font_subheader()
nxt = write_table(ws, ["Rating bucket", "Brands"], buckets, start_row=r0, number_cols={1})
dist_end = r0 + len(buckets)  # last data row (no totals)

chart = create_bar_chart(chart_type="col", width=17, height=8.5)
setup_chart_titles(chart, title="Brand rating distribution (306 brands)", y_title="Brands", x_title="Rating bucket")
data = Reference(ws, min_col=3, min_row=r0, max_row=dist_end)
cats = Reference(ws, min_col=2, min_row=r0 + 1, max_row=dist_end)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
apply_chart_colors(chart)
chart.legend = None
ws.add_chart(chart, "F4")

# ---- Block B: top 10 most reviewed
r1 = dist_end + 3
ws.cell(row=r1 - 1, column=2, value="Top 10 most-reviewed brands on vexere.com").font = font_subheader()
top_rev = sorted([b for b in BRANDS if b["rating_count"]], key=lambda b: -b["rating_count"])[:10]
rows = [[i, b["name"], b["rating_count"], b["rating"]] for i, b in enumerate(top_rev, 1)]
nxt = write_table(ws, ["#", "Brand", "# Ratings", "Rating"], rows, start_row=r1, number_cols={0, 2, 3})
top_end = r1 + len(rows)

chart2 = create_bar_chart(chart_type="bar", width=17, height=9)
setup_chart_titles(chart2, title="Most-reviewed brands (cumulative customer ratings)", x_title="Ratings")
data = Reference(ws, min_col=4, min_row=r1, max_row=top_end)
cats = Reference(ws, min_col=3, min_row=r1 + 1, max_row=top_end)
chart2.add_data(data, titles_from_data=True)
chart2.set_categories(cats)
apply_chart_colors(chart2)
chart2.legend = None
ws.add_chart(chart2, "F" + str(r1))

# ---- Block C: best rated (min 50 ratings)
r2 = top_end + 3
ws.cell(row=r2 - 1, column=2, value="Best-rated brands (≥ 50 ratings)").font = font_subheader()
best = sorted([b for b in BRANDS if b["rating"] and (b["rating_count"] or 0) >= 50],
              key=lambda b: (-b["rating"], -(b["rating_count"] or 0)))[:12]
rows = [[i, b["name"], b["rating"], b["rating_count"]] for i, b in enumerate(best, 1)]
nxt = write_table(ws, ["#", "Brand", "Rating", "# Ratings"], rows, start_row=r2, number_cols={0, 2, 3})
best_end = r2 + len(rows)

# ---- Block D: star mix of captured reviews
r3 = best_end + 3
ws.cell(row=r3 - 1, column=2, value="Star mix of the captured verbatim reviews").font = font_subheader()
star_rows = [(f"{s} ★", star_counts.get(s, 0)) for s in (5, 4, 3, 2, 1)]
nxt = write_table(ws, ["Stars", "Reviews"], star_rows, start_row=r3, number_cols={1})
star_end = r3 + len(star_rows)

chart3 = create_bar_chart(chart_type="col", width=17, height=8)
setup_chart_titles(chart3, title="Star mix of captured reviews", y_title="Reviews", x_title="Stars")
data = Reference(ws, min_col=3, min_row=r3, max_row=star_end)
cats = Reference(ws, min_col=2, min_row=r3 + 1, max_row=star_end)
chart3.add_data(data, titles_from_data=True)
chart3.set_categories(cats)
apply_chart_colors(chart3)
chart3.legend = None
ws.add_chart(chart3, "F" + str(r3))

# ---- Block E: headline stats
r4 = star_end + 3
ws.cell(row=r4 - 1, column=2, value="Headline statistics").font = font_subheader()
stats = [
    ("Brands analysed", len(BRANDS)),
    ("Brands rated on vexere", len(rated)),
    ("Average rating (rated brands)", round(avg, 2)),
    ("Cumulative ratings across brands", tot_cnt),
    ("Verbatim reviews captured", len(REVIEWS)),
    ("Brands ≥ 4.0 ★", sum(1 for b in rated if b["rating"] >= 4.0)),
    ("Brands < 3.0 ★", sum(1 for b in rated if b["rating"] < 3.0)),
    ("5★ share of captured reviews", f"{star_counts.get(5, 0) / max(1, sum(star_counts.values())):.1%}"),
]
nxt = write_table(ws, ["Metric", "Value"], stats, start_row=r4, number_cols={1})
nxt += 1
caption(ws, nxt, 2, "Ratings are the aggregateRating values published on each brand's vexere.com page (JSON-LD), collected 2026-09-09.", span=4)

auto_fit_columns(ws, min_width=8, max_width=26, header_row=r0, data_start_row=r0 + 1)
ws.column_dimensions["C"].width = 30
ws.column_dimensions["D"].width = 13
ws.column_dimensions["E"].width = 11

# ============================================================ 7. REVIEW (cross-checks)
ws = wb.create_sheet("Review")
ws.sheet_properties.tabColor = "FFC000"
setup_sheet(ws, title="Cross-Validation Review — live checks (all rows must show PASS)", last_col=6)
nb, nr, nrv = len(BRANDS), len(REVIEWS), len(FEATURES)
checks = [
    ("Brand Directory row count", nb, "=COUNTA('Brand Directory'!C5:C10000)"),
    ("Customer Feedback review count", nr, "=COUNTA('Customer Feedback'!C5:C100000)"),
    ("Support Phones researched brands", 30, "=COUNTA('Support Phones'!C5:C1000)"),
    ("Vexere Features documented", nrv, "=COUNTA('Vexere Features'!C5:C1000)"),
    ("Cumulative ratings total", tot_cnt, "=SUM('Brand Directory'!E5:E10000)"),
    ("Distribution buckets sum to 306", nb, "=SUM('Ratings Analysis'!D5:D11)"),
]
rows = [[c[0], c[1], c[2], None] for c in checks]
for i, row in enumerate(rows):
    # expected as literal, actual as formula, status as formula
    r = 5 + i
    ws.cell(row=r, column=2, value=row[0])
    ws.cell(row=r, column=3, value=row[1])
    ws.cell(row=r, column=4, value=row[2])
    ws.cell(row=r, column=5, value=f'=IF(C{r}=D{r},"PASS","FAIL")')
style_header_row(ws, 4, 2, 5)
for i in range(len(rows)):
    style_data_row(ws, 5 + i, 2, 5, i)
    ws.cell(row=5 + i, column=3).alignment = align_number()
    ws.cell(row=5 + i, column=4).alignment = align_number()
    ws.cell(row=5 + i, column=5).alignment = Alignment(horizontal="center", vertical="center")
auto_fit_columns(ws, min_width=10, max_width=40, header_row=4, data_start_row=5)

wb.save(OUT)
print("PART 2 SAVED:", OUT)
print("sheets:", wb.sheetnames)
