"""Offline label contracts and ranking math; these are synthetic unit fixtures."""
from copy import deepcopy
import json

import numpy as np
import pytest

from agent import offline_fool_ranker as ranker


def make_case(seed="seed-0", number=0, damage=9):
    cards = [
        {"id": "CARD.TEST_ATTACK", "index": 0, "cost": 1, "type": "Attack", "rarity": "Common", "stats": {"damage": damage}},
        {"id": "CARD.TEST_BLOCK", "index": 1, "cost": 1, "type": "Skill", "rarity": "Common", "stats": {"block": 8}},
    ]
    state = {"decision": "card_reward", "can_skip": True, "cards": cards,
             "context": {"act": 1, "floor": number + 2},
             "player": {"hp": 45, "max_hp": 70, "gold": 80,
                        "deck": [{"id": "CARD.STRIKE_FOOL", "cost": 1, "type": "Attack", "stats": {"damage": 6}}] * 5,
                        "relics": [], "potions": []}}
    future_state = {"decision": "combat_play", "context": {"act": 1, "floor": number + 3, "room_type": "Monster"},
                    "enemies": [{"id": "TEST_ENEMY", "hp": 20}], "hand": [{"id": "CARD.TEST_ATTACK"}]}
    future_hash = ranker.state_digest(future_state)
    combat_identity = {"act": 1, "floor": number + 3, "room_type": "Monster", "enemyIds": ["TEST_ENEMY"]}
    case = {"schema_version": 1, "case_id": f"{seed}-{number}", "seed": seed,
            "character": "Fool", "ascension": 9, "horizon": "next_combat",
            "state": state, "state_sha256": ranker.state_digest(state), "restore_verified": True,
            "engine": {"game_version": "v0.113.1", "dll_sha256": "a" * 64, "cli_sha256": "b" * 64, "policy_commit": "test-only"},
            "future_control_verified": True, "future_control_initial_sha256": future_hash,
            "future_control_initial_state": future_state, "future_control_combat_identity": combat_identity,
            "future_control_option_index": 0, "continuation_commands": [],
            "future_control_checks": [future_hash] * 3, "restore_checks": [ranker.state_digest(state)] * 3,
            "continuation_id": "fixed-next-room", "heuristic_option_index": 0,
            "heuristic_option_id": cards[0]["id"], "options": [], "results": []}
    for c in cards:
        case["options"].append({"id": c["id"], "index": c["index"], "card": c,
                                "command": {"cmd": "action", "action": "select_card_reward", "args": {"card_index": c["index"]}}})
    case["options"].append({"id": "SKIP", "index": -1, "card": None,
                            "command": {"cmd": "action", "action": "skip_card_reward"}})
    for opt in case["options"]:
        for rep in range(3):
            case["results"].append({"option_id": opt["id"], "option_index": opt["index"], "repeat": rep,
                "seed": seed, "case_id": case["case_id"], "horizon": "next_combat",
                "continuation_id": case["continuation_id"], "budget_ms": 3500,
                "restore_verified": True, "restore_sha256": case["state_sha256"],
                "engine_dll_sha256": case["engine"]["dll_sha256"], "engine_cli_sha256": case["engine"]["cli_sha256"],
                "combat_initial_sha256": future_hash if opt["index"] == 0 else "d" * 64, "status": "completed",
                "survived": True, "hp_start": 45, "max_hp_start": 70,
                "hp_end": {0: 38, 1: 30, -1: 20}[opt["index"]], "max_hp_end": 70,
                "plans": 2, "solver_actions": 4, "rounds": 2,
                "combat_identity": deepcopy(combat_identity)})
    return case


