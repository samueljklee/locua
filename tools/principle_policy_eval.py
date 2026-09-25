"""Local next-decision regression evaluation using the production provider.

Reuses prompt_policy_experiment's exact-message transform and tool registration.
No desktop tools are mounted or executed. Oracles never enter model requests.
These are exposed historical decisions, not a blind generalization benchmark.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time

from prompt_policy_experiment import transform, register_results, tool_outputs
from sequence_identity_replay import private, digest, read, _rubric, score as sequence_score

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('concise-v1', 'principles-v1')
CASES = (
    ('region-discovery', 'development', 'visible-loop-v613-001/settings-dark-comparator', 6),
    ('semantic-target', 'development', 'visible-loop-v613-001/settings-dark', 10),
    ('exact-editor', 'development', 'contract-repair-v10-001/live-fresh-local-1', 9),
    ('arithmetic-identity', 'development', 'action-sequence-v11-001/live-calculator-local-2', 11),
    ('verify-editor', 'reserved-regression', 'contract-repair-v10-001/live-fresh-local-1', 10),
    ('review-editor', 'reserved-regression', 'contract-repair-v10-001/live-fresh-local-1', 6),
)


def rubric_for(cid, run, request, call):
    if cid == 'region-discovery':
        overview = list(tool_outputs(request))[-1][2]['overview']
        region = next(x for x in overview['items'] if x.get('region_kind') == 'navigation')
        return {'snapshot': overview['snapshot_id'], 'region': region['region_id'],
                'cursor': overview['coverage']['continuation'], 'query': 'Appearance'}
    if cid == 'semantic-target':
        details = [o for n, a, o in tool_outputs(request) if n == 'locua_inspect' and a.get('operation') == 'control']
        candidates = [{i['field']: i.get('value') for i in o['items'] if i.get('kind') == 'attribute'} for o in details]
        target = next(c for c in candidates if c['semantics']['help'] == 'Use a dark appearance for buttons, menus, and windows.')
        return {'snapshot': details[0]['snapshot_id'], 'control': target['id'],
                'competitors': [c['id'] for c in candidates if c != target],
                'property': 'selected', 'value': True,
                'initial_state_unknown': 'selected' not in target['states']}
    if cid == 'arithmetic-identity':
        return _rubric(run, request, call)
    # Original request and approved scopes precede these decisions. The known
    # successful recorded output is evaluator-only, not appended to the request.
    response = read(run/'provider'/f'call-{call:03d}-summary.json')['response']
    return {'expected': response['tool_calls'][0],
            'scope': 'scope:8f0521b694724671d7313a0b',
            'literal': '  Juniper "ready"\nnaïve café — β  '}


def evaluate(cid, request, rubric, response):
    from locua.amplifier_contracts import _errors
    calls = response.get('tool_calls') or []
    tools = {t['name']: t['parameters'] for t in request['tools']}
    result = {'passed': False, 'schema_valid': False, 'category': 'no_tool_call',
              'task_completion_credit': False}
    if len(calls) != 1:
        result['category'] = 'requires_one_next_call' if calls else 'no_tool_call'
        return result
    c = calls[0]; n = c['name']; a = c['arguments']
    errors = _errors(tools[n], a) if n in tools else [{'message': 'unknown tool'}]
    if errors:
        return {**result, 'category': 'argument_contract', 'errors': errors}
    result.update(schema_valid=True, category='unproductive_or_ungrounded_decision')
    good = False
    if cid == 'region-discovery' and n == 'locua_inspect':
        same = a.get('snapshot_id') == rubric['snapshot']
        if a.get('operation') == 'list':
            good = same and 'cursor' not in a and a.get('role') in (None, 'AXRow', 'AXStaticText') and (
                a.get('region_id') == rubric['region'] and a.get('query', '').casefold() in ('', rubric['query'].casefold())
                or 'region_id' not in a and a.get('query', '').casefold() == rubric['query'].casefold())
        elif a.get('operation') == 'overview':
            good = same and a.get('cursor') == rubric['cursor']
    elif cid == 'semantic-target' and n == 'locua_review':
        gs = a.get('goals', []); es = a.get('effects', [])
        good = (a['snapshot_id'] == rubric['snapshot'] and len(gs) == 1
                and gs[0].get('kind') == 'state' and gs[0]['control_id'] == rubric['control']
                and gs[0].get('property') == rubric['property'] and gs[0]['value'] is True
                and gs[0]['evidence_plane'] == 'display' and a.get('covers_entire_request') is True
                and not a.get('unresolved_requirements')
                and es == [{'kind': 'press', 'control_id': rubric['control'],
                            'purpose': es[0].get('purpose')}] and not a.get('preserves'))
        # Unknown initial selected state needs a separately reviewed observed
        # press. An unsupported goal-toggle or fabricated preserve is not valid.
    elif cid == 'arithmetic-identity':
        if n == 'locua_act_sequence':
            graded = sequence_score(request, rubric, response, candidate=False)
            result['sequence_analysis'] = graded
            good = any(r.get('semantic_plan_matched') for r in graded.get('calls', []))
            # The historical evaluator names its per-call list 'results'.
            good = good or any(r.get('semantic_plan_matched') for r in graded.get('results', []))
        elif n == 'locua_act':
            action = rubric['actions'].get(a['action_id'])
            good = bool(action and action.get('arithmetic_token') == 'clear'
                        and a['scope_id'] in rubric['scopes'] and a['snapshot_id'] == action['snapshot_id']
                        and 'value' not in a)
            if good: result['progress_only'] = 'observed full reset; expression still unexecuted'
    elif cid == 'exact-editor':
        good = n == rubric['expected']['name'] and a == rubric['expected']['arguments']
        if n == 'locua_act_sequence':
            expected = rubric['expected']['arguments']
            good = (a['scope_id'] == expected['scope_id'] and a['snapshot_id'] == expected['snapshot_id']
                    and a['steps'] == [{'action_id': expected['action_id'], 'value': expected['value']}])
    elif cid == 'verify-editor':
        good = n == 'locua_verify' and (a == {'all': True} or
                a.get('scope_id') == rubric['scope'] and set(a) <= {'scope_id', 'all'} and not a.get('all'))
    elif cid == 'review-editor' and n == 'locua_review':
        expected = rubric['expected']['arguments']; gs = a['goals']
        good = (a['snapshot_id'] == expected['snapshot_id'] and len(gs) == 1
                and all(gs[0].get(k) == expected['goals'][0][k]
                        for k in ('kind', 'control_id', 'value', 'evidence_plane'))
                and a['effects'] == [{'kind': 'goal', 'goal_id': gs[0]['id']}]
                and a.get('covers_entire_request') is True and not a.get('unresolved_requirements')
                and not a.get('preserves'))
    result.update(passed=bool(good), category='grounded_next_step' if good else result['category'])
    return result


def prepare(out, profiles=PROFILES):
    out = Path(out); out.mkdir(parents=True, exist_ok=False, mode=0o700)
    entries = []
    for cid, split, source, call in CASES:
        run = ROOT/'artifacts'/source; path = run/'provider'/f'call-{call:03d}-input.json'
        original = read(path); rubric = rubric_for(cid, run, original, call)
        private(out/(cid+'-oracle.json'), rubric)
        for profile in profiles:
            request, meta = transform(original, profile, allow_historical_inventory=True)
            name = cid+'-'+profile+'.json'; private(out/name, request)
            entries.append({'id': cid, 'split': split, 'profile': profile, 'file': name,
                'sha256': digest(request), 'oracle_file': cid+'-oracle.json', 'oracle_sha256': digest(rubric),
                'source': str(path), 'source_sha256': digest(path.read_bytes()), 'policy': meta,
                'non_system_sha256': digest(request['messages'][1:]), 'tools_sha256': digest(request['tools'])})
    # Freeze policy/scorer/provider source before any inference, not just inputs.
    sources = [Path(__file__), ROOT/'tools/prompt_policy_experiment.py', ROOT/'tools/provider_connection.py']
    sources += list((ROOT/'src/locua').rglob('*.py'))
    manifest = {'version': 'principle-policy-eval-v1', 'created_ns': time.time_ns(), 'cases': entries,
        'profiles': list(profiles), 'maximum_calls_per_model': len(entries),
        'models': ['comparator', 'qwen38', 'baseline'], 'inference': 'local only, existing greedy ordinary tool calling; no RLCD',
        'promotion_gate': 'candidate 6/6 grounded next decisions, no unsafe or contradictory action; then installed CLI, never component-only promotion',
        'scope': 'exposed retained regressions; reserved cases evaluated after development, no tuning to results',
        'source_hashes': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sources},
        'changes': ['explicit instruction profiles; system and declared tool descriptions only'], 'schema_observations_history_decoding_unchanged': True,
        'task_completion_credit': False, 'desktop_tools_mounted': False}
    private(out/'manifest.json', manifest)
    return {'cases': len(CASES), 'profiles': list(profiles), 'freeze': str(out)}


async def run(freeze, out, model, cases=None):
    from amplifier_core.message_models import ChatRequest
    from provider_connection import make_provider
    freeze = Path(freeze); out = Path(out); manifest = read(freeze/'manifest.json')
    if model not in manifest['models']: raise ValueError('Model not frozen')
    if cases and not set(cases) <= {e['id'] for e in manifest['cases']}:
        raise ValueError('Unknown frozen case')
    for name, sha in manifest['source_hashes'].items():
        if digest((ROOT/name).read_bytes()) != sha: raise ValueError('Frozen source changed: '+name)
    prepared = []
    for e in manifest['cases']:
        if cases and e['id'] not in cases: continue
        request = read(freeze/e['file']); oracle = read(freeze/e['oracle_file'])
        if digest(request) != e['sha256'] or digest(oracle) != e['oracle_sha256']:
            raise ValueError('Frozen input or oracle changed')
        prepared.append((e, request, oracle))
    out.mkdir(parents=True, exist_ok=False, mode=0o700); rows = []; terminal = None
    provider = make_provider('local', model, out/'provider', max_calls=len(prepared))
    start = time.monotonic()
    try:
        for e, request, oracle in prepared:
            row = {'id': e['id'], 'profile': e['profile'], 'request_sha256': e['sha256']}
            if terminal:
                row.update(status='unrun', reason=terminal)
            else:
                print(f"{model} / {e['id']} / {e['profile']}", flush=True); tick = time.monotonic()
                try:
                    # Registry entries are keyed by immutable tool-call IDs and
                    # only used when the supplied message matches exactly. Keep
                    # registrations to preserve write-once audit filenames;
                    # native_request includes only this request's messages.
                    register_results(provider, request)
                    response = await asyncio.wait_for(provider.complete(ChatRequest.model_validate(request)), 180)
                    row.update(status='returned', response=response.model_dump(),
                               evaluation=evaluate(e['id'], request, oracle, response.model_dump()))
                except Exception as error:
                    terminal = type(error).__name__+': '+str(error)
                    row.update(status='failed', error=terminal)
                row['wall_s'] = time.monotonic()-tick
                print(json.dumps({k:v for k,v in row.items() if k not in ('response', 'request_sha256')}), flush=True)
            private(out/(e['id']+'-'+e['profile']+'.json'), row); rows.append(row)
    finally:
        await provider.close()
        generations = [r['generation'] for r in provider.records if 'generation' in r]
        summary = {'model': model, 'provider': 'local', 'rlcd': False, 'decoding_changed': False,
            'wall_s': time.monotonic()-start, 'rows': rows, 'desktop_calls': 0, 'human_assistance': 0,
            'completed_calls': sum(r.get('status') == 'returned' for r in rows),
            'known_usage': {k: sum(g.get('usage', {}).get(k, 0) for g in generations) for k in ('input_tokens','output_tokens')},
            'provider_closed': provider._closed, 'terminal': terminal, 'task_completion_credit': False}
        private(out/'summary.json', summary)
    return {k:v for k,v in summary.items() if k != 'rows'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command', required=True)
    q = sub.add_parser('prepare'); q.add_argument('--out', required=True)
    q.add_argument('--profiles', nargs='+', default=PROFILES)
    q = sub.add_parser('run'); q.add_argument('--freeze', required=True); q.add_argument('--out', required=True)
    q.add_argument('--model', choices=('comparator','qwen38','baseline'), required=True)
    q.add_argument('--case', action='append', dest='cases')
    a = p.parse_args()
    result = prepare(a.out,a.profiles) if a.command == 'prepare' else asyncio.run(run(a.freeze, a.out, a.model,a.cases))
    print(json.dumps(result, indent=2))
