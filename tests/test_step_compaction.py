"""Actual unchanged context-simple compaction of the new focused step frame.

Provider choices and token-count units are explicitly synthetic CPU doubles.
There is no local inference, Cua connection, model accuracy or speed claim.
"""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_provider import LocalAmplifierProvider, native_request, _hash
from locua.amplifier_session import CONTEXT_CONFIG, execute_session, verified_completion_checker
from locua.instruction_policy import instruction_policy
from tests.test_semantic_compaction import SYNTHETIC_PAGES, PAGE_PAYLOAD, compact_json
from tests.test_step_policy_eval import fixture, review_arguments, rows_from
import step_policy_eval as evaluation


async def run_pressure(root, *, fault=None):
    from amplifier_core.message_models import ChatResponse, TextBlock, ToolCall, ToolCallBlock, Usage
    root = Path(root); root.mkdir(parents=True, exist_ok=False)
    spec, oracle = fixture(); seen = []; counters = []; seeded = []; reviews = []
    def ask(message, purpose): reviews.append((message, purpose)); return 'run'
    owner = evaluation.owner_for(spec, root / 'desktop', 'step-v1', ask=ask)
    ui = owner.model_interface; outputs = evaluation.informed_setup(owner)
    review_args, controls = review_arguments(spec, oracle, rows_from(outputs), 'step-v1')
    input_ref = controls['draft']['inputs']['set_text']; target = controls['draft']['target']
    reference_ref = controls['reference']['target']; value = oracle['goals'][0]['value']
    updated = {'plan': ['Inspect requested editor and competing editor', 'Apply reviewed literal', 'Verify original request'],
        'current_step': 'Apply reviewed literal', 'constraints': ['Preserve Reference exactly', 'Do not invoke Save'],
        'unresolved': ['Independent final proof remains required']}

    class Counter:
        def __init__(self, **kwargs): self.closed = False; self.counts = []; counters.append(self); kwargs['on_started'](self)
        def info(self): return {'cpu_test_double': True, 'units': 'JSON characters/4; not tokenizer tokens'}
        def count(self, messages, tools):
            payload = {'messages': messages, 'tools': tools}; n = len(compact_json(payload)) // 4 + 1
            self.counts.append(n)
            return {'input_tokens': n, 'output_tokens': 0, 'generation_calls': 0, 'tokenizer_only': True, 'request_sha256': _hash(payload)}
        def generate(self, *a, **kw): raise AssertionError('No inference allowed')
        def close(self): self.closed = True

    class Provider(LocalAmplifierProvider):
        async def mount(self, coordinator):
            self.context = coordinator.get('context'); await coordinator.mount('providers', self, name=self.name)
        async def complete(self, request, **kwargs):
            native = native_request(request, model='qwen38', structured_tool_results=self._structured_tool_results)
            seen.append(deepcopy(native)); n = len(seen)
            (root / f'input-{n:03d}.json').write_text(json.dumps(native, ensure_ascii=False, indent=2) + '\n')
            if n == 1: name, args = 'locua_review', review_args
            elif n == 2:
                if not any(s['status'] == 'approved' for s in owner._scopes.values()): raise AssertionError('Actual review must precede pressure')
                for index in range(SYNTHETIC_PAGES):
                    cid = 'synthetic-observation-' + str(index)
                    pair = [
                        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': cid, 'tool': 'locua_inspect', 'arguments': {'reference': 'synthetic-history'}}]},
                        {'role': 'tool', 'tool_call_id': cid, 'name': 'locua_inspect', 'content': compact_json({
                            'synthetic_history': True, 'potentially_stale': True, 'action_authority': False, 'page': index,
                            'irrelevant_payload': PAGE_PAYLOAD})},
                    ]
                    for message in pair: await self.context.add_message(message); seeded.append(deepcopy(message))
                name, args = 'locua_status', {'decision': updated}
            elif n == 3:
                if fault == 'preserve_changed': owner.desktop.state['reference'] = 'External synthetic change'
                if fault == 'done_claim':
                    name, args = 'locua_status', {'decision': {'plan': ['Done'], 'current_step': 'Done', 'constraints': [], 'unresolved': []}}
                else: name, args = 'locua_act', {'input': input_ref, 'value': value}
            elif n == 4: name, args = 'locua_verify', {}
            elif n == 5 and fault:
                return ChatResponse(content=[TextBlock(text='Synthetic policy ended; final proof remains authoritative.')])
            else: raise AssertionError('Unexpected extra provider decision')
            units = len(compact_json(native)) // 4 + 1
            return ChatResponse(content=[ToolCallBlock(id=f'cpu-{n}', name=name, input=args)],
                tool_calls=[ToolCall(id=f'cpu-{n}', name=name, arguments=args)],
                usage=Usage(input_tokens=units, output_tokens=1, total_tokens=units + 1))

    provider = Provider(model='qwen38', service_factory=Counter, out=root / 'provider')
    try:
        with patch('locua.engine_adapter.runtime_environment', return_value=nullcontext()):
            session = await execute_session(spec['request'], provider, ui.tools(), out=root / 'session',
                system=instruction_policy('continuity-v1'), max_iterations=7, execution_facts=ui.state_text,
                execution_facts_mode='tail', focus_dedup=True, owner_cancellation=lambda: deepcopy(owner._cancellation),
                verified_completion=verified_completion_checker(owner))
        before = owner.desktop.sequence; final = owner.finalize()
        report = {'original_request': spec['request'], 'context_config': deepcopy(CONTEXT_CONFIG),
            'actual_context_config': session['config']['session']['context']['config'],
            'reminder_mode': session['config']['session']['orchestrator']['config']['ephemeral_injection_mode'],
            'compactions': [e for e in session['events'] if e['event'] == 'context:compaction'],
            'provider_requests': len(seen), 'synthetic_prior_pairs': len(seeded)//2,
            'model_generations': 0, 'real_desktop_calls': 0, 'dispatches': len(owner.desktop.executions),
            'final': final, 'post_loop_final_captures': owner.desktop.sequence-before,
            'state': deepcopy(owner.desktop.state), 'stop_reason': session.get('stop_reason'),
            'target': target, 'competitor': reference_ref, 'input': input_ref, 'updated_plan': updated,
            'task_and_constraint_literals_source': 'CPU user request; oracle is not inserted into provider input',
            'token_units': 'JSON characters/4; not real tokenizer counts'}
    finally:
        owner.close(); await provider.close()
    report.update(provider_closed=provider._closed, counter_closed=all(c.closed for c in counters),
                  session_cleanup=session['session_cleanup'])
    (root / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report, seen, session


class StepCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, fault=None):
        with tempfile.TemporaryDirectory() as temp: return await run_pressure(Path(temp) / 'case', fault=fault)

    def assert_frame(self, report, seen, session):
        self.assertEqual(report['actual_context_config'], CONTEXT_CONFIG)
        self.assertEqual(report['reminder_mode'], 'tail'); self.assertTrue(report['compactions'])
        self.assertEqual(report['synthetic_prior_pairs'], SYNTHETIC_PAGES)
        for row in seen:
            self.assertIn(report['original_request'], row['messages'][0]['content'])
            self.assertEqual(sum(m['role'] == 'system' for m in row['messages']), 1)
            reminders = [m for m in row['messages'] if 'Current decision frame. Original request is authoritative;' in m['content']]
            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0]['role'], 'user')
        after = seen[2]
        self.assertTrue(after['translation']['context_compaction_notices'])
        message = next(m['content'] for m in after['messages'] if 'Current decision frame. Original request is authoritative;' in m['content'])
        marker = 'Current decision frame. Original request is authoritative; model hypotheses and retained UI are not approval or fresh verification.\n'
        state, _ = json.JSONDecoder().raw_decode(message.split(marker, 1)[1])
        self.assertEqual(state['model_hypotheses']['decision'], report['updated_plan'])
        self.assertFalse(state['model_hypotheses']['authority'])
        self.assertEqual(state['reviews'][0]['status'], 'approved')
        self.assertTrue(state['reviews'][0]['preserves'])
        self.assertEqual(state['reviews'][0]['goals'][0]['persistence_requirement'], 'not_requested')
        self.assertTrue(state['focus']['potentially_stale']); self.assertFalse(state['focus']['action_authority'])
        self.assertTrue({report['target'], report['competitor']} <= {r.get('target') for r in state['focus']['items']})
        self.assertIn(report['input'], compact_json(state)); self.assertTrue(state['explored_routes']['controls'])
        self.assertTrue(state['explored_routes']['potentially_stale'])
        self.assertTrue(report['provider_closed']); self.assertTrue(report['counter_closed'])
        self.assertEqual(report['session_cleanup'], 'closed')

    async def test_real_compaction_retains_current_step_competitors_constraints_and_fresh_proof(self):
        report, seen, session = await self.run_case(); self.assert_frame(report, seen, session)
        self.assertEqual(report['dispatches'], 1)
        self.assertEqual(report['final']['status'], 'verified_reviewed_scope')
        self.assertGreater(report['post_loop_final_captures'], 0)
        self.assertEqual(report['stop_reason'], 'verified_reviewed_completion')

    async def test_stale_compacted_approval_cannot_override_changed_preserve(self):
        report, seen, session = await self.run_case('preserve_changed'); self.assert_frame(report, seen, session)
        self.assertEqual(report['dispatches'], 0)
        self.assertNotEqual(report['final']['status'], 'verified_reviewed_scope')
        self.assertIsNone(report['stop_reason'])

    async def test_updated_done_claim_cannot_complete_unchanged_application(self):
        report, seen, session = await self.run_case('done_claim'); self.assert_frame(report, seen, session)
        self.assertEqual(report['dispatches'], 0)
        self.assertNotEqual(report['final']['status'], 'verified_reviewed_scope')
        self.assertIsNone(report['stop_reason'])


if __name__ == '__main__': unittest.main()
