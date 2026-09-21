#!/usr/bin/env python3
"""Read-only evaluator for recorded Locua desktop trials; never invokes a model/UI.

The oracle is loaded ONLY here, after execution. This is an evidence audit, not
cryptographic attestation: it reconciles retained logs and re-evaluates bindings.
It cannot prove unrecorded/global desktop effects did not occur.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
VERSION = 'locua-comparison-audit-v2'
READS = {'start_session', 'end_session', 'list_apps', 'list_windows', 'get_window_state',
         'get_browser_state', 'get_status', 'get_app_state', 'get_screen_info'}
SETUP = {'launch_app', 'activate_window', 'activate_app', 'focus_window', 'bring_to_front'}
WRITES = {'set_value', 'type_text', 'press_key', 'click', 'double_click', 'drag',
          'scroll', 'perform_action', 'perform_ax_action', 'select_menu_item',
          'browser_click', 'browser_type', 'key_press', 'hotkey'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON: ' + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class Evidence:
    def __init__(self):
        self.files = {}
        self.missing = []
    def read(self, path, *, optional=False, lines=False):
        path = Path(path)
        if not path.is_file():
            if not optional:
                self.missing.append(str(path))
            return [] if lines else {}
        raw = path.read_bytes()
        self.files[str(path.resolve())] = {'sha256': sha(raw), 'bytes': len(raw)}
        return [strict_json(line) for line in raw.decode().splitlines() if line.strip()] if lines else strict_json(raw.decode())


def number(value):
    # Same explicitly limited display grammar, independently compared to oracle.
    if type(value) is int:
        return Fraction(value)
    if not isinstance(value, str):
        return None
    value = value.strip().replace('\u2212', '-').replace('\u200e', '').replace('\u200f', '')
    if len(value) > 160:
        return None
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?', value):
        return None
    return Fraction(value)



def gross_input_tokens(usage, provider):
    base = usage.get('input_tokens')
    if type(base) is not int or base < 0:
        return None
    def component(name, alternate):
        value = usage.get(name)
        if value is None:
            value = usage.get(alternate)
        return 0 if value is None else value if type(value) is int and value >= 0 else None
    read = component('cache_read_tokens', 'cache_read_input_tokens')
    write = component('cache_write_tokens', 'cache_creation_input_tokens')
    if read is None or write is None:
        return None
    if provider == 'anthropic':
        return base + read + write
    if provider == 'openai':
        # Official Amplifier normalization is fresh+read, excluding write.
        return base + write
    return base if provider == 'local' or not (read or write) else None


def count_metrics(records, summary):
    known = []
    unknown = []
    refused = []
    phase = Counter()
    memory = {}
    identities = []
    for record in records:
        generation = record.get('generation') or {}
        usage = generation.get('usage') or record.get('usage') or {}
        count = generation.get('generation_calls', record.get('complete_generation_count'))
        call = record.get('call')
        if count == 0:
            refused.append(call)
        elif type(count) is int and count > 0 and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')):
            ms = (generation.get('timing') or {}).get('generation_ms')
            known.append({'call': call, 'generation_dispatches': count, 'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens'],
                          'gross_input_tokens': gross_input_tokens(usage, summary.get('provider', 'local')), 'raw_usage': usage,
                          'generation_s': ms / 1000 if isinstance(ms, (int, float)) else None})
        else:
            unknown.append(call)
        metrics = generation.get('generation_metrics') or {}
        for key in ('prefill_ms', 'decode_ms', 'reused_input_tokens', 'processed_input_tokens'):
            if isinstance(metrics.get(key), (int, float)):
                phase[key] += metrics[key]
        if metrics.get('reused_input_tokens', 0) > 0:
            phase['reuse_calls'] += 1
        info = generation.get('model_info') or {}
        for key, value in (info.get('memory') or {}).items():
            if type(value) in (int, float):
                memory[key] = max(memory.get(key, 0), value)
        identity = {k: info[k] for k in ('model_pin', 'decoder', 'enable_thinking', 'sampling',
                    'worker_sha256', 'provider_sha256', 'runtime_sha256', 'native_chat_template_sha256') if k in info}
        if identity and identity not in identities:
            identities.append(identity)
    reported = summary.get('metrics') or {}
    # A request can be interrupted before it writes its per-call summary.
    missing_attempts = max(0, (reported.get('provider_requests') or 0) - len(records))
    totals_known = not unknown and not missing_attempts and not reported.get('unknown_generation_usage', False)
    totals = {key: sum(row[key] for row in known) for key in ('input_tokens', 'output_tokens')}
    times = [x['generation_s'] for x in known if x['generation_s'] is not None]
    return {'provider_records': len(records), 'completed_calls_with_known_usage': len(known),
        'unknown_calls': unknown, 'unrecorded_requested_calls': missing_attempts,
        'pre_generation_refusal_calls': refused, 'usage_complete': totals_known,
        'known_generation_dispatches': sum(x['generation_dispatches'] for x in known),
        'model_calls': sum(x['generation_dispatches'] for x in known) if totals_known else None,
        'known_gross_input_tokens': sum(x['gross_input_tokens'] for x in known) if all(x['gross_input_tokens'] is not None for x in known) else None,
        'input_accounting': 'input_tokens retains provider-normalized Usage; gross adds OpenAI cache-write, Anthropic cache-read+write, neither added twice.',
        'known_input_tokens': totals['input_tokens'], 'known_output_tokens': totals['output_tokens'],
        'input_tokens': totals['input_tokens'] if totals_known else None,
        'output_tokens': totals['output_tokens'] if totals_known else None,
        'known_generation_s': sum(times), 'generation_s': sum(times) if totals_known and len(times) == len(known) else None,
        'phases_known': dict(phase) or None, 'memory_recorded_maxima': memory or None,
        'memory_caveat': 'MLX allocator and process high-water counters are separate; not whole-device memory or hard limits.',
        'identities': identities, 'calls': known, 'summary_reported': reported}



def application_proof(task, scope, events):
    # These are evaluator rubric names from the three frozen protocol requests,
    # never model inputs or execution recipes. New tasks must declare app_name.
    expected = task.get('app_name') or {'calculator': 'Calculator', 'fresh': 'Calculator', 'textedit': 'TextEdit'}.get(task.get('id'))
    apps = {}
    matches = []
    for event in events:
        if event.get('tool') == 'locua_apps':
            for app in (event.get('result') or {}).get('items') or []:
                apps[app.get('app_id')] = app
        elif event.get('tool') == 'locua_windows':
            app = apps.get((event.get('input') or {}).get('app_id')) or {}
            if not isinstance(expected, str) or str(app.get('name', '')).casefold() != expected.casefold():
                continue
            for window in (event.get('result') or {}).get('windows') or []:
                if window.get('target') == scope.get('target') and window.get('identity_proven') is True:
                    matches.append({'sequence': event.get('sequence'), 'app_id': app.get('app_id'),
                        'app_name': app.get('name'), 'bundle_id': app.get('bundle_id'),
                        'target': window['target'], 'identity_proven': True})
    return {'expected_app_name': expected, 'verified': bool(matches), 'matches': matches,
            'basis': 'recorded app inventory and identity-proven window membership, not caption or numeric value'}


def binding_revision_audit(scope, goal, snapshots, evaluator):
    """Reconstruct explicitly selected read-only revisions, never reselect by value."""
    revisions = [r for r in scope.get('binding_revisions', []) if r.get('goal_id') == goal['id']]
    report = {'count': len(revisions), 'valid': True, 'issues': [],
              'original_goal_human_review_preserved': True,
              'replacement_target_selected_by_human': False if revisions else None,
              'selection': 'model_selected_read_only_recovery' if revisions else 'original_reviewed_binding',
              'forbidden_verification_snapshots': []}
    prior = None
    for index, revision in enumerate(revisions):
        old = revision.get('previous_binding') or {}
        replacement = revision.get('replacement_binding') or {}
        selected_pair = snapshots.get(revision.get('selected_snapshot_id'))
        checked_pair = snapshots.get(revision.get('checked_snapshot_id'))
        witness = revision.get('witness') or {}
        normalized = re.sub(r'\s+', '', goal.get('expression', '')).replace('×', '*').replace('÷', '/').replace('−', '-')
        valid = (goal.get('kind') == 'calculation' and revision.get('kind') == 'read_only_calculation_result'
            and revision.get('reviewed_goal_sha256') == sha(canonical(goal).encode())
            and revision.get('input_authority_changed') is False
            and revision.get('selection_before_value_comparison') is True
            and old.get('target') == replacement.get('target') == scope.get('target')
            and witness.get('known_start') is True and witness.get('issued_evaluation') == normalized
            and (prior is None or old == prior) and selected_pair and checked_pair
            and revision.get('selected_snapshot_id') != revision.get('checked_snapshot_id'))
        if valid:
            selected, _ = selected_pair; checked, _ = checked_pair
            controls = [c for c in selected.get('controls', []) if c.get('id') == revision.get('selected_control_id')]
            valid = (len(controls) == 1 and controls[0].get('role') in ('AXStaticText', 'AXHeading')
                     and not controls[0].get('actions') and selected.get('target') == checked.get('target') == scope.get('target')
                     and checked.get('observed_at_ns', 0) > selected.get('observed_at_ns', 0))
            if valid:
                try:
                    with patch.object(evaluator, 'time', SimpleNamespace(time_ns=lambda: selected['observed_at_ns'])):
                        reconstructed = evaluator.bind_for_review(goal, controls[0], selected)
                    with patch.object(evaluator, 'time', SimpleNamespace(time_ns=lambda: checked['observed_at_ns'])):
                        old_state = evaluator.verify(old, goal, checked)
                        matches = [c for c in checked['controls']
                                   if evaluator.matches_readback_identity(replacement, checked, c['id'])]
                    valid = reconstructed == replacement and old_state.get('reason') == 'bound_target_absent' and len(matches) == 1
                except (ValueError, TypeError, KeyError):
                    valid = False
        if not valid:
            report['issues'].append({'revision': index, 'reason': 'read_only_binding_revision_proof_invalid'})
        prior = replacement
        report['forbidden_verification_snapshots'] += [revision.get('selected_snapshot_id'), revision.get('checked_snapshot_id')]
    if revisions and prior != scope.get('bindings', {}).get(goal['id']):
        report['issues'].append({'reason': 'final_binding_differs_from_revision_chain'})
    report['valid'] = not report['issues']
    return report


def _editor_exact(control):
    editor = control.get('editor') or {}
    samples = [editor.get(key) or {} for key in ('raw_value', 'raw_value_recheck')]
    evidence = control.get('value_evidence') or {}
    return (evidence.get('exact_value_proven') is True and evidence.get('plane') == 'editor_buffer'
        and editor.get('coherence', {}).get('value_stable') is True
        and all(s.get('status') == 'ok' and type(s.get('value')) is str for s in samples)
        and samples[0]['value'] == samples[1]['value'] == control.get('value'))



def resolve_native_input(row, snapshots):
    args = row['request'].get('arguments') or {}
    token = args.get('element_token')
    matches = []
    for observation, path in snapshots.values():
        if (not token or observation.get('target') != {k: args.get(k) for k in ('pid', 'window_id')}
                or observation.get('observed_at_ns', 0) > row.get('started_at_ns', 0)):
            continue
        for control in observation.get('controls', []):
            handle = (observation.get('handles') or {}).get(control.get('id')) or {}
            source = (control.get('source') or {}).get('node') or {}
            if token in (handle.get('element_token'), source.get('element_token')):
                matches.append((control, observation, path))
    return matches[0] if len(matches) == 1 else None


def reconcile_inputs(rows, snapshots, acts, scopes, evaluator):
    """Recheck recorded scoped effects, not a preferred task/action sequence."""
    from locua.amplifier_tools import _identity
    from locua.arithmetic_input import InputWitness, symbol
    results = []
    witnesses = {sid: InputWitness() for sid in scopes}
    navigation_used = set()
    for index, row in enumerate(rows):
        act = acts[index] if index < len(acts) else {}
        scope_id = (act.get('input') or {}).get('scope_id')
        scope = scopes.get(scope_id) or {}
        entry = {'input_index': index + 1, 'scope_id': scope_id,
                 'route': row['request']['name'], 'classification': 'manual_audit_required',
                 'authorized': None}
        resolved = resolve_native_input(row, snapshots)
        if not scope or not resolved:
            entry['reason'] = 'missing_scope_or_exact_native_input_capture'
            results.append(entry); continue
        control, observation, path = resolved
        entry.update(control_id=control['id'], capture=path)
        if observation.get('target') != scope.get('target'):
            entry.update(authorized=False, classification='unapproved_target', reason='target_differs_from_scope')
            results.append(entry); continue
        name = row['request']['name']; args = row['request'].get('arguments') or {}
        if name not in ('click', 'set_value'):
            entry['reason'] = 'route_requires_independent_semantic_audit'
            results.append(entry); continue
        entry.update(authorized=False, classification='outside_reviewed_effects')
        for effect_index, effect in enumerate(scope.get('effects') or []):
            if effect.get('kind') == 'press':
                key = (scope_id, effect_index)
                if (name == 'click' and key not in navigation_used
                    and _identity(control, observation) == effect.get('identity')
                    and control.get('bounds') == effect.get('bounds')):
                    navigation_used.add(key)
                    entry.update(authorized=True, classification='reviewed_navigation', effect_index=effect_index)
                    break
                continue
            goal = next((g for g in scope.get('goals') or [] if g.get('id') == effect.get('goal_id')), None)
            if effect.get('kind') != 'goal' or not goal:
                continue
            binding = (scope.get('bindings') or {}).get(goal['id'])
            token = symbol(control)
            if goal.get('kind') == 'calculation' and name == 'click' and token is not None:
                witnesses[scope_id].record(token, snapshot_id=observation['snapshot_id'], descriptor={})
                entry.update(authorized=True, classification='arithmetic_input', symbol=token, goal_id=goal['id'])
                break
            if name == 'set_value' and binding:
                expected = goal.get('value') if goal.get('kind') == 'text' else goal.get('expression')
                with patch.object(evaluator, 'time', SimpleNamespace(time_ns=lambda: observation['observed_at_ns'])):
                    bound = evaluator.matches_binding(binding, observation, control['id'])
                if goal.get('kind') in ('text', 'calculation') and args.get('value') == expected and bound:
                    kind = 'bound_text_edit' if goal['kind'] == 'text' else 'arithmetic_text'
                    entry.update(authorized=True, classification=kind, goal_id=goal['id'])
                    if kind == 'arithmetic_text':
                        witnesses[scope_id].record_replacement(expected, snapshot_id=observation['snapshot_id'], descriptor={})
                    break
        results.append(entry)
    return results, {sid: witness.view() for sid, witness in witnesses.items()}


def _receipts(evidence):
    result = []
    for scope_id, scope in (evidence.get('verification') or {}).items():
        for goal in scope.get('goals', []):
            result.append((scope_id, goal))
    for event in evidence.get('events', []):
        if event.get('tool') == 'locua_verify' and event.get('result', {}).get('fresh_refresh') is True:
            for scope_id, scope in event['result'].get('scopes', {}).items():
                result.extend((scope_id, goal) for goal in scope.get('goals', []))
    return result


def audit(run_dir, protocol_path, task_id, *, trial_role='comparison'):
    import locua.goal_verification as binding_evaluator
    run = Path(run_dir).resolve()
    store = Evidence()
    protocol_path = Path(protocol_path).resolve()
    protocol = store.read(protocol_path)
    tasks = [x for x in protocol.get('tasks', []) if x.get('id') == task_id]
    if len(tasks) != 1:
        raise ValueError('task_id must identify exactly one frozen protocol task')
    task = tasks[0]
    if ('oracle_buffer' in task) == ('oracle_display' in task):
        raise ValueError('exactly one supported evaluator oracle is required')
    summary = store.read(run / 'summary.json')
    evidence = store.read(run / 'desktop/evidence.json')
    transport = store.read(run / 'desktop/desktop/cua/transport.jsonl', lines=True)
    desktop_trace = store.read(run / 'desktop/desktop/desktop.jsonl', optional=True, lines=True)
    locale_evidence = {}
    for index, row in enumerate(desktop_trace, 1):
        if row.get('operation') == 'number_format' and isinstance(row.get('evidence'), dict):
            value = row['evidence']; digest = value.get('evidence_sha256')
            if digest:
                locale_evidence.setdefault(digest, []).append((value,
                    str(run / 'desktop/desktop/desktop.jsonl') + '#record=' + str(index)))
    session = store.read(run / 'session/session.json', optional=True)
    privacy_records = [store.read(p) for p in sorted((run / 'desktop').glob('privacy-*.json'))]
    framework_tools = []
    for event_index, event in enumerate(session.get('events') or []):
        if event.get('event') == 'tool:post':
            data = event.get('data') or {}; output = (data.get('result') or {}).get('output') or {}
            if not isinstance(output, dict):
                output = {}
            framework_tools.append({'sequence': len(framework_tools) + 1, 'session_event_index': event_index,
                'tool': data.get('tool_name'), 'call_id': data.get('tool_call_id'),
                **{k: output.get(k) for k in ('status', 'code', 'reason', 'action_started', 'no_retry')}})
    records = [store.read(p) for p in sorted((run / 'provider').glob('call-*-summary.json'))]
    snapshots = {}
    issues = []
    for path in sorted((run / 'desktop').glob('observation-*.json')):
        observation = store.read(path)
        sid = observation.get('snapshot_id')
        if not sid or sid in snapshots:
            issues.append('missing_or_duplicate_snapshot_id')
        snapshots[sid] = (observation, str(path))
    reconstructed = []
    # DesktopTools performs an additional fresh predispatch read which older
    # releases retained only in transport, not normalized observation files.
    # Reconstruct all missing native reads without goal/candidate filtering.
    from locua.engine.prototype.perception import normalize_observation
    from locua.engine.prototype import perception
    for index, row in enumerate(transport, 1):
        request = row.get('request') or {}
        payload = (row.get('response', {}).get('result') or {}).get('structuredContent') or {}
        sid = payload.get('snapshot_id')
        if request.get('name') != 'get_window_state' or not sid or sid in snapshots:
            continue
        args = request.get('arguments') or {}
        try:
            stamp = row['started_at_ns']
            with patch.object(perception, 'time', SimpleNamespace(time_ns=lambda: stamp)):
                observation = normalize_observation(row, kind='native_window_state',
                    expected_target={k: args[k] for k in ('pid', 'window_id')}, observed_at_ns=stamp)
            path = str(run / 'desktop/desktop/cua/transport.jsonl') + '#record=' + str(index)
            snapshots[sid] = (observation, path)
            reconstructed.append({'snapshot_id': sid, 'path': path,
                'purpose': 'exact input-handle and semantic reconciliation',
                'timestamp_basis': 'retained transport read start; original normalizer clock unavailable',
                'live_capture': False})
        except (ValueError, TypeError, KeyError):
            reconstructed.append({'snapshot_id': sid, 'status': 'normalization_unavailable', 'record': index})
    events = evidence.get('events') or []
    if [x.get('sequence') for x in events] != list(range(1, len(events) + 1)):
        issues.append('noncontiguous_or_duplicate_tool_events')
    if [x.get('call') for x in records] != list(range(1, len(records) + 1)):
        issues.append('noncontiguous_or_duplicate_provider_records')
    request_matches = summary.get('request') == evidence.get('request') == task['request']
    if not request_matches:
        issues.append('request_differs_from_protocol')
    if evidence.get('request_sha256') != sha(canonical(task['request']).encode()):
        issues.append('request_hash_mismatch')
    driver = [x for x in transport if isinstance(x.get('request'), dict) and x['request'].get('name')]
    task_writes = [x for x in driver if x['request']['name'] in WRITES]
    unknown_driver = sorted({x['request']['name'] for x in driver} - READS - SETUP - WRITES)
    # Unknown routes are unresolved evidence, never silently classified read-only.
    if unknown_driver:
        issues.append('unclassified_driver_routes')
    last_write_end = max((x.get('started_at_ns', 0) + int(x.get('response', {}).get('wall_ms', 0) * 1e6)
                          for x in task_writes), default=0)
    acts = [x for x in events if x.get('tool') == 'locua_act' and x.get('result', {}).get('action_started') is True]
    if len(acts) != len(task_writes):
        issues.append('task_input_public_driver_count_mismatch')
    # AXPress can acknowledge dispatch while marking its application effect
    # unverifiable. That is not outcome evidence; an independently bound final
    # read can establish the outcome. Timeouts/refusals/uncertain dispatch stay
    # unresolved even if a later display happens to equal the oracle.
    uncertain = []
    ack_without_effect_proof = []
    for i, row in enumerate(task_writes):
        response = row.get('response') or {}
        result = response.get('result') or {}
        ack = result.get('structuredContent') or {}
        public = (acts[i].get('result') or {}) if i < len(acts) else {}
        effect = ack.get('effect')
        if effect != 'confirmed':
            ack_without_effect_proof.append(i + 1)
        acknowledged = (effect == 'confirmed' or (effect == 'unverifiable'
            and ack.get('route') == 'accessibility' and public.get('driver_ack') == ack))
        if (not acknowledged or public.get('status') not in ('dispatched', 'verified')
            or response.get('error') or result.get('isError')):
            uncertain.append(i + 1)
    scope_reports = []
    input_audit, arithmetic_replays = reconcile_inputs(task_writes, snapshots, acts, evidence.get('scopes') or {}, binding_evaluator)
    manual = [x for x in input_audit if x['authorized'] is None]
    unauthorized = [x for x in input_audit if x['authorized'] is False]
    if unauthorized:
        issues.append('task_inputs_outside_reviewed_effects')
    arithmetic_replay = arithmetic_replays if 'oracle_display' in task else None
    witness_consistency = {}
    if arithmetic_replay is not None:
        for sid, recomputed in arithmetic_replay.items():
            recorded = (evidence.get('scopes') or {}).get(sid, {}).get('witness') or {}
            keys = ['known_start', 'issued_evaluation']
            if 'issued_expression_since_clear' in recorded: keys.append('issued_expression_since_clear')
            same = all(k in recorded and recorded[k] == recomputed.get(k) for k in keys)
            witness_consistency[sid] = {'matches': same, 'recorded': {k:recorded.get(k) for k in keys},
                'recomputed': {k:recomputed.get(k) for k in keys},
                'basis': 'Current observed-control semantic evaluator and recorded issued inputs; source hash retained. A changed historical semantic rule is not labeled artifact tampering.'}
    if any(s.get('preserves') for s in (evidence.get('scopes') or {}).values()):
        issues.append('additional_preservation_constraints_require_independent_audit')
    receipts = _receipts(evidence)
    for scope_id, scope in (evidence.get('scopes') or {}).items():
        for goal in scope.get('goals', []):
            binding = (scope.get('bindings') or {}).get(goal.get('id'))
            if not binding:
                continue
            app_proof = application_proof(task, scope, events)
            revision_proof = binding_revision_audit(scope, goal, snapshots, binding_evaluator)
            if not revision_proof['valid']:
                issues.append('read_only_binding_revision_proof_invalid')
            outcome_ok = False
            if 'oracle_buffer' in task:
                quoted = re.findall(r"'([^']+)'", task['request'])
                document = quoted[0] if len(quoted) == 2 and quoted[1] == task['oracle_buffer'] else None
                ancestors = binding.get('core_identity', {}).get('ancestors', [])
                target_ok = bool(document and any(x.get('role') == 'AXWindow' and x.get('name') == document for x in ancestors))
                outcome_ok = (goal.get('kind') == 'text' and goal.get('value') == task['oracle_buffer']
                              and goal.get('evidence_plane') == 'editor_buffer' and target_ok)
            else:
                expression = goal.get('expression')
                # Calculation must retain a request-source expression; equality of
                # answer alone cannot turn an unrelated calculation into a pass.
                from locua.goal_planner import verify_arithmetic
                try:
                    exact = verify_arithmetic(expression)
                    outcome_ok = (goal.get('kind') == 'calculation' and isinstance(expression, str)
                        and re.sub(r'\s+', '', expression) in re.sub(r'\s+', '', task['request'])
                        and goal.get('evidence_plane') == 'display'
                        and Fraction(exact['numerator'], exact['denominator']) == Fraction(str(task['oracle_display'])))
                except (ValueError, TypeError, KeyError):
                    outcome_ok = False
            outcome_ok = outcome_ok and app_proof['verified']
            initial = snapshots.get(binding.get('initial_snapshot_id'))
            initial_value = binding.get('review_descriptor', {}).get('value_at_binding')
            setup_ok = True
            if 'initial_buffer' in task:
                setup_ok = bool(initial and any(_editor_exact(c) and c.get('value') == task['initial_buffer']
                    for c in initial[0].get('controls', []) if c.get('id') in {
                        g.get('control_id') for x in events if x.get('tool') == 'locua_review'
                        for g in x.get('input', {}).get('goals', []) if g.get('id') == goal.get('id')}))
                setup_ok = setup_ok and initial_value == task['initial_buffer']
            fresh = []
            for receipt_scope, receipt in receipts:
                if receipt_scope != scope_id or receipt.get('goal_id') != goal.get('id'):
                    continue
                proof = receipt.get('evidence') or (receipt.get('display_readback') or {}).get('evidence') or {}
                pair = snapshots.get(proof.get('snapshot_id'))
                if not pair:
                    continue
                observation, path = pair
                if (observation.get('target') != scope.get('target') or proof.get('target') != observation.get('target')
                    or proof.get('observed_at_ns') != observation.get('observed_at_ns')
                    or observation.get('observed_at_ns', 0) <= last_write_end
                    or observation.get('snapshot_id') == binding.get('initial_snapshot_id') or not last_write_end
                    or proof.get('binding_id') != binding.get('id')
                    or observation.get('snapshot_id') in revision_proof['forbidden_verification_snapshots']):
                    continue
                number_format = None
                parsed = None
                format_path = None
                interpretation = proof.get('numeric_interpretation')
                if isinstance(interpretation, dict):
                    from locua.number_format import parse_display_number
                    sources = locale_evidence.get(interpretation.get('locale_evidence_sha256'), [])
                    bundles = {x.get('bundle_id') for x in app_proof['matches'] if x.get('bundle_id')}
                    if len(sources) != 1 or len(bundles) != 1:
                        continue
                    number_format, format_path = sources[0]
                    parsed = parse_display_number(proof.get('actual'), number_format,
                        expected_target=observation['target'], expected_bundle_id=next(iter(bundles)),
                        now_ns=observation['observed_at_ns'])
                    if (parsed != interpretation or parsed.get('status') != 'parsed_under_observed_locale'
                        or parsed.get('application_formatter_proven') is not False):
                        continue
                # Replay historical freshness at its preserved capture clock;
                # never rewrite observation timestamps or claim it is live now.
                import locua.number_format as format_evaluator
                replay_clock = SimpleNamespace(time_ns=lambda: observation['observed_at_ns'])
                with patch.object(binding_evaluator, 'time', replay_clock), patch.object(format_evaluator, 'time', replay_clock):
                    checked = binding_evaluator.verify(binding, goal, observation, number_format=number_format)
                checked_evidence = checked.get('evidence') or {}
                if (not checked.get('matched') or checked_evidence.get('control_id') != proof.get('control_id')
                    or checked_evidence.get('actual') != proof.get('actual')):
                    continue
                controls = [c for c in observation.get('controls', []) if c.get('id') == proof.get('control_id')]
                if len(controls) != 1:
                    continue
                control = controls[0]
                actual = checked_evidence.get('actual')
                actual_number = Fraction(parsed['numerator'], parsed['denominator']) if parsed else number(actual)
                matched = (_editor_exact(control) and actual == task['oracle_buffer'] if 'oracle_buffer' in task
                           else actual_number == Fraction(str(task['oracle_display'])))
                if matched and not any(x['snapshot_id'] == observation['snapshot_id'] for x in fresh):
                    fresh.append({'snapshot_id': observation['snapshot_id'], 'observed_at_ns': observation['observed_at_ns'],
                        'path': path, 'control_id': control['id'], 'plane': checked_evidence.get('plane'),
                        'actual': actual, 'oracle_matched': True,
                        'receipt_issuance_matched': receipt.get('matched') is True,
                        'nested_display_readback': receipt.get('evidence') is None,
                        'binding_origin': revision_proof['selection'],
                        'replacement_target_selected_by_human': revision_proof['replacement_target_selected_by_human'],
                        'numeric_interpretation': parsed,
                        'locale_evidence_path': format_path,
                        'conditional_on_application_following_observed_locale': parsed is not None,
                        'application_formatter_proven': False})
            witness = scope.get('witness') or {}
            issuance = True if 'oracle_buffer' in task else (witness.get('known_start') is True
                and witness.get('issued_evaluation') == re.sub(r'\s+', '', goal.get('expression', '')).replace('×', '*').replace('÷', '/').replace('−', '-'))
            if 'oracle_display' in task:
                replay = arithmetic_replays.get(scope_id) or {}
                issuance = bool(issuance and replay.get('known_start')
                    and replay.get('issued_evaluation') == witness.get('issued_evaluation'))
            scope_reports.append({'scope_id': scope_id, 'goal_id': goal.get('id'),
                'approved': scope.get('status') == 'approved', 'protocol_goal_covered': outcome_ok,
                'requested_application': app_proof,
                'binding_revision_proof': revision_proof,
                'initial_fixture_matches': setup_ok, 'covers_entire_request_reviewed': scope.get('covers_entire_request') is True,
                'arithmetic_issuance_matches_request': issuance if 'oracle_display' in task else None,
                'recorded_verification_failures': [{'reason': r.get('reason'), 'display_readback_reason': (r.get('display_readback') or {}).get('reason')} for sid, r in receipts if sid == scope_id and r.get('goal_id') == goal.get('id') and r.get('matched') is not True],
                'fresh_independent_capture_count': len(fresh),
                'independently_verified_display_or_buffer': bool(fresh) and outcome_ok, 'fresh_outcomes': fresh,
                'pass': outcome_ok and setup_ok and revision_proof['valid'] and scope.get('status') == 'approved' and bool(fresh) and issuance
                    and scope.get('covers_entire_request') is True})
    successful = [x for x in scope_reports if x['pass']]
    # The current protocol has one goal per task. Additional scopes/goals are not
    # automatically credited; they require separate independent semantic review.
    coverage = len(scope_reports) == 1 and len(successful) == 1
    total_goals = sum(len(scope.get('goals') or []) for scope in (evidence.get('scopes') or {}).values())
    coverage = coverage and total_goals == 1
    text_edits = [x for x in input_audit if x['classification'] == 'bound_text_edit']
    # Navigation can include save-like operations: leave no-save unproved unless
    # every task input is an exact bound buffer edit. No app-label guessing.
    no_save_proven = bool('oracle_buffer' in task and input_audit
                         and len(text_edits) == len(input_audit) and not unknown_driver)
    functional = bool(coverage and task_writes and not uncertain and not issues and not store.missing and not manual)
    repeated = []
    refusals = []
    for event in events:
        result = event.get('result') or {}
        model_result = event.get('model_result') or result
        feedback = model_result.get('exploration_feedback') or {}
        if feedback.get('new_information') is False or feedback.get('code') in ('repeated_inspection', 'repeated_failed_read'):
            repeated.append({'sequence': event['sequence'], 'tool': event['tool'], 'code': feedback.get('code'),
                             'equivalent_inspection_count': feedback.get('equivalent_inspection_count')})
        if result.get('status') in ('refused', 'blocked', 'error', 'uncertain', 'failed'):
            refusals.append({'sequence': event['sequence'], 'tool': event['tool'],
                             'code': result.get('code'), 'reason': result.get('reason')})
    disclosure_failures = [x for x in framework_tools if x.get('code') == 'task_disclosure_projection_failed']
    after_privacy_latch = [x for x in framework_tools if x.get('code') == 'privacy_scope_blocked']
    first = None
    if not functional:
        if disclosure_failures:
            first = {'classification': 'harness_observation_disclosure_projection_failure', **disclosure_failures[0],
                     'model_decision_failure_primary': False}
        elif any(x.get('status') == 'projection_failed' for x in privacy_records):
            first = {'classification': 'harness_observation_disclosure_projection_failure',
                     'sequence': None, 'model_decision_failure_primary': False}
        elif refusals:
            first = {'classification': 'recorded_tool_refusal_or_uncertainty', **refusals[0]}
        elif repeated:
            first = {'classification': 'recorded_repeated_inspection_without_new_evidence', **repeated[0]}
        elif any(x['independently_verified_display_or_buffer'] and x['arithmetic_issuance_matches_request'] is False for x in scope_reports):
            first = {'classification': 'arithmetic_issuance_unproved_despite_correct_bound_display',
                     'sequence': None, 'wrong_numeric_result_proven': False}
        elif manual:
            first = {'classification': 'auditor_route_coverage_incomplete', 'sequence': None, 'model_error_proven': False}
        else:
            first = {'classification': 'outcome_or_execution_evidence_incomplete', 'sequence': None,
                     'reason': summary.get('reason'), 'causal_model_error_proven': False}
    unbound_numeric_matches = []
    if 'oracle_display' in task:
        for scope_id, scope in (evidence.get('scopes') or {}).items():
            same_target = [(o, path) for o, path in snapshots.values()
                           if o.get('target') == scope.get('target') and o.get('observed_at_ns', 0) > last_write_end and last_write_end]
            if not same_target:
                continue
            observation, path = max(same_target, key=lambda pair: pair[0].get('observed_at_ns', 0))
            for control in observation.get('controls', []):
                if control.get('role') not in ('AXStaticText', 'AXTextField', 'AXTextArea'):
                    continue
                value = control.get('value')
                interpreted = number(value)
                basis = 'complete_dot_decimal'
                if interpreted is None and isinstance(value, str):
                    clean = value.strip().replace('\u2212', '-').replace('\u200e', '').replace('\u200f', '')
                    if re.fullmatch(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?', clean):
                        interpreted = Fraction(clean.replace(',', ''))
                        basis = 'comma_thousands_grouping_hypothesis_locale_unproved'
                if interpreted == Fraction(str(task['oracle_display'])):
                    unbound_numeric_matches.append({'scope_id': scope_id, 'snapshot_id': observation['snapshot_id'],
                        'control_id': control['id'], 'path': path, 'actual': value, 'interpretation': basis,
                        'fresh_after_inputs': True, 'requested_output_binding_proven': False,
                        'task_completion_proven': False})
    if (not functional and unbound_numeric_matches and not refusals and not disclosure_failures
        and not any(x['independently_verified_display_or_buffer'] for x in scope_reports)
        and any(x['arithmetic_issuance_matches_request'] is False for x in scope_reports)):
        first = {'classification': 'arithmetic_issuance_and_output_binding_unproved', 'sequence': None,
                 'numeric_display_compatible_with_oracle': True, 'wrong_numeric_result_proven': False}
    metrics = count_metrics(records, summary)
    for key in ('input_tokens', 'output_tokens'):
        reported = (summary.get('metrics') or {}).get('reported_usage', {}).get(key)
        if reported is not None and reported != metrics['known_' + key]:
            issues.append('summary_provider_' + key + '_mismatch')
    times = [x.get('started_at_ns') for x in driver if type(x.get('started_at_ns')) is int]
    bounds = []
    for record in metrics['calls']:
        for key, limit_key in (('input_tokens', 'generation_input_limit'), ('output_tokens', 'generation_output_limit')):
            actual = record['gross_input_tokens'] if key == 'input_tokens' else record[key]
            if actual is not None and type(protocol.get(limit_key)) is int and actual > protocol[limit_key]:
                bounds.append({'call': record['call'], 'kind': limit_key, 'actual': actual, 'limit': protocol[limit_key]})
        seconds = record['generation_s']
        if seconds is not None and protocol.get('per_generation_timeout_s') is not None and seconds > protocol['per_generation_timeout_s']:
            bounds.append({'call': record['call'], 'kind': 'completed_generation_wall_exceeds_budget',
                           'actual': seconds, 'limit': protocol['per_generation_timeout_s']})
    calls_requested = (summary.get('metrics') or {}).get('provider_requests', len(records))
    if protocol.get('per_session_model_calls') is not None and calls_requested > protocol['per_session_model_calls']:
        bounds.append({'kind': 'provider_requests_exceed_model_call_budget', 'actual': calls_requested, 'limit': protocol['per_session_model_calls']})
    model_matches = (summary.get('model') == 'qwen38' and all(identity.get('model_pin', {}).get('model_id') == protocol.get('local_model')
        and identity.get('model_pin', {}).get('revision') == protocol.get('local_revision') for identity in metrics['identities'])
        and bool(metrics['identities'])) if summary.get('inference_local_only') is True else any(
        summary.get('provider') == item.get('provider') and summary.get('model') == item.get('model')
        for item in protocol.get('hosted_requested') or [])
    after_freeze = bool(times and min(times) >= protocol.get('created_at_ns', 2**64))
    supported_interface = summary.get('tool_interface') in ('tools-v6.2', 'tools-v6.3', 'tools-v6.4', 'tools-v6.5')
    declared = protocol.get('tool_interface') or re.search(r'tools-v\d+\.\d+', protocol.get('harness_semantics', ''))
    expected_interface = declared.group(0) if hasattr(declared, 'group') else declared or 'tools-v6.2'
    interface_match = summary.get('tool_interface') == expected_interface
    eligible = trial_role == 'comparison' and after_freeze and interface_match and request_matches and model_matches and not bounds
    functional = functional and not issues
    if not functional and first is None:
        first = {'classification': 'evidence_integrity_failure', 'sequence': None}
    manager_stop = store.read(run / 'manager-stop.json', optional=True)
    supplemental = {p.name: store.read(p, optional=True) for p in (run / 'unintended-changes.json', run / 'setup.json', run / 'installed-identity.json') if p.is_file()}
    evaluator_files = {str(p): sha(p.read_bytes()) for p in (Path(__file__).resolve(), ROOT / 'src/locua/goal_verification.py', ROOT / 'src/locua/goal_planner.py', ROOT / 'src/locua/arithmetic_input.py', ROOT / 'src/locua/amplifier_tools.py', ROOT / 'src/locua/engine/prototype/perception.py', ROOT / 'src/locua/number_format.py')}
    return {'schema_version': VERSION, 'run': str(run), 'task_id': task_id, 'trial_role': trial_role,
        'status': 'verified_reviewed_outcome' if functional else ('manual_audit_required' if manual else 'not_verified'), 'functional_pass': functional,
        'conditional_on_application_following_observed_locale': any(
            row['conditional_on_application_following_observed_locale']
            for outcome in scope_reports for row in outcome['fresh_outcomes']),
        'comparison_eligibility': {'eligible': eligible, 'after_protocol_freeze': after_freeze,
                                 'required_tool_interface_matches': interface_match, 'allowed_model_identity_matches': model_matches,
                                 'supported_tool_interface': supported_interface, 'protocol_tool_interface': expected_interface,
                                 'cross_run_source_equivalence_proven': False,
                                 'note_scope': 'Necessary single-run checks only; compare frozen installed source and privacy configuration separately.',
                                 'note': 'Historical audit regressions do not enter the new comparison denominator.'},
        'request_coverage': {'exact_request_matches': request_matches, 'protocol_goal_verified': coverage,
                             'human_reviewed': bool(summary.get('human_interactions')),
                             'autonomous_language_fidelity_proven': False},
        'reconstructed_input_captures': reconstructed,
        'observed_numeric_oracle_matches_without_binding_credit': unbound_numeric_matches,
        'outcomes': scope_reports, 'manual_audit_required': manual,
        'numeric_proof_limit': 'Observed configured locale conventions are independently checked; application_formatter_proven remains false. A locale-backed match is conditional on the application following those conventions.',
        'input_effect_reconciliation': input_audit,
        'execution': {'task_input_requests': len(task_writes), 'public_task_input_events': len(acts),
            'driver_tool_counts': dict(Counter(x['request']['name'] for x in driver)),
            'setup_effects': dict(Counter(x['request']['name'] for x in driver if x['request']['name'] in SETUP)),
            'arithmetic_issuance_recomputed': arithmetic_replay,
            'arithmetic_witness_consistency': witness_consistency if arithmetic_replay is not None else None,
            'acknowledgements_without_effect_proof': ack_without_effect_proof,
            'acknowledgement_rule': 'Successful dispatch is issuance only. AX effect:unverifiable may be resolved for the reviewed outcome by independent final readback; refused/uncertain dispatch is never promoted.',
            'uncertain_task_input_indices': uncertain, 'unclassified_driver_routes': unknown_driver,
            'no_explicit_save_in_recorded_task_inputs': no_save_proven if 'oracle_buffer' in task else None,
            'saved_output_proven': False, 'autosave_or_disk_unchanged_proven': False,
            'global_unintended_change_absence_proven': False, 'unintended_change_sidecars': supplemental},
        'first_consequential_failure': first, 'tool_calls': len(framework_tools) if session else len(events),
        'underlying_desktop_tool_calls': len(events), 'tool_refusals': refusals,
        'framework_tool_results': framework_tools,
        'privacy': {'projection_records': privacy_records, 'blocked_attempts_after_latch': after_privacy_latch},
        'context_compactions': sum(x.get('event') == 'context:compaction' for x in session.get('events') or []),
        'repeated_inspections': repeated, 'reviews': {'requests': sum(x.get('tool') == 'locua_review' for x in events),
            'approved': sum(x.get('tool') == 'locua_review' and x.get('result', {}).get('status') == 'approved' for x in events),
            'human_interactions': len(summary.get('human_interactions') or []), 'human_wait_s': summary.get('human_wait_s')},
        'metrics': metrics, 'recorded_budget_violations': bounds,
        'configuration': {k: summary.get(k) for k in ('model', 'provider', 'provider_configuration', 'decoding', 'thinking', 'inference_local_only', 'rlcd_used', 'policy', 'tool_interface', 'prefix_cache_policy')},
        'api_cost': summary.get('api_cost'),
        'latency': {'workflow_wall_s': summary.get('full_workflow_wall_s'),
            'excluding_review_s': summary.get('wall_excluding_human_s'),
            'practical_limit_s': protocol.get('practical_local_latency_gate_s'),
            'practical_gate_pass': functional and isinstance(summary.get('wall_excluding_human_s'), (float, int))
                and summary['wall_excluding_human_s'] <= protocol.get('practical_local_latency_gate_s', 0),
            'tokenizer_preflight': {k:v for k,v in (summary.get('budget_measurements') or {}).items() if k != 'measurements'}},
        'cleanup': {k: summary.get(k) for k in ('provider_closed', 'session_cleanup', 'desktop_cleanup', 'desktop_control_lease')},
        'cancellation': manager_stop or {'reported_reason': summary.get('reason'), 'actor_independently_recorded': False},
        'oracle_boundary': {'source': str(protocol_path), 'evaluator_only': True, 'model_or_desktop_calls': 0,
            'historical_run_oracle_nonexposure_independently_proven': False,
            'note': 'This audit never forwards oracle data to inference. Answers appearing after model computation are not contamination by themselves.'},
        'evidence': store.files, 'evaluator_source_sha256': evaluator_files,
        'missing_evidence': store.missing, 'integrity_issues': sorted(set(issues)),
        'limitations': ['Only retained run effects are auditable; no global untouched-desktop claim.',
            'Human-reviewed scope is not autonomous language coverage.',
            'Recorded binding evaluator is rerun against archived fresh captures, not a new live capture.',
            'No success from launch, plan, model final text, write acknowledgement, or old cached readback alone.',
            'This single-run report does not establish the six-trial model selection gate.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--protocol', type=Path, default=ROOT / 'artifacts/model-comparison-v7-001/protocol.json')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--trial-role', choices=('comparison', 'historical_regression'), default='comparison')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.run, args.protocol, args.task_id, trial_role=args.trial_role)
    args.out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    print(json.dumps({'status': result['status'], 'functional_pass': result['functional_pass'],
                     'integrity_issues': result['integrity_issues'], 'out': str(args.out)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
