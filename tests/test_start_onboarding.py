"""Entry-point failures must be actionable without granting execution authority."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua import lib
from locua.cli import main
from locua.errors import LocuaError


class StartOnboardingTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_language_alias_dispatches_to_reviewed_language_workflow(self):
        value = {'schema':'locua.result.v1','operation':'do','ok':False,
                 'result':{'status':'blocked','reason':'synthetic','artifacts':'/synthetic'}}
        with patch('locua.lib.do', return_value=value) as language, \
             patch('locua.guided.start', side_effect=AssertionError('no silent manual fallback')):
            for argv in (['start', 'open calculator'], ['do', 'open calculator'], ['open calculator']):
                code, out, err = self.invoke(argv)
                self.assertEqual(code, 6)
                self.assertEqual(language.call_args.kwargs['request'], 'open calculator')
                self.assertTrue(callable(language.call_args.kwargs['ask']))
            self.assertEqual(language.call_args.kwargs['model'], 'comparator')

    def test_library_requires_review_callback_for_language(self):
        with self.assertRaises(LocuaError) as caught:
            lib.do('change something')
        self.assertEqual(caught.exception.code, 'review_interaction_required')

    def test_missing_config_reports_selected_path_and_remedy_without_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / 'not-configured.json'
            with patch('locua.config.default_path', return_value=root/'default.json'), \
                 patch('locua.engine.prototype.cua.CuaOwner', side_effect=AssertionError('no driver')):
                code, out, err = self.invoke(['start', '--manual', '--config', str(config), '--out', str(root/'run')])
            self.assertEqual(code, 6)
            self.assertIn('Missing configuration: driver_binary, driver_socket', out)
            self.assertIn('Configuration: '+str(config), out)
            self.assertIn('Next step:', out)
            self.assertIn('locua setup --help', out)
            summary = json.loads((root/'run'/'summary.json').read_text())
            self.assertEqual(summary['error']['code'], 'runtime_configuration_missing')
            self.assertEqual(summary['remedy'], summary['error']['remedy'])
            self.assertEqual(summary['config_path'], str(config))

    def test_json_preserves_actionable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch('locua.config.default_path', return_value=root/'default.json'):
                code, out, err = self.invoke(['start', '--manual', '--json', '--config', str(root/'absent.json'),
                                              '--out', str(root/'run')])
            self.assertEqual(code, 6)
            result = json.loads(out)
            self.assertFalse(result['ok'])
            self.assertIn('LOCUA_CONFIG', result['result']['remedy'])


if __name__ == '__main__':
    unittest.main()
