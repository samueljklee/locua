"""Captioned exact references and persistence classification; CPU-only owner fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

from locua.amplifier_contracts import _errors
from locua.amplifier_tools import DesktopToolset
from locua.model_interface import InterfaceError
from locua.step_interface import StepModelInterface, StepNamedModelInterface, SPECS
from tests.test_amplifier_tools import Desktop


class NamedStepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.serial = 0
        self.owner, self.ui, self.desktop, self.window, self.rows, self.reviews = self.fixture()

    def fixture(self, interface=StepNamedModelInterface, desktop=None):
        self.serial += 1
        desktop = desktop or Desktop(); reviews = []
        def ask(prompt, purpose): reviews.append((prompt, purpose)); return 'run'
        owner = DesktopToolset({}, Path(self.temporary.name)/str(self.serial),
            'Replace Entry with exact λ. Preserve Competitor. No backing-file requirement.', ask,
            desktop=desktop, tool_profile='semantic-v1', persistence_contract='text-persistence-v1')
        ui = interface(owner); owner.model_interface = ui
        self.addCleanup(owner.close)
        app = ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
        window = ui.call('locua_inspect', {'reference': app})['windows'][0]['window_id']
        view = ui.call('locua_inspect', {'reference': window})['view']
        rows = self.collect(ui, view)
        return owner, ui, desktop, window, rows, reviews

    def collect(self, ui, view):
        rows = []; result = ui.call('locua_search', {'reference': view})
        for _ in range(100):
            self.assertEqual(result['status'], 'ok', result)
            # Existing semantic pagination budgets its page before Step routes
            # and exploration receipts are added. Keep all rows within that
            # unchanged budget; report the complete envelope separately.
            self.assertLessEqual(len(json.dumps(result['items'], ensure_ascii=False, separators=(',', ':')).encode()), 8000)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()), 9000)
            rows.extend(result['items'])
            continuation = result.get('coverage', {}).get('continue_with')
            if not continuation: return rows
            result = ui.call('locua_inspect', continuation)
        self.fail('No end to paged inspection')

    def row(self, name): return next(row for row in self.rows if row.get('name') == name)

    def review_args(self, **changes):
        args = {'summary': 'Replace Entry with exact λ; preserve Competitor; no backing-file requirement.',
            'goals': [{'outcome': self.row('Entry')['outcomes']['text'], 'value': 'exact λ',
                       'persistence_requirement': 'not_requested'}],
            'preserves': [self.row('Competitor')['outcomes']['text']], 'covers_request': True}
        args.update(changes); return args

    def test_exact_named_review_edit_verify_and_compiler_parity(self):
        key = self.row('Entry')['inputs']['set_text']
        self.assertIn(':set_text:Entry@Tool_surface', key)
        reviewed = self.ui.call('locua_review', self.review_args())
        self.assertEqual(reviewed['status'], 'approved', reviewed)
        result = self.ui.call('locua_act', {'input': key, 'value': 'exact λ'})
        self.assertEqual(result['status'], 'verified', result)
        result = self.ui.call('locua_verify', {})
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual((self.desktop.value, self.desktop.other), ('exact λ', 'protected'))
        self.assertEqual(len(self.desktop.executions), 1)
        _, baseline, _, _, rows, _ = self.fixture(StepModelInterface)
        for row in rows:
            named = self.row(row['name'])
            for field, kind in (('outcomes', 'o'), ('inputs', 'i')):
                self.assertEqual(set(row[field]), set(named[field]))
                for capability in row[field]:
                    self.assertEqual(baseline.resolve(row[field][capability], kind),
                                     self.ui.resolve(named[field][capability], kind))

    def test_baseline_schema_and_noncapability_routes_stay_ordinal(self):
        self.assertEqual(SPECS['locua_act'][1]['properties']['input']['pattern'], r'^i[1-9][0-9]*$')
        for row in self.rows:
            self.assertRegex(row['target'], r'^c[1-9][0-9]*$')
            for field, schema_key in (('outcomes', 'outcome'), ('inputs', 'input')):
                for key in row[field].values():
                    self.assertFalse(_errors(self.ui._reference_schemas[schema_key], key), key)
        for name in ('locua_apps', 'locua_inspect', 'locua_search', 'locua_launch', 'locua_activate', 'locua_verify', 'locua_status', 'locua_clarify'):
            self.assertEqual(self.ui.specs[name], SPECS[name])
        self.assertEqual(self.ui.version, 'step-v2')

    def test_candidate_review_help_changes_no_baseline_contract_or_model_prose(self):
        baseline_before = json.dumps(SPECS, ensure_ascii=False, sort_keys=True)
        _, candidate, _, _, _, _ = self.fixture()
        _, baseline, _, _, _, _ = self.fixture(StepModelInterface)
        self.assertEqual(json.dumps(SPECS, ensure_ascii=False, sort_keys=True), baseline_before)
        self.assertEqual(baseline.specs, SPECS)
        old_summary = deepcopy(SPECS['locua_review'][1]['properties']['summary'])
        new_summary = deepcopy(candidate.specs['locua_review'][1]['properties']['summary'])
        guidance = new_summary.pop('description')
        self.assertEqual(new_summary, old_summary)
        self.assertIn('Do not predict or assert unobserved computed results', guidance)
        self.assertIn('fresh verification establishes the actual result', guidance)
        self.assertIn(guidance, candidate.specs['locua_review'][0])
        self.assertNotIn('Calculator', guidance)
        # Guidance is not a prose rewriter or approval gate. The exact model
        # summary remains visible to review and retained in the owner evidence.
        text = 'Model prose: requested exact λ; an unsupported prediction remains visible for review.'
        result = self.ui.call('locua_review', self.review_args(summary=text))
        self.assertEqual(result['status'], 'approved', result)
        self.assertIn(text, self.reviews[-1][0])
        self.assertTrue(any(scope['summary'] == text for scope in self.owner._scopes.values()))

    def test_repeated_issuance_is_stable_and_no_caption_or_ordinal_is_inferred(self):
        key = self.row('Entry')['inputs']['set_text']
        self.assertEqual(self.ui.ref('i', self.ui.resolve(key, 'i')), key)
        for forged in (key.split(':')[0], key.replace('Entry', 'Competitor'),
                       'i99999:set_text:Entry@Tool_surface', self.row('Entry')['outcomes']['text']):
            with self.subTest(forged=forged):
                owner, ui, desktop, _, rows, _ = self.fixture()
                result = ui.call('locua_act', {'input': forged, 'value': 'exact λ'})
                self.assertIn(result['code'], ('argument_contract_invalid', 'unknown_reference'))
                self.assertEqual(desktop.executions, [])
                self.assertIsNone(owner._cancellation)

    def test_stale_named_keys_do_not_follow_new_snapshot(self):
        key = self.row('Entry')['inputs']['set_text']
        old, _ = self.ui.control(self.row('Entry')['target'])
        new_view = self.ui.call('locua_inspect', {'reference': self.window})['view']
        new_rows = self.collect(self.ui, new_view)
        new_key = next(row for row in new_rows if row.get('name') == 'Entry')['inputs']['set_text']
        self.assertNotEqual(new_key, key)
        self.assertEqual(new_key.split(':', 1)[1], key.split(':', 1)[1])
        del self.owner._observations[old['snapshot_id']]
        result = self.ui.call('locua_act', {'input': key, 'value': 'exact λ'})
        self.assertEqual(result['code'], 'stale_view', result)
        self.assertEqual(self.desktop.executions, [])

    def test_duplicate_truncated_and_unnamed_labels_keep_unique_identity_and_pages(self):
        class Many(Desktop):
            def observe(self, target):
                result = super().observe(target); o = result['observation']; sid = o['snapshot_id']
                for n in range(50):
                    o['controls'].append({'id': sid+':duplicate-'+str(n), 'role': 'AXButton',
                        'name': None if n < 2 else 'same caption '*20+str(n), 'value': None,
                        'parent': sid+':0', 'states': {'enabled': True}, 'actions': [],
                        'semantics': {'identifier': 'distinct-'+str(n)}})
                return result
        owner, ui, desktop, _, rows, _ = self.fixture(desktop=Many())
        duplicates = [row for row in rows if str(row.get('identifier', '')).startswith('distinct-')]
        self.assertEqual(len(duplicates), 50)
        keys = [row['inputs']['press'] for row in duplicates]
        self.assertEqual(len(set(keys)), 50)
        self.assertEqual(keys[0].split(':', 1)[1], keys[1].split(':', 1)[1])
        self.assertIn(':press:unnamed-AXButton@', keys[0])
        self.assertEqual(keys[2].split(':', 1)[1], keys[-1].split(':', 1)[1])
        descriptors = [ui.resolve(key, 'i') for key in keys]
        self.assertEqual(len({d['action_id'] for d in descriptors}), 50)
        self.assertEqual(desktop.executions, [])

    def test_captions_sanitize_controls_delimiters_and_never_include_values(self):
        class Hostile(Desktop):
            def observe(self, target):
                result = super().observe(target)
                result['observation']['controls'][0]['name'] = 'Parent\u202e\x1b\n:@/\\'+('長'*60)
                result['observation']['controls'][1]['name'] = '\u202e\x1b\n:@/\\<>"\'`{}[]'+('名'*80)
                return result
        desktop = Hostile(); desktop.value = 'DO_NOT_COPY_THIS_BUFFER_TO_REFERENCE'
        _, ui, _, _, rows, _ = self.fixture(desktop=desktop)
        entry = next(row for row in rows if row.get('identifier') == 'Entry')
        for key in [*entry['outcomes'].values(), *entry['inputs'].values()]:
            self.assertLessEqual(len(key), 128)
            self.assertLessEqual(len(key.encode()), 256)
            self.assertNotIn(desktop.value, key)
            self.assertFalse(any(unicodedata.category(c).startswith('C') for c in key))
            self.assertEqual(key.count('@'), 1); self.assertEqual(key.count(':'), 2)
            self.assertIn(key, ui.refs)

    def test_missing_and_unnamed_parent_use_observed_role_fallback_without_inference(self):
        for missing in (True, False):
            with self.subTest(missing_parent=missing):
                class Parent(Desktop):
                    def observe(self, target):
                        result = super().observe(target)
                        controls = result['observation']['controls']
                        controls[0]['name'] = None
                        if missing: controls[1]['parent'] = 'absent-parent-control'
                        return result
                _, ui, _, _, rows, _ = self.fixture(desktop=Parent())
                entry = next(row for row in rows if row.get('identifier') == 'Entry')
                key = entry['inputs']['set_text']
                self.assertTrue(key.endswith('@parent-unknown' if missing else '@unnamed-AXWindow'), key)
                self.assertFalse(_errors(ui._reference_schemas['input'], key))
                if missing: self.assertNotIn('parent', entry)

    def test_unknown_state_caption_does_not_grant_preservation(self):
        row = self.row('Hide panel')
        key = row['outcomes']['selected_unknown']
        self.assertIn(':selected_unknown:', key)
        result = self.ui.call('locua_review', self.review_args(preserves=[key]))
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.desktop.executions, [])

    def test_known_nontext_persistence_is_actionable_then_correctable_in_both_profiles(self):
        for interface in (StepModelInterface, StepNamedModelInterface):
            for name, kind in (('Result', 'calculation'), ('Result', 'value'), ('Keep', 'checked')):
                with self.subTest(interface=interface.version, kind=kind):
                    owner, ui, desktop, _, rows, reviews = self.fixture(interface)
                    row = next(r for r in rows if r.get('name') == name)
                    args = {'summary': 'CPU incompatible-field check.', 'goals': [
                        {'outcome': row['outcomes'][kind], 'value': True if kind == 'checked' else '2+3',
                         'persistence_requirement': 'backing_file_unchanged'}], 'covers_request': False}
                    result = ui.call('locua_review', args)
                    self.assertEqual(result['code'], 'persistence_requires_text', result)
                    self.assertIsNone(owner._cancellation)
                    self.assertEqual(owner.evidence.get('unmet_persistence_requirements'), [])
                    self.assertEqual(reviews, []); self.assertEqual(desktop.executions, [])
                    if kind == 'calculation':
                        del args['goals'][0]['persistence_requirement']
                        self.assertEqual(ui.call('locua_review', args)['status'], 'approved')

    def test_mixed_text_and_unknown_requirements_latch_before_nontext_error(self):
        for kind in ('text', 'unknown'):
            for reverse_order in (False, True):
                with self.subTest(kind=kind, reverse_order=reverse_order):
                    owner, ui, desktop, _, rows, reviews = self.fixture()
                    calculation = next(r for r in rows if r.get('name') == 'Result')['outcomes']['calculation']
                    text = next(r for r in rows if r.get('name') == 'Entry')['outcomes']['text']
                    goals = [
                        {'outcome': calculation, 'value': '2+3', 'persistence_requirement': 'backing_file_unchanged'},
                        {'outcome': text if kind == 'text' else 'o99999:text:Entry@Tool_surface',
                         'value': 'exact λ', 'persistence_requirement': 'backing_file_unchanged'}]
                    if reverse_order: goals.reverse()
                    result = ui.call('locua_review', {'summary': 'CPU mixed declaration check.', 'goals': goals, 'covers_request': False})
                    self.assertEqual(result['code'], 'text_persistence_unmet', result)
                    self.assertTrue(result['execution_stopped'])
                    pending = deepcopy(owner.evidence['unmet_persistence_requirements'])
                    self.assertEqual(len(pending), 1)
                    followup = ui.call('locua_review', {'summary': 'Change model declaration.', 'goals': [
                        {'outcome': calculation, 'value': '2+3'}], 'covers_request': True})
                    self.assertEqual(followup['code'], 'text_persistence_unmet')
                    self.assertEqual(owner.evidence['unmet_persistence_requirements'], pending)
                    self.assertEqual(desktop.executions, []); self.assertEqual(reviews, [])

    def test_prior_pending_requirement_checked_even_if_new_goal_is_known_nontext(self):
        owner = self.owner
        owner._text_persistence([{'kind': 'text', 'target': 'previous-unknown',
            'persistence_requirement': 'saved_output_required'}], stage='prior_cpu_declaration')
        self.assertIsNone(owner._cancellation)
        result = self.ui.call('locua_review', {'summary': 'CPU prior requirement check.', 'goals': [
            {'outcome': self.row('Result')['outcomes']['calculation'], 'value': '2+3',
             'persistence_requirement': 'backing_file_unchanged'}], 'covers_request': True})
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertTrue(result['execution_stopped'])
        self.assertEqual([r['requirement'] for r in owner.evidence['unmet_persistence_requirements']], ['saved_output_required'])
        self.assertEqual(self.desktop.executions, [])


if __name__ == '__main__': unittest.main()
