#!/usr/bin/env python3
"""Organize vehicle-type images by brand / vehicle type + remove vexere watermark.

Layout produced under --out (default download/vehicle-type-media/):
    vehicles/<brand-slug>/<vtype-slug>/<seq:02d>-<tags>.<ext>
    covers/<brand-slug>.<ext>          (brand cover image, watermark-removed)
    manifest.json                      (written by finalize step)
    gallery.html                       (written by finalize step)

Resumable: existing output files are skipped unless --force.
Stops gracefully after --max-seconds budget.
"""
import argparse
import json
import multiprocessing as mp
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, "/home/z/my-project/scripts")
from wmremove import remove_watermark  # noqa: E402

SRC = "/home/z/my-project/download"
IMG_SRC = "/home/z/my-project/download/vehicle_images"

def fold(s):
    """ASCII-fold Vietnamese text -> dashed slug."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("đ", "d").replace("Đ", "D")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)

# caption phrase -> file tag (order of list = scan order)
CAPTION_PATTERNS = [
    (r"mặt\s*trước", "mat-truoc"),
    (r"mặt\s*sau", "mat-sau"),
    (r"bên\s*hông", "ben-hong"),
    (r"tiện\s*ích", "tien-ich"),
    (r"nội\s*thất", "noi-that"),
    (r"giường\s*nằm", "giuong-nam"),
    (r"ghế\s*ngồi", "ghe-ngoi"),
    (r"limousine", "limousine"),
]

def caption_tags(caption):
    if not caption:
        return "photo"
    text = unicodedata.normalize("NFD", caption.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tags = []
    for pat, tag in CAPTION_PATTERNS:
        if re.search(pat, text) and tag not in tags:
            tags.append(tag)
    if not tags:
        return "photo"
    return "-".join(tags[:3])[:44]

def build_plan():
    """Deterministic processing plan: list of (src, dst, wm_info_key)."""
    manifest = json.load(open(f"{SRC}/vehicle_type_media.json"))
    comps = manifest["companies"]
    # unique brand slugs
    used = {}
    brand_dir = {}
    for cid in sorted(comps, key=lambda k: int(k)):
        c = comps[cid]
        base = c.get("brand_slug") or fold(c["name"])
        if not base:
            base = f"brand-{cid}"
        if base in used:
            brand_dir[cid] = f"{base}-{cid}"
        else:
            brand_dir[cid] = base
        used.setdefault(brand_dir[cid], cid)
    # cover target per company
    cover_src = {}
    for cid, c in comps.items():
        cv = c.get("cover_url") or c.get("logo_url")
        if not cv:
            continue
        found = None
        for vt in c.get("vehicle_types", []):
            for im in vt.get("images", []):
                if im.get("url") == cv:
                    found = im
                    break
            if found:
                break
        if found:
            cover_src[cid] = ("gallery", found["image_id"], cv)
        else:
            cover_src[cid] = ("url", None, cv)
    # vehicle types
    tasks = []
    for cid in sorted(comps, key=lambda k: int(k)):
        c = comps[cid]
        seen_labels = {}
        vtypes = c.get("vehicle_types", []) or []
        # sort templates by seat_template_id for determinism
        for vt in sorted(vtypes, key=lambda v: v.get("seat_template_id") or 0):
            label = vt.get("label") or f"xe-{vt.get('seat_template_id')}"
            vslug = fold(label)
            if not vslug:
                vslug = f"xe-{vt.get('seat_template_id')}"
            if vslug in seen_labels:
                vslug = f"{vslug}-t{vt.get('seat_template_id')}"
            seen_labels[vslug] = True
            for i, im in enumerate(vt.get("images", []) or [], 1):
                ext = os.path.splitext(im.get("url", ""))[1].lstrip(".") or "jpeg"
                if ext.lower() == "jpg":
                    ext = "jpeg"
                src = os.path.join(IMG_SRC, f"{im['image_id']}.{ext}")
                if not os.path.exists(src):
                    # tolerate id/ext mismatches
                    alt = os.path.join(IMG_SRC, f"{im['image_id']}.jpeg")
                    src = alt if os.path.exists(alt) else None
                fname = f"{i:02d}-{caption_tags(im.get('caption_vi'))}.{ext}"
                dst = os.path.join("vehicles", brand_dir[cid], vslug, fname)
                tasks.append(dict(src=src, dst=dst, image_id=im["image_id"],
                                  company=cid, vtype=vt.get("seat_template_id")))
    # cover tasks (reuse cleaned gallery file when possible; handled at finalize)
    return dict(tasks=tasks, brand_dir=brand_dir, cover_src=cover_src,
                companies=comps)

def process_one(task):
    src, dst = task["src"], task["dst"]
    out_abs = os.path.join(OUT_DIR, dst)
    if os.path.exists(out_abs) and not FORCE:
        return (dst, "skipped")
    if src is None or not os.path.exists(src):
        return (dst, "missing_src")
    import cv2
    img = cv2.imread(src)
    if img is None:
        return (dst, "unreadable")
    out, info = remove_watermark(img)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    ext = os.path.splitext(dst)[1].lower()
    if ext in (".png",):
        cv2.imwrite(out_abs, out)
    else:
        cv2.imwrite(out_abs, out, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return (dst, "done" if info.get("found") else "nowm")

def init_worker(out_dir, force):
    global OUT_DIR, FORCE
    OUT_DIR = out_dir
    FORCE = force

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/home/z/my-project/download/vehicle-type-media")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-seconds", type=float, default=240)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--plan-cache", default="/home/z/my-project/scripts/vm_plan.json")
    args = ap.parse_args()

    t_start = time.time()
    plan = build_plan()
    json.dump({k: plan[k] for k in ("tasks", "brand_dir", "cover_src")},
              open(args.plan_cache, "w"))
    print(f"plan: {len(plan['tasks'])} images, {len(plan['brand_dir'])} brands, "
          f"{len(plan['cover_src'])} covers")

    os.makedirs(args.out, exist_ok=True)
    pending = [t for t in plan["tasks"]]
    results = {"done": 0, "skipped": 0, "nowm": 0, "missing_src": 0, "unreadable": 0}
    with mp.Pool(args.workers, initializer=init_worker,
                 initargs=(args.out, args.force)) as pool:
        for dst, status in pool.imap_unordered(process_one, pending, chunksize=16):
            results[status] = results.get(status, 0) + 1
            if results["done"] % 200 == 0 and status == "done":
                print(f"  progress: {sum(results.values())}/{len(pending)}  {results}", flush=True)
            if time.time() - t_start > args.max_seconds:
                pool.terminate()
                print("budget reached, stopping (rerun to resume)")
                break
    print("final:", results)

if __name__ == "__main__":
    main()
