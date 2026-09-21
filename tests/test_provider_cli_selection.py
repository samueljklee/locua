"""Explicit provider selection, no credentials/model/desktop needed."""
from contextlib import redirect_stderr,redirect_stdout
import io
import unittest
from unittest.mock import patch
from locua import lib
from locua.cli import main
from locua.errors import LocuaError

class SelectionTests(unittest.TestCase):
    def test_default_keeps_local_comparator_and_no_new_options(self):
        with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as run:
            lib.do('Use Calculator',ask=lambda _: 'run')
        self.assertEqual(run.call_args.kwargs['model'],'comparator')
        for field in ('provider','thinking','task_observations','budget_ledger'):
            self.assertNotIn(field,run.call_args.kwargs)

    def test_explicit_hosted_request_and_provider_reach_same_loop(self):
        goal='Use Calculator to evaluate a supplied expression.'
        with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as run:
            result=lib.do(goal,provider='openai',model='gpt-5.6-sol',budget_ledger='/tmp/private-ledger.json',ask=lambda _: 'run')
        self.assertEqual(run.call_args.args,(goal,))
        self.assertEqual(run.call_args.kwargs['provider'],'openai')
        self.assertEqual(run.call_args.kwargs['model'],'gpt-5.6-sol')
        self.assertFalse(result['ok']);run.assert_called_once()

    def test_no_hosted_model_default_or_cross_path_fallback(self):
        invalid=[{'provider':'openai','model':None}, {'provider':'anthropic','model':'claude-opus-5','harness':'legacy'},
                 {'provider':'openai','model':'gpt-5.6-sol','url':'http://localhost'},
                 {'provider':'openai','model':'gpt-5.6-sol','thinking':True},
                 {'provider':'local','model':'baseline','thinking':True},
                 {'provider':'local','model':'qwen38','thinking':True,'harness':'legacy'},
                 {'provider':'local','model':'unknown'}, {'provider':'unknown','model':'qwen38'}]
        with patch('locua.amplifier_session.run') as run:
            for args in invalid:
                with self.subTest(args=args),self.assertRaises(LocuaError):lib.do('Task',ask=lambda _: 'run',**args)
        run.assert_not_called()

    def test_explicit_local_thinking_and_task_scope_are_forwarded(self):
        with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as run:
            lib.do('Use Calculator',model='qwen38',thinking=True,task_observations=True,ask=lambda _: 'run')
        self.assertTrue(run.call_args.kwargs['thinking']);self.assertTrue(run.call_args.kwargs['task_observations'])
        self.assertEqual(run.call_args.kwargs['provider'],'local')

    def test_cli_requires_hosted_model_before_any_backend(self):
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),patch('locua.amplifier_session.run') as run:
            code=main(['do','Use Calculator','--provider','openai'])
        self.assertEqual(code,2);run.assert_not_called()

    def test_manual_stays_local_and_has_no_experimental_provider(self):
        with patch('locua.guided.start') as run,self.assertRaises(LocuaError):
            lib.start(manual=True,provider='anthropic',model='claude-opus-5',ask=lambda _: 'run')
        run.assert_not_called()

if __name__=='__main__':unittest.main()
