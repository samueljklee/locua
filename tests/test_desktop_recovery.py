"""Explicit bounded activation recovery against a synthetic driver only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_desktop_tools as fixtures
TARGET=fixtures.TARGET

PROCESS={'pid':41,'started_at_utc':'Thu Sep 17 23:50:00 2026',
         'executable_path':'/Applications/Fixture.app/Contents/MacOS/Fixture'}


class RecoveryTests(unittest.TestCase):
    capture=fixtures.DesktopTests.capture
    action=fixtures.DesktopTests.action
    mutations=fixtures.DesktopTests.mutations
    enable_activation=fixtures.DesktopTests.enable_activation
    def setUp(self):
        fixtures.DesktopTests.setUp(self)
        self.process=patch('locua.desktop_tools._process_identity',return_value=deepcopy(PROCESS))
        self.probe=self.process.start();self.addCleanup(self.process.stop)
        self.enable_activation()
    def activations(self):return [c for c in self.peer.calls if c[0]=='bring_to_front']
    def fail_read(self):
        self.peer.next_error=RuntimeError('AX read currently unavailable')
        result=self.tools.observe(TARGET);self.assertEqual(result['status'],'unavailable');return result
    def test_verified_activation_then_read_failure_allows_one_explicit_reconciled_retry(self):
        self.assertEqual(self.tools.activate(TARGET)['status'],'activated');self.fail_read()
        self.assertEqual(len(self.activations()),1,'Failed read must not autoactivate')
        result=self.tools.activate(TARGET)
        self.assertEqual(result['status'],'activated');self.assertEqual(result['activation_attempt'],2)
        self.assertTrue(result['explicit_recovery']);self.assertTrue(result['process_identity_reconciled'])
        self.assertFalse(result['accessibility_readability_proven']);self.assertFalse(result['task_complete'])
        self.assertEqual(len(self.activations()),2);self.assertFalse(self.mutations())
        self.assertEqual(self.tools.activate(TARGET)['code'],'activation_recovery_not_eligible')
    def test_successful_observation_consumes_failure_eligibility_and_old_action_stays_stale(self):
        self.tools.activate(TARGET);self.fail_read();obs=self.capture();action=self.action(obs)
        self.assertEqual(self.tools.activate(TARGET)['code'],'activation_recovery_not_eligible')
        self.fail_read();self.assertEqual(self.tools.activate(TARGET)['status'],'activated')
        self.assertEqual(self.tools.execute(action,obs)['status'],'refused');self.assertFalse(self.mutations())
    def test_changed_process_incarnation_or_executable_refuses_before_activation(self):
        self.tools.activate(TARGET);self.fail_read()
        for field,value in (('started_at_utc','later'),('executable_path','/Applications/Other.app/Other')):
            self.probe.return_value={**PROCESS,field:value}
            self.assertEqual(self.tools.activate(TARGET)['code'],'activation_process_changed')
        self.assertEqual(len(self.activations()),1)
    def test_process_change_around_window_inventory_refuses_before_activation(self):
        self.tools.activate(TARGET);self.fail_read()
        self.probe.side_effect=[PROCESS,{**PROCESS,'started_at_utc':'later'}]
        self.assertEqual(self.tools.activate(TARGET)['code'],'activation_process_changed_during_reconciliation')
        self.assertEqual(len(self.activations()),1)
    def test_absent_exact_window_refuses_recovery_without_target_substitution(self):
        self.tools.activate(TARGET);self.fail_read();self.peer.window_present=False
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused');self.assertEqual(len(self.activations()),1)
    def test_three_effectful_attempts_are_a_hard_per_target_bound(self):
        for attempt in range(1,4):
            if attempt>1:self.fail_read()
            self.assertEqual(self.tools.activate(TARGET)['activation_attempt'],attempt)
        self.fail_read();result=self.tools.activate(TARGET)
        self.assertEqual(result['code'],'activation_recovery_budget_exhausted');self.assertEqual(len(self.activations()),3)
    def test_uncertain_task_write_cannot_be_unlocked_by_observe_or_activation(self):
        self.tools.activate(TARGET);obs=self.capture();action=self.action(obs)
        self.peer.mutation_error=TimeoutError('write outcome unknown')
        self.assertEqual(self.tools.execute(action,obs)['status'],'uncertain')
        self.peer.mutation_error=None;self.fail_read()
        self.assertEqual(self.tools.activate(TARGET)['code'],'target_effect_uncertain')
        fresh=self.capture();self.assertEqual(self.tools.execute(self.action(fresh),fresh)['status'],'refused')
        self.assertEqual(len(self.activations()),1);self.assertEqual(len(self.mutations()),1)
    def test_uncertain_activation_is_never_a_recovery_permit(self):
        self.peer.activation_reply['observed']['focused_window_id']=999
        self.assertEqual(self.tools.activate(TARGET)['status'],'uncertain')
        self.peer.activation_reply['observed']['focused_window_id']=52;self.fail_read()
        self.assertEqual(self.tools.activate(TARGET)['code'],'target_effect_uncertain');self.assertEqual(len(self.activations()),1)
    def test_missing_original_process_proof_cannot_be_backfilled_into_retry_authority(self):
        self.probe.side_effect=ValueError('process unavailable')
        self.assertEqual(self.tools.activate(TARGET)['status'],'activated');self.fail_read()
        self.probe.side_effect=None;self.probe.return_value=PROCESS
        self.assertEqual(self.tools.activate(TARGET)['code'],'activation_process_identity_unproved')
        self.assertEqual(len(self.activations()),1)
