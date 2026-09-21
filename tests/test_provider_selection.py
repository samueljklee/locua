"""Hosted selection/budget checks with fake SDK boundaries; no network/model IO."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from amplifier_core.message_models import ChatRequest, ChatResponse, Message, TextBlock, Usage
from locua.provider_selection import (CONFIGURATIONS, HostedAmplifierProvider, HostedLimitError,
                                      SpendLedger, load_credential, validate_selection)


def response():
    return ChatResponse(content=[TextBlock(text='fixture response')],
                        usage=Usage(input_tokens=1000, output_tokens=20, total_tokens=1020))


class FakeOfficial:
    def __init__(self, name):
        self.name = name; self.use_streaming = False; self._retry_config = SimpleNamespace(max_retries=0)
        self.calls = []; self.fail = False; self.escalate = False; self.continue_once = False
        self.count = 1000; self.closed = False
        async def create(**params):
            self.calls.append(deepcopy(params))
            if self.fail: raise TimeoutError('Synthetic uncertain request')
            return response()
        async def count_tokens(**params): return SimpleNamespace(input_tokens=self.count)
        self.client = SimpleNamespace(max_retries=0, messages=SimpleNamespace(
            count_tokens=count_tokens, with_raw_response=SimpleNamespace(create=create)))
    async def _create_response(self, params, *, native_input_tokens=None):
        self.calls.append(deepcopy(params))
        if self.fail: raise TimeoutError('Synthetic uncertain request')
        return response()
    async def _native_input_token_count(self, params): return self.count
    @staticmethod
    def _count_tokens_params(params): return {'model': params['model'], 'messages': params['messages']}
    async def request_budget(self, request, *, context_estimate):
        return {'measurement': {'kind': 'provider_count', 'input_tokens': self.count, 'source': 'official-fake'},
                'estimated_input_tokens': self.count, 'input_limit_tokens': 1000000,
                'context_token_budget': context_estimate, 'max_output_tokens': 2048}
    async def complete(self, request):
        model = CONFIGURATIONS[self.name]['model']
        if self.name == 'openai':
            params = {'model': model, 'max_output_tokens': 2048, 'input': [{'role': 'user', 'content': 'fixture'}]}
            result = await self._create_response(params, native_input_tokens=self.count)
            if self.escalate:
                await self._create_response({**params, 'max_output_tokens': 128000})
            if self.continue_once: await self._create_response(params)
            return result
        return await self.client.messages.with_raw_response.create(model=model, max_tokens=2048, messages=[])
    def parse_tool_calls(self, result): return result.tool_calls
    async def close(self): self.closed = True


class ProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.sequence = 0
        self.request = ChatRequest(messages=[Message(role='user', content='Pure fixture request')])
    def provider(self, name='openai', **kwargs):
        self.sequence += 1
        result = HostedAmplifierProvider(name, CONFIGURATIONS[name]['model'], out=self.root/str(self.sequence),
            budget_path=self.root/'shared.json', **kwargs)
        result._provider = FakeOfficial(name); result._cleanup = result._provider.close
        result._install_dispatch_guard()
        return result

    async def test_original_provider_response_and_request_semantics_are_preserved(self):
        for name in CONFIGURATIONS:
            with self.subTest(name=name):
                provider = self.provider(name); original = deepcopy(self.request.model_dump())
                result = await provider.complete(self.request)
                self.assertEqual(result.content[0].text, 'fixture response')
                self.assertEqual(self.request.model_dump(), original)
                self.assertEqual(len(provider._provider.calls), 1)
                self.assertEqual(provider.records[0]['complete_generation_count'], 1)
                self.assertEqual(provider.cost_report()['unknown_reservations'], 0)
                self.assertEqual(provider.metadata['config']['max_retries'], 0)
                self.assertFalse(provider.metadata['config']['use_streaming'])
                await provider.close(); self.assertTrue(provider._closed)

    async def test_hidden_output_escalation_never_reaches_sdk(self):
        provider = self.provider(); provider._provider.escalate = True
        with self.assertRaisesRegex(HostedLimitError, 'escalation'):
            await provider.complete(self.request)
        self.assertEqual(len(provider._provider.calls), 1)
        self.assertEqual(len(provider.dispatch_records), 1)
        self.assertEqual(provider.dispatch_records[0]['max_output_tokens'], 2048)

    async def test_hidden_continuations_consume_actual_dispatch_cap(self):
        provider = self.provider(max_calls=1); provider._provider.continue_once = True
        with self.assertRaisesRegex(HostedLimitError, 'dispatch limit'):
            await provider.complete(self.request)
        self.assertEqual(len(provider._provider.calls), 1)
        self.assertEqual(provider.cost_report()['dispatch_reservations'], 1)

    async def test_shared_budget_survives_unknown_calls_and_new_provider_instances(self):
        first = self.provider(spend_cap_usd='0.15'); first._provider.fail = True
        with self.assertRaises(TimeoutError): await first.complete(self.request)
        charged = first.cost_report()
        self.assertEqual(charged['unknown_reservations'], 1)
        self.assertGreater(charged['charged_upper_bound_usd'], 0)
        second = self.provider(spend_cap_usd='0.15'); second._provider.fail = True
        with self.assertRaisesRegex(HostedLimitError, 'budget exhausted'):
            await second.complete(self.request)
        self.assertFalse(second._provider.calls)
        self.assertEqual(second.cost_report()['dispatch_reservations'], 1)

    async def test_shared_cap_cannot_be_reset_or_increased(self):
        ledger = SpendLedger(self.root/'cap.json', 1)
        ledger.reserve(100000, {'provider': 'fixture'})
        with self.assertRaises(HostedLimitError): SpendLedger(self.root/'cap.json', 15).report()
        with self.assertRaises(ValueError): SpendLedger(self.root/'other.json', 16)

    async def test_count_unavailable_or_oversized_never_generates(self):
        for count in (None, True, 24577):
            provider = self.provider(); provider._provider.count = count
            with self.subTest(count=count), self.assertRaises(HostedLimitError):
                await provider.complete(self.request)
            self.assertFalse(provider._provider.calls)

    async def test_native_measured_budget_requests_standard_context_compaction(self):
        provider = self.provider(); provider._provider.count = 25000
        decision = await provider.request_budget(self.request, context_estimate=10000)
        self.assertEqual(decision['measurement']['input_tokens'], 25000)
        self.assertEqual(decision['input_limit_tokens'], 24576)
        self.assertLess(decision['context_token_budget'], 10000)
        self.assertFalse(provider.records); self.assertFalse(provider.dispatch_records)

    async def test_overrides_and_streaming_do_not_bypass_frozen_configuration(self):
        changes = [{'model': 'other'}, {'max_output_tokens': 4096}, {'stream': True},
                   {'reasoning_effort': 'high'}, {'temperature': 1}]
        for change in changes:
            provider = self.provider()
            with self.subTest(change=change), self.assertRaises(HostedLimitError):
                await provider.complete(self.request.model_copy(update=change))
            self.assertFalse(provider._provider.calls)
        provider = self.provider()
        with self.assertRaises(HostedLimitError): await provider.complete(self.request, request_options={'model': 'other'})
        provider._provider.use_streaming = True
        with self.assertRaises(HostedLimitError): await provider.complete(self.request)
        self.assertFalse(provider._provider.calls)

    async def test_ledger_symlink_refuses_without_touching_target(self):
        target = self.root/'target'; target.write_text('do not change')
        link = self.root/'link'; link.symlink_to(target)
        with self.assertRaises(OSError): SpendLedger(link).report()
        self.assertEqual(target.read_text(), 'do not change')

    async def test_safe_key_loading_does_not_execute_expand_or_return_other_secrets(self):
        path = self.root/'keys.env'
        path.write_text("UNRELATED_SECRET=ignored\nexport OPENAI_API_KEY='fixture-key'\n")
        self.assertEqual(load_credential('OPENAI_API_KEY', path), 'fixture-key')
        for value in ('$(touch bad)', '`echo bad`', 'one two'):
            path.write_text('OPENAI_API_KEY='+value+'\n')
            with self.assertRaises(ValueError): load_credential('OPENAI_API_KEY', path)
        path.write_text('OPENAI_API_KEY=one\nOPENAI_API_KEY=two\n')
        with self.assertRaises(ValueError): load_credential('OPENAI_API_KEY', path)
        with self.assertRaises(ValueError): load_credential('UNRELATED_SECRET', path)

    async def test_exact_models_required_and_no_default_cloud_selection(self):
        for provider, model in [('openai', None), ('anthropic', 'claude-latest'), ('local', 'qwen38')]:
            with self.assertRaises(ValueError): validate_selection(provider, model)

    async def test_default_ledger_is_persistent_config_sibling(self):
        with patch('locua.config.default_path', return_value=self.root/'config/config.json'):
            provider = HostedAmplifierProvider('openai', 'gpt-5.6-sol', out=self.root/'default')
        self.assertEqual(provider.ledger.path, self.root/'config/hosted-budget.json')

    async def test_secret_canary_never_enters_request_artifacts(self):
        provider = self.provider(); provider._credential = 'fixture-SECRET-CANARY-1709'
        request = self.request.model_copy(update={'messages': [Message(role='user', content=provider._credential)]})
        with self.assertRaisesRegex(HostedLimitError, 'Credential material'):
            await provider.complete(request)
        self.assertFalse(list(provider.out.glob('*')))
        for params in ({'api_key': 'unused'}, {'extra_headers': {'Authorization': 'unused'}},
                       {'model': provider.model, 'input': provider._credential}):
            provider._active = {'call': 1, 'dispatches': []}
            with self.assertRaises(HostedLimitError):
                await provider._dispatch(params, 100, AsyncMock())
        self.assertFalse(provider.dispatch_records)
        self.assertFalse(list(provider.out.glob('*')))

    async def test_invalid_usage_preserves_unknown_reservation(self):
        for usage in ({'input_tokens': True, 'output_tokens': 10},
                      {'input_tokens': 10, 'output_tokens': -1}, {'input_tokens': 10}):
            provider = self.provider()
            async def bad_response(params, **kwargs):
                return response().model_copy(update={'usage': usage})
            provider._provider._create_response = bad_response
            provider._install_dispatch_guard()
            with self.assertRaisesRegex(HostedLimitError, 'usage unavailable or invalid'):
                await provider.complete(self.request)
            report = provider.cost_report()
            self.assertEqual(report['per_run_unknown_reservation_count'], 1)
            self.assertEqual(report['per_run_known_charge_upper_usd'], 0)
            self.assertIsNone(provider.records[-1]['complete_generation_count'])

    async def test_per_run_cost_does_not_include_other_runs_and_usage_is_preserved(self):
        first = self.provider(); await first.complete(self.request)
        second = self.provider('anthropic'); await second.complete(self.request)
        first_cost, second_cost = first.cost_report(), second.cost_report()
        self.assertGreater(second_cost['charged_upper_bound_usd'], second_cost['per_run_known_charge_upper_usd'])
        self.assertEqual(first_cost['per_run_known_charge_upper_usd'], 0.0054)
        self.assertEqual(second_cost['per_run_known_charge_upper_usd'], 0.0105)
        self.assertEqual(second_cost['usage_by_dispatch'][0]['usage']['input_tokens'], 1000)
        self.assertEqual(second.budget_records[0]['status'], 'completed')
        self.assertEqual(second.budget_records[0]['worker_measurement']['input_tokens'], 1000)
        self.assertEqual(second.records[0]['request'], self.request.model_dump(mode='json'))

    async def test_malformed_cache_usage_and_corrupt_negative_ledger_fail_closed(self):
        provider = self.provider()
        self.assertIsNone(provider._charge({'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': -1}))
        ledger = SpendLedger(self.root/'corrupt.json')
        ledger.reserve(10, {})
        state = json.loads(ledger.path.read_text()); state['reservations'][0]['charged_micro_usd'] = -100
        ledger.path.write_text(json.dumps(state))
        with self.assertRaises(HostedLimitError): ledger.reserve(10, {})


@unittest.skipUnless(importlib.util.find_spec('amplifier_module_provider_openai') and
                     importlib.util.find_spec('amplifier_module_provider_anthropic'), 'Optional official providers absent')
class OfficialAssemblyTests(unittest.IsolatedAsyncioTestCase):
    def test_official_frozen_configs_keep_native_output_limit_without_dispatch(self):
        from amplifier_module_provider_openai import OpenAIProvider
        from amplifier_module_provider_anthropic import AnthropicProvider
        request = ChatRequest(messages=[Message(role='user', content='Offline assembly fixture')])
        for name, cls in [('openai', OpenAIProvider), ('anthropic', AnthropicProvider)]:
            provider = cls(api_key='unused-fixture-key', config=CONFIGURATIONS[name]['config'])
            if name == 'openai': params = provider._budget_params(request)
            else: params = provider._assemble_request_params(request, request_options={}, request_caps=provider._default_caps).params
            self.assertEqual(params['model'], CONFIGURATIONS[name]['model'])
            self.assertEqual(params.get('max_output_tokens', params.get('max_tokens')), 2048)
            self.assertFalse(provider.use_streaming)
            self.assertEqual(provider._retry_config.max_retries, 0)
            if name == 'anthropic':
                self.assertFalse(provider._fallback_on_overload)
                self.assertFalse(provider._refusal_fallback_enabled)
                self.assertEqual(params['thinking'], {'type': 'adaptive', 'display': 'summarized'})

    async def test_real_official_mount_uses_existing_client_instance_guard_and_no_auth_artifacts(self):
        class Coordinator:
            def __init__(self): self.providers = {}; self.contributors = []
            async def mount(self, category, value, *, name): self.providers[name] = value
            def get(self, category): return self.providers
            def register_contributor(self, *args): self.contributors.append(args)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); keys = root/'keys.env'
            keys.write_text('OPENAI_API_KEY=fixture-secret-openai\nANTHROPIC_API_KEY=fixture-secret-anthropic\n')
            for name in CONFIGURATIONS:
                provider = HostedAmplifierProvider(name, CONFIGURATIONS[name]['model'],
                    out=root/name, budget_path=root/'ledger', keys_path=keys)
                coordinator = Coordinator()
                try:
                    await provider.mount(coordinator)
                    self.assertIs(coordinator.providers[name], provider)
                    self.assertEqual(provider._provider.client.max_retries, 0)
                    self.assertEqual(provider.get_info().defaults['max_output_tokens'], 2048)
                    if name == 'openai':
                        with self.assertRaisesRegex(HostedLimitError, 'outside an active'):
                            await provider._provider._create_response({}, native_input_tokens=1)
                    else:
                        resource = provider._provider.client.messages.with_raw_response
                        self.assertIs(resource, provider._provider.client.messages.with_raw_response)
                        provider._provider.client.messages.count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=1))
                        with self.assertRaisesRegex(HostedLimitError, 'outside an active'):
                            await resource.create(model=provider.model, messages=[], max_tokens=2048)
                    for artifact in provider.out.glob('*.json'):
                        self.assertNotIn('fixture-secret', artifact.read_text())
                    self.assertEqual(provider.metadata['dependencies'][CONFIGURATIONS[name]['sdk']], CONFIGURATIONS[name]['sdk_version'])
                finally: await provider.close()
                self.assertTrue(provider._closed)


if __name__ == '__main__': unittest.main()
