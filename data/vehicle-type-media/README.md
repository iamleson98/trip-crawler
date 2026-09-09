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
