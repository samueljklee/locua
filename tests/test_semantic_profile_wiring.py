"""The experimental projection must reach the existing guarded local loop."""
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch

from locua import lib
from locua.cli import main
from locua.errors import LocuaError


class SemanticProfileWiringTests(unittest.TestCase):
    profile = 'semantic-v1'
    def test_actual_cli_forwards_original_request_and_explicit_projection(self):
        request = 'Replace only the disposable α buffer; preserve its sibling and do not save.'
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
                'locua.amplifier_session.run', return_value={'status': 'blocked', 'artifacts': '/synthetic'}) as run:
            main([request, '--provider', 'local', '--model', 'qwen38',
                  '--tool-profile', self.profile, '--instruction-profile', 'continuity-v1'])
        run.assert_called_once()
        self.assertEqual(run.call_args.args, (request,))
        self.assertEqual(run.call_args.kwargs['tool_profile'], self.profile)
        self.assertEqual(run.call_args.kwargs['instruction_profile'], 'continuity-v1')
        self.assertEqual(run.call_args.kwargs['model'], 'qwen38')

    def test_each_existing_local_model_reaches_same_entry_without_fallback(self):
        for model in ('baseline', 'comparator', 'qwen38'):
            with self.subTest(model=model), patch('locua.amplifier_session.run', return_value={'status': 'blocked'}) as run:
                lib.do('Exact original outcome.', model=model, tool_profile=self.profile,
                       instruction_profile='continuity-v1', ask=lambda _: 'run')
                self.assertEqual(run.call_args.kwargs['model'], model)
                self.assertEqual(run.call_args.kwargs['tool_profile'], self.profile)

    def test_new_projection_does_not_authorize_hosted_or_other_engine_paths(self):
        invalid = [dict(provider='openai', model='gpt-5.6-sol'),
                   dict(harness='legacy'), dict(task_observations=True),
                   dict(url='http://localhost/fixture'), dict(document='/disposable/file.txt')]
        with patch('locua.amplifier_session.run') as run:
            for options in invalid:
                with self.subTest(options=options), self.assertRaises(LocuaError):
                    lib.do('Original outcome.', tool_profile=self.profile, ask=lambda _: 'run', **options)
        run.assert_not_called()


class SemanticV2ProfileWiringTests(SemanticProfileWiringTests):
    profile = 'semantic-v2'


if __name__ == '__main__':
    unittest.main()
