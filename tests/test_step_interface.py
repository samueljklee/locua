"""Typed choices over real owner guards; deterministic CPU desktop fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_contracts import _errors
from locua.amplifier_tools import DesktopToolset
from locua.step_interface import DECISION, SPECS, StepModelInterface
from tests.test_amplifier_tools import Desktop
from tests.test_semantic_projection import ContextDesktop


class StepInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.reviews = []; self.desktop = Desktop()
        self.owner, self.ui = self.make(self.desktop)
        self.app, self.window, self.view, self.rows = self.discover(self.ui)

    def make(self, desktop):
        def ask(prompt, purpose): self.reviews.append((prompt, purpose)); return 'run'
        owner = DesktopToolset({}, Path(self.tmp.name)/str(len(list(Path(self.tmp.name).iterdir()))),
            'Replace Entry with exact λ. Preserve Competitor. No backing-file requirement.', ask,
            desktop=desktop, tool_profile='semantic-v1', persistence_contract='text-persistence-v1')
        owner.model_interface = StepModelInterface(owner)
        self.addCleanup(owner.close)
        return owner, owner.model_interface

    def discover(self, ui):
        app = ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
        window = ui.call('locua_inspect', {'reference': app})['windows'][0]['window_id']
        view = ui.call('locua_inspect', {'reference': window})['view']
        rows = self.collect(ui, {'reference': view}, search=True)
        return app, window, view, rows

    def collect(self, ui, args, *, search=False):
        rows = []
        for n in range(100):
            result = ui.call('locua_search' if search and n == 0 else 'locua_inspect', args)
            self.assertEqual(result['status'], 'ok', result)
            rows.extend(result['items'])
            args = result.get('coverage', {}).get('continue_with')
            if not args: return rows
        self.fail('Pagination did not end')

    def row(self, name): return next(row for row in self.rows if row.get('name') == name)
    def outcome(self, name, kind):
        return self.row(name)['outcomes'][kind]
    def input(self, name): return next(iter(self.row(name)['inputs'].values()))

    def review(self, **changes):
        args = {'summary': 'Replace Entry with exact λ; preserve Competitor; no backing-file requirement.',
                'goals': [{'outcome': self.outcome('Entry', 'text'), 'value': 'exact λ',
                           'persistence_requirement': 'not_requested'}],
                'preserves': [self.outcome('Competitor', 'text')], 'covers_request': True}
        args.update(changes)
        return self.ui.call('locua_review', args)

    def test_actual_review_edit_fresh_verify_and_exact_preserve(self):
        reviewed = self.review(); self.assertEqual(reviewed['status'], 'approved', reviewed)
        changed = self.ui.call('locua_act', {'input': self.input('Entry'), 'value': 'exact λ'})
        self.assertEqual(changed['status'], 'verified', changed)
        before = self.desktop.sequence
        proof = self.ui.call('locua_verify', {})
        self.assertEqual(proof['status'], 'verified', proof)
        self.assertGreater(self.desktop.sequence, before)
        self.assertEqual((self.desktop.value, self.desktop.other), ('exact λ', 'protected'))
        self.assertEqual(len(self.desktop.executions), 1)
        self.assertEqual(len(self.reviews), 1)

    def test_apps_empty_inventory_argument_is_not_published_or_accepted(self):
        self.assertEqual(set(SPECS['locua_apps'][1]['properties']), {'query'})
        result = self.ui.call('locua_apps', {'query': '', 'inventory_id': ''})
        self.assertEqual(result['code'], 'argument_contract_invalid')

    def test_application_pages_use_one_typed_continuation(self):
        result = self.ui.call('locua_apps', {'query': ''}); apps = result['items'][:]
        self.assertNotIn('inventory_id', result)
        while result.get('continue_with'):
            args = result['continue_with']; self.assertEqual(set(args), {'reference'})
            result = self.ui.call('locua_inspect', args); apps.extend(result['items'])
        self.assertEqual(len(apps), 71)
        self.assertEqual(len({row['app_id'] for row in apps}), 71)

    def test_all_reference_routes_and_navigation_are_explicit(self):
        before = len(self.desktop.calls)
        page = self.ui.call('locua_inspect', {'reference': self.view})
        self.assertEqual(len(self.desktop.calls), before)
        region = next(row['region'] for row in page['items'] if 'region' in row)
        listed = self.ui.call('locua_inspect', {'reference': region})
        self.assertEqual(listed['status'], 'ok')
        detail = self.ui.call('locua_inspect', {'reference': self.row('Entry')['target']})
        self.assertEqual(detail['items'][0]['target'], self.row('Entry')['target'])
        self.assertEqual(self.ui.state()['focus']['selected_reference'], self.row('Entry')['target'])
        self.assertEqual(self.desktop.executions, [])
        self.assertEqual(self.ui.call('locua_launch', {'app': self.app})['status'], 'launched')
        self.assertEqual(self.ui.call('locua_activate', {'window': self.window})['status'], 'activated')

    def test_typed_refs_cannot_substitute_for_each_other(self):
        result = self.ui.call('locua_act', {'input': self.outcome('Entry', 'text'), 'value': 'exact λ'})
        self.assertEqual(result['code'], 'argument_contract_invalid')
        self.assertIn('$.input', result['reason'])
        result = self.review(goals=[{'outcome': self.input('Entry'), 'value': 'exact λ',
                                    'persistence_requirement': 'not_requested'}])
        self.assertEqual(result['code'], 'argument_contract_invalid')
        self.assertIn('$.goals[0].outcome', result['reason'])
        self.assertEqual(self.desktop.executions, [])

    def test_captured_27b_review_key_and_prose_errors_are_actionable_without_repair(self):
        # Exact returned review from frozen phase002, not a model answer fixture.
        args = {
            'summary': "In Layout Lab's calculator, clear the current calculation, enter the expression 192*231-100 using the keypad, and evaluate it with Equals. Expected result: 43352.",
            'goals': [
                {'outcome': 'calculation', 'value': '192*231-100', 'persistence_requirement': 'not_requested'},
                {'outcome': 'value', 'value': '43352', 'persistence_requirement': 'not_requested'}],
            'navigation': ['Press All Clear (i2) to reset the calculator',
                           'Press 1 (i4), 9 (i12), 2 (i5) to enter 192',
                           'Press × (i15) for multiplication',
                           'Press 2 (i5), 3 (i6), 1 (i4) to enter 231',
                           'Press − (i14) for subtraction',
                           'Press 1 (i4), 0 (i3), 0 (i3) to enter 100',
                           'Press Equals (i1) to evaluate'],
            'covers_request': True,
            'decision': {
                'plan': ['Clear calculator with All Clear', 'Enter 192', 'Press multiply',
                         'Enter 231', 'Press minus', 'Enter 100', 'Press Equals',
                         'Verify readout shows 43352'],
                'current_step': 'Request review for the full calculation sequence',
                'constraints': ['Must use the calculator keypad, not type a precomputed answer',
                                'Preserve operation order: 192*231-100'], 'unresolved': []}}
        before = deepcopy(args)
        result = self.ui.call('locua_review', args)
        self.assertEqual(result['code'], 'argument_contract_invalid', result)
        self.assertIn('$.goals[0].outcome', result['reason'])
        self.assertIn('control.outcomes', result['reason'])
        self.assertIn('reference VALUE', result['reason'])
        self.assertIn('$.navigation[0]', result['reason'])
        self.assertIn('control.inputs.press', result['reason'])
        self.assertEqual(args, before)
        self.assertEqual(self.owner._scopes, {})
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_captured_7b_malformed_review_still_latches_persistence(self):
        # Actual second malformed contract: do not discard its unsupported
        # declaration merely because the new reference pattern can reject first.
        result = self.ui.call('locua_review', {
            'summary': 'Use the calculator in Layout Lab to compute 192*231-100.',
            'goals': [{'outcome': 'calculation', 'value': '192*231-100',
                       'persistence_requirement': 'backing_file_unchanged'}],
            'preserves': [], 'navigation': ['w1', 'r4'], 'covers_request': True})
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertTrue(result['execution_stopped'])
        self.assertEqual(self.review()['code'], 'text_persistence_unmet')
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_reference_shape_never_grants_issued_identity(self):
        result = self.review(goals=[{'outcome': 'o99999', 'value': 'exact λ',
                                    'persistence_requirement': 'not_requested'}])
        self.assertEqual(result['code'], 'unknown_reference', result)
        self.assertIn('$.goals[0].outcome', result['reason'])
        self.assertIn('not issued', result['reason'])
        result = self.ui.call('locua_act', {'input': 'i99999'})
        self.assertEqual(result['code'], 'unknown_reference', result)
        self.assertIn('$.input', result['reason'])
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_preserve_and_sequence_field_errors_do_not_extract_embedded_refs(self):
        result = self.review(preserves=['Keep '+self.outcome('Competitor', 'text')])
        self.assertEqual(result['code'], 'argument_contract_invalid', result)
        self.assertIn('$.preserves[0]', result['reason'])
        self.assertIn('actual observed property', result['reason'])
        result = self.ui.call('locua_act_sequence', {'inputs': ['Press '+self.input('2')]})
        self.assertEqual(result['code'], 'argument_contract_invalid', result)
        self.assertIn('$.inputs[0]', result['reason'])
        self.assertIn('bare issued i', result['reason'])
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_calculation_persistence_and_display_only_goal_feedback(self):
        result = self.review(goals=[{'outcome': self.outcome('Result', 'calculation'),
                                    'value': '2+3', 'persistence_requirement': 'not_requested'}], preserves=[])
        self.assertEqual(result['code'], 'persistence_requires_text', result)
        self.assertIn('$.goals[0].persistence_requirement', result['reason'])
        self.assertIn('Omit', result['reason'])
        result = self.review(goals=[{'outcome': self.outcome('Result', 'value'), 'value': '5'}], preserves=[])
        self.assertEqual(result['code'], 'preservation_only', result)
        self.assertIn('preserves[]', result['reason'])
        self.assertIn('original requested expression', result['reason'])
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_published_reference_guidance_and_shape_match_valid_runtime_fields(self):
        schema = SPECS['locua_review'][1]
        goal = schema['properties']['goals']['items']['properties']
        self.assertIn('reference VALUE', goal['outcome']['description'])
        self.assertIn('original expression', goal['value']['description'])
        self.assertIn('omit for calculation or state', goal['persistence_requirement']['description'])
        self.assertIn('never invent', goal['persistence_requirement']['description'])
        self.assertTrue(_errors(goal['outcome'], 'calculation'))
        self.assertFalse(_errors(goal['outcome'], self.outcome('Result', 'calculation')))
        self.assertTrue(_errors(schema['properties']['navigation']['items'], 'Press i2'))
        self.assertFalse(_errors(schema['properties']['navigation']['items'], self.input('2')))
        self.assertEqual(self.review()['status'], 'approved')

    def test_controls_remain_visible_without_outcomes_or_inputs(self):
        root = next(row for row in self.rows if row['role'] == 'AXWindow')
        self.assertEqual(root['outcomes'], {}); self.assertEqual(root['inputs'], {})
        button = self.row('2')
        self.assertEqual(set(button['outcomes']), {'selected_unknown', 'checked_unknown'})
        self.assertEqual(set(button['inputs']), {'press'})
        readout = self.row('Result')
        self.assertEqual(readout['inputs'], {})
        self.assertIn('calculation', readout['outcomes'])

    def test_navigation_before_final_outcomes_bind_is_supported(self):
        result = self.review(goals=[], preserves=[], navigation=[self.input('Hide panel')],
                             summary='Open the requested panel to inspect it.', covers_request=False,
                             unresolved=['Final requested outcome not yet observed'])
        self.assertEqual(result['status'], 'approved', result)
        action = self.ui.call('locua_act', {'input': self.input('Hide panel')})
        self.assertEqual(action['status'], 'dispatched', action)
        self.assertFalse(self.owner.finalize()['task_complete'])

    def test_calculation_expression_and_observed_presses_remain_model_choices(self):
        # Sequence preflight requires real-shaped native addresses; the basic
        # fixture deliberately has none. Retain the same synthetic semantics.
        original = self.desktop.observe
        def observe(target):
            result = original(target); o = result['observation']; sid = o['snapshot_id']
            ids = {c['id']: 'native:'+c['id'] for c in o['controls']}
            for index, c in enumerate(o['controls']):
                c['id'] = ids[c['id']]; c['parent'] = ids.get(c['parent'])
                token = sid+':'+str(index)
                c['source'] = {'kind': 'native', 'node': {'role': c['role'], 'element_index': index, 'element_token': token}}
                o['handles'][c['id']] = {'kind': 'native', **target, 'snapshot_id': sid,
                                       'element_index': index, 'element_token': token}
            return result
        self.desktop.observe = observe
        self.view = self.ui.call('locua_inspect', {'reference': self.window})['view']
        self.rows = self.collect(self.ui, {'reference': self.view}, search=True)
        result = self.review(goals=[{'outcome': self.outcome('Result', 'calculation'), 'value': '2+3'}], preserves=[])
        self.assertEqual(result['status'], 'approved', result)
        scope = next(iter(self.owner._scopes.values()))
        self.assertEqual(scope['goals'][0]['expression'], '2+3')
        result = self.ui.call('locua_act_sequence', {'inputs': [self.input(n) for n in ('All Clear', '2', '+', '3', '=')]})
        self.assertEqual(result['status'], 'sequence_completed', result)
        proof = self.ui.call('locua_verify', {})
        self.assertEqual(proof['status'], 'verified', proof)
        self.assertEqual(len(self.desktop.executions), 5)

    def test_sequence_rejects_editor_and_press_rejects_value(self):
        result = self.ui.call('locua_act_sequence', {'inputs': [self.input('Entry')]})
        self.assertEqual(result['code'], 'sequence_requires_press')
        result = self.ui.call('locua_act', {'input': self.input('Keep'), 'value': ''})
        self.assertEqual(result['code'], 'press_has_value')
        self.assertEqual(self.desktop.executions, [])

    def test_persistence_latches_before_invalid_reference_binding(self):
        result = self.review(goals=[{'outcome': 'o999', 'value': 'exact λ',
                                    'persistence_requirement': 'backing_file_unchanged'}])
        self.assertEqual(result['code'], 'text_persistence_unmet')
        self.assertTrue(result['execution_stopped'])
        self.assertEqual(self.review()['code'], 'text_persistence_unmet')
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_text_persistence_omission_is_not_defaulted(self):
        result = self.review(goals=[{'outcome': self.outcome('Entry', 'text'), 'value': 'exact λ'}])
        self.assertEqual(result['code'], 'argument_contract_invalid', result)
        self.assertEqual(self.reviews, []); self.assertEqual(self.desktop.executions, [])

    def test_changed_preserve_and_missing_approval_still_block(self):
        missing = self.ui.call('locua_act', {'input': self.input('Entry'), 'value': 'exact λ'})
        self.assertEqual(missing['code'], 'approval_required')
        self.assertEqual(self.review()['status'], 'approved')
        self.desktop.other = 'changed'
        result = self.ui.call('locua_act', {'input': self.input('Entry'), 'value': 'exact λ'})
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('preservation', result['reason'].lower())
        self.assertEqual(self.desktop.executions, [])

    def test_old_typed_refs_never_follow_new_snapshot(self):
        original = self.input('Entry')
        self.ui.call('locua_inspect', {'reference': self.window})
        result = self.ui.call('locua_act', {'input': original, 'value': 'exact λ'})
        self.assertEqual(result['code'], 'stale_view')
        self.assertEqual(self.desktop.executions, [])

    def test_same_named_competitors_keep_actual_semantics_and_no_inferred_section(self):
        owner, ui = self.make(ContextDesktop()); _, _, view, _ = self.discover(ui)
        page = ui.call('locua_search', {'reference': view, 'query': 'Muted'})
        rows = page['items']; self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['identifier'], rows[1]['identifier'])
        self.assertNotEqual(rows[0]['target'], rows[1]['target'])
        detail = ui.call('locua_inspect', {'reference': rows[0]['target']})
        self.assertFalse(detail['neighborhood']['section_membership_proven'])
        self.assertFalse(detail['neighborhood']['association_inferred'])

    def test_unknown_state_is_not_a_false_boolean_or_preservable(self):
        d = Desktop(); original = d.observe
        def observe(target):
            result = original(target)
            next(c for c in result['observation']['controls'] if c['name'] == 'Hide panel')['states']['selected'] = None
            return result
        d.observe = observe
        _, ui = self.make(d); _, _, _, rows = self.discover(ui)
        row = next(r for r in rows if r.get('name') == 'Hide panel')
        state = row['outcomes']['selected_unknown']
        self.assertNotIn('selected', row['outcomes'])
        self.assertIsNone(row['states']['selected'])
        blocked = ui.call('locua_review', {'summary': 'Keep the unknown state', 'goals': [],
            'preserves': [state], 'navigation': [row['inputs']['press']], 'covers_request': False})
        self.assertEqual(blocked['code'], 'preservation_unknown')
        result = ui.call('locua_review', {'summary': 'Select the requested control',
            'goals': [{'outcome': state, 'value': True}], 'covers_request': True})
        self.assertEqual(result['status'], 'approved', result)
        result = ui.call('locua_act', {'input': row['inputs']['press']})
        self.assertEqual(result['status'], 'dispatched', result)
        self.assertEqual(len(d.executions), 1)
        self.assertNotEqual(ui.call('locua_verify', {})['status'], 'verified')

    def test_enabled_only_button_can_review_unknown_selected_via_one_time_press(self):
        row = self.row('Hide panel')
        self.assertEqual(row['states'], {'enabled': True})
        cap = row['outcomes']['selected_unknown']
        self.assertNotIn('selected', row['outcomes'])
        result = self.review(goals=[{'outcome': cap, 'value': True}], preserves=[])
        self.assertEqual(result['status'], 'approved', result)
        scope = next(iter(self.owner._scopes.values()))
        self.assertEqual(scope['effects'][0]['kind'], 'press')
        result = self.ui.call('locua_act', {'input': row['inputs']['press']})
        self.assertEqual(result['status'], 'dispatched', result)
        self.assertNotEqual(self.ui.call('locua_verify', {})['status'], 'verified')
        self.assertEqual(len(self.desktop.executions), 1)

    def test_decision_is_bounded_optional_and_cannot_create_authority(self):
        self.assertIsNone(self.ui.state()['model_hypotheses']['decision'])
        decision = {'plan': ['Completed requested change'], 'current_step': 'Done',
                    'constraints': ['No constraints'], 'unresolved': []}
        result = self.ui.call('locua_status', {'decision': decision})
        self.assertEqual(result['model_hypotheses']['decision'], decision)
        self.assertFalse(result['model_hypotheses']['authority'])
        self.assertEqual(result['original_request'], self.owner.request)
        self.assertFalse(self.owner.finalize()['task_complete'])
        bad = deepcopy(decision); bad['current_step'] = ' '*3
        self.assertTrue(_errors(DECISION, bad))
        self.assertNotIn('decision', SPECS['locua_act'][1]['properties'])

    def test_model_plan_revision_does_not_reset_nonprogress(self):
        for n in range(3):
            result = self.ui.call('locua_status', {'decision': {'plan': [str(n)], 'current_step': str(n),
                                                              'constraints': [], 'unresolved': []}})
        self.assertTrue(result['execution_stopped'])
        self.assertEqual(self.owner._cancellation['reason'], 'nonprogress_limit')

    def test_revised_model_claims_cannot_silently_erase_prior_constraints(self):
        first = {'plan': ['Inspect'], 'current_step': 'Inspect',
                 'constraints': ['Keep competing value unchanged'], 'unresolved': ['Exact target is unknown']}
        self.ui.call('locua_status', {'decision': first})
        second = {'plan': ['Done'], 'current_step': 'Done', 'constraints': [], 'unresolved': []}
        result = self.ui.call('locua_status', {'decision': second})
        self.assertEqual(result['model_hypotheses']['previous_declarations'],
                         {key: first[key] for key in ('constraints', 'unresolved')})
        self.assertFalse(result['model_hypotheses']['authority'])
        self.assertEqual(result['original_request'], self.owner.request)
        self.assertFalse(self.owner.finalize()['task_complete'])

    def test_oversized_model_claims_are_rejected_without_truncation(self):
        decision = {'plan': ['x'*600]*12, 'current_step': 'Inspect', 'constraints': [], 'unresolved': []}
        result = self.ui.call('locua_status', {'decision': decision})
        self.assertEqual(result['code'], 'model_claim_budget')
        self.assertIsNone(self.ui._model_decision)

    def test_typed_aliases_do_not_make_recapture_semantic_progress(self):
        for _ in range(3):
            result = self.ui.call('locua_inspect', {'reference': self.window})
            if result.get('execution_stopped'): break
        self.assertTrue(result['execution_stopped'], result)
        self.assertEqual(self.desktop.executions, [])

    def test_focus_and_readable_notes_are_nonexhaustive_stale_and_deduplicated(self):
        self.ui.call('locua_inspect', {'reference': self.row('Entry')['target']})
        state = self.ui.state()
        self.assertTrue(state['focus']['potentially_stale'])
        focused = {r.get('target') for r in state['focus']['items']}
        self.assertFalse(any(r['target'] in focused for r in state['readable_evidence']['items']))
        self.assertFalse(state['readable_evidence']['complete_history'])
        self.assertNotIn('explored', state)
        self.assertIn('explored_routes', state)

    def test_large_literal_detail_paging_is_lossless_with_public_routes(self):
        d = Desktop(); d.value = 'λ "quoted"\nno terminal newline' * 150
        _, ui = self.make(d); _, _, view, rows = self.discover(ui)
        entry = next(r for r in rows if r.get('name') == 'Entry')
        self.assertEqual(entry['value']['next'], {'reference': entry['target']})
        items = self.collect(ui, {'reference': entry['target']})
        parts = [r for r in items if r.get('kind') == 'json_fragment' and r.get('field') == 'value']
        self.assertEqual(json.loads(''.join(r['text'] for r in parts)), d.value)

    def test_post_input_artifact_failure_is_uncertain_never_safe_replay(self):
        self.assertEqual(self.review()['status'], 'approved')
        with patch('locua.step_interface.private_json', side_effect=OSError('Synthetic artifact failure')):
            result = self.ui.call('locua_act', {'input': self.input('Entry'), 'value': 'exact λ'})
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertTrue(result['action_started']); self.assertTrue(result['no_retry'])
        self.assertEqual(len(self.desktop.executions), 1)
        again = self.ui.call('locua_act', {'input': self.input('Entry'), 'value': 'exact λ'})
        self.assertTrue(again['execution_stopped']); self.assertEqual(len(self.desktop.executions), 1)

if __name__ == '__main__': unittest.main()
