"""Extract the STS2 Card Advisor tier list + archetype axes into committed JSON.

Source: https://ing-gom.github.io/sts2-card-advisor/ (ing-gom/sts2-card-advisor).
The advisor renders its full card catalogue as an HTML table: one
  <tr class="card-row" data-card-id="CARD.X" data-card-name="..."
      data-card-tier="S|A|B|C|D" data-card-axes="..." data-origin-char="..."
      ...>...</tr>
row per card (shared-origin cards get one row per character context they were
simulated for, plus a "공용"/shared aggregate row). Older versions of this
script scanned for inline JS objects of the form `{"id":"CARD.X", ...}`
embedded elsewhere on the page; that format is a legacy leftover that now
covers only ~132 of the ~511 cataloged cards. We parse the table rows
instead, which covers the full catalogue.

We dedupe by data-card-id the same way the advisor's own in-page
`_getCardsById()` JS helper does: a row whose data-card-name does NOT end in
"+" is a "base" row and wins over a "+"-suffixed (upgraded) row; if only a
"+" row exists for an id, it is kept. In practice this also resolves the
character-context duplicates described above by keeping whichever row for
that id was encountered first (exactly mirroring the reference JS's
first-base-row-wins behavior).

Run `.venv/bin/python agent/build_advisor_ratings.py --help` for usage.
"""
import argparse
import json
import os
import re
import urllib.request
from html import unescape

ADVISOR_URL = "https://ing-gom.github.io/sts2-card-advisor/"
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

# Korean owner-character labels -> the uppercase English names used elsewhere
# in this repo. Rows whose data-origin-char is not one of these keys are junk
# (the advisor page occasionally leaves an unresolved `${CSS.escape(char)}`
# template literal in that attribute) and must be skipped, not guessed at.
_CHAR_MAP = {
    "아이언클래드": "IRONCLAD",
    "사일런트": "SILENT",
    "디펙트": "DEFECT",
    "네크로바인더": "NECROBINDER",
    "리젠트": "REGENT",
    "공용": "SHARED",
}

_HANGUL_RE = re.compile(r"[가-힣]")


def _norm_id(raw: str) -> str:
    cid = str(raw).upper().strip()
    return cid[5:] if cid.startswith("CARD.") else cid


def _attr(row: str, name: str) -> str | None:
    """Extract one double-quoted HTML attribute value from a <tr> tag string."""
    m = re.search(r"(?<![\w-])" + re.escape(name) + r'="([^"]*)"', row)
    return unescape(m.group(1)) if m else None


def _iter_rows(html: str):
    """Yield each raw `<tr class="card-row" ...>` opening tag (no children).

    NOTE: cannot use a naive `<tr class="card-row"([^>]*)>` regex here.
    Attribute values (data-card-desc, data-upg-desc, ...) contain literal
    newlines and can contain a literal ">" character, so a `[^>]*` class
    would either fail to span the value or truncate the tag at the wrong
    ">". Instead we scan forward from each row start, tracking whether we
    are inside a double-quoted attribute value, and stop at the first ">"
    seen *outside* quotes. Same spirit as the brace-counting caution this
    file used to carry for the old JS-object parser: naive
    regex/delimiter-matching breaks the moment a value contains the
    delimiter itself.
    """
    for m in re.finditer(r'<tr class="card-row"', html):
        i = m.start()
        j = i
        in_quotes = False
        while j < len(html):
            ch = html[j]
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == ">" and not in_quotes:
                break
            j += 1
        yield html[i:j + 1]


def _parse_int(raw):
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_float(raw):
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_na_int(raw):
    """Int attribute where the page uses -1 to mean "not applicable"."""
    v = _parse_int(raw)
    return None if v is None or v == -1 else v


def _parse_vars(raw) -> dict:
    """"Cards:1,Damage:5" -> {"Cards": 1, "Damage": 5}; non-int values stay strings."""
    out: dict = {}
    if not raw:
        return out
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            out[k] = int(v)
        except ValueError:
            out[k] = v
    return out


def _axis_list(raw) -> list:
    """"SCALING,강화 가치,SHIV" -> ["SCALING", "SHIV"] (Hangul filtered)."""
    axes = [a.strip() for a in (raw or "").split(",")]
    return [a for a in axes if a and not _HANGUL_RE.search(a)]


