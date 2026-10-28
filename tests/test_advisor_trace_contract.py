# tests/test_advisor_trace_contract.py
"""Real-game decision JSON (the mod's advisor_trace.jsonl, saved as a fixture after
the first in-game session) has the fields and JSON types the headless engine
produces for the same decision type. greedy silently mis-scores a field that is
missing or has another type, so this is the check the mod's serializer needs.

Fixture: tests/fixtures/advisor/real_game_trace.jsonl.gz, one in-game run with the mod
(Silent-like character, A1F1 -> A3F3, 2026-10-28, plan Task C8). Each line is
{ts, decision, state, reply, error?}. Every attempt is logged again (the screen is
re-sent on a retry, and Undo/Rewind mods put the player back on screens already seen),
so `load_game_states` dedupes identical states and drops failed requests.

What is compared, per decision type, against tests/fixtures/advisor/ooc_corpus.jsonl.gz
(`corpus_rows`, the headless engine's states):
  * key paths: every path all headless samples have must exist in the real state;
  * JSON types: the real game may not use a type headless never does at that path;
  * null: only where headless has null at that path somewhere (null where headless is
    never null is a missing field in disguise);
  * enum-like string leaves (`type`, `rarity`, `option_id`, `target_type`, `keywords[]`):
    no value headless has never produced, except ALLOWED_NEW_VALUES;
  * the share of deck cards whose `after_upgrade` is null and of states with
    `context.boss` -- DecisionSerializer swallows exceptions there, so a silently
    broken read shows up as a share that moves, not as a missing key.

Not compared on purpose: list lengths (the first map of an act lists [start, ...children]
in headless and only [start] in the mod); the keys of the dynamic maps `stats.*` and
`vars.*`, which are named after the card/relic/event and so differ with the character
and the content seen (only keys headless has are type-checked); `context.room_type` on a
map_select (headless says "Map", the real game still reports the room the player just
left; greedy's map strategy does not read it); an X-cost card's `cost`, 0 outside combat
in both."""
import gzip
import json
import os

import pytest

from tests.agent.advisor_corpus import corpus_rows

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "advisor",
                       "real_game_trace.jsonl.gz")

# (decision, top-level key) the real game legitimately lacks or leaves null.
# Never add a field greedy reads (GREEDY_READ_TOP_KEYS; test_allowed_missing_...).
ALLOWED_MISSING = {
    ("card_reward", "gold_earned"),  # DecisionCapture passes null (22/22 real states): no pre-combat gold snapshot
    ("card_reward", "from_event"),   # headless-only marker for event card rewards (absent from today's corpus too)
}

# Top-level keys greedy_action (agent/combat_env.py) reads from a decision, plus the
# decision-specific ones inside them (cards[]/deck[]: id, name, stats, cost, type,
# rarity, keywords; player.hp/max_hp/gold/deck/potions; context.act/floor/room_type;
# events: title/description/is_locked; shop: is_stocked/cost; card_removal_cost).
GREEDY_READ_TOP_KEYS = {
    "act", "bundles", "can_skip", "card_removal_cost", "cards", "choices", "context",
    "decision", "floor", "max_select", "min_select", "options", "player", "potions",
    "relics",
}

# {(decision, path): values} the real game has that the headless corpus never produced.
ALLOWED_NEW_VALUES = {
    # A Neow "ancient card" option puts a rarity-"Ancient" card in the deck; no headless
    # card_select state happens to list one.
    ("card_select", "cards[].rarity"): {"Ancient"},
}

ENUM_LEAVES = ("type", "rarity", "option_id", "target_type")
SHARE_TOLERANCE = 0.10  # percentage points, as a fraction


def _type(v):
    return {dict: "object", list: "array", str: "string", bool: "bool",
            type(None): "null"}.get(type(v), "number")


def paths(obj) -> dict:
    """{key path: set of JSON types}; list items share the path segment `[]`."""
    out = {}

    def walk(v, p):
        out.setdefault(p, set()).add(_type(v))
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{p}.{k}" if p else k)
        elif isinstance(v, list):
            for x in v:
                walk(x, f"{p}[]")
    walk(obj, "")
    return out


def contract_problems(headless_states: list, game_states: list, decision: str) -> list:
    """Paths every headless sample has but a game sample lacks, types the game uses
    that headless never does, and null where headless is never null."""
    required = None
    seen_types = {}
    for s in headless_states:
        ps = paths(s)
        required = set(ps) if required is None else required & set(ps)
        for p, t in ps.items():
            seen_types.setdefault(p, set()).update(t)
    problems = []
    for g in game_states:
        gp = paths(g)
        for p in sorted(required or ()):
            top = p.split(".")[0].split("[")[0]
            if p not in gp and (decision, top) not in ALLOWED_MISSING:
                problems.append(f"{decision}: missing {p}")
        for p, types in gp.items():
            if p not in seen_types:
                continue
            top = p.split(".")[0].split("[")[0]
            extra = types - seen_types[p] - {"null"}
            if extra:
                problems.append(f"{decision}: {p} is {sorted(extra)}, headless uses {sorted(seen_types[p])}")
            elif "null" in types and "null" not in seen_types[p] \
                    and (decision, top) not in ALLOWED_MISSING:
                problems.append(f"{decision}: {p} is null, headless never has null there")
    return sorted(set(problems))


