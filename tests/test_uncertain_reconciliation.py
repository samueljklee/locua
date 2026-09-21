"""Uncertain input recovery is proof of current predicates, never replay authority."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_amplifier_tools as fixtures
from locua.amplifier_tools import MODEL_RESPONSE_BYTES
from locua.amplifier_contracts import validate_tool_arguments, ArgumentContractError


class ReconciliationTests(unittest.TestCase):
    setUp=fixtures.ToolTests.setUp
    call=fixtures.ToolTests.call
    items=fixtures.ToolTests.items
    item=fixtures.ToolTests.item
    review_text=fixtures.ToolTests.review_text
    act=fixtures.ToolTests.act

    def uncertain_edit(self,*,preserve=False,extra_goal=False,large=False):
        if extra_goal:
            goals=[{'id':name,'kind':'text','target':name,'control_id':self.item(name)['control']['id'],
                    'value':value,'evidence_plane':'editor_buffer'} for name,value in [('Entry','exact'),('Competitor','second')]]
            scope=self.call('review',snapshot_id=self.sid,summary='Both fields',goals=goals,
                effects=[{'kind':'goal','goal_id':g['id']} for g in goals],covers_entire_request=True)['scope_id']
        else:scope=self.review_text(complete=True,preserve=preserve)
        execute=self.desktop.execute
        def uncertain(action,observation):
            result=execute(action,observation)
            result.update(status='uncertain',code='post_readback_mismatch',
                verification={'readback':{'matched':False,'reason':'bound_target_absent'}})
            if large:result['observation']['text']='private full capture '*100000
            return result
        with patch.object(self.desktop,'execute',side_effect=uncertain):result=self.act(scope,'Entry','exact')
        return scope,result

    def test_full_capture_private_compact_precise_no_retry_receipt(self):
        scope,result=self.uncertain_edit(large=True)
        self.assertEqual(result['status'],'uncertain');self.assertEqual(result['reason'],'bound_target_absent')
        self.assertTrue(result['action_started']);self.assertTrue(result['no_retry'])
        self.assertLess(len(json.dumps(result).encode()),MODEL_RESPONSE_BYTES)
        self.assertEqual(result['reconciliation']['arguments'],{'scope_id':scope,'reconcile':True})
        event=json.loads(Path(result['full_response_ref'].split('#')[0]).read_text())
        self.assertGreater(len(json.dumps(event['result']['raw_action_result'])),1_000_000)
        self.assertNotIn('private full capture',json.dumps(result))
        self.assertEqual(self.tools._scopes[scope]['status'],'blocked_uncertain')

    def test_huge_error_and_control_content_do_not_hide_no_retry(self):
        scope=self.review_text();blob='\u263a'*100000
        with patch.object(self.desktop,'execute',return_value={'status':'uncertain','action_started':True,
                'reason':blob,'control':{'name':blob,'value':blob}}):result=self.act(scope,'Entry','exact')
        self.assertLess(len(json.dumps(result).encode()),MODEL_RESPONSE_BYTES)
        self.assertEqual(result['reason_detail']['utf8_bytes'],len(blob.encode()))
        self.assertTrue(result['no_retry']);self.assertIn('retained privately',result['reason'])

    def test_reconcile_exact_original_scope_read_only_and_final_fresh(self):
        scope,result=self.uncertain_edit(preserve=True)
        original=deepcopy(self.tools.evidence['events'][-1]);bindings=deepcopy(self.tools._scopes[scope]['bindings'])
        count=len(self.desktop.executions)
        result=self.call('verify',scope_id=scope,reconcile=True)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['scope_status'],'reconciled_verified')
        self.assertFalse(result['reconciliation']['delivery_proven'])
        self.assertFalse(result['reconciliation']['other_side_effects_proven_absent'])
        self.assertFalse(result['input_authority_restored'])
        self.assertEqual(self.tools._scopes[scope]['bindings'],bindings)
        self.assertIn(original,self.tools.evidence['events'])
        status=self.call('status',operation='scopes')['items'][0]
        self.assertEqual(status['status'],'reconciled_verified');self.assertFalse(status['input_authority_available'])
        sid=result['snapshot_id'];denied=self.act(scope,'Entry','exact',sid=sid)
        self.assertEqual(denied['status'],'refused');self.assertEqual(len(self.desktop.executions),count)
        final=self.tools.finalize();self.assertEqual(final['status'],'verified_reviewed_scope',final)
        self.assertFalse(final['saved_output_proven'])
        proof=final['verification']['scopes'][scope]['goals'][0]['evidence']
        self.assertNotEqual(proof['snapshot_id'],sid)

    def test_reconciled_scope_can_be_verified_again_but_changed_value_fails(self):
        scope,_=self.uncertain_edit();self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'verified')
        self.assertEqual(self.call('verify',scope_id=scope)['status'],'verified')
        self.desktop.value='changed externally'
        self.assertEqual(self.tools.finalize()['status'],'blocked')
        self.assertEqual(self.tools._scopes[scope]['status'],'reconciled_verified')

    def test_missing_original_identity_cannot_be_rebound_by_matching_value(self):
        scope,_=self.uncertain_edit();observe=self.desktop.observe
        def renamed(target):
            r=observe(target);c=r['observation']['controls'][1]
            c['name']='Another';c['semantics']['identifier']='another';return r
        with patch.object(self.desktop,'observe',side_effect=renamed):r=self.call('verify',scope_id=scope,reconcile=True)
        self.assertEqual(r['status'],'unverified');self.assertEqual(self.tools._scopes[scope]['status'],'blocked_uncertain')
        self.assertEqual(r['scopes'][scope]['goals'][0]['reason'],'bound_target_absent')

    def test_duplicate_identity_refuses_despite_equal_values(self):
        scope,_=self.uncertain_edit();observe=self.desktop.observe
        def duplicate(target):
            r=observe(target);o=r['observation'];c=deepcopy(o['controls'][1]);c['id']=o['snapshot_id']+':twin';o['controls'].append(c);return r
        with patch.object(self.desktop,'observe',side_effect=duplicate):r=self.call('verify',scope_id=scope,reconcile=True)
        self.assertEqual(r['status'],'unverified');self.assertFalse(r['scopes'][scope]['all_reviewed_predicates_matched'])

    def test_foreign_capture_never_resolves_scope(self):
        scope,_=self.uncertain_edit();observe=self.desktop.observe
        def foreign(target):
            r=observe(target);r['observation']['target']['window_id']=999;return r
        with patch.object(self.desktop,'observe',side_effect=foreign):r=self.call('verify',scope_id=scope,reconcile=True)
        self.assertEqual(r['status'],'refused');self.assertEqual(self.tools._scopes[scope]['status'],'blocked_uncertain')

    def test_reused_or_pre_operation_capture_is_not_independent(self):
        scope,result=self.uncertain_edit();old=deepcopy(self.tools._observations[result['snapshot_id']])
        with patch.object(self.desktop,'observe',return_value={'status':'observed','observation':old}):
            self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')
        old['snapshot_id']='new-but-old-time'
        with patch.object(self.desktop,'observe',return_value={'status':'observed','observation':old}):
            self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')

    def test_inexact_fresh_value_cannot_resolve(self):
        scope,_=self.uncertain_edit();observe=self.desktop.observe
        def inexact(target):
            r=observe(target);r['observation']['controls'][1]['value_evidence']['exact_value_proven']=False;return r
        with patch.object(self.desktop,'observe',side_effect=inexact):r=self.call('verify',scope_id=scope,reconcile=True)
        self.assertEqual(r['status'],'unverified')

    def test_recorded_preservation_failure_cannot_be_laundered_by_later_restore(self):
        scope=self.review_text(complete=True,preserve=True);execute=self.desktop.execute
        def changed(action,o):
            self.desktop.checked=False;r=execute(action,o);r['status']='uncertain';return r
        with patch.object(self.desktop,'execute',side_effect=changed):result=self.act(scope,'Entry','exact')
        self.assertFalse(result['reconciliation']['available']);self.desktop.checked=True
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')
        self.assertEqual(self.tools.finalize()['status'],'blocked')

    def test_missing_post_preservation_evidence_blocks_reconciliation(self):
        scope=self.review_text(preserve=True)
        with patch.object(self.desktop,'execute',return_value={'status':'uncertain','action_started':True}):r=self.act(scope,'Entry','exact')
        self.assertFalse(r['reconciliation']['available'])
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')

    def test_fresh_preservation_failure_latches_even_after_external_restore(self):
        scope,_=self.uncertain_edit(preserve=True);self.desktop.checked=False
        r=self.call('verify',scope_id=scope,reconcile=True);self.assertEqual(r['status'],'unverified')
        self.desktop.checked=True
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')

    def test_changed_review_or_attempted_permit_refused(self):
        scope,_=self.uncertain_edit();s=self.tools._scopes[scope]
        s['goals'][0]['value']='other'
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')
        s['goals'][0]['value']='exact';s['uncertain_action']['permit']['goal_id']='invented'
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')

    def test_no_partial_goal_or_new_review_completion(self):
        scope,_=self.uncertain_edit(extra_goal=True)
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'unverified')
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        replacement=self.review_text(complete=True)
        self.assertEqual(self.act(replacement,'Entry','exact')['status'],'refused')
        self.assertEqual(self.tools.finalize()['status'],'blocked')
        self.assertEqual(len(self.desktop.executions),1)

    def test_reconciled_target_cannot_regain_input_through_new_review(self):
        scope,_=self.uncertain_edit();self.call('verify',scope_id=scope,reconcile=True)
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        replacement=self.review_text(value='another',complete=True)
        statuses=self.call('status',operation='scopes')['items']
        self.assertFalse(next(s for s in statuses if s['scope_id']==replacement)['input_authority_available'])
        r=self.act(replacement,'Entry','another')
        self.assertEqual(r['status'],'refused');self.assertIn('never restores input authority',r['reason'])
        self.assertEqual(len(self.desktop.executions),1)

    def test_state_goal_reconciles_current_state_without_retoggling(self):
        cid=self.item('Keep')['control']['id']
        scope=self.call('review',snapshot_id=self.sid,summary='Set state',
            goals=[{'id':'state','kind':'state','target':'Keep','control_id':cid,'value':False,'evidence_plane':'display'}],
            effects=[{'kind':'goal','goal_id':'state'}],covers_entire_request=True)['scope_id']
        execute=self.desktop.execute
        def uncertain(action,o):
            r=execute(action,o);r['status']='uncertain';return r
        with patch.object(self.desktop,'execute',side_effect=uncertain):r=self.act(scope,'Keep')
        self.assertTrue(r['reconciliation']['available']);self.assertFalse(self.desktop.checked)
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'verified')
        self.assertEqual(len(self.desktop.executions),1);self.assertFalse(self.desktop.checked)

    def test_hosted_projection_retains_safety_and_hides_private_capture_path(self):
        from locua.task_observation_scope import TaskObservationScope
        scope,result=self.uncertain_edit(large=True)
        disclosure=TaskObservationScope('Use Tool surface.',{'name':'Tool surface','app_id':self.app})
        disclosure.register_windows([{'window_id':self.window,'target':{'pid':41,'window_id':52},
            'title':'Tool surface','identity_proven':True}],self.app)
        for o in self.tools._observations.values():disclosure.register_observation(o)
        projected=disclosure.project('locua_act',result)
        self.assertNotIn('full_response_ref',projected)
        self.assertTrue(projected['no_retry']);self.assertEqual(projected['scope_id'],scope)
        self.assertEqual(projected['reason'],'bound_target_absent')
        self.assertEqual(projected['reconciliation']['arguments'],{'scope_id':scope,'reconcile':True})
        proof=self.call('verify',scope_id=scope,reconcile=True)
        for o in self.tools._observations.values():disclosure.register_observation(o)
        projected=disclosure.project('locua_verify',proof)
        self.assertEqual(projected['status'],'verified');self.assertFalse(projected['input_authority_restored'])
        self.assertFalse(projected['reconciliation']['delivery_proven'])

    def test_arithmetic_and_navigation_uncertainty_unsupported(self):
        for kind in ('calculation','navigation'):
            with self.subTest(kind=kind):
                self.sid=self.call('observe',window_id=self.window)['snapshot_id']
                if kind=='calculation':
                    goals=[{'id':'calc','kind':'calculation','target':'Result','control_id':self.item('Result')['control']['id'],
                            'expression':'2+3','evidence_plane':'display'}];effects=[{'kind':'goal','goal_id':'calc'}];name='All Clear'
                else:goals=[];effects=[{'kind':'press','control_id':self.item('Hide panel')['control']['id'],'purpose':'Navigation'}];name='Hide panel'
                scope=self.call('review',snapshot_id=self.sid,summary='Scope',goals=goals,effects=effects)['scope_id']
                # Isolate each case from the earlier target's permanent input lock.
                for other in list(self.tools._scopes):
                    if other!=scope:self.tools._scopes.pop(other)
                self.desktop.uncertain=True;r=self.act(scope,name)
                self.assertFalse(r['reconciliation']['available'])
                self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'refused')

    def test_explicit_no_effect_refusal_is_not_uncertainty(self):
        scope=self.review_text()
        with patch.object(self.desktop,'execute',return_value={'status':'refused','action_started':False,'reason':'not sent'}):r=self.act(scope,'Entry','exact')
        self.assertEqual(r['status'],'refused');self.assertEqual(self.tools._scopes[scope]['status'],'approved')
        self.assertNotIn('uncertain_action',self.tools._scopes[scope])

    def test_cancellation_prevents_even_reconciliation_capture(self):
        scope,_=self.uncertain_edit();count=len(self.desktop.calls)
        self.tools._cancellation={'status':'canceled','reason':'user_declined_review','authority_revoked':True}
        self.assertEqual(self.call('verify',scope_id=scope,reconcile=True)['status'],'canceled')
        self.assertEqual(len(self.desktop.calls),count)

    def test_schema_requires_only_original_scope_and_reconcile_true(self):
        validate_tool_arguments('locua_verify',{'scope_id':'scope:original','reconcile':True})
        for extra in ({'goal_id':'x'},{'all':True},{'snapshot_id':'new','control_id':'chosen'},{'reconcile':False}):
            with self.subTest(extra=extra),self.assertRaises(ArgumentContractError):
                validate_tool_arguments('locua_verify',{'scope_id':'scope:original','reconcile':True,**extra})


if __name__=='__main__':unittest.main()
