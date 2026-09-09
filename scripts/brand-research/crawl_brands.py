#!/usr/bin/env python3
"""Crawl vexere.com brand 'danh-gia' pages for ALL brands in our dataset.

Extracts per brand:
- Product JSON-LD: name, aggregateRating, review[] (author/date/rating/body)
- LocalBusiness JSON-LD: telephone, priceRange, streetAddress
- BreadcrumbList: brand page URL + logo image
- Body text: operator's own phones ("Số điện thoại xe X Y: NNN") + office addresses
Saves compact JSON (HTML discarded).
"""
import gzip
import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

MANUAL = {
    "Liên Hưng": "xe-lien-hung-nam-dinh",
    "Lữ Hành Quỳnh Thanh": "xe-quynh-thanh-vip-limo",
    "Hạnh Cafe - Hà Phương Limousine": "xe-hanh-cafe",
    "LH Minh Trí Limousine": "xe-minh-tri-limousine-da-lat",
    "G8 OPEN TOUR": "xe-g8-sapa-open-tour",
    "Cửa Ông Bus": "xe-cua-ong-limousine",
    "Thăng Long Hà Nội Limousine": "xe-thang-long-limousine",
    "Bus Vip Limousine Ha Giang": "xe-bus-limousine-ha-giang",
}


def fetch(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
                "Accept-Language": "vi,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


PHONE_CTX = re.compile(
    r"Số điện thoại\s*xe\s+(.{1,80}?)\s*[:：]\s*([0-9()\s.\-+/]{6,20})", re.S
)
OFFICE_CTX = re.compile(
    r"Địa chỉ văn phòng,?\s*tram?\s*xe?\s+(.{1,80}?)\s*[:：]\s*([^\n\r<]{5,140})", re.S
)


def parse_page(slug, html):
    out = {"slug": slug}
    # JSON-LD blocks
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        for it in d if isinstance(d, list) else [d]:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            if t == "Product":
                out["product_name"] = it.get("name")
                out["image"] = it.get("image")
                out["description"] = (it.get("description") or "")[:300]
                ar = it.get("aggregateRating") or {}
                out["rating"] = ar.get("ratingValue")
                out["rating_count"] = ar.get("ratingCount")
                out["reviews"] = [
                    {
                        "date": r.get("datePublished"),
                        "author": (r.get("author") or {}).get("name"),
                        "rating": ((r.get("reviewRating") or {}).get("ratingValue")),
                        "text": (r.get("reviewBody") or "").strip()[:400],
                    }
                    for r in (it.get("review") or [])
                    if isinstance(r, dict)
                ]
            elif t == "LocalBusiness":
                out["telephone"] = it.get("telephone")
                out["price_range"] = it.get("priceRange")
                out["address"] = (it.get("address") or {}).get("streetAddress")
            elif t == "BreadcrumbList" and "brand_url" not in out:
                items = it.get("itemListElement") or []
                if len(items) >= 2:
                    b = items[1].get("item") or {}
                    out["brand_url"] = b.get("@id")
                    out["brand_title"] = b.get("name")
                    out["logo"] = b.get("image")
    # strip scripts/styles then get plain text for body regexes
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    phones = {}
    for m in PHONE_CTX.finditer(text):
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        num = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(".,;")
        if num and len(re.sub(r"\D", "", num)) >= 6:
            phones.setdefault(num, loc)
    out["own_phones"] = phones
    offices = []
    for m in OFFICE_CTX.finditer(text):
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        addr = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(".,;")
        offices.append(f"{loc}: {addr}")
    # dedupe
    seen, oo = set(), []
    for o in offices:
        if o not in seen:
            seen.add(o)
            oo.append(o)
    out["offices"] = oo[:10]
    # FAQ count + price mentions for feature stats
    out["faq_count"] = len(re.findall(r"(?i)câu hỏi thường gặp|FAQ", html))
    return out


def main():
    inv = json.load(open("/home/z/my-project/research/brand_matches.json"))
    tasks = {}  # slug -> [brand names]
    for b in inv:
        slug = b.get("slug") or MANUAL.get(b["name"])
        if b["name"] in MANUAL:
            slug = MANUAL[b["name"]]
        if not slug:
            continue
        tasks.setdefault(slug, []).append(b["name"])
    print(f"unique slugs to crawl: {len(tasks)}", file=sys.stderr)

    results = {}
    errors = []

    def work(slug):
        url = f"https://vexere.com/vi-VN/{slug}/danh-gia"
        html = fetch(url)
        return slug, parse_page(slug, html)

    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, s): s for s in tasks}
        for fut in as_completed(futs):
            slug = futs[fut]
            try:
                s, data = fut.result()
                results[s] = data
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(tasks)} done", file=sys.stderr)
            except Exception as e:
                errors.append((slug, str(e)[:100]))
                print(f"  ERR {slug}: {e}", file=sys.stderr)

    json.dump(results, open("/home/z/my-project/research/brands_data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nCRAWLED: {len(results)} ok, {len(errors)} errors", file=sys.stderr)
    for s, e in errors[:10]:
        print(f"  {s}: {e}", file=sys.stderr)
    # quick stats
    n_rev = sum(len(r.get("reviews", [])) for r in results.values())
    n_own = sum(1 for r in results.values() if r.get("own_phones"))
    n_addr = sum(1 for r in results.values() if r.get("address") or r.get("offices"))
    print(f"reviews captured: {n_rev} | brands with own phones: {n_own} | with address: {n_addr}", file=sys.stderr)


if __name__ == "__main__":
    main()
