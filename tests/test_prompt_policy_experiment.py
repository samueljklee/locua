"""CPU-only policy isolation, strict fidelity, cost authorization and lifecycle."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import prompt_policy_experiment as exp
from locua.instruction_policy import BASELINE,PROFILES,HELP
from amplifier_core.message_models import ChatResponse


def request():
    return {'messages':[{'role':'system','content':BASELINE+exp.MARKER+'Keep exact  spaces\n.'},{'role':'user','content':'Keep exact  spaces\n.'}],
        'tools':[{'name':'locua_act','description':'original','parameters':{'type':'object','properties':{'scope_id':{'type':'string'},'snapshot_id':{'type':'string'},'action_id':{'type':'string'},'value':{'type':'string'}},'required':['scope_id','snapshot_id','action_id'],'additionalProperties':False}},
                 {'name':'locua_status','description':'recover','parameters':{'type':'object','properties':{'operation':{'type':'string'}},'additionalProperties':False}}], 'stream':False}


class IsolationTests(unittest.TestCase):
    def test_baseline_exact_and_concise_only_first_system_changed(self):
        r=request();before=deepcopy(r)
        b,_=exp.transform(r,'baseline');c,_=exp.transform(r,'concise-v1')
        self.assertEqual(b,r);self.assertEqual(r,before)
        self.assertNotEqual(c['messages'][0]['content'],r['messages'][0]['content'])
        c['messages'][0]['content']=r['messages'][0]['content'];self.assertEqual(c,r)
    def test_unknown_prefix_refused(self):
        r=request();r['messages'][0]['content']='unexpected'+r['messages'][0]['content']
        with self.assertRaises(ValueError):exp.transform(r,'concise-v1')
    def test_opaque_hosted_blocks_and_literal_compaction_are_not_rewritten(self):
        r=request();r['messages'] += [{'role':'assistant','content':[{'type':'thinking','thinking':'opaque','signature':'signature'}]}, {'role':'tool','tool_call_id':'old','content':'[compacted arbitrary not JSON]'}]
        c,_=exp.transform(r,'concise-v1');self.assertEqual(c['messages'][1:],r['messages'][1:])
        class Provider:
            def register_structured_tool_result(self,*_):raise AssertionError('literal must stay literal')
        exp.register_results(Provider(),r)
    def test_help_changes_only_top_level_description_not_schema(self):
        r=request();r['tools']=[{'name':name,'description':'old','parameters':{'type':'object','properties':{'help':{'description':'nested unchanged'}}}} for name in HELP]
        c,metadata=exp.transform(r,'concise-help-v1')
        self.assertTrue(metadata['top_level_tool_help_changed'])
        self.assertEqual([t['parameters'] for t in c['tools']],[t['parameters'] for t in r['tools']])
        self.assertTrue(all(t['description']==HELP[t['name']] for t in c['tools']))
    def test_help_missing_inventory_refused(self):
        with self.assertRaises(ValueError):exp.transform(request(),'concise-help-v1')
    def test_template_count_requests_plain_token_list_not_batch_encoding(self):
        class Tokenizer:
            def apply_chat_template(self,*_,**kwargs):
                self.kwargs=kwargs
                return list(range(27)) if kwargs.get('return_dict') is False else {'input_ids':list(range(27)),'attention_mask':[1]*27}
        tok=Tokenizer();fp=exp.footprints(request(),tokenizer=tok)
        self.assertEqual(fp['local_native']['full_template_input_tokens'],27)
        self.assertTrue(tok.kwargs['tokenize']);self.assertFalse(tok.kwargs['enable_thinking'])
    def test_pending_capability_gaps_never_scored_as_live(self):
        rows=exp.pending_scenarios();navigation=next(x for x in rows if x['id']=='navigation-reveal')
        self.assertIn('CAPABILITY GAP',navigation['capability'])
        self.assertEqual(sum(x['split']=='heldout' for x in rows),2)


class GradingTests(unittest.TestCase):
    def rubric(self):
        return {'kind':'approved-text','facts':{'snapshot_ids':['s'],'control_ids':['c'],'region_ids':[],'window_ids':[],'actions':{'a':{'snapshot_id':'s','kind':'set_text'}},'past_inspections':[]},'scope_ids_approved_before_decision':['scope'],'scope_id':'scope','snapshot_id':'s','action_ids':['a'],'value':'  exact\nvalue  '}
    def score(self,args,rubric=None,name='locua_act'):
        return exp.evaluate({'tool_calls':[{'name':name,'arguments':args}]},rubric or self.rubric(),request())
    def test_exact_whitespace_no_trim_or_terminal_newline_credit(self):
        base={'scope_id':'scope','snapshot_id':'s','action_id':'a','value':'  exact\nvalue  '}
        self.assertTrue(self.score(base)['effect_advancement'])
        for value in ('exact\nvalue','  exact\nvalue  \n','  exact value  '):
            self.assertFalse(self.score({**base,'value':value})['effect_advancement'])
    def test_wrong_scope_and_stale_action_fail_without_execution(self):
        for args in ({'scope_id':'invented','snapshot_id':'s','action_id':'a'},{'scope_id':'scope','snapshot_id':'s','action_id':'old'}):
            result=self.score(args);self.assertFalse(result['effect_advancement']);self.assertFalse(result['task_completed']);self.assertEqual(result['desktop_actions'],0)
    def test_missing_reference_read_is_evidence_not_effect_completion(self):
        rubric=self.rubric();rubric['kind']='missing-goal'
        r=self.score({'operation':'summary'},rubric,'locua_status')
        self.assertTrue(r['justified_evidence_acquisition']);self.assertFalse(r['effect_advancement']);self.assertFalse(r['task_completed'])
    def test_already_delivered_status_not_progress(self):
        rubric=self.rubric();rubric['kind']='missing-goal';rubric['facts']['past_inspections']=[{'name':'locua_status','arguments':{'operation':'summary'}}]
        self.assertFalse(self.score({'operation':'summary'},rubric,'locua_status')['justified_evidence_acquisition'])
    def test_grouping_losing_divide_distinct_from_supported_alternative(self):
        rubric=self.rubric();rubric.update(kind='grouping',grouping_losing_action_ids=['a'])
        r=self.score({'scope_id':'scope','snapshot_id':'s','action_id':'a'},rubric)
        self.assertEqual(r['calls'][0]['classification'],'continues_expression_with_requested_grouping_lost')
        self.assertFalse(r['effect_advancement'])
    def test_multiple_calls_cannot_get_one_step_progress_credit(self):
        args={'scope_id':'scope','snapshot_id':'s','action_id':'a','value':'  exact\nvalue  '}
        r=exp.evaluate({'tool_calls':[{'name':'locua_act','arguments':args}]*2},self.rubric(),request())
        self.assertFalse(r['effect_advancement']);self.assertFalse(r['single_tool_policy_observed'])


class AuthorizationTests(unittest.TestCase):
    def fixture(self,root):
        old=root/'old.json';exp.write(old,{'reserved_unknown':261210});manifest=root/'manifest.json';exp.write(manifest,{'budget':{'ledger':exp.record(old)}})
        doc={'authorized':True,'authorization_basis':'Explicit test user renewal','provider':'openai','model':'gpt-5.6-sol','reasoning_effort':'medium','maximum_calls':16,'deadline_utc':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'manifest_sha256':exp.sha(manifest.read_bytes()),'ledger':str(root/'new.json'),'cap_usd':12,'new_budget_explicitly_authorized':True,'prior_ledger':exp.record(old)}
        return old,manifest,doc
    def test_missing_auth_fails_before_provider_or_ledger(self):
        with self.assertRaisesRegex(ValueError,'renewed authorization'):exp.authorize(None,'absent',8)
    def test_new_cap_requires_explicit_old_ledger_link_and_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old,manifest,doc=self.fixture(root);auth=root/'auth.json';exp.write(auth,doc)
            before=old.read_bytes()
            with patch.object(exp,'LEDGER',old):self.assertEqual(exp.authorize(auth,manifest,16)['cap_usd'],12)
            self.assertEqual(old.read_bytes(),before);self.assertFalse(Path(doc['ledger']).exists())
    def test_expiry_source_mismatch_missing_authority_and_cap_rejected(self):
        for change in ({'deadline_utc':'2020-01-01T00:00:00Z'},{'manifest_sha256':'wrong'},{'new_budget_explicitly_authorized':False},{'cap_usd':13},{'maximum_calls':8}):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);old,manifest,doc=self.fixture(root);doc.update(change);auth=root/'auth.json';exp.write(auth,doc)
                with patch.object(exp,'LEDGER',old),self.assertRaises(ValueError):exp.authorize(auth,manifest,16)
    def test_changed_old_ledger_blocks_new_allocation_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old,manifest,doc=self.fixture(root);auth=root/'auth.json';exp.write(auth,doc);old.write_text('{}')
            with patch.object(exp,'LEDGER',old),self.assertRaisesRegex(ValueError,'immutable ledger'):exp.authorize(auth,manifest,16)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self,error):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name);freeze=root/'freeze';freeze.mkdir();exp.write(freeze/'manifest.json',{})
        exp.write(freeze/'r.json',request());exp.write(freeze/'rubric.json',GradingTests().rubric())
        cases=[{'id':cid,'status':'retained_replay_ready','initial_screen':True,'local_eligible':True,'hosted_eligible':False,'requests':{'baseline':{'file':'r.json'}},'rubric_file':'rubric.json'} for cid in ('first','second')]
        made=[]
        @asynccontextmanager
        async def factory(*_):
            class Provider:
                _closed=False
                records=[]
                async def complete(self,r):
                    if len(made)==1:raise error
                    return ChatResponse(content=[])
                async def close(self):self._closed=True
            p=Provider();made.append(p)
            try:yield p
            finally:await p.close()
        with patch.object(exp,'load',return_value={'cases':cases,'profiles':['baseline']}):
            if isinstance(error,asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):await exp.run(freeze,root/'out',provider='local',factory=factory)
                result=exp.read(root/'out/summary.json')
            else:result=await exp.run(freeze,root/'out',provider='local',factory=factory)
        self.assertTrue(all(p._closed for p in made));return result
    async def test_healthy_parse_failure_not_mislabeled_closed_transport(self):
        result=await self.invoke(ValueError('invalid generated tool'))
        self.assertEqual(result['attempted_provider_calls'],2);self.assertEqual(result['unrun_calls'],0);self.assertIsNone(result['terminal_failure'])
    async def test_eof_stops_without_extra_generation(self):
        result=await self.invoke(EOFError('worker ended'))
        self.assertEqual(result['attempted_provider_calls'],1);self.assertEqual(result['unrun_calls'],1)
    async def test_cancel_preserves_fixed_denominator_and_cleanup(self):
        result=await self.invoke(asyncio.CancelledError())
        self.assertEqual(result['attempted_provider_calls'],1);self.assertEqual(result['unrun_calls'],1)
        self.assertEqual(result['rows'][0]['error_type'],'CancelledError')
    async def test_cleanup_session_even_when_provider_close_raises(self):
        from locua import amplifier_session,provider_selection
        closed=[]
        class Provider:
            def __init__(self,*_,**__):pass
            async def mount(self,*_):pass
            async def close(self):closed.append('provider');raise RuntimeError('close failure')
        class Session:
            def __init__(self,*_):self.coordinator=object()
            async def initialize(self):pass
            async def cleanup(self):closed.append('session')
        with patch.object(provider_selection,'HostedAmplifierProvider',Provider),patch.object(amplifier_session,'_dependencies',return_value=(Session,None)):
            with self.assertRaisesRegex(RuntimeError,'close failure'):
                async with exp.provider_context('openai','gpt-5.6-sol','unused',None,{'ledger':'unused','cap_usd':12}):pass
        self.assertEqual(closed,['provider','session'])

if __name__=='__main__':unittest.main()
