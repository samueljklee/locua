"""Semantic output, paging and unchanged guarded effects; CPU fixtures only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from locua.amplifier_tools import DesktopToolset
from locua.model_interface import ModelInterface
from locua.semantic_projection import SemanticModelInterface, PAGE_BYTES, EXPLORED_CONTROLS
from tests.test_amplifier_tools import Desktop


class ContextDesktop(Desktop):
    def observe(self, target):
        result = super().observe(target)
        if result.get('status') != 'observed':
            return result
        o = result['observation']; sid = o['snapshot_id']
        rows = [
            ('label-a', 'AXStaticText', None, 'Preview surface', 10, None, {}),
            ('choice-a', 'AXButton', 'Muted', None, 11, {'x': 400, 'y': 100, 'width': 80, 'height': 40},
             {'help': 'Reduce saturation in the preview surface', 'identifier': 'preview-muting'}),
            ('label-b', 'AXStaticText', None, 'Export annotations', 20, None, {}),
            ('choice-b', 'AXButton', 'Muted', None, 21, {'x': 400, 'y': 300, 'width': 80, 'height': 40},
             {'help': 'Reduce saturation in annotation markers', 'identifier': 'annotation-muting'}),
        ]
        # The normalizer can append unaddressed readouts after native controls;
        # capture array position is not the producer's rendered sibling order.
        for key, role, name, value, line, bounds, sem in [rows[1], rows[3], rows[0], rows[2]]:
            o['controls'].append({'id': sid+':'+key, 'role': role, 'name': name, 'value': value,
                'parent': sid+':0', 'bounds': bounds, 'semantics': sem, 'states': {'selected': False},
                'actions': [], 'source': {'kind': 'native_markdown', 'line_number': line}})
        return result


class SemanticProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.desktop = ContextDesktop(); self.reviews = []
        self.owner = self.make_owner(self.desktop, 'semantic-v1')
        self.ui = self.owner.model_interface
        self.window, self.view = self.discover(self.ui)
        self.rows = self.collect(self.ui, {'view': self.view})

    def make_owner(self, desktop, profile):
        def ask(prompt, purpose):
            self.reviews.append((prompt, purpose)); return 'run'
        path = Path(self.tmp.name)/str(len(list(Path(self.tmp.name).iterdir())))
        owner = DesktopToolset({}, path, 'Replace Entry with exact new text. Keep Competitor unchanged. Do not save.',
            ask, desktop=desktop, tool_profile=profile)
        self.addCleanup(owner.close)
        return owner

    def discover(self, ui):
        app = ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
        window = ui.call('locua_windows', {'app_id': app})['windows'][0]['window_id']
        view = ui.call('locua_observe', {'window_id': window})['view']
        return window, view

    def collect(self, ui, args):
        rows = []
        for _ in range(1000):
            page = ui.call('locua_inspect', args)
            self.assertEqual(page['status'], 'ok', page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()), PAGE_BYTES+2000)
            rows.extend(page['items']); args = page['coverage'].get('continue_with')
            if not args:
                return rows
        self.fail('Semantic pagination failed to terminate')

    def row(self, name):
        return next(r for r in self.rows if r.get('name') == name)

    def review(self):
        return self.ui.call('locua_review', {'summary': 'Replace Entry; preserve Competitor; do not save',
            'goals': [{'kind': 'text', 'target': self.row('Entry')['target'], 'value': 'exact new text'}],
            'preserve': [{'target': self.row('Competitor')['target'], 'property': 'value'}], 'covers_request': True})

    def act(self, value='exact new text'):
        return self.ui.call('locua_act', {'target': self.row('Entry')['target'], 'operation': 'set_text', 'value': value})

    def test_same_tools_schemas_and_help_and_explicit_projection_version(self):
        baseline = ModelInterface(self.owner)
        self.assertEqual([(t.name, t.description, t.input_schema) for t in self.ui.tools()],
                         [(t.name, t.description, t.input_schema) for t in baseline.tools()])
        self.assertIsInstance(self.ui, SemanticModelInterface)
        self.assertEqual(self.ui.call('locua_status', {})['projection_version'], 'semantic-v1')

    def test_competing_same_names_keep_actual_help_identifier_parent_and_geometry(self):
        found = self.ui.call('locua_inspect', {'view': self.view, 'query': 'Muted'})
        choices = found['items']
        self.assertEqual(len(choices), 2)
        self.assertNotEqual(choices[0]['target'], choices[1]['target'])
        self.assertNotEqual(choices[0]['help'], choices[1]['help'])
        self.assertNotEqual(choices[0]['identifier'], choices[1]['identifier'])
        self.assertNotEqual(choices[0]['bounds']['y'], choices[1]['bounds']['y'])
        self.assertEqual(choices[0]['parent']['target'], choices[1]['parent']['target'])
        self.assertTrue(all(c['states']['selected'] is False for c in choices))
        self.assertTrue(all(c['operations'] == ['press'] for c in choices))

    def test_neighborhood_uses_captured_source_order_without_inventing_section_membership(self):
        target = next(r['target'] for r in self.rows if r.get('identifier') == 'annotation-muting')
        page = self.ui.call('locua_inspect', {'view': self.view, 'target': target})
        self.assertEqual(page['neighborhood']['basis'], 'captured_rendered_tree_order')
        self.assertFalse(page['neighborhood']['association_inferred'])
        self.assertFalse(page['neighborhood']['section_membership_proven'])
        neighbors = [r for r in page['items'] if r.get('kind') == 'neighbor']
        adjacent = next(r for r in neighbors if r['offset'] == -1)
        self.assertIsNone(adjacent['name'])
        self.assertEqual(adjacent['value'], 'Export annotations')
        self.assertNotIn('section', page['items'][0])
        self.assertGreater(page['neighborhood']['unknown_order_count'], 0)
        self.assertEqual(self.desktop.executions, [])

    def test_control_array_order_fallback_is_explicit_and_not_visual_distance(self):
        page = self.ui.call('locua_inspect', {'view': self.view, 'target': self.row('Entry')['target']})
        self.assertEqual(page['neighborhood']['basis'], 'captured_control_array_order')
        self.assertTrue(page['neighborhood']['order_is_not_visual_distance'])
        self.assertFalse(page['neighborhood']['association_inferred'])

    def test_exact_long_unicode_value_losslessly_pages_without_raw_provenance_stream(self):
        desktop = Desktop(); desktop.value = '  λ "ready"\nno terminal LF' * 180
        original = desktop.observe
        def observe(target):
            result = original(target)
            c = result['observation']['controls'][1]
            c.update(capabilities={'execution_internal': 'PRIVATE-CAPABILITY-'*8000},
                name_evidence={'provenance': 'PRIVATE-PROVENANCE-'*8000},
                editor={'focused': True, 'value_settable': True, 'raw_value': {'value': desktop.value}})
            return result
        desktop.observe = observe
        owner = self.make_owner(desktop, 'semantic-v1'); ui = owner.model_interface
        _, view = self.discover(ui)
        rows = self.collect(ui, {'view': view, 'query': 'Entry'}); target = rows[0]['target']
        self.assertTrue(rows[0]['value']['deferred'])
        self.assertFalse(rows[0]['value']['exact_value_in_this_view'])
        captured = deepcopy(owner._observations)
        details = self.collect(ui, {'view': view, 'target': target})
        fragments = [r for r in details if r.get('kind') == 'json_fragment' and r['field'] == 'value']
        self.assertGreater(len(fragments), 1)
        self.assertEqual([r['part'] for r in fragments], list(range(fragments[0]['parts'])))
        self.assertEqual(json.loads(''.join(r['text'] for r in fragments)), desktop.value)
        serialized = json.dumps(details)
        self.assertNotIn('PRIVATE-CAPABILITY', serialized); self.assertNotIn('PRIVATE-PROVENANCE', serialized)
        self.assertEqual(owner._observations, captured)
        self.assertEqual(desktop.executions, [])

    def test_projected_list_page_splitting_never_drops_later_owner_pages(self):
        desktop = Desktop(); original = desktop.observe
        def observe(target):
            result = original(target); o = result['observation']; sid = o['snapshot_id']
            for n in range(65):
                o['controls'].append({'id': f'{sid}:readout{n}', 'parent': f'{sid}:0',
                    'role': 'AXStaticText', 'name': f'Meter {n}', 'value': f'Value {n}',
                    'semantics': {'help': 'A captured status description. '*7}, 'states': {}, 'actions': []})
            return result
        desktop.observe = observe
        owner = self.make_owner(desktop, 'semantic-v1'); ui = owner.model_interface
        _, view = self.discover(ui)
        rows = self.collect(ui, {'view': view, 'limit': 32})
        ids = [ui.resolve(row['target'], 'c')[1] for row in rows if 'target' in row]
        captured = ui.observation(view)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {c['id'] for c in captured['controls']})
        self.assertLessEqual(len(ui.state()['explored']['controls']), EXPLORED_CONTROLS)

    def test_stale_and_mixed_refs_still_refuse_and_never_authorize_an_edit(self):
        original = self.row('Entry')['target']
        self.assertEqual(self.review()['status'], 'approved')
        fresh = self.ui.call('locua_observe', {'window_id': self.window})['view']
        result = self.ui.call('locua_inspect', {'view': fresh, 'target': original})
        self.assertEqual(result['code'], 'stale_view', result)
        self.assertEqual(self.act()['code'], 'stale_view')
        self.assertEqual(self.desktop.executions, [])
        self.assertTrue(all(n['potentially_stale'] for n in self.ui.state()['explored']['controls']))
        self.assertFalse(self.ui.state()['explored']['action_authority'])

    def test_mixed_current_windows_and_semantic_continuation_refuse_before_owner_call(self):
        desktop = Desktop(); desktop.value = 'long exact λ\n'*1600
        original = desktop.app_windows
        def windows(app):
            result = original(app)
            second = deepcopy(result['windows'][0]); second['window_id'] += 1
            result['windows'].append(second)
            return result
        desktop.app_windows = windows
        owner = self.make_owner(desktop, 'semantic-v1'); ui = owner.model_interface
        window, first_view = self.discover(ui)
        target = self.collect(ui, {'view': first_view, 'query': 'Entry'})[0]['target']
        detail = ui.call('locua_inspect', {'view': first_view, 'target': target})
        cursor = detail['coverage']['continue_with']['cursor']
        self.assertEqual(ui.resolve(cursor, 'p')['semantic_projection'], 'semantic-v1')
        other_window = next(w for w, (kind, value) in ui.refs.items() if kind == 'w' and w != window)
        second_view = ui.call('locua_observe', {'window_id': other_window})['view']
        count = len(owner.evidence['events'])
        mixed = ui.call('locua_inspect', {'view': second_view, 'target': target})
        self.assertEqual(mixed['code'], 'mixed_views', mixed)
        wrong_cursor = ui.call('locua_inspect', {'view': second_view, 'cursor': cursor})
        self.assertEqual(wrong_cursor['code'], 'stale_cursor', wrong_cursor)
        self.assertEqual(len(owner.evidence['events']), count)
        self.assertEqual(desktop.executions, [])

    def test_review_and_input_and_independent_verification_use_unchanged_private_guards(self):
        self.assertEqual(self.act()['code'], 'approval_required')
        self.assertEqual(self.review()['status'], 'approved')
        self.assertEqual(self.act('different')['code'], 'approval_required')
        self.assertEqual(self.desktop.executions, [])
        result = self.act(); self.assertIn(result['status'], ('verified', 'dispatched'), result)
        self.assertEqual(self.desktop.value, 'exact new text'); self.assertEqual(self.desktop.other, 'protected')
        count = self.desktop.sequence
        proof = self.ui.call('locua_verify', {})
        self.assertEqual(proof['status'], 'verified', proof)
        self.assertGreater(self.desktop.sequence, count)
        self.assertEqual(len(self.desktop.executions), 1)

    def test_changed_preserve_blocks_fresh_input(self):
        self.assertEqual(self.review()['status'], 'approved')
        self.desktop.other = 'changed since inspection'
        result = self.act()
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('preservation', result['reason'].lower())
        self.assertEqual(self.desktop.executions, [])

    def test_arithmetic_noise_omitted_only_for_an_actual_noncalculation_scope(self):
        raw = {'status': 'dispatched', 'arithmetic_input': {'retained': 'witness'}}
        self.assertIn('arithmetic_input', self.ui._project('locua_act', raw))
        self.review(); sid = next(iter(self.owner._scopes))
        self.assertNotIn('arithmetic_input', self.ui._project('locua_act', {**raw, 'scope_id': sid}))
        self.owner._scopes[sid]['status'] = 'proposed'
        self.assertIn('arithmetic_input', self.ui._project('locua_act', {**raw, 'scope_id': sid}))
        self.owner._scopes[sid]['status'] = 'approved'
        self.owner._scopes[sid]['goals'].append({'kind': 'calculation'})
        self.assertIn('arithmetic_input', self.ui._project('locua_act', {**raw, 'scope_id': sid}))

    def test_continuity_baseline_detail_is_not_replaced(self):
        owner = self.make_owner(Desktop(), 'continuity-v1'); ui = owner.model_interface
        _, view = self.discover(ui)
        target = self.collect(ui, {'view': view, 'query': 'Entry'})[0]['target']
        detail = ui.call('locua_inspect', {'view': view, 'target': target})
        self.assertNotIn('projection_version', detail)
        self.assertTrue(any(r.get('field') == 'capabilities' for r in detail['items']))


if __name__ == '__main__':
    unittest.main()
