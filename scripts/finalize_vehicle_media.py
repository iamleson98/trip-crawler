#!/usr/bin/env python3
"""Finalize the organized vehicle-type media tree.

1. covers/<brand-slug>.<ext>  — brand cover image (reuse cleaned gallery file,
   or download from CDN when the cover is not part of the gallery)
2. vehicle_type_media.json    — updated manifest with new relative paths,
   is_cover flags, cover_file per company
3. gallery.html               — review gallery grouped by brand / vehicle type
4. README.md                  — folder documentation
"""
import json
import os
import shutil
import sys
import urllib.request

BASE = "/home/z/my-project/download/vehicle-type-media"
SRC = "/home/z/my-project/download"
PLAN = json.load(open("/home/z/my-project/scripts/vm_plan.json"))
BRAND_DIR = PLAN["brand_dir"]
COVER_SRC = PLAN["cover_src"]
COMPANIES = json.load(open(f"{SRC}/vehicle_type_media.json"))["companies"]

sys.path.insert(0, "/home/z/my-project/scripts")
from organize_vehicle_media import fold  # noqa: E402

def download(url, dst):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)

def norm_url(u):
    if not u:
        return u
    u = u.strip()
    if u.startswith("https:////"):
        u = "https://" + u[len("https:////"):]
    return u

