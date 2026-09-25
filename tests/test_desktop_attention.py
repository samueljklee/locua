from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.desktop_attention import VisibleDesktopOwner, area_for
from locua.desktop_tools import DesktopTools
from test_desktop_tools import Peer, TARGET

WINDOW = {'x': -2600., 'y': -2000., 'width': 700., 'height': 600.}


class CursorPeer(Peer):
    def __init__(self, directory):
        super().__init__(directory);self.sessions=[];self.frame=deepcopy(WINDOW)
        for name in ('move_cursor', 'set_agent_cursor_motion', 'set_agent_cursor_enabled', 'get_window_state', 'click'):
            self.inventory['tools'].append({'name': name, 'inputSchema': {'properties': {'session': {'type': 'string'}}}})
    def start_session(self, name):self.sessions.append(name)
    def call(self, name, args, **kw):
        if name in ('move_cursor', 'set_agent_cursor_motion', 'set_agent_cursor_enabled'):
            self.calls.append((name, deepcopy(args)));return {}, {'effect': 'unverifiable'}
        record, payload = super().call(name, args, **kw)
        if name == 'list_windows':
            for w in payload['windows']:w['bounds'] = deepcopy(self.frame)
        return record, payload


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.peer=CursorPeer(Path(self.tmp.name)/'peer')
        p=patch('locua.engine_adapter.owner',return_value=self.peer);p.start();self.addCleanup(p.stop)
        self.d=DesktopTools({},Path(self.tmp.name)/'run');self.addCleanup(self.d.close)

    def test_observe_inspect_and_input_share_one_visible_cursor(self):
        o=self.d.observe(TARGET)['observation'];result=self.d.attention(o)
        self.assertEqual(result['status'],'shown');self.assertFalse(result['application_input'])
        self.assertEqual(result['position'],{'x':-2250.,'y':-1700.})
        a=next(a for a in self.d.actions(o)['actions'] if a['name']=='One')
        self.d.execute(a,o)
        calls=[(n,a) for n,a in self.peer.calls if n in ('get_window_state','click','move_cursor')]
        self.assertEqual(len({a['session'] for _,a in calls}),1)
        self.assertEqual(len(self.peer.sessions),1)
        self.assertEqual(next(a for n,a in self.peer.calls if n=='set_agent_cursor_motion')['idle_hide_ms'],0)
        self.assertTrue(all(a['scope']=='window' for n,a in calls if n=='move_cursor'))

    def test_moved_window_does_not_display_stale_control_position(self):
        o=self.d.observe(TARGET)['observation'];self.peer.frame['x']+=10
        self.assertEqual(self.d.attention(o,retained=True)['status'],'unavailable')
        self.assertFalse(any(n=='move_cursor' for n,_ in self.peer.calls))

    def test_competitors_use_union_not_first_query_match(self):
        o={'controls':[{'id':'a','bounds':{'x':-2500.,'y':-1900.,'width':40.,'height':20.}},
                       {'id':'b','bounds':{'x':-2100.,'y':-1800.,'width':40.,'height':20.}},
                       {'id':'menu','bounds':{'x':5.,'y':5.,'width':10.,'height':10.}}]}
        self.assertEqual(area_for(o,WINDOW,['a','b','menu']),{'x':-2500.,'y':-1900.,'width':440.,'height':120.})
        self.assertIsNone(area_for(o,WINDOW,['menu']))

    def test_missing_overlay_does_not_break_observation_or_claim_visible(self):
        self.peer.inventory={'tools':[]}
        o=self.d.observe(TARGET)['observation']
        result=self.d.attention(o)
        self.assertEqual(result['status'],'unavailable')
        self.assertFalse(any(n in ('click','move_cursor') for n,_ in self.peer.calls))
