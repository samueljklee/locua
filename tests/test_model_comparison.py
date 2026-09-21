import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

path=Path(__file__).resolve().parents[1]/'tools/model_comparison.py'
spec=importlib.util.spec_from_file_location('locua_model_comparison',path)
comparison=importlib.util.module_from_spec(spec);spec.loader.exec_module(comparison)


def case():return {'id':'sample','request':{'messages':[{'role':'user','content':'Compute (9-2)/3.'}], 'tools':comparison._tools(),'stream':False}}


def rubric():return {'kind':'calculation_review','expression':'(9-2)/3','readout_candidates':[{'snapshot_id':'s1','control_id':'display'}]}


def review(control='display',expression='(9-2)/3'):
    return {'tool_calls':[{'name':'locua_review','arguments':{'snapshot_id':'s1','summary':'Enter and evaluate the requested expression, then read the captured display.',
        'goals':[{'id':'g','kind':'calculation','target':'observed readout','control_id':control,'expression':expression,'evidence_plane':'display'}],
        'effects':[{'kind':'goal','goal_id':'g'}],'covers_entire_request':True}}]}


class EvaluationTests(unittest.TestCase):
    def test_semantic_review_accepts_right_readout_without_expected_result(self):
        got=comparison.evaluate_response(case(),rubric(),review())
        self.assertTrue(got['semantic_progress']);self.assertFalse(got['task_completed'])
        self.assertFalse(got['desktop_action_executed'])

    def test_wrong_container_expression_or_extra_effect_is_not_progress(self):
        for response in (review('window'),review(expression='9-2*3')):
            self.assertFalse(comparison.evaluate_response(case(),rubric(),response)['semantic_progress'])
        response=review();response['tool_calls'][0]['arguments']['effects'].append({'kind':'press','control_id':'other','purpose':'unrelated'})
        self.assertFalse(comparison.evaluate_response(case(),rubric(),response)['semantic_progress'])

    def test_repeated_inspection_is_distinct_from_schema_failure(self):
        args={'snapshot_id':'s1','operation':'control','control_id':'display'}
        c=case();c['request']['messages'].append({'role':'assistant','content':[], 'tool_calls':[{'tool':'locua_inspect','arguments':args}]})
        got=comparison.evaluate_response(c,rubric(),{'tool_calls':[{'name':'locua_inspect','arguments':args}]})
        self.assertEqual(got['calls'][0]['classification'],'repeated_inspection')
        args={'operation':'control','window_id':44}
        bad=comparison.evaluate_response(c,rubric(),{'tool_calls':[{'name':'locua_inspect','arguments':args}]})
        self.assertEqual(bad['calls'][0]['classification'],'schema_invalid')

    def test_text_requires_exact_approved_scope_action_and_literal(self):
        r={'kind':'approved_text_action','scope_id':'scope','snapshot_id':'s1','action_id':'action','value':' 00028\r\nΩ '}
        args={k:r[k] for k in ('scope_id','snapshot_id','action_id','value')}
        self.assertTrue(comparison.evaluate_response(case(),r,{'tool_calls':[{'name':'locua_act','arguments':args}]})['semantic_progress'])
        for key,value in [('scope_id','other'),('action_id','other'),('value','00028\nΩ')]:
            bad={**args,key:value}
            self.assertFalse(comparison.evaluate_response(case(),r,{'tool_calls':[{'name':'locua_act','arguments':bad}]})['semantic_progress'])

    def test_new_grounded_inspection_counts_progress_without_a_fixed_sequence(self):
        r=rubric();r['inspection']={'snapshots':{'s1':{'controls':[
            {'id':'display','role':'AXStaticText','name':None,'value':'pending','region_id':'main'},
            {'id':'button','role':'AXButton','name':'Mode','value':None,'region_id':'main'},
            {'id':'menu','role':'AXMenuItem','name':'View','value':None,'region_id':'menus'}],
            'regions':['main','menus']}},'exposed_control_ids':['display','button'],
            'detailed_control_ids':['display'],'known_region_ids':['main'],'known_window_ids':['w'],
            'prior_inspections':[],'recorded_recovery_needed':False}
        def score(args):return comparison.evaluate_response(case(),r,{'tool_calls':[{'name':'locua_inspect','arguments':args}]})
        for args in ({'operation':'control','snapshot_id':'s1','control_id':'button'},
                     {'operation':'overview','snapshot_id':'s1'},
                     {'operation':'list','snapshot_id':'s1','role':'AXMenuItem'}):
            self.assertTrue(score(args)['semantic_progress'])
        for args in ({'operation':'control','snapshot_id':'s1','control_id':'invented'},
                     {'operation':'control','snapshot_id':'s1','control_id':'display'},
                     {'operation':'list','snapshot_id':'s1','region_id':'invented'},
                     {'operation':'list','snapshot_id':'foreign','role':'AXButton'},
                     {'operation':'list','snapshot_id':'s1','query':'not-present'}):
            self.assertFalse(score(args)['semantic_progress'])
        args={'window_id':'w'}
        self.assertEqual(comparison.classify_inspection('locua_activate',args,r),'freshness_or_readability_need_not_established')
        r['inspection']['readability_recovery_window_ids']=['w']
        self.assertEqual(comparison.classify_inspection('locua_activate',args,r),'grounded_readability_recovery')
        self.assertEqual(comparison.classify_inspection('locua_activate',{'window_id':'wrong'},r),'wrong_or_unexposed_window')

    def test_full_inventory_and_no_scoring_fields_in_provider_request(self):
        c=case();names=[x['name'] for x in c['request']['tools']]
        self.assertEqual(names,list(comparison.SPECS));self.assertNotIn('rubric',c['request'])
        self.assertNotIn('readout_candidates',json.dumps(c['request']))


