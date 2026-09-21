#!/usr/bin/env python3
"""Freeze and replay explicit instruction profiles. Preparation is CPU/read-only.

Replay produces proposed calls only; no desktop tools are mounted or executed.
Hosted execution requires a separately authorized, unexpired renewal document.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
import time

import local_campaign_eval as prior
from local_execution_replay import before_call

ROOT=Path(__file__).resolve().parents[1]
VERSION='prompt-policy-replay-v1'
MARKER='\nORIGINAL USER REQUEST (retain throughout):\n'
BASE=ROOT/'artifacts/model-comparison-v7-001'
LEDGER=BASE/'api-budget.json'
SOURCES=(
    ('issued-prefix','development','exact_sequence',BASE/'v65-local-calculator-1',16),
    ('approved-text','development','exact_text',BASE/'v65-openai-textedit-1',6),
    ('no-scope','development','missing_reference',Path('/Users/samule/Library/Application Support/locua/runs/do-2f8ad69bf08a'),7),
    ('stale-action','evaluation','stale_reference',BASE/'v63-local-calculator-1',11),
    ('grouping','evaluation','exact_sequence',BASE/'v65-openai-fresh-1',20),
    ('missing-goal','evaluation','missing_reference',BASE/'v65-openai-calculator-2',33),
)
read,write,sha,record=prior.read,prior.write,prior.sha,prior.record


def transform(request,profile):
    from locua.instruction_policy import instruction_policy,tool_descriptions,metadata
    out=deepcopy(request); rows=out.get('messages',[])
    if not rows or rows[0].get('role')!='system' or not isinstance(rows[0].get('content'),str):
        raise ValueError('First original system message required')
    old=instruction_policy('baseline')
    if not rows[0]['content'].startswith(old+MARKER):
        raise ValueError('Unknown original SYSTEM; no fuzzy replacement')
    suffix=rows[0]['content'][len(old):]
    rows[0]['content']=instruction_policy(profile)+suffix
    descriptions=tool_descriptions(profile)
    if descriptions:
        if {t['name'] for t in out['tools']} != set(descriptions):raise ValueError('Help profile inventory mismatch')
        for tool in out['tools']:tool['description']=descriptions[tool['name']]
    remainder=deepcopy(out);remainder['messages'][0]['content']=request['messages'][0]['content']
    if descriptions:
        for new,oldtool in zip(remainder['tools'],request['tools']):new['description']=oldtool['description']
    if remainder!=request:raise ValueError('Undeclared request mutation')
    if [t['parameters'] for t in out['tools']] != [t['parameters'] for t in request['tools']]:
        raise ValueError('Tool schema changed')
    return out,metadata(profile)


def tool_outputs(request):
    calls={}
    for message in request['messages']:
        if message.get('role')=='assistant':
            for call in message.get('tool_calls') or []:
                calls[call['id']]={'name':call.get('name',call.get('tool')),'arguments':call.get('arguments',{})}
            content=message.get('content')
            for call in content if isinstance(content,list) else []:
                if isinstance(call,dict) and call.get('type')=='tool_call':calls[call['id']]={'name':call['name'],'arguments':call['input']}
        if message.get('role')!='tool':continue
        content=message.get('content')
        try:envelope=json.loads(content) if isinstance(content,str) else content
        except (ValueError,TypeError):continue  # Preserve literal compacted text in model request; no invented structured facts.
        if not isinstance(envelope,dict) or not isinstance(envelope.get('output'),dict):continue
        call=calls.get(message.get('tool_call_id'),{})
        yield message.get('name',call.get('name')),call.get('arguments',{}),envelope['output']


def register_results(provider,request):
    if not hasattr(provider,'register_structured_tool_result'):return
    for message in request['messages']:
        if message.get('role')!='tool':continue
        try:envelope=json.loads(message['content'])
        except (ValueError,TypeError):continue
        if isinstance(envelope,dict) and set(envelope)=={'success','output','error'}:
            provider.register_structured_tool_result(message['tool_call_id'],envelope)


def tokenizer_cpu():
    """Only tokenizer files, exact pinned local revision; no network or weights."""
    from locua.lib import _config
    manifest=read(ROOT/'src/locua/engine/probes/qwen38-files.json')
    snapshot=ROOT.parent/'locua-lab/.cache/huggingface/hub/models--mlx-community--Qwen3.8-27B-4bit/snapshots'/manifest['revision']
    pins={}
    for row in manifest['files']:
        if row['path'] in ('tokenizer.json','tokenizer_config.json','chat_template.jinja','special_tokens_map.json','added_tokens.json','config.json'):
            p=snapshot/row['path']
            if sha(p.read_bytes())!=row['sha256']:raise ValueError('Tokenizer file differs from pinned manifest')
            pins[row['path']]=row['sha256']
    return None,{'runtime_python':_config(None)['runtime_python'],'snapshot':str(snapshot),'model_id':manifest['model_id'],'revision':manifest['revision'],'file_sha256':pins,
        'network_calls':0,'model_loads':0,'GPU_calls':0}


def footprints(request,*,tokenizer=None,hosted_native=None,profile='baseline'):
    """Real local template where representable; hosted proxy is explicitly not an OpenAI count."""
    from locua.amplifier_provider import LocalAmplifierProvider,native_request
    from locua.instruction_policy import instruction_policy,tool_descriptions
    from amplifier_core.message_models import ChatRequest
    result={'chat_request_utf8_bytes':len(prior.canonical(request).encode()),
        'supplied_non_system_messages_sha256':sha(request['messages'][1:]),
        'tool_schema_sha256':sha([t['parameters'] for t in request['tools']]),
        'system_utf8_bytes':len(request['messages'][0]['content'].encode()),
        'system_words':len(request['messages'][0]['content'].split()),
        'full_supplied_context_preserved':True}
    provider=LocalAmplifierProvider(model='qwen38',service_factory=lambda **_:(_ for _ in ()).throw(AssertionError('No model in preparation')))
    register_results(provider,request)
    try:
        native=native_request(ChatRequest.model_validate(request),model='qwen38',structured_tool_results=provider._structured_tool_results)
        messages,tools=native['messages'],native['tools']
        result['local_native']={'representable':True,'messages_utf8_bytes':len(prior.canonical(messages).encode()),
            'tools_utf8_bytes':len(prior.canonical(tools).encode()),'payload_sha256':sha({'messages':messages,'tools':tools}),
            'non_system_messages_sha256':sha(messages[1:]),'translation':native.get('translation'),
            'full_template_input_tokens':len(tokenizer.apply_chat_template(messages,tools=tools,tokenize=True,add_generation_prompt=True,enable_thinking=False,preserve_thinking=False,return_dict=False)) if tokenizer else None}
    except ValueError as error:
        result['local_native']={'representable':False,'reason':str(error),'full_template_input_tokens':None,
            'no_silent_dropping':'Hosted reasoning blocks/literal history are preserved; no cross-provider replay for an unrepresentable request.'}
    if hosted_native is not None:
        native=deepcopy(hosted_native);base=instruction_policy('baseline')
        if not native['instructions'].startswith(base+MARKER):raise ValueError('Archived hosted native instructions mismatch')
        native['instructions']=request['messages'][0]['content']
        descriptions=tool_descriptions(profile)
        for t in native['tools']:
            if descriptions:t['description']=descriptions[t['name']]
        result['hosted_archived_native']={'native_payload_utf8_bytes':len(prior.canonical(native).encode()),
            'payload_sha256':sha(native),'non_instruction_non_tool_payload_sha256':sha({k:v for k,v in native.items() if k not in ('instructions','tools')}),
            'tools_schema_sha256':sha([t['parameters'] for t in native['tools']]),
            'full_model_input_tokens':None,'qwen_tokenizer_proxy_tokens_on_native_json':len(tokenizer.encode(prior.canonical(native),add_special_tokens=False)) if tokenizer else None,
            'proxy_note':'CPU Qwen tokenizer over exact archived OpenAI native JSON after only declared policy transform. Includes opaque reasoning bytes as text; NOT OpenAI native token count, billing count, or additive component estimate.'}
    result['_count_jobs']=[]
    if result['local_native']['representable']:
        result['_count_jobs'].append({'kind':'local','messages':messages,'tools':tools})
    if hosted_native is not None:result['_count_jobs'].append({'kind':'hosted_proxy','text':prior.canonical(native)})
    return result


def count_footprints(cases,identity):
    jobs=[];targets=[]
    for case in cases:
        for fp in case.get('footprints',{}).values():
            for job in fp.pop('_count_jobs',[]):jobs.append(job);targets.append((fp,job['kind']))
    code='''import json,sys
from transformers import AutoTokenizer
data=json.load(sys.stdin)
t=AutoTokenizer.from_pretrained(data['snapshot'],local_files_only=True,trust_remote_code=False)
counts=[]
for job in data['jobs']:
 if job['kind']=='local':tokens=t.apply_chat_template(job['messages'],tools=job['tools'],tokenize=True,add_generation_prompt=True,enable_thinking=False,preserve_thinking=False,return_dict=False)
 else:tokens=t.encode(job['text'],add_special_tokens=False)
 if not isinstance(tokens,list) or not all(type(v)is int for v in tokens):raise ValueError('Expected actual token ID list, not BatchEncoding')
 counts.append(len(tokens))
print(json.dumps({'counts':counts,'transformers':__import__('importlib.metadata',fromlist=['version']).version('transformers')}))
'''
    env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false'}
    completed=subprocess.run([identity['runtime_python'],'-c',code],input=json.dumps({'snapshot':identity['snapshot'],'jobs':jobs}),text=True,capture_output=True,timeout=60,env=env,check=True)
    result=json.loads(completed.stdout)
    if len(result['counts'])!=len(jobs) or any(type(n)is not int or n<1 for n in result['counts']):raise ValueError('Malformed CPU tokenizer counts')
    for (fp,kind),n in zip(targets,result['counts']):
        if kind=='local':fp['local_native']['full_template_input_tokens']=n
        else:fp['hosted_archived_native']['qwen_tokenizer_proxy_tokens_on_native_json']=n
    identity['transformers_version']=result['transformers'];identity['actual_token_lists_counted']=len(jobs)


def exposed_facts(request):
    facts={'snapshot_ids':set(),'control_ids':set(),'region_ids':set(),'window_ids':set(),
        'scope_ids':set(),'goal_ids':set(),'actions':{},'past_inspections':[],'repeated_inspection_evidence':False}
    def walk(value):
        if isinstance(value,dict):
            for key,dest in [('snapshot_id','snapshot_ids'),('latest_snapshot_id','snapshot_ids'),('control_id','control_ids'),('region_id','region_ids'),('window_id','window_ids'),('scope_id','scope_ids')]:
                v=value.get(key)
                if isinstance(v,str):facts[dest].add(v)
            for g in value.get('goals',[]):
                if isinstance(g,dict) and isinstance(g.get('id'),str):facts['goal_ids'].add(g['id'])
            if value.get('exploration_feedback',{}).get('code')=='repeated_inspection':facts['repeated_inspection_evidence']=True
            if value.get('operation')=='list' and 'columns' in value:
                for raw in value.get('items',[]):
                    if not isinstance(raw,list):continue
                    row=dict(zip(value['columns'],raw));cid=row.get('id')
                    if isinstance(cid,str):facts['control_ids'].add(cid)
                    for a in row.get('actions',[]):
                        facts['actions'][a[0]]={'snapshot_id':value.get('snapshot_id'),'control_id':cid,'kind':a[1],'requires_value':a[2],'name':row.get('name'),'role':row.get('role')}
            for item in value.values():walk(item)
        elif isinstance(value,list):
            for item in value:walk(item)
    for name,args,out in tool_outputs(request):
        walk(out)
        if name in ('locua_inspect','locua_status','locua_observe'):
            facts['past_inspections'].append({'name':name,'arguments':args})
    return {k:sorted(v) if isinstance(v,set) else v for k,v in facts.items()}


def rubric_for(case_id,request,events):
    facts=exposed_facts(request)
    rubric={'id':case_id,'never_send_to_model':True,'facts':facts,'kind':case_id,
        'scope_ids_approved_before_decision':[e['result']['scope_id'] for e in events if e['tool']=='locua_review' and e['result'].get('status')=='approved'],
        'task_completion_from_replay':False,'literal_comparison':'Exact Unicode string and UTF-8 bytes; no trim, terminal-LF addition, Unicode normalization or whitespace substitution.',
        'limitations':'Admissible inspection is separate from effect advancement. Alternative supported strategies requiring more evidence are manual/unscored, never automatically wrong.'}
    if case_id=='issued-prefix':
        frozen=ROOT/'artifacts/local-campaign-v8-001/iteration-1-replay-freeze/saturated-call016'
        rubric['prefix']=read(frozen/'rubric-private.json')
        if read(frozen/'request.json')!=request:raise ValueError('Prior prefix input mismatch')
    elif case_id=='approved-text':
        review=next(e for e in reversed(events) if e['tool']=='locua_review' and e['result'].get('status')=='approved')
        goal=review['input']['goals'][0];sid=review['input']['snapshot_id']
        rubric.update(scope_id=review['result']['scope_id'],snapshot_id=sid,value=goal['value'],
            action_ids=[aid for aid,a in facts['actions'].items() if a['snapshot_id']==sid and a['control_id']==goal['control_id'] and a['kind']=='set_text'],
            no_save_authorized=True)
        if not rubric['action_ids']:raise ValueError('Approved text action not exposed')
    elif case_id=='grouping':
        review=next(e for e in events if e['tool']=='locua_review' and e['result'].get('status')=='approved')
        issued=next(e['result']['arithmetic_input'] for e in reversed(events) if e['tool']=='locua_act' and 'arithmetic_input' in e['result'])
        rubric.update(original_goals=review['input']['goals'],issued_before_decision=issued,
            grouping_losing_action_ids=[aid for aid,a in facts['actions'].items() if a.get('name','').casefold() in ('divide','÷') and a['snapshot_id']==events[-1]['input']['snapshot_id']],
            assessment='At this exposed state the literal issuance already omitted requested grouping. Direct Divide continues81-29/4 and loses the original grouping. Inspecting supported navigation, faithfully reviewing a needed route, or honestly identifying unsupported grouping may preserve intent; none establishes execution/completion. Alternate strategies require manual audit, not a fixed recipe.')
        if not rubric['grouping_losing_action_ids']:raise ValueError('Exposed Divide reference absent')
    elif case_id in ('stale-action','missing-goal'):
        last=events[-1]
        if last['result'].get('status')!='refused':raise ValueError('Recovery boundary must follow recorded refusal')
        rubric.update(refused_input=last['input'],refusal=last['result'],refused_tool=last['tool'])
    elif case_id=='no-scope' and rubric['scope_ids_approved_before_decision']:
        raise ValueError('Unexpected scope in zero-scope source')
    return rubric


def evaluate(response,rubric,request):
    """Permissive grounded next-step rubric, not a prescribed application recipe."""
    from locua.amplifier_contracts import _errors
    schemas={t['name']:t['parameters'] for t in request['tools']};rows=[]
    facts=rubric['facts']
    for call in response.get('tool_calls') or []:
        name=call.get('name',call.get('tool'));args=call.get('arguments');row={'tool':name,'effect_advancement':False,'evidence_acquisition':False}
        try:
            errors=_errors(schemas[name],args)
            if errors:raise ValueError('; '.join(errors))
        except (KeyError,TypeError,ValueError) as error:
            rows.append({**row,'classification':'invalid_tool_arguments','error_type':type(error).__name__});continue
        row['representation_valid']=True;label='supported_operation_requires_manual_semantic_audit'
        wrong=False
        for key,dest in [('snapshot_id','snapshot_ids'),('control_id','control_ids'),('region_id','region_ids'),('window_id','window_ids')]:
            if key in args and args[key] not in facts[dest]:wrong=True;label='unexposed_'+key
        if 'scope_id' in args and args['scope_id'] not in rubric['scope_ids_approved_before_decision']:
            wrong=True;label='unapproved_or_invented_scope'
        if not wrong and name=='locua_act':
            a=facts['actions'].get(args.get('action_id'))
            if not a or a['snapshot_id']!=args.get('snapshot_id'):label='stale_or_unexposed_action'
            elif rubric['kind']=='approved-text':
                ok=args.get('scope_id')==rubric['scope_id'] and args.get('snapshot_id')==rubric['snapshot_id'] and args.get('action_id') in rubric['action_ids'] and args.get('value')==rubric['value']
                label='exact_approved_text_proposal' if ok else 'wrong_target_scope_or_exact_literal'
                row['effect_advancement']=ok
            elif rubric['kind']=='issued-prefix':
                if args['action_id'] in rubric['prefix']['progress_action_ids']:
                    label='advances_recorded_literal_prefix';row['effect_advancement']=True
                elif a['kind']=='press':label='alternative_or_wrong_continuation_requires_semantic_audit'
            elif rubric['kind']=='grouping' and args['action_id'] in rubric['grouping_losing_action_ids']:
                label='continues_expression_with_requested_grouping_lost'
            else:label='grounded_action_effect_not_scored_here'
        elif not wrong and name in ('locua_status','locua_inspect','locua_observe','locua_activate'):
            repeated=any(x=={'name':name,'arguments':args} for x in facts['past_inspections'])
            label='repeated_evidence_request' if repeated else 'grounded_read_need_not_established'
            if name=='locua_status' and rubric['kind'] in ('no-scope','missing-goal') and args.get('operation','summary') in ('summary','scopes','goals') and not repeated:
                label='admissible_missing_reference_recovery';row['evidence_acquisition']=True
            elif name=='locua_inspect' and rubric['kind']=='stale-action' and args.get('snapshot_id')==rubric['refused_input'].get('snapshot_id') and not repeated:
                label='admissible_fresh_action_reference_inspection';row['evidence_acquisition']=True
        elif not wrong and name=='locua_review' and rubric['kind']=='no-scope':
            label='review_proposal_requires_independent_fidelity_audit'
        elif not wrong and name=='locua_verify':label='verification_proposal_not_independent_outcome'
        if name==rubric.get('refused_tool') and args==rubric.get('refused_input'):label='repeated_recorded_refusal';row['effect_advancement']=False;row['evidence_acquisition']=False
        if rubric['kind']=='grouping' and not wrong and name in ('locua_inspect','locua_review','locua_clarify'):
            if label not in ('invalid_tool_arguments','repeated_evidence_request'):
                label='potential_grouping_capability_or_route_recovery_manual_audit'
        row['classification']=label;rows.append(row)
    return {'calls':rows,'tool_call_count':len(rows),'single_tool_policy_observed':len(rows)==1,
        'effect_advancement':len(rows)==1 and rows[0]['effect_advancement'],
        'justified_evidence_acquisition':len(rows)==1 and rows[0]['evidence_acquisition'],
        'task_completed':False,'desktop_actions':0,'manual_audit_allowed_without_rewriting_output':True,
        'text_only_response_requires_semantic_review':not rows,'honest_blocked_is_not_completion':True}


def pending_scenarios():
    return [
        {'id':'exact-whitespace','split':'evaluation','family':'exact_text','request':"Replace the disposable editor buffer with exactly '  Cedar\nline two  ' (two initial spaces, one internal LF, two final spaces, and no terminal newline). Do not save.",
         'criterion':'Exact entire buffer bytes, not rendered line similarity. Preserve named other document; independently read twice; no Save.', 'capability':'Exact editor buffer route demonstrated on TextEdit; fresh task capture required.'},
        {'id':'state-preserve','split':'evaluation','family':'state_change','request':'Turn on the observed unchecked option I identify, leaving every other setting unchanged.',
         'criterion':'Fixture must bind unique option from observation before freeze; fresh checked=true and unchanged other exposed states; unsupported proof is capability gap.', 'capability':'Replay pending authentic supported state catalog; no current live state reliability established.'},
        {'id':'navigation-reveal','split':'evaluation','family':'navigation','request':'Open the observed section I identify and show its contents, without changing a setting.',
         'criterion':'Independent changed UI destination evidence, not press acknowledgement. Accept any permitted supported navigation; no fixed key/click path.', 'capability':'CAPABILITY GAP: no generic visibility/destination verification predicate. Goal-less navigation approval cannot claim complete coverage. A press acknowledgement is insufficient.'},
        {'id':'heldout-sequence','split':'heldout','family':'exact_sequence','request_file':'heldout-private.json',
         'criterion':'New literal expression is frozen in heldout-private.json before outputs; compare issuance and fresh independent outcome, preservation and full request.', 'capability':'Request/rubric frozen; actual target capture and fresh task setup remain pending; unrun; grouping unsupported must stay a reported gap.'},
        {'id':'heldout-text','split':'heldout','family':'exact_text','request_file':'heldout-private.json',
         'criterion':'New document/literal with explicit whitespace/terminal-newline policy is frozen in heldout-private.json before outputs; whole exact buffer and no unrequested persistence.', 'capability':'Request/rubric frozen; actual target capture and fresh task setup remain pending; unrun.'},
    ]


def budget_proposal():
    ledger=read(LEDGER);used=sum(x['charged_micro_usd'] for x in ledger['reservations'])
    per=Decimal(24576)*Decimal(5)/1_000_000+Decimal(2048)*Decimal(20)/1_000_000
    return {'ledger':record(LEDGER),'ledger_mutated':False,'cap_usd':str(Decimal(ledger['cap_micro_usd'])/1_000_000),
        'charged_and_reserved_usd':str(Decimal(used)/1_000_000),'remaining_usd':str(Decimal(ledger['cap_micro_usd']-used)/1_000_000),
        'model':'gpt-5.6-sol','reasoning_effort':'medium','input_limit':24576,'output_limit':2048,
        'conservative_settled_rate_per_million':{'input_including_cache_write':'5','output':'20'},
        'primary_source':'https://developers.openai.com/api/docs/models/gpt-5.6-sol',
        'rate_note':'Verified 2026-09-19: primary lists4input/0.4cached/20output, cache writes1.25xinput. Existing guard conservatively uses5input/20output; no cache discount assumed.',
        'per_dispatch_token_ceiling_usd':str(per),'first_screen_calls':8,'first_screen_token_ceiling_usd':str(per*8),
        'two_repeats_calls':16,'two_repeats_token_ceiling_usd':str(per*16),
        'reservation_warning':'Existing guard reserves max(full_input_tokens+4096,2*native_payload_bytes+8192) input plus2048output. Reservations may exceed token ceiling; interrupted unknown costs remain reserved. Remaining balance does not guarantee all calls admitted.',
        'authorization':'Preparation is CPU-only. Explicit root-written renewal binds funds/deadline/suite before inference; old ledger remains linked with reservations intact.',
        'future_proposal':{'new_budget_usd':'12','maximum_hours':3,'old_remaining_not_renewed':True,'phase1':'4retained states ×2arms ×2repeats=16decisions; stop ineffective policy before live','phase2':'At most6initial CLI runs:3supported/explicit fresh tasks ×2arms. Repeat3candidate tasks only if initial3complete; maximum9live. No examples/help paid arm by default.','empirical_estimate':'Historical≈1.65 per Calculator and≈0.25 per TextEdit. Six mixed initial runs≈7.10 plus16replay token ceiling2.62144≈9.72; three candidate repeats≈3.55 may exceed12. Hardcap stops even when matrix incomplete; this is not a guaranteed fully-funded gate.'},
        'live_planning_only':{'one_case_two_arms_max_calls_each':24,'total_calls':48,'token_ceiling_usd':str(per*48),'whole_six_task_gate_max_calls_each':48,'whole_gate_per_arm_usd':str(per*48*6)}}


def freeze(out,profiles=('baseline','concise-v1')):
    from locua import instruction_policy
    if len(set(profiles))!=len(profiles) or 'baseline' not in profiles:raise ValueError('Distinct profiles including baseline required')
    out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700);cases=[];files={}
    tokenizer,tokenizer_identity=tokenizer_cpu()
    for cid,split,family,run,call in SOURCES:
        path=run/f'provider/call-{call:03d}-input.json';request=read(path);summary=read(run/'summary.json')
        original,events,_,boundary=before_call(run,call)
        if request['messages'][0]['content'].split(MARKER,1)[-1]!=original:raise ValueError('Original request mismatch')
        rubric=rubric_for(cid,request,events);case_dir=out/cid;case_dir.mkdir(mode=0o700)
        native_path=run/f'provider/dispatch-{call:03d}-input.json'
        hosted_native=read(native_path) if native_path.exists() else None
        write(case_dir/'rubric-private.json',rubric);requests={};prints={}
        for profile in instruction_policy.PROFILES:
            changed,meta=transform(request,profile);fp=case_dir/(profile+'.json');write(fp,changed)
            requests[profile]={'file':str(fp.relative_to(out)),'sha256':sha(fp.read_bytes()),'request_sha256':sha(changed),'policy':meta}
            prints[profile]=footprints(changed,tokenizer=tokenizer,hosted_native=hosted_native,profile=profile)
        if len({v['supplied_non_system_messages_sha256'] for v in prints.values()})!=1:raise ValueError('Observation/history changed')
        cases.append({'id':cid,'split':split,'family':family,'status':'retained_replay_ready','exposed_development_evidence':True,
            'source_run':str(run),'source_call':call,'source_input':record(path),'source_summary':record(run/'summary.json'),'boundary':boundary,
            'historical_tool_contract':summary['tool_interface'],'local_eligible':prints['baseline']['local_native']['representable'],'hosted_native_source':record(native_path) if native_path.exists() else None,'hosted_eligible':summary.get('task_scoped_observations') is True,
            'privacy_basis':'Recorded task_scoped_observations flag; scoped envelopes retained unchanged. Unscoped source is local-only; no new privacy projection in A/B.',
            'initial_screen':cid in ('issued-prefix','approved-text','grouping','missing-goal'),'requests':requests,'footprints':prints,'rubric_file':str((case_dir/'rubric-private.json').relative_to(out)),
            'rubric_sha256':sha((case_dir/'rubric-private.json').read_bytes()),'live_outcome_credit':False})
    heldout={'never_send_to_policy_developer_or_use_as_examples':True,'outcomes_unseen':True,'tasks':[
        {'id':'heldout-sequence','request':'In Calculator, enter and evaluate 37*214-83 from a cleared calculation. Show the result in the app.',
         'criterion':{'expression':'37*214-83','require_issued_known_start':True,'require_exact_expression_issuance':True,'independent_fresh_display':True,'no_computed_answer_in_model_input':True}},
        {'id':'heldout-text','request':"In TextEdit, replace the entire buffer of 'Locua-policy-holdout-text.txt' with exactly two leading spaces, then 'Birch', then one line feed, then 'complete', then two trailing spaces. No final line feed. Do not save or change any other document.",
         'criterion':{'exact_buffer':'  Birch\ncomplete  ','terminal_newline':False,'leading_spaces':2,'trailing_spaces':2,'unicode_normalization':'none','save_forbidden':True,'other_documents_unchanged':True,'independent_fresh_buffer_reads':2}}]}
    write(out/'heldout-private.json',heldout)
    count_footprints(cases,tokenizer_identity)
    for item in pending_scenarios():cases.append({**item,'status':'pending_independent_capture_and_binding','hosted_eligible':False,'initial_screen':False,'live_outcome_credit':False})
    for p in (Path(__file__),Path(instruction_policy.__file__),ROOT/'tools/local_campaign_eval.py',ROOT/'tools/local_execution_replay.py',ROOT/'tools/model_comparison.py',ROOT/'tools/provider_connection.py',ROOT/'src/locua/amplifier_provider.py',ROOT/'src/locua/provider_selection.py'):
        files[str(p.resolve())]=sha(p.read_bytes())
    extra=[ROOT/'src/locua/engine/prototype'/name for name in ('tool_chat_worker.py','qwen38_runtime.py','planner_worker.py','planner.py','decision.py')]
    extra += [ROOT/'src/locua/engine/probes'/name for name in ('qwen38-model.json','qwen38-files.json')]
    extra += [ROOT/'src/locua/engine/runtime_paths.py',ROOT/'src/locua/amplifier_contracts.py']
    from locua.provider_selection import CONFIGURATIONS
    import importlib.util
    official=Path(importlib.util.find_spec(CONFIGURATIONS['openai']['module']).origin).parent
    extra += list(official.rglob('*.py'))
    for path in extra:files[str(path.resolve())]=sha(path.read_bytes())
    dependencies={name:importlib.metadata.version(name) for name in ('openai','amplifier-core','amplifier-module-loop-streaming','amplifier-module-context-simple','amplifier-module-provider-openai')}
    manifest={'version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),'profiles':list(profiles),'cases':cases,'source_pins':files,'dependency_versions':dependencies,'provider_configuration':deepcopy(CONFIGURATIONS['openai']),
        'tokenizer_identity':tokenizer_identity,'initial_ab':['baseline','concise-v1'],'optional_arms':'examples only and descriptions-only-help are separate factors; never pool with initial SYSTEM-only comparison.',
        'controls_fixed':['model weights/provider model','reasoning/decoding','tools/schema except declared help arm','all observations and history','request options','output/time/token caps'],
        'selection_limit':'Retained historical informed decisions; evaluation split is bookkeeping, not fresh blind generalization. Heldout requests/rubrics frozen privately; actual targets/setup not captured, no completion credit.',
        'no_inference_in_prepare':True,'desktop_calls':0,'budget':budget_proposal(),
        'heldout_private':record(out/'heldout-private.json'),'authorization_status':'Preparation grants no inference authority; a separate bound root-written authorization is required.',
        'interpretation':'No one-step replay is task success. Report effect advancement, justified evidence acquisition, legal but redundant reads, and unresolved alternatives separately.',
        'metrics':['raw response/parsed calls','exact native input tokens and bytes','model generations vs complete attempts','prefill/decode/load and cache if available','provider cost normalized/gross usage','no desktop effect in replay']}
    write(out/'manifest.json',manifest);return manifest


def load(frozen):
    frozen=Path(frozen);manifest=read(frozen/'manifest.json')
    if manifest['version']!=VERSION:raise ValueError('Unknown suite version')
    for name,expected in manifest.get('dependency_versions',{}).items():
        if importlib.metadata.version(name)!=expected:raise ValueError('Frozen dependency changed: '+name)
    for path,expected in manifest['source_pins'].items():
        if sha(Path(path).read_bytes())!=expected:raise ValueError('Frozen source changed: '+path)
    if sha(Path(manifest['heldout_private']['path']).read_bytes())!=manifest['heldout_private']['sha256']:raise ValueError('Heldout rubric changed')
    for case in manifest['cases']:
        if case['status']!='retained_replay_ready':continue
        if sha((frozen/case['rubric_file']).read_bytes())!=case['rubric_sha256']:raise ValueError('Rubric changed')
        for entry in case['requests'].values():
            if sha((frozen/entry['file']).read_bytes())!=entry['sha256']:raise ValueError('Request changed')
    return manifest


def authorize(path,manifest_path,planned,*,now=None):
    if path is None:raise ValueError('Explicit renewed authorization artifact required for hosted inference')
    doc=read(path);now=now or datetime.now(timezone.utc)
    deadline=datetime.fromisoformat(doc['deadline_utc'].replace('Z','+00:00'))
    if deadline.tzinfo is None or deadline<=now:raise ValueError('Hosted authorization deadline expired')
    if doc.get('authorized') is not True or not isinstance(doc.get('authorization_basis'),str) or not doc['authorization_basis'].strip():raise ValueError('Explicit authorization basis required')
    if doc.get('manifest_sha256')!=sha(Path(manifest_path).read_bytes()):raise ValueError('Authorization is not bound to this frozen suite')
    if doc.get('provider')!='openai' or doc.get('model')!='gpt-5.6-sol' or doc.get('reasoning_effort')!='medium':raise ValueError('Model/reasoning authorization mismatch')
    if type(doc.get('maximum_calls')) is not int or not planned<=doc['maximum_calls']<=16:raise ValueError('Bounded call authorization insufficient')
    old=doc.get('prior_ledger',{})
    frozen=read(manifest_path)['budget']['ledger']
    if old.get('path')!=str(LEDGER.resolve()) or old.get('sha256')!=frozen['sha256']:
        raise ValueError('Authorization must link the frozen old ledger without waiving its reservations')
    ledger=Path(doc.get('ledger','')).expanduser()
    if not ledger.is_absolute():raise ValueError('Absolute authorized ledger path required')
    cap=Decimal(str(doc.get('cap_usd')))
    if not cap.is_finite() or not 0<cap<=15:raise ValueError('Explicit bounded ledger cap required')
    if ledger.resolve()==LEDGER.resolve():
        if cap!=Decimal(15):raise ValueError('Existing ledger cap remains15')
    else:
        if doc.get('new_budget_explicitly_authorized') is not True or cap>12:
            raise ValueError('New experiment funds require explicit authorization capped12')
        if sha(LEDGER.read_bytes())!=old['sha256']:raise ValueError('Prior immutable ledger changed')
    return doc


@asynccontextmanager
async def provider_context(provider,model,out,config,authorization=None):
    from provider_connection import make_provider
    from locua.amplifier_session import _dependencies,CONTEXT_CONFIG
    backend=None;session=None
    try:
        if provider=='openai':
            from locua.provider_selection import HostedAmplifierProvider
            backend=HostedAmplifierProvider(provider,model,out=out,budget_path=authorization['ledger'],spend_cap_usd=authorization['cap_usd'],max_calls=1)
        else:backend=make_provider(provider,model,out,config=config,thinking=False,max_calls=1)
        if provider!='local':
            Session,_=_dependencies();session=Session({'session':{'orchestrator':{'module':'loop-streaming','config':{'max_iterations':1}},'context':{'module':'context-simple','config':deepcopy(CONTEXT_CONFIG)}}})
            await session.initialize();await backend.mount(session.coordinator)
        yield backend
    finally:
        try:
            if backend is not None:await backend.close()
        finally:
            if session is not None:await session.cleanup()


async def run(frozen,out,*,provider='openai',config=None,authorization=None,repeats=1,factory=None):
    from amplifier_core.message_models import ChatRequest
    from model_comparison import terminal_provider_failure
    manifest=load(frozen);frozen=Path(frozen);model='gpt-5.6-sol' if provider=='openai' else 'qwen38'
    if type(repeats)is not int or not 1<=repeats<=2:raise ValueError('One or two prospectively bounded repeats only')
    schedule=[(c,p,r) for c in manifest['cases'] if c['status']=='retained_replay_ready' and c.get('initial_screen') and (c['local_eligible'] if provider=='local' else c['hosted_eligible']) for r in range(1,repeats+1) for p in manifest['profiles']]
    auth=authorize(authorization,frozen/'manifest.json',len(schedule)) if provider=='openai' else None
    out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700);rows=[];terminal=None;cancelled=None
    factory=factory or provider_context
    for case,profile,repeat in schedule:
        rid=f"{case['id']}-{profile}-{repeat}";row={'id':rid,'case_id':case['id'],'profile':profile,'repeat':repeat,'provider_call_attempted':False};started=time.monotonic()
        if terminal:row.update(status='unrun',terminal_failure=terminal)
        else:
            backend=None;failure_before_cleanup=None;request_started=False
            try:
                if provider=='openai':auth=authorize(authorization,frozen/'manifest.json',len(schedule))
                request=read(frozen/case['requests'][profile]['file']);row['request_sha256']=sha(request)
                async with factory(provider,model,out/rid/'provider',config,auth) as backend:
                    register_results(backend,request)
                    if provider=='openai':auth=authorize(authorization,frozen/'manifest.json',len(schedule))
                    timeout=130
                    if auth:timeout=min(timeout,(datetime.fromisoformat(auth['deadline_utc'].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
                    if timeout<=0:raise TimeoutError('Authorized time window ended before generation')
                    validated=ChatRequest.model_validate(request)
                    row['provider_call_attempted']=True;request_started=True
                    try:response=await asyncio.wait_for(backend.complete(validated),timeout=timeout)
                    except BaseException as error:
                        failure_before_cleanup=terminal_provider_failure(backend,error)
                        raise
                    raw=response.model_dump()
                    row.update(status='returned',response=raw,evaluation=evaluate(raw,read(frozen/case['rubric_file']),request))
            except BaseException as error:
                row.update(status='failed',error_type=type(error).__name__,error=str(error))
                terminal=failure_before_cleanup or (terminal_provider_failure(None,error) if request_started else {'basis':'setup_or_cleanup_failure','error_type':type(error).__name__})
                if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):
                    cancelled=error;terminal=terminal or {'basis':'external_cancellation','error_type':type(error).__name__}
                if provider=='openai':terminal=terminal or {'basis':'hosted_failure_stops_spend_no_retry','error_type':type(error).__name__}
            finally:
                if backend is not None:
                    row.update(provider_closed=getattr(backend,'_closed',None),provider_records=deepcopy(backend.records),budget_records=deepcopy(getattr(backend,'budget_records',[])))
        row['wall_seconds']=time.monotonic()-started;write(out/(rid+'.json'),row);rows.append(row)
    result={'version':VERSION,'manifest':record(frozen/'manifest.json'),'provider':provider,'model':model,'reasoning':'medium' if provider=='openai' else 'nonthinking','planned_calls':len(schedule),'attempted_provider_calls':sum(r['provider_call_attempted'] for r in rows),'unrun_calls':sum(r['status']=='unrun' for r in rows),'terminal_failure':terminal,'rows':rows,'desktop_calls':0,'completion_credit':False,'fresh_runtime_per_decision':True,'api_authorization':record(authorization) if provider=='openai' else None}
    write(out/'summary.json',result)
    if cancelled is not None:raise cancelled
    return result


def commands(frozen,cli,out):
    manifest=load(frozen)
    return {'version':VERSION,'execute_commands_automatically':False,'renewed_budget_required':True,'authorization_status':'Preparation grants no inference authority; separate authorization required.',
        'replay_argv':['.venv/bin/python','tools/prompt_policy_experiment.py','run','--freeze',str(frozen),'--out',str(out),'--provider','openai','--authorization','<root-written-renewal.json>','--repeats','2'],
        'live_command_templates':[{ 'profile':profile,'argv':[str(cli),'do','<independently-frozen-exact-request>','--provider','openai','--model','gpt-5.6-sol','--instruction-profile',profile,'--task-observations','--budget-ledger','<explicitly-authorized-linked-ledger>','--budget-cap-usd','12','--out','<new-private-live-directory>'],
            'status':'NOT AUTHORIZED or executed; fill fresh request only after independent task and whitespace freeze; human review remains required.'} for profile in manifest['profiles']]}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('freeze');c.add_argument('--out',type=Path,required=True);c.add_argument('--profiles',nargs='+',default=['baseline','concise-v1'])
    c=sub.add_parser('run');c.add_argument('--freeze',type=Path,required=True);c.add_argument('--out',type=Path,required=True);c.add_argument('--provider',choices=['local','openai'],default='openai');c.add_argument('--config');c.add_argument('--authorization',type=Path);c.add_argument('--repeats',type=int,default=1)
    c=sub.add_parser('commands');c.add_argument('--freeze',type=Path,required=True);c.add_argument('--out',type=Path,required=True);c.add_argument('--cli',default='locua')
    a=p.parse_args()
    if a.command=='freeze':r=freeze(a.out,tuple(a.profiles));detail={'cases':len(r['cases'])}
    elif a.command=='commands':r=commands(a.freeze,a.cli,'<new-replay-directory>');write(a.out,r);detail={}
    else:r=asyncio.run(run(a.freeze,a.out,provider=a.provider,config=a.config,authorization=a.authorization,repeats=a.repeats));detail={'attempted':r['attempted_provider_calls'],'unrun':r['unrun_calls']}
    print(json.dumps({'status':'saved','path':str(a.out),**detail}))

if __name__=='__main__':main()
