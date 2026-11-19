#!/usr/bin/env python3
"""Isolated Fool A9 reward collection using the native engine and Solver only.

Discovery records a complete command/observation prefix. Every option trial
starts a fresh CLI, verifies EVERY observation (not just visible player stats),
then executes Solver plans to the same next combat. No live-game API is used.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'python')]


def canonical_hash(value):
    payload=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.pending')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    temp.replace(path)


def verify_observation(reference, restored):
    if canonical_hash(reference)!=canonical_hash(restored):
        keys=[k for k in sorted(set(reference)|set(restored)) if reference.get(k)!=restored.get(k)]
        raise ValueError(f'observation mismatch: {keys}')


def action(name, **args):
    result={'cmd':'action','action':name}
    if args:result['args']=args
    return result


def reward_options(state):
    if state.get('decision')!='card_reward':
        raise ValueError('expected a card_reward decision')
    options=[]
    for position,card in enumerate(state.get('cards') or []):
        index=card.get('index',position)
        options.append({'id':card['id'],'index':index,'card':card,
                        'command':action('select_card_reward',card_index=index)})
    if state.get('can_skip') is True:
        options.append({'id':'SKIP','index':-1,'card':None,'command':action('skip_card_reward')})
    if len({o['index'] for o in options})!=len(options):
        raise ValueError('duplicate reward indices')
    return options


def _deck(state):
    return Counter((c.get('id'),bool(c.get('upgraded'))) for c in (state.get('player') or {}).get('deck',[]))


def verify_reward_transition(before,after,option):
    if after.get('type')=='error':
        raise ValueError(f'reward refused: {after.get("message")}')
    old,new=_deck(before),_deck(after)
    if option['index']==-1:
        if old!=new:raise ValueError('skip changed deck')
    else:
        card=option['card'];key=(card['id'],bool(card.get('upgraded')))
        if new[key]!=old[key]+1:
            raise ValueError('reward was not added to the deck exactly once')
        if sum(new.values())!=sum(old.values())+1:
            raise ValueError('reward changed deck size unexpectedly')


def result_is_training_label(case,row):
    return (case.get('future_control_verified') is True
            and row.get('restore_verified') is True and row.get('status')=='completed'
            and row.get('restore_sha256')==case.get('state_sha256')
            and row.get('engine_dll_sha256')==case.get('engine',{}).get('dll_sha256')
            and row.get('engine_cli_sha256')==case.get('engine',{}).get('cli_sha256')
            and row.get('continuation_id')==case.get('continuation_id')
            and row.get('combat_identity')==case.get('future_control_combat_identity')
            and row.get('plans',0)>0 and row.get('solver_actions',0)>0)


def terminal_outcome(state):
    if state.get('technical_failure_kind'):
        raise RuntimeError(f'technical engine failure: {state["technical_failure_kind"]}')
    return bool(state.get('victory')) if state.get('decision')=='game_over' else None


def combat_identity(state):
    ctx=state.get('context') or {}
    return {'act':ctx.get('act'),'floor':ctx.get('floor'),'room_type':ctx.get('room_type'),
            'enemyIds':[e.get('id',e.get('name')) for e in state.get('enemies',[])]}


def reuse_cached_result(old,expected,retry_errors=False):
    keys=('case_id','option_index','repeat','budget_ms','engine_dll_sha256','engine_cli_sha256','continuation_id')
    if any(old.get(k)!=expected.get(k) for k in keys):
        raise ValueError('cached result identity drift')
    return old.get('status')=='completed' or not retry_errors



def player_summary(state):
    p=state.get('player') or {}
    return {k:p.get(k) for k in ('hp','max_hp','gold','deck_size')}


class Client:
    def __init__(self,args,tag):
        from engine_process import EngineProcess
        self.stderr_path=args.output/'native-logs'/f'{tag}.log'
        self.stderr_path.parent.mkdir(parents=True,exist_ok=True)
        self.stderr=self.stderr_path.open('w')
        env={'STS2_GAME_DIR':str(args.game_dir),'STS2_SOLVER_THREADS':'1',
             'STS2_SOLVER_INFERRER':'on','STS2_COLLECTION_BUDGET_MS':str(args.budget_ms),
             'OMP_NUM_THREADS':'1'}
        argv=['/usr/bin/env',*[f'{k}={v}' for k,v in env.items()],str(args.dotnet),str(args.engine)]
        self.engine=EngineProcess(argv,stderr=self.stderr)
        self.timeout=max(45,args.budget_ms/1000+30)
        try:
            ready=self.engine.read_json(60)
            if ready.get('type')!='ready':raise RuntimeError(f'CLI not ready: {ready}')
        except BaseException:
            self.close();raise

    def send(self,cmd):
        self.engine.write(cmd)
        result=self.engine.read_json(self.timeout)
        terminal_outcome(result)
        if result.get('type')=='error':raise RuntimeError(f'CLI refused {cmd}: {result.get("message")}')
        return result

    def close(self):
        self.engine.close();self.stderr.close()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def initialize_policy():
    # Match the live heuristic, without importing or communicating with its server.
    os.environ['OMP_NUM_THREADS']='1'
    os.environ['STS2_CARD_QUALITY_GATE']='0'
    os.environ['STS2_CARD_PICK_TEMP']='0'
    os.environ['STS2_MC_ROLLOUT']=''
    from agent.greedy_trace import reset_greedy_context
    reset_greedy_context()


def _fixed_ooc(state):
    """Option-independent continuation up to the next fight.

    Keep the route/other rewards/shop actions fixed, rather than allowing a
    candidate's altered deck to change the route or another card reward.
    """
    kind=state.get('decision')
    if kind=='map_select':
        choices=state.get('choices') or []
        if not choices:raise RuntimeError('no map continuation')
        c=min(choices,key=lambda c:(c.get('row',0),c.get('col',0)))
        return action('select_map_node',row=c['row'],col=c['col'])
    if kind=='card_reward':
        if state.get('can_skip') is False:raise RuntimeError('another mandatory reward before next combat')
        return action('skip_card_reward')
    if kind=='shop':return action('leave_room')
    if kind=='rest_site':
        opts=[o for o in state.get('options',[]) if o.get('is_enabled',True)]
        rest=next((o for o in opts if 'rest' in str(o.get('id',o.get('name',''))).lower()),None)
        if rest is None:raise RuntimeError('rest continuation has no deterministic heal')
        return action('choose_option',option_index=rest['index'])
    if kind=='event_choice':
        opts=[o for o in state.get('options',[]) if not o.get('is_locked') and not o.get('was_chosen')]
        if not opts:raise RuntimeError('event has no legal continuation')
        return action('choose_option',option_index=min(o['index'] for o in opts))
    if kind=='card_select':
        if state.get('min_select',1)==0:return action('skip_select')
        return action('select_cards',indices=','.join(str(i) for i in range(state.get('min_select',1))))
    if kind=='bundle_select':return action('select_bundle',bundle_index=0)
    if kind in {'treasure','rewards'}:return action('proceed')
    raise RuntimeError(f'unsupported continuation decision: {kind}')


def execute_solver(send,state,budget_ms,metrics):
    from combat_plan_driver import _execute_combat_plan_actions
    plan=send(action('plan_combat_turn'))
    search=plan.get('search') or {}
    if plan.get('type')!='combat_plan' or not plan.get('actions'):
        raise RuntimeError('Solver returned no usable plan')
    if search.get('budget_ms')!=budget_ms or search.get('inferrer')!='on':
        raise RuntimeError(f'Solver profile mismatch: {search}')
    metrics['plans']=metrics.get('plans',0)+1
    metrics.setdefault('searches',[]).append(search)
    played=metrics.setdefault('cards_played',{})
    cursor=state
    def execute(cmd):
        nonlocal cursor
        name=cmd.get('action');a=cmd.get('args') or {}
        if name=='play_card':
            hand=cursor.get('hand') or [];index=a.get('card_index')
            if isinstance(index,int) and 0<=index<len(hand):
                cid=hand[index].get('id','UNKNOWN');played[cid]=played.get(cid,0)+1
        result=send(cmd)
        if name not in {'get_map'}:metrics['solver_actions']=metrics.get('solver_actions',0)+1
        cursor=result
        return result
    stats={}
    result,ok=_execute_combat_plan_actions(execute,state,plan['actions'],stats=stats)
    if not ok:raise RuntimeError('Solver plan failed actual execution')
    if result.get('decision')=='card_select':raise RuntimeError('unresolved Solver combat card_select')
    if stats.get('stale_no_progress'):raise RuntimeError('Solver plan made no progress')
    metrics['replans']=metrics.get('replans',0)+stats.get('stale_unresolved',0)+stats.get('stale_refusals',0)
    return result


def engine_identity(args):
    import hashlib
    dll=ROOT/'lib/sts2.dll'
    raw=ROOT/'lib/sts2.dll.original'
    info=args.game_dir.parent/'release_info.json'
    release=json.loads(info.read_text())
    raw_hash=hashlib.sha256(raw.read_bytes()).hexdigest()
    install_hash=hashlib.sha256((args.game_dir/'sts2.dll').read_bytes()).hexdigest()
    if raw_hash!=install_hash:raise RuntimeError('CLI original DLL does not match installed game')
    if release['version']!='v0.113.1':raise RuntimeError('collector needs audited v0.113.1')
    return {'game_version':release['version'],'dll_sha256':hashlib.sha256(dll.read_bytes()).hexdigest(),
            'original_dll_sha256':raw_hash,'cli_sha256':hashlib.sha256(args.engine.read_bytes()).hexdigest(),'policy_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}


def discover(args):
    from agent.combat_env import greedy_action
    identity=engine_identity(args)
    existing=list((args.output/'cases').glob('*/decision.json'))
    count=len(existing);selected=[]
    for seed in args.seeds:
        if count>=args.cases:break
        if list((args.output/'cases').glob(f'{seed}-*/decision.json')):continue
        prefix=[];taken=0;last_floor=-100;metrics={'plans':0,'solver_actions':0}
        with Client(args,f'discover-{seed}') as cli:
            def interact(cmd):
                result=cli.send(cmd)
                if cmd.get('action')!='plan_combat_turn':
                    prefix.append({'command':cmd,'observation':result,'sha256':canonical_hash(result)})
                return result
            state=interact({'cmd':'start_run','character':'Fool','ascension':9,'seed':seed,'lang':'en'})
            for step in range(args.max_steps):
                kind=state.get('decision');ctx=state.get('context') or {};floor=ctx.get('floor',0)
                if kind=='game_over':break
                if kind=='combat_play':
                    state=execute_solver(interact,state,args.budget_ms,metrics);continue
                if kind=='card_reward' and len(state.get('cards') or [])>=2 and floor>=args.min_floor and floor-last_floor>=args.spacing:
                    case_id=f'{seed}-d{taken+1:02d}'
                    target=args.output/'cases'/case_id
                    options=reward_options(state);chosen=greedy_action(state)
                    index=(chosen.get('args') or {}).get('card_index',-1) if chosen else -1
                    heuristic=next((o for o in options if o['index']==index),options[-1])
                    frozen_prefix=list(prefix);reference=state
                    # ORIGINAL process anchor reveals hidden RNG after the exact
                    # reward state. Replays must match this, not only each other.
                    after=interact(heuristic['command']);verify_reward_transition(reference,after,heuristic)
                    anchor=advance_to_combat(interact,after)
                    tail=[r['command'] for r in prefix[len(frozen_prefix)+1:]]
                    case={'schema_version':1,'case_id':case_id,'seed':seed,'character':'Fool','ascension':9,
                          'horizon':'next_combat','state':reference,'state_sha256':canonical_hash(reference),
                          'restore_verified':False,'engine':identity,'options':options,
                          'heuristic_option_id':heuristic['id'],'heuristic_option_index':heuristic['index'],
                          'continuation_id':canonical_hash({'policy':'fixed-ooc-v2','commands':tail,'cli_sha256':identity['cli_sha256']}),
                          'continuation_commands':tail,'future_control_option_index':heuristic['index'],
                          'future_control_initial_state':anchor,'future_control_initial_sha256':canonical_hash(anchor),
                          'future_control_combat_identity':combat_identity(anchor),'prefix_steps':len(frozen_prefix)}
                    save(target/'prefix.json',frozen_prefix);save(target/'decision.json',case)
                    count+=1;taken+=1;last_floor=floor;selected.append(case_id);state=anchor
                    print(json.dumps({'discovered':case_id,'act':ctx.get('act'),'floor':floor,'total':count}),flush=True)
                    if taken>=args.per_seed or count>=args.cases:break
                    continue
                move=greedy_action(state)
                if not move:raise RuntimeError(f'discovery has no action for {kind}')
                new=interact(move)
                if new==state:raise RuntimeError(f'discovery stalled on {move}')
                state=new
    save(args.output/'manifest.json',{'schema_version':1,'engine':identity,'cases':sorted(p.parent.name for p in (args.output/'cases').glob('*/decision.json')),
                                    'target_cases':args.cases,'repetitions':args.repetitions,'budget_ms':args.budget_ms,'solver_threads':1})
    print(json.dumps({'discovered_total':count,'new_cases':selected}),flush=True)


def restore(cli,case_dir,case):
    state=None
    for step,row in enumerate(json.loads((case_dir/'prefix.json').read_text()),1):
        state=cli.send(row['command'])
        try:verify_observation(row['observation'],state)
        except ValueError as exc:
            tag=cli.stderr_path.stem if hasattr(cli,'stderr_path') else str(time.time_ns())
            save(case_dir/'restore-failures'/f'{tag}-step{step}.json',
                 {'step':step,'command':row['command'],'expected':row['observation'],'actual':state})
            raise ValueError(f'prefix step {step}: {exc}') from exc
    verify_observation(case['state'],state)
    return state


def advance_to_combat(send, state):
    seen=Counter()
    for _ in range(60):
        if state.get('decision')=='combat_play':return state
        if state.get('decision')=='game_over':raise RuntimeError('control ended before next combat')
        seen[canonical_hash(state)]+=1
        if seen[canonical_hash(state)]>2:raise RuntimeError('future control continuation stalled')
        state=send(_fixed_ooc(state))
    raise RuntimeError('future control did not reach combat')


def follow_continuation(send,state,case):
    for cmd in case['continuation_commands']:
        if state.get('decision')=='combat_play':raise RuntimeError('continuation reached combat too early')
        state=send(cmd)
    if state.get('decision')!='combat_play':raise RuntimeError('frozen continuation did not reach combat')
    if combat_identity(state)!=case['future_control_combat_identity']:
        raise RuntimeError('next combat identity differs from original control')
    return state


def verify_future_controls(fingerprints, original=None):
    if len(fingerprints)<3:raise ValueError('at least three future controls required')
    if len(set(fingerprints))!=1 or (original is not None and fingerprints[0]!=original):
        raise ValueError('future RNG/control drift')


def verify_cases(args):
    identity=engine_identity(args);reports=[]
    for path in selected_cases(args):
        case=json.loads(path.read_text())
        if (case['engine']['dll_sha256']!=identity['dll_sha256']
                or case['engine']['cli_sha256']!=identity['cli_sha256']):raise RuntimeError('engine changed since discovery')
        checks=[];future=[]
        for repeat in range(args.repetitions):
            with Client(args,f'verify-{case["case_id"]}-{repeat}') as cli:
                restored=restore(cli,path.parent,case);checks.append(canonical_hash(restored))
                control=next(o for o in case['options'] if o['index']==case['future_control_option_index'])
                after=cli.send(control['command']);verify_reward_transition(restored,after,control)
                following=follow_continuation(cli.send,after,case)
                verify_observation(case['future_control_initial_state'],following)
                future.append(canonical_hash(following))
        verify_future_controls(future,case['future_control_initial_sha256'])
        case['restore_verified']=True;case['restore_checks']=checks
        case['future_control_verified']=True;case['future_control_initial_sha256']=future[0]
        case['future_control_checks']=future
        case['future_control_option_index']=control['index']
        save(path,case);reports.append({'case_id':case['case_id'],'identical_restores':len(checks),'state_sha256':case['state_sha256'],'future_control_sha256':future[0]})
        print(json.dumps(reports[-1]),flush=True)
    save(args.output/'restore-report.json',reports)


def selected_cases(args):
    paths=sorted((args.output/'cases').glob('*/decision.json'))
    return [p for p in paths if not args.case or p.parent.name in args.case]


def branch(args,case_path,case,option,repeat):
    identity=engine_identity(args);name=f'b{args.budget_ms}-o{option["index"]}-r{repeat}'
    dest=case_path.parent/(name+'.json')
    row={'schema_version':1,'case_id':case['case_id'],'seed':case['seed'],'option_id':option['id'],'option_index':option['index'],
         'repeat':repeat,'budget_ms':args.budget_ms,'solver_threads':1,'horizon':case['horizon'],
         'engine_dll_sha256':identity['dll_sha256'],'engine_cli_sha256':identity['cli_sha256'],'continuation_id':case['continuation_id'],
         'restore_verified':False,'plans':0,'solver_actions':0,'status':'technical_error'}
    if dest.exists():
        old=json.loads(dest.read_text())
        if reuse_cached_result(old,row,retry_errors=args.retry_errors):return old
        archive=dest.parent/'attempts';archive.mkdir(exist_ok=True)
        stamp=str(time.time_ns());dest.rename(archive/(dest.stem+'-'+stamp+'.json'))
        trace=dest.with_suffix('.jsonl')
        if trace.exists():trace.rename(archive/(trace.stem+'-'+stamp+'.jsonl'))
    start=time.monotonic()
    try:
        if (identity['dll_sha256']!=case['engine']['dll_sha256']
                or identity['cli_sha256']!=case['engine']['cli_sha256']):raise RuntimeError('branch engine drift')
        with Client(args,case['case_id']+'-'+name) as cli, dest.with_suffix('.jsonl').open('w',buffering=1) as log:
            state=restore(cli,case_path.parent,case)
            row['restore_verified']=True;row['restore_sha256']=canonical_hash(state)
            row['hp_start']=state['player']['hp'];row['max_hp_start']=state['player']['max_hp']
            def interact(cmd):
                result=cli.send(cmd)
                log.write(json.dumps({'command':cmd,'observation':result},ensure_ascii=False)+'\n')
                return result
            after=interact(option['command']);verify_reward_transition(state,after,option)
            state=follow_continuation(interact,after,case)
            combat_started=False;rounds=set();unchanged=0
            for step in range(args.max_steps):
                kind=state.get('decision');ctx=state.get('context') or {}
                if kind=='combat_play':
                    if not combat_started:
                        combat_started=True
                        row['combat_initial_sha256']=canonical_hash(state)
                        row['combat_identity']=combat_identity(state)
                    rounds.add((ctx.get('act'),ctx.get('floor'),state.get('round')))
                    if len(rounds)>args.max_rounds:row['status']='censored';break
                    state=execute_solver(interact,state,args.budget_ms,row)
                    continue
                if kind=='game_over':
                    row['survived']=terminal_outcome(state);row['status']='completed' if combat_started else 'censored';break
                if combat_started:
                    if kind=='card_select':raise RuntimeError('unresolved combat selection')
                    row['survived']=True;row['status']='completed';break
                raise RuntimeError('unexpected pre-combat decision after frozen continuation')
            else:row['status']='censored'
            row['rounds']=len(rounds);row['hp_end']=(state.get('player') or {}).get('hp',0)
            row['max_hp_end']=(state.get('player') or {}).get('max_hp',row['max_hp_start'])
            row['finish']=player_summary(state)
    except Exception as exc:
        row['status']='technical_error';row['error']=str(exc)
    row['elapsed_s']=round(time.monotonic()-start,3);save(dest,row)
    return row


def collect(args):
    for path in selected_cases(args):
        case=json.loads(path.read_text())
        if not case.get('restore_verified'):raise RuntimeError(f'{case["case_id"]} restoration not verified')
        for option in case['options']:
            if args.option is not None and option['index'] not in args.option:continue
            for repeat in range(1,args.repetitions+1):
                result=branch(args,path,case,option,repeat)
                print(json.dumps({k:result.get(k) for k in ['case_id','option_index','repeat','status','survived','hp_end','plans','error']}),flush=True)
    export(args)


def export(args):
    rows=[]
    for path in selected_cases(args):
        case=json.loads(path.read_text());case['results']=[]
        for item in sorted(path.parent.glob('b*-o*-r*.json')):
            case['results'].append(json.loads(item.read_text()))
        rows.append(case)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'dataset.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    report={'cases':len(rows),'results':sum(len(r['results']) for r in rows),
            'verified_solver_results':sum(result_is_training_label(c,r) for c in rows for r in c['results']),
            'statuses':dict(Counter(r['status'] for c in rows for r in c['results']))}
    save(args.output/'collection-report.json',report);print(json.dumps(report),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['discover','verify','collect','export'])
    parser.add_argument('--engine',type=Path,default=ROOT/'src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll')
    parser.add_argument('--game-dir',type=Path,default=Path.home()/'Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64')
    parser.add_argument('--dotnet',type=Path,default=Path.home()/'.dotnet-arm64/dotnet')
    parser.add_argument('--output',type=Path,default=ROOT/'logs/experiments/fool-a9-offline')
    parser.add_argument('--budget-ms',type=int,default=3500)
    parser.add_argument('--seeds',nargs='+',default=[f'FOOLA9OFFLINE{i:02d}' for i in range(1,13)])
    parser.add_argument('--cases',type=int,default=24)
    parser.add_argument('--per-seed',type=int,default=4)
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--min-floor',type=int,default=2)
    parser.add_argument('--spacing',type=int,default=2)
    parser.add_argument('--max-steps',type=int,default=3000)
    parser.add_argument('--max-rounds',type=int,default=30)
    parser.add_argument('--case',action='append')
    parser.add_argument('--option',action='append',type=int)
    parser.add_argument('--retry-errors',action='store_true')
    args=parser.parse_args()
    if not 1000<=args.budget_ms<=30000:parser.error('budget must be 1000..30000ms')
    if args.repetitions<3:parser.error('at least 3 repetitions required')
    args.output=args.output.resolve();initialize_policy()
    {'discover':discover,'verify':verify_cases,'collect':collect,'export':export}[args.command](args)


if __name__=='__main__':main()