def _parse_row_fields(row: str) -> dict | None:
    """Parse one `<tr class="card-row">` tag into a raw field dict, or None
    if the row should be skipped entirely (blank tier / unmapped
    data-origin-char / blank id) -- see parse_cards()'s docstring for why.

    Single source of per-row extraction shared by parse_cards() (which keeps
    the historical narrower field set advisor_card_ratings.json shipped
    with, for byte-compatibility) and build_card_db() (which keeps the full
    set, including the upgrade-path fields parse_cards() drops).
    """
    tier = _attr(row, "data-card-tier")
    if not tier:
        return None

    # data-origin-char, not data-owner-char: the page renders shared cards
    # once per character *context* (6 identical rows — tier/ev/axes match
    # across all of them), and data-owner-char names that context. Only
    # data-origin-char says where the card actually comes from, so it is
    # the one that yields SHARED instead of an arbitrary character.
    character = _CHAR_MAP.get(_attr(row, "data-origin-char") or "")
    if character is None:
        return None

    raw_id = _attr(row, "data-card-id") or ""
    cid = _norm_id(raw_id)
    if not cid:
        return None

    name = _attr(row, "data-card-name") or raw_id
    is_upg = name.rstrip().endswith("+")

    versions_raw = _attr(row, "data-card-versions") or ""
    versions = versions_raw.split(",") if versions_raw else []

    return {
        "cid": cid,
        "is_upg": is_upg,
        "tier": tier,
        "axes": _axis_list(_attr(row, "data-card-axes")),
        "character": character,
        "anchor_score": _parse_float(_attr(row, "data-anchor-score")),
        "ev": _parse_float(_attr(row, "data-card-ev")),
        "cost": _parse_int(_attr(row, "data-card-cost")),
        "type": _attr(row, "data-card-type"),
        "rarity": _attr(row, "data-card-rarity"),
        "damage": _parse_na_int(_attr(row, "data-card-damage")),
        "block": _parse_na_int(_attr(row, "data-card-block")),
        "vars": _parse_vars(_attr(row, "data-card-vars")),
        "versions": versions,
        "upg_axes": _axis_list(_attr(row, "data-upg-axes")),
        "upg_cost": _parse_na_int(_attr(row, "data-upg-cost")),
        "upg_damage": _parse_na_int(_attr(row, "data-upg-damage")),
        "upg_block": _parse_na_int(_attr(row, "data-upg-block")),
    }


def _dedupe_rows(html: str) -> dict:
    """cid -> raw field dict, deduped by data-card-id the way the advisor's
    own in-page `_getCardsById()` JS helper does: a row whose data-card-name
    does NOT end in "+" is a "base" row and wins over a "+"-suffixed
    (upgraded) row; if only a "+" row exists for an id, it is kept. The
    first row for an id is kept once it's a base row, otherwise later rows
    keep overwriting until a base row is seen (or the last "+" row stands if
    no base row ever appears)."""
    out: dict = {}
    is_upg_by_id: dict = {}
    for row in _iter_rows(html):
        f = _parse_row_fields(row)
        if f is None:
            continue
        cid = f["cid"]
        if cid in out and not is_upg_by_id[cid]:
            continue  # a base row already won this id; keep it
        out[cid] = f
        is_upg_by_id[cid] = f["is_upg"]
    return out


def parse_cards(html: str) -> dict:
    """Return { NORM_ID: {...} } from advisor HTML `<tr class="card-row">` rows.

    Rows with a blank/missing tier are skipped entirely -- they never enter
    the output and never participate in dedupe. Rows whose data-origin-char
    doesn't map to a known character (see _CHAR_MAP) are also skipped
    entirely, since we'd otherwise have to fabricate a character. Remaining
    rows are deduped by data-card-id (see _dedupe_rows).

    This is the exact field set data/advisor_card_ratings.json has shipped
    with since it was introduced -- score_card() depends on that shape, so
    it must stay byte-compatible. See build_card_db() for the fuller field
    set (adds the upgrade-path fields) written to data/card_db_advisor.json.
    """
    deduped = _dedupe_rows(html)
    return {cid: {
        "tier": f["tier"],
        "axes": f["axes"],
        "character": f["character"],
        "anchor_score": f["anchor_score"],
        "ev": f["ev"],
        "cost": f["cost"],
        "type": f["type"],
        "rarity": f["rarity"],
        "damage": f["damage"],
        "block": f["block"],
        "vars": f["vars"],
        "versions": f["versions"],
    } for cid, f in deduped.items()}


