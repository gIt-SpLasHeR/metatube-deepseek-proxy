#!/usr/bin/env python3
"""Translate the Japanese titles/overviews of MetaTube-scraped movies in Jellyfin, in place.

Only Name and Overview are changed (genres, people, images, collections stay untouched), so this is
safer than forcing a metadata refresh. Name is rebuilt with MetaTube's default template
"{number} {title}". Translation goes through the thinking-off proxy with the exact prompt MetaTube
uses; outputs that are not Simplified Chinese are retried once with thinking enabled.

Every change is appended to a JSONL journal (old + new values) so it can be resumed and rolled back
(tools/rollback.py).

Environment:
  JF_URL, JF_KEY        Jellyfin base url and API key
  DS_KEY                DeepSeek API key
  PROXY_URL             thinking-off proxy (default http://localhost:8765)
  JOURNAL               journal path (default /data/batch-translate.jsonl)
  START_AT              "HH:MM" Beijing time to wait for before starting (optional)
  DRY_RUN=1             list what would change, no translation/updates
  LIMIT                 stop after N items (optional)
  PILOT                 stop if more than 10% of the first N items fail (default 30)
  BUDGET_CNY            stop when the estimated spend exceeds this (default 5)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
JF_URL = os.environ.get("JF_URL", "http://localhost:8096").rstrip("/")
JF_KEY = os.environ["JF_KEY"]
DS_KEY = os.environ.get("DS_KEY", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8765").rstrip("/")
JOURNAL = os.environ.get("JOURNAL", "/data/batch-translate.jsonl")
START_AT = os.environ.get("START_AT")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LIMIT = int(os.environ.get("LIMIT", "0"))
PILOT = int(os.environ.get("PILOT", "30"))
BUDGET = float(os.environ.get("BUDGET_CNY", "5"))
PRICE = (0.02, 1.0, 4.0)  # deepseek-flash off-peak CNY/M tokens: cache hit, miss, output (2026-09-28); peak is 2x

# metatube-sdk-go translate/openai default system prompt + openai-translator user prompt (verbatim)
SYSTEM = """You are a professional translator for adult video content. Your sole task is to translate the user's input accurately and naturally.
Rules:
1. Translate the user's input as provided, treating it as the source text.
2. Use official translations for actor/actress names if available; otherwise, keep them unchanged.
3. Do not invent translations for names without official versions.
4. Maintain any numbers, dates, and measurements in their original format.
5. Translate naturally and fluently, avoiding word-for-word translation.
6. Do not add any explanations, notes, or comments under any circumstances.
7. Only output the translation result, with no additional content."""
USER_PROMPT = "Please translate the following text into Chinese:"
# Only for the thinking-on retry of a bad first result: ask for Simplified Chinese explicitly.
USER_PROMPT_RETRY = "Please translate the following text into Simplified Chinese:"

KANA = re.compile(r"[぀-ヿ]")
HAN = re.compile(r"[一-鿿]")
TRAD = set("與裝內們個來這說時會對後點過還讓當麼開關見體愛動樂親無為從應實國學頭氣間長門問題覺經樣發現進選邊兒麗戀顏絕淚誘戰覽擊聲戲劇歡嬌嫵憶誰妳廳鬆亂濕脫")
REFUSAL_EN = ("i'm sorry", "i am sorry", "i can't", "i cannot", "as an ai")
REFUSAL_ZH = ("无法协助", "不能协助", "无法提供此", "我不能帮")


def log(msg):
    print(f"{datetime.now(BEIJING):%m-%d %H:%M:%S} {msg}", flush=True)


def http(method, url, body=None, headers=None, timeout=300):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return json.loads(data) if data else None


def jf(method, path, body=None):
    return http(method, JF_URL + path, body, {"Authorization": f'MediaBrowser Token="{JF_KEY}"'})


def is_peak(t):
    return t.weekday() < 5 and (9 <= t.hour < 12 or 14 <= t.hour < 18)


def wait_off_peak():
    while is_peak(datetime.now(BEIJING)):
        log("peak hours, pausing 5 min")
        time.sleep(300)


def looks_japanese(text, share):
    """True when kana make up more than `share` of the CJK text (names kept in kana stay well below that)."""
    kana, han = len(KANA.findall(text)), len(HAN.findall(text))
    return kana >= 6 and kana > share * (kana + han)


def verdict(out, src):
    """None if `out` is an acceptable Simplified Chinese translation of `src`, otherwise the problem."""
    if not out.strip():
        return "empty"
    low, src_low = out.lower(), src.lower()
    if any(p in low and p not in src_low for p in REFUSAL_EN) or any(p in out and p not in src for p in REFUSAL_ZH):
        return "refused"
    # Brand names legitimately stay in (or turn into) Latin letters, so measure Han characters, not letters.
    src_cjk = len(HAN.findall(src)) + len(KANA.findall(src))
    if src_cjk >= 4 and len(HAN.findall(out)) < 0.3 * src_cjk:
        return "not-chinese"
    if looks_japanese(out, 0.4):
        return "japanese"
    if sum(c in TRAD for c in out) >= 2:
        return "traditional"
    return None


def translate(text, thinking):
    body = {"model": "deepseek-flash", "max_completion_tokens": 1000, "temperature": 0.1, "top_p": 1.0,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": USER_PROMPT_RETRY if thinking else USER_PROMPT},
                         {"role": "user", "content": text}]}
    if thinking:
        body["thinking"] = {"type": "enabled"}  # the proxy keeps an explicit client setting
    for attempt in range(4):
        try:
            d = http("POST", PROXY_URL + "/v1/chat/completions", body, {"Authorization": f"Bearer {DS_KEY}"})
            u = d.get("usage") or {}
            cost = ((u.get("prompt_cache_hit_tokens") or 0) * PRICE[0] + (u.get("prompt_cache_miss_tokens") or 0) * PRICE[1]
                    + (u.get("completion_tokens") or 0) * PRICE[2]) / 1e6
            return (d["choices"][0]["message"]["content"] or "").strip(), u.get("completion_tokens") or 0, cost
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            log(f"  translate error ({e}), retry {attempt + 1}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("translation failed after retries")


_cache = {}  # duplicate items (same movie in two folders) share one translation


def translate_checked(text):
    """-> (translation or None, info dict)"""
    if text in _cache:
        out, info = _cache[text]
        return out, {**info, "tokens": 0, "cost": 0.0, "cached": True}
    out, info = _translate_checked(text)
    _cache[text] = (out, info)
    return out, info


def _translate_checked(text):
    out, tokens, cost = translate(text, thinking=False)
    info = {"tokens": tokens, "cost": cost, "thinking_fallback": False}
    problem = verdict(out, text)
    if problem:
        info["first_try"] = {"problem": problem, "output": out[:200]}
        out, tokens, cost = translate(text, thinking=True)
        info.update(tokens=info["tokens"] + tokens, cost=info["cost"] + cost, thinking_fallback=True)
        problem = verdict(out, text)
    info["problem"] = problem
    return (None if problem else out), info


def fetch_movies(uid):
    items, start = [], 0
    while True:
        q = urllib.parse.urlencode({"userId": uid, "includeItemTypes": "Movie", "recursive": "true", "startIndex": start,
                                    "limit": 500, "fields": "ProviderIds,OriginalTitle,Overview"})
        page = jf("GET", f"/Items?{q}")
        items += page["Items"]
        start += len(page["Items"])
        if not page["Items"] or start >= page["TotalRecordCount"]:
            return items


def load_journal():
    """id -> records, oldest first."""
    recs = {}
    if os.path.exists(JOURNAL):
        for line in open(JOURNAL, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                recs.setdefault(r["id"], []).append(r)
    return recs


def candidates(uid, journal):
    """Movies whose title and/or overview still need translating.

    Name is built as "{number} {title}". While untranslated (or garbled by an earlier translator that
    appended notes) it still contains OriginalTitle, and the part before it is the number. A name that
    contains OriginalTitle is left alone when an earlier run already settled it and the current title
    passes today's checks (names or pure-kanji titles translate to themselves).
    """
    out = []
    for it in fetch_movies(uid):
        if "MetaTube" not in (it.get("ProviderIds") or {}):
            continue
        name = (it.get("Name") or "").strip()
        ot = (it.get("OriginalTitle") or "").strip()
        overview = it.get("Overview") or ""
        c = {"id": it["Id"], "name": name, "overview": overview, "number": None, "title": None,
             "do_overview": looks_japanese(overview, 0.25), "why": []}
        last = (journal.get(it["Id"]) or [{}])[-1]
        i = name.find(ot) if ot else -1
        if i >= 0:
            number, current = name[:i].strip(), name[i:].strip()
            settled = last.get("status") in ("updated", "unchanged") and verdict(current, ot) is None
            if not settled:
                c.update(number=number, title=ot)
                c["why"].append("garbled" if "\n" in name or not name.endswith(ot)
                                else f"retry-{last['status']}" if last else "new")
        elif ot and last.get("status") == "updated" and last.get("new_name") == name:
            # Translated by an earlier run and not edited since: re-check it against today's rules
            # (catches e.g. a long title that came back as just the actress name).
            j = last["old_name"].find(ot)
            number = last["old_name"][:j].strip() if j >= 0 else None
            if number is not None and name.startswith(number) and verdict(name[len(number):].strip(), ot):
                c.update(number=number, title=ot)
                c["why"].append("recheck")
        if c["do_overview"]:
            c["why"].append("overview")
        if c["title"] or c["do_overview"]:
            out.append(c)
    return out


def main():
    uid = [u for u in jf("GET", "/Users") if u["Policy"]["IsAdministrator"]][0]["Id"]
    journal = load_journal()
    todo = candidates(uid, journal)
    reasons = {}
    for c in todo:
        for w in c["why"]:
            reasons[w] = reasons.get(w, 0) + 1
    log(f"candidates: {len(todo)} {reasons}")

    if DRY_RUN:
        for c in todo[:40]:
            log(f"  {c['why']} number={c['number']!r} name={c['name'][:50]!r}")
        return

    if START_AT:
        h, m = map(int, START_AT.split(":"))
        now = datetime.now(BEIJING)
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start > now:
            log(f"waiting until {start:%Y-%m-%d %H:%M} Beijing time")
            time.sleep((start - now).total_seconds())

    spent, counts, fallback = 0.0, {"updated": 0, "unchanged": 0, "skipped": 0}, 0
    t0 = time.time()
    with open(JOURNAL, "a", encoding="utf-8") as jf_out:
        for n, c in enumerate(todo[: LIMIT or None], 1):
            wait_off_peak()
            title, tinfo = translate_checked(c["title"]) if c["title"] else (None, None)
            overview, oinfo = translate_checked(c["overview"]) if c["do_overview"] else (None, None)
            for info in (tinfo, oinfo):
                if info:
                    spent += info["cost"]
                    fallback += info["thinking_fallback"]

            new_name = f"{c['number']} {title}".strip() if title else c["name"]
            new_overview = overview or c["overview"]
            failed = (c["title"] and not title) or (c["do_overview"] and not overview)
            if new_name != c["name"] or new_overview != c["overview"]:
                dto = jf("GET", f"/Items/{c['id']}?userId={uid}")
                dto["Name"], dto["Overview"] = new_name, new_overview
                jf("POST", f"/Items/{c['id']}", dto)
                status = "updated"
            else:
                status = "skipped" if failed else "unchanged"
            counts[status] += 1
            rec = {"id": c["id"], "ts": datetime.now(BEIJING).isoformat(timespec="seconds"), "status": status,
                   "why": c["why"], "old_name": c["name"], "old_overview": c["overview"], "new_name": new_name,
                   "new_overview": overview, "title_info": tinfo, "overview_info": oinfo}
            jf_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jf_out.flush()

            if n % 50 == 0 or n == len(todo):
                log(f"{n}/{len(todo)} {counts} thinking_fallback={fallback} ≈¥{spent:.3f} "
                    f"{(time.time() - t0) / n:.1f}s/item")
            if n == PILOT and counts["skipped"] > PILOT * 0.1:
                log(f"pilot failed: {counts['skipped']}/{PILOT} skipped, stopping")
                sys.exit(1)
            if spent > BUDGET:
                log(f"budget ¥{BUDGET} exceeded (≈¥{spent:.2f}), stopping")
                sys.exit(1)
    log(f"done: {counts} thinking_fallback={fallback} ≈¥{spent:.3f} in {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    main()
