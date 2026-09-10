#!/usr/bin/env python3
"""CLI for the VeXeRe watermark removal tool.

Subcommands
-----------
fetch     Download the original (watermarked) images listed in a manifest.
process   Remove watermarks with LaMa inpainting (CPU, resumable, parallel).
compare   Build before/after inspection sheets for a processed run.
sync      Copy processed images into the repo media tree (in-place update).
stats     Print a summary of a processed run.

Typical end-to-end flow (from the repo root)::

    python scripts/watermark-removal/cli.py fetch \
        --manifest data/vehicle-type-media/vehicle_type_media.json \
        --output originals

    python scripts/watermark-removal/cli.py process \
        --input originals --output cleaned

    python scripts/watermark-removal/cli.py compare \
        --input originals --output cleaned --sheets review

    python scripts/watermark-removal/cli.py sync \
        --processed cleaned \
        --manifest data/vehicle-type-media/vehicle_type_media.json \
        --target data/vehicle-type-media
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import multiprocessing as mp
import os
import pathlib
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

IMAGE_EXTS = (".jpeg", ".jpg", ".png", ".webp")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# process (multiprocessing) — worker state
# ---------------------------------------------------------------------------
_REMOVER = None
_INIT_ERROR = None


def _init_worker(template: str | None, context_scale: float) -> None:
    """Pool initializer: build one WatermarkRemover per worker process.

    Errors are captured instead of raised — a raising initializer would kill
    the worker and hang the pool silently.
    """
    global _REMOVER, _INIT_ERROR
    try:
        import cv2

        cv2.setNumThreads(1)  # one worker per core: no oversubscription
        from wmremove import WatermarkRemover

        _REMOVER = WatermarkRemover(template_path=template, threads=1,
                                    context_scale=context_scale)
    except Exception as exc:  # noqa: BLE001
        _INIT_ERROR = f"worker init failed: {exc}"


def _process_one(job: tuple[str, str]) -> tuple[str, dict, str | None]:
    if _REMOVER is None:
        return job[1], None, _INIT_ERROR or "worker not initialised"
    src, dst = job
    tmp = dst + ".part"
    try:
        info = _REMOVER.process_file(src, tmp)
        os.replace(tmp, dst)
        return dst, info, None
    except Exception as exc:  # noqa: BLE001 — worker must never crash the pool
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        return dst, None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
def _url_name(url: str) -> str:
    """Deterministic local filename for a source URL."""
    path = urlparse(url).path.lower()
    ext = next((e for e in IMAGE_EXTS if path.endswith(e)), ".jpeg")
    if ext == ".jpg":
        ext = ".jpeg"
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ext


def _collect_urls(manifest_path: pathlib.Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text())
    urls: list[str] = []
    for comp in manifest["companies"].values():
        for key in ("cover_url", "logo_url"):
            u = comp.get(key)
            if u:
                urls.append(u)
        for vt in comp.get("vehicle_types", []):
            for img in vt.get("images", []):
                urls.append(img["url"])
    return list(dict.fromkeys(urls))  # dedupe, preserve order


def _download(url: str, out_dir: pathlib.Path, retries: int = 3) -> tuple[str, str | None, int]:
    dest = out_dir / _url_name(url)
    if dest.exists() and dest.stat().st_size > 500:
        return url, str(dest), 0
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Referer": "https://vexere.com/"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) < 500:
                raise IOError(f"suspiciously small ({len(data)} bytes)")
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return url, str(dest), len(data)
        except Exception:
            if attempt == retries - 1:
                return url, None, 0
            time.sleep(1.5 * (attempt + 1))
    return url, None, 0  # unreachable


def cmd_fetch(args: argparse.Namespace) -> int:
    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = _collect_urls(pathlib.Path(args.manifest))
    print(f"unique source URLs: {len(urls)}")

    index: dict[str, str] = {}
    failures: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_download, u, out_dir): u for u in urls}
        for i, fut in enumerate(as_completed(futs), 1):
            url, path, size = fut.result()
            if path:
                index[url] = pathlib.Path(path).name
                done += 1
            else:
                failures.append({"url": url})
            if i % 200 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)} downloaded={done} failed={len(failures)}",
                      flush=True)
    (out_dir / "_index.json").write_text(json.dumps(index, indent=1))
    if failures:
        (out_dir / "_failures.json").write_text(json.dumps(failures, indent=1))
    print(f"fetch complete: {done} files in {out_dir} "
          f"({len(failures)} failures logged to _failures.json)")
    return 1 if failures and not done else 0


# ---------------------------------------------------------------------------
# process
# ---------------------------------------------------------------------------
def cmd_process(args: argparse.Namespace) -> int:
    in_dir = pathlib.Path(args.input)
    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for ext in IMAGE_EXTS:
        files += [f for f in glob.glob(str(in_dir / f"*{ext}"))]
    files = sorted(files)
    if args.limit:
        files = files[: args.limit]
    jobs = [(f, str(out_dir / pathlib.Path(f).name))
            for f in files
            if not os.path.exists(str(out_dir / pathlib.Path(f).name))]
    print(f"images: {len(files)}  todo (resumable): {len(jobs)}")
    if not jobs:
        return 0

    stats_path = out_dir / "_stats.json"
    stats: dict[str, dict] = {}
    if stats_path.exists():
        stats = json.loads(stats_path.read_text())

    t0 = time.time()
    n_ok = n_wm = n_fail = 0
    template = str(pathlib.Path(args.template)) if args.template else None
    with mp.Pool(processes=args.workers, initializer=_init_worker,
                 initargs=(template, args.context_scale)) as pool:
        try:
            for i, (dst, info, err) in enumerate(
                    pool.imap_unordered(_process_one, jobs, chunksize=4), 1):
                name = pathlib.Path(dst).name
                if err is not None:
                    n_fail += 1
                    stats[name] = {"error": err}
                    print(f"FAIL {name}: {err}", flush=True)
                else:
                    n_ok += 1
                    stats[name] = info
                    n_wm += bool(info.get("found"))
                if i % 100 == 0 or i == len(jobs):
                    dt = time.time() - t0
                    eta = dt / i * (len(jobs) - i)
                    rate = i / dt * 60
                    print(f"  {i}/{len(jobs)} ok={n_ok} watermark-removed={n_wm} "
                          f"fail={n_fail} | {rate:.0f} img/min, "
                          f"eta {eta/60:.1f} min", flush=True)
                    tmp = stats_path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(stats))
                    tmp.replace(stats_path)
        except KeyboardInterrupt:
            print("interrupted — partial results kept; re-run to resume")
            pool.terminate()
    stats_path.write_text(json.dumps(stats))
    print(f"process complete: {n_ok} done ({n_wm} cleaned, {n_ok - n_wm} untouched), "
          f"{n_fail} failed, {(time.time()-t0)/60:.1f} min")
    print(f"stats: {stats_path}")
    return 1 if n_fail else 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def cmd_compare(args: argparse.Namespace) -> int:
    import cv2
    import numpy as np

    in_dir, out_dir = pathlib.Path(args.input), pathlib.Path(args.output)
    sheets = pathlib.Path(args.sheets)
    sheets.mkdir(parents=True, exist_ok=True)
    stats = json.loads((out_dir / "_stats.json").read_text())
    cleaned = [n for n, s in stats.items()
               if s.get("found") and not n.startswith("_")]
    random.Random(args.seed).shuffle(cleaned)
    cleaned = cleaned[: args.samples]

    for name in cleaned:
        src = in_dir / name
        dst = out_dir / name
        if not src.exists() or not dst.exists():
            continue
        before = cv2.imread(str(src))
        after = cv2.imread(str(dst))
        bbox = stats[name].get("full_bbox")
        if bbox and before is not None:
            x0, y0, x1, y1 = bbox
            pad = max(24, (y1 - y0))
            h, w = before.shape[:2]
            bx0, by0 = max(0, x0 - pad), max(0, y0 - pad)
            bx1, by1 = min(w, x1 + pad), min(h, y1 + pad)
            before_c, after_c = before[by0:by1, bx0:bx1], after[by0:by1, bx0:bx1]
            zoom = min(2.0, 720 / max(1, before_c.shape[1]))
            zoom = (zoom, zoom) if zoom < 2.0 else (2.0, 2.0)
            f = zoom[0]
            before_c = cv2.resize(before_c, None, fx=f, fy=f,
                                  interpolation=cv2.INTER_CUBIC)
            after_c = cv2.resize(after_c, None, fx=f, fy=f,
                                 interpolation=cv2.INTER_CUBIC)
            sep = np.full((before_c.shape[0], 6, 3), 255, np.uint8)
            sheet = np.hstack([before_c, sep, after_c])
        else:
            sheet = np.hstack([before, np.full((before.shape[0], 6, 3), 255,
                                               np.uint8), after])
        out = sheets / f"cmp_{name}"
        cv2.imwrite(str(out.with_suffix(".jpg")), sheet,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        print(f"sheet: {out.with_suffix('.jpg')}")
    print(f"\n{len(cleaned)} comparison sheets in {sheets}/ "
          f"(left = original with watermark, right = cleaned)")
    return 0


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def cmd_sync(args: argparse.Namespace) -> int:
    processed = pathlib.Path(args.processed)
    manifest = json.loads(pathlib.Path(args.manifest).read_text())
    target = pathlib.Path(args.target)

    plan: list[tuple[str, str]] = []  # (src, dst)
    for comp in manifest["companies"].values():
        if comp.get("cover_file"):
            url = comp.get("cover_url") or ""
            plan.append((str(processed / _url_name(url)),
                         str(target / comp["cover_file"])))
        for vt in comp.get("vehicle_types", []):
            for img in vt.get("images", []):
                if img.get("file") and img.get("url"):
                    plan.append((str(processed / _url_name(img["url"])),
                                 str(target / img["file"])))

    replaced = missing = 0
    missing_urls: list[str] = []
    seen: set[str] = set()
    for src, dst in plan:
        if dst in seen:
            continue
        seen.add(dst)
        if not os.path.exists(src):
            missing += 1
            missing_urls.append(src)
            continue
        if args.dry_run:
            print(f"would replace {dst}")
        else:
            pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        replaced += 1
    print(f"sync: {replaced} files {'planned' if args.dry_run else 'replaced'}, "
          f"{missing} processed files missing")
    for u in missing_urls[:10]:
        print(f"  missing: {u}")
    return 1 if missing else 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def cmd_stats(args: argparse.Namespace) -> int:
    stats = json.loads((pathlib.Path(args.output) / "_stats.json").read_text())
    entries = {k: v for k, v in stats.items() if not k.startswith("_")}
    found = sum(1 for v in entries.values() if v.get("found"))
    errors = {k: v for k, v in entries.items() if v.get("error")}
    scores = [v["score"] for v in entries.values() if v.get("score") is not None]
    print(f"files processed : {len(entries)}")
    print(f"watermark found : {found} ({100 * found / max(1, len(entries)):.1f}%)")
    print(f"untouched       : {len(entries) - found - len(errors)}")
    print(f"errors          : {len(errors)}")
    if scores:
        scores.sort()
        print(f"match score    : median {scores[len(scores)//2]:.3f}, "
              f"min {scores[0]:.3f}, max {scores[-1]:.3f}")
    if errors:
        print("sample errors:")
        for k, v in list(errors.items())[:5]:
            print(f"  {k}: {v['error']}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watermark-removal",
        description="VeXeRe watermark removal — LaMa AI inpainting, CPU-only")
    sub = p.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("fetch", help="download original watermarked images")
    pf.add_argument("--manifest", required=True,
                    help="vehicle_type_media.json from the repo")
    pf.add_argument("--output", required=True, help="directory for originals")
    pf.add_argument("--workers", type=int, default=12, help="HTTP threads")
    pf.set_defaults(func=cmd_fetch)

    pp = sub.add_parser("process", help="AI watermark removal (resumable)")
    pp.add_argument("--input", required=True, help="directory of originals")
    pp.add_argument("--output", required=True, help="directory for cleaned images")
    pp.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: CPU count)")
    pp.add_argument("--template", default=None,
                    help="custom logo template PNG (default: bundled)")
    pp.add_argument("--context-scale", type=float, default=2.5,
                    help="context padding around the watermark as a multiple "
                         "of logo height (higher = better object "
                         "reconstruction, slower; 2.5 recommended)")
    pp.add_argument("--limit", type=int, default=0,
                    help="process only the first N images (testing)")
    pp.set_defaults(func=cmd_process)

    pc = sub.add_parser("compare", help="before/after inspection sheets")
    pc.add_argument("--input", required=True, help="directory of originals")
    pc.add_argument("--output", required=True, help="cleaned directory")
    pc.add_argument("--sheets", required=True, help="output dir for sheets")
    pc.add_argument("--samples", type=int, default=12)
    pc.add_argument("--seed", type=int, default=7)
    pc.set_defaults(func=cmd_compare)

    ps = sub.add_parser("sync", help="apply cleaned files to the repo tree")
    ps.add_argument("--processed", required=True, help="cleaned directory")
    ps.add_argument("--manifest", required=True)
    ps.add_argument("--target", required=True,
                    help="repo media dir, e.g. data/vehicle-type-media")
    ps.add_argument("--dry-run", action="store_true")
    ps.set_defaults(func=cmd_sync)

    pt = sub.add_parser("stats", help="summary of a processed run")
    pt.add_argument("--output", required=True, help="cleaned directory")
    pt.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
