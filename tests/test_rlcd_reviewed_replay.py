"""CPU-only replay coverage/provenance; no inference or desktop operations."""
from copy import deepcopy
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from locua.engine.prototype.perception import normalize_observation
from locua.engine.prototype.regions import catalog_regions

SPEC=importlib.util.spec_from_file_location('rlcd_reviewed_replay',Path(__file__).parents[1]/'tools/rlcd_reviewed_replay.py')
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)


def fixture():
    nodes=[]
    for index,(role,name,parent) in enumerate([
        ('AXWindow','Synthetic',None),('AXGroup','First area',0),('AXButton','Duplicate',1),
        ('AXGroup','Other area',0),('AXButton','Duplicate',3),('AXButton','Unrelated',3)]):
        n={'role':role,'label':name,'element_index':index,'element_token':f's00000001:{index}','enabled':True,
            'actions':['AXPress'] if role=='AXButton' else []}
        if parent is not None:n['parent_index']=parent
        nodes.append(n)
    obs=normalize_observation({'pid':1,'window_id':2,'snapshot_id':'s00000001','elements':nodes,
        'elements_complete':True,'tree_markdown':''},kind='native',expected_target={'pid':1,'window_id':2},observed_at_ns=1)
    actions=[]
    for c in obs['controls']:
        if c['role']=='AXButton':actions.append({'id':'action:'+c['id'],'kind':'press','control_id':c['id'],
            'snapshot_id':obs['snapshot_id'],'handle':obs['handles'][c['id']],'target':obs['target'],'name':c['name'],
            'role':c['role'],'description':c['name'],'requires_value':False})
    region=next(r for r in catalog_regions(obs)['regions'] if r['label']=='First area')
    event={'sequence':2,'tool':'locua_inspect','input':{'operation':'list','region_id':region['id'],'snapshot_id':obs['snapshot_id']},'result':{}}
    return obs,actions,event


