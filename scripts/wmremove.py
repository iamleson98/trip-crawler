#!/usr/bin/env python3
"""vexere watermark removal module.

Watermark anatomy (all measurements relative to logo box L=(x,y,w,h)):
  - solid yellow rounded-square logo with white bus icon   (the template)
  - white lowercase 'vexere' text right of logo:  x+[0.95..5.1]*w, y-0.15h .. y+0.9h
  - small yellow/orange smile curve below text:   x+[3.4..5.1]*w,  y+0.72h .. y+0.95h

Detection: multi-scale template match of the logo (scale 2.5%..16% of max side),
restricted to bottom-left region, + structure validation.
Removal: mask = logo rect + text/smile pixels (color/contrast) + dilation,
then cv2.inpaint TELEA.
"""
import numpy as np
import cv2

LOGO_TEMPLATE = cv2.imread("/home/z/my-project/scripts/wm_crops/logo_template.png")
LOGO_H, LOGO_W = LOGO_TEMPLATE.shape[:2]

def _rb_channel(bgr):
    """R-B difference image: emphasizes yellow hue, invariant to brightness
    (semi-transparent watermark over dark background keeps hue)."""
    return (bgr[..., 2].astype(np.int16) - bgr[..., 0].astype(np.int16)).astype(np.float32)

LOGO_RB = _rb_channel(LOGO_TEMPLATE.astype(np.uint8))

def detect_watermark(img_bgr):
    """Return dict(score, logo=(x,y,w,h), full_bbox=(x0,y0,x1,y1)) or None.
    Tries raw-BGR matching (opaque watermarks) and R-B channel matching
    (semi-transparent watermarks over dark backgrounds)."""
    h, w = img_bgr.shape[:2]
    m = max(w, h)
    # watermark is always bottom-left anchored: x0 <= ~13% of width in all
    # observed ground truths. ROI must CONTAIN the whole logo template, so
    # extend x_hi by the max possible logo width (16% of max side).
    x_hi = min(w, int(w * 0.14) + int(m * 0.165) + 4)
    y_lo = int(h * 0.60)
    if x_hi < 20 or h - y_lo < 20:
        return None
    roi = img_bgr[y_lo:, :x_hi]
    roi_rb = _rb_channel(roi)
    best = (0.0, None)
    # true watermark logos observed at 5.1%-15% of max side; 4% floor kills
    # small-scale false positives (2.5-3% yellow-bus patches)
    for wf in np.arange(0.040, 0.161, 0.0015):
        tw = int(round(m * wf))
        th = int(round(tw * LOGO_H / LOGO_W))
        if tw < 10 or th < 10 or tw >= roi.shape[1] or th >= roi.shape[0]:
            continue
        tmpl = cv2.resize(LOGO_TEMPLATE, (tw, th), interpolation=cv2.INTER_AREA)
        tmpl_rb = cv2.resize(LOGO_RB, (tw, th), interpolation=cv2.INTER_AREA)
        for src, tp in ((roi, tmpl), (roi_rb, tmpl_rb)):
            res = cv2.matchTemplate(src, tp, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best[0]:
                best = (float(mx), (loc[0], loc[1] + y_lo, tw, th))
    score, hit = best
    if hit is None or score < 0.42:
        return None
    x, y, tw, th = hit
    full = (x, max(0, y - int(0.18 * th)), min(w, x + int(5.6 * tw)), min(h, y + int(1.05 * th)))
    return dict(score=score, logo=(x, y, tw, th), full_bbox=full)

def _validate(img_bgr, det):
    """Structure validation: solid yellow square logo + text evidence right."""
    x, y, tw, th = det["logo"]
    h, w = img_bgr.shape[:2]
    # logo yellow fraction + solid-square connected-component check
    lx0, ly0 = x, y
    lx1, ly1 = min(w, x + tw), min(h, y + th)
    if lx1 - lx0 < 5 or ly1 - ly0 < 5:
        return False
    logo = img_bgr[ly0:ly1, lx0:lx1].astype(np.int16)
    r, g, b = logo[..., 2], logo[..., 1], logo[..., 0]
    ymask = (((r - b) > 70) & (r > 130) & (g > 60) & (r >= g))
    yfrac = float(ymask.mean())
    # yfrac cap 0.90 rejects yellow-bus false positives, but REAL watermarks
    # over yellow bus backgrounds read fully yellow (icon is semi-transparent
    # white over yellow) — accept those when the template score is very high.
    ycap = 1.01 if det["score"] > 0.93 else 0.90
    if not (0.25 <= yfrac <= ycap):
        return False
    # largest yellow component must be a solid block covering most of the box
    from scipy import ndimage as ndi
    lbl, n = ndi.label(ymask)
    if n == 0:
        return False
    sizes = ndi.sum(ymask, lbl, range(1, n + 1))
    big = int(np.argmax(sizes)) + 1
    if sizes[big - 1] < 0.30 * ymask.size:
        return False
    ys, xs = np.nonzero(lbl == big)
    if (xs.max() - xs.min() + 1) < 0.75 * (lx1 - lx0) or (ys.max() - ys.min() + 1) < 0.75 * (ly1 - ly0):
        return False
    # text band: bright-ish pixels (white text) — compare against band median
    tx0 = min(w, x + int(1.0 * tw))
    tx1 = min(w, x + int(5.5 * tw))
    ty0 = max(0, y - int(0.15 * th))
    ty1 = min(h, y + int(0.85 * th))
    if tx1 - tx0 < 8:
        return False
    band = img_bgr[ty0:ty1, tx0:tx1]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.int16)
    med = float(np.median(gray))
    bright = float((gray > max(med + 18, 200)).mean())
    yellowish = (band[..., 2].astype(np.int16) - band[..., 0].astype(np.int16) > 70).mean()
    det["logo_yfrac"] = round(yfrac, 3)
    det["text_bright"] = round(bright, 3)
    det["smile_y"] = round(float(yellowish), 3)
    # accept if there is text/smile evidence, OR the logo match itself is
    # high-confidence (handles watermark over bright/white sky where the white
    # text is nearly invisible)
    return (bright > 0.02 or yellowish > 0.02 or det["score"] > 0.70)

