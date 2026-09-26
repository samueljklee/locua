"""Progress derives from actual guarded reviews and fresh proof, never claims."""
from copy import deepcopy
import unittest

from locua.plan_progress import reviewed_plan_progress
from tests import test_step_interface as fixtures


class PlanProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StepInterfaceTests('test_actual_review_edit_fresh_verify_and_exact_preserve')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner = self.fixture.owner
        self.ui = self.fixture.ui

    def progress(self):
        return reviewed_plan_progress(self.owner, lambda sid: self.ui.ref('q', sid))

    def review(self, **kwargs):
        result = self.fixture.review(**kwargs)
        self.assertEqual(result['status'], 'approved', result)
        return result

    def complete(self):
        result = self.ui.call('locua_act', {'input': self.fixture.input('Entry'), 'value': 'exact λ'})
        self.assertEqual(result['status'], 'verified', result)
        result = self.ui.call('locua_verify', {})
        self.assertEqual(result['status'], 'verified', result)
        return result

    def test_actual_loop_progress_updates_without_model_done(self):
        self.assertEqual(self.progress()['items'], [])
        review = self.review()
        self.assertEqual(review['plan_progress']['items'][0]['status'], 'current')
        proof = self.complete()
        self.assertEqual(proof['plan_progress']['items'][0]['status'], 'completed')
        self.assertIsNone(proof['plan_progress']['active_review'])
        self.assertFalse(proof['plan_progress']['task_complete'])
        self.assertEqual(len(self.fixture.desktop.executions), 1)
        self.assertEqual(len(self.fixture.reviews), 1)

    def test_model_prose_done_is_not_a_predicate_or_completion(self):
        result = self.ui.call('locua_status', {'decision': {'plan': ['Edit and save'],
            'current_step': 'Done', 'constraints': [], 'unresolved': []}})
        self.assertEqual(result['status'], 'ok', result)
        self.review(covers_request=False, unresolved=['Saving is not bound'])
        self.complete()
        progress = self.progress()
        self.assertEqual(progress['items'][0]['status'], 'completed')
        self.assertFalse(progress['model_plan_prose_coverage_proven'])
        self.assertFalse(progress['items'][0]['whole_request_coverage_reviewed'])
        self.assertNotIn('Edit and save', str(progress))
        self.assertEqual(self.owner.finalize()['status'], 'partial')

    def test_navigation_without_goals_is_progress_not_a_final_outcome(self):
        self.review(goals=[], preserves=[], navigation=[self.fixture.input('Hide panel')],
                    covers_request=False, unresolved=['Requested editor not yet located'])
        action = self.ui.call('locua_act', {'input': self.fixture.input('Hide panel')})
        self.assertEqual(action['status'], 'dispatched', action)
        self.assertEqual(self.progress()['items'], [])
        self.assertTrue(self.progress()['navigation_reviews'])

    def test_new_capture_invalidates_completion_but_preserves_historical_need(self):
        self.review(); self.complete()
        self.ui.call('locua_inspect', {'reference': self.fixture.window})
        row = self.progress()['items'][0]
        self.assertNotEqual(row['status'], 'completed')
        self.assertTrue(row['requires_recheck'])

    def test_changed_preserve_prevents_completion(self):
        self.review(); self.complete()
        self.fixture.desktop.other = 'changed independently'
        self.assertEqual(self.ui.call('locua_verify', {})['status'], 'unverified')
        row = self.progress()['items'][0]
        self.assertNotEqual(row['status'], 'completed')
        self.assertIn('preserved', row['verification']['reason'])

    def test_cancellation_uncertainty_persistence_and_missing_receipt_block(self):
        self.review(); self.complete()
        sid = next(iter(self.owner._scopes))
        self.owner._scopes[sid]['uncertain_action'] = {'reason': 'delivery unknown'}
        self.assertNotEqual(self.progress()['items'][0]['status'], 'completed')
        self.owner._scopes[sid].pop('uncertain_action')
        self.owner.evidence['unmet_persistence_requirements'] = [{'requirement': 'backing_file_unchanged'}]
        self.assertNotEqual(self.progress()['items'][0]['status'], 'completed')
        self.owner.evidence['unmet_persistence_requirements'] = []
        self.owner.evidence['verification'][sid]['goals'] = []
        self.assertNotEqual(self.progress()['items'][0]['status'], 'completed')
        self.owner._cancellation = {'status': 'canceled'}
        self.assertNotEqual(self.progress()['items'][0]['status'], 'completed')

    def test_long_approved_summary_does_not_become_a_projection_failure(self):
        result = self.review(summary='Requested editor change. ' * 40)
        self.assertEqual(result['status'], 'approved', result)
        self.assertIsNone(self.owner._cancellation)
        self.complete()
        self.assertEqual(self.progress()['items'][0]['status'], 'completed')

    def test_projection_never_mutates_owner_evidence(self):
        self.review(); self.complete()
        before = deepcopy(self.owner.evidence)
        self.progress(); self.progress()
        self.assertEqual(self.owner.evidence, before)

    def test_persistence_latches_before_optional_claim_budget_failure(self):
        result = self.fixture.review(goals=[{'outcome': 'calculation', 'value': 'x',
            'persistence_requirement': 'backing_file_unchanged'}], decision={
                'plan': ['x'*590]*12, 'current_step': 'unused', 'constraints': [], 'unresolved': []})
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertEqual(self.fixture.review()['code'], 'text_persistence_unmet')
        self.assertEqual(self.fixture.reviews, [])
        self.assertEqual(self.fixture.desktop.executions, [])


if __name__ == '__main__':
    unittest.main()
