#!/usr/bin/env python3
"""Send titles through the proxy exactly like MetaTube does and flag outputs that are not Simplified Chinese.

usage: DS_KEY=... python3 tools/quality_check.py <proxy-base-url> <titles.json> [rounds]
titles.json: [{"title": "..."}, ...]
"""
import json, os, re, sys, urllib.request

# metatube-sdk-go translate/openai default system prompt (verbatim)
SYSTEM = """You are a professional translator for adult video content. Your sole task is to translate the user's input accurately and naturally. 
Rules:
1. Translate the user's input as provided, treating it as the source text.
2. Use official translations for actor/actress names if available; otherwise, keep them unchanged.
3. Do not invent translations for names without official versions.
4. Maintain any numbers, dates, and measurements in their original format.
5. Translate naturally and fluently, avoiding word-for-word translation.
6. Do not add any explanations, notes, or comments under any circumstances.
7. Only output the translation result, with no additional content."""
# characters that only exist in Traditional Chinese (common subset)
TRAD = set("與裝內們個來這說時會對後點過還讓當麼開關見體愛動樂親無為從應實國學頭氣間長門問題覺經樣發現進選邊兒麗戀顏絕淚誘戰覽擊聲戲劇歡嬌嫵憶誰妳廳鬆亂濕脫")


def translate(base, key, text):
    body = {"model": "deepseek-flash", "max_completion_tokens": 1000, "temperature": 0.1, "top_p": 1.0,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "Please translate the following text into Chinese:"},
                         {"role": "user", "content": text}]}
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def verdict(out):
    cjk = len(re.findall(r"[一-鿿]", out))
    latin = len(re.findall(r"[A-Za-z]", out))
    if cjk == 0 or latin > cjk:
        return "NOT-CHINESE"
    trad = [c for c in out if c in TRAD]
    return f"TRADITIONAL({''.join(trad)})" if len(trad) >= 2 else "ok"


def main():
    base, titles = sys.argv[1], json.load(open(sys.argv[2]))
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    bad = 0
    for r in range(rounds):
        for t in titles:
            out = translate(base, os.environ["DS_KEY"], t["title"])
            v = verdict(out)
            if v != "ok":
                bad += 1
                print(f"  round {r+1} {v}: {t['title'][:30]} -> {out[:60]}")
    print(f"bad {bad}/{rounds*len(titles)}")


if __name__ == "__main__":
    main()
