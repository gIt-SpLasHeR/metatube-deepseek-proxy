#!/usr/bin/env python3
"""Undo tools/batch_translate.py: restore Name/Overview from its journal.

usage: JF_URL=... JF_KEY=... python3 tools/rollback.py [--all | ITEM_ID ...] [--journal /data/batch-translate.jsonl]
"""
import json
import os
import sys
import urllib.request

JF_URL = os.environ.get("JF_URL", "http://localhost:8096").rstrip("/")
JF_KEY = os.environ["JF_KEY"]


def jf(method, path, body=None):
    req = urllib.request.Request(JF_URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": f'MediaBrowser Token="{JF_KEY}"'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return json.loads(data) if data else None


def main():
    args = sys.argv[1:]
    journal = args[args.index("--journal") + 1] if "--journal" in args else "/data/batch-translate.jsonl"
    ids = [a for a in args if not a.startswith("--") and a != journal]
    if not ids and "--all" not in args:
        sys.exit(__doc__)
    uid = [u for u in jf("GET", "/Users") if u["Policy"]["IsAdministrator"]][0]["Id"]
    restored = 0
    for line in open(journal, encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("status") != "updated" or (ids and rec["id"] not in ids):
            continue
        dto = jf("GET", f"/Items/{rec['id']}?userId={uid}")
        dto["Name"], dto["Overview"] = rec["old_name"], rec["old_overview"]
        jf("POST", f"/Items/{rec['id']}", dto)
        restored += 1
    print(f"restored {restored} items")


if __name__ == "__main__":
    main()
