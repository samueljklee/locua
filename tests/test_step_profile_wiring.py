"""Explicit experimental routing; prior local and decoder options unchanged."""
import unittest
from unittest.mock import patch
import test_semantic_profile_wiring as fixtures
from locua import lib
from locua.errors import LocuaError


class StepProfileWiringTests(fixtures.SemanticProfileWiringTests):
    profile = 'step-v1'

    def test_old_argument_help_is_not_combined_with_new_typed_tools(self):
        for instructions in ('principles-help-v1', 'continuity-arguments-v1', 'concise-v1'):
            with self.subTest(instructions=instructions), patch('locua.amplifier_session.run') as run:
                with self.assertRaises(LocuaError):
                    lib.do('An ordinary original request.', tool_profile=self.profile,
                           instruction_profile=instructions, ask=lambda _: 'run')
                run.assert_not_called()

    def test_explicit_model_uses_current_default_tools(self):
        with patch('locua.amplifier_session.run', return_value={'status':'blocked'}) as run:
            lib.do('Original request.', model='comparator', ask=lambda _: 'run')
            self.assertEqual(run.call_args.kwargs.get('tool_profile'), 'step-v2')
            self.assertEqual(run.call_args.kwargs['model'], 'comparator')


if __name__ == '__main__':
    unittest.main()
