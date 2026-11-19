"""Collection correctness: exact state identity and actual reward consequences."""
import copy
import pytest
from tools.collect_fool_a9 import canonical_hash, verify_observation, reward_options, verify_reward_transition, result_is_training_label


def state():
    return {'decision':'card_reward','context':{'act':1,'floor':3},'can_skip':True,
            'cards':[{'index':0,'id':'CARD.KI_HOP','upgraded':False},
                     {'index':1,'id':'CARD.KI_HOP','upgraded':True}],
            'player':{'hp':55,'max_hp':77,'gold':101,'deck':[{'id':'CARD.STRIKE_FOOL','upgraded':False}]}}


def test_hash_preserves_array_order_and_ignores_object_key_order():
    a=state(); b={k:a[k] for k in reversed(a)}
    assert canonical_hash(a)==canonical_hash(b)
    b=copy.deepcopy(a);b['cards'].reverse()
    assert canonical_hash(a)!=canonical_hash(b)


@pytest.mark.parametrize('field,value',[('hp',54),('gold',100),('deck',[])])
def test_restoration_rejects_observation_drift(field,value):
    a=state();b=copy.deepcopy(a);b['player'][field]=value
    with pytest.raises(ValueError,match='observation mismatch'):
        verify_observation(a,b)


def test_options_keep_duplicate_card_occurrences_and_skip_separate():
    choices=reward_options(state())
    assert [c['index'] for c in choices]==[0,1,-1]
    assert choices[0]['card']['upgraded'] is False
    assert choices[1]['card']['upgraded'] is True
    assert choices[-1]['command']['action']=='skip_card_reward'


def test_cannot_skip_reward_is_not_offered_a_skip_label():
    s=state();s['can_skip']=False
    assert [o['index'] for o in reward_options(s)]==[0,1]


def test_reward_must_really_enter_deck():
    before=state();after=copy.deepcopy(before)
    with pytest.raises(ValueError,match='reward was not added'):
        verify_reward_transition(before,after,reward_options(before)[1])
    after['player']['deck'].append({'id':'CARD.KI_HOP','upgraded':True})
    verify_reward_transition(before,after,reward_options(before)[1])


def test_skip_must_preserve_deck():
    before=state();after=copy.deepcopy(before)
    verify_reward_transition(before,after,reward_options(before)[-1])
    after['player']['deck'].append({'id':'CARD.KI_HOP','upgraded':False})
    with pytest.raises(ValueError,match='skip changed deck'):
        verify_reward_transition(before,after,reward_options(before)[-1])


@pytest.mark.parametrize('change',[{'restore_verified':False},{'status':'technical_error'},
                                {'plans':0},{'solver_actions':0},{'restore_sha256':'different'},
                                {'engine_dll_sha256':'other'},{'engine_cli_sha256':'other'},
                                {'continuation_id':'other'},{'combat_identity':{'floor':3}}])
def test_unverified_or_unsolved_results_are_not_labels(change):
    case={'state_sha256':'abc','future_control_verified':True,'engine':{'dll_sha256':'game','cli_sha256':'cli'},'continuation_id':'path','future_control_combat_identity':{'floor':2}}
    row={'restore_verified':True,'restore_sha256':'abc','status':'completed','plans':1,'solver_actions':2,'engine_dll_sha256':'game','engine_cli_sha256':'cli','continuation_id':'path','combat_identity':{'floor':2}}
    assert result_is_training_label(case,row)
    assert not result_is_training_label(case,{**row,**change})


def test_future_rng_control_checks_initial_hand_not_just_reward():
    from tools.collect_fool_a9 import verify_future_controls
    verify_future_controls(['same','same','same'])
    with pytest.raises(ValueError,match='future RNG/control drift'):
        verify_future_controls(['same','different','same'])
    with pytest.raises(ValueError,match='three'):
        verify_future_controls(['same','same'])


@pytest.mark.parametrize('victory',[False,True])
def test_technical_game_over_never_becomes_combat_loss(victory):
    from tools.collect_fool_a9 import terminal_outcome
    with pytest.raises(RuntimeError,match='technical engine failure'):
        terminal_outcome({'decision':'game_over','victory':victory,'technical_failure_kind':'stuck'})
    assert terminal_outcome({'decision':'game_over','victory':victory}) is victory


def test_completed_cache_is_preserved_and_identity_checked():
    from tools.collect_fool_a9 import reuse_cached_result
    expected={'case_id':'a','option_index':0,'repeat':1,'budget_ms':3500,'engine_dll_sha256':'game','engine_cli_sha256':'cli','continuation_id':'path'}
    old={**expected,'status':'completed'}
    assert reuse_cached_result(old,expected,retry_errors=True)
    assert not reuse_cached_result({**old,'status':'technical_error'},expected,retry_errors=True)
    with pytest.raises(ValueError,match='cached result identity drift'):
        reuse_cached_result({**old,'engine_cli_sha256':'other'},expected,retry_errors=False)


def test_three_identically_wrong_future_restores_fail_original_anchor():
    from tools.collect_fool_a9 import verify_future_controls
    with pytest.raises(ValueError,match='future RNG/control drift'):
        verify_future_controls(['wrong']*3,original='actual')
