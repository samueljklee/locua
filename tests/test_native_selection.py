"""Collection selection is a capability, never an inferred AXPress or task recipe."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_desktop_tools import Peer, TARGET
from locua.desktop_tools import DesktopTools
from locua.engine.prototype.core import action_available


class SelectionPeer(Peer):
    def __init__(self, directory):
        super().__init__(directory)
        self.selected = False
        self.label = 'Archive'
        self.select_effect = True
        self.rename_before_input = False
        self.inventory['tools'].append({'name': 'click', 'inputSchema': {'properties': {
            'pid': {'type': 'integer'}, 'window_id': {'type': 'integer'},
            'element_token': {'type': 'string'}}}})

    def snapshot(self):
        raw = super().snapshot();s = raw['snapshot_id']
        raw['elements'] += [
            {'element_index': 5, 'element_token': s+':5', 'role': 'AXRow',
             'label': None, 'selected': self.selected, 'actions': ['AXShowDefaultUI'], 'parent_index': 0},
            {'element_index': 6, 'element_token': s+':6', 'role': 'AXStaticText',
             'label': self.label, 'actions': [], 'parent_index': 5}]
        return raw

    def call(self, name, args, **kw):
        if name == 'get_window_state' and self.rename_before_input and self.sequence:
            self.label = 'Different row'
        if name == 'click' and args.get('element_token', '').endswith(':5'):
            if self.select_effect:
                self.selected = True;self.title = 'A newly selected page'
            self.ack = {'effect': 'confirmed', 'evidence': [{'kind': 'value_readback'}]}
        return super().call(name, args, **kw)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.peer = SelectionPeer(Path(self.tmp.name)/'peer')
        p = patch('locua.engine_adapter.owner', return_value=self.peer);p.start();self.addCleanup(p.stop)
        self.d = DesktopTools({}, Path(self.tmp.name)/'run');self.addCleanup(self.d.close)

    def selected_action(self):
        o = self.d.observe(TARGET)['observation']
        a = next(a for a in self.d.actions(o)['actions'] if a['name'] == 'Archive')
        return o, a

    def test_unnamed_row_gets_proven_child_label_and_guarded_selection(self):
        o, a = self.selected_action();row = next(c for c in o['controls'] if c['id'] == a['control_id'])
        self.assertEqual(row['actions'], ['AXShowDefaultUI'])
        self.assertEqual(row['name_evidence']['kind'], 'single_direct_static_child')
        self.assertFalse(action_available(o, row, 'press'))  # Original RLCD route unchanged.
        self.assertTrue(any(c['role'] == 'AXStaticText' and c['name'] == 'Archive' for c in o['controls']))
        result = self.d.execute(a, o)
        self.assertEqual(result['status'], 'dispatched')
        self.assertTrue(result['verification']['selection_readback']['matched'])
        self.assertFalse(result['task_complete'])

    def test_changed_child_label_is_not_another_anonymous_row(self):
        o, a = self.selected_action();self.peer.rename_before_input = True
        result = self.d.execute(a, o)
        self.assertEqual(result['status'], 'refused')
        self.assertFalse(any(n == 'click' for n, _ in self.peer.calls))

    def test_driver_ack_without_post_selection_does_not_count(self):
        o, a = self.selected_action();self.peer.select_effect = False
        result = self.d.execute(a, o)
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse(result['verification']['selection_readback']['matched'])
        self.assertEqual(self.d.execute(a, o)['status'], 'refused')

    def test_unpinned_schema_only_and_unknown_selection_cannot_grant_route(self):
        self.peer.server = {'serverInfo': {'name': 'different-driver', 'version': '0.28.2'}}
        o = self.d.observe(TARGET)['observation']
        self.assertFalse(any(a['role'] == 'AXRow' for a in self.d.actions(o)['actions']))

    def test_disabled_selection_never_published(self):
        original = self.peer.snapshot
        def snap():
            raw = original();raw['elements'][5]['enabled'] = False;return raw
        self.peer.snapshot = snap
        o = self.d.observe(TARGET)['observation']
        self.assertFalse(any(a['role'] == 'AXRow' for a in self.d.actions(o)['actions']))

    def test_duplicate_row_labels_stay_ambiguous(self):
        original = self.peer.snapshot
        def snap():
            raw = original();s = raw['snapshot_id']
            raw['elements'] += [{**deepcopy(raw['elements'][5]), 'element_index': 7, 'element_token': s+':7'},
                                {**deepcopy(raw['elements'][6]), 'element_index': 8, 'element_token': s+':8', 'parent_index': 7}]
            return raw
        self.peer.snapshot = snap
        o, a = self.selected_action()
        self.assertEqual(len([a for a in self.d.actions(o)['actions'] if a['role'] == 'AXRow']), 2)
        self.assertEqual(self.d.execute(a, o)['status'], 'refused')
        self.assertFalse(any(n == 'click' for n, _ in self.peer.calls))
