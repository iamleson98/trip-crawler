#!/usr/bin/env python3
"""fetch_vehicle_media.py — collect VeXeRe vehicle-type pictures.

What it does
------------
VeXeRe serves a captioned photo gallery per (company, seat-template) —
i.e. per vehicle type of each operator:

    GET https://api.vexere.com/v3/company/{company_id}/media
        ?resource_id={seat_template_id}
    Authorization: bearer <anonymous token from POST https://vexere.com/getToken>

The (company_id, seat_template_id) pairs are harvested from the trip
search API used by the crawler:

    GET https://internal-vroute.vexere.com/v2/route/
        ?filter[from]=..&filter[to]=..&filter[quantity]=1&filter[date]=YYYY-MM-DD
        &page=N&pagesize=100
    -> data[].company{id,name,images}  +  data[].route.schedules[]{
         vehicle_type, total_seats, seat_template_id }

Pipeline (all steps resumable via JSON caches):
  1. token   — anonymous bearer (auto-refresh on 401)
  2. search  — all 72 N×N pairs x DATES from the crawl manifest
  3. media   — one call per unique (company, seat_template)
  4. download— unique image files (dedupe by image_id)
  5. manifest+ vehicle_type_media.json, HTML gallery, and
     booking_seed.sqlite brand.logo_url update (matched by operator name)

Usage: python3 fetch_vehicle_media.py [--no-download] [--no-db-update]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

BASE = Path("/home/z/my-project/download")
SCRIPTS = Path("/home/z/my-project/scripts")
PAIRS_CACHE = BASE / "vehicle_pairs_cache.json"
MEDIA_CACHE = BASE / "vehicle_media_cache.json"
IMG_DIR = BASE / "vehicle_images"
MANIFEST = BASE / "vehicle_type_media.json"
GALLERY = BASE / "vehicle_images.html"
DB_PATH = BASE / "booking_seed.sqlite"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
DATES = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"]
WORKERS_SEARCH = 4
WORKERS_MEDIA = 6
WORKERS_DL = 4


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_url(u: str) -> str:
    """VeXeRe returns URLs with stray leading slashes ('////static...',
    'https:////static...'). Strip any scheme + slashes, rebuild cleanly."""
    u = str(u or "").strip()
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = u.lstrip("/")
    return "https://" + u


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


# --------------------------------------------------------------------------
# Token management (anonymous bearer; refresh on 401/expiry)
# --------------------------------------------------------------------------
class TokenManager:
    def __init__(self):
        self._token: str | None = None
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token is None:
                self._token = self._fetch()
            return self._token

    def refresh(self):
        with self._lock:
            self._token = self._fetch()

    def _fetch(self) -> str:
        r = requests.post(
            "https://vexere.com/getToken",
            headers={"Accept": "application/json, text/plain, */*",
                     "Origin": "https://vexere.com",
                     "Referer": "https://vexere.com/", "User-Agent": UA},
            timeout=20)
        r.raise_for_status()
        return r.json()["access_token"]


TOKENS = TokenManager()


def api_get(url: str, params: dict | None = None, tries: int = 4):
    """GET with bearer auth, retry/backoff, token refresh on 401."""
    last_err = None
    for attempt in range(tries):
        try:
            r = requests.get(
                url, params=params,
                headers={"Authorization": f"bearer {TOKENS.get()}",
                         "Accept": "application/json, text/plain, */*",
                         "Accept-Language": "vi-VN",
                         "Origin": "https://vexere.com",
                         "Referer": "https://vexere.com/", "User-Agent": UA},
                timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                TOKENS.refresh()
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((attempt + 1) * 2)
                continue
            print(f"  [http] {r.status_code} {url[:90]} — skipping")
            return None
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep((attempt + 1) * 1.5)
    print(f"  [fail] {url[:90]} ({last_err})")
    return None


# --------------------------------------------------------------------------
# Step 1: search all pairs -> (company, seat_template) inventory
# --------------------------------------------------------------------------
def search_pair(pair: dict, date: str) -> dict:
    """Search one (from, to, date); return {'companies': {...}, 'n_trips': n}."""
    out = {"companies": {}, "n_trips": 0, "n_templates": 0}
    page = 1
    while True:
        data = api_get(
            "https://internal-vroute.vexere.com/v2/route/",
            params={"filter[from]": pair["from_id"],
                    "filter[to]": pair["to_id"],
                    "filter[quantity]": 1,
                    "filter[date]": date,
                    "page": page, "pagesize": 100})
        if data is None:
            break
        trips = data.get("data") or []
        if isinstance(trips, dict):            # defensive
            trips = trips.get("trips") or []
        out["n_trips"] += len(trips)
        for t in trips:
            comp = t.get("company") or {}
            cid, cname = comp.get("id"), comp.get("name")
            if not cid or not cname:
                continue
            c = out["companies"].setdefault(
                str(cid), {"id": cid, "name": cname.strip(),
                           "cover": "", "templates": {}})
            if not c["cover"]:
                imgs = comp.get("images") or []
                if imgs:
                    files = imgs[0].get("files") or {}
                    c["cover"] = norm_url(files.get("1000x600")
                                          or next(iter(files.values()), ""))
            for s in (t.get("route") or {}).get("schedules") or []:
                tpl = s.get("seat_template_id")
                if tpl is None:
                    continue
                label = re.sub(r"\s+", " ",
                               str(s.get("vehicle_type") or "")).strip()
                if str(tpl) not in c["templates"]:
                    c["templates"][str(tpl)] = {
                        "seat_template_id": tpl, "label": label,
                        "total_seats": s.get("total_seats")}
                    out["n_templates"] += 1
        total_pages = int(data.get("total_pages") or 1)
        if page >= total_pages or not trips:
            break
        page += 1
        time.sleep(0.3)
    return out


def collect_pairs():
    if PAIRS_CACHE.exists():
        blob = json.loads(PAIRS_CACHE.read_text())
        print(f"  [cache] {len(blob['companies'])} companies, "
              f"{blob['n_templates']} templates — reusing")
        return blob
    manifest = json.loads(
        (Path("/home/z/my-project/trip-crawler-clone/data/"
              "vexere_nxn_manifest.json")).read_text())
    companies: dict[str, dict] = {}
    n_trips = 0
    done = 0
    jobs = [(p, d) for p in manifest for d in DATES]
    with ThreadPoolExecutor(max_workers=WORKERS_SEARCH) as ex:
        futs = {ex.submit(search_pair, p, d): (p, d) for p, d in jobs}
        for fut in as_completed(futs):
            p, d = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [err] pair {p['from_id']}->{p['to_id']} {d}: {e}")
                continue
            n_trips += res["n_trips"]
            for cid, c in res["companies"].items():
                agg = companies.setdefault(
                    cid, {"id": c["id"], "name": c["name"],
                          "cover": c["cover"], "templates": {}})
                if not agg["cover"]:
                    agg["cover"] = c["cover"]
                agg["templates"].update(c["templates"])
            done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  searched {done}/{len(jobs)} pair-dates — "
                      f"{len(companies)} companies, {n_trips} trips")
    blob = {"generated_at": now_iso(), "dates": DATES,
            "companies": companies,
            "n_templates": sum(len(c["templates"])
                               for c in companies.values()),
            "n_trips": n_trips}
    PAIRS_CACHE.write_text(json.dumps(blob, ensure_ascii=False, indent=1))
    return blob


# --------------------------------------------------------------------------
# Step 2: media gallery per (company, seat_template)
# --------------------------------------------------------------------------
def fetch_media(cid: int, tpl: int):
    data = api_get(f"https://api.vexere.com/v3/company/{cid}/media",
                   params={"resource_id": tpl})
    if data is None:
        return None
    images = (data.get("data") or {}).get("images") or []
    out = []
    for im in images:
        files = im.get("files") or {}
        url = norm_url(files.get("1000x600")
                       or next(iter(files.values()), ""))
        if not url:
            continue
        cap = im.get("caption") or {}
        title = im.get("title") or {}
        out.append({"image_id": im.get("image_id"), "url": url,
                    "caption_vi": cap.get("vi"), "caption_en": cap.get("en"),
                    "title_vi": title.get("vi")})
    return out


def collect_media(pairs_blob: dict):
    cache = {}
    if MEDIA_CACHE.exists():
        cache = json.loads(MEDIA_CACHE.read_text())
        print(f"  [cache] {len(cache)} media entries")
    jobs = []
    for cid, c in pairs_blob["companies"].items():
        for tpl in c["templates"]:
            key = f"{cid}:{tpl}"
            if key not in cache:
                jobs.append((int(cid), int(tpl), key))
    print(f"  media to fetch: {len(jobs)}")
    lock = threading.Lock()
    done = 0
    if jobs:
        with ThreadPoolExecutor(max_workers=WORKERS_MEDIA) as ex:
            futs = {ex.submit(fetch_media, cid, tpl): key
                    for cid, tpl, key in jobs}
            for fut in as_completed(futs):
                key = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    print(f"  [err] media {key}: {e}")
                    res = None
                with lock:
                    cache[key] = res if res is not None else []
                    done += 1
                    if done % 100 == 0 or done == len(jobs):
                        print(f"  media {done}/{len(jobs)}")
                        MEDIA_CACHE.write_text(
                            json.dumps(cache, ensure_ascii=False))
    MEDIA_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    return cache


# --------------------------------------------------------------------------
# Step 3: download unique images
# --------------------------------------------------------------------------
IMG_EXTS = ("jpeg", "png", "webp", "gif")


def sniff_ext(content: bytes) -> str | None:
    if content[:2] == b"\xff\xd8":
        return "jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[:4] in (b"GIF8",):
        return "gif"
    return None


def existing_file(iid) -> Path | None:
    for ext in IMG_EXTS:
        p = IMG_DIR / f"{iid}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def download_image(im: dict) -> str | None:
    iid = im["image_id"]
    p = existing_file(iid)
    if p:
        return str(p)
    for attempt in range(4):
        try:
            r = requests.get(im["url"], headers={"User-Agent": UA,
                                                 "Referer": "https://vexere.com/"},
                             timeout=45)
            if r.status_code == 200:
                ext = sniff_ext(r.content)
                if ext:
                    path = IMG_DIR / f"{iid}.{ext}"
                    tmp = IMG_DIR / f"{iid}.tmp"
                    tmp.write_bytes(r.content)
                    os.replace(tmp, path)      # atomic — no partial files
                    return str(path)
                print(f"  [dl] 200 but not an image: {im['url'][:80]}")
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((attempt + 1) * 2)
                continue
            print(f"  [dl] {r.status_code} {im['url'][:80]}")
            return None
        except requests.RequestException:
            time.sleep((attempt + 1) * 1.5)
    return None


def download_all(pairs_blob: dict, media_cache: dict, do_download: bool):
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    # unique images across all (company, template)
    images: dict[int, dict] = {}
    for key, imgs in media_cache.items():
        cid, tpl = key.split(":")
        for im in imgs or []:
            iid = im.get("image_id")
            if iid is None:
                continue
            rec = images.setdefault(iid, dict(im))
            rec.setdefault("companies", set()).add((cid, tpl))
    jobs = list(images.values())
    print(f"  unique images: {len(jobs)}")
    if not do_download:
        # still record files already on disk so the manifest is complete
        results = {}
        for iid in images:
            p = existing_file(iid)
            if p:
                results[iid] = str(p)
        return images, results
    results = {}
    done = ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS_DL) as ex:
        futs = {ex.submit(download_image, im): im["image_id"]
                for im in jobs}
        for fut in as_completed(futs):
            iid = futs[fut]
            try:
                results[iid] = fut.result()
            except Exception:
                results[iid] = None
            done += 1
            if results[iid]:
                ok += 1
            if done % 250 == 0 or done == len(jobs):
                print(f"  downloaded {done}/{len(jobs)} (ok {ok})")
    print(f"  download complete: {ok}/{len(jobs)} ok")
    return images, results


# --------------------------------------------------------------------------
# Step 4: manifest + HTML gallery + DB update
# --------------------------------------------------------------------------
def build_manifest(pairs_blob, media_cache, images, dl_results):
    # seed DB brand lookup by normalized name
    import sqlite3
    brand_by_name = {}
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        for slug, name in con.execute("SELECT slug, name FROM brand"):
            brand_by_name[norm_name(name)] = slug
        con.close()

    companies = {}
    n_vehicle_types = 0
    for cid, c in pairs_blob["companies"].items():
        vts = []
        for tpl, meta in c["templates"].items():
            imgs = media_cache.get(f"{cid}:{tpl}") or []
            entries = []
            for im in imgs:
                iid = im.get("image_id")
                if iid is None:
                    continue
                e = {"image_id": iid, "url": im["url"],
                     "caption_vi": im.get("caption_vi"),
                     "caption_en": im.get("caption_en")}
                f = dl_results.get(iid)
                if f:
                    e["file"] = "vehicle_images/" + Path(f).name
                entries.append(e)
            vts.append({"seat_template_id": meta["seat_template_id"],
                        "label": meta["label"],
                        "total_seats": meta["total_seats"],
                        "n_images": len(entries), "images": entries})
            n_vehicle_types += 1
        if vts or c["cover"]:
            companies[str(cid)] = {
                "id": c["id"], "name": c["name"], "cover_url": c["cover"],
                "logo_url": c["cover"],      # same asset on VeXeRe cards
                "brand_slug": brand_by_name.get(norm_name(c["name"])),
                "n_vehicle_types": len(vts), "vehicle_types": vts}

    manifest = {
        "meta": {
            "generated_at": now_iso(),
            "source": "vexere.com",
            "media_api": "GET https://api.vexere.com/v3/company/"
                         "{company_id}/media?resource_id={seat_template_id}",
            "search_dates": DATES,
            "n_companies": len(companies),
            "n_vehicle_types": n_vehicle_types,
            "n_images": len(images),
            "n_downloaded": sum(1 for v in dl_results.values() if v),
            "notes": "seat_template_id identifies one vehicle type of the "
                     "company; images are captioned exterior/interior/"
                     "amenity photos of that vehicle. 'brand_slug' joins to "
                     "booking_seed.sqlite.brand. 'label' matches "
                     "bus_layout.name / the Excel 'Vehicle Type' column.",
        },
        "companies": companies,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def build_gallery(manifest: dict):
    parts = ["""<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8">
