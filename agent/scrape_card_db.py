#!/usr/bin/env python3
"""Scrape Ironclad card details from sts2-wiki.org and save JSON."""
import json, re, sys, time
import requests
from bs4 import BeautifulSoup

with open("/tmp/sts2-cli/ironclad_urls.json") as f:
    urls = json.loads(json.load(f) if False else open("/tmp/sts2-cli/ironclad_urls.json").read().strip())
# urls was JSON-escaped string from CLI output
if isinstance(urls, str):
    urls = json.loads(urls)
print(f"Scraping {len(urls)} cards…", file=sys.stderr)

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; ARM64) sts2-rl-scrape/1.0"

def parse_card(url, zh_name):
    """Return dict with id/en_name/zh_name/cost/type/rarity/normal_text/upgraded_text."""
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = {"zh_name": zh_name, "url": url}
    # id = last URL segment
    out["id"] = url.rstrip("/").split("/")[-1]
    # English name from page title
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
        m = re.match(r"^(.+?)\s*[–\-]", title)
        out["en_name"] = m.group(1).strip() if m else title
    # Meta line: "Attack • Common • Cost: 0 • Ironclad"
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if "Cost:" in text and ("Common" in text or "Uncommon" in text or "Rare" in text or "Basic" in text or "Starter" in text):
            parts = [x.strip() for x in re.split(r"[•·]", text)]
            out["meta_raw"] = text
            for part in parts:
                if part in {"Attack", "Skill", "Power"}:
                    out["type"] = part
                elif part in {"Basic", "Starter", "Common", "Uncommon", "Rare", "Special"}:
                    out["rarity"] = part
                elif part.startswith("Cost:"):
                    val = part.replace("Cost:", "").strip()
                    out["cost"] = val
                elif part == "Ironclad":
                    out["character"] = part
            break
    # Fallback: natural-language meta sentence like
    # "Aggression is a Rare Power card (Cost 1) in Slay the Spire 2"
    if "type" not in out:
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            m = re.search(r"(Basic|Starter|Common|Uncommon|Rare|Special)\s+(Attack|Skill|Power)\s+card", text)
            if m:
                out["rarity"] = m.group(1)
                out["type"] = m.group(2)
                cm = re.search(r"Cost\s+(\d+|X)", text)
                if cm:
                    out["cost"] = cm.group(1)
                out["meta_raw"] = text[:200]
                out["character"] = "Ironclad"
                break
    # Normal / Upgraded card text — labels are inside divs within sections,
    # immediately followed by <img>, then <p>{effect_text}</p>
    for label, key in [("Normal", "normal_text"), ("Upgraded", "upgraded_text")]:
        # Find the div whose stripped text == label exactly
        div = soup.find("div", string=lambda s: s and s.strip() == label)
        if div:
            nxt = div.find_next("p")
            if nxt:
                out[key] = nxt.get_text(strip=True)
    # Fix en_name: SEO title overrides h1 for some cards. Prefer last URL segment
    # turned to Title Case as fallback.
    if "en_name" not in out or "Guide" in out.get("en_name", ""):
        slug = out["id"].replace("-", " ").title()
        out["en_name"] = slug
    return out


results = []
errors = []
for i, c in enumerate(urls):
    try:
        data = parse_card(c["url"], c["zh"])
        results.append(data)
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(urls)} done…", file=sys.stderr)
        time.sleep(0.3)  # be polite
    except Exception as e:
        errors.append({"url": c["url"], "error": str(e)})
        print(f"  ERROR {c['zh']}: {e}", file=sys.stderr)

print(f"\nDone. Got {len(results)} cards, {len(errors)} errors.", file=sys.stderr)

with open("data/ironclad_cards.json", "w") as f:
    json.dump({"cards": results, "errors": errors}, f, indent=2, ensure_ascii=False)
print(f"Wrote data/ironclad_cards.json", file=sys.stderr)
