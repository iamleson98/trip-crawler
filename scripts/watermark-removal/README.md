# VeXeRe Watermark Removal — AI Inpainting (CPU-only)

Removes the VeXeRe watermark (yellow logo square + white "vexere" text +
smile) from bus photos using **LaMa** — a real generative inpainting model —
replacing the earlier TELEA approach whose fills looked smudged and blurry.

The result: watermark regions are reconstructed with plausible texture that
matches the surrounding scene (asphalt, seats, bus panels, floors), and every
pixel **outside** the watermark mask stays bit-identical to the original.

```
scripts/watermark-removal/
├── README.md                 ← this file
├── requirements.txt
├── template.png              ← logo template used for detection
├── wmremove/                 ← importable Python package
│   ├── detection.py          ← watermark detection + mask construction
│   ├── inpainting.py         ← LaMa model wrapper
│   └── pipeline.py           ← single-image removal pipeline
└── cli.py                    ← command-line interface
```

---

## How it works

```
original ──► detect watermark ──► build mask ──► crop region ──► LaMa ──► feathered paste
             (template match      (solid rect     (watermark +    (AI fills   (only masked
              + validation)       over full       2.5× logo-h     the hole)    pixels are
                                   watermark)      context)                    touched)
```

1. **Detection** — the yellow logo is found by multi-scale template matching
   (4 %–16 % of the image's long side) restricted to the bottom-left region,
   in both raw BGR and an R−B "yellow-hue" channel that stays discriminative
   over dark backgrounds. A structure validator (solid yellow connected
   block + text-band evidence) rejects false positives such as yellow buses
   and orange seats. Because the watermark geometry is fixed per image size,
   the detected box is cached per `(width, height)` — later images of the
   same size skip the 400 ms search after a 1 ms presence check.

2. **Masking** — a *solid rectangle* covering the whole watermark bounding
   box (+8 px margin). This is the single most important quality decision:
   the watermark is semi-transparent, so pixel-precise masks always leave
   faint remnants (halo, JPEG ghosting) visible around the hole. LaMa treats
   those remnants as *context* and dutifully "completes" the watermark
   inside the hole — you get the logo back, painted more vividly than
   before. A solid rectangle removes every remnant from the model's view and
   matches LaMa's training distribution (random rectangular holes).

3. **Crop-based inference** — only the watermark region plus context padding
   (2.5 × logo height, min 48 px) is sent through the model. A crop is
   ~0.1–0.5 megapixels instead of the full photo: this is what makes CPU
   inference fast (≈1–2 s per image per core) *and* gives the model a small
   canvas on which it reconstructs texture convincingly. Generous context
   also lets LaMa rebuild real objects the watermark overlaps (wheels, seat
   rows) far better.

4. **Feathered paste-back** — the model's fill is blended back with a ~1 px
   soft alpha **only on watermark pixels**. Everything outside the mask is
   untouched: no global re-render, no colour shift, no detail loss. Images
   without a detectable watermark are copied byte-for-byte.

---

## Install (CPU-only)

Python 3.10+ and ~2 GB free disk. No GPU required.

```bash
cd scripts/watermark-removal
python3 -m venv .venv
source .venv/bin/activate

# CPU-only torch wheel (≈200 MB, much smaller than the default CUDA build)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

The LaMa model (big-lama.pt, 196 MB) downloads automatically from GitHub
releases on first use and is cached at `~/.cache/torch/hub/checkpoints/`.

---

## Usage

Run everything from the repository root so relative manifest paths resolve.

### 1. Fetch the original (watermarked) images

The repo's `data/vehicle-type-media/` images are already cleaned, so start
from the source URLs recorded in the manifest:

```bash
python scripts/watermark-removal/cli.py fetch \
    --manifest data/vehicle-type-media/vehicle_type_media.json \
    --output originals
# → 3,753 unique images (~325 MB), resumable
```

### 2. Remove watermarks

```bash
python scripts/watermark-removal/cli.py process \
    --input originals \
    --output cleaned \
    --workers $(nproc)
```

- One worker process per CPU core (each pinned to 1 torch thread and
  1 OpenCV thread — oversubscription otherwise costs 2× throughput).
- **Resumable**: re-running skips images whose output already exists.
- Progress + per-file stats are written to `cleaned/_stats.json`.

### 3. Eyeball the quality

```bash
python scripts/watermark-removal/cli.py compare \
    --input originals --output cleaned --sheets review --samples 24
```

Writes `review/cmp_*.jpg` side-by-side sheets (left = watermarked original,
right = cleaned) around each watermark location.

### 4. Apply the cleaned files to the repo tree

```bash
python scripts/watermark-removal/cli.py sync \
    --processed cleaned \
    --manifest data/vehicle-type-media/vehicle_type_media.json \
    --target data/vehicle-type-media
```

Replaces the content of `vehicles/<brand>/<vehicle-type>/*.jpeg` and
`covers/*.jpeg` in place (same file names, better bytes). `--dry-run`
prints the plan first.

### Other commands

```bash
python scripts/watermark-removal/cli.py stats --output cleaned   # run summary
python scripts/watermark-removal/cli.py process --input originals --output cleaned --limit 20  # quick test
```

### Python API

```python
import sys, cv2
sys.path.insert(0, "scripts/watermark-removal")
from wmremove import WatermarkRemover

remover = WatermarkRemover()          # CPU, 1 thread
img = cv2.imread("photo.jpeg")
clean, info = remover.remove(img)     # info: found / score / logo / bbox
```

---

## Performance (measured on 2 vCPU)

| workers | throughput | 3,753 images | RAM per worker |
|---------|-----------|---------------|----------------|
| 1       | ~17 img/min | ~3.7 h        | ~0.7 GB |
| 2       | ~33 img/min | ~1.9 h        | ~0.7 GB |
| 8       | ~130 img/min | ~29 min      | ~0.7 GB each |

- Model load + warm-up adds ~10 s per worker at start-up.
- `--context-scale 1.6` is ~1.5× faster than the default 2.5 at a small
  quality cost on watermarks that overlap real objects — fine for batch
  previews.
- Memory: ≈0.7 GB per worker; on a machine with N cores and ≥1 GB/core, use
  `--workers N`.

---

## Quality notes

- **Detection rate**: ~93 % of photos carry the watermark and are detected
  (match score median 0.98). The rest either have no watermark or are
  rejected by validation — those files are copied through untouched.
- **Fill quality**: the vast majority of fills are indistinguishable from
  the surrounding texture (asphalt, wood, fabric, tile, metal). Hard cases
  exist where the watermark overlaps complex real geometry (bus wheels,
  flat cartoon-style floor mats) — LaMa rebuilds a *plausible* continuation
  rather than the exact original content. Use the `compare` sheets to spot
  them.
- **What is guaranteed**: outside the watermark rectangle the output is
  pixel-identical to the input; untouched images keep their original bytes;
  cleaned JPEGs are re-encoded once at quality 95, 4:4:4 (no chroma
  subsampling).

---

## Troubleshooting

| symptom | fix |
|---------|-----|
| `FileNotFoundError: logo template` | run from the repo, or pass `--template` |
| model download is slow/blocked | manually place `big-lama.pt` in `~/.cache/torch/hub/checkpoints/` (from the [simple-lama-inpainting releases](https://github.com/enesmsahin/simple-lama-inpainting/releases)) |
| throughput far below the table | ensure `--workers` matches core count; close other CPU-heavy jobs; check `cv2.setNumThreads` isn't overridden |
| a few images look soft/odd | inspect with `compare`; re-run with `--context-scale 3` |
| interrupted run | just re-run the same `process` command — it resumes |

---

## Replaces

`scripts/wmremove.py` + `organize_vehicle_media.py` (TELEA inpainting +
noise blending — the "smudged, unclear" results). This tool supersedes both
for watermark removal; the old scripts remain for history.
