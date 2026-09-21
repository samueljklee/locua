"""Recorded sequence evaluator checks; actual guards, no model or desktop."""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('action_sequence_audit',ROOT/'tools/action_sequence_audit.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
from locua.action_sequence import run_sequence
import test_action_sequence as fixture


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SequenceTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.owner=self.f.owner;self.run=Path(self.f.tmp.name);self.scope=self.f.review_calc()

    def capture(self,names=('All Clear','2','+','3','=')):
        steps=[self.f.step(name) for name in names]
        result=run_sequence(self.owner,self.scope,self.f.sid,steps)
        event={'sequence':1,'tool':'locua_act_sequence','input':{'scope_id':self.scope,'snapshot_id':self.f.sid,'steps':steps},'result':result}
        snapshots={sid:(deepcopy(o),'synthetic:'+sid) for sid,o in self.owner._observations.items()}
        return event,snapshots

    def audit(self,event,snapshots):return a.sequence_receipts(event,self.run,snapshots)
    def replace(self,path,obj):Path(path).write_text(json.dumps(obj))

    def test_actual_guarded_sequence_not_task_completion(self):
        event,snapshots=self.capture();acts,report=self.audit(event,snapshots)
        self.assertEqual(report['issues'],[]);self.assertEqual(len(acts),5)
        self.assertEqual(report['steps_completed'],5);self.assertFalse(report['task_completion_inferred'])
        self.assertTrue(all(x['pre_dispatch_observation']['snapshot_id']!=x['input']['snapshot_id'] for x in acts))

    def test_thirtytwo_steps_reconcile_full_private_prefix_and_public_tail(self):
        event,snapshots=self.capture(['All Clear']*32)
        self.assertEqual(len(event['result']['receipts']),8)
        acts,report=self.audit(event,snapshots)
        self.assertEqual(report['issues'],[]);self.assertEqual(len(acts),32)
        self.assertEqual(report['steps_attempted'],32);self.assertEqual(len(report['step_evidence']),32)

    def test_completed_single_action_counts_even_when_post_transition_stops(self):
        from locua.action_sequence import check_transition
        def stop(before,after,**kwargs):
            if kwargs.get('acted_control_id') is not None:return {'matched':False,'reason':'synthetic topology change'}
            return check_transition(before,after,**kwargs)
        with patch('locua.action_sequence.check_transition',side_effect=stop):event,snapshots=self.capture()
        acts,report=self.audit(event,snapshots)
        self.assertEqual(report['issues'],[]);self.assertEqual(report['steps_completed'],1)
        self.assertEqual(report['steps_attempted'],1);self.assertTrue(report['partial']);self.assertEqual(len(acts),1)
        self.assertEqual(report['terminal_step']['post_transition']['matched'],False)

    def test_zero_attempt_cancellation_not_missing_evidence(self):
        event={'input':{'steps':[{'action_id':'a'}]},'result':{'status':'canceled','action_started':False,'steps_attempted':0}}
        acts,report=self.audit(event,{})
        self.assertEqual(acts,[]);self.assertEqual(report['issues'],[]);self.assertTrue(report['preflight_only'])

    def test_missing_start_flag_needs_independent_zero_write_prevalidation_proof(self):
        event={'input':{'steps':[{'action_id':'a'}]},'result':{'status':'refused','reason':'Approved scope required'}}
        _,without=self.audit(event,{})
        self.assertIn('missing_or_invalid_source_evidence',without['issues'])
        acts,proved=a.sequence_receipts(event,self.run,{},zero_write_prevalidation=True)
        self.assertEqual(proved['issues'],[]);self.assertEqual(acts,[])
        self.assertEqual(proved['steps_attempted'],0)
        self.assertNotIn('action_started',event['result'])

    def test_omitted_private_prefix_not_silently_credited(self):
        event,snapshots=self.capture(['All Clear']*12)
        source=Path(event['result']['source_evidence_ref'])
        source.with_name(source.name.replace('-source.json','-step-001.json')).unlink()
        _,report=self.audit(event,snapshots)
        self.assertIn('receipt_counter_mismatch:steps_attempted',report['issues'])
        self.assertIn('private_step_differs_from_model_proposal',report['issues'])

    def test_altered_original_model_proposal_refused(self):
        event,snapshots=self.capture();event['input']['steps'][1]['action_id']='made-up'
        _,report=self.audit(event,snapshots)
        self.assertIn('step_differs_from_model_original_proposal',report['issues'])

    def test_fresh_dispatch_proof_must_match_retained_snapshot(self):
        event,snapshots=self.capture();path=event['result']['receipts'][0]['full_response_ref'].split('#')[0]
        record=a.read(path);record['pre_dispatch_observation']['target']['pid']=999;self.replace(path,record)
        _,report=self.audit(event,snapshots)
        self.assertIn('missing_or_conflicting_fresh_dispatch_proof',report['issues'])

    def test_frozen_source_action_must_equal_original_issued_descriptor(self):
        event,snapshots=self.capture();catalogs={sid:{'actions':deepcopy(actions)} for sid,actions in self.owner._actions.items()}
        _,valid=a.sequence_receipts(event,self.run,snapshots,catalogs=catalogs)
        self.assertEqual(valid['issues'],[])
        path=event['result']['source_evidence_ref'];source=a.read(path)
        source['steps'][1]['action']['control_id']=source['steps'][2]['action']['control_id'];self.replace(path,source)
        _,bad=a.sequence_receipts(event,self.run,snapshots,catalogs=catalogs)
        self.assertIn('frozen_step_differs_from_issued_source_descriptor',bad['issues'])

    def test_public_private_receipt_disagreement(self):
        event,snapshots=self.capture();event['result']['receipts'][0]['action_started']=False
        _,report=self.audit(event,snapshots)
        self.assertIn('public_private_step_receipt_mismatch',report['issues'])

    def test_sequence_receipt_cannot_claim_goal_success(self):
        event,snapshots=self.capture();event['result']['goal_verified']=True
        _,report=self.audit(event,snapshots)
        self.assertIn('sequence_receipt_claims_task_verification',report['issues'])

    def test_uncertain_private_step_prevents_further_attempts(self):
        event,snapshots=self.capture();path=event['result']['receipts'][1]['full_response_ref'].split('#')[0]
        record=a.read(path);record['result'].update(status='uncertain',action_started=None);record['transition']=None;self.replace(path,record)
        _,report=self.audit(event,snapshots)
        self.assertIn('step_attempted_after_terminal_sequence_receipt',report['issues'])
        self.assertIn('false_sequence_completion',report['issues'])

    def test_path_escape_refused(self):
        event,snapshots=self.capture();event['result']['source_evidence_ref']=str(ROOT/'pyproject.toml')
        _,report=self.audit(event,snapshots)
        self.assertIn('missing_or_invalid_source_evidence',report['issues'])


class FrozenAndOutcomeTests(unittest.TestCase):
    def test_amendment_is_explicit_and_original_suite_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite=Path(tmp)/'suite.json';change=Path(tmp)/'change.json'
            old={'id':'editor','request':'old','oracle':{'value':'before'}}
            new={'id':'editor','request':'new','oracle':{'value':'  exact\nUnicode β  '}}
            body={'tasks':[old]};a.write(suite,{**body,'content_sha256':a.digest(body)})
            body={'original_suite':a.file_record(suite),'replace_task_id':'editor','task':new}
            a.write(change,{**body,'content_sha256':a.digest(body)})
            original=suite.read_bytes();_,actual_old=a.task_from_suite(suite,'editor');_,actual_new=a.task_from_suite(suite,'editor',change)
            self.assertEqual(actual_old,old);self.assertEqual(actual_new,new);self.assertEqual(original,suite.read_bytes())
            body['task']['request']='changed';change.write_text(json.dumps(body))
            with self.assertRaises(ValueError):a.task_from_suite(suite,'editor',change)

    def test_sequence_completion_or_assistant_answer_without_independent_proof_fails(self):
        task={'id':'calculator','app_name':'Calculator','oracle':{'kind':'calculation','expression':'(81-29)/4','numerator':13,'denominator':1}}
        result=a.final_outcomes(task,{'status':'verified_reviewed_scope','assistant_response':'13'}, {'scopes':{}},{},[],{})
        self.assertFalse(result['pass'])

    def test_wrong_evaluated_expression_precedes_later_topology_stop(self):
        task={'oracle':{'kind':'calculation','expression':'192*231-100'}}
        result=a.classify_failure(task,{'status':'blocked'},[],{'pass':False},
            [{'partial':True,'terminal_step':{'step':13,'post_transition':{'matched':False}}}],
            {'scope:1':{'issued_evaluation':'102*123-100'}})
        self.assertEqual(result['classification'],'issued_evaluation_differs_from_requested_expression')
        self.assertTrue(result['later_transition_stop_is_not_the_input_error'])

    def test_unsupported_family_kept_as_gap(self):
        result=a.final_outcomes({'oracle':{'kind':'presentation'}},{},{},{},[],{})
        self.assertFalse(result['pass']);self.assertFalse(result['supported'])

    def test_model_unknown_usage_is_not_zero_and_read_counts_not_nonprogress(self):
        result=a.common.count_metrics([{'call':1,'generation':{'generation_calls':1,'usage':{'input_tokens':10,'output_tokens':2},'timing':{'generation_ms':20}}},
            {'call':2,'inference_started':True}],{'provider':'local','metrics':{'provider_requests':2,'unknown_generation_usage':True}})
        self.assertIsNone(result['model_calls']);self.assertEqual(result['unknown_calls'],[2]);self.assertEqual(result['known_input_tokens'],10)


class FinalProofTests(unittest.TestCase):
    def setUp(self):
        import test_comparison_audit as original
        self.f=original.AuditTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        self.task={'id':'editor','app_name':'TextEdit','oracle':{'kind':'text','document':'Example.txt','value':'Done.  '}}
        self.evidence=deepcopy(f.evidence)
        self.evidence['events']=[{'tool':'locua_apps','input':{},'result':{'items':[{'app_id':'app','name':'TextEdit','bundle_id':'test'}]}},
            {'tool':'locua_windows','input':{'app_id':'app'},'result':{'windows':[{'target':f.before['target'],'identity_proven':True}]}}]
        self.snapshots={'s1':(f.before,'before'),'s2':(f.after,'after')}
        self.transport=deepcopy(f.driver)+[{'started_at_ns':f.after['observed_at_ns'],
            'request':{'name':'get_window_state','arguments':f.after['target']},
            'response':{'result':{'structuredContent':{'snapshot_id':'s2'}}}}]
        self.summary={'status':'verified_reviewed_scope','verification':{'all_reviewed_goals_verified':True,
            'request_coverage':{'user_reviewed_complete_declaration':True},
            'verification':{'scopes':{'scope':{'goals':[deepcopy(f.proof)]}}}}}

    def run_check(self):return a.final_outcomes(self.task,self.summary,self.evidence,self.snapshots,self.transport,{})

    def test_latest_independent_original_binding_is_required(self):
        self.assertTrue(self.run_check()['pass'])
        self.transport.append({'started_at_ns':self.f.after['observed_at_ns']+1,
            'request':{'name':'get_window_state','arguments':self.f.after['target']},'response':{'error':'unavailable'}})
        self.assertFalse(self.run_check()['pass'])

    def test_later_target_drift_never_credits_earlier_matching_buffer(self):
        self.assertTrue(self.run_check()['pass'])
        later=self.f.observation('s3','changed',self.f.after['observed_at_ns']+2)
        self.snapshots['s3']=(later,'later');self.transport.append({'started_at_ns':later['observed_at_ns'],
            'request':{'name':'get_window_state','arguments':later['target']},'response':{'result':{'structuredContent':{'snapshot_id':'s3'}}}})
        self.assertFalse(self.run_check()['pass'])

    def test_named_app_and_exact_whitespace_are_independent_requirements(self):
        self.evidence['events'][0]['result']['items'][0]['name']='Other app'
        self.assertFalse(self.run_check()['pass'])
        self.evidence['events'][0]['result']['items'][0]['name']='TextEdit'
        self.task['oracle']['value']='Done.'
        self.assertFalse(self.run_check()['pass'])

    def test_proposal_mapping_uses_actual_pure_catalog_not_expected_actions(self):
        from threading import RLock
        from types import SimpleNamespace
        from locua.desktop_tools import DesktopTools
        o=self.f.before
        facade=SimpleNamespace(_lock=RLock(),_issued_observation=lambda o:None,_catalogs={},
            _result=lambda operation,**x:x,_failure=lambda operation,error:{})
        # Synthetic observation advertises no native press route: unknown ID is not guessed.
        event={'sequence':1,'input':{'snapshot_id':'s1','scope_id':'fabricated', 'steps':[{'action_id':'invented'}]}}
        report=a.proposal_mapping(event,self.snapshots,self.evidence['scopes'])
        self.assertFalse(report['scope_exists']);self.assertTrue(report['source_snapshot_exists'])
        self.assertFalse(report['steps'][0]['found_in_reconstructed_original_catalog']);self.assertFalse(report['action_authority'])


class AliasCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.run=Path(self.tmp.name)
        self.obs={'kind':'native_window_state','snapshot_id':'s1','target':{'pid':1,'window_id':2},'observed_at_ns':1,
            'controls':[{'id':'c1','role':'AXButton','name':'One','actions':['AXPress'],'states':{}},
                        {'id':'c2','role':'AXButton','name':'Two','actions':['AXPress'],'states':{}}],
            'handles':{'c1':{'kind':'native'},'c2':{'kind':'native'}}}
        self.snapshots={'s1':(self.obs,'synthetic')}
        from locua.action_references import public_catalog
        public,private=public_catalog(list(a.historical_catalog(self.obs).values()),1)
        self.data={'snapshot_id':'s1','public_actions':public,'private_descriptors':private}
        self.path=self.run/'desktop/action-catalog-1.json';self.path.parent.mkdir()
        self.save()
    def save(self):self.path.write_text(json.dumps(self.data))

    def test_alias_keeps_full_catalog_and_exact_private_identity(self):
        catalogs,issues,_=a.load_action_catalogs(self.run,self.snapshots)
        self.assertEqual(issues,[]);self.assertEqual(len(catalogs['s1']['actions']),2)
        event={'input':{'snapshot_id':'s1','steps':[{'action_id':'action:1:2'}]}}
        mapped=a.proposal_mapping(event,self.snapshots,{},catalogs)['steps'][0]
        self.assertEqual(mapped['observed_name'],'Two')
        self.assertEqual(mapped['private_issued_action_id'],self.data['private_descriptors']['action:1:2']['id'])

    def test_alias_cannot_reassign_private_control_or_label(self):
        self.data['public_actions'][0]['name']='Two';self.save()
        catalogs,issues,_=a.load_action_catalogs(self.run,self.snapshots)
        self.assertTrue(issues);self.assertFalse(catalogs['s1']['valid'])

    def test_catalog_omission_does_not_make_ambiguous_competitor_disappear(self):
        self.data['public_actions'].pop();self.data['private_descriptors'].pop('action:1:2');self.save()
        _,issues,_=a.load_action_catalogs(self.run,self.snapshots)
        self.assertTrue(issues)


class ReviewedContractTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SequenceTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.scope_id=self.f.review_calc();self.owner=self.f.owner
        steps=[self.f.step(name) for name in ('All Clear','2','+','3','=')]
        result=run_sequence(self.owner,self.scope_id,self.f.sid,steps)
        self.source=a.read(result['source_evidence_ref'])
        self.event={'sequence':len(self.owner.evidence['events'])+1,'tool':'locua_act_sequence',
            'input':{'scope_id':self.scope_id,'snapshot_id':self.f.sid,'steps':steps},'result':result}
        self.evidence=deepcopy(self.owner.evidence)
        self.snapshots={sid:(o,'synthetic:'+sid) for sid,o in self.owner._observations.items()}

    def check(self):return a.reviewed_contract_check(self.event,self.source,self.evidence,self.snapshots)

    def test_hash_binds_original_approved_contract(self):
        checked=self.check();self.assertTrue(checked['matched'],checked)
        self.assertEqual(checked['source_contract_sha256'],checked['original_approved_contract_sha256'])
        self.source['reviewed_contract_sha256']='0'*64
        self.assertIn('sequence_reviewed_contract_hash_mismatch',self.check()['issues'])

    def test_later_goal_effect_preserve_or_coverage_change_cannot_reauthorize_source(self):
        original=deepcopy(self.evidence)
        for field,value in [('effects',[]),('preserves',[{'goal':{'value':'changed'}}]),
                            ('covers_entire_request',True),('unresolved_requirements',['new']),
                            ('goals',[{'id':'calc','kind':'calculation','expression':'99+1'}])]:
            with self.subTest(field=field):
                self.evidence=deepcopy(original);self.evidence['scopes'][self.scope_id][field]=value
                self.assertFalse(self.check()['matched'])
                self.assertIn('reviewed_contract_changed:'+field,self.check()['issues'])

    def rebind_data(self,*,before_sequence=False):
        import test_comparison_audit as original
        f=original.AuditTests();f.setUp();self.addCleanup(f.doCleanups);f.rebind_fixture()
        from locua.goal_verification import bind_for_review
        f.revision['previous_binding']=bind_for_review(f.goal,f.before['controls'][1],f.before)
        scope=deepcopy(f.scope);scope['unresolved_requirements']=[]
        review={'sequence':1,'tool':'locua_review','input':{'snapshot_id':'s1','goals':[{**f.goal,'control_id':'s1:1'}],
            'effects':deepcopy(scope['effects']),'preserves':[],'covers_entire_request':True},
            'result':{'status':'approved','scope_id':'scope'}}
        revision={'sequence':2 if before_sequence else 4,'tool':'locua_verify',
            'input':{'scope_id':'scope','goal_id':'goal','snapshot_id':'selected','control_id':'selected:1'},
            'result':{'binding_recovery':{'checked_snapshot_id':'checked'}}}
        snapshot=f.after if before_sequence else f.before
        contract={k:deepcopy(scope[k]) for k in ('target','goals','bindings','effects','preserves','covers_entire_request','unresolved_requirements')}
        if not before_sequence:contract['bindings']['goal']=deepcopy(f.revision['previous_binding'])
        source={'scope_id':'scope','source_observation':snapshot,'reviewed_contract_sha256':a.digest(contract)}
        evidence={'scopes':{'scope':scope},'events':[review,revision]}
        snapshots={o['snapshot_id']:(o,'synthetic') for o in (f.before,f.after,f.selected,f.checked)}
        return {'sequence':3},source,evidence,snapshots

    def test_legitimate_later_read_only_revision_keeps_original_source_hash(self):
        args=self.rebind_data();checked=a.reviewed_contract_check(*args)
        self.assertTrue(checked['matched'],checked)
        self.assertFalse(checked['read_only_binding_revisions'][0]['applied_before_sequence'])
        self.assertEqual(checked['source_contract_sha256'],checked['original_approved_contract_sha256'])

    def test_legitimate_earlier_revision_explicitly_changes_only_start_binding(self):
        args=self.rebind_data(before_sequence=True);checked=a.reviewed_contract_check(*args)
        self.assertTrue(checked['matched'],checked)
        self.assertTrue(checked['read_only_binding_revisions'][0]['applied_before_sequence'])
        self.assertNotEqual(checked['source_contract_sha256'],checked['original_approved_contract_sha256'])

    def test_revision_cannot_cover_changed_goals_or_missing_chronology(self):
        event,source,evidence,snapshots=self.rebind_data()
        evidence['scopes']['scope']['effects']=[]
        checked=a.reviewed_contract_check(event,source,evidence,snapshots)
        self.assertIn('reviewed_contract_changed:effects',checked['issues'])
        event,source,evidence,snapshots=self.rebind_data();evidence['events'].pop()
        checked=a.reviewed_contract_check(event,source,evidence,snapshots)
        self.assertFalse(checked['matched']);self.assertIn('binding_revision_event_not_unique:goal',checked['issues'])

if __name__=='__main__':unittest.main()
