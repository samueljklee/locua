"""Bounded execution of model-selected, already observed action descriptors.

This module never chooses actions, invents controls or evaluates a goal. Every
dispatch delegates to the same guarded single-action path. Retained identities
only select a candidate; fresh scope, preservation and transition checks remain
mandatory before input. Private receipts preserve every attempted step.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from .action_sequence_guard import check_transition, remap_control
from .engine.prototype.cli import private_json

VERSION = 'observed-action-sequence-v1'
MAX_STEPS = 32
MAX_PUBLIC_RECEIPTS = 8


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _reason(value, limit=768):
    text = value if isinstance(value, str) else str(value)
    if len(text.encode('utf-8')) <= limit:
        return text
    return 'Exact blocking detail retained privately (sha256 ' + _digest(text) + ').'


class SequencePreflightError(ValueError):
    """A rejected source plan, before any step or input was attempted."""
    def __init__(self, reason, *, scope_id, snapshot_id, steps, failed_step=None,
                 action=None, control=None, source_action_id=None, recover_scope=False,
                 arithmetic_mismatch=None):
        super().__init__(str(reason))
        self.scope_id = scope_id if isinstance(scope_id, str) and len(scope_id) <= 256 else None
        self.snapshot_id = snapshot_id if isinstance(snapshot_id, str) and len(snapshot_id) <= 256 else None
        self.steps_planned = len(steps) if isinstance(steps, list) else 0
        self.failed_step = failed_step
        self.selected_control = None
        self.recover_scope = recover_scope
        self.arithmetic_mismatch = arithmetic_mismatch
        self.control_id = control.get('id') if isinstance(control, dict) else None
        if failed_step is not None:
            name = control.get('name') if isinstance(control, dict) else None
            role = control.get('role') if isinstance(control, dict) else None
            source_id = action.get('id') if isinstance(action, dict) else source_action_id
            self.selected_control = {
                'name': name if isinstance(name, str) and len(name.encode('utf-8')) <= 512 else None,
                'role': role if isinstance(role, str) and len(role) <= 128 else None,
                'action_kind': action.get('kind') if isinstance(action, dict) else None,
                'source_action_id': source_id if isinstance(source_id, str) and len(source_id) <= 256 else None}
            if isinstance(name, str) and self.selected_control['name'] is None:
                self.selected_control['name_deferred'] = {'sha256': _digest(name),
                    'utf8_bytes': len(name.encode('utf-8')), 'retained_inspection_required': True}

    def as_result(self):
        if self.recover_scope:
            recovery = {'tool': 'locua_status', 'arguments': {'operation': 'scopes'}}
            next_step = 'Use locua_status operation=scopes to recover an existing approved scope; no input was attempted.'
        elif self.snapshot_id is not None:
            arguments = {'snapshot_id': self.snapshot_id, 'operation': 'control' if self.control_id else 'list'}
            if self.control_id:
                arguments['control_id'] = self.control_id
            recovery = {'tool': 'locua_inspect', 'arguments': arguments}
            next_step = ('Inspect the selected retained control and reviewed scope; choose only actions within its existing effects. '
                         'If the snapshot is superseded, use locua_status to recover retained references. No step was attempted or substituted.')
        else:
            recovery = {'tool': 'locua_status', 'arguments': {'operation': 'summary'}}
            next_step = 'Use locua_status to recover observed snapshots and scopes, then provide 1..32 original observed action IDs.'
        reason = str(self)
        bounded_reason = reason if len(reason.encode('utf-8')) <= 768 else 'Preflight detail exceeds the response budget; inspect the identified source step.'
        result = {'version': VERSION, 'status': 'refused', 'code': 'sequence_preflight_refused',
            'reason': bounded_reason, 'scope_id': self.scope_id, 'source_snapshot_id': self.snapshot_id,
            'snapshot_id': self.snapshot_id, 'failed_step': self.failed_step,
            'selected_control': self.selected_control, 'steps_planned': self.steps_planned,
            'steps_attempted': 0, 'steps_completed': 0, 'steps_unattempted': self.steps_planned,
            'action_started': False, 'no_retry': False, 'arguments_rewritten': False,
            'goal_verified': False, 'task_complete': False, 'current_state_proven': False,
            'recovery': recovery, 'next': next_step}
        if bounded_reason != reason:
            result['reason_sha256'] = _digest(reason)
        if self.arithmetic_mismatch is not None:
            result['arithmetic_preflight'] = deepcopy(self.arithmetic_mismatch)
            result['recovery'] = {'tool': 'locua_status', 'arguments': {
                'operation': 'goals', 'scope_id': self.scope_id}}
            result['next'] = ('Compare the proposed observed actions with the original reviewed expression. '
                'Inspect retained controls or use locua_status operation=witness for prior issued input. '
                'Choose any corrected actions yourself; no input or argument substitution occurred.')
        return result


def _expression_evidence(expression):
    encoded = expression.encode('utf-8')
    return {'expression': expression if len(encoded) <= 512 else None,
            'sha256': hashlib.sha256(encoded).hexdigest(), 'utf8_bytes': len(encoded),
            'deferred': len(encoded) > 512}


def _arithmetic_mismatch(owner, scope, original, frozen):
    """Simulate issuance, never application behavior or successful delivery.

    The owner has one witness per scope and ordered effect routing. Mirror that
    on a copy, including one-shot navigation effects, so a numeric-looking
    navigation press cannot accidentally become arithmetic in this check.
    Unknown start or an unavailable hypothetical route supplies no mismatch
    proof; the unchanged per-step guards remain responsible for those cases.
    """
    if not any(g['kind'] == 'calculation' for g in scope['goals']):
        return None
    simulated = deepcopy(scope)
    witness = simulated['witness']
    for item in frozen:
        action = item['action']
        try:
            permit = owner._permit(simulated, original, action, item.get('value'))
        except (ValueError, KeyError, TypeError):
            return None
        kind = permit['kind']
        if kind == 'press':
            simulated['issued_press_effects'].append(permit['effect_index'])
        elif kind == 'arithmetic_text':
            witness.record_replacement(item['value'], snapshot_id=original['snapshot_id'],
                                       descriptor=action['description'])
        elif kind == 'arithmetic':
            token = permit['symbol']
            if not witness.known_start and token not in ('clear', 'clear_entry'):
                # _act would refuse this step, so do not simulate later input
                # as though a blocked prefix had actually been issued.
                return None
            witness.record(token, snapshot_id=original['snapshot_id'], descriptor=action['description'])
            if token == '=' and witness.known_start:
                goal = next(g for g in simulated['goals'] if g['id'] == permit['goal_id'])
                if not witness.matches(goal['expression']):
                    return item, {'status': 'contradictory_evaluated_expression',
                        'goal_id': goal['id'], 'proposed': _expression_evidence(witness.evaluated),
                        'reviewed': _expression_evidence(goal['expression']),
                        'known_start': True, 'simulation_only': True,
                        'actual_witness_changed': False, 'result_computed': False,
                        'basis': 'Existing input witness plus original observed actions under reviewed effect routing.'}
    return None


def _freeze(owner, scope_id, snapshot_id, steps):
    def fail(error, **details):
        return SequencePreflightError(error, scope_id=scope_id, snapshot_id=snapshot_id,
                                      steps=steps, **details)
    if not isinstance(scope_id, str) or not isinstance(snapshot_id, str):
        raise fail('scope_id and snapshot_id must be observed string references',
                   recover_scope=not isinstance(scope_id, str))
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise fail('steps must contain 1..32 observed actions')
    try:
        scope = owner._scope(scope_id)
    except (ValueError, KeyError, TypeError) as error:
        raise fail(error, recover_scope=True) from error
    try:
        original = deepcopy(owner._observation(snapshot_id))
    except (ValueError, KeyError, TypeError) as error:
        raise fail(error) from error
    if original['target'] != scope['target']:
        raise fail('Sequence snapshot differs from the reviewed target', recover_scope=True)
    if any(s['target'] == scope['target'] and s.get('uncertain_action')
           for s in owner._scopes.values()):
        raise fail('Target has uncertain input; sequence cannot restore input authority', recover_scope=True)
    frozen = []
    for index, entry in enumerate(steps, 1):
        action = control = action_id = None
        try:
            if not isinstance(entry, dict) or set(entry) - {'action_id', 'value'}:
                raise ValueError('Each step accepts only action_id and optional literal value')
            action_id = entry.get('action_id')
            if not isinstance(action_id, str) or not action_id:
                raise ValueError('Every step requires an original observed action_id')
            action = owner._actions.get(snapshot_id, {}).get(action_id)
            if not isinstance(action, dict):
                raise ValueError('Action is absent from the original snapshot')
            if (action.get('snapshot_id') != snapshot_id or action.get('target') != scope['target']
                    or action.get('kind') not in ('press', 'set_text')):
                raise ValueError('Original action descriptor is inconsistent or unsupported')
            control = owner._find(original, action['control_id'])
            has_value = 'value' in entry
            if action['kind'] == 'set_text':
                if not has_value or not isinstance(entry['value'], str) or '\x00' in entry['value']:
                    raise ValueError('set_text requires a literal string without NUL')
            elif has_value:
                raise ValueError('press does not accept a value')
            # Refuse already-known out-of-scope steps before executing any prefix.
            # _act repeats permission checks against independently fresh evidence.
            owner._permit(scope, original, action, entry.get('value'))
        except (ValueError, KeyError, TypeError) as error:
            raise fail(error, failed_step=index, action=action, control=control,
                       source_action_id=action_id) from error
        item = {'index': index, 'source_action_id': action_id,
                'action': deepcopy(action), 'value_present': has_value}
        if has_value:
            item['value'] = entry['value']
        frozen.append(item)
    mismatch = _arithmetic_mismatch(owner, scope, original, frozen)
    if mismatch is not None:
        item, evidence = mismatch
        raise fail('Proposed observed actions would evaluate a different expression from the reviewed calculation.',
            failed_step=item['index'], action=item['action'],
            control=owner._find(original, item['action']['control_id']), arithmetic_mismatch=evidence)
    return original, frozen, _digest(owner._reviewed_contract(scope))


def _mapped(owner, original, item, current):
    control_id = remap_control(original, item['action']['control_id'], current)
    actions = [a for a in owner._actions.get(current['snapshot_id'], {}).values()
               if a.get('control_id') == control_id and a.get('kind') == item['action']['kind']]
    if len(actions) != 1:
        raise ValueError('Original action no longer has one matching current capability')
    return deepcopy(actions[0])


def _unknown(owner, scope_id, action, before, error):
    """Unclassified exceptions cannot establish that input did not start."""
    return owner._block_action(scope_id, owner._scopes[scope_id], action,
        {'kind': 'sequence_unclassified'}, before,
        {'status': 'uncertain', 'code': 'sequence_dispatch_exception',
         'reason': str(error), 'error_type': type(error).__name__,
         'action_started': None, 'no_retry': True})


def run_sequence(toolset, scope_id, snapshot_id, steps):
    """Execute one immutable observed sequence under the owner's serial lock.

    The latest eight compact receipts are public; all full results are written
    to private exclusive files. Completion means the sequence was dispatched,
    never that its reviewed goal or the entire user request was verified.
    """
    with toolset._lock:
        return _run(toolset, scope_id, snapshot_id, steps)


def _run(owner, scope_id, snapshot_id, steps):
    if owner._cancellation is not None:
        return {**deepcopy(owner._cancellation), 'scope_id': scope_id,
                'steps_planned': len(steps) if isinstance(steps, list) else 0,
                'steps_completed': 0, 'steps_attempted': 0, 'action_started': False,
                'goal_verified': False, 'task_complete': False}
    if owner._closed:
        raise ValueError('Toolset is closed')
    try:
        original, frozen, contract_hash = _freeze(owner, scope_id, snapshot_id, steps)
    except SequencePreflightError as error:
        return error.as_result()
    sequence_id = 'sequence:' + uuid.uuid4().hex
    stem = 'sequence-' + sequence_id.split(':', 1)[1]
    source_path = (Path(owner.out) / (stem + '-source.json')).resolve()
    private_json(source_path, {'version': VERSION, 'sequence_id': sequence_id,
        'scope_id': scope_id, 'source_snapshot_id': snapshot_id, 'target': original['target'],
        'reviewed_contract_sha256': contract_hash, 'source_observation': original, 'steps': frozen})
    receipts = []
    attempted = completed = 0
    status = 'sequence_completed'
    reason = 'Every requested step was dispatched; verify the original reviewed goal.'
    code = None
    any_started = False
    unknown_started = False
    terminal_no_retry = False
    latest_id = snapshot_id

    for position, item in enumerate(frozen):
        if owner._cancellation is not None:
            status = 'canceled'
            reason = owner._cancellation.get('reason', 'Owner canceled the task')
            code = owner._cancellation.get('code')
            break
        try:
            scope = owner._scope(scope_id)
            if _digest(owner._reviewed_contract(scope)) != contract_hash:
                raise ValueError('Original reviewed contract changed during the sequence')
            current_id = owner._latest.get(_digest(original['target']))
            if not current_id:
                raise ValueError('No current retained target capture; inspect before another action')
            current = deepcopy(owner._observation(current_id))
            # Protect every pending target, not merely the next selected one.
            # Repeated source IDs remain legal only while the original identity
            # and signature still match uniquely; no mutable binding update.
            pending = [_mapped(owner, original, step, current) for step in frozen[position:]]
            action = pending[0]
            future_ids = [a['control_id'] for a in pending[1:]]
        except Exception as error:
            status, code, reason = 'refused', 'sequence_source_changed', str(error)
            break
        step_path = (Path(owner.out) / (stem + '-step-' + str(position + 1).zfill(3) + '.json')).resolve()
        arguments = {'scope_id': scope_id, 'snapshot_id': current_id, 'action_id': action['id']}
        if item['value_present']:
            arguments['value'] = item['value']
        transition_before = {}
        fresh_before = None
        fresh_received = None
        fresh_action_before = None

        def pre_dispatch(fresh):
            nonlocal fresh_before, fresh_received, fresh_action_before, transition_before
            fresh_received = deepcopy(fresh)
            transition_before = check_transition(current, fresh, acted_control_id=None,
                planned_control_ids=[a['control_id'] for a in pending])
            if transition_before.get('matched') is True:
                fresh_before = deepcopy(fresh)
                fresh_action_before = _mapped(owner, original, item, fresh)
            return transition_before

        owner.progress('Action sequence step ' + str(position + 1) + '/' + str(len(frozen)) + ': fresh checks.')
        attempted += 1
        exception = None
        interrupted = None
        try:
            result = owner._act(**arguments, _pre_dispatch=pre_dispatch)
            if not isinstance(result, dict):
                raise ValueError('Single-action result is not an evidence object')
        except BaseException as error:
            exception = {'type': type(error).__name__, 'message': str(error)}
            result = _unknown(owner, scope_id, action, fresh_before or current, error)
            if not isinstance(error, Exception):
                interrupted = error
        started = result.get('action_started')
        any_started = any_started or started is True
        unknown_started = unknown_started or started is None
        terminal_no_retry = terminal_no_retry or result.get('no_retry') is True or result.get('status') == 'uncertain'
        transition = None
        if isinstance(result.get('snapshot_id'), str):
            latest_id = result['snapshot_id']
        if result.get('status') in ('verified', 'dispatched'):
            completed += 1
            try:
                if fresh_before is None:
                    raise ValueError('Fresh sequence predispatch proof was not recorded')
                after = deepcopy(owner._observation(result['snapshot_id']))
                fresh_action = _mapped(owner, original, item, fresh_before)
                fresh_future_ids = [remap_control(current, cid, fresh_before) for cid in future_ids]
                transition = check_transition(fresh_before, after,
                    acted_control_id=fresh_action['control_id'], planned_control_ids=fresh_future_ids)
                if transition.get('matched') is not True:
                    status, code, reason = 'refused', 'sequence_state_changed', transition.get('reason', 'Observed structure changed')
            except Exception as error:
                status, code, reason = 'refused', 'sequence_state_changed', str(error)
                transition = {'matched': False, 'reason': str(error)}
        else:
            status = result.get('status') if result.get('status') in ('refused', 'unavailable', 'uncertain', 'canceled') else 'unavailable'
            code = result.get('code', 'sequence_step_stopped')
            reason = result.get('reason', 'Single-action path did not establish a completed step')
        full = {'version': VERSION, 'sequence_id': sequence_id, 'step': position + 1,
                'source_action_id': item['source_action_id'], 'arguments': arguments,
                'result': deepcopy(result), 'pre_dispatch_transition': transition_before,
                'pre_dispatch_snapshot_id': fresh_received.get('snapshot_id') if fresh_received else None,
                'pre_dispatch_observed_at_ns': fresh_received.get('observed_at_ns') if fresh_received else None,
                'pre_dispatch_observation': fresh_received,
                'pre_dispatch_action': fresh_action_before,
                'transition': transition, 'exception': exception}
        try:
            private_json(step_path, full)
        except Exception as error:
            # No later input after an evidence-write failure, and no inferred
            # safe replay merely because the public receipt could not be saved.
            _unknown(owner, scope_id, action, fresh_before or current, error)
            raise
        receipts.append({'step': position + 1, 'source_action_id': item['source_action_id'],
            'action_id': action['id'], 'input_snapshot_id': current_id,
            'snapshot_id': result.get('snapshot_id'), 'status': result.get('status'),
            'action_started': started, 'transition_matched': transition.get('matched') if transition else None,
            'full_response_ref': str(step_path) + '#result'})
        owner.progress('Action sequence step ' + str(position + 1) + '/' + str(len(frozen)) + ': ' + str(result.get('status')) + '.')
        if interrupted is not None:
            raise interrupted
        if status != 'sequence_completed':
            break

    current_scope = owner._scopes.get(scope_id, {})
    witness = current_scope.get('witness')
    arithmetic = witness.view() if witness is not None else None
    if arithmetic is not None and len(_encoded(arithmetic)) > 2048:
        arithmetic = {'known_start': arithmetic.get('known_start'), 'details_deferred': True,
            'sha256': _digest(arithmetic), 'next': 'locua_status operation=witness for this scope'}
    return {'version': VERSION, 'sequence_id': sequence_id, 'status': status,
        'code': code, 'reason': _reason(reason), 'scope_id': scope_id,
        'source_snapshot_id': snapshot_id, 'snapshot_id': latest_id,
        'target': deepcopy(original['target']), 'steps_planned': len(frozen),
        'steps_completed': completed, 'steps_attempted': attempted,
        'steps_unattempted': len(frozen) - attempted,
        'action_started': True if any_started else None if unknown_started else False,
        'no_retry': terminal_no_retry, 'do_not_repeat_sequence': attempted > 0,
        'goal_verified': False, 'task_complete': False, 'current_state_proven': False,
        'arithmetic_input': arithmetic, 'receipts': receipts[-MAX_PUBLIC_RECEIPTS:],
        'receipts_omitted': max(0, len(receipts) - MAX_PUBLIC_RECEIPTS),
        'source_evidence_ref': str(source_path),
        'next': ('Call locua_verify for this original scope; sequence completion is not goal verification.'
                 if status == 'sequence_completed' else
                 'Do not replay the sequence or its completed prefix. Inspect retained state/status; uncertain input permits read-only recovery only.')}
