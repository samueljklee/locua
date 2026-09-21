#!/usr/bin/env python3
"""Freeze/score retained-state development replays; explicit run is inference only.

The only input intervention is one exact installed Amplifier persisted reminder.
No desktop operations, prompt repair, computed answers, or future events are used.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
import time

import local_campaign_eval as prior

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'local-execution-state-replay-v1'
SPECS = (
    ('issued-prefix16', ROOT/'artifacts/model-comparison-v7-001/v65-local-calculator-1', 16, 'issued_prefix'),
    ('no-scope7', Path.home()/'Library/Application Support/locua/runs/do-2f8ad69bf08a', 7, 'no_approved_scope'),
)
read, write, sha, record = prior.read, prior.write, prior.sha, prior.record


def before_call(run, call):
    session_path = run/'session/session.json'
    session = read(session_path)
    markers = [i for i, e in enumerate(session['events']) if e['event'] == 'provider:request']
    if not 1 <= call <= len(markers): raise ValueError('Provider request boundary absent')
    index = markers[call-1]; prefix = session['events'][:index]
    starts = [e for e in prefix if e['event'] == 'tool:pre']
    finishes = [e for e in prefix if e['event'] == 'tool:post']
    if len(starts) != len(finishes): raise ValueError('Pending tool at selected decision boundary')
    events = []
    for n, (start, finish) in enumerate(zip(starts, finishes), 1):
        if start['data']['tool_call_id'] != finish['data']['tool_call_id']:
            raise ValueError('Parallel/unreconciled tool events unsupported in recorded reconstruction')
        event = read(run/f'desktop/event-{n:03d}.json')
        if (event['sequence'] != n or event['tool'] != start['data']['tool_name']
                or event['input'] != start['data']['tool_input']):
            raise ValueError('Desktop and framework event sequence differ')
        events.append(event)
    return session['request'], events, session['events'][index]['at_ns'], {
        'source': record(session_path), 'selected_provider_call': call,
        'completed_tool_events_before_call': len(events),
        'cutoff_at_ns': session['events'][index]['at_ns'],
        'no_final_summary_or_final_owner_state_used': True}


def reconstruct(run, call):
    """Reconstruct only projection-required fields, without constructing an owner."""
    from locua.amplifier_tools import _hash
    from locua.arithmetic_input import InputWitness, symbol
    request, events, cutoff, provenance = before_call(run, call)
    owner = SimpleNamespace(evidence={'request_sha256': _hash(request), 'events': deepcopy(events)},
        _scopes={}, _latest={}, _observations={}, _window_records={}, _cancellation=None,
        exploration=SimpleNamespace(needs=[]))
    observation_paths = {}
    for path in (run/'desktop').glob('observation-*.json'):
        observation = read(path)
        if observation['observed_at_ns'] >= cutoff: continue
        sid = observation['snapshot_id']
        if sid in owner._observations: raise ValueError('Duplicate retained snapshot')
        owner._observations[sid] = observation; observation_paths[sid] = path
    for observation in sorted(owner._observations.values(), key=lambda x: x['observed_at_ns']):
        owner._latest[_hash(observation['target'])] = observation['snapshot_id']
    used_reviews = []; witness_sources = []
    for event in events:
        args, result, name = event['input'], event['result'], event['tool']
        if name == 'locua_windows' and result.get('status') == 'ok':
            for row in result['windows']:
                owner._window_records[row['window_id']] = {'target': deepcopy(row['target'])}
        elif name == 'locua_review' and result.get('status') == 'approved':
            sid = result['scope_id']; path = run/'desktop'/('review-'+sid.split(':')[1]+'.json')
            review = read(path)
            if (review['scope_id'] != sid or review['original_request'] != request
                    or review['goals'] != result['goals'] or review['effects'] != result['effects']):
                raise ValueError('Approved review artifact and preceding event disagree')
            if review['preserves']:
                raise ValueError('These two development captures require no preserve reconstruction')
            owner._scopes[sid] = {'status': 'approved', 'target': deepcopy(review['target']),
                'goals': deepcopy(review['goals']), 'preserves': [], 'effects': deepcopy(review['effects']),
                'limitations': deepcopy(args.get('limitations', [])),
                'covers_entire_request': args.get('covers_entire_request', False),
                'unresolved_requirements': deepcopy(args.get('unresolved_requirements', [])),
                'issued_press_effects': [], 'witness': InputWitness()}
            used_reviews.append(record(path))
        elif name == 'locua_act':
            if result.get('status') not in ('dispatched', 'verified'):
                raise ValueError('Unsupported uncertain/refused action in these bounded reconstruction cases')
            scope = owner._scopes[args['scope_id']]
            observation = owner._observations[args['snapshot_id']]
            actions = prior.reconstructed_actions(observation)
            matches = [a for a in actions if a['id'] == args['action_id']]
            if len(matches) != 1: raise ValueError('Issued action cannot be uniquely reconstructed')
            action = matches[0]
            control = next(c for c in observation['controls'] if c['id'] == action['control_id'])
            token = symbol(control)
            if action['kind'] != 'press' or token is None:
                raise ValueError('Only recorded arithmetic presses supported in these reconstruction cases')
            scope['witness'].record(token, snapshot_id=result['snapshot_id'], descriptor=action['description'])
            if scope['witness'].view() != result.get('arithmetic_input'):
                raise ValueError('Reconstructed issued inputs disagree with contemporaneous witness')
            witness_sources.append({'event': event['sequence'], 'before_snapshot': observation['snapshot_id'],
                'post_snapshot': result['snapshot_id'], 'recorded_action_id': action['id'],
                'symbol_source': 'full recorded control semantics and unchanged arithmetic_input.symbol'})
        elif name == 'locua_activate':
            raise ValueError('Activation invalidation requires additional reconstruction; absent in selected cases')
        if name == 'locua_status' and args.get('operation') == 'needs' and 'needs' in args:
            owner.exploration.needs = deepcopy(args['needs'])
        if result.get('status') == 'canceled': owner._cancellation = deepcopy(result)
    provenance.update(request_sha256=_hash(request), events=[record(run/f'desktop/event-{e["sequence"]:03d}.json') for e in events],
        observations=[record(p) for _, p in sorted(observation_paths.items())], reviews=used_reviews,
        witness_sources=witness_sources, reconstructed_scopes=len(owner._scopes),
        omitted_later_events=True, fields='Only fields consumed by project_execution_facts; no live owner or driver.',
        goal_text_provenance='Exact approved historical goals, including model-authored target phrase44252 if present; no evaluator result added.')
    return owner, provenance


async def persisted_message(body):
    """Call the installed loop's real persistence wrapper against a memory context."""
    import amplifier_module_loop_streaming as module
    # This is the same method called by the supported provider:request hook.
    loop = module.StreamingOrchestrator({'ephemeral_injection_mode': 'persist'})
    class Context:
        def __init__(self): self.messages = []
        async def get_messages(self): return deepcopy(self.messages)
        async def add_message(self, message): self.messages.append(deepcopy(message))
    context = Context()
    content, changed = await loop._persist_reminder(context, body, tail=True, verify_admitted=True)
    if not changed or len(context.messages) != 1 or context.messages[0]['content'] != content:
        raise ValueError('Installed reminder persistence contract changed')
    return context.messages[0]


