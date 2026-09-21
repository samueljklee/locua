"""Independent frozen-fixture/oracle and policy wiring checks. No model or GUI."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
ev=module('v8eval',ROOT/'tools/transfer_v8_eval.py')
server=module('v8server',ev.FIXTURE/'server.py')
verify=module('v8verify',ev.FIXTURE/'verify.py')
audit=module('v8audit',ROOT/'tools/audit_transfer_v8.py')

class ScriptedSelector:
    """Test-only desired-action script; never used by the model runner."""
    def __init__(self,fixture):self.fixture=fixture;self.index=0;self.requests=[]
    def choose(self,**request):
        self.requests.append(deepcopy(request));intent=self.fixture['task_template']['intents'][self.index]
        selector=intent['selector'];controls=self.fixture['controls']
        matches=[(n,c) for n,c in enumerate(controls) if c['name']==selector['name'] and c['role']==selector.get('role') and (not selector.get('ancestor') or next(p['name'] for p in controls if p['key']==c.get('parent'))==selector['ancestor']['name'])]
        if len(matches)!=1:raise AssertionError('test identity ambiguous')
        n,c=matches[0];choices=request['candidates'];view=json.loads(request['observation_summary'].split('\n',1)[1])
        for row in view['items']:
            cid=row.get('id')
            if row.get('membership')=='primary' and cid and cid.endswith(':'+str(n)):
                actions=[x for x in choices if '; control='+cid in x['description'] and (intent['kind']!='set_text' or json.dumps(intent['value'],ensure_ascii=False) in x['description'])]
                if len(actions)==1:self.index+=1;return {'selected_id':actions[0]['id']}
        parent=selector.get('ancestor',{}).get('name') or ('Visitor collection' if c.get('parent')=='visitor' else c.get('parent'))
        inspect=[x for x in choices if x['id'].startswith('inspect:') and x['description'].endswith(parent)]
        if len(inspect)==1:return {'selected_id':inspect[0]['id']}
        if any(x['id']=='next_page' for x in choices):return {'selected_id':'next_page'}
        return {'selected_id':'overview'}

class FrozenFixtureTests(unittest.TestCase):
    def test_semantic_and_materialization_seals_match(self):self.assertEqual(len(ev.check_freeze()['fixture_freeze_sha256']),64)
    def test_server_initials_match_frozen_independent_layout(self):
        for case in verify.CASES[:2]:self.assertEqual(server.INITIAL[case['id']],verify.initial_values(case))
    def test_dense_targets_follow_twenty_prefix_fields_and_duplicate_names(self):
        html=server.render('dispatch-board').decode()
        self.assertLess(html.index('reference.20'),html.index('evening.destination'))
        self.assertEqual(html.count('>Destination<input'),2);self.assertEqual(html.count('>Send notice</label>'),2)
    def test_server_never_imports_oracle_or_serves_files(self):
        text=(ev.FIXTURE/'server.py').read_text()
        self.assertNotIn('gold.json',text);self.assertNotIn('cases.json',text);self.assertEqual(set(server.ROUTES),{'/dispatch','/access'})
    def test_no_receipt_is_a_failure(self):
        with tempfile.TemporaryDirectory() as d:self.assertFalse(verify.verify_browser('dispatch-board',d)['pass'])
    def test_browser_positive_and_transient_preservation_failure(self):
        with tempfile.TemporaryDirectory() as d:
            store=server.Store(Path(d)/'out');case=verify.CASES[0];final={**server.INITIAL[case['id']],**case['changes']}
            bad={**final,'morning.destination':'changed then repaired'};store.record(case['id'],{'kind':'input','values':bad});store.record(case['id'],{'kind':'input','values':final})
            result=verify.verify_browser(case['id'],store.out);self.assertFalse(result['pass']);self.assertIn('preservation:morning.destination',result['issues'])
        with tempfile.TemporaryDirectory() as d:
            store=server.Store(Path(d)/'out');store.record(case['id'],{'kind':'input','values':final});self.assertTrue(verify.verify_browser(case['id'],store.out)['pass'])
    def test_access_inputs_do_not_count_as_explicit_save(self):
        with tempfile.TemporaryDirectory() as d:
            store=server.Store(Path(d)/'out');case=verify.CASES[1];values={**server.INITIAL[case['id']],**case['changes']}
            store.record(case['id'],{'kind':'input','values':values});self.assertFalse(verify.verify_browser(case['id'],store.out)['pass'])
            store.record(case['id'],{'kind':'save','values':values});self.assertTrue(verify.verify_browser(case['id'],store.out)['pass'])
    def test_store_rejects_partial_duplicate_and_wrong_types(self):
        with tempfile.TemporaryDirectory() as d:
            store=server.Store(Path(d)/'out')
            with self.assertRaises(ValueError):store.record('dispatch-board',{'kind':'input','values':{}})
            values=deepcopy(server.INITIAL['dispatch-board']);values['evening.notice']=1
            with self.assertRaises(ValueError):store.record('dispatch-board',{'kind':'input','values':values})
            with self.assertRaises(ValueError):server.unique_object([('x',1),('x',2)])
    def test_workbook_initial_typed_values_and_formulas(self):
        cells=audit.read_cells(ev.FIXTURE/'sample-register.xlsx')
        self.assertEqual(cells['F7'],('string','Pending review'));self.assertEqual(cells['D7'],('number','3'))
        self.assertEqual(cells['H6'],('formula','D6*E6'));self.assertEqual(cells['H7'],('formula','D7*E7'))
        result=audit.verify_workbook(ev.FIXTURE/'sample-register.xlsx');self.assertEqual(result['mismatched_cells'],['F7'])
    def test_language_cases_and_gold_are_distinct_full_membership(self):
        inputs=ev.read(ev.FIXTURE/'language-cases.json')['cases'];gold=ev.read(ev.FIXTURE/'gold.json')['language']
        self.assertEqual(len(inputs),8);self.assertEqual({x['id'] for x in inputs},set(gold))
        self.assertTrue(all(set(x)=={'id','context_case','request'} for x in inputs))

class PolicyWiringTests(unittest.TestCase):
    def fixtures(self):return ev.read(ev.FIXTURE/'simulations.json')['cases']
    def test_both_policies_complete_dense_simulation_with_scripted_choices(self):
        for policy in ['model_led','reviewed_target_first']:
            fixture=self.fixtures()[0];events=[];selector=ScriptedSelector(fixture)
            result=ev.simulate_one(fixture,selector,policy,threading.Event(),events)
            self.assertEqual(result['status'],'complete',(policy,result['reason']));self.assertEqual(result['simulated_actions'],2)
            self.assertEqual(result['final_controls']['md']['value'],'Harbor 02')
            if policy=='reviewed_target_first':
                route=next(e for e in events if e['type']=='inspection_route');self.assertGreater(route['page_offset'],0)
                self.assertIn('first_page',{x['id'] for x in selector.requests[0]['candidates']})
                self.assertIn('overview',{x['id'] for x in selector.requests[0]['candidates']})
            else:self.assertIn('overview',next(e for e in events if e['type']=='decision_request')['phase'])
    def test_both_policies_complete_menu_dialog_simulation(self):
        for policy in ['model_led','reviewed_target_first']:
            fixture=self.fixtures()[1];events=[];result=ev.simulate_one(fixture,ScriptedSelector(fixture),policy,threading.Event(),events)
            self.assertEqual(result['status'],'complete',(policy,result['reason']));self.assertEqual(result['simulated_actions'],5)
            self.assertFalse(result['final_controls']['edit']['visible']);self.assertTrue(result['final_controls']['saved']['visible'])
            self.assertEqual(result['final_controls']['sb']['value'],'Staff only')
    def test_cancellation_has_no_actions(self):
        f=self.fixtures()[0];cancel=threading.Event();cancel.set();result=ev.simulate_one(f,ScriptedSelector(f),'reviewed_target_first',cancel,[])
        self.assertEqual(result['status'],'canceled');self.assertEqual(result['simulated_actions'],0)
    def test_dry_prepare_does_not_construct_model(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d,patch('locua.engine.prototype.decision.ModelService',side_effect=AssertionError('no model')):
            result=ev.run('simulation','reviewed_target_first','baseline',Path(d)/'out',None,False)
            self.assertEqual(result['status'],'prepared');self.assertEqual(result['unrun'],2)
    def test_explicit_fake_execution_uses_config_value_not_load_tuple(self):
        from unittest.mock import patch
        from contextlib import contextmanager
        seen=[]
        @contextmanager
        def environment(config):
            self.assertEqual(config,{'runtime_python':'fake'});seen.append(config);yield
        class FakeModel:
            closed=False;poisoned=False
            def __init__(self,**_):pass
            def __enter__(self):return self
            def __exit__(self,*_):self.closed=True
            def info(self):return {'fake':True}
        with tempfile.TemporaryDirectory() as d,patch('locua.config.load',return_value=({'runtime_python':'fake'},Path('fake-config'))),patch('locua.engine_adapter.runtime_environment',environment),patch('locua.engine.prototype.decision.ModelService',FakeModel),patch.object(ev,'simulate_one',return_value={'status':'complete','gui_calls':0}):
            result=ev.run('simulation','reviewed_target_first','baseline',Path(d)/'out','fake-config',True)
            self.assertEqual(result['attempted'],2);self.assertEqual(result['status'],'finished');self.assertTrue(result['worker_closed']);self.assertEqual(len(seen),1)

class LanguageAuditTests(unittest.TestCase):
    def setUp(self):
        self.nl=module('v8language',ROOT/'tools/transfer_v8_language.py')
        self.case=ev.read(ev.FIXTURE/'language-cases.json')['cases'][0]
        self.gold=ev.read(ev.FIXTURE/'gold.json')['language'][self.case['id']]
        from locua.engine.prototype.simulation import SimulatedDriver
        from locua.guided import catalog
        from locua.language_planning import observed_catalog
        fixture=ev.read(ev.FIXTURE/'simulations.json')['cases'][0];obs=SimulatedDriver(fixture).observe();fields=catalog(obs)
        self.context={'catalog':observed_catalog(obs,fields),'scope':{'kind':'browser','url':'http://127.0.0.1:1/dispatch'}}
    def test_complete_observed_semantics_and_all_preserves_pass(self):
        from locua.engine.prototype.observed_planner import compile_proposal
        sets=[];keeps=[]
        for f in self.context['catalog']['fields']:
            key=self.nl.semantic_key(f)
            if key in self.gold['changes']:sets.append([f['id'],self.gold['changes'][key],'display' if f['property']=='checked' else 'editor_buffer'])
            else:keeps.append([f['id'],{'observed':True}])
        result=compile_proposal({'set':sets,'keep':keeps,'ask':[]},self.case['request'],self.context['scope'],self.context['catalog'])
        result['status']='proposed';self.assertTrue(self.nl.grade(self.case,self.gold,self.context,result)['pass'])
        result['plan']['constraints'].pop();self.assertFalse(self.nl.grade(self.case,self.gold,self.context,result)['pass'])
    def test_missing_value_requires_correct_question_and_zero_edits(self):
        gold={'disposition':'clarify','reason':'missing_value'}
        report={'status':'clarification','question_codes':['missing_value'],'plan':{'outcomes':[],'constraints':[]}}
        self.assertTrue(self.nl.grade({},gold,{},report)['pass'])
        report['plan']['outcomes']=[{'invented':'edit'}];self.assertFalse(self.nl.grade({},gold,{},report)['pass'])
    def test_unsupported_required_save_is_not_counted_pass(self):
        report={'status':'clarification','question_codes':['unsupported_operation']}
        result=self.nl.grade({}, {'disposition':'propose_or_explicit_unsupported'}, {},report)
        self.assertFalse(result['pass']);self.assertTrue(result['truthful_unsupported'])

if __name__=='__main__':unittest.main()
