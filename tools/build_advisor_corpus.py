# tools/build_advisor_corpus.py
"""Build tests/fixtures/advisor/ooc_corpus.jsonl.gz -- real out-of-combat
decision states from a kept run_batch, each with the action greedy_action
takes on it now. Those actions are the golden the greedy trace work must keep.

greedy has hidden per-process state (card_scoring._MC_CONTEXT, rewritten by
every card_reward), so each game's states are replayed in order and that state
is reset at every game start -- the tests replay the same way.

Real states never reach some greedy branches (a rest site below the heal
threshold with every card upgraded, the shop heal-potion rule, an event with
every option locked, a map with an Elite beside a Monster at floor 1, 11 of the
28 event keywords, ...), so the corpus ends with one extra game, "synthetic":
variants of real states with a few fields rewritten (synthetic_rows).

It also writes ooc_corpus.meta.json next to the corpus: the batch it was built
from and the sha256 of every local data file greedy loads (GREEDY_DATA_FILES),
so the golden test can tell "a data file changed" from "greedy changed".

    .venv/bin/python tools/build_advisor_corpus.py \\
        --batch logs/batches/p3b-route-ab-20261012-160225

    # after greedy or its data files changed, before re-baselining: which
    # golden actions would change? writes nothing, exits 1 if any would.
    .venv/bin/python tools/build_advisor_corpus.py --check \\
        --batch logs/batches/p3b-route-ab-20261012-160225
"""
import argparse
import copy
import glob
import gzip
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.greedy_trace import (  # noqa: E402
    EVENT_KEYWORD_KEYS, GREEDY_BEHAVIOR_ENV, greedy_data_drift,
    greedy_data_sha256, reset_greedy_context)

CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")
ADVISED = frozenset({"card_reward", "rest_site", "event_choice", "bundle_select",
                     "card_select", "shop", "map_select"})
DEFAULT_OUT = os.path.join(ROOT, "tests", "fixtures", "advisor", "ooc_corpus.jsonl.gz")
SYNTHETIC_GAME = "synthetic"
_ACT_NAMES = {1: "Overgrowth", 2: "Hive"}


def meta_path(out: str) -> str:
    """ooc_corpus.jsonl.gz -> ooc_corpus.meta.json"""
    base = out[: -len(".jsonl.gz")] if out.endswith(".jsonl.gz") else out
    return base + ".meta.json"


def _seed_number(path: str):
    """The N of a trailing `_N.jsonl`, or None for a name without one."""
    m = re.search(r"_(\d+)\.jsonl$", path)
    return int(m.group(1)) if m else None


