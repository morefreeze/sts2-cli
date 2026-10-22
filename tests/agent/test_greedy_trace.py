"""greedy_action(state, trace=...) records the evidence it decided with, and the
evidence agrees with the choice. Replays the corpus in game order."""
import copy
import json

import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GreedyTrace, reset_greedy_context
from tests.agent.advisor_corpus import (corpus_games, corpus_rows, fresh_greedy_state,
                                        require_same_greedy_data)


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


# The traced replay of the whole corpus costs ~10 s and nearly every test below
# wants a slice of it, so it is computed once per process (on first use, under
# the autouse fixture's fresh greedy state, starting each game from
# reset_greedy_context() exactly as a per-test replay did) and every caller gets
# a deep copy: a test cannot mutate what the next one sees.
_TRACED_CORPUS = None


def _traced_corpus_all():
    global _TRACED_CORPUS
    if _TRACED_CORPUS is None:
        # a stray env var or a changed data file must fail here, not poison the
        # cache every later test reads
        fresh_greedy_state()
        require_same_greedy_data()
        out = []
        for _game, rows in corpus_games():
            reset_greedy_context()
            for row in rows:
                trace = GreedyTrace()
                action = greedy_action(row["state"], trace=trace)
                # the advisor emits the trace as JSON
                json.dumps(trace.to_dict(), allow_nan=False)
                out.append((row, action, trace))
        _TRACED_CORPUS = out
    return _TRACED_CORPUS


def traced_corpus(decision=None):
    """(row, action, trace) for every corpus state of `decision` (None = all).
    Every state is replayed (traced) in game order so greedy's hidden context
    stays in step; the replay runs once per process and each call yields deep
    copies of the cached results.

    Tests that set an environment variable (or otherwise change what greedy
    does) must call greedy_action themselves, not use this."""
    for row, action, trace in _traced_corpus_all():
        if decision is None or row["state"]["decision"] == decision:
            yield copy.deepcopy((row, action, trace))


def _sums_match(trace):
    for opt in trace.to_dict()["options"]:
        if opt["status"] == "ok" and opt["components"]:
            total = sum(c["value"] for c in opt["components"])
            assert abs(total - opt["score"]) < 1e-9, opt


def test_trace_does_not_change_any_choice():
    for row, action, _trace in traced_corpus():
        assert action == row["expected"], (row["game"], row["seq"])


def test_card_reward_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("card_reward"):
        fired = trace.fired()
        assert len(fired) == 1, (row["game"], row["seq"], trace.rules)
        rule = fired[0]
        if action["action"] == "select_card_reward":
            idx = action["args"]["card_index"]
            assert rule["key"] == "card_reward.pick_best"
            assert trace.score_of("card", idx) == max(trace.ok_scores("card"))
            assert trace.score_of("card", idx) >= rule["values"]["threshold"]
        else:
            assert action["action"] == "skip_card_reward"
            assert rule["key"] in {"card_reward.below_threshold", "card_reward.all_filtered",
                                   "card_reward.all_broken", "card_reward.no_cards"}
            if rule["key"] == "card_reward.below_threshold":
                assert max(trace.ok_scores("card")) < rule["values"]["threshold"]
        _sums_match(trace)
        n += 1
    assert n > 0


def test_card_reward_components_are_the_nine_summed_terms():
    n = 0
    for _row, _action, trace in traced_corpus("card_reward"):
        for opt in trace.to_dict()["options"]:
            if opt["status"] == "ok":
                assert [c["key"] for c in opt["components"]] == [
                    "card.base", "card.set_bonus", "card.tag_synergy", "card.combat_type",
                    "card.boss_defense_gap", "card.boss_readiness", "card.quality_lift",
                    "card.archetype", "card.q_adjust"]
                n += 1
    assert n > 0


HEAL_RULES = {"rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
              "rest.heal_threshold"}
SMITH_RULES = {"rest.must_upgrade", "rest.default_smith"}