def build_card_db(html: str) -> dict:
    """Return { NORM_ID: {...} } for ALL cataloged cards (currently 507),
    for data/card_db_advisor.json. Same row parsing and dedupe as
    parse_cards() (base row wins over a "+" row) but keeps the upgrade-path
    fields (upg_axes/upg_cost/upg_damage/upg_block) that parse_cards()
    intentionally omits to stay byte-compatible with the existing
    advisor_card_ratings.json shape.

    This exists because the card-id vocabulary the HP-loss deck predictor
    (agent/train_deck_predictor_hp.py) trains on was previously limited to
    the ~80 cards seen in deck_history.jsonl (agent/extract_card_db.py); the
    full 507-card catalogue plus its data-card-axes tags lets that predictor
    describe cards it never happened to see played.
    """
    deduped = _dedupe_rows(html)
    return {cid: {
        "cost": f["cost"],
        "type": f["type"],
        "rarity": f["rarity"],
        "tier": f["tier"],
        "ev": f["ev"],
        "damage": f["damage"],
        "block": f["block"],
        "vars": f["vars"],
        "axes": f["axes"],
        "upg_axes": f["upg_axes"],
        "upg_cost": f["upg_cost"],
        "upg_damage": f["upg_damage"],
        "upg_block": f["upg_block"],
        "character": f["character"],
        "versions": f["versions"],
    } for cid, f in deduped.items()}


def build_tags(ratings: dict) -> dict:
    """Return { NORM_ID: [axes...] } for cards that have any axes."""
    return {cid: r["axes"] for cid, r in ratings.items() if r.get("axes")}


def main():
    ap = argparse.ArgumentParser(description="Extract advisor card ratings to data/.")
    ap.add_argument("--html", help="Path to a saved advisor HTML snapshot. "
                                    "If omitted, fetch the live URL.")
    ap.add_argument("--url", default=ADVISOR_URL, help="Advisor URL (used when --html absent).")
    args = ap.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8") as f:
            html = f.read()
    else:
        with urllib.request.urlopen(args.url) as resp:
            html = resp.read().decode("utf-8")

    ratings = parse_cards(html)
    tags = build_tags(ratings)
    card_db = build_card_db(html)

    ratings_path = os.path.join(_DATA_DIR, "advisor_card_ratings.json")
    tags_path = os.path.join(_DATA_DIR, "advisor_card_tags.json")
    card_db_path = os.path.join(_DATA_DIR, "card_db_advisor.json")
    with open(ratings_path, "w", encoding="utf-8") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=0, sort_keys=True)
    with open(tags_path, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=0, sort_keys=True)
    with open(card_db_path, "w", encoding="utf-8") as f:
        json.dump(card_db, f, ensure_ascii=False, indent=0, sort_keys=True)

    by_char: dict = {}
    by_tier: dict = {}
    with_ev = 0
    for r in ratings.values():
        by_char[r["character"]] = by_char.get(r["character"], 0) + 1
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        if r.get("ev") is not None:
            with_ev += 1
    print(f"wrote {len(ratings)} cards -> {ratings_path}")
    print(f"wrote {len(tags)} tag entries -> {tags_path}")
    print(f"wrote {len(card_db)} cards -> {card_db_path}")
    print(f"with ev: {with_ev}/{len(ratings)}")
    print("by character:", dict(sorted(by_char.items())))
    print("by tier:", dict(sorted(by_tier.items())))

    ironclad_shared = {cid: r for cid, r in card_db.items()
                       if r["character"] in ("IRONCLAD", "SHARED")}
    axis_vocab = sorted({a for r in ironclad_shared.values() for a in r["axes"]})
    print(f"IRONCLAD+SHARED cards: {len(ironclad_shared)}")
    print(f"IRONCLAD+SHARED distinct axes: {len(axis_vocab)}")


if __name__ == "__main__":
    main()
