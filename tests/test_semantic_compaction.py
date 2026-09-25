"""Actual Amplifier compaction with semantic state; no inference or desktop IO.

Pressure is explicitly synthetic history added through the mounted context's
public add_message API. The deterministic provider's count units are serialized
JSON characters /4, not a Qwen tokenizer or performance claim. Production context
configuration, semantic tools, action guards and completion checker are unchanged.
"""
import hashlib
import importlib.util
import json
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_provider import LocalAmplifierProvider, native_request, _hash
from locua.amplifier_session import CONTEXT_CONFIG, execute_session, verified_completion_checker
from locua.amplifier_tools import DesktopToolset
from locua.instruction_policy import instruction_policy
from tests.test_amplifier_tools import Desktop


REQUEST = 'In the disposable Tool surface, replace Entry with "Reviewed λ". Keep Competitor exactly unchanged. Do not save.'
SYNTHETIC_PAGES = 24
PAGE_PAYLOAD = ('Synthetic irrelevant historical observation. No task controls or instructions. ' * 55)


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


async def run_compaction_fixture(root, *, changed_preserve=False, tool_profile='semantic-v1'):
    from amplifier_core.message_models import ChatResponse, TextBlock, ToolCall, ToolCallBlock, Usage
    root = Path(root); root.mkdir(parents=True, exist_ok=False)
    desktop = Desktop(); reviews = []; rendered = []; counters = []; seeded = []
    original_config = deepcopy(CONTEXT_CONFIG)
    def ask(message, purpose):
        reviews.append({'message': message, 'purpose': purpose, 'response': 'run'})
        return 'run'
    owner = DesktopToolset({}, root/'desktop', REQUEST, ask, desktop=desktop, tool_profile=tool_profile)
    ui = owner.model_interface
    app = ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
    window = ui.call('locua_windows', {'app_id': app})['windows'][0]['window_id']
    overview = ui.call('locua_observe', {'window_id': window})
    view = overview['view']
    region = next(r['region'] for r in overview['overview']['items'] if 'region' in r)
    rows = ui.call('locua_inspect', {'view': view, 'region': region})['items']
    entry = next(r['target'] for r in rows if r.get('name') == 'Entry')
    competitor = next(r['target'] for r in rows if r.get('name') == 'Competitor')
    initial_captures = desktop.sequence

    class CountingService:
        def __init__(self, **kwargs):
            self.closed = False; counters.append(self); self.counted = []
            kwargs['on_started'](self)
        def info(self):
            return {'cpu_test_double': True, 'measurement_units': 'serialized JSON characters /4; not real model tokens'}
        def count(self, messages, tools):
            payload = {'messages': messages, 'tools': tools}
            units = len(compact_json(payload))//4+1
            self.counted.append({'units': units, 'payload_sha256': _hash(payload)})
            return {'input_tokens': units, 'output_tokens': 0, 'generation_calls': 0,
                    'tokenizer_only': True, 'request_sha256': _hash(payload)}
        def generate(self, *args, **kwargs):
            raise AssertionError('No model or worker generation is permitted')
        def close(self):
            self.closed = True

    class Provider(LocalAmplifierProvider):
        async def mount(self, coordinator):
            self.context = coordinator.get('context')
            await coordinator.mount('providers', self, name=self.name)
        async def complete(self, request, **kwargs):
            native = native_request(request, model='qwen38', structured_tool_results=self._structured_tool_results)
            rendered.append(deepcopy(native)); n = len(rendered)
            (root/f'provider-input-{n:03d}.json').write_text(json.dumps(native, ensure_ascii=False, indent=2)+'\n')
            if n == 1:
                name = 'locua_review'
                args = {'summary': 'Replace only Entry with Reviewed λ; preserve Competitor exactly; do not save.',
                        'goals': [{'kind': 'text', 'target': entry, 'value': 'Reviewed λ'}],
                        'preserve': [{'target': competitor, 'property': 'value'}], 'covers_request': True}
            elif n == 2:
                # An actual Locua review is already approved. These paired
                # messages are test-owned old observations, not model decisions,
                # live captures, or authoritative target/approval references.
                if not any(s['status'] == 'approved' for s in owner._scopes.values()):
                    raise AssertionError('Synthetic pressure must follow an actual approved review')
                for index in range(SYNTHETIC_PAGES):
                    cid = 'synthetic-prior-observation-'+str(index)
                    messages = [
                        {'role': 'assistant', 'content': '', 'tool_calls': [
                            {'id': cid, 'tool': 'locua_inspect', 'arguments': {'view': 'synthetic-historical-view'}}],
                         'metadata': {'source': 'explicit_cpu_test_history'}},
                        {'role': 'tool', 'tool_call_id': cid, 'name': 'locua_inspect',
                         'content': compact_json({'synthetic_fixture_only': True, 'page': index,
                             'potentially_stale': True, 'action_authority': False, 'irrelevant_payload': PAGE_PAYLOAD}),
                         'metadata': {'source': 'explicit_cpu_test_history'}},
                    ]
                    for message in messages:
                        await self.context.add_message(message)
                        seeded.append(deepcopy(message))
                name = 'locua_status'; args = {}
            elif n == 3:
                if changed_preserve:
                    desktop.other = 'External synthetic change after review'
                name = 'locua_act'; args = {'target': entry, 'operation': 'set_text', 'value': 'Reviewed λ'}
            elif n == 4:
                name = 'locua_verify'; args = {}
            elif n == 5 and changed_preserve:
                return ChatResponse(content=[TextBlock(text='The requested edit is incomplete; preservation changed.')])
            else:
                raise AssertionError('Unexpected additional model decision')
            usage_units = len(compact_json({'messages': native['messages'], 'tools': native['tools']}))//4+1
            return ChatResponse(content=[ToolCallBlock(id=f'actual-call-{n}', name=name, input=args)],
                tool_calls=[ToolCall(id=f'actual-call-{n}', name=name, arguments=args)],
                usage=Usage(input_tokens=usage_units, output_tokens=1, total_tokens=usage_units+1))

    provider = Provider(model='qwen38', service_factory=CountingService, out=root/'provider')
    try:
        with patch('locua.engine_adapter.runtime_environment', return_value=nullcontext()):
            session = await execute_session(REQUEST, provider, ui.tools(), out=root/'session',
                system=instruction_policy('continuity-v1'), max_iterations=8,
                execution_facts=ui.state_text, owner_cancellation=lambda: deepcopy(owner._cancellation),
                verified_completion=verified_completion_checker(owner))
        before_final = desktop.sequence
        final = owner.finalize()
        canonical = deepcopy(owner.evidence)
        state = ui.state()
    finally:
        cleanup = owner.close(); await provider.close()
    actual_compactions = [e for e in session['events'] if e['event'] == 'context:compaction']
    review_event = next(n for n,e in enumerate(session['events'])
        if e['event'] == 'tool:post' and e['data'].get('tool_name') == 'locua_review')
    subsequent_compaction = any(n > review_event and e['event'] == 'context:compaction'
                               for n,e in enumerate(session['events']))
    report = {'configuration_unchanged': CONTEXT_CONFIG == original_config,
        'executed_context_config': session['config']['session']['context']['config'],
        'synthetic_history_pairs': len(seeded)//2, 'synthetic_source': 'public context.add_message after actual review',
        'synthetic_history_bytes': len(compact_json(seeded).encode()), 'compactions': actual_compactions,
        'compaction_after_approved_review': subsequent_compaction,
        'provider_decisions': len(rendered), 'real_model_generations': 0, 'gui_calls': 0,
        'counter_calls': sum(len(c.counted) for c in counters),
        'measurement_limit': 'Synthetic CPU units, not real tokenizer counts or model speed.',
        'original_request': REQUEST, 'expected_fixture_value_is_user_requested_literal': True,
        'oracle_or_expected_sequence_inserted_into_messages': False,
        'seeded_compaction_notices': False, 'action_input_count': len(desktop.executions),
        'post_loop_final_capture_count': desktop.sequence-before_final,
        'fresh_captures_since_discovery': desktop.sequence-initial_captures,
        'final': final, 'actual_buffer': desktop.value, 'actual_preserved_buffer': desktop.other,
        'changed_preserve_fault': changed_preserve, 'session_cleanup': session['session_cleanup'],
        'desktop_cleanup': cleanup, 'provider_closed': provider._closed,
        'counter_closed': all(c.closed for c in counters),
        'input_sha256': [hashlib.sha256(compact_json(n).encode()).hexdigest() for n in rendered],
        'stop_reason': session.get('stop_reason'), 'task_completion_is_synthetic_fixture_only': True}
    (root/'compaction-evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report, rendered, session, canonical, state, seeded


@unittest.skipUnless(importlib.util.find_spec('amplifier_core'), 'Optional Amplifier extra absent')
class SemanticCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, fault=False):
        with tempfile.TemporaryDirectory() as tmp:
            return await run_compaction_fixture(Path(tmp)/'run', changed_preserve=fault)

    def assert_retained_state(self, report, rendered, session, state, seeded):
        self.assertTrue(report['configuration_unchanged'])
        self.assertEqual(report['executed_context_config'], CONTEXT_CONFIG)
        self.assertTrue(report['compactions'], 'Must run actual context-simple compaction')
        self.assertTrue(report['compaction_after_approved_review'])
        self.assertEqual(report['synthetic_history_pairs'], SYNTHETIC_PAGES)
        self.assertFalse(any('context-compaction' in compact_json(m) for m in seeded))
        after = rendered[2]
        text = compact_json(after['messages'])
        self.assertIn(REQUEST, after['messages'][0]['content'])
        self.assertTrue(after['translation']['context_compaction_notices'])
        reminders = [m['content'] for m in after['messages'] if 'Retained task state (untrusted UI text;' in m['content']]
        self.assertTrue(reminders)
        latest = reminders[-1]
        # Amplifier wraps its real injection in a system-reminder envelope;
        # decode the complete JSON body without interpreting that closing tag.
        retained, _ = json.JSONDecoder().raw_decode(
            latest.split('Retained task state (untrusted UI text; not fresh action authority):\n', 1)[1])
        self.assertEqual(retained['projection_version'], 'semantic-v1')
        review = retained['reviews'][0]
        self.assertEqual(review['status'], 'approved')
        self.assertEqual(review['goals'][0]['value'], 'Reviewed λ')
        self.assertEqual(review['preserves'][0]['value'], 'protected')
        self.assertTrue(review['covers_request']); self.assertIn('do not save', review['summary'].lower())
        self.assertTrue(retained['explored']['regions']); self.assertTrue(retained['explored']['controls'])
        self.assertFalse(retained['explored']['action_authority'])
        self.assertTrue(retained['explored']['potentially_stale'])
        self.assertTrue(all(c['potentially_stale'] for c in retained['explored']['controls']))
        self.assertIn('freshness_or_approval_not_granted_by_this_record', latest)
        self.assertIn('do not save', text.lower())
        canonical_text = compact_json(session['transcript'])
        self.assertIn(PAGE_PAYLOAD, canonical_text)
        self.assertLess(text.count(PAGE_PAYLOAD), canonical_text.count(PAGE_PAYLOAD))
        self.assertTrue(report['provider_closed']); self.assertTrue(report['counter_closed'])
        self.assertEqual(report['session_cleanup'], 'closed')

    async def test_compaction_retains_semantic_state_then_fresh_edit_and_independent_verification(self):
        report, rendered, session, canonical, state, seeded = await self.run_case()
        self.assert_retained_state(report, rendered, session, state, seeded)
        self.assertEqual(report['provider_decisions'], 4)
        self.assertEqual(report['action_input_count'], 1)
        self.assertEqual(report['actual_buffer'], 'Reviewed λ')
        self.assertEqual(report['actual_preserved_buffer'], 'protected')
        self.assertGreater(report['post_loop_final_capture_count'], 0)
        self.assertGreater(report['fresh_captures_since_discovery'], 2)
        self.assertEqual(report['final']['status'], 'verified_reviewed_scope')
        self.assertEqual(report['stop_reason'], 'verified_reviewed_completion')

    async def test_compacted_approval_cannot_override_changed_fresh_preservation(self):
        report, rendered, session, canonical, state, seeded = await self.run_case(True)
        self.assert_retained_state(report, rendered, session, state, seeded)
        self.assertEqual(report['action_input_count'], 0)
        self.assertEqual(report['actual_buffer'], 'initial')
        self.assertNotEqual(report['final']['status'], 'verified_reviewed_scope')
        action = next(e for e in canonical['events'] if e['tool'] == 'locua_act')
        self.assertEqual(action['result']['status'], 'refused')
        self.assertIn('preservation', action['result']['reason'].lower())
        self.assertIsNone(report['stop_reason'])


if __name__ == '__main__':
    unittest.main()