def build_mask(img_bgr, det):
    """Watermark pixel mask (uint8 255=remove)."""
    h, w = img_bgr.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    x, y, tw, th = det["logo"]
    # 1. logo rect
    mask[y:y + th, x:x + tw] = 255
    # 2. text + smile band: color/contrast detection
    tx0, ty0 = x + int(0.95 * tw), max(0, y - int(0.16 * th))
    tx1, ty1 = min(w, x + int(5.55 * tw)), min(h, y + int(1.0 * th))
    if tx1 > tx0 and ty1 > ty0:
        band = img_bgr[ty0:ty1, tx0:tx1].astype(np.int16)
        r, g, b = band[..., 2], band[..., 1], band[..., 0]
        gray = (r + g + b) / 3.0
        # white-ish text pixels (also catches bright semi-transparent text)
        whiteish = (r > 170) & (g > 170) & (b > 170) & (np.abs(r - b) < 45)
        # strong yellow/orange (smile) — relaxed for semi-transparent instances
        smile = ((r - b) > 50) & (r > 120) & (g > 60) & (r >= g) & (b < g)
        # local contrast: brighter than neighborhood median (text over mid-tone bg)
        k = max(9, (th // 4) * 2 + 1)
        bgmed = cv2.medianBlur((gray.astype(np.uint8)), k).astype(np.int16)
        contrast = gray > (bgmed + 22)
        # darker than bg (text shadow / antialias)
        darker = gray < (bgmed - 25)
        m = (whiteish | smile | contrast | darker).astype(np.uint8) * 255
        # avoid re-inpainting big background areas: contrast/dark masks restricted to text zone
        mask[ty0:ty1, tx0:tx1] = np.maximum(mask[ty0:ty1, tx0:tx1], m)
    # 3. dilate to cover anti-aliasing edges
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    # limit mask to full bbox + small pad
    x0, y0, x1, y1 = det["full_bbox"]
    pad = 3
    keep = np.zeros((h, w), np.uint8)
    keep[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)] = 255
    mask = cv2.bitwise_and(mask, keep)
    return mask

def _texture_blend(img_bgr, mask, inpainted):
    """Blend matched high-frequency noise into the inpainted region so the
    fill matches surrounding texture grain (hides the TELEA smooth smudge)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hf = np.abs(gray - cv2.medianBlur(gray, 5).astype(np.float32))
    ring = cv2.dilate(mask, np.ones((21, 21), np.uint8)) - mask
    ys, xs = np.nonzero(ring > 0)
    if len(ys) == 0:
        return inpainted
    sigma = float(hf[ys, xs].std())
    mu = float(hf[ys, xs].mean())
    out = inpainted.astype(np.float32)
    m = (mask > 0)
    rng = np.random.default_rng(1234)
    noise = rng.normal(mu * 0.3, max(2.0, sigma * 0.6), size=out.shape)
    out[m] = np.clip(out[m] + noise[m], 0, 255)
    return out.astype(np.uint8)

def remove_watermark(img_bgr, min_score=0.50):
    """Main entry. Returns (result_image, info_dict). Best method (VLM-validated):
    TELEA r5 + texture-noise blend."""
    det = detect_watermark(img_bgr)
    info = dict(found=False)
    if det is None:
        return img_bgr, info
    info["score"] = round(det["score"], 3)
    if not _validate(img_bgr, det):
        info["rejected_by"] = "structure"
        return img_bgr, info
    info.update(found=True, logo=det["logo"], full_bbox=det["full_bbox"],
                logo_yfrac=det.get("logo_yfrac"), text_bright=det.get("text_bright"))
    mask = build_mask(img_bgr, det)
    info["mask_px"] = int((mask > 0).sum())
    inp = cv2.inpaint(img_bgr, mask, 5, cv2.INPAINT_TELEA)
    out = _texture_blend(img_bgr, mask, inp)
    return out, info

def remove_watermark_file(src_path, dst_path, jpeg_quality=92):
    img = cv2.imread(src_path)
    if img is None:
        return dict(found=False, error="unreadable")
    out, info = remove_watermark(img)
    cv2.imwrite(dst_path, out, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return info

if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        img = cv2.imread(f"/home/z/my-project/download/vehicle_images/{f}")
        out, info = remove_watermark(img)
        x0, y0, x1, y1 = info.get("full_bbox", (0, 0, 0, 0))
        print(f"{f}: {info}")
        if info.get("found"):
            cv2.imwrite(f"/home/z/my-project/scripts/wm_crops/after_{f}", out)
