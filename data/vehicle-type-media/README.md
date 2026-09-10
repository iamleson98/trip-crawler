# vehicle-type-media — VeXeRe vehicle pictures (watermark removed)

Vehicle-type photo galleries per bus operator, collected from
`api.vexere.com/v3/company/{company_id}/media?resource_id={seat_template_id}`,
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
- file names: `{NN}-<tags>` where NN is the image index inside
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

Stats: **303** companies · **502** vehicle types ·
**4074** pictures · **296** covers.

## Watermark removal

Two generations of removal tooling live in the history of this dataset:

1. **TELEA + noise blending (superseded)** — the first pass removed the
   watermark with OpenCV TELEA inpainting plus local-texture-matched noise.
   The fills looked smudged and slightly unclear where the watermark sat.
2. **LaMa AI inpainting (current)** —
   [`scripts/watermark-removal/`](../../scripts/watermark-removal/) detects
   the watermark (multi-scale template matching on raw BGR and an R−B
   yellow-hue channel — the latter catches semi-transparent instances over
   dark or yellow backgrounds), masks the **full watermark bounding box as a
   solid rectangle** (critical: faint semi-transparent remnants left outside
   a pixel-precise mask make LaMa "complete" the watermark back), and fills
   it with [big-lama](https://github.com/advimman/lama) — a generative
   inpainting model — on a padded crop, pasting back only the masked pixels
   with a 1 px feather. Everything outside the watermark stays bit-identical.

Re-generate this whole tree from the source URLs with:

```bash
python scripts/watermark-removal/cli.py fetch   --manifest data/vehicle-type-media/vehicle_type_media.json --output originals
python scripts/watermark-removal/cli.py process --input originals --output cleaned --workers $(nproc)
python scripts/watermark-removal/cli.py sync    --processed cleaned --manifest data/vehicle-type-media/vehicle_type_media.json --target data/vehicle-type-media
```

See [`scripts/watermark-removal/README.md`](../../scripts/watermark-removal/)
for the full CPU-only guide (install, quality notes, troubleshooting).

Historical scripts: `scripts/fetch_vehicle_media.py` (collection),
`scripts/wmremove.py` (TELEA-based removal, superseded),
`scripts/organize_vehicle_media.py` + `scripts/finalize_vehicle_media.py`
(organization/manifest/gallery).
