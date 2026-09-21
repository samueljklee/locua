"""Prompt-factor isolation and actual framework wiring; no inference/desktop claims."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from locua import lib
from locua.amplifier_contracts import SPECS
from locua.amplifier_session import SYSTEM, execute_session
from locua.errors import LocuaError
from locua.instruction_policy import (
    BASELINE, CONCISE, EXAMPLES, PROFILES, apply_tool_help,
    instruction_policy, metadata, tool_descriptions,
)


class IsolationTests(unittest.TestCase):
    def test_baseline_is_frozen_and_variants_change_only_declared_factors(self):
        self.assertEqual(SYSTEM, BASELINE)
        self.assertEqual(hashlib.sha256(BASELINE.encode()).hexdigest(),
                         'cb93b8df655b02e2b7d9d1f321fddb1bf392d2e17ac6ed5649f77d6e1806ae74')
        self.assertEqual(instruction_policy('concise-v1'), CONCISE)
        self.assertEqual(instruction_policy('concise-examples-v1'), CONCISE + EXAMPLES)
        self.assertEqual(instruction_policy('concise-help-v1'), CONCISE)
        before = deepcopy(SPECS)
        for profile in PROFILES:
            tools = [SimpleNamespace(name=n, description=d, input_schema=deepcopy(s))
                     for n, (d, s) in SPECS.items()]
            apply_tool_help(tools, profile)
            self.assertEqual({t.name: t.input_schema for t in tools},
                             {n: s for n, (_, s) in before.items()})
            for tool in tools:
                expected = tool_descriptions(profile).get(tool.name, before[tool.name][0])
                self.assertEqual(tool.description, expected)
            self.assertFalse(metadata(profile)['guards_changed'])
        self.assertEqual(SPECS, before)

    def test_help_inventory_mismatch_cannot_partly_rewrite_tools(self):
        tool = SimpleNamespace(name='foreign_tool', description='unchanged')
        with self.assertRaises(ValueError):
            apply_tool_help([tool], 'concise-help-v1')
        self.assertEqual(tool.description, 'unchanged')

    def test_library_routes_explicit_profile_and_rejects_ignored_options(self):
        with patch('locua.amplifier_session.run', return_value={'status': 'blocked'}) as run:
            lib.do('Edit the requested draft.', instruction_profile='concise-v1', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['instruction_profile'], 'concise-v1')
            lib.start(request='Edit the requested draft.', instruction_profile='concise-help-v1', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['instruction_profile'], 'concise-help-v1')
            for options in ({'harness': 'legacy'}, {'url': 'http://127.0.0.1/'},
                            {'document': 'draft.txt'}):
                with self.subTest(options=options), self.assertRaises(LocuaError):
                    lib.do('Edit.', instruction_profile='concise-v1', ask=lambda _: 'run', **options)
            with self.assertRaises(LocuaError):
                lib.start(manual=True, instruction_profile='concise-v1', ask=lambda _: 'run')
            with self.assertRaises(LocuaError):
                lib.do('Edit.', instruction_profile='unknown', ask=lambda _: 'run')

    def test_cli_forwards_explicit_profile_without_creating_a_provider(self):
        from locua.cli import main
        with patch('locua.cli.lib.do', return_value={'ok': False, 'result': {'status': 'blocked'}}) as run:
            main(['A test outcome', '--instruction-profile', 'concise-v1', '--json'])
        self.assertEqual(run.call_args.kwargs['instruction_profile'], 'concise-v1')

    def test_declared_hosted_cap_is_forwarded_and_never_applies_to_local(self):
        with patch('locua.amplifier_session.run', return_value={'status': 'blocked'}) as run:
            lib.do('Edit a draft.', provider='openai', model='gpt-5.6-sol',
                   budget_cap_usd=12, budget_ledger='new-authorized-ledger.json', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['budget_cap_usd'], 12)
            for invalid in (0, -1, 16, float('nan'), True):
                with self.subTest(invalid=invalid), self.assertRaises(LocuaError):
                    lib.do('Edit.', provider='openai', model='gpt-5.6-sol',
                           budget_cap_usd=invalid, ask=lambda _: 'run')
            with self.assertRaises(LocuaError):
                lib.do('Edit.', budget_cap_usd=12, ask=lambda _: 'run')


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_receives_exact_profile_and_keeps_tool_result_pairs(self):
        from amplifier_core.message_models import ChatResponse, TextBlock, ToolCall, ToolCallBlock
        from amplifier_core.models import ToolResult
        from locua.amplifier_provider import LocalAmplifierProvider, native_request

        original = 'Preserve this literal exactly: A\nB. Do not save.'
        for profile in PROFILES:
            seen = []

            class Provider(LocalAmplifierProvider):
                request_budget = None

                async def complete(self, request, **kwargs):
                    seen.append(native_request(request, model='qwen38'))
                    if len(seen) == 1:
                        return ChatResponse(
                            content=[ToolCallBlock(id='status1', name='locua_status', input={})],
                            tool_calls=[ToolCall(id='status1', name='locua_status', arguments={})])
                    return ChatResponse(content=[TextBlock(text='Synthetic receipt read; no task execution.')])

            class Tool:
                def __init__(self, name):
                    self.name = name
                    self.description, schema = SPECS[name]
                    self.input_schema = deepcopy(schema)

                async def execute(self, arguments):
                    return ToolResult(success=True, output={'status': 'retained', 'receipt': 'pair-proof'})

            tools = apply_tool_help([Tool(n) for n in SPECS], profile)
            with TemporaryDirectory() as directory:
                result = await execute_session(original, Provider(model='qwen38'), tools,
                    out=Path(directory)/'session', system=instruction_policy(profile))
            self.assertEqual(len(seen), 2)
            for native in seen:
                self.assertEqual(native['messages'][0]['content'],
                    instruction_policy(profile) + '\nORIGINAL USER REQUEST (retain throughout):\n' + original)
                for wire in native['tools']:
                    spec = wire['function'] if 'function' in wire else wire
                    name = spec['name']
                    self.assertEqual(spec['description'],
                        tool_descriptions(profile).get(name, SPECS[name][0]))
            self.assertIn('pair-proof', json.dumps(seen[1]['messages']))
            self.assertEqual(result['session_cleanup'], 'closed')


if __name__ == '__main__':
    unittest.main()