class EvaluatorV2Tests(unittest.TestCase):
    def fixtures(self,*,age=96,fresh=True):
        c=case();r=rubric();r['inspection']={'snapshots':{'s1':{'controls':[
            {'id':'field','role':'AXTextField','name':'Search','value':'VALUE-ONLY-SECRET',
             'semantics':{'description':'Local search field','help':'Find settings','identifier':'search-control'},'region_id':'main'}],
            'regions':['main']}},'exposed_control_ids':[],'known_region_ids':['main'],'known_window_ids':['w']}
        output={'status':'ok','window_id':'w','snapshot_id':'s1','retained_observation_only':True,
            'capture_age_seconds':age,'fresh_capture_required_for_action':fresh}
        c['request']['messages'].append({'role':'tool','name':'locua_inspect','content':json.dumps({'output':output})})
        return c,r

    def classify(self,c,r,name,args):
        return comparison.evaluate_response(c,r,{'tool_calls':[{'name':name,'arguments':args}]})

    def test_one_stale_exact_window_observe_admissible_activation_not_implied(self):
        c,r=self.fixtures()
        got=self.classify(c,r,'locua_observe',{'window_id':'w'})
        self.assertTrue(got['semantic_progress']);self.assertEqual(got['calls'][0]['classification'],'admissible_freshness_recovery')
        self.assertFalse(self.classify(c,r,'locua_activate',{'window_id':'w'})['semantic_progress'])
        self.assertFalse(self.classify(c,r,'locua_observe',{'window_id':'other'})['semantic_progress'])
        fresh_c,fresh_r=self.fixtures(age=1,fresh=False)
        self.assertFalse(self.classify(fresh_c,fresh_r,'locua_observe',{'window_id':'w'})['semantic_progress'])
        # Old observed_at alone cannot be aged using the evaluator's current clock.
        body=json.loads(fresh_c['request']['messages'][-1]['content']);body['output']['observed_at_ns']=1
        fresh_c['request']['messages'][-1]['content']=json.dumps(body)
        self.assertFalse(self.classify(fresh_c,fresh_r,'locua_observe',{'window_id':'w'})['semantic_progress'])

    def test_activation_requires_recorded_readability_failure_not_schema_error(self):
        c,r=self.fixtures()
        r['inspection']['recorded_recovery_needed']=True
        self.assertFalse(self.classify(c,r,'locua_activate',{'window_id':'w'})['semantic_progress'])
        c['request']['messages'].append({'role':'tool','name':'locua_observe','content':json.dumps({'output':{'window_id':'w','status':'unavailable'}})})
        got=self.classify(c,r,'locua_activate',{'window_id':'w'})
        self.assertTrue(got['semantic_progress']);self.assertEqual(got['calls'][0]['classification'],'grounded_readability_recovery')

    def test_equivalent_already_repeated_capture_not_excused_by_age(self):
        c,r=self.fixtures()
        for _ in range(2):
            c['request']['messages'].append({'role':'tool','name':'locua_observe','content':json.dumps({'output':{
                'window_id':'w','status':'observed','snapshot_id':'same','overview':{'retained_observation_only':True,
                'capture_age_seconds':96,'fresh_capture_required_for_action':True}}})})
        got=self.classify(c,r,'locua_observe',{'window_id':'w'})
        self.assertFalse(got['semantic_progress']);self.assertEqual(got['calls'][0]['classification'],'repeated_recapture_evidenced')

    def test_query_matches_production_labels_never_values_or_nonstring_coercion(self):
        c,r=self.fixtures()
        for query in ('VALUE-ONLY-SECRET','None','False'):
            got=self.classify(c,r,'locua_inspect',{'snapshot_id':'s1','operation':'list','query':query})
            self.assertFalse(got['semantic_progress']);self.assertEqual(got['calls'][0]['classification'],'inspection_matches_no_captured_controls')
        for query in ('SEARCH','Local search','Find settings','search-control'):
            self.assertTrue(self.classify(c,r,'locua_inspect',{'snapshot_id':'s1','operation':'list','query':query})['semantic_progress'])

    def test_context_recovery_after_compaction_only_for_unexposed_or_truncated_section(self):
        c,r=self.fixtures()
        self.assertFalse(self.classify(c,r,'locua_status',{})['semantic_progress'])
        c['request']['messages'].append({'role':'user','content':'<system-reminder source="context-compaction">\nOlder messages may be truncated.\n</system-reminder>'})
        self.assertTrue(self.classify(c,r,'locua_status',{})['semantic_progress'])
        self.assertFalse(self.classify(c,r,'locua_status',{'operation':'goals','scope_id':'invented'})['semantic_progress'])
        self.assertFalse(self.classify(c,r,'locua_status',{'operation':'summary','start':12})['semantic_progress'])
        row={'status':'ok','operation':'summary','start':0,'next_start':4,'truncated':False}
        c['request']['messages'].append({'role':'tool','name':'locua_status','content':json.dumps({'output':row})})
        self.assertFalse(self.classify(c,r,'locua_status',{})['semantic_progress'])
        self.assertTrue(self.classify(c,r,'locua_status',{'operation':'summary','start':4})['semantic_progress'])
        row['truncated']=True;c['request']['messages'][-1]['content']=json.dumps({'output':row})
        self.assertTrue(self.classify(c,r,'locua_status',{})['semantic_progress'])

    def test_regrade_keeps_original_outputs_requests_and_grading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);f=root/'freeze';f.mkdir();run=root/'run';run.mkdir();c,r=self.fixtures()
            c['request_sha256']=comparison.digest(c['request'])
            (f/'sample.json').write_text(json.dumps(c));(f/'rubric-private.json').write_text(json.dumps({'cases':[{'id':'sample',**r}]}))
            (f/'manifest.json').write_text(json.dumps({'cases':[{'id':'sample','request_sha256':c['request_sha256']}],
                'rubric_sha256':comparison.digest((f/'rubric-private.json').read_bytes())}))
            old={'status':'returned','request_sha256':c['request_sha256'],'evaluation':{'semantic_progress':False},
                'response':{'tool_calls':[{'name':'locua_observe','arguments':{'window_id':'w'}}]}}
            response=run/'sample-1.json';response.write_text(json.dumps(old));before=response.read_bytes()
            request_before=(f/'sample.json').read_bytes()
            report=comparison.regrade(f,[run],root/'regrade.json');p=report['providers'][0]
            self.assertEqual(p['admissible_next_action_responses'],1);self.assertEqual(p['pending_files'],['sample-2.json'])
            self.assertEqual(p['rows'][0]['original_evaluation'],old['evaluation'])
            self.assertEqual(response.read_bytes(),before);self.assertEqual((f/'sample.json').read_bytes(),request_before)
            self.assertEqual(report['provider_calls'],0)
            with self.assertRaises(FileExistsError):comparison.regrade(f,[run],root/'regrade.json')


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def freeze(self,root,count=4):
        frozen=root/'freeze';frozen.mkdir();entries=[];rubrics=[]
        for index in range(count):
            c=case();c['id']=f'sample-{index}'
            (frozen/(c['id']+'.json')).write_text(json.dumps(c))
            entries.append({'id':c['id'],'request_sha256':comparison.digest(c['request'])})
            rubrics.append({'id':c['id'],**rubric()})
        (frozen/'rubric-private.json').write_text(json.dumps({'cases':rubrics}))
        (frozen/'manifest.json').write_text(json.dumps({'cases':entries,
            'rubric_sha256':comparison.digest((frozen/'rubric-private.json').read_bytes())}))
        return frozen

    async def test_eof_stops_after_one_attempt_preserves_all_eight_slots_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=self.freeze(root);attempts=[];closed=[]
            before={p.name:p.read_bytes() for p in frozen.iterdir()}
            class Provider:
                async def complete(self,request):attempts.append(request);raise EOFError('worker exited')
            @asynccontextmanager
            async def factory(out):
                try:yield Provider()
                finally:closed.append(True)
            result=await comparison.run_replays(frozen,root/'out',factory,provider_label='fake')
            self.assertEqual(len(attempts),1);self.assertEqual(closed,[True])
            self.assertEqual((result['calls'],result['planned_calls'],result['unrun_calls']),(1,8,7))
            self.assertEqual(result['status'],'stopped_terminal_provider_failure')
            self.assertEqual([r['status'] for r in result['results']],['failed']+['unrun']*7)
            self.assertEqual(result['terminal_failure']['error_type'],'EOFError')
            for row in result['results'][1:]:
                saved=json.loads((root/'out'/f"{row['case_id']}-{row['repeat']}.json").read_text())
                self.assertFalse(saved['provider_call_attempted']);self.assertEqual(saved['model_generations'],0)
                self.assertNotIn('evaluation',saved)
            self.assertEqual(before,{p.name:p.read_bytes() for p in frozen.iterdir()})

    async def test_poisoned_runtime_stops_even_when_error_is_validation_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=self.freeze(root,1);attempts=[]
            class Provider:
                _service=SimpleNamespace(closed=False,poisoned=False)
                async def complete(self,request):
                    attempts.append(request);self._service.poisoned=True
                    raise ValueError('runtime result integrity failed')
            @asynccontextmanager
            async def factory(out):yield Provider()
            result=await comparison.run_replays(frozen,root/'out',factory,provider_label='fake')
            self.assertEqual(len(attempts),1);self.assertEqual(result['unrun_calls'],1)
            self.assertEqual(result['terminal_failure']['basis'],'explicit_unusable_state')

    async def test_healthy_parse_failure_and_returned_refusal_do_not_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=self.freeze(root,2);attempts=[]
            class Provider:
                _closed=False
                async def complete(self,request):
                    attempts.append(request)
                    if len(attempts)==1:raise ValueError('model output failed strict parsing')
                    return SimpleNamespace(model_dump=lambda:{'tool_calls':[],'content':[], 'finish_reason':'refusal'})
            @asynccontextmanager
            async def factory(out):yield Provider()
            result=await comparison.run_replays(frozen,root/'out',factory,provider_label='fake')
            self.assertEqual(len(attempts),4);self.assertEqual(result['unrun_calls'],0)
            self.assertIsNone(result['terminal_failure'])
            self.assertEqual([r['status'] for r in result['results']],['failed']+['returned']*3)
            self.assertTrue(all(not r['evaluation']['semantic_progress'] for r in result['results'][1:]))

    async def test_cancellation_retains_remaining_unrun_then_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=self.freeze(root,1);attempts=[];closed=[]
            class Provider:
                async def complete(self,request):attempts.append(request);raise asyncio.CancelledError()
            @asynccontextmanager
            async def factory(out):
                try:yield Provider()
                finally:closed.append(True)
            with self.assertRaises(asyncio.CancelledError):
                await comparison.run_replays(frozen,root/'out',factory,provider_label='fake')
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual(len(attempts),1);self.assertEqual(closed,[True])
            self.assertEqual(summary['unrun_calls'],1)

    async def test_two_identical_requests_no_adaptive_reprompt_or_hidden_rubric(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen=root/'freeze';frozen.mkdir();c=case();seen=[];closed=[]
            (frozen/'sample.json').write_text(json.dumps(c))
            rb={'cases':[{'id':'sample',**rubric()}]};(frozen/'rubric-private.json').write_text(json.dumps(rb))
            manifest={'cases':[{'id':'sample','request_sha256':comparison.digest(c['request'])}],
                'rubric_sha256':comparison.digest((frozen/'rubric-private.json').read_bytes())}
            (frozen/'manifest.json').write_text(json.dumps(manifest))
            class Provider:
                async def complete(self,request):
                    seen.append(request.model_dump(exclude_none=True))
                    return SimpleNamespace(model_dump=lambda:review())
            @asynccontextmanager
            async def factory(out):
                try:yield Provider()
                finally:closed.append(True)
            result=await comparison.run_replays(frozen,root/'out',factory,provider_label='fake-offline')
            self.assertEqual(result['calls'],2);self.assertEqual(seen[0],seen[1]);self.assertEqual(closed,[True])
            self.assertNotIn('readout_candidates',json.dumps(seen));self.assertFalse(result['live_task_completion_claimed'])
            with self.assertRaises(FileExistsError):await comparison.run_replays(frozen,root/'out',factory,provider_label='fake')

    async def test_modified_frozen_request_refused_before_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);f=root/'f';f.mkdir();c=case();c['request']['messages'][0]['content']='changed'
            (f/'sample.json').write_text(json.dumps(c));(f/'rubric-private.json').write_text(json.dumps({'cases':[]}))
            (f/'manifest.json').write_text(json.dumps({'cases':[{'id':'sample','request_sha256':'wrong'}],
                'rubric_sha256':comparison.digest((f/'rubric-private.json').read_bytes())}))
            called=[]
            class Provider:
                async def complete(self,request):called.append(request);raise AssertionError('must not call')
            @asynccontextmanager
            async def factory(out):yield Provider()
            with self.assertRaises(ValueError):await comparison.run_replays(f,root/'out',factory,provider_label='fake')
            self.assertEqual(called,[])
