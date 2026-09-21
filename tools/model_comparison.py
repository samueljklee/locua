"""Frozen, CPU-prepared semantic replays. No GUI or provider call on import/prepare.

Providers are injected existing Amplifier Provider instances. This module owns no
network client, credentials, model loader, output parser, or desktop action loop.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from locua.amplifier_contracts import SPECS, validate_tool_arguments
from locua.task_observation_scope import TaskObservationScope, VERSION as SCOPE_VERSION
from locua.engine.prototype.cli import private_json

VERSION='model-comparison-v7-replay-v2'
REPEATS=2
EVALUATOR_VERSION='admissible-next-action-v2'
RUNNER_VERSION='terminal-transport-stop-v1'
TASKS=[
    {'id':'calculator-original','request':'open calculator app and solve 192*231-100','app':'Calculator','expression':'192*231-100'},
    {'id':'textedit-buffer','request':"In TextEdit, replace the entire text in 'Locua-v10-transfer-draft.txt' with 'Status: reviewed locally.'",'app':'TextEdit','document':'Locua-v10-transfer-draft.txt','value':'Status: reviewed locally.'},
    {'id':'calculator-fresh','request':'Use Calculator to work out (81 - 29) / 4.','app':'Calculator','expression':'(81 - 29) / 4'},
]


def digest(value):
    if not isinstance(value,bytes):value=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def _read(path):return json.loads(Path(path).read_text())


def _tools():
    return [{'name':name,'description':description,'parameters':deepcopy(schema)}
            for name,(description,schema) in SPECS.items()]


def _scope(run,task):
    events=[_read(p) for p in sorted((run/'desktop').glob('event-*.json'))]
    apps=[a for e in events if e['tool']=='locua_apps' for a in e['result'].get('items',[]) if a['name']==task['app']]
    if not apps:raise ValueError('No recorded installed app identity')
    app=apps[0]
    scope=TaskObservationScope(task['request'],app,document_titles=[task['document']] if task.get('document') else [])
    for event in events:
        if event['tool']=='locua_windows':scope.register_windows(event['result'].get('windows',[]),app['app_id'])
    observations={}
    for path in sorted((run/'desktop').glob('observation-*.json')):
        o=_read(path);scope.register_observation(o);observations[o['snapshot_id']]=o
    return scope,observations,events


def _sanitize(request,scope):
    output={k:deepcopy(v) for k,v in request.items() if k in ('messages','stream')}
    output['tools']=_tools()
    for m in output['messages']:
        m.pop('metadata',None)
        if m['role']=='tool':
            envelope=json.loads(m['content']) if isinstance(m['content'],str) else m['content']
            if not isinstance(envelope,dict) or not isinstance(envelope.get('output'),dict):raise ValueError('Replay tool envelope not structured')
            envelope['output']=scope.project(m['name'],envelope['output'])
            if envelope.get('error') is not None:envelope['error']={'raw_error_retained_private':True}
            m['content']=json.dumps(envelope,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    return output


def _swap_request(value,old,new):
    if isinstance(value,str):return value.replace(old,new)
    if isinstance(value,list):return [_swap_request(x,old,new) for x in value]
    if isinstance(value,dict):return {k:_swap_request(v,old,new) for k,v in value.items()}
    return value


def inspection_rubric(request,observations,scope):
    from locua.progressive_ui import exposed_controls
    from locua.engine.prototype.regions import catalog_regions
    exposed=set();detailed=set();known_regions=set();seen=[];known_windows=set();recoverable=False
    for m in request['messages']:
        if m['role']=='assistant':seen.extend(m.get('tool_calls') or [])
        if m['role']!='tool':continue
        result=json.loads(m['content'])['output']
        if isinstance(result.get('window_id'),str):known_windows.add(result['window_id'])
        recoverable |= result.get('status') in ('unavailable','uncertain','refused')
        page=result.get('overview',result)
        if page.get('version')=='progressive-ui-v1':
            for c in exposed_controls(page):
                if c.get('id'):exposed.add(c['id'])
                if c.get('region_id'):known_regions.add(c['region_id'])
            if page.get('operation')=='control':detailed.add(page['control_id'])
            known_regions.update(r for r in page.get('regions',[]) if isinstance(r,str))
            for row in page.get('items',[]):
                if isinstance(row,dict) and row.get('kind')=='region':known_regions.add(row['id'])
    index={}
    visible_snapshots={json.loads(m['content'])['output'].get('snapshot_id') for m in request['messages'] if m['role']=='tool'}-{None}
    for sid in visible_snapshots:
        o=observations[sid];catalog=catalog_regions(o)
        index[sid]={'controls':[{'id':c['id'],'role':c['role'],'name':c.get('name'),'value':c.get('value'),
            'region_id':catalog['memberships'][c['id']],'semantics':{k:c.get('semantics',{}).get(k) for k in ('title','description','help','identifier')}}
            for c in o['controls'] if c['id'] not in scope.blocked_controls],
            'regions':[r['id'] for r in catalog['regions']]}
    return {'snapshots':index,'exposed_control_ids':sorted(exposed),'detailed_control_ids':sorted(detailed),
        'known_region_ids':sorted(known_regions),'known_window_ids':sorted(known_windows),'prior_inspections':seen,
        'recorded_recovery_needed':recoverable,'policy':'Accept new grounded information in the same authorized app/window; never call an inspection task completion.'}

def prepare(repo,out):
    """Freeze requests/evaluator separately before any candidate provider output."""
    repo=Path(repo);out=Path(out);out.mkdir(parents=True,mode=0o700,exist_ok=False)
    base=repo/'artifacts/cli-milestone-v6-001'
    definitions=[('calculator-original-early',TASKS[0],base/'calculator-002',9),
        ('calculator-original-later',TASKS[0],base/'calculator-002',12),
        ('textedit-approved-pre-action',TASKS[1],base/'textedit-001',7),
        ('calculator-fresh-layout-transfer',TASKS[2],base/'calculator-002',12)]
    cases=[];rubric=[];audit=[]
    for cid,task,run,call in definitions:
        source=run/'provider'/f'call-{call:03d}-input.json';original=_read(source)
        scope,obs,events=_scope(run,task)
        old=TASKS[0]['request'] if task['id']=='calculator-fresh' else task['request']
        request=_sanitize(_swap_request(original,old,task['request']),scope)
        # Verify this is the existing provider contract, not invented transport.
        from amplifier_core.message_models import ChatRequest
        ChatRequest.model_validate(request)
        fields={'id':cid,'task_id':task['id'],'request':request,'request_sha256':digest(request),
                'source_input_sha256':digest(source.read_bytes()),'semantic_replay_only':True,
                'retained_capture_times_unchanged':True,'desktop_calls':0}
        private_json(out/(cid+'.json'),fields);cases.append({k:v for k,v in fields.items() if k!='request'})
        # Expected semantics are evaluator-only; never included in ChatRequest.
        if task.get('expression'):
            target_sids={json.loads(m['content'])['output'].get('snapshot_id') for m in request['messages'] if m['role']=='tool'}-{None}
            visible=[]
            for sid in sorted(target_sids):
                for c in obs[sid]['controls']:
                    if c['role']=='AXStaticText':visible.append({'snapshot_id':sid,'control_id':c['id']})
            rubric.append({'id':cid,'kind':'calculation_review','expression':task['expression'],'readout_candidates':visible,
                'preserves':[],'exact_next_tool_sequence_required':False})
        else:
            review_event=next(e for e in events if e['tool']=='locua_review' and e['result'].get('status')=='approved')
            review=review_event['result'];goal=review_event['input']['goals'][0];sid=review['review_capture']['snapshot_id']
            listing=next(json.loads(m['content'])['output'] for m in request['messages'] if m['role']=='tool' and m['name']=='locua_inspect')
            from locua.progressive_ui import exposed_controls
            control=next(c for c in exposed_controls(listing) if c['id']==goal['control_id'])
            actions=[a['id'] for a in control['actions'] if a['kind']=='set_text']
            if len(actions)!=1:raise ValueError('Recorded exact field action unavailable')
            rubric.append({'id':cid,'kind':'approved_text_action','scope_id':review['scope_id'],
                'snapshot_id':sid,'control_id':goal['control_id'],'action_id':actions[0],'value':task['value'],
                'exact_next_tool_sequence_required':False})
        rubric[-1]['inspection']=inspection_rubric(request,obs,scope)
        wire=json.dumps(request,ensure_ascii=False)
        excluded=scope.private_titles|scope.redacted_literals
        remaining=[x for x in excluded if x and x in wire and x not in task['request']]
        if remaining:raise ValueError('Privacy audit detected an excluded literal in prepared request')
        if '/Users/' in wire:raise ValueError('Host account path remains in prepared request')
        audit.append({'id':cid,'scope_counts':scope.audit,'output_bytes':len(wire.encode()),
            'source_observation_hashes':{sid:digest(o) for sid,o in obs.items()},
            'excluded_literal_leaks':len(remaining),'source_metadata_removed':True,
            'full_tool_inventory_retained':len(request['tools'])==len(SPECS),
            'tool_names':[t['name'] for t in request['tools']],
            'task_literals_retained':all(literal in wire for literal in [task.get('expression',''),task.get('value','')])})
    private_json(out/'rubric-private.json',{'version':VERSION,'cases':rubric,'never_send_to_provider':True})
    private_json(out/'privacy-audit.json',{'version':SCOPE_VERSION,'cases':audit})
    manifest={'version':VERSION,'created_at_ns':time.time_ns(),'repeats_per_case':REPEATS,
        'live_tasks':TASKS,'live_repeats_per_task':REPEATS,'cases':cases,
        'semantic_replay_count_per_provider':len(cases)*REPEATS,'replay_is_not_live_task_completion':True,
        'source_scope_sha256':digest((repo/'src/locua/task_observation_scope.py').read_bytes()),
        'source_tools_sha256':digest((repo/'src/locua/amplifier_contracts.py').read_bytes()),
        'rubric_sha256':digest((out/'rubric-private.json').read_bytes()),
        'reset_plan':{'calculator':'Root-owned fixture setup uses fresh observed exact-window clear controls only; separately record initial readout/layout. Do not silently change mode or select expected-result controls.',
            'textedit':'Root-owned fixture setup restores only the named disposable editor buffer to its frozen initial UTF-8 literal, verifies fresh exact editor buffer, no Save claim.',
            'operator_setup_is_not_model_action':True,'desktop_runs_serialized':True,
            'same_initial_state_for_every_provider_repeat':True,'unknown_effect_stops_without_retry':True},
        'no_gpu_gui_network_calls_during_prepare':True}
    private_json(out/'manifest.json',manifest)
    return manifest


def evaluator_rubric_v2(case,rubric):
    """Augment evaluator-only facts from exactly the frozen model-visible input.

    Never use today's wall clock to age a historical capture, never add a new
    observation, and never infer that every refusal means activation is needed.
    """
    result=deepcopy(rubric);index=result.setdefault('inspection',{})
    stale_windows=set();readability=set();known=set(index.get('known_window_ids',[]))
    repeated=set();calls={};observes={};status_pages=[];known_scopes=set();compacted=False
    for message in case['request']['messages']:
        if message.get('role') in ('user','system') and isinstance(message.get('content'),str) and message['content'].startswith('<system-reminder source="context-compaction">'):
            compacted=True
        if message.get('role')=='assistant':
            for call in message.get('tool_calls') or []:
                if isinstance(call,dict) and call.get('id'):calls[call['id']]=call
            continue
        if message.get('role')!='tool':continue
        try:
            envelope=json.loads(message['content']) if isinstance(message['content'],str) else message['content']
            output=envelope.get('output',{})
        except (ValueError,TypeError,AttributeError):continue
        if not isinstance(output,dict):continue
        call=calls.get(message.get('tool_call_id'),{})
        name=message.get('name',call.get('tool',call.get('name')))
        args=call.get('arguments',{})
        if isinstance(output.get('scope_id'),str):known_scopes.add(output['scope_id'])
        if isinstance(output.get('scope'),dict) and isinstance(output['scope'].get('scope_id'),str):known_scopes.add(output['scope']['scope_id'])
        for row in output.get('items',[]):
            if isinstance(row,dict) and isinstance(row.get('scope_id'),str):known_scopes.add(row['scope_id'])
        if name=='locua_status' and output.get('status')=='ok':
            status_pages.append({'operation':output.get('operation',args.get('operation','summary')),
                'scope_id':args.get('scope_id'),'start':output.get('start',args.get('start',0)),
                'next_start':output.get('next_start'),'returned_complete_page':not output.get('truncated',False),
                'scope_count':output.get('scope_count')})
        wid=output.get('window_id',args.get('window_id'))
        if not isinstance(wid,str) or wid not in known:continue
        page=output.get('overview',output)
        if not isinstance(page,dict):page={}
        retained=page.get('retained_observation_only') is True or page.get('retained_evidence_only') is True
        age=page.get('capture_age_seconds')
        explicitly_stale=(type(age) in (int,float) and age>30)
        fresh_required=page.get('fresh_capture_required_for_action') is True
        if retained and (explicitly_stale or fresh_required):stale_windows.add(wid)
        if name=='locua_observe':
            if output.get('status') in ('unavailable','uncertain','refused'):
                readability.add(wid)
            elif output.get('snapshot_id'):
                readability.discard(wid)
                sid=output['snapshot_id'];observes.setdefault(wid,[]).append(sid)
                # Two identical returned snapshot IDs demonstrate repeated
                # equivalent recapture in the actual exposed history.
                if len(observes[wid])>1 and observes[wid][-2]==sid:repeated.add(wid)
                if not (retained and (explicitly_stale or fresh_required)):stale_windows.discard(wid)
        if name=='locua_act' and output.get('action_started') is True:
            observes.pop(wid,None);repeated.discard(wid)
        feedback=output.get('exploration_feedback',{})
        if (name=='locua_observe' and isinstance(feedback,dict)
                and feedback.get('code') in ('repeated_inspection','repeated_failed_read')
                and feedback.get('equivalent_inspection_count',0)>1):repeated.add(wid)
    index.update(admissible_freshness_window_ids=sorted(stale_windows),
        readability_recovery_window_ids=sorted(readability),repeated_recapture_window_ids=sorted(repeated),
        freshness_basis='Only explicit frozen-input retained/fresh-required or age>30s evidence; current time unused.',
        query_fields=['name','title','description','help','identifier'],query_searches_values=False,
        compaction_notice_present=compacted,exposed_status_pages=status_pages,known_scope_ids=sorted(known_scopes))
    return result

def classify_inspection(name,args,rubric):
    index=rubric.get('inspection',{})
    if name=='locua_status':
        operation=args.get('operation','summary');start=args.get('start',0);scope_id=args.get('scope_id')
        if operation=='needs':return 'authored_needs_not_context_read'
        if scope_id is not None and scope_id not in index.get('known_scope_ids',[]):return 'wrong_or_unexposed_scope'
        pages=[p for p in index.get('exposed_status_pages',[]) if p['operation']==operation and p.get('scope_id')==scope_id]
        if any(p['start']==start and p['returned_complete_page'] for p in pages):return 'repeated_exposed_status'
        if start and not any(p.get('next_start')==start for p in pages):return 'status_page_not_grounded'
        if index.get('compaction_notice_present'):return 'admissible_context_recovery'
        return 'context_recovery_need_not_established'
    if name in ('locua_observe','locua_activate'):
        if args.get('window_id') not in index.get('known_window_ids',[]):return 'wrong_or_unexposed_window'
        wid=args['window_id']
        if wid in index.get('repeated_recapture_window_ids',[]):return 'repeated_recapture_evidenced'
        if wid in index.get('readability_recovery_window_ids',[]):return 'grounded_readability_recovery'
        if name=='locua_observe' and wid in index.get('admissible_freshness_window_ids',[]):return 'admissible_freshness_recovery'
        return 'freshness_or_readability_need_not_established'
    if name!='locua_inspect':return None
    source=index.get('snapshots',{}).get(args.get('snapshot_id'))
    if source is None:return 'wrong_or_unexposed_snapshot'
    seen=index.get('prior_inspections',[])
    if any(c.get('tool',c.get('name'))==name and c.get('arguments')==args for c in seen):return 'repeated_inspection'
    if args['operation']=='control':
        cid=args['control_id']
        if cid not in index.get('exposed_control_ids',[]) or cid not in {c['id'] for c in source['controls']}:return 'wrong_or_unexposed_control'
        return 'repeated_control_detail' if cid in index.get('detailed_control_ids',[]) else 'grounded_additional_inspection'
    if args['operation']=='overview':
        if args.get('cursor'):return 'continuation_requires_manual_audit'
        return 'grounded_additional_inspection' if set(source['regions'])-set(index.get('known_region_ids',[])) else 'repeated_overview'
    if args.get('region_id') and args['region_id'] not in index.get('known_region_ids',[]):return 'wrong_or_unexposed_region'
    if args.get('cursor'):return 'continuation_requires_manual_audit'
    rows=source['controls']
    if args.get('region_id'):rows=[c for c in rows if c['region_id']==args['region_id']]
    if args.get('role'):rows=[c for c in rows if c['role']==args['role']]
    if args.get('query'):
        needle=args['query'].casefold()
        rows=[c for c in rows if any(isinstance(v,str) and needle in v.casefold()
            for v in [c.get('name'),*(c.get('semantics',{}).get(k) for k in ('title','description','help','identifier'))])]
    if not rows:return 'inspection_matches_no_captured_controls'
    return 'grounded_additional_inspection' if {c['id'] for c in rows}-set(index.get('exposed_control_ids',[])) else 'repeated_exposed_controls'

def evaluate_response(case,rubric,response):
    """Score schema + task semantics, not a preferred fixed inspection sequence."""
    rubric=evaluator_rubric_v2(case,rubric)
    raw=response.model_dump() if hasattr(response,'model_dump') else deepcopy(response)
    calls=raw.get('tool_calls') or []
    classified=[]
    for call in calls:
        name=call.get('name',call.get('tool'));args=call.get('arguments')
        try:validate_tool_arguments(name,args)
        except Exception as error:
            classified.append({'tool':name,'classification':'schema_invalid','error_type':type(error).__name__});continue
        label='other_supported_tool'
        if name=='locua_inspect':
            seen=[c for m in case['request']['messages'] for c in (m.get('tool_calls') or [])]
            label='repeated_inspection' if any(c.get('tool')==name and c.get('arguments')==args for c in seen) else 'additional_inspection'
        elif name in ('locua_observe','locua_status'):label='retained_state_or_recapture'
        if rubric.get('inspection',{}).get('snapshots'):
            assessment=classify_inspection(name,args,rubric)
            if assessment is not None:label=assessment
        if rubric['kind']=='calculation_review' and name=='locua_review':
            goals=args.get('goals',[])
            valid=len(goals)==1 and goals[0].get('kind')=='calculation'
            if valid:
                g=goals[0];valid=(g.get('expression','').replace(' ','')==rubric['expression'].replace(' ','')
                    and {'snapshot_id':args['snapshot_id'],'control_id':g.get('control_id')} in rubric['readout_candidates']
                    and args.get('effects')==[{'kind':'goal','goal_id':g['id']}]
                    and not args.get('preserves',[]) and args.get('covers_entire_request') is True)
            label='faithful_readout_review' if valid else 'unfaithful_or_unbound_review'
        if rubric['kind']=='approved_text_action' and name=='locua_act':
            valid=all(args.get(k)==rubric[k] for k in ('scope_id','snapshot_id','action_id','value'))
            label='faithful_approved_text_action' if valid else 'wrong_scope_action'
        classified.append({'tool':name,'classification':label})
    return {'evaluator_version':EVALUATOR_VERSION,'case_id':case['id'],'calls':classified,'tool_call_count':len(calls),
        'semantic_progress':any(c['classification'] in ('faithful_readout_review','faithful_approved_text_action','grounded_additional_inspection','grounded_readability_recovery','admissible_freshness_recovery','admissible_context_recovery') for c in classified),
        'text_only_response':not calls,'task_completed':False,'desktop_action_executed':False}


def regrade(freeze_dir,runs,out):
    """Uniform offline grading into a new file; original outputs stay untouched.

    Running suites are permitted: missing or incomplete result files are listed
    as pending, not scored failures. Invoke again to a new path after completion.
    """
    frozen=Path(freeze_dir);manifest=_read(frozen/'manifest.json')
    if digest((frozen/'rubric-private.json').read_bytes())!=manifest['rubric_sha256']:
        raise ValueError('Frozen rubric changed')
    rubrics={c['id']:c for c in _read(frozen/'rubric-private.json')['cases']};cases={}
    for entry in manifest['cases']:
        c=_read(frozen/(entry['id']+'.json'))
        if digest(c['request'])!=entry['request_sha256']:raise ValueError('Frozen request changed')
        cases[c['id']]=c
    providers=[]
    for run in runs:
        run=Path(run);rows=[];pending=[]
        for cid,c in cases.items():
            for repeat in range(1,REPEATS+1):
                path=run/(cid+f'-{repeat}.json')
                if not path.exists():pending.append(path.name);continue
                try:raw=path.read_bytes();record=json.loads(raw)
                except (OSError,ValueError):pending.append(path.name);continue
                if record.get('request_sha256')!=c['request_sha256']:raise ValueError('Response references a different request')
                row={'file':str(path.resolve()),'source_sha256':digest(raw),'case_id':cid,'repeat':repeat,
                    'status':record.get('status'),'original_evaluation':deepcopy(record.get('evaluation')),
                    'request_sha256':c['request_sha256']}
                if record.get('status')=='returned' and isinstance(record.get('response'),dict):
                    row['evaluation_v2']=evaluate_response(c,rubrics[cid],record['response'])
                else:row['evaluation_v2']=None;row['unscored_reason']='No returned standard provider response'
                rows.append(row)
        scored=[r['evaluation_v2'] for r in rows if r['evaluation_v2'] is not None]
        providers.append({'run':str(run.resolve()),'completed_files':len(rows),'pending_files':pending,
            'scored_returned_responses':len(scored),'admissible_next_action_responses':sum(r['semantic_progress'] for r in scored),
            'classifications':{label:sum(c['classification']==label for r in scored for c in r['calls'])
                for label in sorted({c['classification'] for r in scored for c in r['calls']})},'rows':rows})
    report={'evaluator_version':EVALUATOR_VERSION,'evaluator_source_sha256':digest(Path(__file__).read_bytes()),
        'frozen_manifest_sha256':digest((frozen/'manifest.json').read_bytes()),
        'request_hashes':{cid:c['request_sha256'] for cid,c in cases.items()},'providers':providers,
        'original_scoring_preserved':True,'provider_calls':0,'desktop_calls':0,'prompts_changed':False,
        'single_next_action_not_live_completion':True,
        'corrections':['Explicit stale retained capture can justify one exact-window read without proving repeated nonprogress.',
                       'Activation requires recorded readability failure, not merely stale state or arbitrary schema refusal.',
                       'Semantic query matches only name/title/description/help/identifier strings; values are not searched.',
                       'Status can recover an absent/truncated requested section after explicit compaction; already exposed equivalent pages do not count.']}
    private_json(Path(out),report);return report


def terminal_provider_failure(provider,error):
    """Stop on an interrupted transport or explicit unusable provider state.

    A healthy provider's parse/validation failure is not a terminal transport
    failure. Returned service refusals are responses, never handled here.
    """
    if isinstance(error,(EOFError,ConnectionError,TimeoutError,asyncio.CancelledError)):
        return {'basis':'transport_or_runtime_interrupted','error_type':type(error).__name__}
    for owner,instance in (('provider',provider),('service',getattr(provider,'_service',None))):
        if instance is None:continue
        for name in ('_closed','closed','poisoned'):
            if getattr(instance,name,None) is True:
                return {'basis':'explicit_unusable_state','owner':owner,'flag':name,'error_type':type(error).__name__}
    return None


async def run_replays(freeze_dir,out,provider_factory,*,provider_label):
    """provider_factory(out) must return an async context manager yielding an
    existing Amplifier Provider. Caller handles official mount/cleanup/budgets.
    Two calls scheduled per case; terminal failures leave the remainder unrun.
    No adaptive reprompt, worker restart, or desktop actions.
    """
    from amplifier_core.message_models import ChatRequest
    frozen=Path(freeze_dir);out=Path(out);out.mkdir(parents=True,mode=0o700,exist_ok=False)
    manifest=_read(frozen/'manifest.json')
    if digest((frozen/'rubric-private.json').read_bytes())!=manifest['rubric_sha256']:raise ValueError('Frozen rubric changed')
    rubrics={c['id']:c for c in _read(frozen/'rubric-private.json')['cases']}
    results=[];terminal=None;attempted=0;cancelled=None
    async with provider_factory(out/'provider') as provider:
        for entry in manifest['cases']:
            case=_read(frozen/(entry['id']+'.json'))
            if digest(case['request'])!=entry['request_sha256']:raise ValueError('Frozen request changed')
            for repeat in range(REPEATS):
                started=time.monotonic();row={'case_id':case['id'],'repeat':repeat+1,'request_sha256':entry['request_sha256']}
                if terminal is not None:
                    row.update(status='unrun',reason='prior_terminal_provider_failure',terminal_failure=terminal,
                        provider_call_attempted=False,model_generations=0,wall_seconds=0)
                else:
                    try:
                        request=ChatRequest.model_validate(deepcopy(case['request']))
                        attempted+=1;row['provider_call_attempted']=True
                        response=await provider.complete(request)
                        row.update(status='returned',response=response.model_dump(),evaluation=evaluate_response(case,rubrics[case['id']],response))
                    except (Exception,asyncio.CancelledError) as error:
                        row.update(status='failed',error_type=type(error).__name__,error=str(error))
                        failure=terminal_provider_failure(provider,error)
                        if failure is not None:
                            terminal={**failure,'case_id':case['id'],'repeat':repeat+1}
                            row['terminal_failure']=terminal
                        if isinstance(error,asyncio.CancelledError):cancelled=error
                    row['wall_seconds']=time.monotonic()-started
                private_json(out/(case['id']+f'-{repeat+1}.json'),row);results.append(row)
    summary={'provider':provider_label,'manifest_sha256':digest((frozen/'manifest.json').read_bytes()),
        'runner_version':RUNNER_VERSION,'runner_source_sha256':digest(Path(__file__).read_bytes()),
        'status':'stopped_terminal_provider_failure' if terminal else 'complete',
        'repeats':REPEATS,'planned_calls':len(manifest['cases'])*REPEATS,'calls':attempted,
        'calls_meaning':'provider.complete attempts, not a count of model generations',
        'unrun_calls':sum(r['status']=='unrun' for r in results),'terminal_failure':terminal,
        'results':[{k:v for k,v in r.items() if k not in ('response','error')} for r in results],
        'live_task_completion_claimed':False,'independent_provider_outputs_not_reprompted':True}
    private_json(out/'summary.json',summary)
    if cancelled is not None:raise cancelled
    return summary


async def run_config(freeze_dir,out,*,provider='local',model='qwen38',thinking=False,config=None,budget_ledger=None):
    from contextlib import asynccontextmanager
    from provider_connection import make_provider
    from locua.amplifier_session import _dependencies, CONTEXT_CONFIG
    @asynccontextmanager
    async def factory(provider_out):
        AmplifierSession,_=_dependencies()
        session=AmplifierSession({'session':{
            'orchestrator':{'module':'loop-streaming','config':{'max_iterations':1}},
            'context':{'module':'context-simple','config':deepcopy(CONTEXT_CONFIG)}}})
        backend=make_provider(provider,model,provider_out,config=config,thinking=thinking,
            budget_ledger=budget_ledger,max_calls=8)
        try:
            await session.initialize()
            if hasattr(backend,'mount'):await backend.mount(session.coordinator)
            else:await session.coordinator.mount('providers',backend,name=backend.name)
            yield backend
        finally:
            await backend.close()
            await session.cleanup()
    return await run_replays(freeze_dir,out,factory,provider_label={'provider':provider,'model':model,'thinking':thinking})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='op',required=True)
    p=sub.add_parser('prepare');p.add_argument('--repo',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--out',required=True)
    p=sub.add_parser('run');p.add_argument('--freeze',required=True);p.add_argument('--out',required=True)
    p.add_argument('--provider',choices=('local','openai','anthropic'),default='local');p.add_argument('--model',default='qwen38')
    p.add_argument('--thinking',action='store_true');p.add_argument('--config');p.add_argument('--budget-ledger')
    p=sub.add_parser('regrade');p.add_argument('--freeze',required=True);p.add_argument('--runs',nargs='+',required=True);p.add_argument('--out',required=True)
    a=parser.parse_args()
    if a.op=='prepare':
        m=prepare(a.repo,a.out);print(json.dumps({'status':'frozen','cases':len(m['cases']),'calls_per_provider':m['semantic_replay_count_per_provider'],'manifest':str(Path(a.out)/'manifest.json')}))

    elif a.op=='run':
        r=asyncio.run(run_config(a.freeze,a.out,provider=a.provider,model=a.model,thinking=a.thinking,config=a.config,budget_ledger=a.budget_ledger))
        print(json.dumps({'status':r['status'],'calls':r['calls'],'unrun_calls':r['unrun_calls'],'out':a.out}))

    elif a.op=='regrade':
        r=regrade(a.freeze,a.runs,a.out)
        print(json.dumps({'status':'regraded','version':r['evaluator_version'],'providers':[{k:v for k,v in p.items() if k!='rows'} for p in r['providers']]}))
