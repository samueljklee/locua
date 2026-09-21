"""CPU tests using the actual single-action and pure transition guards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from locua.action_sequence import run_sequence, MAX_PUBLIC_RECEIPTS, SequencePreflightError, _freeze
from locua.amplifier_tools import DesktopToolset
import test_amplifier_tools as fixtures


class SequenceDesktop(fixtures.Desktop):
    def observe(self, target):
        result = super().observe(target)
        if result.get('status') != 'observed':
            return result
        o = result['observation'];sid = o['snapshot_id']
        for name in ('0', '1', '4', '5', '6', '7', '8', '9', '*', '-', 'Clear Entry'):
            c = deepcopy(next(c for c in o['controls'] if c['name'] == '2'))
            c.update(id=sid + ':' + str(len(o['controls'])), name=name,
                     semantics={'identifier': name})
            c['bounds']['x'] = len(o['controls']) * 10
            o['controls'].append(c)
        ids = {c['id']: 'native:' + c['id'] for c in o['controls']}
        for index, c in enumerate(o['controls']):
            c['id'] = ids[c['id']]
            c['parent'] = ids.get(c['parent'])
            token = sid + ':' + str(index)
            c['source'] = {'kind': 'native', 'node': {'role': c['role'],
                'element_index': index, 'element_token': token}}
            o['handles'][c['id']] = {'kind': 'native', **target, 'snapshot_id': sid,
                'element_index': index, 'element_token': token}
        return result


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.desktop = SequenceDesktop();self.progress = []
        self.owner = DesktopToolset({}, Path(self.tmp.name)/'tools', 'Perform reviewed task',
            lambda *_: 'run', desktop=self.desktop, progress=self.progress.append)
        self.addCleanup(self.owner.close)
        app = self.owner.call('locua_apps', {})['items'][0]['app_id']
        window = self.owner.call('locua_windows', {'app_id': app})['windows'][0]['window_id']
        self.window = window
        self.sid = self.owner.call('locua_observe', {'window_id': window})['snapshot_id']

    def control(self, name, sid=None):
        return next(c for c in self.owner._observations[sid or self.sid]['controls'] if c['name'] == name)

    def step(self, name, value=None, sid=None):
        sid = sid or self.sid;cid = self.control(name, sid)['id']
        action = next(a for a in self.owner._actions[sid].values() if a['control_id'] == cid)
        return {'action_id': action['id'], **({'value': value} if value is not None else {})}

    def review_calc(self, expression='2+3'):
        r = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Perform arithmetic',
            'goals': [{'id': 'calc', 'kind': 'calculation', 'target': 'Result',
                'control_id': self.control('Result')['id'], 'expression': expression, 'evidence_plane': 'display'}],
            'effects': [{'kind': 'goal', 'goal_id': 'calc'}]})
        self.assertEqual(r['status'], 'approved', r)
        return r['scope_id']

    def review_text(self, preserve=False):
        args = {'snapshot_id': self.sid, 'summary': 'Edit exact text',
            'goals': [{'id': 'entry', 'kind': 'text', 'target': 'Entry',
                'control_id': self.control('Entry')['id'], 'value': 'exact', 'evidence_plane': 'editor_buffer'}],
            'effects': [{'kind': 'goal', 'goal_id': 'entry'}]}
        if preserve:
            args['preserves'] = [{'control_id': self.control('Keep')['id'], 'property': 'checked', 'value': True}]
        r = self.owner.call('locua_review', args)
        self.assertEqual(r['status'], 'approved', r)
        return r['scope_id']

    def run_steps(self, scope, *names):
        return run_sequence(self.owner, scope, self.sid, [self.step(n) for n in names])

    def records(self, result):
        return [json.loads(Path(r['full_response_ref'].split('#')[0]).read_text()) for r in result['receipts']]

    def test_original_observed_calculation_uses_single_action_guards_and_needs_verify(self):
        scope = self.review_calc()
        result = self.run_steps(scope, 'All Clear', '2', '+', '3', '=')
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual((result['steps_completed'], result['steps_attempted']), (5, 5))
        self.assertFalse(result['goal_verified']);self.assertFalse(result['task_complete'])
        self.assertEqual(result['arithmetic_input']['issued_evaluation'], '2+3')
        self.assertEqual(len(self.desktop.executions), 5)
        self.assertEqual(len({a['snapshot_id'] for a in self.desktop.executions}), 5)
        records = self.records(result)
        self.assertTrue(all(r['pre_dispatch_transition']['matched'] and r['transition']['matched'] for r in records))
        self.assertTrue(all(r['pre_dispatch_action']['snapshot_id'] == r['pre_dispatch_snapshot_id'] for r in records))
        self.assertTrue(all(r['arguments']['snapshot_id'] != r['pre_dispatch_snapshot_id'] for r in records))
        self.assertEqual(self.owner.call('locua_verify', {'scope_id': scope})['status'], 'verified')
        self.assertEqual(len(self.progress), 10)

    def test_full_source_validation_precedes_any_input(self):
        scope = self.review_calc();good = self.step('All Clear')
        for bad in [{'action_id': 'foreign'}, {'action_id': []}, {'action_id': True},
                    {'action_id': good['action_id'], 'value': 'ignored'},
                    {'action_id': good['action_id'], 'extra': 1}]:
            with self.subTest(bad=bad):
                result = run_sequence(self.owner, scope, self.sid, [good, bad])
                self.assertEqual(result['code'], 'sequence_preflight_refused')
                self.assertEqual(result['failed_step'], 2)
                self.assertFalse(result['action_started'])
        self.assertFalse(self.desktop.executions)
        self.assertFalse(list(self.owner.out.glob('sequence-*-source.json')))

    def test_bounds_empty_or_more_than32_refuse(self):
        scope = self.review_calc()
        for steps in [[], [self.step('2')]*33, {}, None]:
            with self.subTest(steps=type(steps).__name__):
                result = run_sequence(self.owner, scope, self.sid, steps)
                self.assertEqual(result['code'], 'sequence_preflight_refused')
                self.assertEqual(result['steps_attempted'], 0)
        self.assertFalse(self.desktop.executions)

    def test_known_out_of_scope_later_step_refuses_whole_prefix(self):
        scope = self.review_calc()
        result = self.run_steps(scope, 'All Clear', 'Hide panel')
        self.assertEqual(result['code'], 'sequence_preflight_refused')
        self.assertEqual(result['failed_step'], 2)
        self.assertFalse(self.desktop.executions)

    def test_missing_or_wrong_text_value_refuses_before_any_input(self):
        scope = self.review_text()
        for value in [None, 3, 'wrong', 'exact\x00']:
            entry = self.step('Entry')
            if value is not None:entry['value'] = value
            with self.subTest(value=value):
                result = run_sequence(self.owner, scope, self.sid, [entry])
                self.assertEqual(result['code'], 'sequence_preflight_refused')
                self.assertFalse(result['action_started'])
        self.assertFalse(self.desktop.executions)

    def test_step7_mode_refusal_names_original_choice_without_replacement(self):
        mode = self.control('Hide panel');mode['name'] = 'Mode'
        scope = self.review_calc()
        steps = [self.step('All Clear')] + [self.step('2')]*5 + [self.step('Mode')]
        original = deepcopy(steps)
        result = run_sequence(self.owner, scope, self.sid, steps)
        self.assertEqual(result['failed_step'], 7)
        self.assertEqual(result['selected_control'], {'name': 'Mode', 'role': 'AXButton',
            'action_kind': 'press', 'source_action_id': steps[6]['action_id']})
        self.assertEqual(result['recovery'], {'tool': 'locua_inspect', 'arguments': {
            'operation': 'control', 'snapshot_id': self.sid, 'control_id': mode['id']}})
        self.assertEqual((result['steps_completed'], result['steps_attempted']), (0, 0))
        self.assertFalse(result['arguments_rewritten']);self.assertFalse(result['task_complete'])
        self.assertEqual(steps, original);self.assertFalse(self.desktop.executions)
        self.assertFalse(list(self.owner.out.glob('sequence-*.json')))

    def test_direct_freeze_raises_typed_information(self):
        scope = self.review_calc();steps = [self.step('All Clear'), {'action_id': 'foreign'}]
        with self.assertRaises(SequencePreflightError) as caught:
            _freeze(self.owner, scope, self.sid, steps)
        self.assertEqual(caught.exception.failed_step, 2)
        self.assertEqual(caught.exception.as_result()['selected_control']['source_action_id'], 'foreign')

    def test_unknown_scope_recovery_uses_status_without_reading_ui(self):
        before = len(self.desktop.calls)
        result = run_sequence(self.owner, 'scope:unknown', self.sid, [self.step('All Clear')])
        self.assertEqual(result['code'], 'sequence_preflight_refused')
        self.assertEqual(result['recovery'], {'tool': 'locua_status', 'arguments': {'operation': 'scopes'}})
        self.assertIsNone(result['failed_step']);self.assertIsNone(result['selected_control'])
        self.assertEqual(len(self.desktop.calls), before);self.assertFalse(result['action_started'])

    def test_preflight_value_error_does_not_disclose_submitted_literal(self):
        scope = self.review_text();secret_literal = 'caller literal that must not be echoed'
        result = run_sequence(self.owner, scope, self.sid, [self.step('Entry', secret_literal)])
        self.assertNotIn(secret_literal, json.dumps(result))
        self.assertNotIn('source_observation', result)
        self.assertEqual(result['selected_control']['name'], 'Entry')

    def test_oversized_observed_label_defers_exact_name_to_inspection(self):
        control = self.control('Hide panel');control['name'] = '界'*100000
        scope = self.review_calc()
        result = run_sequence(self.owner, scope, self.sid, [self.step(control['name'])])
        self.assertLess(len(json.dumps(result).encode()), 12*1024)
        self.assertIsNone(result['selected_control']['name'])
        self.assertEqual(result['selected_control']['name_deferred']['utf8_bytes'], 300000)
        self.assertEqual(result['recovery']['arguments']['control_id'], control['id'])

    def test_preflight_error_raised_during_act_is_unknown_not_safe_refusal(self):
        scope = self.review_calc()
        error = SequencePreflightError('wrong stage', scope_id=scope, snapshot_id=self.sid,
                                       steps=[self.step('All Clear')])
        with patch.object(self.owner, '_act', side_effect=error):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['code'], 'action_outcome_unverified')
        self.assertIsNone(result['action_started']);self.assertTrue(result['no_retry'])
        self.assertEqual(result['steps_attempted'], 1)

    def test_contradictory_arithmetic_refuses_all_input_and_preserves_actual_witness(self):
        scope = self.review_calc('192*231-100')
        original_scope = deepcopy(self.owner._scopes[scope])
        before = len(self.desktop.calls)
        steps = [self.step(n) for n in ['All Clear', *'102*123-100', '=']]
        original_steps = deepcopy(steps)
        result = run_sequence(self.owner, scope, self.sid, steps)
        self.assertEqual(result['code'], 'sequence_preflight_refused')
        self.assertEqual(result['failed_step'], 13)
        proof = result['arithmetic_preflight']
        self.assertEqual(proof['proposed']['expression'], '102*123-100')
        self.assertEqual(proof['reviewed']['expression'], '192*231-100')
        self.assertEqual(proof['goal_id'], 'calc')
        self.assertTrue(proof['simulation_only']);self.assertFalse(proof['result_computed'])
        self.assertFalse(proof['actual_witness_changed'])
        self.assertEqual((result['steps_attempted'], result['steps_completed']), (0, 0))
        self.assertFalse(result['action_started']);self.assertFalse(result['arguments_rewritten'])
        self.assertFalse(self.desktop.executions);self.assertEqual(len(self.desktop.calls), before)
        self.assertEqual(steps, original_steps)
        actual = self.owner._scopes[scope]
        self.assertEqual(vars(actual['witness']), vars(original_scope['witness']))
        self.assertEqual({k: v for k, v in actual.items() if k != 'witness'},
                         {k: v for k, v in original_scope.items() if k != 'witness'})
        self.assertFalse(list(self.owner.out.glob('sequence-*.json')))

    def test_partial_expression_is_not_required_to_match_reviewed_prefix(self):
        scope = self.review_calc('192*231-100')
        result = self.run_steps(scope, 'All Clear', '2', '+', '3')
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertIsNone(result['arithmetic_input']['issued_evaluation'])
        self.assertFalse(result['goal_verified'])

    def test_existing_issued_prefix_is_simulated_without_mutating_it_on_refusal(self):
        scope = self.review_calc()
        prefix = self.run_steps(scope, 'All Clear', '2', '+')
        self.sid = prefix['snapshot_id']
        before = deepcopy(vars(self.owner._scopes[scope]['witness']))
        count = len(self.desktop.executions)
        result = self.run_steps(scope, '2', '=')
        self.assertEqual(result['arithmetic_preflight']['proposed']['expression'], '2+2')
        self.assertEqual(vars(self.owner._scopes[scope]['witness']), before)
        self.assertEqual(len(self.desktop.executions), count)
        good = self.run_steps(scope, '3', '=')
        self.assertEqual(good['status'], 'sequence_completed', good)

    def test_unknown_start_or_entry_clear_is_not_a_contradictory_evaluation_proof(self):
        for names in [('2', '+', '2', '='), ('Clear Entry', '2', '+', '2', '=')]:
            scope = self.review_calc()
            result = self.run_steps(scope, *names)
            self.assertEqual(result['code'], 'arithmetic_start_unproved')
            self.assertNotIn('arithmetic_preflight', result)
            self.assertIsNone(self.owner._scopes[scope]['witness'].evaluated)
            self.sid = result['snapshot_id']
        self.assertEqual(len(self.desktop.executions), 1)  # Only entry clear.

    def test_wrong_intermediate_evaluation_is_not_erased_by_later_reset(self):
        scope = self.review_calc()
        result = self.run_steps(scope, 'All Clear', '2', '+', '2', '=',
                                'All Clear', '2', '+', '3', '=')
        self.assertEqual(result['failed_step'], 5)
        self.assertEqual(result['arithmetic_preflight']['proposed']['expression'], '2+2')
        self.assertFalse(self.desktop.executions)

    def test_reset_only_clears_a_previous_mismatching_witness_without_refusal(self):
        scope = self.review_calc()
        self.owner._scopes[scope]['witness'].record_replacement('2+2', snapshot_id=self.sid, descriptor={})
        self.owner._scopes[scope]['witness'].record('=', snapshot_id=self.sid, descriptor={})
        result = self.run_steps(scope, 'All Clear')
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(result['arithmetic_input']['issued_expression_since_clear'], '')

    def test_normalization_matches_existing_witness_semantics(self):
        scope = self.review_calc(' 2 × 3 ')
        result = self.run_steps(scope, 'All Clear', '2', '*', '3', '=')
        self.assertEqual(result['status'], 'sequence_completed', result)

    def test_ordered_multiple_goal_effects_cannot_use_any_matching_goal(self):
        goals = [{'id': key, 'kind': 'calculation', 'target': 'Result',
                  'control_id': self.control('Result')['id'], 'expression': expression,
                  'evidence_plane': 'display'} for key, expression in [('first', '2+2'), ('second', '2+3')]]
        reviewed = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Two reviewed calculations',
            'goals': goals, 'effects': [{'kind': 'goal', 'goal_id': 'first'}, {'kind': 'goal', 'goal_id': 'second'}]})
        self.assertEqual(reviewed['status'], 'approved', reviewed)
        result = self.run_steps(reviewed['scope_id'], 'All Clear', '2', '+', '3', '=')
        self.assertEqual(result['arithmetic_preflight']['goal_id'], 'first')
        self.assertEqual(result['arithmetic_preflight']['reviewed']['expression'], '2+2')
        self.assertFalse(self.desktop.executions)
        reversed_review = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Reordered effects',
            'goals': goals, 'effects': [{'kind': 'goal', 'goal_id': 'second'}, {'kind': 'goal', 'goal_id': 'first'}]})
        self.assertEqual(reversed_review['status'], 'approved', reversed_review)
        frozen = _freeze(self.owner, reversed_review['scope_id'], self.sid,
                         [self.step(n) for n in ('All Clear', '2', '+', '3', '=')])
        self.assertEqual(len(frozen[1]), 5)

    def test_numeric_navigation_effect_does_not_invent_arithmetic_evaluation(self):
        r = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Navigate then calculate',
            'goals': [{'id': 'calc', 'kind': 'calculation', 'target': 'Result',
                      'control_id': self.control('Result')['id'], 'expression': '2+3', 'evidence_plane': 'display'}],
            'effects': [{'kind': 'press', 'control_id': self.control('=')['id'], 'purpose': 'Navigate'},
                        {'kind': 'goal', 'goal_id': 'calc'}]})
        self.assertEqual(r['status'], 'approved', r)
        scope = r['scope_id']
        witness = self.owner._scopes[scope]['witness']
        witness.record_replacement('2+2', snapshot_id=self.sid, descriptor={})
        frozen = _freeze(self.owner, scope, self.sid, [self.step('=')])
        self.assertEqual(len(frozen[1]), 1)
        self.assertIsNone(witness.evaluated)
        self.assertFalse(self.owner._scopes[scope]['issued_press_effects'])
        result = self.run_steps(scope, '=', '=')
        self.assertEqual(result['failed_step'], 2)
        self.assertEqual(result['arithmetic_preflight']['proposed']['expression'], '2+2')
        self.assertFalse(self.desktop.executions)

    def test_whole_expression_replacement_uses_existing_witness_semantics(self):
        r = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Replace calculation input',
            'goals': [{'id': 'calc', 'kind': 'calculation', 'target': 'Entry',
                      'control_id': self.control('Entry')['id'], 'expression': '2+3', 'evidence_plane': 'display'}],
            'effects': [{'kind': 'goal', 'goal_id': 'calc'}]})
        self.assertEqual(r['status'], 'approved', r)
        scope = r['scope_id']
        steps = [self.step('Entry', '2+3'), self.step('=')]
        original = deepcopy(vars(self.owner._scopes[scope]['witness']))
        frozen = _freeze(self.owner, scope, self.sid, steps)
        self.assertEqual(len(frozen[1]), 2)
        self.assertEqual(vars(self.owner._scopes[scope]['witness']), original)
        wrong = run_sequence(self.owner, scope, self.sid,
            [self.step('Entry', '2+3'), self.step('2'), self.step('=')])
        self.assertEqual(wrong['arithmetic_preflight']['proposed']['expression'], '2+32')
        self.assertFalse(self.desktop.executions)

    def test_navigation_only_scope_still_accepts_observed_press(self):
        r = self.owner.call('locua_review', {'snapshot_id': self.sid, 'summary': 'Navigate only',
            'goals': [], 'effects': [{'kind': 'press', 'control_id': self.control('Hide panel')['id'], 'purpose': 'Navigate'}]})
        self.assertEqual(r['status'], 'approved', r)
        result = self.run_steps(r['scope_id'], 'Hide panel')
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(len(self.desktop.executions), 1)

    def test_oversized_prior_expression_is_deferred_in_mismatch_evidence(self):
        scope = self.review_calc()
        witness = self.owner._scopes[scope]['witness']
        witness.record_replacement('2'*100000, snapshot_id=self.sid, descriptor={})
        before = deepcopy(vars(witness))
        result = self.run_steps(scope, '=')
        proposed = result['arithmetic_preflight']['proposed']
        self.assertIsNone(proposed['expression']);self.assertTrue(proposed['deferred'])
        self.assertEqual(proposed['utf8_bytes'], 100000)
        self.assertLess(len(json.dumps(result).encode()), 12*1024)
        self.assertEqual(vars(witness), before)

    def test_text_effect_exact_and_sequence_not_goal_completion(self):
        scope = self.review_text(preserve=True)
        result = run_sequence(self.owner, scope, self.sid, [self.step('Entry', 'exact')])
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(self.desktop.value, 'exact');self.assertTrue(self.desktop.checked)
        self.assertFalse(result['goal_verified'])

    def test_frozen_source_survives_caller_mutation(self):
        scope = self.review_calc();steps = [self.step('All Clear'), self.step('2')]
        execute = self.desktop.execute
        def mutate(action, o):
            steps[1]['action_id'] = self.step('Hide panel')['action_id']
            return execute(action, o)
        with patch.object(self.desktop, 'execute', side_effect=mutate):
            result = run_sequence(self.owner, scope, self.sid, steps)
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(result['arithmetic_input']['issued_expression_since_clear'], '2')
        source = json.loads(Path(result['source_evidence_ref']).read_text())
        self.assertNotEqual(source['steps'][1]['source_action_id'], steps[1]['action_id'])

    def test_same_original_id_can_repeat_if_unchanged(self):
        scope = self.review_calc('22')
        result = self.run_steps(scope, 'All Clear', '2', '2')
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(result['arithmetic_input']['issued_expression_since_clear'], '22')
        self.assertNotEqual(result['receipts'][1]['action_id'], result['receipts'][2]['action_id'])

    def test_retained_review_age_does_not_grant_or_prevent_fresh_dispatch(self):
        o = self.owner._observations[self.sid]
        o['observed_at_ns'] -= 600 * 1_000_000_000
        o['provenance']['observed_at_ns'] = o['observed_at_ns']
        scope = self.review_calc()
        result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'sequence_completed', result)
        first = self.records(result)[0]
        self.assertGreater(first['pre_dispatch_observed_at_ns'], o['observed_at_ns'])
        self.assertEqual(json.loads(Path(result['source_evidence_ref']).read_text())[
            'source_observation']['observed_at_ns'], o['observed_at_ns'])

    def test_changed_review_contract_stops_remaining_inputs(self):
        scope = self.review_calc();execute = self.desktop.execute
        def change_contract(action, o):
            r = execute(action, o)
            self.owner._scopes[scope]['goals'][0]['expression'] = '7+8'
            return r
        with patch.object(self.desktop, 'execute', side_effect=change_contract):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['code'], 'sequence_source_changed')
        self.assertEqual(len(self.desktop.executions), 1)

    def test_uncertain_first_step_stops_and_keeps_target_latched(self):
        scope = self.review_calc();self.desktop.uncertain = True
        result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertEqual((result['steps_attempted'], result['steps_completed']), (1, 0))
        self.assertTrue(result['no_retry']);self.assertEqual(len(self.desktop.executions), 1)
        self.assertTrue(self.owner._scopes[scope]['uncertain_action'])
        self.assertEqual(self.records(result)[0]['result']['raw_action_result']['reason'], 'Synthetic timeout')

    def test_known_no_action_refusal_stops_without_inventing_input(self):
        scope = self.review_calc()
        result = self.run_steps(scope, '2', '+')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['code'], 'arithmetic_start_unproved')
        self.assertFalse(result['action_started']);self.assertFalse(result['no_retry'])
        self.assertFalse(self.desktop.executions)

    def test_unavailable_predispatch_capture_stops_zero_inputs(self):
        scope = self.review_calc();self.desktop.unavailable = True
        result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertEqual(result['steps_attempted'], 1);self.assertFalse(self.desktop.executions)

    def test_unclassified_act_exception_latches_unknown_and_no_retry(self):
        scope = self.review_calc()
        with patch.object(self.owner, '_act', side_effect=RuntimeError('uncertain boundary')):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertIsNone(result['action_started']);self.assertTrue(result['no_retry'])
        self.assertEqual(result['steps_attempted'], 1)
        self.assertFalse(self.owner._scopes[scope]['uncertain_action']['eligible_for_read_only_reconciliation'])

    def test_preservation_failure_stops_no_later_step(self):
        scope = self.review_text(preserve=True);self.desktop.checked = False
        result = run_sequence(self.owner, scope, self.sid, [self.step('Entry', 'exact')])
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse(self.desktop.executions)
        self.assertEqual(self.owner._scopes[scope]['status'], 'blocked_preservation_unknown')

    def test_fresh_dialog_before_dispatch_refuses_zero_input(self):
        scope = self.review_calc();observe = self.desktop.observe
        def dialog(target):
            r = observe(target);r['observation']['controls'][0]['name'] = 'Different window state';return r
        with patch.object(self.desktop, 'observe', side_effect=dialog):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['code'], 'sequence_fresh_state_changed')
        self.assertFalse(self.desktop.executions)
        self.assertFalse(self.records(result)[0]['pre_dispatch_transition']['matched'])

    def test_structure_change_after_success_stops_before_next_input(self):
        scope = self.review_calc();execute = self.desktop.execute
        def navigation(action, o):
            r = execute(action, o);r['observation']['controls'][0]['name'] = 'Unexpected dialog';return r
        with patch.object(self.desktop, 'execute', side_effect=navigation):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['code'], 'sequence_state_changed')
        self.assertEqual((result['steps_attempted'], result['steps_completed']), (1, 1))
        self.assertEqual(len(self.desktop.executions), 1);self.assertTrue(result['do_not_repeat_sequence'])

    def test_future_target_drift_stops_after_current_input(self):
        scope = self.review_calc();execute = self.desktop.execute
        def change(action, o):
            r = execute(action, o)
            next(c for c in r['observation']['controls'] if c['name'] == '2')['states']['enabled'] = False
            return r
        with patch.object(self.desktop, 'execute', side_effect=change):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(len(self.desktop.executions), 1)

    def test_duplicate_target_is_refused_before_input(self):
        scope = self.review_calc();o = self.owner._observations[self.sid]
        duplicate = deepcopy(self.control('2'));duplicate['id'] += ':twin';o['controls'].append(duplicate)
        result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'refused', result)
        self.assertFalse(self.desktop.executions)

    def test_cancel_between_steps_preserves_prefix_and_stops(self):
        scope = self.review_calc();execute = self.desktop.execute
        def cancel(action, o):
            r = execute(action, o)
            self.owner._cancellation = {'status': 'canceled', 'reason': 'user_declined_review'}
            return r
        with patch.object(self.desktop, 'execute', side_effect=cancel):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['status'], 'canceled', result)
        self.assertEqual(result['reason'], 'user_declined_review')
        self.assertEqual((result['steps_attempted'], result['steps_completed']), (1, 1))
        self.assertEqual(len(self.desktop.executions), 1)

    def test_existing_cancellation_does_not_read_or_create_files(self):
        scope = self.review_calc();n = len(self.desktop.calls)
        self.owner._cancellation = {'status': 'canceled', 'reason': 'user_declined_review'}
        result = self.run_steps(scope, 'All Clear', '2')
        self.assertEqual(result['steps_attempted'], 0);self.assertEqual(len(self.desktop.calls), n)
        self.assertFalse(list(self.owner.out.glob('sequence-*.json')))

    def test_keyboardinterrupt_preserves_receipt_latch_and_propagates(self):
        scope = self.review_calc()
        with patch.object(self.owner, '_act', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_steps(scope, 'All Clear', '2')
        files = list(self.owner.out.glob('sequence-*-step-001.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())['exception']['type'], 'KeyboardInterrupt')
        self.assertEqual(self.owner._scopes[scope]['status'], 'blocked_uncertain')

    def test_32_steps_bounded_public_receipts_full_private_records(self):
        scope = self.review_calc('2'*31)
        result = self.run_steps(scope, 'All Clear', *(['2']*31))
        self.assertEqual(result['status'], 'sequence_completed', result)
        self.assertEqual(result['steps_completed'], 32)
        self.assertEqual(len(result['receipts']), MAX_PUBLIC_RECEIPTS)
        self.assertEqual(result['receipts_omitted'], 32-MAX_PUBLIC_RECEIPTS)
        self.assertEqual(len(list(self.owner.out.glob('sequence-*-step-*.json'))), 32)
        self.assertLess(len(json.dumps(result).encode()), 12*1024)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.owner.out.glob('sequence-*.json')))

    def test_long_error_private_exact_public_bounded(self):
        scope = self.review_calc();message = '界'*100000
        with patch.object(self.owner, '_act', side_effect=RuntimeError(message)):
            result = self.run_steps(scope, 'All Clear', '2')
        self.assertLess(len(json.dumps(result).encode()), 12*1024)
        self.assertTrue(result['no_retry'])
        self.assertEqual(self.records(result)[0]['exception']['message'], message)


if __name__ == '__main__':
    unittest.main()