def test_rest_site_rules_stop_at_the_deciding_one():
    n = 0
    for row, action, trace in traced_corpus("rest_site"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        assert trace.rules[-1] is fired[0]
        assert all(r["outcome"] == "skipped" for r in trace.rules[:-1])
        key = fired[0]["key"]
        if action["action"] == "choose_option":
            idx = action["args"]["option_index"]
            opt = next(o for o in row["state"]["options"] if o["index"] == idx)
            if key in HEAL_RULES:
                assert "heal" in opt["option_id"].lower()
            elif key in SMITH_RULES:
                assert "smith" in opt["option_id"].lower()
            else:
                assert key == "rest.fallback"
        else:
            assert key == "rest.no_choice"
        n += 1
    assert n > 0


def _rest_state(*, floor, hp, max_hp=80, boss=None, heal_enabled=True,
                smith_enabled=True):
    """A real corpus rest_site state, edited: act-local `floor`, `hp`/`max_hp`,
    optionally another boss, and the HEAL / SMITH option disabled."""
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "rest_site":
                state = copy.deepcopy(row["state"])
                state.pop("floor", None)
                state["context"]["floor"] = floor
                if boss is not None:
                    state["context"]["boss"] = boss
                state["player"]["hp"] = hp
                state["player"]["max_hp"] = max_hp
                for opt in state["options"]:
                    name = opt["option_id"].lower()
                    if "heal" in name:
                        opt["is_enabled"] = heal_enabled
                    elif "smith" in name:
                        opt["is_enabled"] = smith_enabled
                return state
    pytest.fail("no rest_site state in the corpus")


def _option_index(state, name):
    return next(o["index"] for o in state["options"] if name in o["option_id"].lower())


def _vantom_rest_state():
    """A corpus rest_site state moved to the Vantom mid-boss window: a Vantom
    boss, act-local floor 12, HP at 60%."""
    return _rest_state(floor=12, hp=48, boss={"id": "VANTOM_BOSS", "name": "Vantom"})


def test_rest_site_vantom_mid_boss_rule_is_env_gated():
    state = _vantom_rest_state()
    heal = next(o for o in state["options"] if "heal" in o["option_id"].lower())

    # gate closed (the shipped default): the rule is checked and skipped
    trace = GreedyTrace()
    greedy_action(state, trace=trace)
    vantom = next(r for r in trace.rules if r["key"] == "rest.vantom_mid_boss")
    assert vantom["outcome"] == "skipped"
    assert vantom["values"]["enabled"] is False

    # gate open: critical_heal (floor 12 -> 0.40) is skipped at 60% HP, and the
    # Vantom rule is the one that decides. STS2_VANTOM_REST_HEAL is read on every
    # call, so setting it only inside the context is enough; fresh_greedy_state
    # refuses to run with it set, hence it must not leak out.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STS2_VANTOM_REST_HEAL", "1")
        trace = GreedyTrace()
        traced = greedy_action(state, trace=trace)
        untraced = greedy_action(state)
    assert traced == untraced
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": heal["index"]}}
    keys = [(r["key"], r["outcome"]) for r in trace.rules]
    assert keys == [("rest.critical_heal", "skipped"), ("rest.vantom_mid_boss", "fired")]
    vantom = trace.rules[-1]
    assert vantom["values"]["enabled"] is True
    assert vantom["values"]["is_vantom"] is True
    assert vantom["values"]["available"] is True
    assert vantom["values"]["floor"] == 12
    assert abs(vantom["values"]["hp_ratio"] - 0.6) < 0.02
    json.dumps(trace.to_dict(), allow_nan=False)


def test_rest_site_heal_rules_say_when_heal_is_unavailable():
    """10/80 HP at floor 3 is far below the critical threshold, but HEAL is
    disabled: the skipped rule must say so, or it reads as a rule that failed."""
    state = _rest_state(floor=3, hp=10, heal_enabled=False,
                       boss={"id": "CEREMONIAL_BEAST_BOSS", "name": "Ceremonial Beast"})
    smith_index = _option_index(state, "smith")
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": smith_index}}
    json.dumps(trace.to_dict(), allow_nan=False)

    by_key = {r["key"]: r for r in trace.rules}
    critical = by_key["rest.critical_heal"]
    assert critical["outcome"] == "skipped"
    assert critical["values"]["hp_ratio"] == 0.125
    assert critical["values"]["hp_ratio"] < critical["values"]["threshold"]
    assert critical["values"]["available"] is False
    # every heal rule that was checked says HEAL is unavailable (rest.heal_threshold
    # only appears when must_upgrade did not already decide)
    heal_rules = [r for r in trace.rules if r["key"] in HEAL_RULES]
    assert {r["key"] for r in heal_rules} >= {
        "rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal"}
    assert all(r["values"]["available"] is False for r in heal_rules)
    assert by_key["rest.vantom_mid_boss"]["values"]["is_vantom"] is False
    # the SMITH rules decide: must_upgrade if the deck has a target, else default_smith
    fired = trace.fired()
    assert len(fired) == 1 and trace.rules[-1] is fired[0]
    assert fired[0]["key"] in {"rest.must_upgrade", "rest.default_smith"}


def test_rest_site_must_upgrade_says_when_smith_is_unavailable():
    """Full HP, SMITH disabled: must_upgrade cannot fire whatever the deck holds."""
    state = _rest_state(floor=3, hp=80, smith_enabled=False)
    heal_index = _option_index(state, "heal")
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": heal_index}}
    json.dumps(trace.to_dict(), allow_nan=False)

    by_key = {r["key"]: r for r in trace.rules}
    must = by_key["rest.must_upgrade"]
    assert must["outcome"] == "skipped"
    assert must["values"]["available"] is False
    assert must["values"]["deck_index"] is None
    # HEAL is available but full HP clears every heal rule; with no SMITH either,
    # the fallback takes HEAL
    assert all(by_key[k]["values"]["available"] is True for k in (
        "rest.critical_heal", "rest.pre_boss_heal", "rest.heal_threshold"))
    assert by_key["rest.default_smith"]["outcome"] == "skipped"
    assert trace.rules[-1]["key"] == "rest.fallback"
    assert trace.rules[-1]["outcome"] == "fired"
    assert len(trace.fired()) == 1


def test_event_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("event_choice"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        if action["action"] == "choose_option":
            idx = action["args"]["option_index"]
            assert fired[0]["key"] == "event.pick_best"
            assert trace.score_of("option", idx) == max(trace.ok_scores("option"))
        else:
            assert fired[0]["key"] == "event.no_available"
        locked = {o["index"] for o in row["state"]["options"] if o.get("is_locked")}
        for opt in trace.to_dict()["options"]:
            assert (opt["status"] == "locked") == (opt["index"] in locked)
        _sums_match(trace)
        n += 1
    assert n > 0


def test_event_keyword_hits_are_recorded():
    from agent.combat_env import _score_event_option
    hits = []
    # text = "obtain a rare card  lose 5 hp.": lose_hp (-2) fires ("lose" + "hp",
    # no "max"), then rare_card (+8) ("rare" + "card"); the elif'd card_reward
    # branch is skipped. lose_hp precedes rare_card in the source.
    score = _score_event_option(
        {"title": "Obtain a Rare card", "description": "Lose 5 HP."}, hits)
    assert [h[0] for h in hits] == ["event.kw.lose_hp", "event.kw.rare_card"]
    assert score == sum(h[1] for h in hits) == 6.0


def test_event_keyword_hits_follow_source_order_and_cover_every_branch():
    """Hits come out in _score_event_option's source order (= EVENT_KEYWORD_KEYS
    order), each branch at most once; across the corpus every branch fires."""
    from agent.greedy_trace import EVENT_KEYWORD_KEYS
    position = {key: i for i, key in enumerate(EVENT_KEYWORD_KEYS)}
    seen = set()
    for row, _action, trace in traced_corpus("event_choice"):
        for opt in trace.to_dict()["options"]:
            if opt["status"] != "ok":
                continue
            keys = [c["key"] for c in opt["components"]]
            assert all(k in position for k in keys), (row["game"], row["seq"], keys)
            places = [position[k] for k in keys]
            assert places == sorted(set(places)), (row["game"], row["seq"], keys)
            seen.update(keys)
    assert seen == set(EVENT_KEYWORD_KEYS), sorted(set(EVENT_KEYWORD_KEYS) - seen)


def test_bundle_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("bundle_select"):
        fired = trace.fired()
        assert len(fired) == 1
        idx = action["args"]["bundle_index"]
        if fired[0]["key"] == "bundle.pick_best":
            assert trace.score_of("bundle", idx) == max(trace.ok_scores("bundle"))
            _sums_match(trace)
        else:
            assert fired[0]["key"] == "bundle.single" and idx == 0
        n += 1
    assert n > 0


def test_card_select_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("card_select"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        key = fired[0]["key"]
        if action["action"] == "skip_select":
            assert key == "card_select.no_cards"
            continue
        picked = [int(i) for i in action["args"]["indices"].split(",")]
        assert fired[0]["values"]["picked"] == picked
        scores = {o["index"]: o["score"] for o in trace.to_dict()["options"]
                  if o["status"] == "ok"}
        if key in {"card_select.rest_best", "card_select.combat_best"}:
            assert len(picked) == 1
            assert fired[0]["values"]["fallback"] is (not scores)
            if scores:
                assert scores[picked[0]] == max(scores.values())
            else:
                assert picked == [0]
        else:
            assert key in {"card_select.top_scores", "card_select.lowest_scores"}
            ranked = sorted(scores, key=lambda i: scores[i],
                            reverse=(key == "card_select.top_scores"))
            want = [scores[i] for i in ranked[:len(picked)]]
            assert [scores[i] for i in picked] == want
        n += 1
    assert n > 0


def _run_both(state):
    """(action, trace) of a traced greedy_action call, after checking it picks
    exactly what the untraced call picks and that the trace is JSON-safe."""
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    json.dumps(trace.to_dict(), allow_nan=False)
    return traced, trace


def _corpus_card_pool():
    """[(score_card score, card)] for each distinct, non-BROKEN card id in the
    corpus's card_select / card_reward states, ascending by (score, id)."""
    from agent.card_scoring import BROKEN_CARDS, score_card
    by_id = {}
    for row in corpus_rows():
        state = row["state"]
        if state["decision"] in ("card_select", "card_reward"):
            for card in state["cards"]:
                if card["id"].removeprefix("CARD.") not in BROKEN_CARDS:
                    by_id.setdefault(card["id"], card)
    pool = [(score_card(card), card) for card in by_id.values()]
    pool.sort(key=lambda sc: (sc[0], sc[1]["id"]))
    return pool


def _card_select_state(room_type, cards, max_select=1):
    """A real corpus card_select state with its room, cards and max_select replaced."""
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "card_select":
                state = copy.deepcopy(row["state"])
                state["context"]["room_type"] = room_type
                state["cards"] = copy.deepcopy(list(cards))
                state["max_select"] = max_select
                return state
    pytest.fail("no card_select state in the corpus")


def _bundle_state(bundles):
    """A real corpus bundle_select state with its bundles replaced."""
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "bundle_select":
                state = copy.deepcopy(row["state"])
                state["bundles"] = copy.deepcopy(bundles)
                return state
    pytest.fail("no bundle_select state in the corpus")


def _picked(action):
    return [int(i) for i in action["args"]["indices"].split(",")]


def test_card_select_lowest_scores_for_a_long_junk_free_list():
    """More than 10 cards and none scoring < 3: still a deck selection (the
    worst card goes), and has_junk says why it is not the junk rule."""
    from agent.card_scoring import score_card
    by_score = {}
    for sc, c in _corpus_card_pool():
        if sc >= 3.0:
            by_score.setdefault(sc, c)             # one card per distinct score
    assert len(by_score) >= 11, len(by_score)
    cards = [by_score[sc] for sc in sorted(by_score, reverse=True)[:11]]
    scores = [score_card(c) for c in cards]        # best first, worst last
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 11
    assert min(scores) >= 3.0
    action, trace = _run_both(_card_select_state("Event", cards))
    (rule,) = trace.fired()
    assert rule["key"] == "card_select.lowest_scores"
    assert rule["values"]["has_junk"] is False
    assert rule["values"]["n_cards"] == 11
    worst = min(range(len(scores)), key=lambda i: scores[i])
    assert worst == 10 and _picked(action) == [worst]
    assert rule["values"]["picked"] == [worst]
    assert [trace.score_of("card", i) for i in range(11)] == scores


def test_card_select_one_junk_card_makes_a_short_list_a_deck_selection():
    """Exactly 10 cards, one scoring < 3: has_junk alone triggers lowest_scores."""
    from agent.card_scoring import score_card
    pool = _corpus_card_pool()
    junk = next(c for sc, c in pool if sc < 3.0)
    good = [c for sc, c in pool if sc >= 3.0][-9:]
    cards = good[:4] + [junk] + good[4:]
    assert len(cards) == 10
    scores = [score_card(c) for c in cards]
    assert [i for i, sc in enumerate(scores) if sc < 3.0] == [4]
    action, trace = _run_both(_card_select_state("Shop", cards))
    (rule,) = trace.fired()
    assert rule["key"] == "card_select.lowest_scores"
    assert rule["values"]["has_junk"] is True
    assert _picked(action) == [4] and rule["values"]["picked"] == [4]


def test_card_select_ties_keep_the_lower_index_first():
    from agent.card_scoring import score_card
    pool = _corpus_card_pool()
    junk = next(c for sc, c in pool if sc < 3.0)          # score < 3
    mid = next(c for sc, c in pool if 3.0 <= sc < 9.0)
    high = pool[-1][1]
    assert score_card(junk) < score_card(mid) < score_card(high)

    # lowest N: three tied junk cards (0, 2, 4), then the tied mid cards (1, 3)
    cards = [junk, mid, junk, mid, junk]
    action, trace = _run_both(_card_select_state("Event", cards, max_select=4))
    (rule,) = trace.fired()
    assert rule["key"] == "card_select.lowest_scores" and rule["values"]["has_junk"] is True
    assert _picked(action) == [0, 2, 4, 1] == rule["values"]["picked"]

    # highest N: no junk, tied high cards (0, 2, 4), then the tied mid cards (1, 3)
    cards = [high, mid, high, mid, high]
    action, trace = _run_both(_card_select_state("Event", cards, max_select=4))
    (rule,) = trace.fired()
    assert rule["key"] == "card_select.top_scores" and rule["values"]["has_junk"] is False
    assert _picked(action) == [0, 2, 4, 1] == rule["values"]["picked"]


@pytest.mark.parametrize("room_type, rule_key", [
    ("RestSite", "card_select.rest_best"), ("Monster", "card_select.combat_best")])
def test_card_select_all_broken_cards_fall_back_to_index_zero(room_type, rule_key):
    from agent.card_scoring import BROKEN_CARDS
    pool = _corpus_card_pool()
    template = pool[-1][1]
    broken = []
    for name in sorted(BROKEN_CARDS):
        card = copy.deepcopy(template)
        card["id"] = "CARD." + name
        broken.append(card)
    assert len(broken) >= 2
    action, trace = _run_both(_card_select_state(room_type, broken))
    (rule,) = trace.fired()
    assert rule["key"] == rule_key
    assert rule["values"]["fallback"] is True
    assert rule["values"]["picked"] == [0] and _picked(action) == [0]
    assert {o["status"] for o in trace.to_dict()["options"]} == {"broken"}
    assert trace.ok_scores("card") == []

    # a broken card in front of a playable one is skipped, and that is no fallback
    action, trace = _run_both(_card_select_state(room_type, [broken[0], template]))
    (rule,) = trace.fired()
    assert rule["values"]["fallback"] is False
    assert rule["values"]["picked"] == [1] and _picked(action) == [1]
    assert [o["status"] for o in trace.to_dict()["options"]] == ["broken", "ok"]


def _real_bundle_cards():
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "bundle_select":
                cards = row["state"]["bundles"][0]["cards"]
                assert len(cards) >= 2
                return copy.deepcopy(cards)
    pytest.fail("no bundle_select state in the corpus")


def test_bundle_equal_sums_pick_the_first_bundle():
    a, b = _real_bundle_cards()[:2]
    bundles = [{"index": 0, "cards": [a, b]}, {"index": 1, "cards": [b, a]}]
    action, trace = _run_both(_bundle_state(bundles))
    assert action["args"] == {"bundle_index": 0}
    (rule,) = trace.fired()
    assert rule["key"] == "bundle.pick_best" and rule["values"]["best_index"] == 0
    assert trace.score_of("bundle", 0) == trace.score_of("bundle", 1)
    _sums_match(trace)


def test_bundle_single_still_shows_what_the_bundle_holds():
    from agent.card_scoring import score_card
    cards = _real_bundle_cards()
    action, trace = _run_both(_bundle_state([{"index": 0, "cards": cards}]))
    assert action["args"] == {"bundle_index": 0}
    (rule,) = trace.fired()
    assert rule["key"] == "bundle.single" and rule["values"]["n_bundles"] == 1
    scores = [score_card(c) for c in cards]
    assert trace.score_of("bundle", 0) == sum(scores)
    (opt,) = trace.to_dict()["options"]
    assert opt["kind"] == "bundle" and opt["status"] == "ok"
    assert [c["key"] for c in opt["components"]] == ["bundle.card"] * len(cards)
    assert [c["value"] for c in opt["components"]] == scores
    _sums_match(trace)


def test_bundle_none_records_only_the_rule():
    action, trace = _run_both(_bundle_state([]))
    assert action["args"] == {"bundle_index": 0}
    assert trace.to_dict()["options"] == []
    (rule,) = trace.fired()
    assert rule["key"] == "bundle.single" and rule["values"]["n_bundles"] == 0


SHOP_RULES = ["shop.heal_potion", "shop.remove_basic", "shop.remove_value",
              "shop.card_must_buy", "shop.card_threshold", "shop.relic",
              "shop.potion", "shop.leave"]
SHOP_ACTION = {"shop.heal_potion": "buy_potion", "shop.remove_basic": "remove_card",
               "shop.remove_value": "remove_card", "shop.card_must_buy": "buy_card",
               "shop.card_threshold": "buy_card", "shop.relic": "buy_relic",
               "shop.potion": "buy_potion", "shop.leave": "leave_room"}


def test_shop_rule_chain_matches_action():
    n = 0
    for row, action, trace in traced_corpus("shop"):
        keys = [r["key"] for r in trace.rules]
        fired = trace.fired()
        assert len(fired) == 1 and trace.rules[-1] is fired[0], keys
        assert keys == SHOP_RULES[:len(keys)]
        assert SHOP_ACTION[fired[0]["key"]] == action["action"]
        if action["action"] == "buy_card":
            idx = action["args"]["card_index"]
            assert trace.score_of("card", idx) == max(trace.ok_scores("card"))
        n += 1
    assert n > 0


def test_shop_remove_basic_reports_its_ironclad_only_scope():
    for row, _action, trace in traced_corpus("shop"):
        rule = next((r for r in trace.rules if r["key"] == "shop.remove_basic"), None)
        if rule is not None:
            assert rule["values"]["basic_scope"] == "ironclad_only"
            return
    pytest.fail("no shop state reached shop.remove_basic")


def _non_ironclad_shop_state(character_prefix, *, gold=500, removal_cost=75):
    """A real corpus shop state from a `character_prefix` game ("Silent",
    "Defect"), edited so the deck holds >= 3 of that character's own basic
    Strike/Defend, with plenty of gold and a removal cost (so the basic purge
    would fire if it were not scoped to Ironclad's two card ids)."""
    own = {"Silent": ("STRIKE_SILENT", "DEFEND_SILENT"),
           "Defect": ("STRIKE_DEFECT", "DEFEND_DEFECT")}[character_prefix]
    for game, rows in corpus_games():
        if not game.startswith(character_prefix):
            continue
        for row in rows:
            if row["state"]["decision"] == "shop":
                state = copy.deepcopy(row["state"])
                deck = state["player"]["deck"]
                for card, cid in zip(deck[:4], (own[0], own[0], own[1], own[1])):
                    card["id"] = "CARD." + cid
                state["player"]["gold"] = gold
                state["card_removal_cost"] = removal_cost
                state["player"]["hp"] = state["player"]["max_hp"]  # no heal-potion rule
                return state
    pytest.fail(f"no {character_prefix} shop state in the corpus")


@pytest.mark.parametrize("character", ["Silent", "Defect"])
def test_shop_remove_basic_is_skipped_for_other_characters_basics(character):
    """The basic purge only counts Ironclad's STRIKE/DEFEND ids (the A/B-measured
    configuration), so another character's deck of basics reports n_basic == 0
    and the rule is skipped, whatever the gold. Shown as-is."""
    state = _non_ironclad_shop_state(character)
    assert sum(1 for c in state["player"]["deck"]
               if c["id"].split(".")[-1].startswith(("STRIKE_", "DEFEND_"))
               and not c["id"].endswith("_IRONCLAD")) >= 3
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    json.dumps(trace.to_dict(), allow_nan=False)

    rule = next(r for r in trace.rules if r["key"] == "shop.remove_basic")
    assert rule["outcome"] == "skipped"
    assert rule["values"]["basic_scope"] == "ironclad_only"
    assert rule["values"]["n_basic"] == 0
    assert rule["values"]["removal_cost"] == 75
    assert rule["values"]["gold"] == 500

    # STS2_BASIC_PURGE_ALL widens the scope: the same state then fires the rule.
    # It is read on every call; fresh_greedy_state refuses to run with it set, so
    # it must not leak out of the context.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STS2_BASIC_PURGE_ALL", "1")
        wide = GreedyTrace()
        wide_action = greedy_action(state, trace=wide)
        assert wide_action == greedy_action(state)
    assert wide_action == {"cmd": "action", "action": "remove_card"}
    rule = next(r for r in wide.rules if r["key"] == "shop.remove_basic")
    assert rule["outcome"] == "fired"
    assert rule["values"]["basic_scope"] == "all"
    assert rule["values"]["n_basic"] >= 3


def test_map_evidence_matches_choice():
    n = 0
    unhandled = 0
    for row, action, trace in traced_corpus("map_select"):
        fired = trace.fired()
        if action["action"] != "select_map_node":
            # no choices to pick from: greedy falls through to its proceed default
            assert [r["key"] for r in fired] == ["greedy.unhandled"]
            assert not row["state"].get("choices")
            unhandled += 1
            continue
        assert [r["key"] for r in fired] == ["map.pick_min_priority"]
        args = action["args"]
        idx = next(i for i, c in enumerate(row["state"]["choices"])
                   if c["col"] == args["col"] and c["row"] == args["row"])
        assert trace.score_of("node", idx) == min(trace.ok_scores("node"))
        assert fired[0]["values"]["best_index"] == idx
        assert idx == min(fired[0]["values"]["tied_best"])
        _sums_match(trace)
        n += 1
    assert n > 0
    assert unhandled > 0  # the corpus' empty-choices row
