"""Aggregate accounting only: no provider, model or desktop calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('comparison_report',ROOT/'tools/comparison_report.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class ReportTests(unittest.TestCase):
    def protocols(self):
        return {v:{'protocol':{'tasks':[{'id':t} for t in ('calculator','textedit','fresh')], 'desktop_repeats_per_task':2}}
            for v in ('tools-v6.3','tools-v6.4')}
    def row(self,version='tools-v6.3',task='calculator',ordinal=1,suffix='',eligible=True):
        return {'harness':version,'provider':'openai','task_id':task,'trial_ordinal':ordinal,'development_suffix':suffix,
            'run':'synthetic-'+task+str(ordinal)+suffix,'audit':{'functional_pass':True,
                'conditional_on_application_following_observed_locale':False,'comparison_eligibility':{'eligible':eligible}}}
    def test_one_verified_task_is_not_six_case_qualification(self):
        q=next(x for x in r.qualifications(self.protocols(),[self.row()]) if x['provider']=='openai' and x['harness']=='tools-v6.3')
        self.assertEqual((q['verified_slots'],q['unrun_slots']),(1,5));self.assertFalse(q['six_outcome_gate_satisfied'])
    def test_versions_and_development_retries_do_not_fill_missing_slots(self):
        rows=[self.row(),self.row(suffix='b'),self.row(version='tools-v6.4',task='textedit')]
        q=r.qualifications(self.protocols(),rows)
        self.assertEqual([x['verified_slots'] for x in q if x['provider']=='openai'],[1,1])
        self.assertTrue(all(not x['six_outcome_gate_satisfied'] for x in q))
    def test_ineligible_task_does_not_get_gate_credit(self):
        q=next(x for x in r.qualifications(self.protocols(),[self.row(eligible=False)]) if x['provider']=='openai' and x['harness']=='tools-v6.3')
        self.assertEqual(q['verified_slots'],0);self.assertEqual(q['completed_qualification_slots'],1)
    def test_six_valid_outcomes_still_do_not_claim_cross_run_source_equivalence(self):
        rows=[self.row(task=t,ordinal=i) for t in ('calculator','textedit','fresh') for i in (1,2)]
        q=next(x for x in r.qualifications(self.protocols(),rows) if x['provider']=='openai' and x['harness']=='tools-v6.3')
        self.assertTrue(q['six_outcome_gate_satisfied']);self.assertFalse(q['source_equivalence_across_runs_independently_proven'])
    def test_poisoned_channel_is_not_eight_model_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'provider').mkdir()
            for i in range(1,9):
                record={'call':i,'status':'failed','complete_generation_count':None,'error':{'type':'EOFError' if i==1 else 'ProtocolError',
                    'message':'Local planner worker exited' if i==1 else 'Decision service is closed/poisoned; create a new service'}}
                (run/f'provider/call-{i:03}-summary.json').write_text(json.dumps(record))
            m=r.record_metrics(r.Reader(),run,{'provider':{'provider':'local','thinking':True}})
            self.assertEqual(m['service_requests'],8);self.assertEqual(m['known_generation_dispatches'],0)
            self.assertEqual(m['unresolved_generation_calls'],[1]);self.assertEqual(m['unknown_calls'],[1])
            self.assertEqual(m['poisoned_channel_refusals_before_worker_send'],list(range(2,9)))
            self.assertIsNone(m['model_calls']);self.assertIsNone(m['input_tokens'])
    def test_hosted_reasoning_not_mislabeled_by_local_thinking_flag(self):
        self.assertEqual(r.provider_key({'provider':'openai','thinking':False}),'openai')
        self.assertEqual(r.provider_key({'provider':'local','thinking':True}),'local-thinking')
    def test_unknown_manifest_never_inherits_legacy_version(self):
        p={'tools-v6.4':{'protocol':{'replay_manifest_sha256':'new'}}};u={'frozen_manifest_sha256':'old'}
        self.assertEqual(r.replay_harness({'manifest_sha256':'old'},p,u),'tools-v6.2')
        self.assertEqual(r.replay_harness({'manifest_sha256':'new'},p,u),'tools-v6.4')
        self.assertEqual(r.replay_harness({'manifest_sha256':'unrecognized'},p,u),'unknown_manifest')
    def test_terminal_runner_retains_planned_denominator_and_unrun_counts(self):
        c=r.replay_counts({'planned_calls':8,'calls':1,'unrun_calls':7,'runner_version':'terminal-transport-stop-v1'})
        self.assertEqual((c['scheduled_decisions'],c['attempted_provider_calls'],c['unrun_decisions']),(8,1,7))
        c=r.replay_counts({'calls':8,'results':[{'status':'failed'}]*8})
        self.assertEqual((c['scheduled_decisions'],c['attempted_provider_calls'],c['unrun_decisions']),(8,8,0))
    def test_tool_intervals_union_parallel_reads_and_separate_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'session').mkdir();events=[]
            for key,name,start,end in [('a','locua_inspect',1,4),('b','locua_inspect',2,5),('c','locua_review',5,12)]:
                for kind,at in [('pre',start),('post',end)]:events.append({'event':'tool:'+kind,'at_ns':at*10**9,'data':{'tool_call_id':key,'tool_name':name}})
            (run/'session/session.json').write_text(json.dumps({'events':sorted(events,key=lambda e:e['at_ns'])}))
            t=r.timing_breakdown(r.Reader(),run,{'budget_measurements':{'service_wall_s':3,'requests':2,'includes_initial_model_load':True}},
                {'model_load_ms':2000},{'known_generation_s':20,'usage_complete':False},
                {'latency':{'workflow_wall_s':40,'excluding_review_s':34},'reviews':{'human_wait_s':6}})
            self.assertEqual(t['tool_nonreview_observed_union_s'],4)
            self.assertEqual(t['review_tool_observed_union_s'],7)
            self.assertEqual(t['human_review_s'],6);self.assertEqual(t['model_load_s'],2)
            self.assertTrue(t['token_meter_includes_initial_model_load']);self.assertFalse(t['generation_complete'])
    def test_missing_or_unfinished_tool_timing_is_not_zero_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);a={'latency':{},'reviews':{}}
            t=r.timing_breakdown(r.Reader(),run,{}, {},{},a)
            self.assertIsNone(t['tool_nonreview_observed_union_s']);self.assertIsNone(t['model_load_s'])
            self.assertFalse(t['tool_timing_complete'])
            (run/'session').mkdir();(run/'session/session.json').write_text(json.dumps({'events':[{'event':'tool:pre','at_ns':1,'data':{'tool_call_id':'x','tool_name':'locua_act'}}]}))
            t=r.timing_breakdown(r.Reader(),run,{}, {},{},a)
            self.assertFalse(t['tool_timing_complete']);self.assertIn('Unfinished',t['tool_timing_issues'][0])
    def test_v65_notes_remain_named_source_records_not_success_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);d=base/'audit-v65-independent-001';d.mkdir()
            for name in ('fresh-1-diagnosis.json','action-reference-note.json'):(d/name).write_text('{"functional_pass":false}')
            reader=r.Reader();notes=r.independent_notes(reader,base,'v65-openai-fresh-1')
            self.assertEqual(len(notes),2);self.assertEqual(len(reader.sources),2)
            self.assertEqual(r.independent_notes(reader,base,'v65-local-calculator-1'),[])
    def test_operator_abort_is_not_model_failure_or_zero_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'provider').mkdir();(run/'desktop').mkdir()
            (run/'operator-abort.json').write_text('{"kind":"operator_abort_before_task_input"}')
            (run/'provider/dispatch-001-input.json').write_text('{}')
            a=r.operator_abort(r.Reader(),run)
            self.assertEqual(a['status'],'operator_aborted');self.assertFalse(a['qualification_credit'])
            self.assertFalse(a['model_failure_inferred']);self.assertIsNone(a['generation_count'])
            self.assertEqual(a['public_tool_event_files'],0)
            (run/'desktop/event-001.json').write_text('{}')
            self.assertTrue(r.operator_abort(r.Reader(),run)['integrity_issues'])

if __name__=='__main__':unittest.main()
