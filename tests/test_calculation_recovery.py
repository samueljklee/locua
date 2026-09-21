"""Generic calculation evidence recovery; synthetic UI only, no app recipes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from locua.amplifier_tools import DesktopToolset
from locua.goal_verification import matches_binding, matches_readback_identity
from test_amplifier_tools import Desktop
from test_single_readout_binding import tree

class ChangingReadoutDesktop(Desktop):
    def __init__(self):
        super().__init__();self.finished=False;self.result='15';self.expression='3*5';self.reset_name='Clear'
    def observe(self,target):
        self.sequence+=1
        o=tree(self.expression if self.finished else '0',snapshot='r'+str(self.sequence),
               parent_name='Tool surface',labels=(self.reset_name,'3'),
               extra_readouts=(self.result,) if self.finished else ())
        return {'status':'observed','observation':o}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.desktop=ChangingReadoutDesktop();self.questions=[]
        def ask(prompt,kind):self.questions.append((prompt,kind));return 'run'
        self.tools=DesktopToolset({},Path(self.tmp.name)/'trace','Calculate 3*5.',ask,desktop=self.desktop)
        self.addCleanup(self.tools.close)
        app=self.call('apps')['items'][0]['app_id']
        self.window=self.call('windows',app_id=app)['windows'][0]['window_id']
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        c=self.readouts()[0]
        r=self.call('review',snapshot_id=self.sid,summary='Calculate and verify the displayed outcome.',
            goals=[{'id':'g','kind':'calculation','expression':'3*5','target':'Output','evidence_plane':'display','control_id':c['id']}],
            effects=[{'kind':'goal','goal_id':'g'}],covers_entire_request=True)
        self.assertEqual(r['status'],'approved',r);self.scope=r['scope_id'];self.review=r
    def call(self,name,**args):return self.tools.call('locua_'+name,args)
    def readouts(self):return [c for c in self.tools._observations[self.sid]['controls'] if c['role']=='AXStaticText']
    def finished(self,*,witness=True):
        if witness:
            for token in ['clear','3','*','5','=']:
                self.tools._scopes[self.scope]['witness'].record(token,snapshot_id=self.sid,descriptor={})
        self.desktop.finished=True
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
    def recover(self,index=1):
        return self.call('verify',scope_id=self.scope,goal_id='g',snapshot_id=self.sid,control_id=self.readouts()[index]['id'])
    def test_layout_change_requires_explicit_identity_selection_and_independent_capture(self):
        original=deepcopy(self.tools._scopes[self.scope]['goals']);w=self.tools._scopes[self.scope]['witness']
        self.finished();initial_sid=self.sid
        r=self.recover();self.assertEqual(r['status'],'verified',r)
        self.assertEqual(self.tools._scopes[self.scope]['goals'],original)
        self.assertIs(self.tools._scopes[self.scope]['witness'],w)
        self.assertEqual(len(self.questions),1);self.assertEqual(self.desktop.executions,[])
        proof=r['scopes'][self.scope]['goals'][0]['evidence']
        self.assertNotEqual(proof['snapshot_id'],initial_sid)
        self.assertFalse(r['binding_recovery']['input_authority_changed'])
        binding=self.tools._scopes[self.scope]['bindings']['g'];o=self.tools._observations[proof['snapshot_id']]
        self.assertTrue(matches_readback_identity(binding,o,proof['control_id']))
        self.assertFalse(matches_binding(binding,o,proof['control_id']))
    def test_no_expected_answer_search_when_selected_binding_exists(self):
        self.finished();self.desktop.expression='99'
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        first=self.recover(0);self.assertEqual(first['status'],'unverified')
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        second=self.recover(1);self.assertEqual(second['code'],'existing_result_binding_present')
        self.assertEqual(len(self.tools._scopes[self.scope]['binding_revisions']),1)
    def test_missing_input_witness_cannot_be_replaced_by_matching_number(self):
        self.finished(witness=False);r=self.recover()
        self.assertEqual(r['code'],'arithmetic_issuance_unproved');self.assertEqual(self.desktop.executions,[])
        self.assertNotIn('binding_revisions',self.tools._scopes[self.scope])
    def test_existing_correct_binding_does_not_authorize_new_choice(self):
        for token in ['clear','3','*','5','=']:
            self.tools._scopes[self.scope]['witness'].record(token,snapshot_id=self.sid,descriptor={})
        r=self.recover(0);self.assertEqual(r['code'],'existing_result_binding_present')
    def test_zero_does_not_hide_known_start_prerequisite_or_allow_first_digit(self):
        self.assertFalse(self.review['arithmetic_input']['known_start'])
        action=next(a for a in self.tools._actions[self.sid].values() if a['description']=='3')
        r=self.call('act',scope_id=self.scope,snapshot_id=self.sid,action_id=action['id'])
        self.assertEqual(r['code'],'arithmetic_start_unproved');self.assertFalse(r['action_started'])
        self.assertEqual(self.desktop.executions,[])
    def test_generic_entry_clear_does_not_authorize_digits_until_explicit_full_reset(self):
        action=next(a for a in self.tools._actions[self.sid].values() if a['description']=='Clear')
        r=self.call('act',scope_id=self.scope,snapshot_id=self.sid,action_id=action['id'])
        self.assertEqual(r['status'],'dispatched',r)
        self.assertFalse(r['arithmetic_input']['known_start'])
        self.sid=r['snapshot_id'];count=len(self.desktop.executions)
        digit=next(a for a in self.tools._actions[self.sid].values() if a['description']=='3')
        refused=self.call('act',scope_id=self.scope,snapshot_id=self.sid,action_id=digit['id'])
        self.assertEqual(refused['code'],'arithmetic_start_unproved')
        self.assertEqual(len(self.desktop.executions),count)
        self.desktop.reset_name='All Clear'
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        reset=next(a for a in self.tools._actions[self.sid].values() if a['description']=='All Clear')
        result=self.call('act',scope_id=self.scope,snapshot_id=self.sid,action_id=reset['id'])
        self.assertEqual(result['status'],'dispatched',result)
        self.assertTrue(result['arithmetic_input']['known_start'])
    def test_ambiguous_original_readout_cannot_be_replaced_with_matching_alternative(self):
        class AmbiguousDesktop(Desktop):
            ambiguous=False
            def observe(self,target):
                result=super().observe(target);o=result['observation']
                original=next(c for c in o['controls'] if c['name']=='Result')
                if self.ambiguous:
                    twin=deepcopy(original);twin['id']=o['snapshot_id']+':90'
                    alternate=deepcopy(original)
                    alternate.update(id=o['snapshot_id']+':91',name='Alternate',value='15',semantics={'identifier':'alternate'})
                    o['controls'] += [twin,alternate]
                return result
        desktop=AmbiguousDesktop()
        tools=DesktopToolset({},Path(self.tmp.name)/'ambiguity','Calculate 3*5.',lambda *_:'run',desktop=desktop)
        self.addCleanup(tools.close)
        def call(name,**args):return tools.call('locua_'+name,args)
        app=call('apps')['items'][0]['app_id'];window=call('windows',app_id=app)['windows'][0]['window_id']
        sid=call('observe',window_id=window)['snapshot_id']
        original=next(c for c in tools._observations[sid]['controls'] if c['name']=='Result')
        review=call('review',snapshot_id=sid,summary='Calculate 3*5.',
            goals=[{'id':'g','kind':'calculation','expression':'3*5','target':'Result','evidence_plane':'display','control_id':original['id']}],
            effects=[{'kind':'goal','goal_id':'g'}],covers_entire_request=True)
        scope=tools._scopes[review['scope_id']];binding=deepcopy(scope['bindings']['g'])
        for token in ['clear','3','*','5','=']:scope['witness'].record(token,snapshot_id=sid,descriptor={})
        desktop.ambiguous=True;sid=call('observe',window_id=window)['snapshot_id']
        alternate=next(c for c in tools._observations[sid]['controls'] if c['name']=='Alternate')
        result=call('verify',scope_id=review['scope_id'],goal_id='g',snapshot_id=sid,control_id=alternate['id'])
        self.assertEqual(result['code'],'existing_result_binding_present')
        self.assertEqual(scope['bindings']['g'],binding)
        self.assertNotIn('binding_revisions',scope)
        self.assertEqual(desktop.executions,[])

    def test_recovery_argument_pair_and_read_only_type_are_required(self):
        r=self.call('verify',scope_id=self.scope,goal_id='g',snapshot_id=self.sid)
        self.assertEqual(r['code'],'argument_contract_invalid')
        self.finished();button=next(c for c in self.tools._observations[self.sid]['controls'] if c['role']=='AXButton')
        r=self.call('verify',scope_id=self.scope,goal_id='g',snapshot_id=self.sid,control_id=button['id'])
        self.assertEqual(r['status'],'refused');self.assertIn('read-only',r['reason'])

if __name__=='__main__':unittest.main()
