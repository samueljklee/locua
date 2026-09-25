"""CPU-only candidate transform, strict assertion and frozen replay tests."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('sequence_identity_replay',ROOT/'tools/sequence_identity_replay.py')
replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)


def fixture():
    from locua.amplifier_contracts import obj,S,ID,array
    schema=obj({'scope_id':ID,'snapshot_id':ID,'steps':array(obj({'action_id':ID,'value':S},('action_id',)),minimum=1,maximum=32)},
               ('scope_id','snapshot_id','steps'))
    request={'messages':[{'role':'system','content':'Unchanged policy.'},{'role':'user','content':'Preserve literal  Ω\r\n.'}],
        'tools':[{'name':replay.TOOL,'description':'Original exact description.','parameters':schema},
                 {'name':'locua_act','description':'Do not change me.','parameters':obj({})}], 'stream':False}
    actions={f'a{i}':{'id':f'a{i}','snapshot_id':'s1','control_id':f'c{i}','target':{'pid':1,'window_id':2},
        'kind':'press','name':name,'arithmetic_token':token}
        for i,(name,token) in enumerate([('All Clear','clear'),('1','1'),('2','2'),('Equals','='),(None,None),('1','1')])}
    scope={'status':'approved','target':{'pid':1,'window_id':2},'goals':[{'id':'g','kind':'calculation','expression':'12'}],
        'effects':[{'kind':'goal','goal_id':'g'}],
        'witness':{'known_start':False,'issued_expression_since_clear':'','issued_evaluation':None}}
    rubric={'actions':actions,'scopes':{'scope:1':scope}}
    args={'scope_id':'scope:1','snapshot_id':'s1','steps':[{'action_id':f'a{i}'} for i in range(4)]}
    return request,rubric,args


class AssertionTests(unittest.TestCase):
    def setUp(self):
        self.request,self.rubric,self.args=fixture()
        self.candidate=replay.transform(self.request)
        self.schema=self.candidate['tools'][0]['parameters']
    def asserted(self):
        args=deepcopy(self.args)
        for s in args['steps']:s['expected_control_name']=self.rubric['actions'][s['action_id']]['name']
        return args
    def test_only_three_authored_tool_deltas_no_context_or_single_act_change(self):
        source=deepcopy(self.request);candidate=replay.transform(source)
        reverted=deepcopy(candidate);tool=reverted['tools'][0]
        self.assertEqual(tool['description'],source['tools'][0]['description']+replay.ASSERTION_HELP)
        tool['description']=source['tools'][0]['description']
        item=tool['parameters']['properties']['steps']['items']
        del item['properties']['expected_control_name'];item['required'].remove('expected_control_name')
        self.assertEqual(reverted,source);self.assertEqual(source,self.request)
        with self.assertRaises(ValueError):replay.transform(candidate)
    def test_matching_plan_is_component_only_and_arguments_unchanged(self):
        args=self.asserted();original=deepcopy(args)
        got=replay.score(self.candidate,self.rubric,{'tool_calls':[{'name':replay.TOOL,'arguments':args}]},candidate=True)
        self.assertTrue(got['one_consistent_matching_plan']);self.assertFalse(got['task_completed'])
        self.assertFalse(got['runtime_authority_granted']);self.assertEqual(args,original)
    def test_correct_name_wrong_id_refuses_before_any_prefix_without_remap(self):
        args=self.asserted();args['steps'][1]['action_id']='a2'
        got=replay.validate_sequence(args,self.schema,self.rubric['actions'],candidate=True)
        self.assertEqual(got['category'],'id_name_mismatch');self.assertEqual(got['failed_step'],2)
        self.assertTrue(got['would_refuse_before_input']);self.assertEqual(args['steps'][1]['action_id'],'a2')
    def test_null_is_distinct_from_literal_null_empty_numeric_and_missing(self):
        args={'scope_id':'scope:1','snapshot_id':'s1','steps':[{'action_id':'a4','expected_control_name':None}]}
        self.assertTrue(replay.validate_sequence(args,self.schema,self.rubric['actions'],candidate=True)['valid'])
        for value in ['null','',0,False]:
            bad=deepcopy(args);bad['steps'][0]['expected_control_name']=value
            self.assertFalse(replay.validate_sequence(bad,self.schema,self.rubric['actions'],candidate=True)['valid'])
        del args['steps'][0]['expected_control_name']
        self.assertEqual(replay.validate_sequence(args,self.schema,self.rubric['actions'],candidate=True)['category'],'schema_invalid')
    def test_whitespace_quotes_unicode_case_and_canonical_normalization_exact(self):
        actions=deepcopy(self.rubric['actions']);name='  Ω "one"\r\nCafe\u0301 '
        actions['a1']['name']=name
        args={'scope_id':'scope:1','snapshot_id':'s1','steps':[{'action_id':'a1','expected_control_name':name}]}
        self.assertTrue(replay.validate_sequence(args,self.schema,actions,candidate=True)['valid'])
        for bad_name in [name.strip(),name.replace('\r\n','\n'),name.replace('e\u0301','é'),name.lower()]:
            args['steps'][0]['expected_control_name']=bad_name
            self.assertFalse(replay.validate_sequence(args,self.schema,actions,candidate=True)['valid'])
    def test_duplicate_labels_do_not_collapse_or_choose_action(self):
        args=self.asserted();args['steps'][1]['action_id']='a5'
        got=replay.validate_sequence(args,self.schema,self.rubric['actions'],candidate=True)
        self.assertTrue(got['valid']);self.assertEqual(got['resolved'][1]['id'],'a5')
    def test_unknown_id_snapshot_scope_and_unknown_argument_refuse(self):
        variants=[]
        a=self.asserted();a['steps'][0]['action_id']='invented';variants.append(a)
        a=self.asserted();a['snapshot_id']='s2';variants.append(a)
        a=self.asserted();a['scope_id']='foreign';variants.append(a)
        a=self.asserted();a['steps'][0]['extra']='ignored?';variants.append(a)
        for args in variants:
            got=replay.score(self.candidate,self.rubric,{'tool_calls':[{'name':replay.TOOL,'arguments':args}]},candidate=True)
            self.assertFalse(got['one_consistent_matching_plan'])
    def test_consistent_wrong_names_still_do_not_match_expression(self):
        args=self.asserted();args['steps'][1:3]=list(reversed(args['steps'][1:3]))
        got=replay.score(self.candidate,self.rubric,{'tool_calls':[{'name':replay.TOOL,'arguments':args}]},candidate=True)
        self.assertTrue(got['calls'][0]['valid']);self.assertFalse(got['one_consistent_matching_plan'])
        self.assertEqual(got['calls'][0]['predicted_issuance']['issued_evaluation'],'21')
    def test_wrong_evaluation_cannot_be_erased_by_later_reset_and_correct_result(self):
        args=self.asserted();wrong=deepcopy(args['steps']);wrong[1:3]=list(reversed(wrong[1:3]))
        args['steps']=wrong+args['steps']
        got=replay.score(self.candidate,self.rubric,{'tool_calls':[{'name':replay.TOOL,'arguments':args}]},candidate=True)
        self.assertFalse(got['one_consistent_matching_plan'])
        self.assertEqual(got['calls'][0]['contradictory_evaluation_steps'],[4])
    def test_native_xml_nested_nullable_array_needs_no_decoder_change(self):
        from locua.engine.prototype.qwen38_runtime import parse_tool_output
        args={'scope_id':'scope:1','snapshot_id':'s1','steps':[{'action_id':'a4','expected_control_name':None}]}
        raw='<tool_call>\n<function=locua_act_sequence>\n'+''.join(
            f'<parameter={k}>\n{json.dumps(v) if isinstance(v,list) else v}\n</parameter>\n' for k,v in args.items())+'</function>\n</tool_call>'
        tools=[{'type':'function','function':t} for t in self.candidate['tools']]
        blocks=parse_tool_output(raw,tools)
        self.assertIn('expected_control_name',str(blocks));self.assertIn('None',str(blocks))


class FrozenCasesTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'artifacts/action-sequence-v11-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_actual_exposed_pair_preparation_preserves_originals_and_all_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'freeze';manifest=replay.prepare(ROOT,out)
            self.assertEqual(len(manifest['cases']),4);self.assertEqual(manifest['maximum_provider_calls'],4)
            for cid,_,_ in replay.DEFINITIONS:
                original=replay.read(out/(cid+'-original.json'));candidate=replay.read(out/(cid+'-candidate.json'))
                self.assertEqual(candidate,replay.transform(original))
                self.assertEqual(original['messages'],candidate['messages'])
                entry=next(x for x in manifest['cases'] if x['case_id']==cid and x['variant']=='original')
                self.assertEqual((out/entry['request_file']).read_bytes(),Path(entry['source_file']).read_bytes())
            rubric=replay.read(out/'rubric-private.json')['cases']
            self.assertEqual(rubric['live2-call11']['actions']['action:2:7']['name'],'9')
            self.assertEqual(rubric['live2-call11']['actions']['action:2:18']['name'],'0')
            self.assertEqual(rubric['live2-call11']['preceding_tool_event_count'],10)
            self.assertEqual(rubric['live1-call13']['preceding_tool_event_count'],12)
            self.assertFalse(rubric['live2-call11']['scopes']['scope:1']['witness']['known_start'])
            self.assertTrue(all(c['original_native_archive_parity'] for c in manifest['cases']))
            self.assertEqual((out/'rubric-private.json').stat().st_mode&0o777,0o600)
    @unittest.skipUnless((ROOT / 'artifacts/action-sequence-v11-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_source_or_native_drift_refuses_before_provider_factory(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmp:
                frozen=Path(tmp)/'freeze';replay.prepare(ROOT,frozen)
                @asynccontextmanager
                async def forbidden(out):
                    raise AssertionError('No provider may be created')
                    yield
                with patch.object(replay,'native_payload',return_value={'changed':True}):
                    with self.assertRaisesRegex(ValueError,'native translation'):
                        await replay.run_replays(frozen,Path(tmp)/'native',forbidden)
                manifest=replay.read(frozen/'manifest.json')
                manifest['source_hashes']['tools/sequence_identity_replay.py']='0'*64
                (frozen/'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError,'source identity'):
                    await replay.run_replays(frozen,Path(tmp)/'source',forbidden)
        asyncio.run(exercise())
    @unittest.skipUnless((ROOT / 'artifacts/action-sequence-v11-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_fake_provider_four_calls_no_tool_execution_and_cleanup(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmp:
                frozen=Path(tmp)/'freeze';replay.prepare(ROOT,frozen)
                class Provider:
                    _closed=False
                    def __init__(self):self.calls=[];self.registered={}
                    def register_structured_tool_result(self,call_id,envelope):self.registered[call_id]=deepcopy(envelope)
                    async def complete(self,request):
                        for m in request.messages:
                            if m.role=='tool':assert m.tool_call_id in self.registered
                        self.calls.append(request.model_dump());return SimpleNamespace(model_dump=lambda:{'tool_calls':[]})
                provider=Provider()
                @asynccontextmanager
                async def factory(out):
                    try:yield provider
                    finally:provider._closed=True
                result=await replay.run_replays(frozen,Path(tmp)/'run',factory)
                self.assertEqual(len(provider.calls),4);self.assertTrue(result['provider_closed'])
                self.assertFalse(result['task_completion_claimed']);self.assertEqual(result['desktop_calls'],0)
        asyncio.run(exercise())


if __name__=='__main__':unittest.main()
