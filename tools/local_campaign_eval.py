#!/usr/bin/env python3
"""CPU-only baseline sealing and first-divergence scoring. No provider/UI calls.

The exposed post-input regression separates action progress from legal reads.
It neither selects a live action nor substitutes for independent task completion.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'local-campaign-progress-v1'
BASE = ROOT/'artifacts/model-comparison-v7-001'
CAMPAIGN = ROOT/'artifacts/local-campaign-v8-001'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value,bytes) else canonical(value).encode()).hexdigest()


def read(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON key: '+key)
            result[key]=value
        return result
    return json.loads(Path(path).read_text(),object_pairs_hook=pairs,
        parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Nonfinite JSON: '+x)))


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
        f.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def record(path):
    path=Path(path);raw=path.read_bytes()
    return {'path':str(path.resolve()),'sha256':sha(raw),'bytes':len(raw)}


def seal_baseline(out=CAMPAIGN/'baseline.json'):
    identity_path=BASE/'package-identity-v65-ui1.json';identity=read(identity_path)
    installed=ROOT/'dist/guided-installed-wheel-v2/lib/python3.12/site-packages'
    verified={}
    for name,expected in identity['files'].items():
        p=installed/name
        if sha(p.read_bytes())!=expected:raise ValueError('Installed baseline changed: '+name)
        verified[name]=expected
    wheel=BASE/'locua-tools-v65-ui1.whl'
    if sha(wheel.read_bytes())!=identity['wheel_sha256']:raise ValueError('Baseline wheel changed')
    runs={}
    for name in ('v65-local-calculator-1','v65-openai-calculator-1b','v65-openai-calculator-2',
                 'v65-openai-textedit-1','v65-openai-textedit-2','v65-openai-fresh-1'):
        path=BASE/name;s=read(path/'summary.json')
        runs[name]={'source':record(path/'summary.json'),'status':s['status'],
            'request':s['request'],'tool_interface':s['tool_interface'],'model':s['model'],
            'provider':s['provider'],'metrics':s['metrics'],'wall_excluding_review_s':s['wall_excluding_human_s'],
            'human_review_s':s.get('human_wait_s'),'task_evidence':record(path/'desktop/evidence.json'),
            'retained_failure_or_success_not_new_acceptance':True}
    worker=BASE/'v65-local-calculator-1/provider/worker-ready.json';w=read(worker)
    source_files={name:verified[name] for name in verified if name.endswith((
        'amplifier_provider.py','tool_chat_worker.py','qwen38_runtime.py','amplifier_contracts.py',
        'amplifier_session.py','amplifier_tools.py','exploration.py','progressive_ui.py'))}
    result={'schema_version':'locua-local-campaign-baseline-v1','created_at_ns':time.time_ns(),
        'installed_identity':record(identity_path),'installed_files_verified_now':verified,
        'installed_cli':str(ROOT/'dist/guided-installed-wheel-v2/bin/locua'),
        'wheel':record(wheel),'tool_interface':identity['tool_interface'],
        'provider_integration':identity['provider_integration'],'presentation_build':identity['presentation_build'],
        'source_files':source_files,'local_runtime_source':record(worker),
        'local_runtime':{k:w[k] for k in ('model_pin','decoder','sampling','enable_thinking','template_kwargs',
            'installed_dependencies','prompt_cache_policy','worker_sha256','provider_sha256','runtime_sha256',
            'native_chat_template_sha256','memory_config')},
        'limits':{'maximum_iterations':4,'maximum_campaign_hours':8,'cpu_prepare_model_calls':0,'desktop_calls':0,
            'no_new_model_or_decoder':True,'fresh_acceptance_owned_separately':True},
        'runs':runs,'independent_evidence':[record(BASE/'audit-v65-independent-001'/name) for name in (
            'local-calculator-1-diagnosis.json','fresh-1-diagnosis.json','action-reference-note.json',
            'calculator-1b-independent-reconciliation.json','calculator-2-diagnosis.json',
            'textedit-independent-reconciliation.json')],
        'presentation_equivalence':record(BASE/'cli-presentation-001/audit.json'),
        'prior_summary':record(BASE/'results-preliminary/20260919T103329.429678Z/report.json'),
        'interpretation':'No qualified winner. Existing states are exposed development regressions. Original RLCD/1.5B/7B remain separate, unchanged; this campaign baseline is ordinary local27B tool calling.'}
    write(out,result);return result


def output_records(request):
    calls={}
    for message in request['messages']:
        if message.get('role')=='assistant':
            for call in message.get('tool_calls') or []:
                calls[call['id']]={'name':call.get('name',call.get('tool')),'arguments':call.get('arguments')}
            for call in message.get('content') or []:
                if isinstance(call,dict) and call.get('type')=='tool_call':
                    value={'name':call['name'],'arguments':call['input']}
                    if call['id'] in calls and calls[call['id']]!=value:raise ValueError('Conflicting recorded call aliases')
                    calls[call['id']]=value
        if message.get('role')!='tool':continue
        content=message.get('content')
        envelope=json.loads(content) if isinstance(content,str) else content
        if not isinstance(envelope,dict) or not isinstance(envelope.get('output'),dict):
            raise ValueError('Expected retained structured tool envelope')
        call=calls.get(message.get('tool_call_id'),{})
        yield message.get('name',call.get('name')),call.get('arguments',{}),envelope['output']


def outputs(request):
    for name,_,out in output_records(request):yield name,out


def build_rubric(request, observation, *, approved_scope=None, allow_missing_action=False):
    """Derive an evaluator-only prefix frontier from reviewed state, not later acts.

    Limited to an already approved single calculation with an unambiguous valid
    issued prefix and a currently exposed matching keypad capability. Alternative
    strategies may be unscored; safe reads are never falsely labeled mutations.
    """
    source=list(outputs(request));scope_rows=[];pages=[]
    for name,out in source:
        if name=='locua_status':
            scope_rows.extend(row for row in out.get('items',[]) if isinstance(row,dict)
                and row.get('scope_id') and row.get('arithmetic_issuance'))
        for page in (out.get('overview',out),out.get('fresh_region',{})):
            if page.get('version')=='progressive-ui-v1':pages.append(page)
    if not scope_rows and approved_scope is None:raise ValueError('Reviewed scope/issuance absent from exact request')
    scope=deepcopy(scope_rows[-1] if scope_rows else approved_scope)
    for name,args,out in output_records(request):
        if (name=='locua_act' and args.get('scope_id')==scope['scope_id'] and out.get('status') in ('dispatched','verified')
                and isinstance(out.get('arithmetic_input'),dict)):
            scope['arithmetic_issuance']=deepcopy(out['arithmetic_input']);scope['latest_snapshot_id']=out['snapshot_id']
    goals=scope.get('goals',[]);witness=scope['arithmetic_issuance']
    if len(goals)!=1 or goals[0].get('kind')!='calculation' or scope.get('status')!='approved':
        raise ValueError('Single approved calculation required')
    expression=re.sub(r'\s+','',goals[0]['expression']);prefix=witness.get('issued_expression_since_clear')
    if (witness.get('known_start') is not True or witness.get('issued_evaluation') is not None
            or not isinstance(prefix,str) or not expression.startswith(prefix) or prefix==expression
            or not re.fullmatch(r'[0-9()+*/.%-]+',expression)):
        raise ValueError('Unambiguous unfinished arithmetic prefix required')
    sid=scope['latest_snapshot_id']
    if observation['snapshot_id']!=sid:raise ValueError('Observation does not match exposed current scope')
    from locua.arithmetic_input import symbol
    controls={c['id']:c for c in observation['controls']};actions=[];exposed=set();detailed=set()
    for page in pages:
        if page.get('snapshot_id')!=sid:continue
        if page.get('operation')=='list':
            cols=page['columns']
            for raw in page.get('items',[]):
                row=dict(zip(cols,raw));cid=row['id'];exposed.add(cid)
                if cid not in controls:raise ValueError('Exposed row absent from retained source')
                for action in row.get('actions',[]):
                    actions.append({'action_id':action[0],'kind':action[1], 'requires_value':action[2],
                        'control_id':cid,'name':row.get('name'),'role':row.get('role'),
                        'symbol':symbol(controls[cid]),'enabled':row.get('states',{}).get('enabled')})
        elif page.get('operation')=='control':detailed.add(page['control_id'])
    ids=[a['action_id'] for a in actions]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate exposed action IDs require explicit reconciliation')
    desired=expression[len(prefix)]
    progressing=[a['action_id'] for a in actions if a['kind']=='press' and a['symbol']==desired and a['enabled'] is True]
    if not progressing and not allow_missing_action:raise ValueError('No exposed capability advances the literal reviewed prefix')
    return {'schema_version':VERSION,'never_send_to_model':True,'scope_id':scope['scope_id'],
        'snapshot_id':sid,'goal':goals[0],'reviewed_expression':expression,'issued_prefix':prefix,
        'progress_basis':'Evaluator-only literal continuation of an already-issued valid reviewed expression; no computed answer and no future action used.',
        'progress_action_ids':progressing,'all_exposed_actions':actions,'source_control_count':len(controls),
        'exposed_control_ids':sorted(exposed),'detailed_control_ids':sorted(detailed),
        'visible_pages':pages,'needed_scope_issuance_and_next_action_present':bool(progressing),
        'limitations':['Read novelty is separate from demonstrated task advancement.',
            'Alternative arithmetic strategies/navigation are not automatically incorrect, but this one-step rubric cannot establish them.',
            'One selected action is not executed or verified completion.']}


def freeze_case(run,call,out):
    run=Path(run);out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700)
    request_path=run/f'provider/call-{call:03}-input.json';request=read(request_path)
    scope_rows=[v for _,o in outputs(request) for v in o.get('items',[]) if isinstance(v,dict) and v.get('arithmetic_issuance')]
    if not scope_rows:raise ValueError('No exposed reviewed state')
    sid=scope_rows[-1]['latest_snapshot_id'];matches=[]
    for p in (run/'desktop').glob('observation-*.json'):
        obj=read(p)
        if obj.get('snapshot_id')==sid:matches.append((p,obj))
    if len(matches)!=1:raise ValueError('Current observation absent/ambiguous')
    rubric=build_rubric(request,matches[0][1])
    return save_case(request,request_path,matches[0][0],rubric,out)


def save_case(request,request_path,observation_path,rubric,out,*,transformation=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True,mode=0o700)
    validator=ROOT/'dist/guided-installed-wheel-v2/lib/python3.12/site-packages/locua/amplifier_contracts.py'
    raw=validator.read_bytes()
    # Frozen trusted source is pure representation validation, not model code.
    with os.fdopen(os.open(out/'validator.py',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:f.write(raw)
    raw_request=request_path.read_bytes() if transformation is None else (json.dumps(request,ensure_ascii=False,indent=2)+'\n').encode()
    with os.fdopen(os.open(out/'request.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:f.write(raw_request)
    write(out/'rubric-private.json',rubric)
    manifest={'schema_version':VERSION,'created_at_ns':time.time_ns(),'source_request':record(request_path),
        'request_sha256':sha(request),'request_file_sha256':sha((out/'request.json').read_bytes()),
        'rubric_sha256':sha((out/'rubric-private.json').read_bytes()),'validator_sha256':sha(raw),
        'helper_sha256':sha(Path(__file__).read_bytes()),'source_observation':record(observation_path),
        'arithmetic_symbol_source':record(ROOT/'src/locua/arithmetic_input.py'),
        'request_changed':transformation is not None,'transformation':transformation,'candidate_filtering':False,'development_state':True,
        'expected_action_only_in_private_rubric':True,'model_calls':0,'desktop_calls':0}
    write(out/'manifest.json',manifest);return manifest


def reconstructed_actions(observation):
    """Invoke the pure existing catalog method with a model-free recorded owner.

    No DesktopTools constructor, connection, inventory or mutation is invoked.
    Catalog hashes are cross-checked against delivered archived actions below.
    """
    from types import SimpleNamespace
    from threading import RLock
    from locua.desktop_tools import DesktopTools
    owner=SimpleNamespace(_lock=RLock(),_catalogs={},_issued_observation=lambda _:None,
        _result=lambda operation,**result:result)
    return DesktopTools.actions(owner,observation)['actions']


def freeze_suite(run,out):
    """Freeze immediate call14 base/new views and unchanged saturated call16."""
    from unittest.mock import patch
    from locua.interaction_context import post_action_views
    from locua.task_observation_scope import TaskObservationScope
    run=Path(run);out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700)
    before_event=read(run/'desktop/event-013.json')
    source_output=before_event.get('model_result') or before_event['result']
    before_sid=before_event['input']['snapshot_id'];after_sid=source_output['snapshot_id']
    observations={}
    for path in (run/'desktop').glob('observation-*.json'):
        obs=read(path);observations[obs['snapshot_id']]=(path,obs)
    before=observations[before_sid][1];after=observations[after_sid][1]
    old_actions=reconstructed_actions(before);actions=reconstructed_actions(after)
    acted=next(a for a in old_actions if a['id']==before_event['input']['action_id'])
    # No wall-clock staleness is invented for a historical frozen observation.
    with patch('time.time_ns',return_value=after['observed_at_ns']):
        views=post_action_views(before,acted['control_id'],after,actions)
    review=read(run/'desktop/event-007.json');approved={'scope_id':review['result']['scope_id'],
        'goals':review['input']['goals'],'status':'approved',
        'latest_snapshot_id':review['result']['review_capture']['snapshot_id'],
        'arithmetic_issuance':review['result']['arithmetic_input']}
    request_path=run/'provider/call-014-input.json';original=read(request_path);candidate=deepcopy(original)
    indices=[i for i,m in enumerate(candidate['messages']) if m.get('role')=='tool' and m.get('name')=='locua_act']
    if not indices:raise ValueError('No last act response in call14')
    index=indices[-1];message=candidate['messages'][index];envelope=json.loads(message['content'])
    if envelope['output']['snapshot_id']!=after_sid:raise ValueError('Last act is not the declared post-input state')
    app_events=read(run/'desktop/event-001.json');launch=read(run/'desktop/event-002.json')
    app=next(a for a in app_events['result']['items'] if a['app_id']==launch['input']['app_id'])
    scope=TaskObservationScope(read(run/'summary.json')['request'],app)
    scope.register_windows(read(run/'desktop/event-003.json')['result']['windows'],app['app_id'])
    # Register only captured observations up to the selected post-input point.
    for _,obs in observations.values():
        if obs['observed_at_ns']<=after['observed_at_ns']:scope.register_observation(obs)
    envelope['output'].update(views)
    envelope['output']=scope.project('locua_act',envelope['output'])
    message['content']=json.dumps(envelope,indent=2)
    check=deepcopy(candidate);check['messages'][index]=original['messages'][index]
    if check!=original:raise ValueError('Transformation escaped the one declared tool result')
    # The model receives the same goal, tools and competitors, never this rubric.
    manifests=[]
    for name,request,change in [('baseline-call014',original,None),('fresh-region-call014',candidate,{
        'kind':'replace_only_last_act_observation_views','message_index':index,
        'source_event':record(run/'desktop/event-013.json'),'renderer':record(ROOT/'src/locua/interaction_context.py'),
        'render_time_basis':'exact recorded post-input observed_at_ns','privacy_projection':record(ROOT/'src/locua/task_observation_scope.py'),
        'all_other_messages_and_tools_unchanged':True,'original_feedback_retained':True,
        'no_new_capture_or_future_model_answer':True})]:
        rubric=build_rubric(request,after,approved_scope=approved,allow_missing_action=True)
        manifest=save_case(request,request_path,observations[after_sid][0],rubric,out/name,transformation=change)
        manifests.append({'id':name,'manifest_sha256':sha((out/name/'manifest.json').read_bytes()),
            'request_sha256':manifest['request_sha256']})
    name='saturated-call016';manifest=freeze_case(run,16,out/name)
    manifests.append({'id':name,'manifest_sha256':sha((out/name/'manifest.json').read_bytes()),'request_sha256':manifest['request_sha256']})
    result={'schema_version':VERSION,'cases':manifests,'helper_sha256':sha(Path(__file__).read_bytes()),
        'source_run':str(run.resolve()),'fixed_calls':3,'fresh_models_per_case':True,
        'execution':'Local-only pinnedqwen38 nonthinking ordinary provider; no desktop tool dispatch. One generation per case; stop on terminal error.',
        'baseline_missing_next_ref_is_not_model_failure':True,'saturated_context_is_separate_regression':True}
    write(out/'suite.json',result);return result


async def run_suite(directory,out,config=None):
    """Explicit GPU command; never invoked by prepare, score or tests."""
    from provider_connection import make_provider
    from model_comparison import terminal_provider_failure
    from amplifier_core.message_models import ChatRequest
    directory=Path(directory);out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700)
    suite=read(directory/'suite.json');rows=[];terminal=None
    if suite['helper_sha256']!=sha(Path(__file__).read_bytes()):raise ValueError('Suite evaluator changed')
    for entry in suite['cases']:
        case=directory/entry['id']
        if sha((case/'manifest.json').read_bytes())!=entry['manifest_sha256']:raise ValueError('Case freeze changed')
        request,rubric,validate,manifest=load_case(case)
        row={'case_id':entry['id'],'request_sha256':manifest['request_sha256'],'desktop_calls':0}
        if terminal:
            row.update(status='unrun',terminal_failure=terminal);rows.append(row);continue
        provider=make_provider('local','qwen38',out/entry['id']/'provider',config=config,thinking=False,max_calls=1)
        started=time.monotonic()
        try:
            # Reproduce trusted framework normalization for these frozen actual
            # ToolResult envelopes; unrelated text is never parsed as authority.
            for message in request['messages']:
                if message.get('role')=='tool':
                    provider.register_structured_tool_result(message['tool_call_id'],json.loads(message['content']))
            response=await provider.complete(ChatRequest.model_validate(deepcopy(request)))
            row.update(status='returned',response=response.model_dump(),evaluation=evaluate(response.model_dump(),rubric,validate))
        except Exception as error:
            row.update(status='failed',error_type=type(error).__name__,error=str(error))
            terminal=terminal_provider_failure(provider,error)
        finally:
            await provider.close();row.update(provider_closed=provider._closed,wall_seconds=time.monotonic()-started)
        write(out/(entry['id']+'.json'),row);rows.append(row)
    result={'schema_version':VERSION,'suite':record(directory/'suite.json'),'results':rows,
        'planned_calls':len(suite['cases']),'attempted_calls':sum(r['status']!='unrun' for r in rows),
        'unrun_calls':sum(r['status']=='unrun' for r in rows),'terminal_failure':terminal,
        'desktop_calls':0,'fresh_runtime_per_case':True,'no_task_completion_claim':True}
    write(out/'summary.json',result);return result


def load_case(directory):
    d=Path(directory);m=read(d/'manifest.json')
    for filename,key in [('request.json','request_file_sha256'),('rubric-private.json','rubric_sha256'),('validator.py','validator_sha256')]:
        if sha((d/filename).read_bytes())!=m[key]:raise ValueError('Frozen file changed: '+filename)
    if sha(Path(__file__).read_bytes())!=m['helper_sha256']:raise ValueError('Frozen evaluator changed')
    spec=importlib.util.spec_from_file_location('campaign_frozen_validator',d/'validator.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    request=read(d/'request.json')
    if sha(request)!=m['request_sha256']:raise ValueError('Frozen request changed')
    return request,read(d/'rubric-private.json'),module.validate_tool_arguments,m


def evaluate(response,rubric,validate):
    calls=response.get('tool_calls') or [];rows=[]
    for call in calls:
        name=call.get('name',call.get('tool'));args=call.get('arguments')
        try:validate(name,args)
        except Exception as error:
            rows.append({'tool':name,'classification':'invalid_arguments','representation_valid':False,'task_progress':False,'error_type':type(error).__name__});continue
        label='supported_operation_progress_unproved';progress=False;grounded=None
        if name=='locua_act':
            action=next((a for a in rubric['all_exposed_actions'] if a['action_id']==args.get('action_id')),None)
            grounded=(args.get('scope_id')==rubric['scope_id'] and args.get('snapshot_id')==rubric['snapshot_id'] and action is not None)
            if not grounded:label='wrong_or_unexposed_action_reference'
            elif set(args)-{'scope_id','snapshot_id','action_id'}:label='unexpected_action_payload'
            elif args['action_id'] in rubric['progress_action_ids']:label='reviewed_prefix_advancement';progress=True
            elif action['symbol'] is not None:label='different_arithmetic_input_no_progress_credit'
            else:label='observed_action_scope_or_progress_requires_manual_audit'
        elif name=='locua_status':
            if args.get('scope_id',rubric['scope_id'])!=rubric['scope_id']:label='wrong_or_unexposed_scope'
            elif args.get('operation','summary') in ('summary','effects','goals','scopes'):
                label=('context_read_when_needed_state_already_present' if rubric['needed_scope_issuance_and_next_action_present']
                    else 'context_or_action_evidence_acquisition_not_task_progress')
            else:label='status_read_progress_unproved'
        elif name=='locua_inspect':
            if args.get('snapshot_id')!=rubric['snapshot_id']:label='wrong_or_unexposed_snapshot'
            elif args.get('cursor'):label='continuation_progress_requires_result_evidence'
            elif args.get('operation')=='control':
                cid=args.get('control_id')
                if cid not in rubric['exposed_control_ids']:label='unexposed_control_reference'
                elif cid in rubric['detailed_control_ids']:label='repeated_control_detail'
                else:label='additional_detail_without_established_progress_need'
            else:
                repeated=any(p.get('operation')==args.get('operation') and
                    p.get('coverage',{}).get('remaining_count')==0 and
                    all(p.get('coverage',{}).get('scope',{}).get(k)==args.get(k) for k in ('region_id','role','query'))
                    for p in rubric['visible_pages'] if p.get('snapshot_id')==rubric['snapshot_id'])
                label='repeated_complete_exposed_page' if repeated else 'inspection_novelty_requires_result_evidence'
        elif name in ('locua_observe','locua_activate'):label='recapture_or_activation_need_not_established'
        elif name=='locua_verify':label='premature_verification_issued_expression_incomplete'
        rows.append({'tool':name,'classification':label,'representation_valid':True,'grounded_action':grounded,'task_progress':progress})
    return {'schema_version':VERSION,'calls':rows,'tool_call_count':len(calls),
        'immediate_task_progress':len(rows)==1 and rows[0]['task_progress'],
        'one_tool_policy_satisfied':len(rows)==1,'read_only_nonprogress':bool(rows) and all(r['tool'] in ('locua_inspect','locua_status','locua_observe') for r in rows),
        'task_completed':False,'desktop_calls':0,'literal_prefix_rubric_not_general_strategy_proof':True}


def score_case(directory,response_path,out,request_sha256):
    _,rubric,validate,manifest=load_case(directory)
    if request_sha256!=manifest['request_sha256']:raise ValueError('Response request identity differs from frozen input')
    response=read(response_path)
    result={'request_sha256':request_sha256,'manifest':record(Path(directory)/'manifest.json'),
        'response':record(response_path),'response_request_binding':'Caller-supplied exact request hash; reconcile provider request artifacts independently.',
        'evaluation':evaluate(response,rubric,validate),'source_evaluator_sha256':sha(Path(__file__).read_bytes())}
    write(out,result);return result


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('baseline');b.add_argument('--out',type=Path,default=CAMPAIGN/'baseline.json')
    f=sub.add_parser('freeze');f.add_argument('--run',type=Path,required=True);f.add_argument('--call',type=int,required=True);f.add_argument('--out',type=Path,required=True)
    f=sub.add_parser('freeze-suite');f.add_argument('--run',type=Path,required=True);f.add_argument('--out',type=Path,required=True)
    s=sub.add_parser('score');s.add_argument('--freeze',type=Path,required=True);s.add_argument('--response',type=Path,required=True);s.add_argument('--request-sha256',required=True);s.add_argument('--out',type=Path,required=True)
    g=sub.add_parser('run');g.add_argument('--freeze',type=Path,required=True);g.add_argument('--out',type=Path,required=True);g.add_argument('--config')
    a=p.parse_args()
    if a.command=='baseline':r=seal_baseline(a.out)
    elif a.command=='freeze':r=freeze_case(a.run,a.call,a.out)
    elif a.command=='freeze-suite':r=freeze_suite(a.run,a.out)
    elif a.command=='run':
        import asyncio
        r=asyncio.run(run_suite(a.freeze,a.out,a.config))
    else:r=score_case(a.freeze,a.response,a.out,a.request_sha256)
    print(json.dumps({'status':'saved','output':str(a.out),'version':r.get('schema_version')}))


if __name__=='__main__':main()