def main():
    import cv2
    from wmremove import remove_watermark

    # ---------- 1. covers ----------
    n_copy = n_dl = n_skip = 0
    id2task = {t["image_id"]: t for t in PLAN["tasks"]}
    cover_file = {}
    for cid, (kind, image_id, url) in COVER_SRC.items():
        brand = BRAND_DIR[cid]
        if kind == "gallery":
            task = id2task.get(image_id)
            if task is None:
                n_skip += 1
                continue
            src = os.path.join(BASE, task["dst"])
            if not os.path.exists(src):
                base_noext = os.path.splitext(src)[0]
                alt = base_noext + (".jpeg" if src.endswith(".png") else ".png")
                if os.path.exists(alt):
                    src = alt
                else:
                    n_skip += 1
                    continue
            ext = os.path.splitext(src)[1]
            dst = os.path.join(BASE, "covers", f"{brand}{ext}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            cover_file[cid] = os.path.relpath(dst, BASE)
            n_copy += 1
        else:
            ext = os.path.splitext(url)[1].lstrip(".") or "jpeg"
            if ext == "jpg":
                ext = "jpeg"
            dst = os.path.join(BASE, "covers", f"{brand}.{ext}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                download(url, dst)
                img = cv2.imread(dst)
                if img is not None:
                    out, _ = remove_watermark(img)
                    cv2.imwrite(dst, out, [cv2.IMWRITE_JPEG_QUALITY, 92])
                cover_file[cid] = os.path.relpath(dst, BASE)
                n_dl += 1
            except Exception as e:
                print(f"cover download failed {cid} {url}: {e}")
                n_skip += 1
    print(f"covers: reused {n_copy}, downloaded {n_dl}, skipped {n_skip}")

    # ---------- 2. manifest ----------
    # map image_id -> new file path (with extension fallback)
    id2file = {}
    for t in PLAN["tasks"]:
        p = os.path.join(BASE, t["dst"])
        if os.path.exists(p):
            id2file[t["image_id"]] = t["dst"]
        else:
            alt = os.path.splitext(t["dst"])[0] + (".png" if t["dst"].endswith((".jpeg", ".jpg")) else ".jpeg")
            if os.path.exists(os.path.join(BASE, alt)):
                id2file[t["image_id"]] = alt
    # cover url per company (to mark is_cover)
    cover_url = {}
    for cid, (kind, image_id, url) in COVER_SRC.items():
        cover_url[cid] = url
    out_comps = {}
    n_missing = 0
    for cid in sorted(COMPANIES, key=lambda k: int(k)):
        c = COMPANIES[cid]
        seen = {}
        vt_out = []
        for vt in sorted(c.get("vehicle_types", []) or [],
                         key=lambda v: v.get("seat_template_id") or 0):
            label = vt.get("label") or f"xe-{vt.get('seat_template_id')}"
            vslug = fold(label) or f"xe-{vt.get('seat_template_id')}"
            if vslug in seen:
                vslug = f"{vslug}-t{vt.get('seat_template_id')}"
            seen[vslug] = True
            # directory on disk (may differ in file extension only)
            vdir = os.path.join("vehicles", BRAND_DIR[cid], vslug)
            imgs_out = []
            for i, im in enumerate(vt.get("images", []) or [], 1):
                newf = id2file.get(im["image_id"])
                if newf is None:
                    n_missing += 1
                    continue
                entry = {
                    "image_id": im["image_id"],
                    "url": im.get("url"),
                    "caption_vi": im.get("caption_vi"),
                    "caption_en": im.get("caption_en"),
                    "file": newf,
                }
                if cover_url.get(cid) and im.get("url") == cover_url[cid]:
                    entry["is_cover"] = True
                imgs_out.append(entry)
            if imgs_out:
                vt_out.append({
                    "seat_template_id": vt.get("seat_template_id"),
                    "label": label,
                    "total_seats": vt.get("total_seats"),
                    "dir": vdir,
                    "images": imgs_out,
                })
        out_c = {
            "id": c["id"],
            "name": c["name"],
            "cover_url": c.get("cover_url"),
            "logo_url": c.get("logo_url"),
            "brand_slug": BRAND_DIR[cid],
            "n_vehicle_types": len(vt_out),
            "vehicle_types": vt_out,
        }
        if cid in cover_file:
            out_c["cover_file"] = cover_file[cid]
        out_comps[cid] = out_c
    n_imgs = sum(len(v["images"]) for c in out_comps.values() for v in c["vehicle_types"])
    manifest = {
        "meta": {
            "generated_at": "2026-09-09",
            "source": "vexere.com",
            "media_api": "GET https://api.vexere.com/v3/company/{company_id}/media?resource_id={seat_template_id}",
            "organization": "vehicles/<brand-slug>/<vehicle-type-slug>/<seq>-<tags>.<ext>; covers/<brand-slug>.<ext>",
            "watermark_removal": "vexere logo/text/smile detected via multi-scale template matching "
                                 "(raw-BGR + R-B hue channel) and removed via masked TELEA inpainting "
                                 "+ local-texture-matched noise blending",
            "n_companies": len(out_comps),
            "n_vehicle_types": sum(c["n_vehicle_types"] for c in out_comps.values()),
            "n_images": n_imgs,
            "n_covers": len(cover_file),
        },
        "companies": out_comps,
    }
    with open(os.path.join(BASE, "vehicle_type_media.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"manifest: {len(out_comps)} companies, {n_imgs} images, "
          f"{len(cover_file)} covers, {n_missing} missing images skipped")

    # ---------- 3. gallery.html ----------
    html = ["<!doctype html><html><head><meta charset='utf-8'>",
            "<title>VeXeRe vehicle-type pictures (watermark removed)</title>",
            "<style>body{font-family:system-ui,sans-serif;background:#f5f5f7;margin:0;padding:24px}"
            "h1{font-size:22px}h2{font-size:18px;margin-top:28px}h3{font-size:14px;margin:14px 0 6px;color:#444}"
            ".co{background:#fff;border-radius:10px;padding:16px;margin:14px 0}"
            "img{height:120px;margin:3px;border-radius:6px;background:#eee}"
            ".tag{display:inline-block;font-size:11px;background:#eef;padding:1px 6px;border-radius:8px;margin-left:6px}"
            "</style></head><body>",
            "<h1>VeXeRe vehicle-type pictures — watermark removed</h1>",
            f"<p>{len(out_comps)} companies · {sum(c['n_vehicle_types'] for c in out_comps.values())} vehicle types · "
            f"{n_imgs} pictures · {len(cover_file)} covers. Watermarks removed before upload.</p>"]
    for cid in sorted(out_comps, key=lambda k: out_comps[k]["name"].lower()):
        c = out_comps[cid]
        html.append(f"<div class='co'><h2>{c['name']} <span class='tag'>{c['brand_slug']}</span></h2>")
        if c.get("cover_file"):
            html.append(f"<img src='{c['cover_file']}' title='cover' loading='lazy' "
                        f"onerror=\"this.src='{norm_url(c.get('cover_url'))}'\">")
        for vt in c["vehicle_types"]:
            html.append(f"<h3>{vt['label']} <span class='tag'>{vt['dir']}</span></h3>")
            for im in vt["images"]:
                cap = (im.get("caption_vi") or "").replace("<", "&lt;").replace("'", "&#39;")[:80]
                src = norm_url(im.get("url"))
                html.append(f"<img src='{im['file']}' title='{cap}' loading='lazy' "
                            f"onerror=\"this.onerror=null;this.src='{src}'\">")
        html.append("</div>")
    html.append("</body></html>")
    with open(os.path.join(BASE, "gallery.html"), "w") as f:
        f.write("\n".join(html))
    print("gallery.html written")

    # ---------- 4. README.md ----------
    n_vt = sum(c["n_vehicle_types"] for c in out_comps.values())
    readme = f"""# vehicle-type-media — VeXeRe vehicle pictures (watermark removed)

Vehicle-type photo galleries per bus operator, collected from
`api.vexere.com/v3/company/{{company_id}}/media?resource_id={{seat_template_id}}`,
with the burned-in **vexere watermark removed** (logo + "vexere" text + smile),
organized **by brand → vehicle type** with human-readable folder and file names.

## Layout

```
vehicle-type-media/
  vehicles/<brand-slug>/<vehicle-type-slug>/<NN>-<tags>.<jpeg|png>
  covers/<brand-slug>.<jpeg|png>        # brand cover/logo image (cleaned too)
  vehicle_type_media.json               # manifest: company -> vehicle types -> images
  gallery.html                          # offline review gallery
```

- `<brand-slug>`: ASCII-folded operator name, e.g. `tan-aba`, `giuong-nam-46-cho`
- `<vehicle-type-slug>`: vehicle type label, e.g. `giuong-nam-40-cho`,
  `limousine-24-phong` (suffixed `-t<seat_template_id>` when a company has two
  templates with the same label)
- file names: `{chr(123)}NN{chr(125)}-<tags>` where NN is the image index inside
  the vehicle type and tags derive from the vexere caption
  (`mat-truoc` front, `mat-sau` rear, `ben-hong` side, `noi-that` interior,
  `tien-ich` amenities, …); `photo` when the caption has no known tag.
- PNG sources keep the `.png` extension (17 images); everything else is JPEG
  re-encoded at quality 92.

## Manifest

`vehicle_type_media.json` maps every image to its new path and keeps the
original metadata: `image_id`, source `url`, `caption_vi` / `caption_en`,
plus `cover_file` per company and `is_cover` on the image that doubles as the
company cover.

Stats: **{len(out_comps)}** companies · **{n_vt}** vehicle types ·
**{n_imgs}** pictures · **{len(cover_file)}** covers.

## Watermark removal

The vexere watermark (yellow rounded-square logo with bus icon + white
"vexere" text + orange smile, bottom-left) was detected per image with
multi-scale template matching on both the raw BGR image and an R−B
(yellow-hue) channel — the latter catches semi-transparent instances over dark
or yellow backgrounds — then removed by masked inpainting (OpenCV TELEA) with
local-texture-matched noise blending so the filled area matches surrounding
grain. Images without a watermark (~10%) were left untouched.

Source scripts: `scripts/fetch_vehicle_media.py` (collection),
`scripts/wmremove.py` (watermark detection/removal),
`scripts/organize_vehicle_media.py` + `scripts/finalize_vehicle_media.py`
(organization/manifest/gallery).
"""
    with open(os.path.join(BASE, "README.md"), "w") as f:
        f.write(readme)
    print("README.md written")

if __name__ == "__main__":
    main()
