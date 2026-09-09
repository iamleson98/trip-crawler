#!/usr/bin/env python3
"""Extract phone numbers + context from web-search snippets for top-30 brands.
Produces a review-ready TSV so numbers can be curated before going into Excel."""
import json
import glob
import re
import os

SEARCH_DIR = "/home/z/my-project/research/websearch"

PHONE = re.compile(
    r"(?:(\+?84[\s.\-]?\d{1,3}|0\d{1,3})[\s.\-]?\d{2,3}[\s.\-]?\d{2,4}(?:[\s.\-]?\d{1,4})?|(1900[\s.\-]?\d{2,5}(?:[\s.\-]?\d{1,4})?))"
)
CLEAN = re.compile(r"[^\d0+]")

def find_phones(text):
    out = []
    for m in PHONE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").strip()
        digits = CLEAN.sub("", raw.replace("+84", "0"))
        if 8 <= len(digits) <= 11:
            out.append(raw)
    return out

rows = []
for f in sorted(glob.glob(os.path.join(SEARCH_DIR, "phone_*.json"))):
    brand_key = os.path.basename(f)[len("phone_"):-len(".json")]
    d = json.load(open(f))
    per_result = []
    for r in d:
        host = r.get("host_name", "")
        name = r.get("name", "")
        snip = r.get("snippet", "") or ""
        phones = find_phones(name + " " + snip)
        if phones:
            per_result.append((host, name[:80], phones[:4], snip[:180]))
    rows.append((brand_key, per_result))

with open("/home/z/my-project/research/phone_extract.tsv", "w", encoding="utf-8") as out:
    for brand_key, per_result in rows:
        out.write(f"### {brand_key}\n")
        for host, title, phones, snip in per_result:
            out.write(f"  [{host}] {title}\n")
            out.write(f"    PHONES: {phones}\n")
            out.write(f"    {snip}\n")
        out.write("\n")

print("wrote research/phone_extract.tsv")
# also dump a compact JSON
compact = {b: [{"host": h, "title": t, "phones": p, "snippet": s} for h, t, p, s in pr] for b, pr in rows}
json.dump(compact, open("/home/z/my-project/research/phone_extract.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"brands with phone candidates: {sum(1 for _, pr in rows if pr)}/{len(rows)}")
