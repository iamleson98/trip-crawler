# Brand Research — Vexere.com Platform, Support Phones & Customer Feedback

Deep-research pipeline that complements the trip crawler: it enriches the crawled
operator list (from the N×N dataset) with everything vexere.com publishes about
each transport brand, plus web-researched official support hotlines, and builds
the report workbook `data/vexere_brands_feedback_report.xlsx`.

## What the report contains (7 sheets)

| Sheet | Content |
|-------|---------|
| Summary | Key metrics, methodology, data sources |
| Vexere Features | 24 features in 5 categories: ticketing (bus/train/flight/Tet/charter/motorbike), platform (multimodal comparison, GPS tracking, e-ticket, promotions, payments, apps), B2B (BMS software, white-label sites, agents/affiliate), content/SEO, company |
| Brand Directory | All 306 operators: rating, # ratings, trips, routes, vehicle types, price range, hotlines, own phones, address, vexere page URL |
| Support Phones | Web-researched official hotlines for the top 30 brands, cross-checked with vexere pages |
| Customer Feedback | 1,199 verbatim customer reviews (author, date, stars, text) |
| Ratings Analysis | Distribution buckets, most-reviewed & best-rated brands, star mix + 3 charts |
| Review | Live cross-check formulas (row counts / totals) — all PASS |

## Pipeline (run in order)

```
python3 extract_brands.py     # 1. unique brands from the enriched N×N Excel
python3 match_brands.py       # 2. match brand names → vexere slugs (sitemap)
python3 resolve_manual.py     # 3. probe candidates for unmatched brands
python3 crawl_brands.py       # 4. crawl ~300 brand 'danh-gia' pages (8 workers,
                              #    JSON-LD: rating, reviews, phone, address)
bash web_research.sh          # 5. 44 web searches: features + 30 brand hotlines
python3 extract_phones.py     # 6. phone numbers out of search snippets
python3 merge_data.py         # 7. merge everything → final_*.json
python3 build_features.py     # 8. curated feature inventory
python3 build_report.py && python3 build_report2.py && python3 fix_review.py
                              # 9. build the workbook (xlsx-skill design system)
```

## Data sources

- `vexere.com/bus-operator-details*.xml` — ~4,300 brand-page URLs
  (each brand has `danh-gia` (reviews), `so-dien-thoai-dia-chi` (phone/address),
  `hinh-anh` (images) pages)
- Brand pages: JSON-LD `Product` (aggregateRating + review[]),
  `LocalBusiness` (telephone, priceRange, address), plus visible
  "Số điện thoại xe …" contact blocks
- Web search: official operator websites, App Store / Google Play,
  blog.vexere.com, bms.vexere.com, press (Spiral Ventures 2018,
  Woowa Brothers 2019)
- Crawled trips: `data/vexere_nxn_trips_enriched.xlsx` (41,993 trips / 72 pairs)

## Notes

- 296/306 brands have a matched vexere page; 10 brands are flagged in the
  report (no dedicated page, shared page, or broken page).
- Ratings are the `aggregateRating` values published on vexere.com
  (collected 2026-09-09); the platform exposes only the latest 5–10 reviews
  per brand in page data — 1,199 captured verbatim.