def no_scope_rubric(request, owner):
    from locua.goal_verification import bind_for_review
    if owner._scopes: raise ValueError('No-scope rubric requires confirmed empty scope map')
    latest = set(owner._latest.values())
    if len(latest) != 1: raise ValueError('One current recorded target required')
    sid = next(iter(latest)); observation = owner._observations[sid]
    exposed = set(); detailed = set()
    for _, output in prior.outputs(request):
        if output.get('snapshot_id') != sid: continue
        if output.get('operation') == 'list':
            for row in output.get('items', []): exposed.add(dict(zip(output['columns'], row))['id'])
        if output.get('operation') == 'control':
            detailed.add(output['control_id']); exposed.add(output['control_id'])
    # This is evaluator-only request transcription, not a prompt or hidden target filter.
    expression = '192*231-100'
    if expression not in request['messages'][1]['content']:
        raise ValueError('Frozen original request expression differs')
    eligible = []
    goal = {'id':'evaluation-only', 'kind':'calculation', 'expression':expression,
        'target':'observed readout', 'evidence_plane':'display'}
    for control in observation['controls']:
        if control['id'] not in exposed: continue
        try: bind_for_review(goal, control, observation)
        except ValueError: continue
        eligible.append(control['id'])
    if not eligible: raise ValueError('No already exposed bindable arithmetic display')
    return {'kind':'no_approved_scope', 'never_send_to_model':True, 'scope_count':0,
        'snapshot_id':sid, 'expression':expression, 'exposed_control_ids':sorted(exposed),
        'detailed_control_ids':sorted(detailed), 'reviewable_display_ids':eligible,
        'no_task_inputs_recorded':not any(e['tool']=='locua_act' for e in owner.evidence['events']),
        'limitations':['One faithful review proposal is progress toward approval, not authorization or task completion.',
            'An already-correct display does not prove task issuance; no fresh solve credit from this replay.']}


