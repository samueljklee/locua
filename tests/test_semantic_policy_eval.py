"""Frozen evaluation/oracle/attestation and actual Amplifier loop; CPU only."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import semantic_policy_eval as evaluation
from test_continuity_eval import ScriptedProvider


def fixture(mixed=False):
    controls=[
        {'key':'window','role':'AXWindow','name':'Layout Lab','identifier':'main','parent':None},
        {'key':'draft-region','role':'AXGroup','name':'Working draft','identifier':'draft-group','parent':'window'},
        {'key':'draft','role':'AXTextField','name':'Body','identifier':'draft-editor','parent':'draft-region','state_key':'draft'},
        {'key':'reference-region','role':'AXGroup','name':'Reference note','identifier':'reference-group','parent':'window'},
        {'key':'reference','role':'AXTextField','name':'Body','identifier':'reference-editor','parent':'reference-region','state_key':'reference'},
        {'key':'appearance','role':'AXButton','name':'Quiet','identifier':'document-quiet','parent':'window','state_key':'appearance','help':'Document appearance only'},
        {'key':'badge','role':'AXButton','name':'Quiet','identifier':'badge-quiet','parent':'window','state_key':'badge','help':'Badge appearance only'},
        {'key':'save','role':'AXButton','name':'Save','identifier':'save-document','parent':'window'}]
    initial={'draft':'old','reference':'keep','appearance':False,'badge':True,'saved_bytes':'original','auxiliary':'untouched'}
    literal='  Café "ready"\nλ  '
    spec={'id':'mixed' if mixed else 'editor','split':'development','family':'mixed' if mixed else 'editor',
          'controls':controls,'initial':initial,'force_compaction':True,
          'request':'Replace Body in Working draft with '+json.dumps(literal,ensure_ascii=False)+'. Keep Reference note unchanged. Do not save.'+
                    (' Select Quiet for Document appearance; keep Badge appearance unchanged.' if mixed else '')}
    goals=[{'key':'draft','kind':'text','value':literal,'evidence_plane':'editor_buffer'}]
    preserves=[{'key':'reference','property':'value','value':'keep'}]
    if mixed:
        goals.append({'key':'appearance','kind':'state','value':True,'property':'selected','evidence_plane':'display'})
        preserves.append({'key':'badge','property':'selected','value':True})
    state=deepcopy(initial)
    for goal in goals:state[goal['key']]=goal['value']
    oracle={'goals':goals,'preserves':preserves,'expected_state':state,'required_effect_keys':[g['key'] for g in goals],
            'minimum_dispatches':len(goals),'maximum_dispatches':len(goals),'no_save':True}
    return spec,oracle


class ScriptedSemanticProvider(ScriptedProvider):
    """Predeclared CPU choices test wiring; this is never a model score."""
    def __init__(self,spec,oracle,*,wrong_target=False,repeat_bad=False,summary=None):
        super().__init__(spec)
        self.oracle=oracle;self.stage='apps';self.remaining=deepcopy(oracle['goals'])
        self.wrong_target=wrong_target;self.repeat_bad=repeat_bad;self.summary=summary
        self.by_key={c['key']:c for c in spec['controls']};self.selected={}

    def remember(self,output):
        rows=[r for r in output.get('items',[]) if isinstance(r,dict) and r.get('target')]
        for key,spec in self.by_key.items():
            match=next((r for r in rows if r.get('identifier')==spec['identifier']),None)
            if match:self.selected[key]=match['target']

    def next_call(self,output):
        if self.repeat_bad:return 'locua_inspect',{'view':'invented'}
        if self.stage=='apps':self.stage='windows';return 'locua_apps',{'query':'Layout Lab'}
        if self.stage=='windows':self.stage='observe';return 'locua_windows',{'app_id':output['items'][0]['app_id']}
        if self.stage=='observe':
            self.saved['window']=output['windows'][0]['window_id'];self.stage='inspect'
            return 'locua_observe',{'window_id':self.saved['window']}
        if self.stage in ('inspect','refresh_inspect'):
            self.stage='review' if self.stage=='inspect' else 'next_action'
            return 'locua_inspect',{'view':output['view']}
        if self.stage=='review':
            self.remember(output);self.stage='approved'
            goals=[]
            for goal in self.oracle['goals']:
                key='reference' if self.wrong_target and goal['key']=='draft' else goal['key']
                item={k:v for k,v in goal.items() if k not in ('key','evidence_plane')}
                goals.append({**item,'target':self.selected[key]})
            return 'locua_review',{'summary':self.summary or self.spec['request'],'goals':goals,'covers_request':True,
                'preserve':[{'target':self.selected[p['key']],'property':p['property']} for p in self.oracle['preserves']]}
        if self.stage=='approved':
            if output.get('status')!='approved':return None
            self.saved['review']=output['review'];self.stage='next_action'
        if self.stage=='next_action':
            self.remember(output);goal=self.remaining.pop(0);self.stage='acted'
            args={'target':self.selected[goal['key']],'operation':'set_text' if goal['kind']=='text' else 'press'}
            if goal['kind']=='text':args['value']=goal['value']
            return 'locua_act',args
        if self.stage=='acted':
            if self.remaining:
                self.stage='refresh_inspect';return 'locua_observe',{'window_id':self.saved['window']}
            self.stage='done';return 'locua_verify',{}
        return None


class ActualLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,*,profile='continuity-v1',mixed=False,**kwargs):
        spec,oracle=fixture(mixed);provider=ScriptedSemanticProvider(spec,oracle,**kwargs)
        with tempfile.TemporaryDirectory() as temp:
            try:
                report=await evaluation.run_case(spec,oracle,Path(temp)/'run',provider,profile=profile,
                                                 timeout_s=10,review_wait_s=0)
                session=json.loads((Path(temp)/'run/session/session.json').read_text())
            finally:await provider.close()
        return report,provider,session

    async def test_actual_multistep_review_input_and_fresh_verification_both_projections(self):
        for profile in evaluation.PROFILES:
            for mixed in (False,True):
                with self.subTest(profile=profile,mixed=mixed):
                    report,provider,session=await self.run_case(profile=profile,mixed=mixed)
                    self.assertEqual(report['status'],'passed',report)
                    self.assertTrue(report['audit']['independent_post_input_ui_verified'])
                    self.assertTrue(report['audit']['requested_action_executed'])
                    self.assertEqual(report['audit']['dispatches'],2 if mixed else 1)
                    self.assertEqual(session['config']['session']['context']['config'],evaluation.CONTEXT_CONFIG)
                    self.assertTrue(all(provider.spec['request'] in row['messages'][0]['content'] for row in provider.seen))
                    self.assertTrue(report['first_pass']['all_published_arguments_valid'])

    async def test_same_named_wrong_target_is_rejected_before_input(self):
        report,_,_=await self.run_case(wrong_target=True)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['failure_category'],'review_semantic_mismatch')
        self.assertEqual(report['audit']['dispatches'],0)

    async def test_unknown_review_prose_remains_pending_not_model_failure(self):
        report,_,_=await self.run_case(summary='Replace the working editor, preserving the reference.')
        self.assertEqual(report['status'],'pending_review',report)
        self.assertEqual(report['failure_category'],'review_audit_pending')
        self.assertEqual(report['audit']['dispatches'],0)

    async def test_two_equivalent_failures_stop_without_third_model_decision(self):
        report,provider,_=await self.run_case(repeat_bad=True)
        self.assertEqual(len(provider.seen),2)
        self.assertEqual(report['failure_category'],'repeated_nonprogress',report)
        self.assertEqual(report['audit']['dispatches'],0)

    async def test_bound_parent_attestation_resumes_loop_and_wait_is_outside_budget(self):
        spec,oracle=fixture();provider=ScriptedSemanticProvider(spec,oracle,summary='Replace the working editor and preserve the reference.')
        errors=[]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'run';pending=root/'review-pending.json'
            def reviewer():
                try:
                    deadline=time.monotonic()+5
                    while not pending.exists() and time.monotonic()<deadline:time.sleep(.01)
                    time.sleep(1.1)
                    evaluation.attest(pending,decision='approve',reviewer='CPU oracle',reason='Exact requested scope and faithful summary checked.')
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=reviewer);thread.start()
            try:
                report=await evaluation.run_case(spec,oracle,root,provider,profile='semantic-v1',timeout_s=1,review_wait_s=3)
            finally:
                thread.join(timeout=5);await provider.close()
        self.assertEqual(errors,[])
        self.assertEqual(report['status'],'passed',report)
        self.assertGreater(report['review_wait_s'],1)
        self.assertLess(report['elapsed_s'],1)
        self.assertEqual(report['manager_prose_reviews'],1)


class ProjectionContractTests(unittest.TestCase):
    def test_all_three_explicit_projections_keep_exact_schema_help_and_context(self):
        spec,_=fixture()
        self.assertEqual(evaluation.PROFILES,('continuity-v1','semantic-v1','semantic-v2'))
        self.assertEqual(evaluation.EVAL_CONTEXT,evaluation.CONTEXT_CONFIG)
        with tempfile.TemporaryDirectory() as temp:
            interfaces={profile:evaluation._interface_spec(profile,spec,Path(temp)/profile)
                        for profile in evaluation.PROFILES}
        self.assertEqual(interfaces['continuity-v1'],interfaces['semantic-v1'])
        self.assertEqual(interfaces['continuity-v1'],interfaces['semantic-v2'])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.phase=self.root/'phase';self.phase.mkdir()
        (self.phase/'phase.json').write_text('{"fixed":"source"}')
        (self.root/'exposure-ledger.json').write_text('{"events":[],"closed_cases":[]}')
        self.spec={'id':'fresh','split':'held-out'}

    def record(self,report=None,**kwargs):
        return evaluation.exposure(self.root,kwargs.get('phase',self.phase),kwargs.get('model','baseline'),
            kwargs.get('profile','continuity-v1'),self.spec,report=report)

    def test_atomic_started_finished_append_and_duplicate_rejection(self):
        self.record();self.record({'status':'passed','failure_category':'component_verified'})
        ledger=json.loads((self.root/'exposure-ledger.json').read_text())
        self.assertEqual([e['event'] for e in ledger['events']],['started','finished'])
        self.assertEqual((self.root/'exposure-ledger.json').stat().st_mode&0o777,0o600)
        with self.assertRaisesRegex(ValueError,'already attempted'):self.record()
        self.assertEqual(json.loads((self.root/'exposure-ledger.json').read_text()),ledger)

    def test_shared_exposure_allows_frozen_other_model_but_not_retuned_phase(self):
        self.record();self.record(model='comparator',profile='semantic-v1')
        other=self.root/'phase2';other.mkdir();(other/'phase.json').write_text('{"changed":"source"}')
        with self.assertRaisesRegex(ValueError,'globally fresh'):self.record(model='qwen38',phase=other)

    def test_two_informed_failures_stop_lane_and_preserve_ledger(self):
        self.spec['split']='development'
        for name in ('first','second'):
            self.spec['id']=name;self.record();self.record({'status':'failed','failure_category':'reference_or_grounding'})
        self.spec['id']='third'
        with self.assertRaisesRegex(ValueError,'two equivalent'):self.record()

    def test_atomic_replace_preserves_exact_unicode(self):
        path=self.root/'decision.json'
        evaluation.replace_json(path,{'value':'first'})
        evaluation.replace_json(path,{'value':'  Café\nλ  '})
        self.assertEqual(json.loads(path.read_text())['value'],'  Café\nλ  ')


if __name__=='__main__':unittest.main()
