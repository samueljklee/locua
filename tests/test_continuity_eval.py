"""CPU-only multi-turn integration and adversarial oracle checks."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import continuity_eval as evaluation
from locua.amplifier_provider import LocalAmplifierProvider, native_request
from locua.amplifier_tools import DesktopToolset
from locua.progressive_ui import exposed_controls


class ScriptedProvider(LocalAmplifierProvider):
    """Predeclared CPU test policy; never starts a local model worker."""
    request_budget = None
    def __init__(self, spec, *, invalid_review=False, repeat_bad=False, compaction=False, wrong_review=False, timeout_after_verification=False):
        super().__init__(model='comparator')
        self.spec = spec; self.seen = []; self.outputs = []; self.phase = 0
        self.invalid_review = invalid_review; self.repeat_bad = repeat_bad
        self.timeout_after_verification = timeout_after_verification
        self.wrong_review = wrong_review
        self.compaction = compaction; self.reads = 0; self.saved = {}

    async def complete(self, request, **kwargs):
        from amplifier_core.message_models import ChatResponse, TextBlock, ToolCall, ToolCallBlock, Usage
        native = native_request(request, model='comparator', structured_tool_results=self._structured_tool_results)
        self.seen.append(native)
        latest = next((json.loads(m['content']) for m in reversed(native['messages']) if m['role'] == 'tool'), None)
        output = None
        if latest:
            output = latest['output']
            if isinstance(output, str): output = json.loads(output)
            if 'output' in output: output = output['output']
            self.outputs.append(deepcopy(output))
        if self.timeout_after_verification and self.phase == 8:
            await asyncio.sleep(60)
        call = self.next_call(output)
        n = len(self.seen)
        if call is None:
            response = ChatResponse(content=[TextBlock(text='Synthetic policy ended; evaluator checks outcome separately.')])
        else:
            name, args = call; cid = 'cpu-' + str(n)
            response = ChatResponse(content=[ToolCallBlock(id=cid, name=name, input=args)],
                                    tool_calls=[ToolCall(id=cid, name=name, arguments=args)])
        tokens = len(json.dumps(native['messages'])) // 4
        response.usage = Usage(input_tokens=tokens, output_tokens=20, total_tokens=tokens + 20)
        self.records.append({'status': 'completed', 'request': request.model_dump(), 'response': response.model_dump(),
                             'generation': {'usage': {'input_tokens': tokens, 'output_tokens': 20}}})
        return response

    def next_call(self, output):
        if self.repeat_bad:
            return 'locua_inspect', {'snapshot_id': 'invented', 'operation': 'list'}
        if self.phase == 0:
            self.phase = 1; return 'locua_apps', {'query': 'Workspace Lab'}
        if self.phase == 1:
            self.phase = 2; return 'locua_windows', {'app_id': output['items'][0]['app_id']}
        if self.phase == 2:
            self.phase = 3; return 'locua_observe', {'window_id': output['windows'][0]['window_id']}
        if self.phase == 3:
            self.saved['sid'] = output['snapshot_id']; self.phase = 4
            return 'locua_inspect', {'snapshot_id': self.saved['sid'], 'operation': 'list'}
        if self.phase == 4:
            rows = exposed_controls(output)
            self.saved['target'] = next(r for r in rows if r['name'] == (self.spec['label'] if self.spec['family'] == 'editor' else self.spec['choice']) and r['role'] in ('AXTextField', 'AXButton'))
            self.saved['competitor'] = next(r for r in rows if r['id'] != self.saved['target']['id'] and r['role'] == self.saved['target']['role'])
            self.phase = 5
            return 'locua_review', self.review(invalid=self.invalid_review)
        if self.phase == 5 and self.invalid_review:
            self.invalid_review = False
            if output['status'] != 'refused': raise AssertionError('Invalid review must be refused before approval')
            return 'locua_review', self.review()
        if self.phase == 5:
            if output['status'] != 'approved': return None
            self.saved['scope'] = output['scope_id']; self.phase = 6
        if self.phase == 6:
            if self.compaction and self.reads < 12:
                self.reads += 1
                return 'locua_inspect', {'snapshot_id': self.saved['sid'], 'operation': 'list', 'query': str(self.reads)}
            self.phase = 7
            args = {'scope_id': self.saved['scope'], 'snapshot_id': self.saved['sid'], 'action_id': self.saved['target']['actions'][0]['id']}
            if self.spec['family'] == 'editor': args['value'] = self.spec['choice']
            return 'locua_act', args
        if self.phase == 7:
            self.phase = 8
            return 'locua_verify', {'scope_id': self.saved['scope'], **({'reconcile': True} if output['status'] == 'uncertain' else {})}
        return None

    def review(self, invalid=False):
        target = self.saved['target']; competitor = self.saved['competitor']
        text = self.spec['family'] == 'editor'
        goal = {'id': 'desired', 'kind': 'text' if text else 'state', 'target': self.spec['label'],
                'control_id': 'invented' if invalid else target['id'], 'value': self.spec['choice'] if text else True,
                'evidence_plane': 'editor_buffer' if text else 'display'}
        if self.wrong_review: goal['value'] = 'Different than requested' if text else False
        if not text: goal['property'] = 'selected'
        return {'snapshot_id': self.saved['sid'], 'summary': 'Change the requested primary control; preserve the reference; do not save.',
                'goals': [goal], 'effects': [{'kind': 'goal', 'goal_id': 'desired'}], 'covers_entire_request': True,
                'preserves': [{'control_id': competitor['id'], 'property': 'value' if text else 'selected',
                               'value': self.spec['initial']['competitor']}]}


class CompactScriptedProvider(ScriptedProvider):
    """Same CPU semantic choices, expressed through the compact public schemas."""
    def next_call(self, output):
        if self.repeat_bad:
            return 'locua_inspect', {'view': 'invented'}
        if self.phase == 0:
            self.phase = 1; return 'locua_apps', {'query': 'Workspace Lab'}
        if self.phase == 1:
            self.phase = 2; return 'locua_windows', {'app_id': output['items'][0]['app_id']}
        if self.phase == 2:
            self.phase = 3; return 'locua_observe', {'window_id': output['windows'][0]['window_id']}
        if self.phase == 3:
            self.saved['view'] = output['view']; self.phase = 4
            return 'locua_inspect', {'view': self.saved['view']}
        if self.phase == 4:
            rows = output['items']
            self.saved['target'] = next(r for r in rows if r.get('identifier') == 'primary')
            self.saved['competitor'] = next(r for r in rows if r.get('identifier') == 'competitor')
            self.phase = 5
            return 'locua_review', self.review(invalid=self.invalid_review)
        if self.phase == 5 and self.invalid_review:
            self.invalid_review = False
            if output['status'] != 'refused': raise AssertionError('Invalid target must be refused')
            return 'locua_review', self.review()
        if self.phase == 5:
            if output['status'] != 'approved': return None
            self.saved['review'] = output['review']; self.phase = 6
        if self.phase == 6:
            if self.compaction and self.reads < 2:
                self.reads += 1
                return 'locua_inspect', {'view': self.saved['view'], 'query': str(self.reads)}
            self.phase = 7
            args = {'target': self.saved['target']['target'], 'operation': 'set_text' if self.spec['family'] == 'editor' else 'press'}
            if self.spec['family'] == 'editor': args['value'] = self.spec['choice']
            return 'locua_act', args
        if self.phase == 7:
            self.phase = 8
            return 'locua_verify', {'review': self.saved['review'], 'reconcile': True} if output['status'] == 'uncertain' else {}
        return None

    def review(self, invalid=False):
        text = self.spec['family'] == 'editor'
        goal = {'kind': 'text' if text else 'state', 'target': 'invented' if invalid else self.saved['target']['target'],
                'value': self.spec['choice'] if text else True}
        if self.wrong_review: goal['value'] = 'Different than requested' if text else False
        if not text: goal['property'] = 'selected'
        return {'summary': 'Change the requested primary control; preserve the reference; do not save.',
                'goals': [goal], 'covers_request': True,
                'preserve': [{'target': self.saved['competitor']['target'], 'property': 'value' if text else 'selected'}]}


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.spec, self.oracle = evaluation.fixtures()[0]
        self.desktop = evaluation.SimulatedDesktop(self.spec)
        self.final = {'status': 'verified_reviewed_scope', 'all_reviewed_goals_verified': True}
        self.events = [{'tool': 'locua_verify', 'result': {'status': 'verified'}}]

    def correct(self):
        before = deepcopy(self.desktop.state)
        self.desktop.state['primary'] = self.oracle['primary']
        self.desktop.executions.append({'key': 'primary', 'before': before, 'after': deepcopy(self.desktop.state)})

    def test_correct_state_without_input_does_not_pass(self):
        self.desktop.state['primary'] = self.oracle['primary']
        self.assertFalse(evaluation.independent_audit(self.desktop, self.oracle, self.final, self.events)['passed'])

    def test_wrong_value_fails_even_with_claimed_verification(self):
        self.correct(); self.desktop.state['primary'] += '\n'
        self.assertFalse(evaluation.independent_audit(self.desktop, self.oracle, self.final, self.events)['passed'])

    def test_temporary_unintended_change_cannot_be_hidden_by_restoration(self):
        self.correct()
        before = deepcopy(self.desktop.state); altered = {**before, 'competitor': 'changed'}
        self.desktop.executions.extend([{'key': 'competitor', 'before': before, 'after': altered},
                                        {'key': 'competitor', 'before': altered, 'after': before}])
        audit = evaluation.independent_audit(self.desktop, self.oracle, self.final, self.events)
        self.assertFalse(audit['passed']); self.assertEqual(audit['unintended_changes'], 2)

    def test_boolean_expected_value_does_not_accept_integer(self):
        spec, oracle = evaluation.fixtures()[1]; desktop = evaluation.SimulatedDesktop(spec)
        before = deepcopy(desktop.state); desktop.state['primary'] = 1
        desktop.executions.append({'key': 'primary', 'before': before, 'after': deepcopy(desktop.state)})
        self.assertFalse(evaluation.independent_audit(desktop, oracle, self.final, self.events)['passed'])

    def test_finalization_cannot_replace_model_verification(self):
        self.correct()
        self.assertFalse(evaluation.independent_audit(self.desktop, self.oracle, self.final, [])['passed'])
        self.assertTrue(evaluation.independent_audit(self.desktop, self.oracle, self.final, self.events)['passed'])

    def test_verified_outcome_does_not_overrule_timeout_or_abnormal_cleanup(self):
        self.correct()
        audit = evaluation.independent_audit(self.desktop, self.oracle, self.final, self.events)
        for args in ({'error': 'TimeoutError: ', 'session_returned': False, 'session_cleanup': 'closed'},
                     {'error': None, 'session_returned': True, 'session_cleanup': 'failed'},
                     {'error': None, 'session_returned': True, 'session_cleanup': 'closed', 'stopped': True}):
            result = evaluation.completion_status(audit, **args)
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(result['outcome_achieved'])
            self.assertFalse(result['normal_session_completion'])
        self.assertEqual(evaluation.classify([], audit, 'TimeoutError: '), 'provider_availability_or_time_budget')

    def test_freeze_rejects_changed_fixture_or_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'frozen'; manifest = evaluation.prepare(directory)
            self.assertEqual(manifest['models'], list(evaluation.MODELS))
            compared = [c for c in manifest['candidates'] if c['id'] in ('compact', 'compact-arguments')]
            self.assertEqual(len(compared), 2)
            self.assertTrue(all(c['tool_profile'] == 'continuity-v1' and c['protocol_recovery'] is True for c in compared))
            self.assertTrue(all(c['verified_completion_policy'] == evaluation.VERIFIED_COMPLETION_POLICY for c in compared))
            self.assertTrue(all(c['verified_completion_policy'] == 'off' for c in manifest['candidates'] if c['tool_profile'] == 'baseline'))
            self.assertEqual({c['split'] for c in manifest['cases']}, {'development', 'held-out'})
            with self.assertRaisesRegex(ValueError, 'not frozen'):
                evaluation.load_frozen(directory, model='invented', tool_profile='baseline', instruction_profile='principles-help-v1')
            path = directory/'draft-development.json'; spec = json.loads(path.read_text()); spec['request'] += '!'; path.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, 'fixture changed'):
                evaluation.load_frozen(directory, model='comparator', tool_profile='baseline', instruction_profile='principles-help-v1')


@unittest.skipUnless(importlib.util.find_spec('amplifier_core'), 'Optional Amplifier dependency absent')
class RealLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, index=0, *, compact=False, timeout_s=30, **provider_options):
        spec, oracle = evaluation.fixtures()[index]
        provider = (CompactScriptedProvider if compact else ScriptedProvider)(spec, **provider_options)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                report = await evaluation.run_case(spec, oracle, Path(tmp)/'run', provider, max_calls=30, timeout_s=timeout_s,
                            tool_profile='continuity-v1' if compact else 'baseline',
                            instruction_profile='continuity-v1' if compact else 'principles-help-v1')
                session = json.loads((Path(tmp)/'run/session/session.json').read_text())
                evidence = json.loads((Path(tmp)/'run/tools/evidence.json').read_text())
            finally:
                await provider.close()
        self.assertTrue(provider._closed)
        return report, provider, session, evidence

    async def test_real_loop_consumes_review_and_action_results_before_verification(self):
        report, provider, session, _ = await self.run_fixture()
        self.assertTrue(report['audit']['passed'], report)
        self.assertEqual(report['audit']['dispatches'], 1)
        self.assertEqual(report['session_cleanup'], 'closed')
        self.assertEqual([e['accepted'] for e in report['reviews']], [True])
        self.assertIn('scope:1', json.dumps(provider.seen[-2]))
        self.assertEqual(report['real_desktop_calls'], 0)
        self.assertEqual(len(provider.seen), 8)
        self.assertEqual(report['verified_completion_policy'], 'off')
        self.assertIsNone(report['verified_completion'])

    async def test_invalid_review_returns_feedback_then_can_be_repaired(self):
        report, provider, session, evidence = await self.run_fixture(invalid_review=True)
        self.assertTrue(report['audit']['passed'], report)
        reviews = [e for e in evidence['events'] if e['tool'] == 'locua_review']
        self.assertEqual([e['result']['status'] for e in reviews], ['refused', 'approved'])
        self.assertEqual(report['audit']['dispatches'], 1)

    async def test_timeout_after_verified_edit_is_failed_with_separate_outcome(self):
        report, provider, session, evidence = await self.run_fixture(timeout_after_verification=True, timeout_s=.5)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['audit']['passed'])
        self.assertTrue(report['outcome_achieved'])
        self.assertFalse(report['normal_session_completion'])
        self.assertTrue(report['error'].startswith('TimeoutError'))
        self.assertEqual(report['session_cleanup'], 'closed')
        self.assertEqual(report['failure_category'], 'provider_availability_or_time_budget')
        self.assertEqual(report['audit']['dispatches'], 1)

    async def test_wrong_review_is_declined_and_cancels_without_input(self):
        report, provider, session, evidence = await self.run_fixture(wrong_review=True)
        self.assertFalse(report['audit']['passed'])
        self.assertEqual(report['audit']['dispatches'], 0)
        self.assertEqual([e['accepted'] for e in report['reviews']], [False])
        self.assertEqual(evidence['cancellation']['reason'], 'user_declined_review')
        self.assertEqual(len(provider.seen), 5)

    async def test_competing_state_control_and_preservation(self):
        report, *_ = await self.run_fixture(1)
        self.assertTrue(report['audit']['passed'], report)
        self.assertTrue(report['independent_state']['competitor'])

    async def test_uncertain_delivered_action_requires_read_only_reconciliation(self):
        report, _, _, evidence = await self.run_fixture(2)
        self.assertTrue(report['audit']['passed'], report)
        self.assertEqual(report['audit']['dispatches'], 1)
        action = next(e for e in evidence['events'] if e['tool'] == 'locua_act')
        self.assertEqual(action['result']['status'], 'uncertain')
        self.assertTrue(action['result']['no_retry'])
        self.assertTrue(any(e['tool'] == 'locua_verify' and e['input'].get('reconcile') for e in evidence['events']))

    async def test_changed_geometry_before_input_is_refused_without_mutation(self):
        report, _, _, evidence = await self.run_fixture(3)
        self.assertFalse(report['audit']['passed'])
        self.assertEqual(report['audit']['dispatches'], 0)
        self.assertEqual(report['failure_category'], 'reference_or_grounding')

    async def test_repeated_equivalent_failure_is_bounded(self):
        report, provider, *_ = await self.run_fixture(repeat_bad=True)
        self.assertFalse(report['audit']['passed'])
        self.assertLessEqual(len(provider.seen), 5)
        self.assertEqual(report['failure_category'], 'repeated_nonprogress')

    async def test_compact_interface_completes_editor_and_state_in_real_loop(self):
        for index in (0, 1):
            with self.subTest(index=index):
                report, provider, session, evidence = await self.run_fixture(index, compact=True)
                self.assertTrue(report['audit']['passed'], report)
                self.assertEqual(report['audit']['dispatches'], 1)
                act = next(c for r in provider.records for c in r['response'].get('tool_calls', []) if c['name'] == 'locua_act')
                self.assertNotIn('scope_id', act['arguments'])
                self.assertNotIn('snapshot_id', act['arguments'])
                self.assertNotIn('action_id', act['arguments'])

    async def test_compact_evaluator_matches_cli_verified_stop_without_closing_generation(self):
        report, provider, session, evidence = await self.run_fixture(compact=True, timeout_after_verification=True, timeout_s=.5)
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue(report['outcome_achieved'])
        self.assertTrue(report['normal_session_completion'])
        self.assertIsNone(report['error'])
        self.assertEqual(len(provider.seen), 7)
        self.assertEqual(report['model_calls'], 7)
        self.assertEqual(report['verified_completion_policy'], evaluation.VERIFIED_COMPLETION_POLICY)
        self.assertEqual(report['model_loop_stop_reason'], 'verified_reviewed_completion')
        self.assertTrue(report['verified_completion']['caller_final_refresh_required'])
        self.assertNotIn('cancellation', evidence)
        self.assertNotIn('cancellation', session)

    async def test_compact_invalid_reference_recovers_before_approved_input(self):
        report, provider, session, evidence = await self.run_fixture(compact=True, invalid_review=True)
        self.assertTrue(report['audit']['passed'], report)
        self.assertEqual(report['audit']['dispatches'], 1)
        self.assertEqual(report['reviews'][0]['accepted'], True)

    async def test_compact_uncertainty_reconciles_without_replaying(self):
        report, *_ = await self.run_fixture(2, compact=True)
        self.assertTrue(report['audit']['passed'], report)
        self.assertEqual(report['audit']['dispatches'], 1)

    async def test_compact_progress_survives_real_compaction(self):
        from locua.amplifier_session import CONTEXT_CONFIG
        config = {**CONTEXT_CONFIG, 'max_tokens': 5600, 'compact_threshold': .60, 'target_usage': .55, 'token_meter': 'estimate'}
        with patch('locua.amplifier_session.CONTEXT_CONFIG', config):
            report, provider, session, _ = await self.run_fixture(compact=True, compaction=True)
        self.assertTrue(report['audit']['passed'], report)
        self.assertTrue(any(e['event'] == 'context:compaction' for e in session['events']))
        self.assertIn('Retained task state', json.dumps(provider.seen[-2]))
        self.assertIn('q1', json.dumps(provider.seen[-2]))

    async def test_original_request_and_review_references_survive_real_compaction(self):
        from locua.amplifier_session import CONTEXT_CONFIG
        config = {**CONTEXT_CONFIG, 'max_tokens': 6200, 'compact_threshold': .60, 'target_usage': .55, 'token_meter': 'estimate'}
        with patch('locua.amplifier_session.CONTEXT_CONFIG', config):
            report, provider, session, _ = await self.run_fixture(compaction=True)
        self.assertTrue(report['audit']['passed'], report)
        self.assertTrue(any(e['event'] == 'context:compaction' for e in session['events']))
        self.assertTrue(all(provider.spec['request'] in r['messages'][0]['content'] for r in provider.seen))
        self.assertIn('scope:1', json.dumps(provider.seen[-2]))
        self.assertEqual(report['audit']['dispatches'], 1)


if __name__ == '__main__':
    unittest.main()
