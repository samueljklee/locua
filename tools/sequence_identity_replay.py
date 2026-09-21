"""Private, offline-prepared sequence identity assertion comparison.

Preparation and scoring make no model/desktop calls. Explicit `run` makes at
most four local provider calls using the existing Amplifier provider. It never
executes returned tools, changes production schemas, or substitutes arguments.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

VERSION = 'sequence-identity-replay-v1'
TOOL = 'locua_act_sequence'
ASSERTION_HELP = (' Each step must also supply expected_control_name: copy the exact observed '
    'control name, or JSON null when the observed name is null. It is an identity consistency '
    'assertion, not a requested value. An ID/name mismatch refuses the whole sequence before '
    'any input; no action is substituted or selected by name.')
NAME_SCHEMA = {'type': ['string', 'null'], 'description':
    'Exact observed control name for this action ID; JSON null only for an observed null name. No normalization or coercion.'}
DEFINITIONS = [('live2-call11', 'live-calculator-local-2', 11),
               ('live1-call13', 'live-calculator-local-1', 13)]


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def private(path, value):
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def register_results(provider, request):
    """Re-register only actual frozen framework ToolResult envelopes."""
    count=0
    for message in request['messages']:
        if message.get('role')!='tool': continue
        envelope=json.loads(message['content'])
        if not isinstance(envelope,dict) or set(envelope)!={'success','output','error'}:
            raise ValueError('Frozen tool result is not an exact framework envelope')
        provider.register_structured_tool_result(message['tool_call_id'],envelope);count+=1
    return count


def native_payload(request):
    from amplifier_core.message_models import ChatRequest
    from locua.amplifier_provider import LocalAmplifierProvider,native_request
    def no_service(**kwargs): raise AssertionError('CPU preparation must not load a model')
    provider=LocalAmplifierProvider(model='qwen38',service_factory=no_service)
    register_results(provider,request)
    result=native_request(ChatRequest.model_validate(request),model='qwen38',
                          structured_tool_results=provider._structured_tool_results)
    assert provider._service is None
    return result


def transform(request):
    """Only the sequence description, step property, and required list change."""
    output = deepcopy(request)
    tools = [t for t in output['tools'] if t['name'] == TOOL]
    if len(tools) != 1:
        raise ValueError('Exactly one original sequence tool required')
    tool = tools[0]
    item = tool['parameters']['properties']['steps']['items']
    if 'expected_control_name' in item['properties'] or 'expected_control_name' in item.get('required', []):
        raise ValueError('Source already has candidate assertion')
    item['properties']['expected_control_name'] = deepcopy(NAME_SCHEMA)
    item['required'] = [*item['required'], 'expected_control_name']
    tool['description'] += ASSERTION_HELP
    return output


def validate_sequence(arguments, schema, actions, *, candidate):
    """Evaluator-only validation. No coercion, correction, remapping or input."""
    from locua.amplifier_contracts import _errors
    errors = _errors(schema, arguments)
    if errors:
        return {'valid': False, 'category': 'schema_invalid', 'errors': errors}
    resolved = []
    for index, step in enumerate(arguments['steps'], 1):
        action = actions.get(step['action_id'])
        if action is None:
            return {'valid': False, 'category': 'unknown_action_id', 'failed_step': index}
        if action['snapshot_id'] != arguments['snapshot_id']:
            return {'valid': False, 'category': 'foreign_snapshot', 'failed_step': index}
        if candidate and (type(step['expected_control_name']) is not type(action['name'])
                          or step['expected_control_name'] != action['name']):
            return {'valid': False, 'category': 'id_name_mismatch', 'failed_step': index,
                    'selected_name': action['name'], 'asserted_name': step['expected_control_name'],
                    'would_refuse_before_input': True}
        if action['kind'] == 'press' and 'value' in step:
            return {'valid': False, 'category': 'press_value_not_supported', 'failed_step': index}
        if action['kind'] == 'set_text' and not isinstance(step.get('value'), str):
            return {'valid': False, 'category': 'exact_text_missing', 'failed_step': index}
        resolved.append(deepcopy(action))
    return {'valid': True, 'category': 'consistent_observed_sequence', 'resolved': resolved}


def score(request, rubric, response, *, candidate):
    from locua.amplifier_contracts import _errors
    from locua.arithmetic_input import InputWitness
    if hasattr(response, 'model_dump'):
        response = response.model_dump()
    tools = {t['name']: t for t in request['tools']}
    results = []
    for call in response.get('tool_calls') or []:
        name = call.get('name', call.get('tool'))
        args = call.get('arguments')
        if name not in tools:
            results.append({'category': 'unknown_tool', 'semantic_plan_matched': False}); continue
        errors = _errors(tools[name]['parameters'], args)
        if errors:
            results.append({'category': 'schema_invalid', 'errors': errors, 'semantic_plan_matched': False}); continue
        if name != TOOL:
            results.append({'category': 'other_schema_valid_tool', 'tool': name,
                            'semantic_plan_matched': False,
                            'note': 'Not a sequence comparison success; legal inspection is not task completion.'}); continue
        checked = validate_sequence(args, tools[name]['parameters'], rubric['actions'], candidate=candidate)
        row = {k: v for k, v in checked.items() if k != 'resolved'}
        row['semantic_plan_matched'] = False
        scope = rubric['scopes'].get(args['scope_id'])
        if not scope or scope['status'] != 'approved':
            row.update(valid=False, category='unapproved_scope'); results.append(row); continue
        if checked['valid']:
            resolved = checked['resolved']
            row['selected_names'] = [a['name'] for a in resolved]
            if any(a['target'] != scope['target'] for a in resolved):
                row.update(valid=False, category='foreign_reviewed_target')
            elif any(a['kind'] != 'press' or a['arithmetic_token'] is None for a in resolved):
                row.update(category='not_entirely_reviewed_arithmetic', would_require_separate_effect_analysis=True)
            else:
                witness = InputWitness()
                initial = scope['witness']
                witness.entered = initial['issued_expression_since_clear']
                witness.evaluated = initial['issued_evaluation']
                witness.known_start = initial['known_start']
                contradictions=[];evaluations=[];unknown_prefix=False
                goals=[g for g in scope['goals'] if g['kind']=='calculation']
                effects=scope.get('effects',[])
                if not goals or not effects or effects[0]!={'kind':'goal','goal_id':goals[0]['id']}:
                    row.update(category='arithmetic_effect_routing_not_proved')
                    results.append(row);continue
                goal=goals[0]
                for index,action in enumerate(resolved,1):
                    if not witness.known_start and action['arithmetic_token'] not in ('clear','clear_entry'):
                        unknown_prefix=True;break
                    witness.record(action['arithmetic_token'], snapshot_id=args['snapshot_id'], descriptor=action['name'])
                    if action['arithmetic_token']=='=' and witness.known_start:
                        evaluations.append({'step':index,'issued_expression':witness.evaluated})
                        if not witness.matches(goal['expression']):contradictions.append(index)
                row.update(predicted_issuance=witness.view(),
                           contradictory_evaluation_steps=contradictions,evaluations=evaluations,
                           semantic_plan_matched=not unknown_prefix and not contradictions and witness.matches(goal['expression']))
                row['category'] = ('contradictory_intermediate_evaluation' if contradictions else
                    'unknown_start_prefix' if unknown_prefix else
                    'matching_evaluated_issuance_plan' if row['semantic_plan_matched'] else 'issuance_plan_not_matched')
        results.append(row)
    return {'calls': results, 'one_consistent_matching_plan': len(results)==1 and results[0]['semantic_plan_matched'],
            'desktop_calls': 0, 'task_completed': False, 'runtime_authority_granted': False,
            'limits': 'Evaluator predicts issuance from observed symbols only; no delivery, transition or output verification is proved.'}


def prior_events(run, call_number):
    """Join actual returned provider calls to event order before this request."""
    expected=[]
    for n in range(1,call_number):
        summary=read(run/'provider'/f'call-{n:03d}-summary.json')
        for call in summary.get('response',{}).get('tool_calls') or []:
            expected.append((call['name'],call['arguments']))
    events=[]
    for index,(name,args) in enumerate(expected,1):
        event=read(run/'desktop'/f'event-{index:03d}.json')
        if event['tool']!=name or event['input']!=args:
            raise ValueError('Cannot establish exact preceding provider/tool event boundary')
        events.append(event)
    return events


def _rubric(run, request, call_number):
    from locua.arithmetic_input import symbol
    observations = {o['snapshot_id']: o for f in (run/'desktop').glob('observation-*.json') for o in [read(f)]}
    visible_snapshots = set()
    for message in request['messages']:
        if message.get('role') == 'tool':
            output = json.loads(message['content']).get('output', {})
            if isinstance(output.get('snapshot_id'), str): visible_snapshots.add(output['snapshot_id'])
    actions = {}
    catalog_files = sorted((run/'desktop').glob('action-catalog-*.json'))
    if catalog_files:
        catalog_rows = [a for f in catalog_files for a in read(f)['public_actions']]
    else:
        records = [json.loads(line) for line in (run/'desktop/desktop/desktop.jsonl').read_text().splitlines()]
        catalog_rows = [a for r in records if r.get('result', {}).get('operation')=='actions'
                        for a in r['result'].get('actions', [])]
    for action in catalog_rows:
        if action['snapshot_id'] not in visible_snapshots: continue
        o = observations[action['snapshot_id']]
        c = next(c for c in o['controls'] if c['id']==action['control_id'])
        assert action.get('name') == c.get('name')
        row = {**deepcopy(action), 'arithmetic_token': symbol(c)}
        if action['id'] in actions and actions[action['id']] != row:
            raise ValueError('Conflicting original action IDs')
        actions[action['id']] = row
    # Exact provider/tool boundary, not file mtime or capture age.
    prior=prior_events(run,call_number)
    scopes = {}
    for event in prior:
        result=event['result'];sid=result.get('scope_id',event['input'].get('scope_id'))
        if event['tool']=='locua_review' and result.get('status')=='approved':
            scopes[sid]={'status':'approved','target':result['target'],'goals':result['goals'],
                'effects':result['effects'],'witness':result.get('arithmetic_input'),
                'approval_event_sequence':event['sequence']}
        elif event['tool'] in ('locua_act','locua_act_sequence') and result.get('action_started') is not False:
            if (result.get('status')=='refused' and sid not in scopes
                    and result.get('reason')=='Approved, nonblocked scope required'):
                continue  # Exact pre-scope refusal; no scope capable of authorizing input.
            # These two exposed cases have zero input before the decision.
            # Do not silently infer a historical mutable scope from final state.
            raise ValueError('This frozen component requires no prior attempted task input')
    if not actions or not scopes or any(s['witness'] is None for s in scopes.values()):
        raise ValueError('Recorded action catalog and approved initial witness required')
    return {'actions':actions, 'scopes':scopes, 'never_send_to_provider':True,
            'provider_call_boundary':call_number,'preceding_tool_event_count':len(prior),
            'observation_hashes':{sid:digest(observations[sid]) for sid in visible_snapshots}}


def prepare(repo, out):
    from amplifier_core.message_models import ChatRequest
    repo=Path(repo);out=Path(out);out.mkdir(mode=0o700,parents=True,exist_ok=False)
    entries=[];rubrics={}
    for cid, run_name, call in DEFINITIONS:
        run=repo/'artifacts/action-sequence-v11-001'/run_name
        source=run/'provider'/f'call-{call:03d}-input.json';raw=source.read_bytes();original=json.loads(raw)
        candidate=transform(original)
        rubrics[cid]=_rubric(run,original,call)
        original_native=native_payload(original);archived_native=read(run/'provider'/f'call-{call:03d}-native.json')
        if original_native!=archived_native:raise ValueError('Original native payload parity failed')
        for variant, request in [('original',original),('candidate',candidate)]:
            ChatRequest.model_validate(request)
            name=cid+'-'+variant+'.json';private(out/name,raw if variant=='original' else request)
            entries.append({'case_id':cid,'variant':variant,'request_file':name,'request_sha256':digest(request),
                            'request_file_sha256':digest((out/name).read_bytes()),'source_file':str(source),
                            'source_file_sha256':digest(raw),'messages_sha256':digest(request['messages']),
                            'native_payload_sha256':digest(native_payload(request)),
                            'original_native_archive_parity':True})
    private(out/'rubric-private.json',{'version':VERSION,'cases':rubrics,'no_provider_access':True})
    source_files=[p for p in (repo/'src/locua').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    source_files += [Path(__file__).resolve(),repo/'tests/test_sequence_identity_replay.py',repo/'tools/provider_connection.py']
    manifest={'version':VERSION,'prepared_at_ns':time.time_ns(),'cases':entries,'maximum_provider_calls':4,
        'provider_policy':{'provider':'local','model':'qwen38','thinking':False,'existing_decoder':True},
        'controlled_delta':['locua_act_sequence description append only','steps.items.properties.expected_control_name add string|null',
                            'steps.items.required add expected_control_name'],
        'all_other_tools_system_context_and_messages_unchanged':True,
        'source_hashes':{str(p.relative_to(repo)):digest(p.read_bytes()) for p in sorted(source_files)},
        'rubric_sha256':digest((out/'rubric-private.json').read_bytes()),
        'frozen_exposed_decisions_not_fresh_tasks':True,'no_reprompts_or_tools_executed':True,
        'qualification_limit':'At most a copying/identity-assertion component result; not end-to-end completion or transfer.'}
    private(out/'manifest.json',manifest)
    return manifest


async def run_replays(freeze, out, provider_factory):
    from amplifier_core.message_models import ChatRequest
    freeze=Path(freeze);out=Path(out);manifest=read(freeze/'manifest.json')
    if len(manifest['cases'])!=4 or manifest['maximum_provider_calls']!=4: raise ValueError('Exactly four frozen cells required')
    repo=Path(__file__).resolve().parents[1]
    for name,expected in manifest['source_hashes'].items():
        path=repo/name
        if not path.is_file() or digest(path.read_bytes())!=expected:
            raise ValueError('Frozen source identity changed: '+name)
    if digest((freeze/'rubric-private.json').read_bytes())!=manifest['rubric_sha256']: raise ValueError('Rubric changed')
    rubric=read(freeze/'rubric-private.json')['cases'];prepared=[]
    for entry in manifest['cases']:
        path=freeze/entry['request_file'];request=read(path)
        if digest(path.read_bytes())!=entry['request_file_sha256'] or digest(request)!=entry['request_sha256']:
            raise ValueError('Frozen request changed')
        if digest(native_payload(request))!=entry['native_payload_sha256']:
            raise ValueError('Frozen native translation changed')
        prepared.append((entry,request))
    out.mkdir(mode=0o700,parents=True,exist_ok=False);results=[];terminal=None;cancelled=None
    async with provider_factory(out/'provider') as provider:
        for entry,request in prepared:
            row={'case_id':entry['case_id'],'variant':entry['variant'],'request_sha256':entry['request_sha256']}
            if terminal:
                row.update(status='unrun',reason='prior_provider_failure',provider_call_attempted=False)
            else:
                start=time.monotonic();row['provider_call_attempted']=True
                try:
                    row['structured_results_registered']=register_results(provider,request)
                    response=await asyncio.wait_for(provider.complete(ChatRequest.model_validate(deepcopy(request))),360)
                    row.update(status='returned',response=response.model_dump(),
                        evaluation=score(request,rubric[entry['case_id']],response,candidate=entry['variant']=='candidate'))
                except (Exception,asyncio.CancelledError) as error:
                    terminal={'type':type(error).__name__,'message':str(error)}
                    row.update(status='failed',error=terminal)
                    if isinstance(error,asyncio.CancelledError):cancelled=error
                row['wall_seconds']=time.monotonic()-start
            private(out/(entry['case_id']+'-'+entry['variant']+'.json'),row);results.append(row)
    summary={'version':VERSION,'calls_attempted':sum(r['provider_call_attempted'] for r in results),
        'planned_calls':4,'terminal_failure':terminal,'desktop_calls':0,'task_completion_claimed':False,
        'provider_closed':getattr(provider,'_closed',None),
        'results':[{k:v for k,v in r.items() if k!='response'} for r in results]}
    private(out/'summary.json',summary)
    if cancelled is not None:raise cancelled
    return summary


async def run_local(freeze,out,config=None):
    from provider_connection import make_provider
    from locua.amplifier_session import _dependencies,CONTEXT_CONFIG
    @asynccontextmanager
    async def factory(provider_out):
        AmplifierSession,_=_dependencies()
        session=AmplifierSession({'session':{'orchestrator':{'module':'loop-streaming','config':{'max_iterations':1}},
            'context':{'module':'context-simple','config':deepcopy(CONTEXT_CONFIG)}}})
        backend=make_provider('local','qwen38',provider_out,config=config,max_calls=4)
        try:
            await session.initialize();await session.coordinator.mount('providers',backend,name=backend.name)
            yield backend
        finally:
            await backend.close();await session.cleanup()
    return await run_replays(freeze,out,factory)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='operation',required=True)
    q=sub.add_parser('prepare');q.add_argument('--repo',default=str(Path(__file__).resolve().parents[1]));q.add_argument('--out',required=True)
    q=sub.add_parser('run');q.add_argument('--freeze',required=True);q.add_argument('--out',required=True);q.add_argument('--config')
    a=p.parse_args()
    result=prepare(a.repo,a.out) if a.operation=='prepare' else asyncio.run(run_local(a.freeze,a.out,a.config))
    print(json.dumps({k:v for k,v in result.items() if k not in ('source_hashes','cases','results')},indent=2))