<title>VeXeRe vehicle-type pictures</title><style>
body{font-family:system-ui,sans-serif;margin:24px;background:#f7f7f9}
h1{font-size:22px} h2{font-size:18px;margin:28px 0 4px} h3{font-size:15px;
margin:16px 0 8px;color:#444}
.card{display:inline-block;margin:6px;vertical-align:top;background:#fff;
border-radius:8px;padding:8px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.card img{width:280px;height:168px;object-fit:cover;border-radius:6px;
display:block;background:#eee}
.cap{font-size:12px;color:#555;margin-top:6px;max-width:280px;
line-height:1.35}
.meta{color:#888;font-size:12px} .miss{color:#c00}
</style></head><body><h1>VeXeRe vehicle-type pictures</h1>"""]
    comps = sorted(manifest["companies"].values(),
                   key=lambda c: c["name"].casefold())
    for c in comps:
        if not c["vehicle_types"] and not c["cover"]:
            continue
        slug = f" → <span class='meta'>{c['brand_slug']}</span>" \
            if c.get("brand_slug") else " → <span class='miss'>no brand match</span>"
        parts.append(f"<h2>{c['name']} <span class='meta'>"
                     f"company {c['id']}, {c['n_vehicle_types']} vehicle "
                     f"type(s)</span>{slug}</h2>")
        for vt in c["vehicle_types"]:
            seats = vt.get("total_seats")
            seats_s = f", {seats} seats" if seats else ""
            parts.append(f"<h3>{vt['label'] or '(unlabeled)'}{seats_s} "
                         f"<span class='meta'>(seat_template_id "
                         f"{vt['seat_template_id']}, {vt['n_images']} "
                         f"images)</span></h3>")
            for im in vt["images"]:
                f = im.get("file")
                src = f if f else im.get("url", "")
                cap = im.get("caption_vi") or im.get("caption_en") or ""
                if src:
                    parts.append(
                        f"<div class='card'><img src='{src}' loading='lazy'>"
                        f"<div class='cap'>{cap}</div></div>")
                else:
                    parts.append(
                        f"<div class='card'><div class='miss'>"
                        f"[no image] image_id {im['image_id']}</div>"
                        f"<div class='cap'>{cap}</div></div>")
    parts.append("</body></html>")
    GALLERY.write_text("\n".join(parts))


def update_db(manifest: dict):
    import sqlite3
    if not DB_PATH.exists():
        print("  [db] booking_seed.sqlite not found — skipping logo update")
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    updated = matched = 0
    rows = cur.execute("SELECT slug, name FROM brand").fetchall()
    by_name = {norm_name(n): s for s, n in rows}
    by_slug = {s: n for s, n in rows}
    for cid, c in manifest["companies"].items():
        slug = c.get("brand_slug")
        if slug and c.get("logo_url"):
            cur.execute("UPDATE brand SET logo_url=? WHERE slug=?",
                        (c["logo_url"], slug))
            updated += 1
    for cid, c in manifest["companies"].items():
        if c.get("brand_slug"):
            matched += 1
    con.commit()
    n_logos = cur.execute(
        "SELECT COUNT(*) FROM brand WHERE logo_url IS NOT NULL").fetchone()[0]
    con.close()
    print(f"  [db] brands matched to companies: {matched}/{len(rows)}; "
          f"logo_url set on {updated} brands (total with logo: {n_logos})")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-db-update", action="store_true")
    args = ap.parse_args()

    print("[1/4] Collecting (company, seat_template) inventory via search")
    pairs_blob = collect_pairs()
    print(f"      {len(pairs_blob['companies'])} companies, "
          f"{pairs_blob['n_templates']} vehicle templates, "
          f"{pairs_blob['n_trips']} trips")

    print("[2/4] Fetching media galleries")
    media_cache = collect_media(pairs_blob)
    n_img = sum(len(v or []) for v in media_cache.values())
    print(f"      {len(media_cache)} galleries, {n_img} image entries")

    print("[3/4] Downloading unique images")
    images, dl_results = download_all(pairs_blob, media_cache,
                                      not args.no_download)

    print("[4/4] Building manifest / gallery / DB update")
    manifest = build_manifest(pairs_blob, media_cache, images, dl_results)
    build_gallery(manifest)
    if not args.no_db_update:
        update_db(manifest)
    print(f"\nManifest : {MANIFEST}")
    print(f"Gallery  : {GALLERY}")
    print(f"Images   : {IMG_DIR} "
          f"({sum(1 for v in dl_results.values() if v)} files)")


if __name__ == "__main__":
    main()
