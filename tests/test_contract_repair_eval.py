"""Offline evidence checks. No model, driver, time rewriting, or GUI calls."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
spec=importlib.util.spec_from_file_location('contract_repair_eval',ROOT/'tools/contract_repair_eval.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
from locua.engine.prototype.native_driver_contract import CONTRACT_ID,SOURCE_FINGERPRINT
from locua.engine.prototype.perception import normalize_observation
from locua.goal_verification import bind_for_review


def captured(value,sid,stamp,*,document='Example.txt',duplicate=False):
    contract={'id':CONTRACT_ID,'source_fingerprint_sha256':SOURCE_FINGERPRINT,'platform':'macos',
              'plane':'editor_buffer','handle_binding':'same_snapshot_element_token'}
    editor={'contract':CONTRACT_ID,'plane':'editor_buffer','raw_value':{'status':'ok','value':value},
        'raw_value_recheck':{'status':'ok','value':value},'coherence':{'value_stable':True,'focus_stable':True},
        'value_settable':{'status':'ok','value':True},'focused':{'status':'ok','value':True}}
    elements=[{'role':'AXWindow','element_index':0,'element_token':sid+':0','label':document,'actions':['AXRaise']}]
    lines=[f'- [0] AXWindow "{document}" [id=window-identity actions=[raise]]']
    for index in ((1,2) if duplicate else (1,)):
        shown=value.strip()
        elements.append({'role':'AXTextArea','element_index':index,'element_token':f'{sid}:{index}',
            'parent_index':0,'label':shown,'value':shown,'editor':deepcopy(editor),'actions':['AXShowMenu'],
            'frame':{'x':10,'y':20,'w':300,'h':200}})
        lines.append(f'  - [{index}] AXTextArea = "{shown}" [id=Text View actions=[showmenu]]')
    raw={'pid':41,'window_id':52,'snapshot_id':sid,'elements_complete':False,
         'native_editor_contract':contract,'elements':elements,'tree_markdown':'\n'.join(lines)}
    obs=normalize_observation(raw,kind='native',expected_target={'pid':41,'window_id':52},observed_at_ns=stamp)
    return raw,obs


class EditorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.literal='  Quote "ok"\nnaïve — β  '
        self.goal={'id':'edit','kind':'text','target':'Contents of Example.txt','value':self.literal,'evidence_plane':'editor_buffer'}
        self.stamp=1_700_000_000_000_000_000
        self.raw,self.before=captured('Before\n','s1',self.stamp)
        self.raw_after,self.after=captured(self.literal,'s2',self.stamp+20_000_000)

    def test_literal_multiline_quotes_unicode_identity_and_raw_are_exact(self):
        original=deepcopy((self.before,self.after))
        report=a.retained_identity_check(self.before,self.before['controls'][1]['id'],self.goal,self.after)
        self.assertTrue(report['matched_at_capture']);self.assertTrue(report['exact_raw_matches_expected'])
        self.assertFalse(report['current_freshness_proven']);self.assertFalse(report['action_authority'])
        self.assertEqual((self.before,self.after),original)
        self.assertEqual(report['actual_utf8_sha256'],a.sha(self.literal.encode()))

    def test_expected_value_never_selects_a_competing_editor(self):
        _,after=captured(self.literal,'s2',self.stamp+20_000_000,duplicate=True)
        report=a.retained_identity_check(self.before,self.before['controls'][1]['id'],self.goal,after)
        self.assertFalse(report['matched_at_capture']);self.assertFalse(report['exact_raw_buffer'])

    def test_wrong_window_document_or_missing_exact_recheck_cannot_pass(self):
        for mutate in (
            lambda o:o['target'].update(window_id=53),
            lambda o:o['controls'][0].update(name='Other.txt'),
            lambda o:o['controls'][1]['editor'].pop('raw_value_recheck'),
            lambda o:o['controls'][1]['value_evidence'].update(exact_value_proven=False),
        ):
            after=deepcopy(self.after);mutate(after)
            try:report=a.retained_identity_check(self.before,self.before['controls'][1]['id'],self.goal,after)
            except ValueError:continue
            self.assertFalse(report.get('matched_at_capture') and report.get('exact_raw_matches_expected'))

    def test_whitespace_and_terminal_lf_are_never_repaired(self):
        for text in (self.literal+'\n',self.literal.strip(),self.literal.replace('\n','\r\n')):
            _,after=captured(text,'s2',self.stamp+20_000_000)
            report=a.retained_identity_check(self.before,self.before['controls'][1]['id'],self.goal,after)
            self.assertFalse(report['matched_at_capture']);self.assertFalse(report['exact_raw_matches_expected'])

    def test_freeze_is_exclusive_and_request_tampering_refuses(self):
        frozen=self.root/'freeze';a.freeze(frozen);public,oracle=a.load(frozen)
        self.assertIn(oracle['expected_buffer'],public['request'])
        self.assertTrue(oracle['expected_buffer'].startswith('  '));self.assertTrue(oracle['expected_buffer'].endswith('  '))
        with self.assertRaises(FileExistsError):a.freeze(frozen)
        (frozen/'request.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'changed'):a.load(frozen)

    def test_disk_attestation_does_not_modify_files_and_requires_fixture_names(self):
        frozen=self.root/'freeze';a.freeze(frozen);_,oracle=a.load(frozen);files={}
        for key,row in oracle['initial_files'].items():
            p=self.root/row['basename'];p.write_bytes(row['utf8'].encode());files[key]=p
        result=a.disk_attestation(frozen,files['target'],files['sentinel'])
        self.assertTrue(result['read_only'])
        for key,path in files.items():self.assertEqual(a.sha(path.read_bytes()),result['files'][key]['sha256'])
        with self.assertRaises(ValueError):a.disk_attestation(frozen,files['sentinel'],files['target'])

    def test_exposed_disk_attestation_records_current_state_without_historical_rewrite(self):
        frozen=self.root/'freeze';a.freeze(frozen);task=a.exposed_task(frozen)
        p=self.root/task['document'];p.write_bytes(task['expected'].encode())
        report=a.exposed_disk_attestation(frozen,p)
        self.assertEqual(report['files']['target']['sha256'],a.sha(task['expected'].encode()))
        self.assertFalse(report['historical_disk_state_or_cause_inferred'])
        self.assertFalse(report['other_document_preservation_proven'])
        self.assertEqual(report['mode'],'exposed_regression_current_disk_baseline')

    def test_missing_expected_file_hash_cannot_appear_as_empty_file_preservation(self):
        oracle,before,after=self.disk_pair()
        oracle['initial_files']['target']['sha256']=None
        before['files']['target']['sha256']=after['files']['target']['sha256']=None
        result=a.compare_disks(before,after,oracle,first_write_start_ns=self.stamp+10_000_000,last_write_end_ns=self.stamp+11_000_000)
        self.assertFalse(result['all_fixture_files_unchanged'])

    def disk_pair(self):
        oracle={'initial_files':{key:{'basename':key+'.txt','utf8':'Initial\n','sha256':a.sha(b'Initial\n')} for key in ('target','sentinel')}}
        before={'files':{key:{'path':'/synthetic/'+key+'.txt','sha256':a.sha(b'Initial\n'),'bytes':8,
            'captured_at_ns':self.stamp-100,'device':1,'inode':2,'mtime_ns':3} for key in oracle['initial_files']}}
        after=deepcopy(before)
        for row in after['files'].values():row['captured_at_ns']=self.stamp+40_000_000
        return oracle,before,after

    def test_preservation_requires_pre_first_input_and_post_last_input(self):
        oracle,before,after=self.disk_pair()
        def check():return a.compare_disks(before,after,oracle,first_write_start_ns=self.stamp+10_000_000,last_write_end_ns=self.stamp+11_000_000)
        self.assertTrue(check()['all_fixture_files_unchanged']);self.assertFalse(check()['unobserved_other_buffers_or_global_desktop_preservation_proven'])
        before['files']['target']['captured_at_ns']=self.stamp+10_500_000
        self.assertFalse(check()['all_fixture_files_unchanged'])
        before['files']['target']['captured_at_ns']=self.stamp-100
        after['files']['sentinel']['captured_at_ns']=self.stamp+10_500_000
        self.assertFalse(check()['all_fixture_files_unchanged'])

    def test_same_unapproved_changed_bytes_or_other_path_are_not_preservation(self):
        for mutation in ('bytes','path','metadata'):
            oracle,before,after=self.disk_pair()
            if mutation=='bytes':
                for record in (before,after):record['files']['target']['sha256']=a.sha(b'Wrong!!\n')
            elif mutation=='path':after['files']['target']['path']='/other/target.txt'
            else:after['files']['target']['mtime_ns']=4
            result=a.compare_disks(before,after,oracle,first_write_start_ns=self.stamp+10_000_000,last_write_end_ns=self.stamp+11_000_000)
            self.assertEqual(result['all_fixture_files_unchanged'],mutation=='metadata')
            if mutation=='metadata':self.assertFalse(result['files']['target']['metadata_equal'])

    def final_parts(self):
        binding=bind_for_review(self.goal,self.before['controls'][1],self.before)
        check=a.retained_identity_check(self.before,self.before['controls'][1]['id'],self.goal,self.after)
        captures=[{'snapshot_id':'s2','observed_at_ns':self.after['observed_at_ns'],'contract_check':check}]
        scope={'target':self.before['target'],'goals':[self.goal],'bindings':{'edit':binding},'effects':[{'kind':'goal','goal_id':'edit'}],
               'preserves':[],'covers_entire_request':True,'unresolved_requirements':[]}
        evidence={'binding_id':binding['id'],'target':self.before['target'],'snapshot_id':'s2','observed_at_ns':self.after['observed_at_ns'],
            'control_id':self.after['controls'][1]['id'],'property':'value','plane':'editor_buffer','actual':self.literal}
        final={'request_coverage':{'scope_ids':['scope']},'verification':{'scopes':{'scope':{'goals':[{'goal_id':'edit','matched':True,'evidence':evidence}]}}}}
        return scope,final,captures

    def test_final_proof_must_name_original_scope_goal_and_latest_exact_capture(self):
        scope,final,captures=self.final_parts()
        def check():return a.final_original_scope_check(final,'scope',self.goal,self.before['target'],captures,{'scopes':{'scope':scope}},'s2')
        self.assertTrue(check()['matched_latest_original_scope_predicate'])
        final['verification']['scopes']['scope']['goals'][0]['goal_id']='another'
        self.assertFalse(check()['matched_latest_original_scope_predicate'])

    def test_later_drift_or_failed_read_cannot_use_earlier_matching_history(self):
        scope,final,captures=self.final_parts()
        report=a.final_original_scope_check(final,'scope',self.goal,self.before['target'],captures,{'scopes':{'scope':scope}},None)
        self.assertFalse(report['matched_latest_original_scope_predicate'])
        captures.append({'snapshot_id':'s3','observed_at_ns':self.stamp+30_000_000,'contract_check':{'matched_at_capture':False}})
        report=a.final_original_scope_check(final,'scope',self.goal,self.before['target'],captures,{'scopes':{'scope':scope}},'s3')
        self.assertFalse(report['matched_latest_original_scope_predicate'])

    def test_reconciliation_keeps_original_contract_and_unknown_delivery(self):
        scope,_,_=self.final_parts();digest=a.sha(a.encoded(scope));scope['uncertain_action']={'reviewed_contract_sha256':digest,
            'completed_at_ns':self.stamp+11_000_000,'pre_snapshot_id':'s1','returned_snapshot_id':'uncertain'}
        proof={'status':'current_predicates_verified','snapshot_id':'s2','observed_at_ns':self.after['observed_at_ns'],
            'original_contract_sha256':digest,'original_uncertain_receipt_preserved':True,'delivery_proven':False,
            'other_side_effects_proven_absent':False,'input_authority_restored':False}
        event={'sequence':4,'input':{'scope_id':'scope','reconcile':True},'result':{'status':'verified','scope_id':'scope',
            'scope_status':'reconciled_verified','action_started':False,'no_retry':True,'reconciliation':proof}}
        def check():return a.reconciliation_check(event,{'scope':scope},{'s2':(self.after,None)})
        self.assertTrue(check()['saved_contract_and_receipt_consistent'])
        self.assertFalse(check()['delivery_proven']);self.assertFalse(check()['input_authority_restored'])
        scope['goals'][0]['value']='Changed review'
        self.assertFalse(check()['saved_contract_and_receipt_consistent'])

    def test_reconciliation_cannot_restore_authority_or_reuse_failed_capture(self):
        scope,_,_=self.final_parts();digest=a.sha(a.encoded(scope));scope['uncertain_action']={'reviewed_contract_sha256':digest,
            'completed_at_ns':self.stamp+11_000_000,'pre_snapshot_id':'s1','returned_snapshot_id':'s2'}
        proof={'status':'current_predicates_verified','snapshot_id':'s2','observed_at_ns':self.after['observed_at_ns'],
            'original_contract_sha256':digest,'original_uncertain_receipt_preserved':True,'delivery_proven':False,
            'other_side_effects_proven_absent':False,'input_authority_restored':True}
        event={'sequence':4,'input':{'scope_id':'scope','reconcile':True},'result':{'status':'verified','scope_id':'scope',
            'scope_status':'reconciled_verified','action_started':False,'no_retry':True,'reconciliation':proof}}
        result=a.reconciliation_check(event,{'scope':scope},{'s2':(self.after,None)})
        self.assertFalse(result['saved_contract_and_receipt_consistent'])

    def saved_run(self):
        run=self.root/'run';scope,final,_=self.final_parts()
        raw3,obs3=captured(self.literal,'s3',self.stamp+30_000_000)
        final['verification']['scopes']['scope']['goals'][0]['evidence'].update(
            snapshot_id='s3',observed_at_ns=obs3['observed_at_ns'],control_id=obs3['controls'][1]['id'])
        final.update(all_reviewed_goals_verified=True,scope_statuses={'scope':'approved'})
        final['request_coverage']['user_reviewed_complete_declaration']=True
        summary={'request':'Synthetic exact editor request','status':'verified_reviewed_scope','wall_excluding_human_s':4,'verification':final}
        events=[
            {'tool':'locua_apps','input':{},'result':{'items':[{'app_id':'app','name':'TextEdit'}]}},
            {'tool':'locua_windows','input':{'app_id':'app'},'result':{'windows':[{'target':self.before['target'],'identity_proven':True}]}},
            {'tool':'locua_review','input':{'snapshot_id':'s1','goals':[{**self.goal,'control_id':self.before['controls'][1]['id']}]},'result':{'status':'approved','scope_id':'scope'}},
            {'tool':'locua_act','input':{'scope_id':'scope'},'result':{'status':'verified','action_started':True}},
        ]
        def read_row(raw,stamp):return {'started_at_ns':stamp,'request':{'name':'get_window_state','arguments':self.before['target']},'response':{'wall_ms':1,'result':{'structuredContent':raw}}}
        driver=[read_row(self.raw,self.stamp),{'started_at_ns':self.stamp+10_000_000,
            'request':{'name':'set_value','arguments':{**self.before['target'],'element_token':'s1:1','value':self.literal}},
            'response':{'wall_ms':1,'result':{'structuredContent':{'effect':'confirmed'}}}},
            read_row(self.raw_after,self.after['observed_at_ns']),read_row(raw3,obs3['observed_at_ns'])]
        a.write(run/'summary.json',summary);a.write(run/'desktop/evidence.json',{'scopes':{'scope':scope}})
        a.write(run/'desktop/observation-s1.json',self.before)
        for index,event in enumerate(events,1):event['sequence']=index;a.write(run/f'desktop/event-{index:03}.json',event)
        path=run/'desktop/desktop/cua/transport.jsonl';path.parent.mkdir(parents=True);path.write_text('\n'.join(json.dumps(row) for row in driver))
        oracle,before,after=self.disk_pair()
        return run,oracle,before,after

    def saved_audit(self,fixture):
        run,oracle,before,after=fixture
        return a.audit_saved(run,request='Synthetic exact editor request',expected=self.literal,document='Example.txt',
            disk_before=before,disk_after=after,oracle=oracle)

    def test_full_saved_transport_replay_retains_raw_timestamps_and_final_proof(self):
        fixture=self.saved_run();result=self.saved_audit(fixture)
        self.assertEqual(result['issues'],[]);self.assertTrue(result['functional_pass']);self.assertTrue(result['practical_pass'])
        self.assertEqual(len(result['reconstructed_capture_sources']),2)
        self.assertEqual([row['observed_at_ns'] for row in result['capture_checks']],
            [self.stamp+20_000_000,self.stamp+30_000_000])
        self.assertEqual(result['recorded_task_input_count'],1);self.assertFalse(result['independent_language_coverage_proven'])

    def test_matching_buffers_do_not_erase_historical_cli_failure(self):
        fixture=self.saved_run();p=fixture[0]/'summary.json';summary=a.read(p);summary['status']='incomplete';p.write_text(json.dumps(summary))
        result=self.saved_audit(fixture)
        self.assertTrue(result['two_distinct_later_exact_buffer_captures']);self.assertFalse(result['functional_pass'])

    def test_wrong_application_and_missing_original_scope_receipt_refuse(self):
        fixture=self.saved_run();p=fixture[0]/'desktop/event-001.json';event=a.read(p);event['result']['items'][0]['name']='Other app';p.write_text(json.dumps(event))
        p=fixture[0]/'summary.json';summary=a.read(p);summary['verification']['verification']['scopes']={};p.write_text(json.dumps(summary))
        result=self.saved_audit(fixture)
        self.assertIn('requested_application_identity_unproven',result['issues'])
        self.assertIn('final_original_scope_predicate_unproven_or_later_drift',result['issues']);self.assertFalse(result['functional_pass'])

    def test_no_save_route_can_be_hidden_by_matching_later_buffer(self):
        fixture=self.saved_run();p=fixture[0]/'desktop/desktop/cua/transport.jsonl'
        row={'started_at_ns':self.stamp+12_000_000,'request':{'name':'perform_action','arguments':{'action':'save'}},'response':{'wall_ms':1}}
        p.write_text(p.read_text()+'\n'+json.dumps(row))
        result=self.saved_audit(fixture)
        self.assertIn('unreconciled_write_or_unsupported_route_manual_audit',result['issues']);self.assertFalse(result['functional_pass'])


if __name__=='__main__':unittest.main()
