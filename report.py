#!/usr/bin/env python3
"""Summarise usage.jsonl written by proxy.py: requests, tokens and estimated cost per day.

usage: python3 report.py [usage.jsonl] [--since YYYY-MM-DD]

Prices are CNY per million tokens from https://api-docs.deepseek.com/zh-cn/quick_start/pricing
as checked on 2026-09-28 (last price change: 2026-09-10). DeepSeek changes prices often; re-check
before trusting the totals. Peak hours: Mon-Fri 09:00-12:00 and 14:00-18:00 Beijing time
(statutory holidays are off-peak but not modelled here).
"""
import json
import sys
from collections import defaultdict
from datetime import datetime

PRICES = {  # model: (cache hit, cache miss, output) at off-peak; peak is 2x
    "deepseek-flash": (0.02, 1.0, 4.0),
    "deepseek-v4-pro": (0.15, 4.5, 13.5),
}


def is_peak(ts: datetime) -> bool:
    return ts.weekday() < 5 and (9 <= ts.hour < 12 or 14 <= ts.hour < 18)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "usage.jsonl"
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None

    days = defaultdict(lambda: defaultdict(float))
    for line in open(path, encoding="utf-8"):
        e = json.loads(line)
        if since and e["ts"][:10] < since:
            continue
        ts = datetime.fromisoformat(e["ts"])
        d = days[e["ts"][:10]]
        d["requests"] += 1
        d["errors"] += e.get("status") != 200
        for k in ("cache_hit", "cache_miss", "completion", "reasoning"):
            d[k] += e.get(k) or 0
        hit, miss, out = PRICES.get(e.get("model"), PRICES["deepseek-flash"])
        factor = 2 if is_peak(ts) else 1
        d["cost"] += factor * ((e.get("cache_hit") or 0) * hit + (e.get("cache_miss") or 0) * miss
                               + (e.get("completion") or 0) * out) / 1e6

    print(f"{'date':10s} {'req':>6s} {'err':>4s} {'in(hit)':>9s} {'in(miss)':>9s} {'out':>9s} {'reasoning':>9s} {'≈CNY':>8s}")
    total = 0.0
    for day in sorted(days):
        d = days[day]
        total += d["cost"]
        print(f"{day:10s} {d['requests']:6.0f} {d['errors']:4.0f} {d['cache_hit']:9.0f} {d['cache_miss']:9.0f} "
              f"{d['completion']:9.0f} {d['reasoning']:9.0f} {d['cost']:8.3f}")
    print(f"total ≈ ¥{total:.3f}")


if __name__ == "__main__":
    main()