def evaluate(response, rubric, validate):
    from locua.arithmetic_input import normalized
    if rubric['kind'] == 'issued_prefix':
        return prior.evaluate(response, rubric['prefix'], validate)
    calls = response.get('tool_calls') or []; rows = []
    for call in calls:
        name = call.get('name', call.get('tool')); args = call.get('arguments')
        try: validate(name, args)
        except Exception as error:
            rows.append({'tool':name,'classification':'invalid_arguments','progress':False,'error_type':type(error).__name__});continue
        label = 'other_operation_no_progress_proven'; progress = False
        if name == 'locua_status':
            label = 'recovering_absent_scope_despite_explicit_zero' if args.get('operation','summary') in (
                'summary','scopes','goals','effects','preserves','witness') else 'read_progress_unproved'
        elif name in ('locua_act','locua_verify'):
            label = 'invented_or_unapproved_scope'
        elif name == 'locua_review':
            goals = args.get('goals', [])
            faithful = (args.get('snapshot_id') == rubric['snapshot_id'] and len(goals) == 1
                and goals[0].get('kind') == 'calculation' and isinstance(goals[0].get('expression'), str)
                and normalized(goals[0]['expression']) == normalized(rubric['expression'])
                and goals[0].get('control_id') in rubric['reviewable_display_ids']
                and goals[0].get('evidence_plane') == 'display' and args.get('covers_entire_request') is True
                and not args.get('unresolved_requirements') and not args.get('preserves')
                and args.get('effects') == [{'kind':'goal','goal_id':goals[0].get('id')}])
            progress = faithful; label = 'faithful_review_proposal_no_action_authority' if faithful else 'review_fidelity_or_binding_unproved'
        elif name == 'locua_inspect':
            label = 'additional_read_without_established_missing_evidence'
            if args.get('snapshot_id') != rubric['snapshot_id']: label = 'wrong_or_unexposed_snapshot'
            elif args.get('operation') == 'control' and args.get('control_id') in rubric['detailed_control_ids']:
                label = 'repeated_already_delivered_detail'
        elif name in ('locua_observe','locua_activate'): label = 'recapture_need_not_established'
        rows.append({'tool':name,'classification':label,'progress':progress,'representation_valid':True})
    return {'calls':rows,'immediate_task_progress':len(rows)==1 and rows[0]['progress'],
        'task_completed':False,'desktop_calls':0,'tool_call_count':len(calls)}


async def freeze(out):
    from locua import execution_state
    import amplifier_module_loop_streaming as loop_module
    out = Path(out); out.mkdir(parents=True, mode=0o700, exist_ok=False)
    suite = {'schema_version':VERSION,'cases':[], 'initial_run_candidates_only':True,
        'model':'qwen38','thinking':False,'generation_limits_unchanged':True,'desktop_calls':0,
        'source':record(__file__), 'projection':record(execution_state.__file__),
        'prior_helper':record(prior.__file__), 'loop_source':record(loop_module.__file__),
        'loop_version':importlib.metadata.version('amplifier-module-loop-streaming'),
        'condition':'Single first-injection intervention at selected retained decision; full header. Not a full historical session replay.'}
    for name, run, call, kind in SPECS:
        folder = out/name; folder.mkdir(mode=0o700)
        owner, provenance = reconstruct(run, call)
        facts = execution_state.render_execution_facts(owner)
        request_path = run/f'provider/call-{call:03d}-input.json'; original = read(request_path)
        candidate = deepcopy(original); reminder = await persisted_message(facts)
        candidate['messages'].append(reminder)
        if candidate['messages'][:-1] != original['messages']: raise ValueError('Original messages changed')
        if kind == 'issued_prefix':
            sid = next(iter(owner._latest.values()))
            rubric = {'kind':kind, 'prefix':prior.build_rubric(original, owner._observations[sid])}
        else: rubric = no_scope_rubric(original, owner)
        response = read(run/f'provider/call-{call:03d}-summary.json')['response']
        validator = ROOT/'src/locua/amplifier_contracts.py'
        (folder/'validator.py').write_bytes(validator.read_bytes())
        (folder/'original-request.json').write_bytes(request_path.read_bytes())
        for filename, value in [('candidate-request.json',candidate),('facts.json',json.loads(facts)),
                ('reconstruction.json',provenance),('rubric-private.json',rubric),('original-response.json',response)]:
            write(folder/filename,value)
        manifest = {'id':name,'kind':kind,'original_source':record(request_path),
            'files':{p.name:record(p) for p in folder.iterdir() if p.is_file()},
            'candidate_request_sha256':sha(candidate),'original_request_sha256':sha(original),
            'original_response_source':record(run/f'provider/call-{call:03d}-summary.json'),
            'candidate_changes_only_appended_reminder':True,'no_expected_answer_added':True,
            'reconstructed_event_count':len(owner.evidence['events']),
            'projection_scope_count':len(owner._scopes),'reminder_bytes':len(reminder['content'].encode())}
        write(folder/'manifest.json',manifest)
        suite['cases'].append({'id':name,'manifest_sha256':sha((folder/'manifest.json').read_bytes())})
    write(out/'suite.json',suite); return suite