def test_features_exclude_seed_results_card_ids_and_do_not_mutate_state():
    case = make_case()
    original = deepcopy(case)
    features = ranker.option_features(case["state"], case["options"][0])
    other = deepcopy(case)
    other.update(seed="LEAK", results=[{"hp_end": 999}], case_id="LEAK")
    other["state"]["cards"][0]["id"] = "CARD.OTHER_UNKNOWN"
    other["options"][0]["id"] = "CARD.OTHER_UNKNOWN"
    assert features == ranker.option_features(other["state"], other["options"][0])
    assert case == original
    assert all(isinstance(v, float) and np.isfinite(v) for v in features.values())
    assert not any(word in key for key in features for word in ("seed", "result", "option_id", "case_id"))


def test_skip_is_explicit_and_candidate_context_interactions_change_with_hp():
    case = make_case()
    skipped = ranker.option_features(case["state"], case["options"][-1])
    assert skipped["candidate.skip"] == 1
    assert skipped["candidate.attack"] == 0
    before = ranker.option_features(case["state"], case["options"][0])
    case["state"]["player"]["hp"] = 5
    after = ranker.option_features(case["state"], case["options"][0])
    assert before["interaction.attack_x_hp_missing"] != after["interaction.attack_x_hp_missing"]


def test_fool_effects_distinguish_actual_draw_from_sadness_creation():
    state = make_case()["state"]
    sadness = {"index": 0, "id": "CARD.SORROWFUL_STRIKE", "card": {"id": "CARD.SORROWFUL_STRIKE", "stats": {"damage": 13, "cards": 2}}}
    draw = {"index": 0, "id": "CARD.RUMMAGE", "card": {"id": "CARD.RUMMAGE", "stats": {"cards": 3}}}
    assert ranker.option_features(state, sadness)["candidate.draw"] == 0
    assert ranker.option_features(state, sadness)["candidate.sadness"] == 2
    assert ranker.option_features(state, draw)["candidate.draw"] == 3


def test_exact_verified_repetitions_form_only_within_state_pairs():
    labeled, diagnostics = ranker.prepare_cases([make_case()])
    assert diagnostics["accepted_cases"] == 1
    assert len(labeled[0].pairs) == 3
    assert labeled[0].oracle_indices == {0}
    assert labeled[0].option_stats[-1]["valid_repetitions"] == 3
    x, y, weights = ranker.pairwise_matrix(labeled)
    assert x.shape == (6, len(ranker.FEATURE_NAMES))
    assert np.allclose(x[::2], -x[1::2])
    assert y.tolist() == [1, 0, 1, 0, 1, 0]
    assert weights.sum() == pytest.approx(1)


@pytest.mark.parametrize("field,value", [("restore_verified", False), ("restore_sha256", "wrong"),
    ("engine_dll_sha256", "wrong"), ("horizon", "next_boss"), ("continuation_id", "different"),
    ("status", "technical_error"), ("plans", 0), ("solver_actions", 0), ("seed", "another")])
def test_bad_branch_never_becomes_loss_or_label(field, value):
    case = make_case()
    case["results"][0][field] = value
    labeled, diagnostics = ranker.prepare_cases([case])
    assert not labeled
    assert diagnostics["excluded_results"]
    assert diagnostics["rejected_cases"]


def test_bad_state_hash_or_missing_skip_is_rejected():
    for change in ("hash", "skip"):
        case = make_case()
        if change == "hash":
            case["state"]["player"]["gold"] += 1
        else:
            case["options"].pop()
        assert not ranker.prepare_cases([case])[0]


def test_different_next_combat_and_duplicate_repetitions_are_rejected():
    for change in ("combat", "repeat"):
        case = make_case()
        if change == "combat":
            case["results"][0]["combat_identity"]["enemyIds"] = ["OTHER"]
        else:
            case["results"][0]["repeat"] = 1
        assert not ranker.prepare_cases([case])[0]


def test_duplicate_card_ids_use_option_index_not_id():
    case = make_case()
    case["state"]["cards"][1]["id"] = case["options"][1]["id"] = "CARD.TEST_ATTACK"
    case["state_sha256"] = ranker.state_digest(case["state"])
    case["restore_checks"] = [case["state_sha256"]] * 3
    for result in case["results"]:
        result["restore_sha256"] = case["state_sha256"]
        if result["option_index"] == 1:
            result["option_id"] = "CARD.TEST_ATTACK"
    assert len(ranker.prepare_cases([case])[0]) == 1


