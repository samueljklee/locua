"""Lease boundaries and concise tool progress; no driver or model starts."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua import amplifier_session as session, desktop_session_lock as locks, engine_adapter
from locua.errors import LocuaError


class Peer:
    def __init__(self, *args, **kwargs):
        self.closed = False
    def close(self):
        self.closed = True
        return []
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class LeaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'private' / 'desktop.lock'
        self.patch = patch.object(locks, 'default_lock_path', return_value=self.path)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.cfg = {'driver_binary': '/fake/bin', 'driver_socket': '/fake/socket',
                    'runtime_python': '/fake/python', 'model_cache': '/fake/cache'}

    def assert_available(self):
        with locks.acquire_desktop_session(): pass

    def test_standalone_owner_excludes_second_owner_then_releases(self):
        with patch('locua.engine.prototype.cua.CuaOwner', Peer):
            first = engine_adapter.owner(self.cfg, self.root/'one')
            try:
                self.assertIsNone(locks.current_desktop_session_token())
                with self.assertRaises(LocuaError) as caught:
                    engine_adapter.owner(self.cfg, self.root/'two')
                self.assertEqual(caught.exception.code, 'desktop_control_busy')
            finally:
                self.assertEqual(first.close(), [])
            first.close()
            self.assert_available()

    def test_explicit_outer_lease_retains_lock_after_nested_owner_close(self):
        with patch('locua.engine.prototype.cua.CuaOwner', Peer), locks.acquire_desktop_session():
            with engine_adapter.owner(self.cfg, self.root/'one') as peer:
                self.assertFalse(peer.closed)
            self.assertTrue(peer.closed)
            with self.assertRaises(LocuaError): locks.acquire_desktop_session()
        self.assert_available()
        self.assertIsNone(locks.current_desktop_session_token())

    def test_owner_construction_error_releases_lease(self):
        with patch('locua.engine.prototype.cua.CuaOwner', side_effect=RuntimeError('construction failed')):
            with self.assertRaisesRegex(RuntimeError, 'construction failed'):
                engine_adapter.owner(self.cfg, self.root/'one')
        self.assert_available()

    def test_owner_cleanup_error_propagates_and_releases_lease(self):
        class Broken(Peer):
            def close(self): raise RuntimeError('cleanup failed')
        with patch('locua.engine.prototype.cua.CuaOwner', Broken):
            connection=engine_adapter.owner(self.cfg,self.root/'one')
            with self.assertRaisesRegex(RuntimeError,'cleanup failed'): connection.close()
        self.assert_available()

    def _run(self, *, execution_error=False, cleanup_error=False):
        checks=[];test=self
        def owns(stage):
            test.assertIsNotNone(locks.current_desktop_session_token())
            with test.assertRaises(LocuaError): locks.acquire_desktop_session()
            checks.append(stage)
        class Provider:
            def __init__(self,**kwargs):
                owns('provider init');self._closed=False;self._service=None
                self.records=[];self.budget_records=[]
            async def close(self): owns('provider close');self._closed=True
        class Desktop:
            def __init__(self,*args,**kwargs):
                owns('desktop init');self.evidence={}
                self.owner=engine_adapter.owner(test.cfg,test.root/'nested')
            def tools(self):return []
            def finalize(self):return {'status':'blocked','reason':'synthetic no completion'}
            def close(self):
                owns('desktop close');self.owner.close()
                if cleanup_error: raise RuntimeError('desktop cleanup failed')
                return {'status':'closed'}
        async def execute(*args,**kwargs):
            owns('execute')
            if execution_error:raise RuntimeError('execution failed')
            return {'response':'synthetic response','session_cleanup':'closed'}
        with ExitStack() as stack:
            stack.enter_context(patch('locua.engine.prototype.cua.CuaOwner',Peer))
            stack.enter_context(patch('locua.amplifier_provider.LocalAmplifierProvider',Provider))
            stack.enter_context(patch('locua.amplifier_tools.DesktopToolset',Desktop))
            stack.enter_context(patch.object(session,'_dependencies'))
            stack.enter_context(patch.object(session,'execute_session',execute))
            report=session.run('A full request',config=self.cfg,out=self.root/'run',ask=lambda _: 'no',progress=lambda _: None)
        self.assert_available()
        self.assertIsNone(locks.current_desktop_session_token())
        self.assertEqual(report['desktop_control_lease'],{'status':'acquired','scope':'per-user desktop','released':True})
        self.assertEqual(report['policy'],'amplifier-standard-tool-loop-v6.5')
        self.assertEqual(report['tool_interface'],'tools-v6.20')
        self.assertEqual(checks,['provider init','desktop init','execute','desktop close','provider close'])
        return report

    def test_session_holds_lease_before_provider_through_cleanup(self):self._run()
    def test_execution_failure_still_closes_nested_outer_and_provider(self):
        self.assertIn('execution failed',self._run(execution_error=True)['reason'])
    def test_cleanup_failure_still_closes_provider_and_outer(self):
        self.assertIn('desktop cleanup failed',self._run(cleanup_error=True)['reason'])

    def test_busy_session_never_constructs_provider(self):
        with locks.acquire_desktop_session(), patch.object(session,'_dependencies'), \
             patch('locua.amplifier_provider.LocalAmplifierProvider') as provider:
            # Deliberately independently acquire outer session: no nested token.
            report=session.run('A request',config=self.cfg,out=self.root/'run',ask=lambda _: 'no',progress=lambda _: None)
        provider.assert_not_called()
        self.assertEqual(report['error']['code'],'desktop_control_busy')
        self.assert_available()


class ProgressTests(unittest.TestCase):
    def summary(self,output,success=True):
        return session._tool_progress({'tool_name':'locua_inspect',
            'result':{'success':success,'output':output,'error':None}})

    def test_progressive_counts_and_continuation_never_show_values(self):
        output={'status':'observed','overview':{'version':'progressive-ui-v1','operation':'overview',
            'counts':{'controls':74,'regions':8},'items':[{'value':'PRIVATE'*1000}],
            'coverage':{'returned_count':4,'remaining_count':3,'continuation':'PRIVATE-CURSOR'}}}
        text=' '.join(self.summary(output))
        for expected in ('4 overview entries returned','3 remaining','74 total controls','8 total regions','continuation available'):
            self.assertIn(expected,text)
        self.assertNotIn('PRIVATE',text)

    def test_list_and_detail_counts(self):
        for operation,label in (('list','list entries'),('control','detail entries')):
            with self.subTest(operation=operation):
                text=' '.join(self.summary({'version':'progressive-ui-v1','status':'ok','operation':operation,
                    'coverage':{'returned_count':2,'remaining_count':0,'continuation':None}}))
                self.assertIn('2 '+label+' returned',text)
                self.assertNotIn('continuation available',text)

    def test_inventory_and_windows_counts(self):
        self.assertIn('2 entries returned of 3; continuation available',' '.join(self.summary(
            {'status':'ok','items':[{},{}],'total':3,'next_start':2})))
        self.assertIn('1 windows returned',' '.join(self.summary({'status':'ok','windows':[{'title':'PRIVATE'}]})))

    def test_refusal_is_bounded_and_terminal_control_characters_removed(self):
        text=' '.join(self.summary({'status':'refused','code':'scope_invalid','reason':'bad\n\x1b[31m'+('x'*1000)},False))
        self.assertIn('scope_invalid',text);self.assertIn('bad',text)
        self.assertNotIn('\n',text);self.assertNotIn('\x1b',text)
        self.assertLess(len(text),310)

    def test_repeat_feedback_explains_no_new_evidence_without_values(self):
        text=' '.join(self.summary({'status':'ok','exploration_feedback':{'code':'repeated_inspection',
            'equivalent_inspection_count':3,'unresolved_needs':['PRIVATE'], 'reason':'PRIVATE'}}))
        self.assertIn('Repeated inspection (3 times): no new UI evidence',text)
        self.assertNotIn('PRIVATE',text)

    def test_unrecognized_text_is_not_parsed_or_printed(self):
        self.assertEqual(session._tool_progress({'result':{'success':True,'output':'{"private":"x"}'}}),['Tool returned.'])