def enum_values(states: list) -> dict:
    """{path: set of string values} for the enum-like string leaves of `states`."""
    out = {}

    def walk(v, p):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{p}.{k}" if p else k)
        elif isinstance(v, list):
            for x in v:
                walk(x, f"{p}[]")
        elif isinstance(v, str) and (p.rsplit(".", 1)[-1] in ENUM_LEAVES
                                     or p.endswith("keywords[]")):
            out.setdefault(p, set()).add(v)
    for s in states:
        walk(s, "")
    return out


def enum_problems(headless_states: list, game_states: list, decision: str) -> list:
    """Enum-like string values the real game has that headless never produced."""
    known = enum_values(headless_states)
    problems = []
    for p, vals in sorted(enum_values(game_states).items()):
        if p not in known:
            continue
        new = vals - known[p] - ALLOWED_NEW_VALUES.get((decision, p), set())
        if new:
            problems.append(f"{decision}: {p} has {sorted(new)}, headless only {sorted(known[p])}")
    return problems


def after_upgrade_null_share(states: list) -> float:
    """Share of deck cards (over all states) whose `after_upgrade` is null."""
    cards = [c for s in states for c in (s.get("player") or {}).get("deck") or []]
    return sum(1 for c in cards if c.get("after_upgrade") is None) / len(cards)


def swallowed_upgrade_info(states: list) -> list:
    """Ids of not-yet-upgraded Attack/Skill/Power deck cards with a null `after_upgrade`.
    Only Curse/Status cards have no upgrade preview; any other one is a read that threw."""
    return sorted({c["id"] for s in states for c in (s.get("player") or {}).get("deck") or []
                   if c.get("after_upgrade") is None and not c.get("upgraded")
                   and c.get("type") in ("Attack", "Skill", "Power")})


def boss_share(states: list) -> float:
    """Share of states whose `context.boss` is present (non-null)."""
    return sum(1 for s in states if (s.get("context") or {}).get("boss")) / len(states)


