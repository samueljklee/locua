"""Generic captured-text query tests and an optional retained-run regression."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest

from locua import progressive_ui as ui
from locua.amplifier_tools import DesktopToolset
from locua.engine.prototype.regions import catalog_regions

ROOT = Path(__file__).resolve().parents[1]
RETAINED = ROOT/'artifacts/continuity-v14-005/live-settings-27b/cli/desktop'


def fixture():
    now = time.time_ns()
    controls = [
        {'id': 'w', 'role': 'AXWindow', 'name': 'Work surface', 'parent': None, 'value': None},
        {'id': 'a', 'role': 'AXGroup', 'name': 'Content', 'parent': 'w', 'value': None},
        {'id': 'b', 'role': 'AXGroup', 'name': 'Secondary', 'parent': 'w', 'value': None},
        {'id': 'read1', 'role': 'AXStaticText', 'name': None, 'parent': 'a', 'value': '  BOREAL service idle\n'},
        {'id': 'read2', 'role': 'AXStaticText', 'name': 'Observed status', 'parent': 'a', 'value': 'Boreal backup'},
        {'id': 'edit', 'role': 'AXTextField', 'name': 'Notes', 'parent': 'a', 'value': 'Boreal draft'},
        {'id': 'button', 'role': 'AXButton', 'name': 'Boreal settings', 'parent': 'a', 'value': None},
        {'id': 'popup', 'role': 'AXPopUpButton', 'name': 'Medium', 'parent': 'a', 'value': 'Medium',
         'semantics': {'identifier': 'BorealTextSize'}},
        {'id': 'hidden', 'role': 'AXStaticText', 'name': None, 'parent': 'b', 'value': 'Boreal alternative', 'states': {'hidden': True}},
        {'id': 'object', 'role': 'AXStaticText', 'name': None, 'parent': 'b', 'value': {'text': 'Boreal opaque'}},
    ]
    # The production observation contract only permits primitive values. The
    # nontext primitive must not be searched via an invented string rendering.
    controls[-1]['value'] = 1234
    for control in controls:
        control.setdefault('states', {}); control.setdefault('semantics', {}); control['actions'] = []
    return {'kind': 'native_window_state', 'target': {'pid': 41, 'window_id': 52}, 'snapshot_id': 'text-search',
            'observed_at_ns': now, 'provenance': {'observed_at_ns': now}, 'controls': controls,
            'text': 'Boreal announcement', 'handles': {}, 'hierarchy': [], 'coverage': {'complete': False}}


def ids(page):
    return [r['id'] for r in ui.exposed_controls(page)]


class ObservedTextSearchTests(unittest.TestCase):
    def test_default_keeps_label_only_search_but_opt_in_includes_captured_string_values(self):
        observation = fixture(); original = deepcopy(observation)
        old = ui.listing(observation, query='boreal')
        new = ui.listing(observation, query='boreal', include_values=True)
        self.assertEqual(ids(old), ['button', 'popup'])
        self.assertFalse(old['query_searches_values'])
        self.assertNotIn('query_semantics', old)
        self.assertEqual(ids(new), ['read1', 'read2', 'edit', 'button', 'popup', 'hidden'])
        self.assertTrue(new['query_searches_values'])
        self.assertFalse(new['coverage']['negative_evidence_proven'])
        self.assertFalse(new['query_semantics']['visibility_or_exactness_proven'])
        self.assertEqual(observation, original)
        readout = next(r for r in ui.exposed_controls(new) if r['id'] == 'read1')
        self.assertEqual(readout['value'], '  BOREAL service idle\n')
        self.assertEqual(readout['parent'], 'a')
        self.assertEqual(readout['actions'], [])

    def test_role_and_region_scopes_apply_to_all_matches_and_preserve_continuations(self):
        observation = fixture()
        region = next(r['id'] for r in catalog_regions(observation)['regions'] if r['root_control_id'] == 'a')
        cursor = None; pages = []
        while True:
            page = ui.listing(observation, region_id=region, role='AXStaticText', query='boreal',
                              include_values=True, limit=1, cursor=cursor)
            pages.append(page); cursor = page['coverage']['continuation']
            if cursor is None: break
        self.assertEqual([cid for p in pages for cid in ids(p)], ['read1', 'read2', 'hidden'])
        competitor = next(r for p in pages for r in ui.exposed_controls(p) if r['id'] == 'hidden')
        self.assertEqual(competitor['membership'], 'context')
        self.assertTrue(competitor['states']['hidden'])
        self.assertEqual(pages[0]['coverage']['matched_total'], 3)
        self.assertEqual(pages[0]['coverage']['outside_scope_count'],
                         ui.listing(observation, region_id=region)['coverage']['outside_scope_count'])
        self.assertFalse(pages[0]['query_semantics']['unbound_text_searched'])
        self.assertTrue(all(not p['representation']['items_clipped'] for p in pages))

    def test_continuation_cannot_switch_between_label_only_and_value_search(self):
        observation = fixture()
        for initial in (False, True):
            page = ui.listing(observation, query='boreal', limit=1, include_values=initial)
            with self.assertRaises(ui.ProgressiveUIError):
                ui.listing(observation, query='boreal', cursor=page['coverage']['continuation'], include_values=not initial)

    def test_scoped_unbound_text_remains_explicit_and_global_coverage_is_disclosed(self):
        observation = fixture()
        region = next(r['id'] for r in catalog_regions(observation)['regions'] if r['root_control_id'] == 'a')
        scoped = ui.listing(observation, region_id=region, query='announcement', include_values=True)
        self.assertEqual(ids(scoped), [])
        self.assertTrue(scoped['query_semantics']['unbound_text_searched'])
        self.assertEqual(scoped['items'], [{'kind': 'unbound_text', 'text': 'Boreal announcement'}])
        self.assertTrue(scoped['query_semantics']['unbound_text_is_not_a_control'])
        global_page = ui.listing(observation, query='announcement', include_values=True)
        self.assertEqual(global_page['items'], [])
        self.assertFalse(global_page['query_semantics']['unbound_text_searched'])
        self.assertIn('not searched', global_page['query_semantics']['unbound_text_scope'])

    def test_oversized_readout_matches_full_string_but_remains_explicitly_deferred(self):
        observation = fixture()
        observation['controls'][3]['value'] = 'x' * 3000 + ' Queue status Ω '
        page = ui.listing(observation, query='queue status ω', include_values=True)
        self.assertEqual(ids(page), ['read1'])
        self.assertTrue(ui.exposed_controls(page)[0]['value']['deferred'])
        self.assertFalse(ui.exposed_controls(page)[0]['value']['exact_value_in_this_view'])
        self.assertEqual(ids(ui.listing(observation, query='1234', include_values=True)), [])

    def test_owner_enables_policy_only_for_compact_interface_and_reports_semantics(self):
        class Desktop:
            def actions(self, observation): return {'status': 'ok', 'actions': []}
            def close(self): return {'status': 'closed'}
        observation = fixture()
        with tempfile.TemporaryDirectory() as directory:
            old = DesktopToolset({}, Path(directory)/'old', 'Inspect observed UI.', lambda *args: '', desktop=Desktop())
            new = DesktopToolset({}, Path(directory)/'new', 'Inspect observed UI.', lambda *args: '', desktop=Desktop(), tool_profile='continuity-v1')
            try:
                old._retain(observation); new._retain(observation)
                result = old.call('locua_inspect', {'snapshot_id': observation['snapshot_id'], 'operation': 'list', 'query': 'boreal'})
                self.assertEqual(ids(result), ['button', 'popup'])
                interface = new.model_interface; view = interface.ref('v', observation['snapshot_id'])
                result = interface.call('locua_inspect', {'view': view, 'query': 'boreal'})
                self.assertEqual([r.get('value') for r in result['items'] if r.get('role') == 'AXStaticText'],
                                 ['  BOREAL service idle\n', 'Boreal backup', 'Boreal alternative'])
                self.assertTrue(result['query_semantics']['includes_unnamed_readouts'])
                self.assertIn('value', result['query_semantics']['fields'])
                help_text = next(t.description for t in interface.tools() if t.name == 'locua_inspect')
                self.assertIn('string values', help_text)
            finally: old.close(); new.close()

    @unittest.skipUnless(RETAINED.exists(), 'Retained local regression artifacts are not distributed')
    def test_retained_settings_query_preserves_size_match_and_recovers_observed_style_readouts(self):
        event = json.loads((RETAINED/'interface-009.json').read_text())
        observation = json.loads(next(RETAINED.glob('observation-*.json')).read_text())
        arguments = event['translated_arguments']
        arguments = {k: v for k, v in arguments.items() if k not in ('snapshot_id', 'operation')}
        old = ui.listing(observation, **arguments)
        new = ui.listing(observation, **arguments, include_values=True)
        self.assertEqual(ids(old), ['native:s00000018:104'])
        self.assertTrue(set(ids(old)) <= set(ids(new)))
        values = [row.get('value') for row in ui.exposed_controls(new)]
        self.assertIn('Icon & widget style', values)
        self.assertIn('Sidebar icon size', values)
        self.assertIn('Medium', values)
        self.assertEqual(new['coverage']['matched_total'], 3)


if __name__ == '__main__':
    unittest.main()
