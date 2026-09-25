"""Public CLI/library defaults and explicit historical routes, without inference."""
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch

from locua import lib
from locua.cli import main
from locua.errors import LocuaError


class PreviewDefaultsTests(unittest.TestCase):
    def test_every_language_cli_entry_resolves_same_preview(self):
        for args in ([], ['start'], ['do'], ['Open Calendar and switch to Day view.'],
                     ['start', 'Open Calendar and switch to Day view.'],
                     ['do', 'Open Calendar and switch to Day view.']):
            with self.subTest(args=args), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
                    'locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
                self.assertEqual(main(args), 6)
                self.assertEqual(run.call_args.kwargs['model'], 'qwen38')
                self.assertEqual(run.call_args.kwargs['tool_profile'], 'step-v2')
                self.assertEqual(run.call_args.kwargs['instruction_profile'], 'continuity-v1')

    def test_library_and_model_overrides_keep_default_profiles(self):
        for entry in (lib.do, lib.start):
            for model in (None, 'qwen38', 'comparator', 'baseline'):
                with self.subTest(entry=entry.__name__, model=model), patch(
                        'locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
                    entry(request='Exact original request.', model=model, ask=lambda _: 'run')
                    self.assertEqual(run.call_args.kwargs['model'], model or 'qwen38')
                    self.assertEqual(run.call_args.kwargs['tool_profile'], 'step-v2')
                    self.assertEqual(run.call_args.kwargs['instruction_profile'], 'continuity-v1')

    def test_cli_explicit_former_baseline_is_not_promoted(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
                'locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            main(['Open Calendar', '--model', 'comparator', '--tool-profile', 'baseline',
                  '--instruction-profile', 'baseline'])
        # Internal session defaults intentionally preserve the historical baseline.
        self.assertEqual(run.call_args.kwargs['model'], 'comparator')
        self.assertEqual(run.call_args.kwargs.get('tool_profile', 'baseline'), 'baseline')
        self.assertEqual(run.call_args.kwargs.get('instruction_profile', 'baseline'), 'baseline')

    def test_legacy_and_explicit_targets_keep_original_adapter_defaults(self):
        routes = (({'harness': 'legacy'}, 'locua.goal_loop.run'),
                  ({'url': 'http://127.0.0.1:1234'}, 'locua.conversation.run'),
                  ({'document': '/synthetic/draft.txt'}, 'locua.conversation.run'))
        for options, target in routes:
            with self.subTest(options=options), patch(target, return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
                lib.do('Exact original request.', ask=lambda _: 'run', **options)
                self.assertEqual(run.call_args.kwargs['model'], 'comparator')
                self.assertNotIn('tool_profile', run.call_args.kwargs)
                self.assertNotIn('instruction_profile', run.call_args.kwargs)
            with self.assertRaises(LocuaError):
                lib.do('Task', model='qwen38', ask=lambda _: 'run', **options)

    def test_manual_run_eval_keep_original_model(self):
        with patch('locua.guided.start', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            lib.start(manual=True, ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['model'], 'baseline')
        with patch('locua.lib._engine') as engine:
            lib.run(task={'synthetic': True})
            self.assertEqual(engine.call_args.args[1]['model'], 'baseline')
            lib.evaluate([])
            self.assertEqual(engine.call_args.args[1]['model'], 'baseline')

    def test_hosted_requires_model_and_never_inherits_local_only_profiles(self):
        with self.assertRaises(LocuaError) as caught:
            lib.do('Task', provider='openai', ask=lambda _: 'run')
        self.assertEqual(caught.exception.code, 'explicit_hosted_model_required')
        with patch('locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            lib.do('Task', provider='openai', model='gpt-5.6-sol', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['provider'], 'openai')
            self.assertEqual(run.call_args.kwargs.get('tool_profile', 'baseline'), 'baseline')
            self.assertEqual(run.call_args.kwargs.get('instruction_profile', 'baseline'), 'baseline')
        with self.assertRaises(LocuaError):
            lib.do('Task', provider='openai', model='gpt-5.6-sol', tool_profile='step-v2', ask=lambda _: 'run')

    def test_incompatible_explicit_instruction_is_not_silently_replaced(self):
        with patch('locua.amplifier_session.run') as run, self.assertRaises(LocuaError):
            lib.do('Task', instruction_profile='concise-v1', ask=lambda _: 'run')
        run.assert_not_called()
        with patch('locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            lib.do('Task', tool_profile='baseline', instruction_profile='concise-v1', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs['instruction_profile'], 'concise-v1')

    def test_help_names_current_default_and_packaged_profile_guidance(self):
        for capability in (None, 'start', 'do'):
            for text in (lib.short_help(capability), lib.skill(capability)):
                for value in ('qwen38', 'step-v2', 'continuity-v1'):
                    self.assertIn(value, text)
