#!/usr/bin/env python3
"""Match the 306 crawled brand names to vexere brand-page slugs.
Handles đ (U+0111) which is NOT decomposable by NFD, plus suffix trimming.
"""
import json
import unicodedata

def norm(s: str) -> str:
    s = s.lower().replace("đ", "d").replace("Đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(c for c in s if c.isalnum())


slugs = [l.strip() for l in open("/home/z/my-project/research/brand_slugs.txt") if l.strip()]
# slug 'xe-sao-viet' → base 'sao-viet' → key 'saoviet'
slug_key = {norm(s.replace("xe-", "", 1).replace("-", "")): s for s in slugs}

inv = json.load(open("/home/z/my-project/research/brand_inventory.json"))

SUFFIXES = ["limousine", "luxury", "buslines", "bus", "travel", "car", "luxurycar", "opentour", "opentours"]

def candidates(name):
    n = norm(name.replace(" ", "").replace("(", "").replace(")", ""))
    yield n, ""
    # strip trailing suffixes one at a time
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf) + 2:
            yield n[: -len(suf)], f"-{suf}"
            # also try stripping a location qualifier in parens e.g. 'haison'
    # strip parenthetical qualifiers: 'vietlao(đanang)' → 'vietlao'
    if "(" in name:
        base = name.split("(")[0]
        yield norm(base.replace(" ", "")), "-paren"

matched, unmatched = {}, []
for b in inv:
    name = b["name"]
    found = None
    for key, note in candidates(name):
        if key in slug_key:
            found = (slug_key[key], note)
            break
    if found:
        matched[name] = {"slug": found[0], "url": f"https://vexere.com/vi-VN/{found[0]}/danh-gia", "match_note": found[1]}
    else:
        unmatched.append(name)

# fuzzy second pass: containment both ways for leftovers
for name in list(unmatched):
    n = norm(name.replace(" ", "").replace("(", "").replace(")", ""))
    for key, s in slug_key.items():
        if (n and (n in key or key in n)) and abs(len(n) - len(key)) <= 6:
            matched[name] = {"slug": s, "url": f"https://vexere.com/vi-VN/{s}/danh-gia", "match_note": "fuzzy"}
            unmatched.remove(name)
            break

print(f"MATCHED: {len(matched)}/{len(inv)}")
print(f"UNMATCHED ({len(unmatched)}):")
for u in unmatched:
    print("  -", u)

out = []
for b in inv:
    m = matched.get(b["name"])
    out.append({**b, "slug": m["slug"] if m else None, "brand_url": m["url"] if m else None})
json.dump(out, open("/home/z/my-project/research/brand_matches.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nSaved research/brand_matches.json")
