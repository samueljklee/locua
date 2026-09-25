"""Generic continuation views preserve competitors, paging and fresh guards."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua import lib, progressive_ui
from locua.amplifier_tools import DesktopToolset, MODEL_RESPONSE_BYTES
from locua.errors import LocuaError
from locua.interaction_context import post_action_views
from test_amplifier_tools import Desktop


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.desktop=Desktop()
        self.tools=DesktopToolset({},Path(self.tmp.name)/'tools','Change Entry; preserve other controls.',
            lambda *args:'run',desktop=self.desktop,tool_profile='fresh-region-v1')
        self.addCleanup(self.tools.close)
        app=self.tools.call('locua_apps',{})['items'][0]['app_id']
        self.wid=self.tools.call('locua_windows',{'app_id':app})['windows'][0]['window_id']
        self.sid=self.tools.call('locua_observe',{'window_id':self.wid})['snapshot_id']

    def test_fresh_complete_competitors_stale_refs_and_outside_scope(self):
        before=self.tools._observations[self.sid]
        cid=next(c['id'] for c in before['controls'] if c['name']=='Entry')
        action=next(a for a in self.tools._actions[self.sid].values() if a['control_id']==cid)
        reviewed=self.tools.call('locua_review',{'snapshot_id':self.sid,'summary':'Replace Entry',
            'goals':[{'id':'edit','kind':'text','target':'Entry','control_id':cid,'value':'new',
                      'evidence_plane':'editor_buffer'}], 'effects':[{'kind':'goal','goal_id':'edit'}],
            'covers_entire_request':True})
        result=self.tools.call('locua_act',{'scope_id':reviewed['scope_id'],'snapshot_id':self.sid,
            'action_id':action['id'],'value':'new'})
        self.assertEqual(result['status'],'verified',result)
        self.assertLessEqual(self.tools._response_bytes(result),MODEL_RESPONSE_BYTES)
        rows=progressive_ui.exposed_controls(result['fresh_region'])
        self.assertIn('Competitor',{r['name'] for r in rows})
        self.assertIn('Keep',{r['name'] for r in rows})
        self.assertTrue(all(r['id'].startswith(result['snapshot_id']+':') for r in rows))
        self.assertFalse(result['continuation_is_action_authority'])
        self.assertEqual(result['window_id'],self.wid)
        old=self.tools.call('locua_act',{'scope_id':reviewed['scope_id'],'snapshot_id':self.sid,
            'action_id':action['id'],'value':'new'})
        self.assertEqual(old['status'],'refused')
        competitor=next(r for r in rows if r['name']=='Competitor')
        bad=self.tools.call('locua_act',{'scope_id':reviewed['scope_id'],'snapshot_id':result['snapshot_id'],
            'action_id':competitor['actions'][0]['id'],'value':'unrequested'})
        self.assertEqual(bad['status'],'refused');self.assertEqual(self.desktop.other,'protected')
        self.assertEqual(len(self.desktop.executions),1)

    def test_duplicate_structural_regions_are_not_matched_by_ordinal(self):
        before=deepcopy(self.tools._observations[self.sid])
        after=self.desktop.observe(before['target'])['observation']
        clone=deepcopy(after['controls'][0]);clone['id']='duplicate-root';after['controls'].append(clone)
        result=post_action_views(before,before['controls'][1]['id'],after,[])
        self.assertNotIn('fresh_region',result)
        self.assertEqual(result['region_continuity']['status'],'unresolved')
        self.assertIn('overview',result)

    def test_large_region_keeps_continuation_and_all_rows_discoverable(self):
        before=deepcopy(self.tools._observations[self.sid]);after=deepcopy(before)
        for n in range(120):
            c=deepcopy(after['controls'][1]);c['id']='other:'+str(n);c['name']='Other '+str(n)
            c['semantics']={'identifier':c['name']};after['controls'].append(c)
        actions=self.desktop.actions(after)['actions']
        view=post_action_views(before,before['controls'][1]['id'],after,actions)
        page=view['fresh_region'];seen=[]
        self.assertIsNotNone(page['coverage']['continuation'])
        while True:
            seen.extend(c['id'] for c in progressive_ui.exposed_controls(page))
            cursor=page['coverage']['continuation']
            if cursor is None:break
            page=progressive_ui.listing(after,region_id=view['region_continuity']['current_region_id'],
                actions=actions,cursor=cursor)
        self.assertEqual(set(seen),{c['id'] for c in after['controls']})

    def test_profile_is_explicit_and_rejects_legacy(self):
        for profile in ('fresh-region-v1','execution-state-v1'):
            with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as run:
                lib.do('Example',tool_profile=profile,ask=lambda _: 'run')
                self.assertEqual(run.call_args.kwargs['tool_profile'],profile)
        with self.assertRaises(LocuaError):
            lib.do('Example',tool_profile='execution-state-v1',provider='openai',ask=lambda _: 'run')
        with self.assertRaises(LocuaError):
            lib.do('Example',tool_profile='execution-state-v1',task_observations=True,ask=lambda _: 'run')
        with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as run:
            lib.do('Example',ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['tool_profile'], 'step-v2')
        for kw in ({'harness':'legacy'},{'url':'https://example.invalid'},{'tool_profile':'invented'}):
            with self.assertRaises(LocuaError):
                lib.do('Example',ask=lambda _: 'run',**({'tool_profile':'fresh-region-v1'}|kw))

    def test_large_verification_receipt_repages_instead_of_losing_action_result(self):
        before=deepcopy(self.tools._observations[self.sid]);after=self.desktop.observe(before['target'])['observation']
        for n in range(25):
            c=deepcopy(after['controls'][1]);c['id']='other:'+str(n);c['name']='Other '+str(n)
            c['semantics']={'identifier':c['name']};after['controls'].append(c)
        self.tools._retain(after)
        views=post_action_views(before,before['controls'][1]['id'],after,
            list(self.tools._actions[after['snapshot_id']].values()))
        result={'status':'verified','snapshot_id':after['snapshot_id'],'target':after['target'],
            'action_started':True,'verification':{'proof':'x'*2500},**views}
        # Force a complete-envelope overflow which can be addressed by paging.
        over=self.tools._response_bytes(result)
        result['verification']['proof']+='x'*max(0,11200-over)
        page_count=result['fresh_region']['coverage']['returned_count']
        delivered=self.tools._record('locua_act',{'snapshot_id':self.sid},result)
        self.assertEqual(delivered['status'],'verified',delivered)
        self.assertTrue(delivered['action_started'])
        self.assertLessEqual(self.tools._response_bytes(delivered),MODEL_RESPONSE_BYTES)
        self.assertLess(delivered['fresh_region']['coverage']['returned_count'],page_count)
        self.assertIsNotNone(delivered['fresh_region']['coverage']['continuation'])
        self.assertEqual(delivered['verification'],result['verification'])


if __name__=='__main__':unittest.main()
