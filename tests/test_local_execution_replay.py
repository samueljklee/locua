"""CPU-only provenance, fixed-boundary and scoring checks for iteration2."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import local_execution_replay as replay
from amplifier_core.message_models import ChatRequest
from locua.amplifier_contracts import validate_tool_arguments
from locua.execution_state import render_execution_facts


class ScoringTests(unittest.TestCase):
    def rubric(self):
        return {'kind':'no_approved_scope','scope_count':0,'snapshot_id':'s1',
            'expression':'8+4','reviewable_display_ids':['readout'],'detailed_control_ids':['readout']}
    def response(self,name,args):return {'tool_calls':[{'name':name,'arguments':args}]}
    def review(self):
        return {'snapshot_id':'s1','summary':'Calculate the original expression in the observed window.',
            'goals':[{'id':'result','kind':'calculation','target':'observed result',
                'control_id':'readout','evidence_plane':'display','expression':'8+4'}],
            'effects':[{'kind':'goal','goal_id':'result'}],'covers_entire_request':True}
    def score(self,name,args):return replay.evaluate(self.response(name,args),self.rubric(),validate_tool_arguments)
    def test_zero_scope_status_is_not_progress(self):
        result=self.score('locua_status',{'operation':'summary'})
        self.assertFalse(result['immediate_task_progress'])
        self.assertEqual(result['calls'][0]['classification'],'recovering_absent_scope_despite_explicit_zero')
    def test_faithful_review_is_only_proposal_progress(self):
        result=self.score('locua_review',self.review())
        self.assertTrue(result['immediate_task_progress']);self.assertFalse(result['task_completed'])
        self.assertEqual(result['desktop_calls'],0)
        formatted=self.review();formatted['goals'][0]['expression']=' 8 + 4 '
        self.assertTrue(self.score('locua_review',formatted)['immediate_task_progress'])
    def test_review_other_expression_wrong_snapshot_or_unrequested_effect_refused_credit(self):
        for key,value in [('expression','8+5'),('control_id','other')]:
            args=self.review();args['goals'][0][key]=value
            self.assertFalse(self.score('locua_review',args)['immediate_task_progress'])
        args=self.review();args['snapshot_id']='old'
        self.assertFalse(self.score('locua_review',args)['immediate_task_progress'])
        args=self.review();args['effects'].append({'kind':'press','control_id':'save','purpose':'Save'})
        self.assertFalse(self.score('locua_review',args)['immediate_task_progress'])
    def test_no_scope_action_and_verification_do_not_invent_authority(self):
        for name,args in [('locua_act',{'scope_id':'invented','snapshot_id':'s1','action_id':'key'}),
                          ('locua_verify',{'scope_id':'invented'})]:
            result=self.score(name,args)
            self.assertFalse(result['immediate_task_progress'])
            self.assertEqual(result['calls'][0]['classification'],'invented_or_unapproved_scope')
    def test_repeated_detail_and_multiple_calls_no_progress(self):
        result=self.score('locua_inspect',{'operation':'control','snapshot_id':'s1','control_id':'readout'})
        self.assertEqual(result['calls'][0]['classification'],'repeated_already_delivered_detail')
        response=self.response('locua_review',self.review());response['tool_calls']*=2
        self.assertFalse(replay.evaluate(response,self.rubric(),validate_tool_arguments)['immediate_task_progress'])


class ReconstructionTests(unittest.TestCase):
    def setup_run(self,root):
        (root/'session').mkdir();(root/'desktop').mkdir()
        event={'sequence':1,'tool':'locua_windows','input':{'app_id':'a'},
            'result':{'status':'ok','windows':[{'window_id':'window:public','target':{'pid':1,'window_id':2}}]}}
        replay.write(root/'desktop/event-001.json',event)
        events=[{'event':'provider:request','at_ns':1,'data':{}},
            {'event':'tool:pre','at_ns':2,'data':{'tool_call_id':'c1','tool_name':'locua_windows','tool_input':{'app_id':'a'}}},
            {'event':'tool:post','at_ns':4,'data':{'tool_call_id':'c1'}},
            {'event':'provider:request','at_ns':10,'data':{}},
            {'event':'tool:pre','at_ns':11,'data':{'tool_call_id':'FUTURE','tool_name':'locua_review','tool_input':{}}}]
        replay.write(root/'session/session.json',{'request':'original request','events':events})
        replay.write(root/'desktop/observation-old.json',{'snapshot_id':'old','observed_at_ns':3,
            'target':{'pid':1,'window_id':2},'coverage':{'complete':True}})
        replay.write(root/'desktop/observation-future.json',{'snapshot_id':'FUTURE','observed_at_ns':12,
            'target':{'pid':1,'window_id':2},'coverage':{'complete':True},'private_future_answer':'not allowed'})
    def test_future_events_and_captures_never_enter_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_run(root)
            owner,provenance=replay.reconstruct(root,2)
            facts=render_execution_facts(owner)
            self.assertEqual(len(owner.evidence['events']),1)
            self.assertEqual(list(owner._observations),['old'])
            self.assertEqual(owner._scopes,{})
            self.assertNotIn('FUTURE',facts);self.assertNotIn('private_future_answer',facts)
            self.assertEqual(provenance['completed_tool_events_before_call'],1)
    def test_mismatched_framework_and_desktop_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_run(root)
            path=root/'desktop/event-001.json';event=replay.read(path);event['input']['app_id']='foreign'
            path.write_text(json.dumps(event))
            with self.assertRaisesRegex(ValueError,'sequence differ'):replay.reconstruct(root,2)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_installed_persistence_wrapper_preserves_facts_exactly(self):
        import amplifier_module_loop_streaming as module
        body='{"version":"test","action_authority":false,"literal":"  exact  "}'
        result=await replay.persisted_message(body)
        self.assertEqual(result['role'],'user')
        self.assertEqual(result['content'],module._wrap_reminders(body,tail=True,header=True))
        self.assertEqual(result['metadata'],{'ephemeral':True,'persisted':True,'reminder_placement':'tail'})
        self.assertIn(body,result['content'])
    async def test_cancellation_retains_attempt_unrun_and_closes_provider(self):
        import model_comparison
        made=[]
        class Provider:
            _closed=False
            async def complete(self,request):raise asyncio.CancelledError()
            async def close(self):self._closed=True
        def factory(*args,**kwargs):
            p=Provider();made.append(p);return p
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=root/'freeze';frozen.mkdir();replay.write(frozen/'suite.json',{})
            cases=[]
            for name in ('a','b'):
                folder=frozen/name;folder.mkdir();replay.write(folder/'candidate-request.json',{
                    'messages':[{'role':'user','content':'original'}]})
                cases.append((folder,{'id':name,'candidate_request_sha256':'test'},validate_tool_arguments))
            with patch.dict(sys.modules,{'provider_connection':SimpleNamespace(make_provider=factory)}),\
                    patch.object(replay,'load',return_value=({},cases)):
                with self.assertRaises(asyncio.CancelledError):await replay.run(frozen,root/'out')
            summary=replay.read(root/'out/summary.json')
            self.assertEqual(summary['attempted_calls'],1);self.assertEqual(summary['unrun_calls'],1)
            self.assertEqual(summary['results'][0]['error_type'],'CancelledError')
            self.assertTrue(made[0]._closed);self.assertTrue((root/'out/a.json').exists())


if __name__=='__main__':unittest.main()