def load_game_states(path: str = FIXTURE) -> list:
    """The states of a mod trace in file order: lines with a null reply or an `error`
    (failed requests) are dropped, and a state seen again is kept once."""
    out, seen = [], set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("reply") is None or row.get("error"):
                continue
            key = json.dumps(row["state"], sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(row["state"])
    return out


def headless_by_decision() -> dict:
    out = {}
    for row in corpus_rows():
        out.setdefault(row["state"]["decision"], []).append(row["state"])
    return out


# --- the check functions, on synthetic data -------------------------------------------

def test_checker_flags_missing_and_retyped_fields():
    headless = [{"decision": "x", "player": {"hp": 5, "deck": [{"id": "A"}]}, "cards": []},
                {"decision": "x", "player": {"hp": 7, "deck": [{"id": "B"}]}, "cards": [{"n": 1}]}]
    assert contract_problems(headless, [headless[0]], "x") == []
    game = [{"decision": "x", "player": {"hp": "5", "deck": []}, "cards": []}]
    assert contract_problems(headless, game, "x") == [
        "x: missing player.deck[]", "x: missing player.deck[].id",
        "x: player.hp is ['string'], headless uses ['number']"]
    assert contract_problems([{"gold_earned": 3}], [{}], "card_reward") == []


def test_checker_null_is_allowed_only_where_headless_has_null():
    headless = [{"hp": 5, "kw": None}, {"hp": 6, "kw": ["Exhaust"]}]
    assert contract_problems(headless, [{"hp": 5, "kw": None}], "x") == []
    assert contract_problems(headless, [{"hp": None, "kw": None}], "x") == [
        "x: hp is null, headless never has null there"]
    # a field that is allowed to be missing is allowed to be null too
    assert contract_problems([{"gold_earned": 3}], [{"gold_earned": None}], "card_reward") == []
    assert contract_problems([{"gold_earned": 3}], [{"gold_earned": None}], "shop") == [
        "shop: gold_earned is null, headless never has null there"]


def test_checker_ignores_keys_headless_has_never_seen():
    # stats/vars are named after the card, so a new key is not a contract break
    headless = [{"cards": [{"stats": {"damage": 6}}]}]
    assert contract_problems(headless, [{"cards": [{"stats": {"damage": 6, "shivs": 2}}]}], "x") == []
    assert contract_problems(headless, [{"cards": [{"stats": {"damage": "6"}}]}], "x") == [
        "x: cards[].stats.damage is ['string'], headless uses ['number']"]


def test_enum_check_flags_values_headless_never_produced():
    headless = [{"cards": [{"type": "Attack", "rarity": "Common", "keywords": ["Exhaust"], "name": "A"}]},
                {"cards": [{"type": "Skill", "rarity": "Rare", "keywords": None, "name": "B"}]}]
    ok = [{"cards": [{"type": "Skill", "rarity": "Common", "keywords": ["Exhaust"], "name": "new name"}]}]
    assert enum_problems(headless, ok, "x") == []
    bad = [{"cards": [{"type": "ATTACK", "rarity": "Ancient", "keywords": ["Retain"], "name": "A"}]}]
    assert enum_problems(headless, bad, "x") == [
        "x: cards[].keywords[] has ['Retain'], headless only ['Exhaust']",
        "x: cards[].rarity has ['Ancient'], headless only ['Common', 'Rare']",
        "x: cards[].type has ['ATTACK'], headless only ['Attack', 'Skill']"]


def test_enum_check_allow_list_is_per_decision_and_path():
    headless = [{"cards": [{"rarity": "Basic"}]}]
    game = [{"cards": [{"rarity": "Ancient"}]}]
    assert enum_problems(headless, game, "card_select") == []
    assert enum_problems(headless, game, "card_reward") != []


def _deck(*cards):
    return {"player": {"deck": [dict(zip(("id", "type", "upgraded", "after_upgrade"), c)) for c in cards]}}


def test_after_upgrade_share_and_swallowed_reads():
    states = [_deck(("A", "Attack", False, {"cost": 1}), ("B", "Curse", False, None)),
              _deck(("C", "Skill", True, None), ("D", "Skill", False, {"cost": 0}))]
    assert after_upgrade_null_share(states) == 0.5
    assert swallowed_upgrade_info(states) == []
    # a plain unupgraded Skill whose preview is null means the read threw
    assert swallowed_upgrade_info([_deck(("E", "Skill", False, None), ("B", "Curse", False, None))]) == ["E"]
    assert after_upgrade_null_share([{"player": {"deck": [{"id": "A"}]}}]) == 1.0  # key absent


def test_boss_share():
    states = [{"context": {"boss": {"id": "X"}}}, {"context": {"boss": None}},
              {"context": {}}, {"context": {"boss": {"id": "Y"}}}]
    assert boss_share(states) == 0.5


def test_load_game_states_drops_failed_requests_and_repeats(tmp_path):
    s1, s2, s3 = ({"decision": "map_select", "n": n} for n in (1, 2, 3))
    rows = [{"state": s1, "reply": {"a": 1}}, {"state": s1, "reply": {"a": 2}},   # re-sent
            {"state": s2, "reply": None}, {"state": s2, "reply": {"a": 1}, "error": "HttpRequestException"},
            {"state": s3, "reply": {"a": 3}}]
    path = tmp_path / "t.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n\n")
    assert load_game_states(str(path)) == [s1, s3]


# --- the real trace -------------------------------------------------------------------

def test_allowed_missing_never_covers_a_field_greedy_reads():
    assert not {k for _, k in ALLOWED_MISSING} & GREEDY_READ_TOP_KEYS


@pytest.fixture(scope="module")
def game_and_headless():
    if not os.path.exists(FIXTURE):
        pytest.skip("record tests/fixtures/advisor/real_game_trace.jsonl.gz in-game first (Task C8)")
    game = {}
    for s in load_game_states():
        game.setdefault(s["decision"], []).append(s)
    return game, headless_by_decision()


def test_real_trace_has_the_main_decision_types(game_and_headless):
    game, _ = game_and_headless
    assert {"map_select", "card_reward", "event_choice", "rest_site", "card_select", "shop"} <= set(game)


def test_real_game_json_matches_headless_shapes(game_and_headless):
    game, headless = game_and_headless
    problems = []
    for decision, states in sorted(game.items()):
        assert decision in headless, f"real game produced {decision!r}, which headless never does"
        problems += contract_problems(headless[decision], states, decision)
        problems += enum_problems(headless[decision], states, decision)
    assert not problems, "\n".join(problems)


def test_real_game_serializer_swallowed_nothing(game_and_headless):
    """DecisionSerializer wraps some reads in try/catch; a throw there nulls the field
    without any error, so compare how often the real game and headless produce null."""
    game, headless = game_and_headless
    real = [s for ss in game.values() for s in ss]
    ref = [s for ss in headless.values() for s in ss]
    assert swallowed_upgrade_info(real) == []
    a, b = after_upgrade_null_share(real), after_upgrade_null_share(ref)
    assert abs(a - b) <= SHARE_TOLERANCE, f"after_upgrade null share: real {a:.3f}, headless {b:.3f}"
    a, b = boss_share(real), boss_share(ref)
    assert abs(a - b) <= SHARE_TOLERANCE, f"context.boss present share: real {a:.3f}, headless {b:.3f}"
