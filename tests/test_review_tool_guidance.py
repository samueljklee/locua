"""Published review guidance and refusal recovery; no desktop/model IO."""
from copy import deepcopy
import json
import unittest

import test_amplifier_tools as fixtures
from locua.amplifier_contracts import SPECS, validate_tool_arguments


class ReviewGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tools = self.fixture.tools
        self.call = self.fixture.call
        self.sid = self.fixture.sid

    def calculation(self, control_id):
        return {'snapshot_id': self.sid, 'summary': 'Calculate the requested expression',
            'goals': [{'id': 'calc', 'kind': 'calculation', 'target': 'Observed result',
                       'control_id': control_id, 'expression': '2+3', 'evidence_plane': 'display'}],
            'effects': [{'kind': 'goal', 'goal_id': 'calc'}]}

    def age_capture(self):
        o = self.tools._observations[self.sid]
        o['observed_at_ns'] -= 180_000_000_000
        o['provenance']['observed_at_ns'] = o['observed_at_ns']
        return o

    def test_published_contract_is_canonical_and_explains_retained_review(self):
        tools = {tool.name: tool for tool in self.tools.tools()}
        for name, tool in tools.items():
            self.assertEqual(tool.description, SPECS[name][0])
            self.assertEqual(tool.input_schema, SPECS[name][1])
        for name in ('locua_inspect', 'locua_review', 'locua_observe'):
            self.assertIn('retained', tools[name].description)
        review = tools['locua_review']
        self.assertIn('do not recapture just to review', review.description)
        self.assertIn('not a window/container', review.description)
        self.assertIn('no need to enumerate buttons before review', review.description)
        self.assertIn('navigation', review.description)
        self.assertIn('act checks fresh state', review.input_schema['properties']['snapshot_id']['description'])

    def test_wrong_readout_role_returns_readonly_options_without_target_replacement(self):
        original_observation = deepcopy(self.age_capture())
        args = self.calculation(self.sid+':0')  # Observed AXWindow, not a result.
        original_args = deepcopy(args)
        calls = deepcopy(self.fixture.desktop.calls)
        result = self.call('review', **args)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['code'], 'calculation_requires_readable_display')
        self.assertEqual(result['selected_control'], {'id': self.sid+':0', 'role': 'AXWindow'})
        self.assertEqual(result['snapshot_id'], self.sid)
        self.assertEqual(result['target'], original_observation['target'])
        self.assertFalse(result['arguments_rewritten'])
        self.assertFalse(result['action_started'])
        self.assertFalse(result['current_state_proven'])
        self.assertFalse(result['action_authority'])
        self.assertFalse(result['task_complete'])
        self.assertFalse(self.tools._scopes)
        self.assertFalse(self.fixture.reviews)
        self.assertFalse(self.fixture.desktop.executions)
        self.assertEqual(self.fixture.desktop.calls, calls)
        self.assertEqual(self.tools._observations[self.sid], original_observation)
        self.assertEqual(args, original_args)
        for option in result['inspect_options']:
            self.assertEqual(option['tool'], 'locua_inspect')
            validate_tool_arguments(option['tool'], option['arguments'])
            self.assertEqual(option['arguments']['snapshot_id'], self.sid)
            self.assertNotIn('query', option['arguments'])
            self.assertNotIn('control_id', option['arguments'])
        listing = result['inspect_options'][1]
        self.assertIn('AXStaticText', listing['optional_role_filters'])
        self.assertNotIn('AXWindow', listing['optional_role_filters'])
        for role in listing['optional_role_filters']:
            validate_tool_arguments('locua_inspect', {**listing['arguments'], 'role': role})
        self.assertNotIn('2+3', json.dumps(result['inspect_options']))

    def test_explicit_result_choice_can_review_same_old_capture_without_button_recipe(self):
        self.age_capture()
        refused = self.call('review', **self.calculation(self.sid+':0'))
        self.assertEqual(refused['status'], 'refused')
        calls = deepcopy(self.fixture.desktop.calls)
        approved = self.call('review', **self.calculation(self.sid+':4'))
        self.assertEqual(approved['status'], 'approved', approved)
        self.assertGreater(approved['review_capture']['age_seconds'], 170)
        self.assertFalse(approved['review_capture']['current_state_proven'])
        self.assertEqual(approved['effects'], [{'kind': 'goal', 'goal_id': 'calc'}])
        self.assertEqual(self.fixture.desktop.calls, calls)
        self.assertFalse(self.fixture.desktop.executions)
        self.assertEqual(self.fixture.desktop.display, '0')  # No expected-answer target selection.

    def test_retained_review_does_not_relax_fresh_action_signature(self):
        self.age_capture()
        scope = self.fixture.review_text()
        self.fixture.desktop.value = 'changed after retained capture'
        result = self.fixture.act(scope, 'Entry', 'exact')
        self.assertEqual(result['status'], 'refused', result)
        self.assertFalse(self.fixture.desktop.executions)

    def test_other_binding_errors_are_not_relabelled_as_wrong_result_role(self):
        args = self.calculation(self.sid+':4')
        args['goals'][0]['expression'] = 'unknown_variable + 3'
        result = self.call('review', **args)
        self.assertEqual(result['status'], 'refused')
        self.assertNotIn('inspect_options', result)
        self.assertNotEqual(result.get('code'), 'calculation_requires_readable_display')
        self.assertFalse(self.fixture.reviews)
        self.assertFalse(self.fixture.desktop.executions)


if __name__ == '__main__':
    unittest.main()
