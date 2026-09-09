#!/usr/bin/env python3
"""Resolve the 9 remaining unmatched brands with manual slug picks.
Verification: fetch each candidate brand page, check the JSON-LD name + routes text.
"""
import json
import re
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

CANDIDATES = {
    "Liên Hưng": ["xe-lien-hung-nam-dinh"],
    "Lữ Hành Quỳnh Thanh": ["xe-quynh-thanh-vip-limo"],
    "Hạnh Cafe - Hà Phương Limousine": ["xe-hanh-cafe", "xe-ha-phuong-limousine"],
    "LH Minh Trí Limousine": ["xe-minh-tri-limousine-da-lat", "xe-minh-tri-phat", "xe-minh-tri-happyday"],
    "G8 OPEN TOUR": ["xe-g8-sapa-open-tour"],
    "Cửa Ông Bus": ["xe-cua-ong-limousine"],
    "Thăng Long Hà Nội Limousine": ["xe-thang-long-limousine", "xe-thang-long-transport"],
    "Cát Bà Limousine": ["xe-bus-cat-ba", "xe-cat-ba-daily-limousine", "xe-adam-viet-cat-ba"],
    "Bus Vip Limousine Ha Giang": ["xe-bus-limousine-ha-giang", "xe-express-ha-giang"],
}

# route hints to pick between candidates
HINTS = {
    "Hạnh Cafe - Hà Phương Limousine": ["Đà Nẵng", "Nha Trang"],
    "LH Minh Trí Limousine": ["Đà Lạt", "Nha Trang"],
    "Thăng Long Hà Nội Limousine": ["Sa Pa", "Sapa"],
    "Cát Bà Limousine": ["Hải Phòng", "Hà Nội"],
    "Bus Vip Limousine Ha Giang": ["Hà Giang"],
}


def fetch(slug):
    url = f"https://vexere.com/vi-VN/{slug}/danh-gia"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def jsonld_name(html):
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None, ""
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None, ""
    if isinstance(d, dict) and d.get("@type") == "Product":
        txt = html
        return d.get("name"), txt
    return None, ""


results = {}
for brand, cands in CANDIDATES.items():
    print(f"\n### {brand}")
    for slug in cands:
        try:
            html = fetch(slug)
            name, _ = jsonld_name(html)
            n_reviews = html.count('"@type": "Review"') + html.count('"@type":"Review"')
            hints = HINTS.get(brand, [])
            hint_hits = sum(1 for h in hints if h in html)
            # route presence: count 'Tuyến' mentions
            print(f"  {slug}: name={name!r}, jsonld_reviews~{n_reviews}, hint_hits={hint_hits}/{len(hints)}, size={len(html)}")
            results.setdefault(brand, []).append((slug, name, hint_hits, len(html)))
        except Exception as e:
            print(f"  {slug}: ERROR {e}")

json.dump(results, open("/home/z/my-project/research/manual_candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