def test_selects_highest_complete_common_budget_not_mixed_trials():
    case = make_case()
    high = deepcopy(case["results"][:3])
    for row in high:
        row.update(budget_ms=12000, hp_end=1)
    case["results"] += high
    labeled, _ = ranker.prepare_cases([case])
    assert labeled[0].budget_ms == 3500
    assert labeled[0].option_stats[0]["hp_end_mean"] == 38


def test_noisy_repetitions_are_reported_without_false_pair_certainty():
    case = make_case()
    for result in case["results"]:
        result["hp_end"] = [5, 30, 45][result["repeat"]]
    labeled, _ = ranker.prepare_cases([case])
    assert not labeled[0].pairs
    assert labeled[0].option_stats[0]["hp_end_std"] > 0


def test_survival_has_priority_over_hp_and_unstable_outcomes_are_rejected():
    case = make_case()
    for result in case["results"]:
        if result["option_index"] == 0:
            result.update(survived=False, hp_end=0)
    labeled, _ = ranker.prepare_cases([case])
    assert labeled[0].oracle_indices == {1}
    case["results"][0].update(survived=True, hp_end=1)
    assert not ranker.prepare_cases([case])[0]


def test_seed_split_is_order_independent_and_never_splits_a_seed():
    cases, _ = ranker.prepare_cases([make_case(f"seed-{s}", i) for s in range(6) for i in range(4)])
    split = ranker.split_by_seed(cases)
    reversed_split = ranker.split_by_seed(list(reversed(cases)))
    seeds = {part: {case.seed for case in rows} for part, rows in split.items()}
    assert seeds == {part: {case.seed for case in rows} for part, rows in reversed_split.items()}
    assert all(seeds.values())
    assert not (seeds["train"] & seeds["validation"] or seeds["train"] & seeds["test"] or seeds["test"] & seeds["validation"])


def test_metrics_report_expected_random_and_skip_regret_in_hp_and_outcome():
    cases, _ = ranker.prepare_cases([make_case()])
    report = ranker.evaluate(cases, lambda case: {0: 3., 1: 2., -1: 1.})
    assert report["ranker"]["top_choice_agreement"] == 1
    assert report["ranker"]["pairwise_accuracy"] == 1
    assert report["random"]["top_choice_agreement"] == pytest.approx(1 / 3)
    assert report["always_skip"]["hp_regret"] == 18
    assert report["heuristic"]["hp_regret"] == 0
    assert report["always_skip"]["outcome_regret"] == 0


def test_small_or_indistinguishable_dataset_does_not_publish_model():
    model, report = ranker.train_records([make_case()])
    assert model is None
    assert report["validation_status"] == "insufficient_data"
    assert report["scope"] == "next_combat_only"
    tied = [make_case(f"seed-{s}", i) for s in range(6) for i in range(4)]
    for case in tied:
        for row in case["results"]:
            row["hp_end"] = 30
    assert ranker.train_records(tied)[0] is None


def test_regularized_fit_is_json_only_and_has_seed_held_out_evaluation():
    records = [make_case(f"seed-{s}", i, damage=8 + i) for s in range(6) for i in range(4)]
    model, report = ranker.train_records(records)
    assert model["offline_only"] is True
    assert model["model_type"] == "pairwise_logistic_l2"
    assert report["splits"]["test"]["states"] == 4
    assert report["metrics"]["test"]["ranker"]["top_choice_agreement"] == 1
    assert len(model["coefficients"]) == len(ranker.FEATURE_NAMES)
    assert ranker.score_options(records[0], json.loads(json.dumps(model)))[0] > ranker.score_options(records[0], model)[-1]
    assert "win_rate_improvement" not in report


