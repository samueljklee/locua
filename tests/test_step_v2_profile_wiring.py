"""Explicit step-v2 routing through the existing local guarded session, CPU only."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua import lib
from locua.cli import main
from locua.errors import LocuaError
from locua.amplifier_tools import DesktopToolset
from tests.test_amplifier_tools import Desktop
import test_step_profile_wiring as step_fixtures


class StepV2PublicWiringTests(step_fixtures.StepProfileWiringTests):
    profile = 'step-v2'

    def test_all_language_entry_forms_and_help_preserve_explicit_selection(self):
        request = 'Replace the disposable text exactly; preserve its sibling.'
        for prefix in ([], ['do'], ['start']):
            with self.subTest(prefix=prefix), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
                    'locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
                main(prefix + [request, '--tool-profile', self.profile, '--instruction-profile', 'continuity-v1'])
                self.assertEqual(run.call_args.args, (request,))
                self.assertEqual(run.call_args.kwargs['tool_profile'], self.profile)
                self.assertEqual(run.call_args.kwargs['model'], 'qwen38')
        for capability in ('start', 'do'):
            self.assertIn('step-v2', lib.short_help(capability))
        self.assertIn('--tool-profile step-v2', lib.manifest()['body'])

    def test_language_start_library_and_manual_restriction(self):
        with patch('locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            lib.start(request='Original exact request.', model='baseline', tool_profile=self.profile,
                      instruction_profile='continuity-v1', ask=lambda _: 'run')
        self.assertEqual(run.call_args.args, ('Original exact request.',))
        self.assertEqual(run.call_args.kwargs['model'], 'baseline')
        self.assertEqual(run.call_args.kwargs['tool_profile'], self.profile)
        with patch('locua.guided.start') as manual, self.assertRaises(LocuaError):
            lib.start(manual=True, tool_profile=self.profile, ask=lambda _: 'run')
        manual.assert_not_called()


class StepV2SessionWiringTests(unittest.TestCase):
    def test_same_guarded_owner_and_session_features_remain_enabled(self):
        from locua import amplifier_session as session
        from locua import desktop_session_lock as locks
        from locua.step_interface import StepModelInterface, StepNamedModelInterface
        from locua.instruction_policy import instruction_policy
        for profile, expected_class, revision in (
                ('step-v1', StepModelInterface, 'tools-v6.22/step-v1'),
                ('step-v2', StepNamedModelInterface, 'tools-v6.24/step-v2')):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); owners = []; providers = []; messages = []; observed_options = {}
                request = 'Replace Entry with exact λ; preserve Competitor. Application persistence is allowed.'
                class Owner(DesktopToolset):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, desktop=Desktop(), **kwargs); owners.append(self)
                class Provider:
                    def __init__(self, **kwargs):
                        self.options = kwargs; self.records = []; self.budget_records = []; self._closed = False
                        providers.append(self)
                    async def close(self): self._closed = True
                async def execute(request_arg, provider, tools, **options):
                    self.assertEqual(request_arg, request)
                    self.assertIs(type(owners[0].model_interface), expected_class)
                    self.assertEqual(options['system'], instruction_policy('continuity-v1'))
                    self.assertEqual(options['execution_facts_mode'], 'tail')
                    self.assertTrue(options['focus_dedup'])
                    self.assertTrue(callable(options['verified_completion']))
                    observed_options.update(options)
                    ui = owners[0].model_interface
                    app = ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
                    window = ui.call('locua_inspect', {'reference': app})['windows'][0]['window_id']
                    view = ui.call('locua_inspect', {'reference': window})['view']
                    rows = ui.call('locua_search', {'reference': view})['items']
                    entry = next(r for r in rows if r.get('name') == 'Entry')
                    keep = next(r for r in rows if r.get('name') == 'Competitor')
                    reviewed = ui.call('locua_review', {'summary': request, 'goals': [
                        {'outcome': entry['outcomes']['text'], 'value': 'exact λ', 'persistence_requirement': 'not_requested'}],
                        'preserves': [keep['outcomes']['text']], 'covers_request': True})
                    self.assertEqual(reviewed['status'], 'approved', reviewed)
                    acted = ui.call('locua_act', {'input': entry['inputs']['set_text'], 'value': 'exact λ'})
                    self.assertEqual(acted['status'], 'verified', acted)
                    self.assertEqual(ui.call('locua_verify', {})['status'], 'verified')
                    proof = options['verified_completion']()
                    self.assertIsNotNone(proof)
                    return {'response': '', 'session_cleanup': 'closed', 'verified_completion': proof}
                with patch('locua.amplifier_tools.DesktopToolset', Owner), patch(
                        'locua.amplifier_provider.LocalAmplifierProvider', Provider), patch(
                        'locua.amplifier_session.execute_session', execute), patch('locua.lib._config', return_value={}), patch(
                        'locua.engine_adapter.require'), patch('locua.amplifier_session._dependencies'), patch.object(
                        locks, 'default_lock_path', return_value=root/'desktop.lock'):
                    result = session.run(request, model='qwen38', tool_profile=profile, instruction_profile='continuity-v1',
                                         config={}, out=root/'run', ask=lambda _: 'run', progress=messages.append)
                self.assertEqual(result['status'], 'verified_reviewed_scope', result.get('reason'))
                self.assertEqual(result['tool_interface'], revision)
                self.assertEqual(result['provider_integration'], 'providers-v7.1')
                self.assertEqual(result['model'], 'qwen38'); self.assertTrue(result['inference_local_only'])
                self.assertFalse(result['rlcd_used'])
                self.assertTrue(result['provider_closed']); self.assertTrue(result['desktop_control_lease']['released'])
                self.assertEqual(owners[0].desktop.value, 'exact λ')
                self.assertEqual(owners[0].desktop.other, 'protected')
                self.assertEqual(len(owners[0].desktop.executions), 1)
                self.assertTrue(providers[0].options['protocol_recovery'])
                self.assertTrue(providers[0].options['qwen38_prompt_cache'])
                self.assertTrue(providers[0].options['qwen38_stable_prefix_cache'])
                self.assertTrue(any(revision in message and 'RLCD is not used' in message for message in messages))
                self.assertIn('Original request is authoritative', observed_options['execution_facts']())


if __name__ == '__main__': unittest.main()
