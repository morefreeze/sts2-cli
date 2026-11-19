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
    row={'restore_verified':True,'combat_provenance_verified':True,'restore_sha256':'abc','status':'completed','plans':1,'solver_actions':2,'engine_dll_sha256':'game','engine_cli_sha256':'cli','continuation_id':'path','combat_identity':{'floor':2}}
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


def phase_state(kind, room='Monster', floor=3, **extra):
    return {'type':'decision','decision':kind,'context':{'act':1,'floor':floor,'room_type':room},**extra}


def phase_step(guard, command, observation, origin='ooc'):
    guard.before(command, origin)
    before=guard.snapshot()
    guard.after(command, origin, observation)
    return {'command':command,'observation':observation,'origin':origin,
            'phase_before':before,'phase_after':guard.snapshot()}


@pytest.mark.parametrize('kind,extra',[('card_select',{}),('bundle_select',{}),('card_reward',{'from_event':True})])
def test_start_of_combat_prompt_cannot_be_hand_picked_or_frozen(kind,extra):
    from tools.collect_fool_a9 import PhaseGuard,action
    guard=PhaseGuard()
    phase_step(guard,{'cmd':'start_run'},phase_state('map_select','Map'))
    phase_step(guard,action('select_map_node',row=2,col=0),phase_state(kind,**extra))
    with pytest.raises(ValueError,match='unproven combat prompt'):
        guard.require_ooc(guard.last_observation)
    with pytest.raises(ValueError,match='unproven combat prompt'):
        guard.before(action('select_cards',indices='0'),'ooc')


@pytest.mark.parametrize('kind,extra',[('card_select',{}),('bundle_select',{}),('card_reward',{'from_event':True})])
def test_unresolved_solver_prompt_is_never_a_survived_label(kind,extra):
    from tools.collect_fool_a9 import PhaseGuard,action
    guard=PhaseGuard()
    phase_step(guard,{'cmd':'start_run'},phase_state('combat_play'))
    result=phase_state(kind,**extra)
    phase_step(guard,action('end_turn'),result,'solver')
    with pytest.raises(ValueError,match='unresolved Solver combat prompt'):
        guard.solver_boundary(result)


def test_postcombat_certificate_allows_reward_and_kifuda_but_not_next_fight():
    from tools.collect_fool_a9 import PhaseGuard,action,audit_records
    guard=PhaseGuard();rows=[]
    rows.append(phase_step(guard,{'cmd':'start_run'},phase_state('combat_play')))
    reward=phase_state('card_reward',can_skip=True)
    rows.append(phase_step(guard,action('play_card',card_index=0),reward,'solver'))
    guard.solver_boundary(reward);guard.require_ooc(reward)
    kifuda=phase_state('card_select')
    rows.append(phase_step(guard,action('select_card_reward',card_index=0),kifuda))
    guard.require_ooc(kifuda)
    rows.append(phase_step(guard,action('select_cards',indices='0'),phase_state('map_select','Map')))
    proof=audit_records(rows)
    assert proof['manual_combat_choices']==0 and proof['solver_commands']==1
    phase_step(guard,action('select_map_node',row=3,col=0),phase_state('card_select',floor=4))
    with pytest.raises(ValueError,match='unproven combat prompt'):
        guard.require_ooc(guard.last_observation)


def test_same_floor_event_subcombat_resets_postcombat_certificate():
    from tools.collect_fool_a9 import PhaseGuard,action
    guard=PhaseGuard()
    phase_step(guard,{'cmd':'start_run'},phase_state('combat_play'))
    event=phase_state('event_choice','Event')
    phase_step(guard,action('end_turn'),event,'solver');guard.solver_boundary(event)
    phase_step(guard,action('choose_option',option_index=0),phase_state('card_reward',from_event=True))
    with pytest.raises(ValueError,match='unproven combat prompt'):
        guard.require_ooc(guard.last_observation)


def test_neow_event_selection_is_ooc_and_unknown_prompt_fails_closed():
    from tools.collect_fool_a9 import PhaseGuard,action
    guard=PhaseGuard()
    phase_step(guard,{'cmd':'start_run'},phase_state('event_choice','Event',floor=1))
    phase_step(guard,action('choose_option',option_index=0),phase_state('card_select','Event',floor=1))
    guard.require_ooc(guard.last_observation)
    with pytest.raises(ValueError,match='unknown prompt room'):
        guard.require_ooc({'decision':'card_select','context':{}})


def test_provenance_audit_rejects_manual_combat_actions_and_forged_phase():
    from tools.collect_fool_a9 import PhaseGuard,action,audit_records
    guard=PhaseGuard()
    row=phase_step(guard,{'cmd':'start_run'},phase_state('combat_play'))
    with pytest.raises(ValueError,match='outside Solver'):
        guard.before(action('play_card',card_index=0),'ooc')
    broken=copy.deepcopy(row);broken['phase_after']['postcombat_room']=[1,3,'Monster']
    with pytest.raises(ValueError,match='phase provenance mismatch'):
        audit_records([broken])
    broken=copy.deepcopy(row);broken.pop('origin')
    with pytest.raises(ValueError,match='missing command provenance'):
        audit_records([broken])


def test_out_of_combat_fruit_juice_use_retains_auditable_ooc_provenance():
    from tools.collect_fool_a9 import PhaseGuard,action,audit_records
    guard=PhaseGuard()
    before=phase_state('map_select','Map',player={
        'hp':55,'max_hp':77,'potions':[{'index':0,'id':'POTION.FRUIT_JUICE'}]})
    after=phase_state('map_select','Map',player={'hp':60,'max_hp':82,'potions':[]})
    rows=[phase_step(guard,{'cmd':'start_run'},before)]
    rows.append(phase_step(guard,action('use_potion',potion_index=0),after))
    proof=audit_records(rows)
    assert proof['ooc_commands']==2
    assert proof['manual_combat_choices']==0
    assert guard.combat_active is False


def test_postcombat_fruit_juice_use_preserves_reward_certificate():
    from tools.collect_fool_a9 import PhaseGuard,action,audit_records
    guard=PhaseGuard()
    rows=[phase_step(guard,{'cmd':'start_run'},phase_state('combat_play'))]
    reward=phase_state('card_reward',can_skip=True)
    rows.append(phase_step(guard,action('end_turn'),reward,'solver'))
    rows.append(phase_step(guard,action('use_potion',potion_index=0),reward))
    guard.require_ooc(reward)
    assert guard.postcombat_room==guard.room_key(reward)
    assert audit_records(rows)['manual_combat_choices']==0


@pytest.mark.parametrize('kind,extra',[
    ('combat_play',{}),('card_select',{}),('card_reward',{'from_event':True})])
def test_ooc_potion_use_cannot_answer_combat_or_unproven_opening_prompts(kind,extra):
    from tools.collect_fool_a9 import PhaseGuard,action
    guard=PhaseGuard()
    phase_step(guard,{'cmd':'start_run'},phase_state('map_select','Map'))
    phase_step(guard,action('select_map_node',row=3,col=0),phase_state(kind,**extra))
    with pytest.raises(ValueError,match='outside Solver|unproven combat prompt'):
        guard.before(action('use_potion',potion_index=0),'ooc')
