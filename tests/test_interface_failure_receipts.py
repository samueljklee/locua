"""CPU fault injection for compact-interface continuity and honest receipts."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_tools import DesktopToolset
from tests.test_amplifier_tools import Desktop


class InterfaceFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desktop = Desktop()
        self.owner = DesktopToolset({}, Path(self.tmp.name)/'tools',
            'Set Entry to exact. Keep Competitor unchanged; do not save.',
            lambda *_: 'run', desktop=self.desktop, tool_profile='continuity-v1')
        self.addCleanup(self.owner.close)
        self.ui = self.owner.model_interface
        app = self.ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
        self.window = self.ui.call('locua_windows', {'app_id': app})['windows'][0]['window_id']
        self.view = self.ui.call('locua_observe', {'window_id': self.window})['view']

    def inspect(self, **args):
        return self.ui.call('locua_inspect', {'view': self.view, **args})

    def approve(self, value='exact'):
        self.target = self.inspect(query='Entry')['items'][0]['target']
        protected = self.inspect(query='Competitor')['items'][0]['target']
        reviewed = self.ui.call('locua_review', {'summary': 'Set exact buffer without saving',
            'goals': [{'kind': 'text', 'target': self.target, 'value': value}],
            'preserve': [{'target': protected, 'property': 'value'}], 'covers_request': True})
        self.assertEqual(reviewed['status'], 'approved', reviewed)
        return reviewed

    def act(self, value='exact'):
        return self.ui.call('locua_act', {'target': self.target, 'operation': 'set_text', 'value': value})

    def assert_stopped_after_one_input(self, result):
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertTrue(result['execution_stopped'])
        self.assertTrue(result['authority_revoked'])
        self.assertTrue(result['no_retry'])
        self.assertFalse(result['task_complete'])
        self.assertIsNotNone(self.owner._cancellation)
        self.assertEqual(len(self.desktop.executions), 1)
        again = self.act()
        self.assertTrue(again['execution_stopped'])
        self.assertEqual(len(self.desktop.executions), 1)

    def test_equivalent_inspection_feedback_does_not_reset_nonprogress(self):
        first = self.inspect(query='Entry')
        second = self.inspect(query='Entry')
        third = self.inspect(query='Entry')
        self.assertEqual(first['exploration_feedback']['equivalent_inspection_count'], 1)
        self.assertEqual(second['nonprogress']['equivalent_attempts'], 2)
        self.assertTrue(third['execution_stopped'])
        self.assertEqual(third['nonprogress']['equivalent_attempts'], 3)
        self.assertEqual(self.owner._cancellation['reason'], 'nonprogress_limit')
        self.assertEqual(self.desktop.executions, [])

    def test_fresh_snapshot_aliases_do_not_make_unchanged_recaptures_progress(self):
        second = self.ui.call('locua_observe', {'window_id': self.window})
        third = self.ui.call('locua_observe', {'window_id': self.window})
        self.assertNotEqual(self.view, second['view'])
        self.assertNotEqual(second['view'], third['view'])
        self.assertEqual(second['nonprogress']['equivalent_attempts'], 2)
        self.assertTrue(third['execution_stopped'])
        self.assertEqual(self.desktop.executions, [])

    def test_different_controls_with_identical_visible_text_remain_distinct(self):
        sid = self.ui.resolve(self.view, 'v')
        observation = self.owner._observations[sid]
        # Use real retained references but deliberately identical projections.
        # A name-only repeat key would incorrectly combine these competitors.
        for c in observation['controls'][1:4]:
            row = {'status': 'ok', 'view': self.view, 'items': [
                {'target': self.ui.ref('c', [sid, c['id']]), 'role': 'AXButton', 'name': 'Same'}]}
            self.ui._nonprogress('locua_inspect', {'view': self.view}, row)
            self.assertNotIn('nonprogress', row)
        self.assertIsNone(self.owner._cancellation)

    def test_actual_semantic_change_resets_repetition(self):
        self.inspect(query='Entry')
        self.assertEqual(self.inspect(query='Entry')['nonprogress']['equivalent_attempts'], 2)
        self.desktop.value = 'Changed outside the agent'
        self.view = self.ui.call('locua_observe', {'window_id': self.window})['view']
        self.assertNotIn('nonprogress', self.inspect(query='Entry'))
        self.assertIsNone(self.owner._cancellation)

    def test_plain_status_repeats_stop_but_distinct_review_pages_do_not(self):
        reviewed = self.approve(value='Long literal ' * 2000)
        args = {'review': reviewed['review']}
        pages = []
        while args:
            result = self.ui.call('locua_status', args)
            self.assertEqual(result['status'], 'ok', result)
            self.assertNotIn('execution_stopped', result)
            pages.append(result)
            args = result.get('coverage', {}).get('continue_with')
        self.assertGreater(len(pages), 3)
        self.assertIsNone(self.owner._cancellation)
        for _ in range(3):
            result = self.ui.call('locua_status', {})
        self.assertTrue(result['execution_stopped'])

    def test_projection_failure_preserves_successful_action_receipt_and_stops(self):
        self.approve()
        with patch.object(self.ui, '_project', side_effect=ValueError('projection failed')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertEqual(result['operation_status'], 'verified')
        self.assertEqual(self.desktop.value, 'exact')
        failure = self.owner.evidence['interface_failures'][-1]
        self.assertIs(failure['owner_receipt']['action_started'], True)
        saved = json.loads((self.owner.out/'evidence.json').read_text())
        self.assertTrue(saved['cancellation']['execution_stopped'])

    def test_owner_recording_failure_recovers_already_retained_receipt(self):
        self.approve()
        original = self.owner._record
        def record_then_fail(*args):
            original(*args)
            raise OSError('owner record transport failed after persistence')
        with patch.object(self.owner, '_record', side_effect=record_then_fail):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertEqual(result['operation_status'], 'verified')

    def test_unknown_owner_delivery_never_becomes_action_started_false(self):
        self.approve()
        with patch.object(self.owner, '_record', side_effect=OSError('receipt unavailable')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIsNone(result['action_started'])
        self.assertFalse(result['owner_receipt_retained'])
        self.assertEqual(self.desktop.value, 'exact')

    def test_interface_artifact_failure_preserves_receipt_and_latches_stop(self):
        self.approve()
        with patch('locua.model_interface.private_json', side_effect=OSError('disk full')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertFalse(result['interface_event_persisted'])
        self.assertEqual(result['operation_status'], 'verified')

    def test_progress_bookkeeping_failure_after_input_preserves_receipt(self):
        self.approve()
        with patch.object(self.ui, '_nonprogress', side_effect=TypeError('progress serialization failed')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertEqual(result['operation_status'], 'verified')

    def test_failed_evidence_persistence_is_explicit_and_still_stops_input(self):
        self.approve()
        with patch.object(self.owner, '_write_evidence', side_effect=OSError('evidence storage full')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertFalse(result['failure_evidence_persisted'])
        self.assertEqual(result['persistence_error'], 'evidence storage full')

    def test_uncertain_owner_receipt_stays_uncertain_after_projection_failure(self):
        self.approve()
        self.desktop.uncertain = True
        with patch.object(self.ui, '_project', side_effect=ValueError('projection failed')):
            result = self.act()
        self.assert_stopped_after_one_input(result)
        self.assertIs(result['action_started'], True)
        self.assertEqual(result['operation_status'], 'uncertain')
        self.assertEqual(self.desktop.value, 'initial')

    def test_known_read_only_failure_does_not_falsely_claim_input(self):
        self.desktop.unavailable = True
        with patch.object(self.ui, '_project', side_effect=ValueError('projection failed')):
            result = self.ui.call('locua_observe', {'window_id': self.window})
        self.assertEqual(result['status'], 'blocked')
        self.assertIs(result['action_started'], False)
        self.assertTrue(result['execution_stopped'])
        self.assertEqual(self.desktop.executions, [])

    def test_real_amplifier_loop_stops_before_next_provider_call(self):
        from amplifier_core.message_models import ChatResponse, ToolCall, ToolCallBlock
        from locua.amplifier_provider import LocalAmplifierProvider, native_request
        from locua.amplifier_session import execute_session
        self.approve()
        chosen = {'target': self.target, 'operation': 'set_text', 'value': 'exact'}
        calls = []
        class Provider(LocalAmplifierProvider):
            request_budget = None  # CPU-only policy; no inference worker.
            async def complete(self, request, **kwargs):
                calls.append(native_request(request, structured_tool_results=self._structured_tool_results))
                if len(calls) != 1:
                    raise AssertionError('Provider must not run after trusted interface failure')
                return ChatResponse(content=[ToolCallBlock(id='edit-1', name='locua_act', input=chosen)],
                    tool_calls=[ToolCall(id='edit-1', name='locua_act', arguments=chosen)])
        async def run():
            provider = Provider()
            try:
                return await execute_session(self.owner.request, provider, self.owner.tools(),
                    out=Path(self.tmp.name)/'session', owner_cancellation=lambda: deepcopy(self.owner._cancellation))
            finally:
                await provider.close()
        with patch.object(self.ui, '_project', side_effect=ValueError('post-input projection failed')):
            report = asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.desktop.executions), 1)
        self.assertEqual(self.desktop.value, 'exact')
        self.assertEqual(report['cancellation']['code'], 'interface_result_unavailable')
        self.assertIs(report['cancellation']['action_started'], True)
        events = [row['event'] for row in report['events']]
        self.assertEqual(events.count('provider:request'), 1)
        self.assertEqual(events.count('tool:post'), 1)
        self.assertIn('cancel:completed', events)
        self.assertEqual(report['session_cleanup'], 'closed')


if __name__ == '__main__': unittest.main()
