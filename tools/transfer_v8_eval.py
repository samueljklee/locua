#!/usr/bin/env python3
"""Bounded v8 selector comparison: synthetic loops or recorded first-state decisions.

Never operates Cua or a GUI. Model execution requires --execute-local-model.
Recorded source timestamps are refreshed only in an explicitly offline copy;
those copies and selected actions are never valid live authorization.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'examples/transfer-v8'
REGRESSION_IDS=('start-2f9b462747a7','start-9b02a560078c','start-f0168e999843')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')

def check_freeze():
    full=read(FIXTURE/'fixture-freeze.json');semantic=read(FIXTURE/'semantic-freeze.json')
    if full['semantic_freeze_sha256']!=sha(FIXTURE/'semantic-freeze.json'):raise ValueError('semantic freeze changed')
    for manifest in (semantic,full):
        for name,want in manifest['files'].items():
            if sha(FIXTURE/name)!=want:raise ValueError('frozen fixture changed: '+name)
    return {'fixture_freeze_sha256':sha(FIXTURE/'fixture-freeze.json'),'semantic_freeze_sha256':sha(FIXTURE/'semantic-freeze.json')}

def source_hashes():
    from locua.engine.prototype import core,observation_loop,context,regions,observation_tools,decision,simulation
    return {m.__name__:sha(m.__file__) for m in (core,observation_loop,context,regions,observation_tools,decision,simulation)}

def recorded_inputs():
    manifest=read(ROOT/'artifacts/user-comparison-audit-001/summary.json');byid={r['run']:r for r in manifest['runs']}
    if set(byid)!=set(REGRESSION_IDS):raise ValueError('regression membership changed')
    rows=[]
    for rid in REGRESSION_IDS:
        source=Path(byid[rid]['source'])
        for name,want in byid[rid]['source_hashes'].items():
            if sha(source/name)!=want:raise ValueError('recorded source changed: '+rid+'/'+name)
        obs=read(source/'execution/initial-observation.json')
        grounded=read(source/'execution/grounded-task.json')
        plan=read(source/'execution/plan.json')
        rows.append({'id':rid,'observation':obs,'task':grounded['task'],'plan':plan,
                     'source_files':{name:sha(source/name) for name in ['execution/initial-observation.json','execution/grounded-task.json','execution/plan.json']},
                     'classification':'exposed user-run regression; initial recorded state only'})
    return rows

def refresh_offline(obs):
    copy=deepcopy(obs);now=time.time_ns();copy['observed_at_ns']=now
    if 'observed_at_ns' in copy.get('provenance',{}):copy['provenance']['observed_at_ns']=now
    # Capability metadata is part of same source observation. This timestamp
    # refresh is an OFFLINE counterfactual only; target/ref/snapshot stay fixed.
    for key in ('execution_context','browser_execution'):
        if isinstance(copy.get(key),dict) and 'observed_at_ns' in copy[key]:copy[key]['observed_at_ns']=now
    return copy

def acceptable_candidates(task,obs,candidates):
    """Independent semantic rubric, not a fresh-dispatch or task-success claim."""
    from locua.engine.prototype.core import assess,matching
    state=assess(task,obs,{})
    active=next((i for i in task['intents'] if state.get('intents',{}).get(i['id'])=='pending'),None)
    correct=[]
    if active:
        controls=matching(obs,active['selector'])
        if len(controls)==1:
            for c in candidates:
                if c.get('control_id')!=controls[0]['id']:continue
                if active['kind']=='set_text' and c['kind']=='set_text' and c.get('value')==active['value']:correct.append(c['id'])
                elif active['kind'] in ('set_state','invoke') and c['kind']=='press':correct.append(c['id'])
    return {'assessment':state,'active_intent_id':active['id'] if active else None,
            'acceptable_candidate_ids':correct,'meaning':'first pending reviewed intent semantic selection; no fresh-dispatch validation'}

def recorded_one(row,selector,policy,cancel,events):
    from locua.engine.prototype.core import build_candidates
    from locua.engine.prototype.context import reviewed_goal
    from locua.engine.prototype.observation_loop import choose_with_tools
    from locua.engine.prototype.task_state import TaskState
    obs=refresh_offline(row['observation']);task=deepcopy(row['task']);candidates=build_candidates(obs,task)
    rubric=acceptable_candidates(task,obs,candidates);state=TaskState(row['plan'])
    state.consume({'type':'observation','observation':obs});state.consume({'type':'assessment',**rubric['assessment']})
    active=next((i for i in task['intents'] if i['id']==rubric['active_intent_id']),None)
    if policy=='reviewed_target_first':goal=reviewed_goal(task,rubric['assessment'],active,{},state)
    else:
        goal=task['goal']+'\nEXPLICIT AUTHORIZED INTENTS:\n'+json.dumps(task['intents'],ensure_ascii=False)+'\nSTATE TO PRESERVE:\n'+json.dumps(task.get('invariants',[]),ensure_ascii=False)
        if active:goal+='\nCURRENT OUTCOME TO ADVANCE:\n'+json.dumps(active,ensure_ascii=False)+'\nInspect its observed region and choose a permitted action for this outcome. Later outcomes with unmet dependencies are deferred. Every UI region and competing action remains available.'
        goal+='\nCURRENT INTENT STATUS:\n'+json.dumps(rubric['assessment']['intents'])+'\n'+state.reminder()
    start=time.monotonic()
    def interrupted():
        if cancel.is_set():return {'status':'canceled','reason':'evaluation_canceled'}
        if time.monotonic()-start>=180:return {'status':'bounded_stop','reason':'recorded_case_time_budget'}
    result=choose_with_tools(obs,candidates,selector,goal=goal,history=[],emit=events.append,interrupted=interrupted,
                             max_inspections=4,search_labels=list(dict.fromkeys(i['selector'].get('name') or i['selector'].get('ancestor',{}).get('name') for i in task['intents'])),
                             inspection_policy=policy,reviewed_task=task,ledger={})
    selected=result.get('selected',{}).get('id')
    return {'status':result.get('status','selected'),'reason':result.get('reason'),'selected_id':selected,
            'correct_current_intent_selection':selected in rubric['acceptable_candidate_ids'],
            'coverage_supported':bool(rubric['acceptable_candidate_ids']),'rubric':rubric,
            'observation_original_sha256':digest(row['observation']),'offline_refreshed_sha256':digest(obs),
            'gui_calls':0,'live_action_authorized':False,'task_complete_claimed':False}

def simulate_one(fixture,selector,policy,cancel,events):
    from locua.engine.prototype.core import run_loop
    from locua.engine.prototype.simulation import SimulatedDriver
    from locua.engine.prototype.task_state import TaskState
    driver=SimulatedDriver(fixture);task=deepcopy(fixture['task_template']);task['target']=driver.observe()['target']
    state=TaskState(fixture['plan'])
    result=run_loop(task,driver,selector,cancel=cancel,max_steps=12,max_seconds=180,trace=events.append,
                    regions=True,observation_tools=True,max_inspections=4,task_state=state,inspection_policy=policy)
    final={c['key']:{'value':c.get('value'),'states':c.get('states',{}),'visible':c.get('visible',True)} for c in driver.controls}
    return {**{k:v for k,v in result.items() if k!='events'},'simulated_actions':len(driver.actions),
            'final_controls':final,'gui_calls':0,'live_completion_claimed':False}

def run(mode,policy,model,out,config,execute_local_model=False):
    freeze=check_freeze();out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700);os.chmod(out,0o700)
    rows=read(FIXTURE/'simulations.json')['cases'] if mode=='simulation' else recorded_inputs()
    prepared={'mode':mode,'policy':policy,'model':model,'cases':rows,'freeze':freeze,'source_hashes':source_hashes(),
              'gold_not_sent_to_model':True,'dispatch_mechanism':'synthetic only' if mode=='simulation' else 'none',
              'no_real_gui':True,'prepared_before_model_load':True}
    write(out/'prepared.json',prepared)
    if not execute_local_model:
        result={'status':'prepared','planned':len(rows),'attempted':0,'unrun':len(rows),'model_calls':0,'prepared_sha256':sha(out/'prepared.json')};write(out/'summary.json',result);return result
    from locua.config import load
    from locua.engine_adapter import runtime_environment
    from locua.engine.prototype.decision import ModelService
    cancel=threading.Event();prior=signal.getsignal(signal.SIGINT);signal.signal(signal.SIGINT,lambda *_:cancel.set())
    started=time.monotonic();summaries=[];worker=None;runtime=None;startup=None;failure=None
    try:
        with runtime_environment(load(config,required=True)[0]):
            loading=time.monotonic()
            with ModelService(model=model) as worker:
                startup=time.monotonic()-loading;runtime=worker.info();write(out/'runtime.json',runtime)
                for row in rows:
                    if cancel.is_set():break
                    events=[];case_start=time.monotonic()
                    try:result=(simulate_one if mode=='simulation' else recorded_one)(row,worker,policy,cancel,events)
                    except Exception as e:result={'status':'error','reason':type(e).__name__+':'+str(e),'gui_calls':0}
                    result.update(id=row['id'],case_wall_s=time.monotonic()-case_start,decision_count=sum(e.get('type')=='decision' for e in events))
                    write(out/(row['id']+'.json'),{'result':result,'events':events});summaries.append(result)
                    if result['status'] in ('error','canceled') or getattr(worker,'poisoned',False):break
    except Exception as e:failure=type(e).__name__+':'+str(e)
    finally:signal.signal(signal.SIGINT,prior)
    result={'schema':'locua.transfer-v8.selector-eval.v1','mode':mode,'policy':policy,'model':model,
            'status':'canceled' if cancel.is_set() else 'error' if failure else 'finished','failure':failure,
            'planned':len(rows),'attempted':len(summaries),'unrun':len(rows)-len(summaries),'cases':summaries,
            'model_startup_s':startup,'total_wall_s':time.monotonic()-started,'worker_closed':worker.closed if worker else None,
            'prepared_sha256':sha(out/'prepared.json'),'source_unchanged':source_hashes()==prepared['source_hashes'],
            'gui_calls':0,'heldout_live_accuracy':False}
    write(out/'summary.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=['simulation','recorded'],required=True)
    p.add_argument('--policy',choices=['model_led','reviewed_target_first'],required=True);p.add_argument('--model',choices=['baseline','comparator'],default='baseline')
    p.add_argument('--out',type=Path,required=True);p.add_argument('--config',type=Path);p.add_argument('--execute-local-model',action='store_true');a=p.parse_args()
    result=run(a.mode,a.policy,a.model,a.out,a.config,a.execute_local_model)
    print(json.dumps({k:result[k] for k in ('status','planned','attempted','unrun')},indent=2))
    raise SystemExit(1 if result['status'] in ('error','canceled') else 0)

if __name__=='__main__':main()
