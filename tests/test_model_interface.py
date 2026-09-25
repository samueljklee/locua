"""Reference compilation and authority tests through the existing guarded owner."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time

from locua.amplifier_tools import DesktopToolset
from tests.test_amplifier_tools import Desktop


class ModelInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desktop = Desktop(); self.reviews = []
        def ask(prompt, purpose):
            self.reviews.append((prompt, purpose)); return 'run'
        self.owner = DesktopToolset({}, Path(self.tmp.name)/'out',
            'Replace Entry with new text. Keep Competitor unchanged. Do not save.', ask,
            desktop=self.desktop, tool_profile='continuity-v1')
        self.ui = self.owner.model_interface
        apps = self.ui.call('locua_apps', {'query': 'Tool surface'})
        self.app = apps['items'][0]['app_id']
        self.window = self.ui.call('locua_windows', {'app_id': self.app})['windows'][0]['window_id']
        self.view = self.ui.call('locua_observe', {'window_id': self.window})['view']
        self.rows = self.ui.call('locua_inspect', {'view': self.view})['items']

    def tearDown(self):
        self.owner.close(); self.tmp.cleanup()

    def row(self, name):
        return next(r for r in self.rows if r.get('name') == name)

    def review(self):
        return self.ui.call('locua_review', {'summary': 'Replace Entry; leave Competitor unchanged; do not save',
            'goals': [{'kind': 'text', 'target': self.row('Entry')['target'], 'value': 'new text'}],
            'preserve': [{'target': self.row('Competitor')['target'], 'property': 'value'}],
            'covers_request': True})

    def act(self, **extra):
        return self.ui.call('locua_act', {'target': self.row('Entry')['target'], 'operation': 'set_text', 'value': 'new text', **extra})

    def test_unknown_approval_never_manufactured(self):
        result = self.act()
        self.assertEqual(result['code'], 'approval_required')
        self.assertEqual(self.owner._scopes, {})
        self.assertEqual(self.desktop.executions, [])
        result = self.act(review='q999')
        self.assertEqual(result['code'], 'unknown_reference')
        self.assertEqual(self.desktop.executions, [])

    def test_real_review_exact_edit_and_independent_refresh(self):
        reviewed = self.review(); self.assertEqual(reviewed['status'], 'approved', reviewed)
        result = self.act(); self.assertIn(result['status'], ('verified', 'dispatched'), result)
        self.assertEqual(self.desktop.value, 'new text'); self.assertEqual(self.desktop.other, 'protected')
        self.assertEqual(len(self.desktop.executions), 1)
        captures = self.desktop.sequence
        proof = self.ui.call('locua_verify', {})
        self.assertEqual(proof['status'], 'verified', proof)
        self.assertGreater(self.desktop.sequence, captures)
        self.assertTrue(all(c['matched'] for c in proof['checks']))
        self.assertEqual(len(self.reviews), 1)

    def test_slow_thinking_keeps_choice_but_requires_fresh_guard(self):
        self.assertEqual(self.review()['status'], 'approved')
        before = self.desktop.sequence
        with patch('time.time_ns', return_value=time.time_ns()+90_000_000_000):
            result = self.act()
        self.assertIn(result['status'], ('verified', 'dispatched'), result)
        self.assertGreater(self.desktop.sequence, before)
        self.assertEqual(len(self.desktop.executions), 1)

    def test_old_choice_still_rechecks_preservation_before_input(self):
        self.assertEqual(self.review()['status'], 'approved')
        self.desktop.other = 'changed while thinking'
        with patch('time.time_ns', return_value=time.time_ns()+90_000_000_000):
            result = self.act()
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('preservation', result['reason'].lower())
        self.assertEqual(self.desktop.executions, [])

    def test_changed_preserve_blocks_before_input(self):
        self.assertEqual(self.review()['status'], 'approved')
        self.desktop.other = 'unexpected'
        result = self.act()
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('preservation', result['reason'].lower())
        self.assertEqual(self.desktop.executions, [])

    def test_two_real_approvals_require_explicit_disambiguation(self):
        a = self.review(); b = self.review()
        self.assertNotEqual(a['review'], b['review'])
        result = self.act(); self.assertEqual(result['code'], 'ambiguous_approval')
        self.assertEqual(self.desktop.executions, [])
        self.assertIn(self.act(review=a['review'])['status'], ('verified', 'dispatched'))

    def test_target_reference_does_not_follow_mutable_latest_window(self):
        self.assertEqual(self.review()['status'], 'approved')
        self.ui.call('locua_observe', {'window_id': self.window})
        result = self.act()
        self.assertEqual(result['code'], 'stale_view')
        self.assertEqual(self.desktop.executions, [])

    def test_mixed_reference_domains_and_press_value_refused(self):
        result = self.ui.call('locua_act', {'target': self.view, 'operation': 'press'})
        self.assertEqual(result['code'], 'unknown_reference')
        result = self.ui.call('locua_act', {'target': self.row('Keep')['target'], 'operation': 'press', 'value': ''})
        self.assertEqual(result['code'], 'press_has_value')
        self.assertEqual(self.desktop.executions, [])

    def test_repeated_refusal_halts_without_input_even_between_recaptures(self):
        for n in range(3):
            result = self.ui.call('locua_observe', {'window_id': self.window})
            if result.get('execution_stopped'):
                break
            view = result['view']
            result = self.ui.call('locua_inspect', {'view': view, 'query': 'Entry'})
            if result.get('execution_stopped'):
                break
            rows = result['items']
            result = self.ui.call('locua_act', {'target': next(r for r in rows if r.get('name') == 'Entry')['target'],
                                               'operation': 'set_text', 'value': 'new text'})
        self.assertTrue(result['execution_stopped'])
        self.assertEqual(self.owner._cancellation['reason'], 'nonprogress_limit')
        self.assertEqual(self.desktop.executions, [])

    def test_unknown_preservation_is_not_false(self):
        result = self.ui.call('locua_review', {'summary': 'Edit Entry',
            'goals': [{'kind': 'text', 'target': self.row('Entry')['target'], 'value': 'new text'}],
            'preserve': [{'target': self.row('Hide panel')['target'], 'property': 'selected'}], 'covers_request': True})
        self.assertEqual(result['code'], 'preservation_unknown')
        self.assertEqual(self.reviews, [])

    def test_projection_contains_competitors_names_parents_and_operations(self):
        self.assertEqual({self.row('Entry')['name'], self.row('Competitor')['name']}, {'Entry', 'Competitor'})
        self.assertEqual(self.row('Entry')['operations'], ['set_text'])
        self.assertEqual(self.row('Entry')['parent']['name'], 'Tool surface')
        self.assertEqual(self.row('Result')['operations'], [])

    def test_control_detail_exposes_only_callable_public_references(self):
        target = self.row('Entry')['target']
        detail = self.ui.call('locua_inspect', {'view': self.view, 'target': target})
        attributes = {r['field']: r['value'] for r in detail['items'] if r['kind'] == 'attribute'}
        self.assertEqual(attributes['target'], target)
        self.assertEqual(attributes['operations'], ['set_text'])
        self.assertTrue(attributes['parent'].startswith('c'))
        self.assertTrue(attributes['region'].startswith('r'))
        self.assertNotIn('actions', attributes)
        self.assertNotIn('id', attributes)

    def test_large_approved_value_is_losslessly_retrievable(self):
        value = 'exact \"δ\"\n  ' * 1800
        reviewed = self.ui.call('locua_review', {'summary': 'Replace only Entry; no save',
            'goals': [{'kind': 'text', 'target': self.row('Entry')['target'], 'value': value}],
            'covers_request': True})
        self.assertEqual(reviewed['status'], 'approved', reviewed)
        compact = reviewed['approved']['goals'][0]
        self.assertEqual(compact['id'], 'g1')
        self.assertEqual(compact['kind'], 'text')
        self.assertTrue(compact['value']['deferred'])
        arguments = compact['value']['next']['arguments']
        parts = []
        while True:
            page = self.ui.call('locua_status', arguments)
            self.assertEqual(page['status'], 'ok', page)
            parts.extend(page['items'])
            arguments = page['coverage'].get('continue_with')
            if not arguments:
                break
        restored = json.loads(''.join(p['text'] for p in parts))
        self.assertEqual(restored['goals'][0]['value'], value)
        self.assertEqual(self.desktop.executions, [])

    def test_reconciliation_uses_public_schema(self):
        reviewed = self.review()
        raw = {'status': 'uncertain', 'scope_id': 'scope:1', 'action_started': True,
               'reconciliation': {'available': True, 'read_only': True, 'input_authority_restored': False,
                    'tool': 'locua_verify', 'arguments': {'scope_id': 'scope:1', 'reconcile': True}}}
        projected = self.ui._project('locua_act', raw)
        self.assertEqual(projected['reconciliation']['arguments'], {'review': reviewed['review'], 'reconcile': True})

    def test_verification_of_named_review_matches_published_contract(self):
        reviewed = self.review()
        self.assertIn(self.act()['status'], ('verified', 'dispatched'))
        result = self.ui.call('locua_verify', {'review': reviewed['review']})
        self.assertEqual(result['status'], 'verified', result)
        rejected = self.ui.call('locua_verify', {'goal': 'g1'})
        self.assertEqual(rejected['code'], 'argument_contract_invalid', rejected)


if __name__ == '__main__': unittest.main()