class ReplayTests(unittest.TestCase):
    def test_full_catalog_and_observation_immutable(self):
        obs,actions,event=fixture();before=deepcopy((obs,actions,event))
        p,routes=probe.project_case(obs,actions,[event],'full')
        self.assertEqual([c['id'] for c in p['candidates']],[c['id'] for c in actions])
        self.assertEqual(routes,{})
        self.assertEqual(p['provenance']['coverage']['omitted_controls'],0)
        self.assertEqual(before,(obs,actions,event))

    def test_region_retains_duplicate_and_all_outside_routes(self):
        obs,actions,event=fixture();before=deepcopy((obs,actions,event))
        p,routes=probe.project_case(obs,actions,[event],'retained-region')
        region=p['provenance']['region'];self.assertIn('native:s00000001:4',region['competing_control_ids'])
        self.assertEqual(len(routes),len(catalog_regions(obs)['regions']))
        partition=p['provenance']['candidate_partition'];self.assertEqual(partition['coverage']['unaccounted_candidates'],0)
        outside={c['candidate_id'] for c in partition['outside_region_candidates']}
        direct={c['id'] for c in p['candidates']} - set(routes)
        self.assertEqual(outside|direct,{c['id'] for c in actions})
        self.assertIn('OTHER REGIONS REMAIN ACCESSIBLE',p['observation_summary'])
        self.assertEqual(before,(obs,actions,event))

    def test_no_invented_region_and_stale_region_refused(self):
        obs,actions,event=fixture()
        for events in ([],[{**event,'input':{**event['input'],'snapshot_id':'s99999999'}}],
                       [{**event,'input':{**event['input'],'region_id':'missing'}}]):
            with self.assertRaises(ValueError):probe.project_case(obs,actions,events,'retained-region')

    def test_prepare_does_not_include_later_correct_action_or_result(self):
        obs,actions,event=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'source';(run/'desktop/desktop').mkdir(parents=True)
            (run/'desktop/observation-1.json').write_text(json.dumps(obs))
            source_actions=[{k:v for k,v in a.items() if k!='handle'} for a in actions]
            (run/'desktop/desktop/desktop.jsonl').write_text(json.dumps({'result':{'operation':'actions','status':'ok','actions':source_actions}})+'\n')
            events=[{'sequence':1,'tool':'locua_observe','input':{},'result':{'snapshot_id':obs['snapshot_id']}},event,
                {'sequence':3,'tool':'locua_review','input':{'goals':[{'kind':'text','value':'EXACT literal'}]},'result':{'status':'approved','scope_id':'scope:1'}},
                {'sequence':4,'tool':'locua_act','input':{'scope_id':'scope:1','action_id':'FUTURE_ANSWER'},'result':{'secret':'FUTURE_READBACK'}}]
            evidence={'request':'Synthetic task','events':events};(run/'desktop/evidence.json').write_text(json.dumps(evidence))
            out=root/'prepared';probe.prepare(run,3,out,'retained-region')
            value=json.loads((out/'prepared.json').read_text());encoded=json.dumps(value['request'])
            self.assertNotIn('FUTURE_ANSWER',encoded);self.assertNotIn('FUTURE_READBACK',encoded)
            self.assertEqual(value['source_catalog_action_count'],3)
            self.assertEqual(len(value['all_source_candidate_ids']),3)
            self.assertEqual(value['desktop_dispatches'],0)
            self.assertEqual(probe.load_frozen(out)[0],value)

    def test_freeze_detects_input_and_runtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.py';source.write_text('unchanged')
            for name in ('prepared.json','full-observation.json'):(root/name).write_text('{}')
            freeze={'prepared_sha256':probe.sha(root/'prepared.json'),'observation_sha256':probe.sha(root/'full-observation.json'),
                    'inference_source_hashes':{str(source):probe.sha(source)}}
            (root/'freeze.json').write_text(json.dumps(freeze));probe.load_frozen(root)
            source.write_text('changed')
            with self.assertRaisesRegex(ValueError,'Frozen decision source'):probe.load_frozen(root)
            source.write_text('unchanged');(root/'prepared.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError,'Prepared case changed'):probe.load_frozen(root)


class Tokenizer:
    def encode(self,text,**_):return [ord(c) for c in text]
    def decode(self,ids):return ''.join(chr(i) for i in ids)
    def get_vocab(self):return {c:ord(c) for c in 'ABC'}


class FakeRuntime:
    def __init__(self):
        self.tokenizer=Tokenizer();self.calls=0;self.model=object();self.decisions=[]
        self.identity=(id(self.model),id(self.tokenizer))
        self.engine=SimpleNamespace(get_engine=lambda:(self.model,self.tokenizer))
        self.mx=SimpleNamespace(clear_cache=lambda:None,get_active_memory=lambda:0,
            get_peak_memory=lambda:0,get_cache_memory=lambda:0)
    def prepare(self,schema):
        return SimpleNamespace(to_parallel_schema_str=lambda:json.dumps(schema))
    def decide(self,context,schema):
        self.decisions.append((context,schema));self.calls+=1
        return {'result':{'parsed_json':{'action':{'value':'A'}}},'wall_ms':1}


class PairedTests(unittest.TestCase):
    def setUp(self):
        self.request={'goal':'Set the selected field exactly; preserve the competitor.',
            'observation_summary':'Two independently observed same-label fields.',
            'candidates':[{'id':'first','description':'Set first field'},
                          {'id':'second','description':'Set competing field'}], 'history':[]}

    def stream(self,token='A',finish='length',received=None):
        def generate(model,tokenizer,**kwargs):
            if received is not None:received.append((model,tokenizer,kwargs))
            yield SimpleNamespace(text=token,token=ord(token),prompt_tokens=len(kwargs['prompt']),
                generation_tokens=1,finish_reason=finish)
        return generate

    def test_exact_same_worker_prompt_and_mapping_both_routes_all_orders(self):
        from locua.engine.prototype.model_worker import choose
        for order,request in probe.orders(self.request):
            with self.subTest(order=order):
                rlcd=FakeRuntime();ordinary=FakeRuntime();received=[];sampler=object()
                a=probe.DecisionRuntime(rlcd,'rlcd')
                b=probe.DecisionRuntime(ordinary,'ordinary',stream=self.stream(received=received),sampler=sampler)
                before=deepcopy(request)
                left=choose(a,request,8192);right=choose(b,request,8192)
                self.assertEqual(left['selected_id'],right['selected_id'])
                self.assertEqual(left['stages'],[{**right['stages'][0],'inference_ms':1}])
                self.assertEqual(a.prompt,b.prompt)
                self.assertEqual(received[0][2],{'prompt':a.prompt['token_ids'],'max_tokens':1,
                    'sampler':sampler,'logits_processors':[]})
                self.assertEqual(received[0][0],ordinary.model)
                self.assertEqual(left['coverage']['candidate_ids'],[c['id'] for c in request['candidates']])
                self.assertEqual(right['coverage']['omitted_candidates'],0)
                self.assertEqual(len(rlcd.decisions),1);self.assertEqual(ordinary.decisions,[])
                self.assertEqual(before,request)

    def test_off_list_eos_or_incomplete_token_not_abstention_or_repair(self):
        from locua.engine.prototype.model_worker import choose
        for token,finish in [('Z','length'),('A','stop'),('A',None)]:
            with self.subTest(token=token,finish=finish):
                saved=[];runtime=FakeRuntime()
                wrapped=probe.DecisionRuntime(runtime,'ordinary',stream=self.stream(token,finish),
                    sampler=object(),record=lambda kind,value:saved.append((kind,value)))
                with self.assertRaises(probe.InvalidDecision):choose(wrapped,self.request,8192)
                self.assertEqual([k for k,v in saved],['prompt','ordinary-raw'])
                self.assertEqual(saved[-1][1]['responses'][0]['token'],ord(token))
                self.assertEqual(runtime.calls,0)

    def test_explicit_abstention_label_is_only_abstention(self):
        from locua.engine.prototype.model_worker import choose
        wrapped=probe.DecisionRuntime(FakeRuntime(),'ordinary',stream=self.stream('C'))
        result=choose(wrapped,self.request,8192)
        self.assertIsNone(result['selected_id']);self.assertTrue(result['abstained'])

    def test_model_identity_change_is_runtime_failure_not_invalid_choice(self):
        from locua.engine.prototype.model_worker import choose
        runtime=FakeRuntime();runtime.identity=(0,0)
        wrapped=probe.DecisionRuntime(runtime,'ordinary',stream=self.stream())
        with self.assertRaisesRegex(RuntimeError,'Model instance'):choose(wrapped,self.request,8192)

    def test_tokenizer_only_boundary_and_oversize_refuse_without_generation(self):
        from locua.engine.prototype.model_worker import choose
        for mechanism in (None,'rlcd','ordinary'):
            runtime=FakeRuntime();stream=unittest.mock.Mock()
            wrapped=probe.DecisionRuntime(runtime,mechanism,stream=stream)
            if mechanism is None:
                with self.assertRaises(probe.PromptBoundary):choose(wrapped,self.request,8192)
                self.assertIsNotNone(wrapped.prompt)
            with self.assertRaisesRegex(ValueError,'Full RLCD input'):
                choose(wrapped,{**self.request,'observation_summary':'a'*8200},8192)
            stream.assert_not_called();self.assertEqual(runtime.calls,0)

    def test_protocol_is_four_serial_cells_no_model_aliases_or_sampling_search(self):
        self.assertEqual(probe.PAIRED_PROTOCOL['models'],['baseline','comparator'])
        self.assertEqual(probe.PAIRED_PROTOCOL['mechanisms'],['rlcd','ordinary'])
        self.assertEqual(probe.PAIRED_PROTOCOL['ordinary_output_tokens'],1)
        self.assertEqual(probe.PAIRED_PROTOCOL['input_limit_tokens'],8192)
        self.assertEqual(len(probe.orders(self.request)),3)
        self.assertEqual(probe.orders(self.request)[1][1]['candidates'],list(reversed(self.request['candidates'])))

    def test_matrix_four_sequential_offline_processes_and_closed_workers(self):
        children=[]
        class Process:
            def __init__(self,command,**kwargs):
                self.command=command;self.code=None;children.append(self)
                self.case=command[command.index('--model')+1]
                self.mechanism=command[command.index('--mechanism')+1]
                lane=Path(command[command.index('--out')+1])
                probe.private_json(lane/'summary.json',{'trials':[{'order':order,'status':'valid',
                    'candidate_ids':['first','second'],'prompt_token_ids_sha256':'same','selected_id':'first'}
                    for order in probe.PAIRED_PROTOCOL['orders']]})
            def wait(self,timeout):self.code=0;return 0
            def poll(self):return self.code
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe,'load_frozen',return_value=(
                {'scope':'conditioned on prior region'}, {'paired_protocol':probe.PAIRED_PROTOCOL,'prepared_sha256':'frozen'})),\
                patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()),\
                patch('locua.lib._config',return_value={}),\
                patch('locua.engine.runtime_paths.runtime_python',return_value='/configured/python'),\
                patch('locua.engine.runtime_paths.worker_environment',return_value={'HF_HUB_OFFLINE':'1'}),\
                patch('subprocess.Popen',side_effect=Process):
            result=probe.execute_matrix(Path(tmp)/'frozen',Path(tmp)/'output')
        self.assertEqual([(c.case,c.mechanism) for c in children],
            [('baseline','rlcd'),('baseline','ordinary'),('comparator','rlcd'),('comparator','ordinary')])
        self.assertTrue(all(c.command[0]=='/usr/bin/sandbox-exec' for c in children))
        self.assertEqual(result['planned_decisions'],12)
        self.assertTrue(all(c['same_prompt_tokens'] and c['same_candidate_order'] for c in result['paired_comparisons']))
        self.assertTrue(all(l['worker_closed'] for l in result['lanes']))
        self.assertFalse(result['task_completion_proven']);self.assertFalse(result['accuracy_scored'])

    def test_matrix_interrupt_closes_only_owned_child_retains_unrun_denominator(self):
        class Process:
            def __init__(self,*args,**kwargs):self.code=None;self.terminated=False
            def wait(self,timeout):
                if not self.terminated:raise KeyboardInterrupt()
                self.code=-15;return self.code
            def poll(self):return self.code
            def terminate(self):self.terminated=True
        child=Process()
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe,'load_frozen',return_value=(
                {'scope':'conditioned on prior region'}, {'paired_protocol':probe.PAIRED_PROTOCOL,'prepared_sha256':'frozen'})),\
                patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()),\
                patch('locua.lib._config',return_value={}),\
                patch('locua.engine.runtime_paths.runtime_python',return_value='/configured/python'),\
                patch('locua.engine.runtime_paths.worker_environment',return_value={}),\
                patch('subprocess.Popen',return_value=child):
            root=Path(tmp)/'output'
            with self.assertRaises(KeyboardInterrupt):probe.execute_matrix(Path(tmp)/'frozen',root)
            result=json.loads((root/'summary.json').read_text())
        self.assertTrue(child.terminated);self.assertTrue(result['lanes'][0]['worker_closed'])
        self.assertEqual(result['status'],'interrupted');self.assertEqual(result['unrun_lanes'],3)
        self.assertEqual(result['planned_decisions'],12)

if __name__=='__main__':unittest.main()