@pytest.mark.parametrize("field,value", [("combat_identity", "not-a-combat"),
    ("combat_identity", {"act": 1, "floor": 2}), ("hp_end", True),
    ("technical_failure_kind", "engine_exception"), ("plans", 1.5), ("budget_ms", True)])
def test_malformed_completed_branch_is_not_accepted(field, value):
    case = make_case()
    for result in case["results"]:
        result[field] = value
    assert not ranker.prepare_cases([case])[0]


def test_insufficient_cli_writes_a_report_and_no_model(tmp_path, monkeypatch):
    output = tmp_path / "experiment"
    monkeypatch.setattr(ranker, "DEFAULT_OUTPUT", output)
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps(make_case()) + "\n")
    assert ranker.main([str(dataset)]) == 2
    reports = list(output.glob("report-*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["model_written"] is False
    assert not list(output.glob("ranker-*.json"))
    with pytest.raises(SystemExit):
        ranker.main([str(dataset), "--out", str(tmp_path / "online")])


def test_engine_version_and_mixed_cohort_are_not_silently_pooled():
    case = make_case()
    case["engine"]["game_version"] = "v0.111.0"
    assert not ranker.prepare_cases([case])[0]
    records = [make_case(f"seed-{s}", i) for s in range(6) for i in range(4)]
    records[0]["engine"]["policy_commit"] = "different-continuation-policy"
    model, report = ranker.train_records(records)
    assert model is None
    assert "fingerprints differ" in report["reason"]


def test_future_control_restore_proof_is_required():
    for field, value in (("future_control_verified", False), ("future_control_initial_sha256", None),
                         ("future_control_checks", ["d" * 64] * 3), ("restore_checks", ["f" * 64] * 3)):
        case = make_case()
        case[field] = value
        assert not ranker.prepare_cases([case])[0]


@pytest.mark.parametrize("field,value", [("engine_cli_sha256", "wrong"), ("combat_initial_sha256", None)])
def test_compiled_engine_and_initial_combat_fingerprints_are_required(field, value):
    case = make_case()
    for result in case["results"]:
        result[field] = value
    assert not ranker.prepare_cases([case])[0]


def test_same_option_must_reveal_identical_initial_combat_in_every_repeat():
    case = make_case()
    case["results"][0]["combat_initial_sha256"] = "e" * 64
    assert not ranker.prepare_cases([case])[0]


def test_three_identical_wrong_future_controls_do_not_prove_original_state():
    case = make_case()
    case["future_control_initial_sha256"] = "e" * 64
    case["future_control_checks"] = ["e" * 64] * 3
    for result in case["results"]:
        if result["option_index"] == 0:
            result["combat_initial_sha256"] = "e" * 64
    assert not ranker.prepare_cases([case])[0]


def test_original_full_combat_hash_detects_changed_hand():
    case = make_case()
    case["future_control_initial_state"]["hand"] = [{"id": "CARD.OTHER_DRAW"}]
    assert not ranker.prepare_cases([case])[0]


def test_original_control_identity_must_match_its_full_state():
    case = make_case()
    case["future_control_combat_identity"]["enemyIds"] = ["OTHER_ENEMY"]
    for result in case["results"]:
        result["combat_identity"]["enemyIds"] = ["OTHER_ENEMY"]
    assert not ranker.prepare_cases([case])[0]


def test_identical_branch_encounters_must_match_original_process_control():
    case = make_case()
    for result in case["results"]:
        result["combat_identity"]["floor"] += 1
    assert not ranker.prepare_cases([case])[0]


@pytest.mark.parametrize("field,value", [("future_control_initial_state", None),
    ("future_control_option_index", -1), ("continuation_commands", None)])
def test_original_control_evidence_must_be_complete(field, value):
    case = make_case()
    case[field] = value
    assert not ranker.prepare_cases([case])[0]


def test_original_anchor_must_be_a_combat_not_a_matching_noncombat_hash():
    case = make_case()
    case["future_control_initial_state"]["decision"] = "map_select"
    future_hash = ranker.state_digest(case["future_control_initial_state"])
    case["future_control_initial_sha256"] = future_hash
    case["future_control_checks"] = [future_hash] * 3
    for result in case["results"]:
        if result["option_index"] == 0:
            result["combat_initial_sha256"] = future_hash
    assert not ranker.prepare_cases([case])[0]


def test_control_branch_initial_observation_must_match_original_full_state():
    case = make_case()
    for result in case["results"]:
        if result["option_index"] == 0:
            result["combat_initial_sha256"] = "f" * 64
    assert not ranker.prepare_cases([case])[0]


def test_frozen_original_control_can_be_skip():
    case = make_case()
    case.update(heuristic_option_index=-1, heuristic_option_id="SKIP", future_control_option_index=-1)
    for result in case["results"]:
        result["combat_initial_sha256"] = case["future_control_initial_sha256"] if result["option_index"] == -1 else "d" * 64
    assert len(ranker.prepare_cases([case])[0]) == 1


def factual_option(cid, **overrides):
    card = {"id": "CARD." + cid, **overrides}
    return {"id": card["id"], "index": 0, "card": card}


def test_real_powers_have_factual_timing_and_cho_requirement():
    state = make_case()["state"]
    roots = ranker.option_features(state, factual_option("HIDDEN_ROOTS"))
    song = ranker.option_features(state, factual_option("SONG_PATH"))
    assert roots != song
    assert roots["candidate.start_turn_stretch"] == 3
    assert roots["candidate.defense"] == 0  # Stretch is not Block already gained.
    assert song["candidate.on_cho_block"] == 5
    assert song["candidate.requires_cho"] == 1
    assert song["interaction.required_cho_missing"] == 1
    state["player"]["deck"].append({"id": "CARD.CHO_READY"})
    supported = ranker.option_features(state, factual_option("SONG_PATH"))
    assert supported["candidate.on_cho_block"] == 5
    assert supported["interaction.required_cho_missing"] == 0
    assert supported["interaction.cho_trigger_support"] > 0


def test_factual_vars_apply_upgrades_and_runtime_override_without_value_helpers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("heuristic value helper was used")
    for name in ("effects", "base_score", "score_in_deck"):
        monkeypatch.setattr(ranker.fool_scoring, name, forbidden)
    state = make_case()["state"]
    assert ranker.option_features(state, factual_option("SONG_PATH", upgraded=True))["candidate.on_cho_block"] == 7
    assert ranker.option_features(state, factual_option("SONG_PATH", upgraded=True, stats={"songpathpower": 9}))["candidate.on_cho_block"] == 9
    archery = ranker.option_features(state, factual_option("ARCHERY"))
    assert archery["candidate.start_turn_aim"] == 4
    assert archery["candidate.aim"] == 0
    motif = ranker.option_features(state, factual_option("MOTIF"))
    assert motif["candidate.on_melody_draw"] == 1
    assert motif["candidate.draw"] == 0


def test_factual_arrow_and_sadness_mechanisms_have_amounts_not_scores():
    state = make_case()["state"]
    assert ranker.option_features(state, factual_option("QUIVER", upgraded=True))["candidate.arrow_capacity"] == 2
    assert ranker.option_features(state, factual_option("QUIETUS"))["candidate.next_shot_multiplier"] == 2
    assert ranker.option_features(state, factual_option("HONING"))["candidate.arrow_damage_bonus_pct"] == 50
    assert ranker.option_features(state, factual_option("ALL_IS_WELL"))["candidate.on_sadness_block"] == 4
    assert ranker.option_features(state, factual_option("GROWTH"))["candidate.stretch_per_sadness_exhausted"] == 4
    assert ranker.option_features(state, factual_option("ANGUISH"))["candidate.damage_per_sadness_created"] == 6
    later = ranker.option_features(state, factual_option("KI_REST"))
    assert later["candidate.energy_next_turn"] == 5
    assert later["candidate.energy"] == 0
    inspected = ranker.option_features(state, factual_option("KI_CHANT"))
    assert inspected["candidate.look_at_top"] == 3
    assert inspected["candidate.draw"] == 0


def test_key_persistent_powers_no_longer_collapse_to_identical_effect_vectors():
    state = make_case()["state"]
    powers = ("HIDDEN_ROOTS", "SONG_PATH", "MOTIF", "LULLABY", "ARCHERY", "LOST_LUTE", "MYTHIC_FORM",
              "QUIVER", "COMPOST", "COVERING_FIRE", "ALL_IS_WELL", "ECSTASY", "ENNUI", "SIMPLETON",
              "HONING", "SERRATED_ARROWS", "EXPLOSIVE_ARROWS", "YAH", "TUNE")
    vectors = [tuple(ranker.option_features(state, factual_option(cid)).values()) for cid in powers]
    assert len(set(vectors)) == len(powers)


@pytest.mark.parametrize("relic", ["RELIC.LANTERN", "Lantern", "lantern", "灯笼", "Philosopher’s Stone",
    {"id": "RELIC.LANTERN"}, {"name": "Lantern"}, {"name": {"en": "Lantern", "zh": "灯笼"}},
    {"en": "Lantern", "zh": "灯笼"}, {"en": "Philosopher's Stone", "zh": "贤者之石"}])
def test_energy_relic_supports_real_name_only_observations(relic):
    case = make_case()
    case["state"]["player"]["relics"] = [relic]
    assert ranker.option_features(case["state"], case["options"][0])["context.energy_relic"] == 1


@pytest.mark.parametrize("relic", ["Not a Lantern", "my_LANTERN_mod", "Stone", "random",
    {"en": "Lantern-like object", "zh": "未知物品"}, {"id": "RELIC.UNKNOWN"}])
def test_unknown_relic_names_do_not_create_energy_false_positives(relic):
    case = make_case()
    case["state"]["player"]["relics"] = [relic]
    assert ranker.option_features(case["state"], case["options"][0])["context.energy_relic"] == 0


def test_feature_collision_diagnostics_expose_conflicting_distinct_options():
    case = make_case()
    other = dict(case["state"]["cards"][0], index=1, id="CARD.OTHER_ATTACK")
    case["state"]["cards"][1] = other
    case["options"][1].update(id=other["id"], card=other)
    case["state_sha256"] = ranker.state_digest(case["state"])
    case["restore_checks"] = [case["state_sha256"]] * 3
    for result in case["results"]:
        result["restore_sha256"] = case["state_sha256"]
        if result["option_index"] == 1:
            result["option_id"] = other["id"]
    model, report = ranker.train_records([case])
    assert model is None
    collisions = report["feature_collisions"]["within_decision"]
    assert collisions[0]["option_indices"] == [0, 1]
    assert collisions[0]["confident_label_conflict"] is True
    assert report["feature_version"] == 2
    assert report["mechanism_provenance"]["game_version"] == "v0.113.1"


@pytest.mark.parametrize("kind", ["stubs", "localization"])
def test_optional_companion_engine_hashes_must_match(kind):
    case = make_case()
    case["engine"][f"{kind}_sha256"] = "e" * 64
    for result in case["results"]:
        result[f"engine_{kind}_sha256"] = "e" * 64
    assert len(ranker.prepare_cases([case])[0]) == 1
    case["results"][0][f"engine_{kind}_sha256"] = "f" * 64
    assert not ranker.prepare_cases([case])[0]


def test_mechanism_parameter_references_exist_in_checked_metadata():
    for cid, facts in ranker._CARD_FACTS.items():
        for source in facts.values():
            if isinstance(source, str):
                assert source in ranker.fool_scoring.METADATA[cid]["stats"], (cid, source)
            else:
                assert source in (1, 2)  # Direct text counts/multipliers, no assigned value priors.
    assert len(ranker.FEATURE_NAMES) == len(set(ranker.FEATURE_NAMES))
