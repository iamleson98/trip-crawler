# -*- coding: utf-8 -*-
"""cleanup_brands.py — Delete every brand on the server + reset state.
Used before a clean re-run when a partial/buggy migration happened.
"""
import json
import sys
import urllib.request

from api_client import Client

BASE = "https://datxevui.com"


def main():
    email, password = sys.argv[1], sys.argv[2]
    c = Client(BASE, email, password, rps=12)
    c.login()
    items = c.get_json("/api/admin/brands")["items"]
    print(f"deleting {len(items)} brands …")
    for i, b in enumerate(items, 1):
        c.request("DELETE", f"/api/admin/brands/{b['id']}")
        if i % 50 == 0:
            print(f"  {i}/{len(items)}")
    left = c.get_json("/api/admin/brands")["items"]
    print(f"done; {len(left)} brands remain")
    from pathlib import Path
    p = Path(__file__).resolve().parent / "migration_state.json"
    p.unlink(missing_ok=True)
    print("state reset")


if __name__ == "__main__":
    main()