def load(directory):
    import importlib.util
    directory = Path(directory); suite = read(directory/'suite.json')
    for key in ('source','projection','prior_helper','loop_source'):
        source = suite[key]
        if sha(Path(source['path']).read_bytes()) != source['sha256']: raise ValueError('Frozen source changed: '+key)
    cases = []
    for row in suite['cases']:
        folder = directory/row['id']; manifest = read(folder/'manifest.json')
        if sha((folder/'manifest.json').read_bytes()) != row['manifest_sha256']: raise ValueError('Manifest changed')
        for name, info in manifest['files'].items():
            if sha((folder/name).read_bytes()) != info['sha256']: raise ValueError('Frozen file changed: '+name)
        spec = importlib.util.spec_from_file_location('frozen_state_validator',folder/'validator.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        cases.append((folder,manifest,module.validate_tool_arguments))
    return suite,cases


async def run(directory, out, config=None):
    from provider_connection import make_provider
    from model_comparison import terminal_provider_failure
    from amplifier_core.message_models import ChatRequest
    suite, cases = load(directory); out = Path(out); out.mkdir(parents=True,mode=0o700,exist_ok=False)
    rows=[]; terminal=None; cancellation=None
    for folder,manifest,validate in cases:
        row={'case_id':manifest['id'],'candidate_request_sha256':manifest['candidate_request_sha256'],'desktop_calls':0}
        if terminal:
            row.update(status='unrun',terminal_failure=terminal);rows.append(row);continue
        provider=None;started=time.monotonic()
        try:
            provider=make_provider('local','qwen38',out/manifest['id']/'provider',config=config,thinking=False,max_calls=1)
            request=read(folder/'candidate-request.json')
            for message in request['messages']:
                if message.get('role')=='tool':
                    provider.register_structured_tool_result(message['tool_call_id'],json.loads(message['content']))
            response=await provider.complete(ChatRequest.model_validate(request))
            row.update(status='returned',response=response.model_dump(),
                evaluation=evaluate(response.model_dump(),read(folder/'rubric-private.json'),validate))
        except BaseException as error:
            row.update(status='failed',error_type=type(error).__name__,error=str(error))
            terminal=terminal_provider_failure(provider,error)
            if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):
                cancellation=error;terminal=terminal or {'basis':'external_cancellation','error_type':type(error).__name__}
        finally:
            if provider is not None:await provider.close()
            row.update(provider_closed=provider is None or provider._closed,wall_seconds=time.monotonic()-started)
        write(out/(manifest['id']+'.json'),row);rows.append(row)
    result={'schema_version':VERSION,'suite':record(Path(directory)/'suite.json'),'results':rows,
        'planned_calls':len(cases),'attempted_calls':sum(r['status']!='unrun' for r in rows),
        'unrun_calls':sum(r['status']=='unrun' for r in rows),'terminal_failure':terminal,
        'desktop_calls':0,'fresh_runtime_per_case':True,'no_task_completion_claim':True}
    write(out/'summary.json',result)
    if cancellation is not None:raise cancellation
    return result


def score_originals(directory,out):
    suite,cases=load(directory);rows=[]
    for folder,manifest,validate in cases:
        rows.append({'case_id':manifest['id'],'evaluation':evaluate(read(folder/'original-response.json'),
            read(folder/'rubric-private.json'),validate)})
    result={'schema_version':VERSION,'suite':record(Path(directory)/'suite.json'),'model_calls':0,
        'desktop_calls':0,'historical_original_controls':rows}
    write(out,result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    command=commands.add_parser('freeze');command.add_argument('--out',type=Path,required=True)
    for name in ('run','score-originals'):
        command=commands.add_parser(name);command.add_argument('--freeze',type=Path,required=True)
        command.add_argument('--out',type=Path,required=True)
        if name=='run':command.add_argument('--config')
    args=parser.parse_args()
    if args.command=='freeze':result=asyncio.run(freeze(args.out))
    elif args.command=='run':result=asyncio.run(run(args.freeze,args.out,args.config))
    else:result=score_originals(args.freeze,args.out)
    print(json.dumps({'status':'saved','output':str(args.out),'schema_version':result['schema_version']}))


if __name__=='__main__':main()
