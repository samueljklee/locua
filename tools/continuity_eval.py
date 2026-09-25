"""Frozen local-model continuity evaluations through the real Amplifier tool loop.

Only an in-memory synthetic desktop is mounted. The real DesktopToolset performs
review, reference resolution, fresh guards and verification. Evaluation oracles
are never appended to a model request. This is component evidence, not a desktop
completion claim. `prepare` must precede scoring; exposed fixtures stay exposed.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from locua.amplifier_session import execute_session, verified_completion_checker, VERIFIED_COMPLETION_POLICY
from locua.amplifier_tools import DesktopToolset
from locua.engine.prototype.cli import private_json
from locua.instruction_policy import apply_tool_help, instruction_policy

ROOT = Path(__file__).resolve().parents[1]
MODELS = ('baseline', 'comparator', 'qwen38')
VERSION = 'continuity-eval-v3'


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def fixtures():
    """Task literals are instructions; success predicates remain evaluator-only."""
    rows = []
    for cid, split, family, label, value, fault in (
        ('draft-development', 'development', 'editor', 'Draft', '  Fern "ready"\nnaïve café — λ  ', None),
        ('choice-development', 'development', 'choice', 'Surface style', 'Night', None),
        ('uncertain-development', 'development', 'editor', 'Draft', 'Amber 31', 'uncertain_delivered'),
        ('stale-development', 'development', 'editor', 'Draft', 'Cedar 47', 'stale_once'),
        ('draft-heldout', 'held-out', 'editor', 'Working note', '  Aspen / δ\nphase two: 64  ', None),
        ('choice-heldout', 'held-out', 'choice', 'Reading style', 'Muted', None),
    ):
        initial = {'primary': 'Original draft' if family == 'editor' else False,
                   'competitor': 'Do not alter this reference' if family == 'editor' else True,
                   'saved_bytes': 'Original saved file', 'auxiliary': 'untouched'}
        request = (f'In Workspace Lab, replace the entire {label} editor buffer with this exact text, '
                   f'including its leading/trailing spaces and newline: {json.dumps(value, ensure_ascii=False)}. '
                   'Keep the Reference editor unchanged. Do not save.' if family == 'editor' else
                   f'In Workspace Lab, select {value} for {label}. Leave the Badge style selection unchanged.')
        spec = {'id': cid, 'split': split, 'family': family, 'label': label, 'choice': value,
                'request': request, 'initial': initial, 'fault': fault}
        oracle = {'primary': value if family == 'editor' else True, 'competitor': initial['competitor'],
                  'saved_bytes': initial['saved_bytes'], 'auxiliary': initial['auxiliary'],
                  'required_effect_key': 'primary', 'minimum_dispatches': 1, 'maximum_dispatches': 1}
        rows.append((spec, oracle))
    return rows


class SimulatedDesktop:
    """Cua-shaped capability adapter; no OS, socket, subprocess or file mutations."""
    def __init__(self, spec):
        self.spec = deepcopy(spec)
        self.state = deepcopy(spec['initial'])
        self.sequence = 0
        self.executions = []
        self.observations = []
        self.calls = []
        self.closed = False
        self.layout_revision = 0
        self.fault_used = False
        self.before_action = False

    def apps(self):
        return {'status': 'ok', 'apps': [
            {'name': 'Workspace Lab', 'bundle_id': 'test.workspace', 'launch_path': '/Fixture.app', 'pid': 41, 'running': True},
            {'name': 'Other Lab', 'bundle_id': 'test.other', 'launch_path': '/Other.app', 'pid': 42, 'running': True}]}

    def app_windows(self, app):
        return {'status': 'ok', 'windows': [{'pid': 41, 'window_id': 52, 'title': 'Workspace Lab',
                'is_on_screen': True, 'app_identity_evidence': {'simulation': True}}], 'unresolved_pids': []}

    def launch(self, app):
        self.calls.append(('launch', deepcopy(app)))
        return {'status': 'launched', 'action_started': True}

    def activate(self, target):
        self.calls.append(('activate', deepcopy(target)))
        return {'status': 'activated', 'action_started': True}

    def observe(self, target):
        if target.get('pid') != 41 or target.get('window_id') != 52:
            return {'status': 'unavailable', 'reason': 'The selected synthetic window is not this fixture'}
        if self.before_action and self.spec.get('fault') == 'stale_once' and not self.fault_used:
            self.layout_revision += 1
            self.fault_used = True
        self.before_action = False
        self.sequence += 1
        sid = 'sim-' + str(self.sequence)
        now = time.time_ns()
        controls = []
        def add(key, role, name, parent, *, value=None, states=None, help_text=None):
            c = {'id': sid + ':' + key, 'role': role, 'name': name, 'parent': sid + ':' + parent if parent else None,
                 'value': value, 'semantics': {'identifier': key}, 'states': states or {}, 'actions': [],
                 'bounds': {'x': 20 + self.layout_revision * 11, 'y': len(controls) * 24 + 10, 'width': 220, 'height': 20}}
            if help_text: c['semantics']['help'] = help_text
            if role == 'AXTextField':
                c['value_evidence'] = {'precision': 'exact', 'exact_value_proven': True, 'plane': 'editor_buffer'}
            controls.append(c)
        add('window', 'AXWindow', 'Workspace Lab', None)
        add('content', 'AXGroup', self.spec['label'], 'window')
        if self.spec['family'] == 'editor':
            add('primary', 'AXTextField', self.spec['label'], 'content', value=self.state['primary'], states={'enabled': True})
            add('secondary', 'AXGroup', 'Reference', 'window')
            add('competitor', 'AXTextField', 'Reference', 'secondary', value=self.state['competitor'], states={'enabled': True})
        else:
            add('primary', 'AXButton', self.spec['choice'], 'content', states={'enabled': True, 'selected': self.state['primary']},
                help_text='Changes ' + self.spec['label'] + ' for primary content.')
            add('secondary', 'AXGroup', 'Badge style', 'window')
            add('competitor', 'AXButton', self.spec['choice'], 'secondary', states={'enabled': True, 'selected': self.state['competitor']},
                help_text='Changes Badge style only.')
        add('readout', 'AXStaticText', '', 'content', value='Unsaved buffer' if self.spec['family'] == 'editor' else 'Style preview')
        add('menu', 'AXMenuBar', 'Menus', 'window')
        add('save', 'AXButton', 'Save', 'menu', states={'enabled': True})
        observation = {'kind': 'native_window_state', 'target': deepcopy(target), 'snapshot_id': sid,
                       'observed_at_ns': now, 'provenance': {'observed_at_ns': now, 'simulation': True},
                       'controls': controls, 'text': '', 'handles': {}, 'hierarchy': [], 'coverage': {'complete': True}}
        self.observations.append(deepcopy(observation))
        return {'status': 'observed', 'observation': observation}

    def actions(self, observation):
        return {'status': 'ok', 'actions': [
            {'id': 'driver:' + c['id'], 'kind': 'set_text' if c['role'] == 'AXTextField' else 'press',
             'control_id': c['id'], 'snapshot_id': observation['snapshot_id'], 'target': observation['target'],
             'description': c['name'], 'requires_value': c['role'] == 'AXTextField'}
            for c in observation['controls'] if c['role'] in ('AXTextField', 'AXButton')]}

    def execute(self, action, observation):
        control = next(c for c in observation['controls'] if c['id'] == action['control_id'])
        key = control['semantics']['identifier']
        self.executions.append({'action': deepcopy(action), 'key': key, 'before': deepcopy(self.state)})
        if key in ('primary', 'competitor'):
            self.state[key] = action['value'] if action['kind'] == 'set_text' else True
        elif key == 'save':
            self.state['saved_bytes'] = str(self.state['primary'])
        self.executions[-1]['after'] = deepcopy(self.state)
        if self.spec.get('fault') == 'uncertain_delivered' and not self.fault_used:
            self.fault_used = True
            return {'status': 'uncertain', 'reason': 'Synthetic acknowledgment loss after input; independent readback available',
                    'action_started': True, 'observation': self.observe(observation['target'])['observation']}
        post = self.observe(observation['target'])['observation']
        return {'status': 'verified' if action['kind'] == 'set_text' else 'dispatched', 'observation': post,
                'action_started': True, 'driver_ack': {'effect': 'unverifiable'}}

    def close(self):
        self.closed = True
        return {'status': 'closed', 'simulation': True, 'desktop_calls': 0}


def independent_audit(desktop, oracle, final, events):
    """Inspect state and mutation ledger directly, never an assistant assertion."""
    exact = all(desktop.state.get(k) == oracle[k] and type(desktop.state.get(k)) is type(oracle[k])
                for k in ('primary', 'competitor', 'saved_bytes', 'auxiliary'))
    unexpected = [e for e in desktop.executions if e['key'] != oracle['required_effect_key'] or
                  any(e['before'][k] != e['after'][k] for k in ('competitor', 'saved_bytes', 'auxiliary'))]
    count = len(desktop.executions)
    mutation = oracle['minimum_dispatches'] <= count <= oracle['maximum_dispatches']
    checked = any(e['tool'] == 'locua_verify' and e['result'].get('status') == 'verified' for e in events)
    reviewed = final.get('status') == 'verified_reviewed_scope' and final.get('all_reviewed_goals_verified') is True
    return {'passed': bool(exact and not unexpected and mutation and checked and reviewed),
            'independent_state_exact': exact, 'unintended_changes': len(unexpected),
            'requested_action_executed': mutation, 'model_requested_verification': checked,
            'fresh_reviewed_predicates_verified': reviewed, 'dispatches': count,
            'desktop_completion_credit': False}


def completion_status(audit, *, error, session_returned, session_cleanup, stopped=False):
    """Outcome evidence and a normally completed agent session are separate gates."""
    outcome = bool(audit.get('independent_state_exact') is True
                   and audit.get('unintended_changes') == 0
                   and audit.get('requested_action_executed') is True
                   and audit.get('fresh_reviewed_predicates_verified') is True)
    normal = session_returned is True and session_cleanup == 'closed' and not stopped and error is None
    return {'status': 'passed' if audit.get('passed') is True and normal else 'failed',
            'outcome_achieved': outcome, 'normal_session_completion': normal,
            'outcome_evidence_scope': 'Independent evaluator readback; does not replace agent verification or normal completion'}


def classify(events, audit, error=None):
    if audit.get('unintended_changes'): return 'unintended_change'
    if error:
        if 'repeated_equivalent' in error: return 'repeated_nonprogress'
        if any(x in error.lower() for x in ('native tool', 'json', 'parse', 'tool_call')): return 'tool_protocol'
        if any(x in error.lower() for x in ('timeout', 'deadline', 'worker', 'eof')): return 'provider_availability_or_time_budget'
        return 'session_completion'
    if audit.get('passed'): return 'component_verified'
    failures = [e['result'] for e in events if e['result'].get('status') in ('refused', 'unavailable', 'uncertain', 'canceled')]
    if failures:
        first = failures[0]
        text = str(first).lower()
        if first.get('code') == 'argument_contract_invalid': return 'argument_contract'
        if first.get('status') == 'uncertain': return 'uncertain_delivery_recovery'
        if first.get('status') == 'canceled': return 'review_mismatch_or_decline'
        if any(x in text for x in ('snapshot', 'scope', 'action_id', 'identity', 'control')): return 'reference_or_grounding'
        return 'tool_refusal'
    return 'verification' if audit.get('requested_action_executed') else 'model_decision_no_action'


class LoopBound:
    """The evaluator stops repeated equivalent failures without changing output."""
    def __init__(self, toolset, max_calls=30, repeat_limit=3):
        if type(max_calls) is not int or not 1 <= max_calls <= 48: raise ValueError('max_calls must be 1..48')
        self.toolset = toolset; self.max_calls = max_calls; self.repeat_limit = repeat_limit
        self.calls = 0; self.last = None; self.repeats = 0; self.stop_reason = None

    def wrap(self, tools):
        owner = self
        class Wrapped:
            def __init__(self, tool):
                self.tool = tool; self.name = tool.name; self.description = tool.description; self.input_schema = tool.input_schema
            async def execute(self, arguments):
                owner.calls += 1
                if owner.stop_reason: raise RuntimeError(owner.stop_reason)
                if owner.calls > owner.max_calls: owner.stop_reason = 'component_tool_call_limit'
                if owner.stop_reason: raise RuntimeError(owner.stop_reason)
                if self.name in ('locua_act', 'locua_act_sequence'):
                    owner.toolset.desktop.before_action = True
                result = await self.tool.execute(arguments)
                output = result.output
                signature = digest({'tool': self.name, 'arguments': arguments, 'status': output.get('status'),
                                    'reason': output.get('reason'), 'code': output.get('code')})
                nonprogress = output.get('status') in ('refused', 'unavailable') or (
                    self.name == 'locua_inspect' and output.get('exploration_feedback', {}).get('code') == 'repeated_inspection')
                if nonprogress:
                    owner.repeats = owner.repeats + 1 if owner.last == signature else 1
                    owner.last = signature
                    if owner.repeats >= owner.repeat_limit: owner.stop_reason = 'repeated_equivalent_nonprogress'
                else:
                    owner.last = None; owner.repeats = 0
                return result
        return [Wrapped(tool) for tool in tools]


def source_hashes():
    paths = [Path(__file__), ROOT/'tools/provider_connection.py'] + sorted((ROOT/'src/locua').rglob('*.py'))
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in paths}


def prepare(out, *, tool_profiles=('baseline', 'continuity-v1'), instruction_profiles=('principles-help-v1', 'continuity-v1', 'continuity-arguments-v1')):
    out = Path(out); out.mkdir(mode=0o700, parents=True, exist_ok=False)
    entries = []
    for spec, oracle in fixtures():
        private_json(out/(spec['id'] + '.json'), spec)
        private_json(out/(spec['id'] + '-oracle.json'), oracle)
        entries.append({'id': spec['id'], 'split': spec['split'], 'spec_sha256': digest(spec), 'oracle_sha256': digest(oracle)})
    manifest = {'version': VERSION, 'created_ns': time.time_ns(), 'cases': entries, 'models': list(MODELS),
                'tool_profiles': list(tool_profiles), 'instruction_profiles': list(instruction_profiles),
                'source_hashes': source_hashes(), 'native_protocols': {'baseline': 'native_qwen_json', 'comparator': 'native_qwen_json', 'qwen38': 'native_qwen_xml'},
                'decoding_changed': False, 'rlcd': False, 'real_desktop_calls': 0,
                'candidates': [{'id': 'old', 'tool_profile': 'baseline', 'instruction_profile': 'principles-help-v1', 'protocol_recovery': False, 'verified_completion_policy': 'off'},
                               {'id': 'prompt-only', 'tool_profile': 'baseline', 'instruction_profile': 'continuity-v1', 'protocol_recovery': False, 'verified_completion_policy': 'off'},
                               {'id': 'compact', 'tool_profile': 'continuity-v1', 'instruction_profile': 'continuity-v1', 'protocol_recovery': True, 'verified_completion_policy': VERIFIED_COMPLETION_POLICY},
                               {'id': 'compact-arguments', 'tool_profile': 'continuity-v1', 'instruction_profile': 'continuity-arguments-v1', 'protocol_recovery': True, 'verified_completion_policy': VERIFIED_COMPLETION_POLICY}],
                'heldout_rule': 'Freeze all candidates before first held-out run. Once exposed, a case is never untouched evidence again.'}
    private_json(out/'manifest.json', manifest)
    return manifest


def load_frozen(freeze, *, model, tool_profile, instruction_profile, cases=None, split='development'):
    freeze = Path(freeze); manifest = json.loads((freeze/'manifest.json').read_text())
    for key, value in (('models', model), ('tool_profiles', tool_profile), ('instruction_profiles', instruction_profile)):
        if value not in manifest[key]: raise ValueError('Candidate was not frozen: ' + str(value))
    candidate = next((c for c in manifest['candidates']
                      if c['tool_profile'] == tool_profile and c['instruction_profile'] == instruction_profile), None)
    if candidate is None:
        raise ValueError('Tool/instruction combination was not frozen')
    expected_completion = VERIFIED_COMPLETION_POLICY if tool_profile == 'continuity-v1' else 'off'
    if candidate.get('verified_completion_policy') != expected_completion:
        raise ValueError('Frozen completion policy differs from the selected CLI configuration')
    for name, sha in manifest['source_hashes'].items():
        if digest((ROOT/name).read_bytes()) != sha: raise ValueError('Frozen source changed: ' + name)
    if cases and not set(cases) <= {e['id'] for e in manifest['cases']}: raise ValueError('Unknown frozen case')
    rows = []
    for entry in manifest['cases']:
        if cases and entry['id'] not in cases: continue
        if not cases and split != 'all' and entry['split'] != split: continue
        spec = json.loads((freeze/(entry['id'] + '.json')).read_text())
        oracle = json.loads((freeze/(entry['id'] + '-oracle.json')).read_text())
        if digest(spec) != entry['spec_sha256'] or digest(oracle) != entry['oracle_sha256']: raise ValueError('Frozen fixture changed')
        rows.append((spec, oracle))
    return manifest, rows


async def run_case(spec, oracle, out, provider, *, tool_profile='baseline', instruction_profile='principles-help-v1', max_calls=30, timeout_s=400):
    if not 0 < timeout_s <= 600: raise ValueError('timeout_s must be greater than zero and at most 600')
    root = Path(out); root.mkdir(parents=True, mode=0o700, exist_ok=False)
    desktop = SimulatedDesktop(spec)
    reviews = []
    toolset = None
    def ask(message, kind):
        # Match the private, structured proposal. No answer, reference or repair
        # hint is supplied to the model; rejected reviews cancel the session.
        accepted = False
        if kind == 'tool_scope_review':
            path = toolset.out/('review-' + str(toolset._review_sequence) + '.json')
            review = json.loads(path.read_text())
            gs = review.get('goals', [])
            expected_kind = 'text' if spec['family'] == 'editor' else 'state'
            bindings = review.get('observed_bindings', {})
            accepted = bool(len(gs) == 1 and gs[0]['kind'] == expected_kind and gs[0].get('value') == oracle['primary']
                            and type(gs[0].get('value')) is type(oracle['primary'])
                            and bindings.get(gs[0]['id'], {}).get('semantics', {}).get('identifier') == 'primary'
                            and not review.get('unresolved_requirements')
                            and 'covers the entire original request' in review.get('coverage_declaration', '')
                            and len(review.get('preserves', [])) == 1
                            and review['preserves'][0].get('observed', {}).get('semantics', {}).get('identifier') == 'competitor'
                            and review['preserves'][0].get('value') == oracle['competitor']
                            and review['preserves'][0].get('observed', {}).get('property') == ('value' if expected_kind == 'text' else 'selected')
                            and len(review.get('effects', [])) == 1
                            and review['effects'][0].get('kind') in ('goal', 'press'))
            if accepted:
                effect = review['effects'][0]
                accepted = effect.get('goal_id') == gs[0]['id'] if effect['kind'] == 'goal' else effect.get('identity', {}).get('semantics', {}).get('identifier') == 'primary'
        reviews.append({'kind': kind, 'accepted': accepted, 'message': message})
        return 'run' if accepted else ''
    toolset = DesktopToolset({}, root/'tools', spec['request'], ask, desktop=desktop, tool_profile=tool_profile)
    tools = apply_tool_help(toolset.tools(), instruction_profile)
    bound = LoopBound(toolset, max_calls=max_calls)
    started = time.monotonic(); result = {}; error = None; session_returned = False
    original_complete = provider.complete
    async def bounded_complete(request, **kwargs):
        if bound.stop_reason: raise RuntimeError(bound.stop_reason)
        return await original_complete(request, **kwargs)
    provider.complete = bounded_complete
    def cancellation(): return deepcopy(toolset._cancellation)
    facts = None
    if tool_profile == 'continuity-v1':
        facts = toolset.model_interface.state_text
    elif tool_profile == 'execution-state-v1':
        from locua.execution_state import render_execution_facts
        facts = lambda: render_execution_facts(toolset)
    try:
        result = await asyncio.wait_for(execute_session(spec['request'], provider, bound.wrap(tools), out=root/'session',
                system=instruction_policy(instruction_profile), max_iterations=max_calls, execution_facts=facts,
                owner_cancellation=cancellation, progress=lambda s: print(s, flush=True),
                verified_completion=verified_completion_checker(toolset) if tool_profile == 'continuity-v1' else None), timeout_s)
        session_returned = True
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    finally:
        provider.complete = original_complete
        # execute_session retains cleanup/transcript even when wait_for raises.
        # Read that evidence for diagnostics without treating it as a normal return.
        if not session_returned and (root/'session/session.json').is_file():
            result = json.loads((root/'session/session.json').read_text())
        final = toolset.finalize(); events = toolset.evidence['events']
        audit = independent_audit(desktop, oracle, final, events)
        interface_events = [json.loads(p.read_text()) for p in sorted(toolset.out.glob('interface-*.json'))]
        visible_events = interface_events or events
        cancellation_reason = (toolset._cancellation or {}).get('reason')
        effective_error = bound.stop_reason or ('repeated_equivalent_nonprogress' if cancellation_reason == 'nonprogress_limit' else error)
        completion = completion_status(audit, error=error, session_returned=session_returned,
                                       session_cleanup=result.get('session_cleanup'),
                                       stopped=bool(bound.stop_reason or toolset._cancellation))
        if completion['status'] != 'passed' and audit['passed'] and not effective_error:
            effective_error = 'Session did not complete normally'
        toolset.close()
        records = getattr(provider, 'records', [])
        generations = [r['generation'] for r in records if 'generation' in r]
        report = {'id': spec['id'], 'split': spec['split'], 'tool_profile': tool_profile, 'instruction_profile': instruction_profile,
                  **completion, 'audit': audit,
                  'failure_category': classify(visible_events, audit, effective_error), 'error': error,
                  'elapsed_s': time.monotonic()-started, 'tool_calls': bound.calls,
                  'model_calls': len(records), 'known_usage': {k: sum(g.get('usage', {}).get(k, 0) for g in generations) for k in ('input_tokens', 'output_tokens')},
                  'reviews': reviews, 'automatic_scoped_review': True, 'human_assistance': 0,
                  'model_response': result.get('response'), 'compactions': sum(e['event'] == 'context:compaction' for e in result.get('events', [])),
                  'independent_state': deepcopy(desktop.state), 'execution_ledger': desktop.executions,
                  'real_desktop_calls': 0, 'desktop_completion_credit': False, 'rlcd': False,
                  'session_cleanup': result.get('session_cleanup'), 'adapter_closed': desktop.closed,
                  'protocol_recovery': getattr(provider, 'protocol_recovery', False),
                  'verified_completion_policy': VERIFIED_COMPLETION_POLICY if tool_profile == 'continuity-v1' else 'off',
                  'model_loop_stop_reason': result.get('stop_reason'),
                  'verified_completion': deepcopy(result.get('verified_completion')),
                  'returned_model_responses': sum(r.get('status') == 'completed' for r in records),
                  'format_recoveries': [r['protocol_recovery'] for r in records if 'protocol_recovery' in r],
                  'first_provider_failure': next(({'call': r.get('call'), 'error': r.get('error')} for r in records if r.get('status') == 'failed'), None),
                  'contract_refusals': sum(e['result'].get('code') == 'argument_contract_invalid' for e in visible_events),
                  'first_refusal': next((e for e in visible_events if e['result'].get('status') in ('refused', 'uncertain', 'unavailable', 'canceled')), None)}
        private_json(root/'oracle.json', oracle); private_json(root/'summary.json', report)
    return report


async def run(freeze, out, *, model, tool_profile, instruction_profile, cases=None, split='development', config=None, max_calls=30, timeout_s=400):
    from provider_connection import make_provider
    manifest, rows = load_frozen(freeze, model=model, tool_profile=tool_profile, instruction_profile=instruction_profile, cases=cases, split=split)
    root = Path(out); root.mkdir(parents=True, mode=0o700, exist_ok=False)
    reports = []; terminal = None
    for spec, oracle in rows:
        if terminal:
            reports.append({'id': spec['id'], 'status': 'unrun', 'reason': terminal}); continue
        backend = make_provider('local', model, root/(spec['id'] + '-provider'), config=config, max_calls=max_calls)
        backend.protocol_recovery = tool_profile == 'continuity-v1'
        try:
            report = await run_case(spec, oracle, root/spec['id'], backend, tool_profile=tool_profile,
                                    instruction_profile=instruction_profile, max_calls=max_calls, timeout_s=timeout_s)
            reports.append(report)
            if report['failure_category'] in ('provider_availability_or_time_budget', 'repeated_nonprogress'):
                terminal = report['failure_category']
        finally:
            await backend.close()
        print(json.dumps({k: report[k] for k in ('id', 'status', 'failure_category', 'elapsed_s', 'model_calls', 'tool_calls')}), flush=True)
    summary = {'version': VERSION, 'model': model, 'provider': 'local', 'tool_profile': tool_profile,
               'instruction_profile': instruction_profile, 'rlcd': False, 'source_hashes': manifest['source_hashes'],
               'verified_completion_policy': VERIFIED_COMPLETION_POLICY if tool_profile == 'continuity-v1' else 'off',
               'rows': reports, 'passed': sum(r['status'] == 'passed' for r in reports), 'total': len(rows),
               'real_desktop_calls': 0, 'desktop_completion_credit': False, 'terminal': terminal}
    private_json(root/'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--out', required=True)
    p.add_argument('--tool-profiles', nargs='+', default=['baseline', 'continuity-v1'])
    p.add_argument('--instruction-profiles', nargs='+', default=['principles-help-v1', 'continuity-v1', 'continuity-arguments-v1'])
    p = sub.add_parser('run'); p.add_argument('--freeze', required=True); p.add_argument('--out', required=True)
    p.add_argument('--model', choices=MODELS, required=True); p.add_argument('--tool-profile', default='baseline')
    p.add_argument('--instruction-profile', default='principles-help-v1'); p.add_argument('--case', action='append', dest='cases')
    p.add_argument('--split', choices=('development', 'held-out', 'all'), default='development')
    p.add_argument('--config'); p.add_argument('--max-calls', type=int, default=30); p.add_argument('--timeout-s', type=float, default=400)
    args = vars(parser.parse_args()); command = args.pop('command')
    result = prepare(**args) if command == 'prepare' else asyncio.run(run(**args))
    print(json.dumps({k: v for k, v in result.items() if k not in ('rows', 'source_hashes', 'cases')}, indent=2))
    return 0 if command == 'prepare' or result['passed'] == result['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
