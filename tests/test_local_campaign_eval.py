"""Model-free checks of progression scoring; never imports an inference worker."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('campaign',ROOT/'tools/local_campaign_eval.py')
campaign=importlib.util.module_from_spec(spec);spec.loader.exec_module(campaign)
from locua.amplifier_contracts import validate_tool_arguments
from amplifier_core.message_models import ChatRequest  # Import once before scoped module mocks.


class CampaignTests(unittest.TestCase):
    def rubric(self,ready=True):
        return {'scope_id':'scope:example','snapshot_id':'s123','progress_action_ids':['digit2'] if ready else [],
            'all_exposed_actions':[{'action_id':'digit2','symbol':'2'},{'action_id':'digit3','symbol':'3'}] if ready else [],
            'exposed_control_ids':['control2','control3','clear'],'detailed_control_ids':[],
            'visible_pages':[{'snapshot_id':'s123','operation':'list','coverage':{
                'scope':{'region_id':'main','role':'AXButton','query':None},'remaining_count':0}}],
            'needed_scope_issuance_and_next_action_present':ready}
    def evaluate(self,name,args,rubric=None):
        return campaign.evaluate({'tool_calls':[{'name':name,'arguments':args}]},rubric or self.rubric(),validate_tool_arguments)
    def test_progress_does_not_dispatch_or_complete(self):
        r=self.evaluate('locua_act',{'scope_id':'scope:example','snapshot_id':'s123','action_id':'digit2'})
        self.assertTrue(r['immediate_task_progress']);self.assertFalse(r['task_completed']);self.assertEqual(r['desktop_calls'],0)
    def test_other_exposed_digit_is_not_prefix_progress(self):
        r=self.evaluate('locua_act',{'scope_id':'scope:example','snapshot_id':'s123','action_id':'digit3'})
        self.assertFalse(r['immediate_task_progress']);self.assertTrue(r['calls'][0]['grounded_action'])
    def test_wrong_scope_or_snapshot_cannot_get_credit(self):
        for key in ('scope_id','snapshot_id','action_id'):
            args={'scope_id':'scope:example','snapshot_id':'s123','action_id':'digit2'};args[key]='foreign'
            self.assertFalse(self.evaluate('locua_act',args)['immediate_task_progress'])
    def test_status_with_compaction_is_not_automatic_progress(self):
        r=self.evaluate('locua_status',{})
        self.assertEqual(r['calls'][0]['classification'],'context_read_when_needed_state_already_present')
        self.assertTrue(r['read_only_nonprogress']);self.assertFalse(r['immediate_task_progress'])
    def test_missing_current_action_read_is_distinguished(self):
        r=self.evaluate('locua_status',{},self.rubric(False))
        self.assertEqual(r['calls'][0]['classification'],'context_or_action_evidence_acquisition_not_task_progress')
        self.assertTrue(r['calls'][0]['representation_valid'])
    def test_complete_list_repeat_ignores_cosmetic_limit(self):
        r=self.evaluate('locua_inspect',{'operation':'list','snapshot_id':'s123','region_id':'main','role':'AXButton','limit':64})
        self.assertEqual(r['calls'][0]['classification'],'repeated_complete_exposed_page')
    def test_extra_detail_is_safe_read_without_progress_proof(self):
        r=self.evaluate('locua_inspect',{'operation':'control','snapshot_id':'s123','control_id':'clear'})
        self.assertEqual(r['calls'][0]['classification'],'additional_detail_without_established_progress_need')
        self.assertTrue(r['calls'][0]['representation_valid']);self.assertFalse(r['immediate_task_progress'])
    def test_schema_error_and_multiple_calls_never_get_progress(self):
        r=self.evaluate('locua_act',{'scope_id':'scope:example','snapshot_id':'s123','action_id':'digit2','invented':True})
        self.assertEqual(r['calls'][0]['classification'],'invalid_arguments')
        calls=[{'name':'locua_act','arguments':{'scope_id':'scope:example','snapshot_id':'s123','action_id':'digit2'}}]*2
        self.assertFalse(campaign.evaluate({'tool_calls':calls},self.rubric(),validate_tool_arguments)['immediate_task_progress'])
    def test_noop_text_response_not_a_completed_action(self):
        r=campaign.evaluate({'content':[{'type':'text','text':'Done'}]},self.rubric(),validate_tool_arguments)
        self.assertFalse(r['immediate_task_progress']);self.assertFalse(r['task_completed'])
    def test_source_rubric_immutability(self):
        rub=self.rubric();before=deepcopy(rub);self.evaluate('locua_status',{},rub);self.assertEqual(rub,before)
    def test_exclusive_write_preserves_frozen_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'baseline.json';campaign.write(path,{'original':True})
            with self.assertRaises(FileExistsError):campaign.write(path,{'overwrite':True})
            self.assertEqual(campaign.read(path),{'original':True})
    def test_duplicate_or_nonfinite_json_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.json'
            for text in ('{"x":1,"x":2}','{"x":NaN}'):
                p.write_text(text)
                with self.assertRaises(ValueError):campaign.read(p)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failure=None):
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys
        instances=[];completed=[]
        class Provider:
            def __init__(self):self._closed=False;self.registrations=[]
            def register_structured_tool_result(self,*args):self.registrations.append(args)
            async def complete(self,request):
                completed.append(request.model_dump())
                if failure is not None:raise failure
                return SimpleNamespace(model_dump=lambda:{'content':[{'type':'text','text':'No operation'}]})
            async def close(self):self._closed=True
        def factory(provider,model,out,**kwargs):
            self.assertEqual((provider,model),('local','qwen38'))
            self.assertEqual(kwargs,{'config':'config.json','thinking':False,'max_calls':1})
            obj=Provider();instances.append(obj);return obj
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=root/'freeze';frozen.mkdir()
            cases=[]
            for name in ('base','candidate','saturated'):
                case=frozen/name;case.mkdir();campaign.write(case/'manifest.json',{'case':name})
                cases.append({'id':name,'manifest_sha256':campaign.sha((case/'manifest.json').read_bytes())})
            campaign.write(frozen/'suite.json',{'helper_sha256':campaign.sha(Path(campaign.__file__).read_bytes()),'cases':cases})
            request={'messages':[{'role':'user','content':'Perform the reviewed task'}]}
            def load(_):return deepcopy(request),CampaignTests().rubric(),validate_tool_arguments,{'request_sha256':campaign.sha(request)}
            modules={'provider_connection':SimpleNamespace(make_provider=factory),
                'model_comparison':SimpleNamespace(terminal_provider_failure=lambda p,e:{'basis':'EOF'} if isinstance(e,EOFError) else None)}
            with patch.dict(sys.modules,modules),patch.object(campaign,'load_case',side_effect=load):
                result=await campaign.run_suite(frozen,root/'output','config.json')
            self.assertTrue(all(p._closed for p in instances))
            self.assertEqual(result['desktop_calls'],0)
            self.assertEqual(result['planned_calls'],3)
            return result,instances,completed
    async def test_existing_provider_serialized_once_per_case_and_closed(self):
        result,providers,calls=await self.exercise()
        self.assertEqual(len(providers),3);self.assertEqual(len(calls),3)
        self.assertEqual(result['attempted_calls'],3);self.assertEqual(result['unrun_calls'],0)
        self.assertTrue(all(row['status']=='returned' for row in result['results']))
    async def test_terminal_transport_failure_keeps_fixed_unrun_denominator(self):
        result,providers,calls=await self.exercise(EOFError('test interrupted transport'))
        self.assertEqual(len(providers),1);self.assertEqual(len(calls),1)
        self.assertEqual(result['attempted_calls'],1);self.assertEqual(result['unrun_calls'],2)
        self.assertEqual([r['status'] for r in result['results']],['failed','unrun','unrun'])


if __name__=='__main__':unittest.main()
