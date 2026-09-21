"""Historical review is not fresh evidence or a bypass of dispatch guards."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch

from locua.goal_verification import (
    bind, bind_for_review, check_review_predicate, matches_binding, verify,
)
from test_goal_verification import observation, editor, control, outcome
import test_amplifier_tools as tool_fixture


def age(observed, seconds=180):
    stamp = time.time_ns() - seconds * 1_000_000_000
    observed['observed_at_ns'] = stamp
    observed['provenance']['observed_at_ns'] = stamp
    return observed


class RetainedBindingTests(unittest.TestCase):
    def test_old_binding_preserves_capture_and_does_not_verify_old_evidence(self):
        goal = outcome('text', evidence_plane='editor_buffer')
        observed = age(observation(editor()))
        original = deepcopy(observed)
        with self.assertRaises(ValueError):
            bind(goal, observed['controls'][1], observed)
        binding = bind_for_review(goal, observed['controls'][1], observed)
        self.assertEqual(observed, original)
        self.assertEqual(binding['bound_at_ns'], observed['observed_at_ns'])
        self.assertEqual(binding['initial_snapshot_id'], observed['snapshot_id'])
        self.assertFalse(binding['review_capture']['current_state_proven'])
        self.assertFalse(binding['review_capture']['action_authority_granted'])
        self.assertFalse(matches_binding(binding, observed, observed['controls'][1]['id']))
        self.assertEqual(verify(binding, goal, observed)['status'], 'unavailable')
        fresh = observation(editor(goal['value']), snapshot='fresh')
        self.assertTrue(verify(binding, goal, fresh)['matched'])
        self.assertTrue(matches_binding(binding, fresh, 'fresh:1'))

    def test_historical_value_check_never_accepts_changed_capture_or_goal(self):
        goal = outcome('text', evidence_plane='editor_buffer', value='initial')
        observed = age(observation(editor()))
        binding = bind_for_review(goal, observed['controls'][1], observed)
        result = check_review_predicate(binding, goal, observed)
        self.assertTrue(result['matched_at_capture'])
        self.assertNotIn('matched', result)
        self.assertFalse(result['current_state_proven'])
        for field in ('value', 'snapshot', 'stamp', 'target'):
            changed = deepcopy(observed)
            if field == 'value': changed['controls'][1]['value'] = 'new'
            elif field == 'snapshot': changed['snapshot_id'] = 'other'
            elif field == 'stamp': age(changed, 240)
            else: changed['target']['window_id'] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_review_predicate(binding, goal, changed)
        with self.assertRaises(ValueError):
            check_review_predicate(binding, {**goal, 'value':'different'}, observed)

    def test_retained_binding_still_refuses_ambiguity_foreign_control_and_future(self):
        goal = outcome(); observed = age(observation(control(), control()))
        with self.assertRaises(ValueError):
            bind_for_review(goal, observed['controls'][1], observed)
        observed = age(observation(control()))
        foreign = {**observed['controls'][1], 'name':'Different'}
        with self.assertRaises(ValueError): bind_for_review(goal, foreign, observed)
        for mutation in ('future', 'disagree'):
            changed = deepcopy(observed)
            if mutation == 'future':
                stamp = time.time_ns() + 60_000_000_000
                changed['observed_at_ns'] = changed['provenance']['observed_at_ns'] = stamp
            else: changed['provenance']['observed_at_ns'] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                bind_for_review(goal, changed['controls'][1], changed)

    def test_later_identity_target_and_ambiguity_still_fail(self):
        goal = outcome(); observed = age(observation(control()))
        binding = bind_for_review(goal, observed['controls'][1], observed)
        cases = [observation(control(value='15'), target={'pid':42,'window_id':52}),
                 observation(control(value='15'), control(value='15')),
                 observation(control(name='Other', value='15'))]
        for fresh in cases:
            with self.subTest(observed=fresh['controls']):
                self.assertFalse(verify(binding, goal, fresh)['matched'])
                self.assertFalse(matches_binding(binding, fresh, fresh['controls'][1]['id']))


class RetainedToolReviewTests(unittest.TestCase):
    # Reuse the existing synthetic fixture without inheriting its test cases.
    setUp = tool_fixture.ToolTests.setUp
    call = tool_fixture.ToolTests.call
    items = tool_fixture.ToolTests.items
    item = tool_fixture.ToolTests.item
    review_text = tool_fixture.ToolTests.review_text
    act = tool_fixture.ToolTests.act

    def old_capture(self):
        return age(self.tools._observations[self.sid])

    def test_old_review_has_no_read_or_input_then_dispatch_uses_fresh_capture(self):
        original = deepcopy(self.old_capture())
        calls = len(self.desktop.calls)
        scope = self.review_text(preserve=True)
        self.assertEqual(len(self.desktop.calls), calls)
        self.assertEqual(self.tools._observations[self.sid], original)
        self.assertFalse(self.desktop.executions)
        self.assertIn('value observed at capture', self.reviews[-1])
        self.assertIn('current state will be checked before input', self.reviews[-1])
        artifact = json.loads(next(self.tools.out.glob('review-*.json')).read_text())
        self.assertEqual(artifact['review_capture']['observed_at_ns'], original['observed_at_ns'])
        self.assertGreaterEqual(artifact['review_capture']['age_seconds'], 180)
        self.assertFalse(artifact['review_capture']['current_state_proven'])
        result = self.act(scope, 'Entry', 'exact')
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(len(self.desktop.executions), 1)
        self.assertNotEqual(self.desktop.executions[0]['snapshot_id'], self.sid)
        self.assertEqual(self.call('verify', scope_id=scope)['status'], 'verified')

    def test_changed_preserved_state_refuses_before_input(self):
        self.old_capture(); scope = self.review_text(preserve=True)
        self.desktop.checked = False
        result = self.act(scope, 'Entry', 'exact')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('preservation', result['reason'])
        self.assertFalse(self.desktop.executions)

    def test_changed_control_value_refuses_original_action(self):
        self.old_capture(); scope = self.review_text()
        self.desktop.value = 'changed outside this task'
        result = self.act(scope, 'Entry', 'exact')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('identity changed', result['reason'])
        self.assertFalse(self.desktop.executions)

    def test_stale_predispatch_capture_is_still_refused(self):
        self.old_capture(); scope = self.review_text()
        observe = self.desktop.observe
        def stale(target):
            result = observe(target); age(result['observation']); return result
        with patch.object(self.desktop, 'observe', side_effect=stale):
            result = self.act(scope, 'Entry', 'exact')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('stale', result['reason'])
        self.assertFalse(self.desktop.executions)

    def test_superseded_retained_review_still_refuses(self):
        self.old_capture(); entry = self.item('Entry')['control']
        self.call('observe', window_id=self.window)
        result = self.call('review', snapshot_id=self.sid, summary='Superseded capture',
            goals=[{'id':'entry','kind':'text','target':'Entry','control_id':entry['id'],
                    'value':'exact','evidence_plane':'editor_buffer'}], effects=[{'kind':'goal','goal_id':'entry'}])
        self.assertEqual(result['status'], 'refused')
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)


if __name__ == '__main__': unittest.main()