def game_states(path: str, stats: dict = None):
    """Advised decision states of one game log, in play order.

    A line that does not parse (a killed lane leaves a truncated last line) is
    skipped; stats["bad_lines"] counts them when a stats dict is given."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not a JSON object")
            except ValueError:
                if stats is not None:
                    stats["bad_lines"] = stats.get("bad_lines", 0) + 1
                continue
            data = row.get("data")
            if (row.get("type") == "state" and isinstance(data, dict)
                    and data.get("type") == "decision"
                    and data.get("decision") in ADVISED):
                yield data


def lane_files(batch: str, character: str) -> list:
    """Game logs of one lane in seed order; names without a trailing _N are skipped."""
    pattern = os.path.join(batch, "games", f"{character}__random", "*.jsonl")
    numbered = [(n, p) for p in glob.glob(pattern)
                if (n := _seed_number(p)) is not None]
    return [p for _, p in sorted(numbered)]


def select_games(batch: str, per_character: int, bundle_games: int) -> list:
    picked = []
    for ch in CHARACTERS:
        files = lane_files(batch, ch)[:per_character]
        if len(files) < per_character:
            print(f"warning: {ch} lane has {len(files)} game log(s), "
                  f"wanted {per_character}", file=sys.stderr)
        picked += files

    # bundle_select is rare (15 in the whole p3b random arm): add games that have
    # one, one per character per pass so no single character supplies them all.
    def with_bundle(ch):
        for path in lane_files(batch, ch):
            if path not in picked and any(
                    s["decision"] == "bundle_select" for s in game_states(path)):
                yield path

    candidates = {ch: with_bundle(ch) for ch in CHARACTERS}
    extra = []
    while len(extra) < bundle_games:
        progressed = False
        for ch in CHARACTERS:
            if len(extra) >= bundle_games:
                break
            path = next(candidates[ch], None)
            if path is not None:
                extra.append(path)
                progressed = True
        if not progressed:
            break
    return picked + extra


def build_rows(files: list, greedy, stats: dict = None) -> list:
    rows = []
    for path in files:
        reset_greedy_context()
        game = os.path.basename(path)[: -len(".jsonl")]
        for seq, state in enumerate(game_states(path, stats)):
            rows.append({"game": game, "seq": seq, "state": state,
                         "expected": greedy(state)})
    return rows


# --- synthetic variants ------------------------------------------------------

# One (title, description) per branch of combat_env._score_event_option, keyed
# by the branch's trace key (agent.greedy_trace.EVENT_KEYWORD_KEYS, source
# order). Each phrase fires its branch and, bar remove_card_hp_cost (which
# refines remove_card), no other. tests/agent/test_event_option_scores.py pins
# every phrase's score and checks it against this table.
EVENT_KEYWORD_CASES = {
    "event.kw.lose_max_hp": ("Sacrifice", "Lose 5 Max HP."),
    "event.kw.curse": ("Dark Pact", "Add a Curse to your deck."),
    "event.kw.lose_all_gold": ("Tribute", "Lose all your gold."),
    "event.kw.lose_gold": ("Toll", "Lose 50 gold."),
    "event.kw.torment": ("Torment", "Accept the mark."),
    "event.kw.take_damage": ("Brace", "Take 10 damage."),
    "event.kw.lose_hp": ("Bleed", "Lose 10 HP."),
    "event.kw.downgrade": ("Wear Down", "Downgrade a card."),
    "event.kw.add_basic": ("Stowaway", "Add an additional Strike to your deck."),
    "event.kw.rare_card": ("Gambit", "Obtain a rare card."),
    "event.kw.uncommon_card": ("Trade", "Obtain an uncommon card."),
    "event.kw.card_reward": ("Offer", "Choose a card to obtain."),
    "event.kw.remove_card": ("Purge", "Remove a card from your deck."),
    "event.kw.remove_card_hp_cost": ("Blood Price", "Pay 6 HP to remove a card from your deck."),
    "event.kw.relic": ("Find", "Obtain a random relic."),
    "event.kw.relic_with_downside": ("Idol", "Obtain a relic and add a Strike to your deck."),
    "event.kw.upgrade": ("Anvil", "Upgrade a card."),
    "event.kw.transform": ("Morph", "Transform a card."),
    "event.kw.gain_gold": ("Windfall", "Gain 100 gold."),
    "event.kw.gain_max_hp": ("Vitality", "Raise your Max HP by 7."),
    "event.kw.potion": ("Elixir", "Obtain a random potion."),
    "event.kw.heal": ("Rest", "Heal 20 HP."),
    "event.kw.colorless_card": ("Gift", "Add a colorless card to your deck."),
    "event.kw.gain_strength": ("Training", "Gain 1 Strength."),
    "event.kw.enemy_strength": ("Rage", "Enemies gain 1 Strength."),
    "event.kw.gain_dexterity": ("Agility", "Gain 1 Dexterity."),
    "event.kw.energy_each_turn": ("Focus", "Gain 1 energy at the start of each turn."),
    "event.kw.energy_once": ("Surge", "Gain 1 energy."),
}
assert list(EVENT_KEYWORD_CASES) == list(EVENT_KEYWORD_KEYS)

# The keyword option is paired with a neutral one (matches no keyword, scores 0).
EVENT_NEUTRAL_OPTION = ("Walk away", "Leave the area.")
# remove_card_hp_cost only trims remove_card's +6, so its rival is the free removal.
EVENT_RIVAL_OPTION = {"event.kw.remove_card_hp_cost": ("Purge", "Remove a card from your deck.")}
# Keyword options whose total score is below the rival's go FIRST: max() keeps
# the first of equal scores, so dropping the branch's negative delta flips the
# pick to the keyword option. The rest go second: dropping a positive delta
# ties them and the first (rival) option wins instead.
EVENT_KEYWORD_FIRST = frozenset({
    "event.kw.lose_max_hp", "event.kw.curse", "event.kw.lose_all_gold",
    "event.kw.lose_gold", "event.kw.torment", "event.kw.take_damage",
    "event.kw.lose_hp", "event.kw.downgrade", "event.kw.add_basic",
    "event.kw.remove_card_hp_cost", "event.kw.enemy_strength",
})


def _first(real_rows: list, decision: str, ok=lambda state: True) -> dict:
    for row in real_rows:
        if row["state"]["decision"] == decision and ok(row["state"]):
            return row["state"]
    raise LookupError(f"no real {decision} state to derive synthetic variants from")


def _set_progress(state: dict, floor: int, act: int = None, force_top: bool = False):
    """Set the act-local floor (and act) in context and, where the real state
    carries them (or force_top), at the top level -- the two must agree."""
    pairs = [("floor", floor)] + ([("act", act)] if act is not None else [])
    for key, value in pairs:
        state["context"][key] = value
        if force_top or key in state:
            state[key] = value
    if act is not None and (force_top or "act_name" in state):
        state["act_name"] = state["context"]["act_name"] = _ACT_NAMES.get(act, f"Act {act}")


def _set_hp(state: dict, ratio: float):
    player = state["player"]
    player["hp"] = max(1, round(player["max_hp"] * ratio))


def synthetic_rows(real_rows: list, greedy) -> list:
    """Deterministic variants of REAL corpus states that reach the greedy
    branches the real states never do, as one extra game named "synthetic".
    Each is a deepcopy of a real state of the same decision type with a few
    fields rewritten; `expected` is what greedy does on it now."""
    variants = []  # (label, state)

    # rest_site: floor x hp bands, with the real deck and with every card
    # upgraded (nothing to SMITH, so the heal-threshold / default-smith bands show).
    rest = _first(real_rows, "rest_site", lambda s: len(s["options"]) >= 2)
    for floor in (3, 8, 12, 15):
        for ratio in (0.25, 0.5, 0.7, 0.9, 1.0):
            for upgraded in (False, True):
                s = copy.deepcopy(rest)
                _set_progress(s, floor)
                _set_hp(s, ratio)
                if upgraded:
                    for card in s["player"]["deck"]:
                        card["upgraded"] = True
                deck = "upgraded-deck" if upgraded else "real-deck"
                variants.append((f"rest f{floor} hp{ratio} {deck}", s))
    for label, keep in (("heal-only", "HEAL"), ("smith-only", "SMITH")):
        s = copy.deepcopy(rest)
        _set_progress(s, 8)
        _set_hp(s, 1.0)
        s["options"] = [o for o in s["options"] if o["option_id"] == keep]
        variants.append((f"rest {label}", s))
    s = copy.deepcopy(rest)  # neither HEAL nor SMITH: take the first enabled option
    _set_progress(s, 8)
    _set_hp(s, 1.0)
    s["options"] = [{"index": 0, "option_id": "RECALL",
                     "name": "RecallRestSiteOption", "is_enabled": True}]
    variants.append(("rest other-option-only", s))
    s = copy.deepcopy(rest)
    for o in s["options"]:
        o["is_enabled"] = False
    variants.append(("rest none-enabled", s))

    # shop: heal potion at low HP; broke with nothing affordable.
    shop = _first(real_rows, "shop", lambda s: s.get("potions"))
    s = copy.deepcopy(shop)
    _set_hp(s, 0.30)
    s["player"]["potions"] = []
    s["potions"][0].update({"name": "Heal Potion", "description": "Heal 20 HP.",
                            "cost": 40, "is_stocked": True})
    s["player"]["gold"] = max(s["player"]["gold"], 40)
    variants.append(("shop heal-potion hp30%", s))
    s = copy.deepcopy(shop)
    s["player"]["gold"] = 0
    s["card_removal_cost"] = 75
    variants.append(("shop broke leave", s))

    # event_choice: every option locked.
    event = _first(real_rows, "event_choice", lambda s: s["options"])
    s = copy.deepcopy(event)
    for o in s["options"]:
        o["is_locked"] = True
    variants.append(("event all-locked", s))

    # event_choice: one row per _score_event_option keyword branch -- exactly two
    # unlocked options, a rival and the keyword option, ordered so that dropping
    # the branch's delta would flip the pick (see EVENT_KEYWORD_FIRST).
    for key, (title, desc) in EVENT_KEYWORD_CASES.items():
        rival = EVENT_RIVAL_OPTION.get(key, EVENT_NEUTRAL_OPTION)
        pair = [rival, (title, desc)]
        if key in EVENT_KEYWORD_FIRST:
            pair.reverse()
        s = copy.deepcopy(event)
        template = s["options"][0]
        s["options"] = [dict(copy.deepcopy(template), index=i, title=t, description=d,
                             is_locked=False, was_chosen=False, vars=None)
                        for i, (t, d) in enumerate(pair)]
        variants.append((key, s))

    # bundle_select: a single bundle.
    s = copy.deepcopy(_first(real_rows, "bundle_select", lambda s: len(s["bundles"]) >= 2))
    s["bundles"] = s["bundles"][:1]
    variants.append(("bundle single", s))

    # card_select: nothing to select; and a small external pool in an Event room
    # (nothing scoring below 3) from which the best max_select cards are taken.
    card_select = _first(real_rows, "card_select", lambda s: 2 <= len(s["cards"]) <= 10)
    s = copy.deepcopy(card_select)
    s["cards"] = []
    variants.append(("card_select empty", s))
    s = copy.deepcopy(card_select)
    s["context"]["room_type"] = "Event"
    s["max_select"] = 2
    variants.append(("card_select event pool top-N", s))

    # card_reward: no cards; a BROKEN_CARDS card among normal ones, and only
    # broken cards; late Act 1 with a >=16-card deck (the quality gate rejects
    # every offered card) and can_skip false, so greedy falls back to all cards.
    reward = _first(real_rows, "card_reward", lambda s: len(s["cards"]) >= 3)
    s = copy.deepcopy(reward)
    s["cards"] = []
    variants.append(("card_reward no-cards", s))
    s = copy.deepcopy(reward)
    s["cards"][0].update(id="CARD.WHIRLWIND", name="Whirlwind")
    variants.append(("card_reward broken-card among others", s))
    s = copy.deepcopy(reward)
    s["cards"] = s["cards"][:2]
    s["cards"][0].update(id="CARD.WHIRLWIND", name="Whirlwind")
    s["cards"][1].update(id="CARD.FLASH_OF_STEEL", name="Flash of Steel")
    variants.append(("card_reward all-broken", s))
    s = copy.deepcopy(reward)
    _set_progress(s, 13, 1)
    deck = s["player"]["deck"]
    starter = list(deck)
    while len(deck) < 16:
        deck.append(copy.deepcopy(starter[len(deck) % len(starter)]))
    s["player"]["deck_size"] = len(deck)
    s["can_skip"] = False
    variants.append(("card_reward act1 gate-rejects-all can_skip=false", s))

    # map_select: an Elite beside a Monster (real col/row shape), and no choices.
    maps = _first(real_rows, "map_select", lambda s: len(s["choices"]) >= 2)

    def elite_vs_monster(floor, ratio, act=1):
        s = copy.deepcopy(maps)
        s["choices"] = [dict(s["choices"][0], type="Elite"),
                        dict(s["choices"][1], type="Monster")]
        _set_progress(s, floor, act, force_top=True)
        _set_hp(s, ratio)
        return s

    for floor, ratio, act in ((1, 1.0, 1), (8, 0.95, 1), (14, 1.0, 1),
                              (16, 0.7, 1), (16, 0.95, 1),
                              (5, 0.7, 2), (5, 0.95, 2), (13, 1.0, 2),
                              (16, 0.7, 2), (16, 0.95, 2), (19, 0.95, 2)):
        variants.append((f"map elite-vs-monster act{act} f{floor} hp{ratio}",
                         elite_vs_monster(floor, ratio, act)))
    s = copy.deepcopy(maps)
    s["choices"] = []
    variants.append(("map no-choices", s))

    reset_greedy_context()
    return [{"game": SYNTHETIC_GAME, "seq": seq, "variant": label, "state": state,
             "expected": greedy(state)}
            for seq, (label, state) in enumerate(variants)]


# --- output ------------------------------------------------------------------

def write_corpus(rows: list, out: str):
    """Write the corpus gzip: UTF-8, no gzip name/mtime, so equal content gives
    equal bytes. Written beside `out` first; the caller os.replace()s it."""
    tmp = out + ".tmp"
    with open(tmp, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as text:
        for row in rows:
            text.write(json.dumps(row, ensure_ascii=False) + "\n")
    return tmp


def write_meta(batch: str, out: str) -> str:
    tmp = meta_path(out) + ".tmp"
    meta = {"batch": batch, "data_sha256": greedy_data_sha256()}
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    return tmp


def read_corpus(path: str) -> list:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def check_rows(rows: list, out: str) -> int:
    """Compare freshly built rows with the corpus at `out`; print what changed."""
    if not os.path.isfile(out):
        print(f"check: no corpus at {out}", file=sys.stderr)
        return 2
    old = {(r["game"], r["seq"]): r for r in read_corpus(out)}
    new = {(r["game"], r["seq"]): r for r in rows}
    changed, state_changed, per_decision = [], 0, {}
    for key, row in new.items():
        was = old.get(key)
        if was is None:
            continue
        if was["state"] != row["state"]:
            state_changed += 1
        elif was["expected"] != row["expected"]:
            changed.append((row, was["expected"]))
            d = row["state"]["decision"]
            per_decision[d] = per_decision.get(d, 0) + 1
    only_new = len(set(new) - set(old))
    only_old = len(set(old) - set(new))
    print(f"check: {len(new)} rows built, {len(old)} in corpus; "
          f"{len(changed)} expected action(s) changed")
    for decision in sorted(per_decision):
        total = sum(1 for r in new.values() if r["state"]["decision"] == decision)
        print(f"  {decision}: {per_decision[decision]} of {total} changed")
    for row, was in changed[:3]:
        print(f"  e.g. {row['game']} #{row['seq']} {row['state']['decision']}"
              f"{' (' + row['variant'] + ')' if 'variant' in row else ''}: "
              f"was {json.dumps(was)} now {json.dumps(row['expected'])}")
    if only_new or only_old or state_changed:
        print(f"  corpus shape differs from the batch: {only_new} new row(s), "
              f"{only_old} missing row(s), {state_changed} row(s) with a different state")
    mp = meta_path(out)
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            drift = greedy_data_drift(json.load(f).get("data_sha256", {}))
        if drift:
            print("  greedy's local data files differ from the corpus meta "
                  "(the golden test fails until the corpus is rebuilt):")
            for line in drift:
                print(f"    {line}")
    return 1 if (changed or only_new or only_old or state_changed) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", required=True, help="logs/batches/<id> with games/<Char>__random/")
    ap.add_argument("--per-character", type=int, default=2)
    ap.add_argument("--bundle-games", type=int, default=3)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="build in memory and report which golden actions would "
                         "change versus --out; write nothing, exit 1 if any do")
    args = ap.parse_args(argv)

    set_knobs = [k for k in GREEDY_BEHAVIOR_ENV if (os.environ.get(k) or "").strip()]
    if set_knobs:
        print(f"refusing: greedy behaviour env set: {set_knobs}", file=sys.stderr)
        return 2

    files = select_games(args.batch, args.per_character, args.bundle_games)
    if not files:
        print(f"refusing: no game logs under {args.batch}/games/<Char>__random/",
              file=sys.stderr)
        return 2

    from agent.combat_env import greedy_action
    stats = {"bad_lines": 0}
    real = build_rows(files, greedy_action, stats)
    missing = sorted(ADVISED - {r["state"]["decision"] for r in real})
    if missing:
        print(f"refusing: no {missing} state in the selected games", file=sys.stderr)
        return 2
    if stats["bad_lines"]:
        print(f"note: skipped {stats['bad_lines']} unparsable log line(s)", file=sys.stderr)
    rows = real + synthetic_rows(real, greedy_action)

    if args.check:
        return check_rows(rows, args.out)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    corpus_tmp = write_corpus(rows, args.out)
    meta_tmp = write_meta(args.batch, args.out)
    os.replace(corpus_tmp, args.out)
    os.replace(meta_tmp, meta_path(args.out))
    counts = {}
    for row in rows:
        counts[row["state"]["decision"]] = counts.get(row["state"]["decision"], 0) + 1
    print(f"{len(files)} games + {SYNTHETIC_GAME}, {len(rows)} states "
          f"({len(rows) - len(real)} synthetic) -> {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
