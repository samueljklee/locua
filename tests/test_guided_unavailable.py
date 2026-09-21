"""User-controlled capture recovery stays before plan approval or model actions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua import lib
from locua.errors import LocuaError
from locua.guided import start, _window_status
from locua.engine.prototype.perception import NativeObservationUnavailable


def unavailable():
    return LocuaError('native_accessibility_unavailable', 'Cannot read accessibility controls.',
                      'Bring the selected window forward, then retry.', details={'target': {'pid': 123, 'window_id': 456}})


WINDOWS = [{'app_name': 'Synthetic app', 'title': 'Panel', 'pid': 123, 'window_id': 456},
           {'app_name': 'Synthetic editor', 'title': 'Note', 'pid': 789, 'window_id': 101}]


class GuidedUnavailableTests(unittest.TestCase):
    def test_window_status_preserves_unknown_and_does_not_claim_closed(self):
        self.assertEqual(_window_status({'is_on_screen': True}), 'on screen')
        self.assertEqual(_window_status({'is_on_screen': False, 'on_current_space': True}), 'not on screen')
        self.assertEqual(_window_status({'is_on_screen': False, 'on_current_space': False}), 'not on screen; another desktop')
        self.assertEqual(_window_status({}), 'visibility unknown')

    def workflow(self, answers, replies, listings=None):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)/'run'; responses = iter(answers); messages = []
        listings = listings or [WINDOWS]
        inventories = [{'result': {'targets': {'windows': w}}} for w in listings]
        with patch('locua.lib.targets', side_effect=inventories) as targets, \
             patch('locua.lib._engine', side_effect=replies) as engine, \
             patch('locua.lib.run', side_effect=AssertionError('no reviewed task')) as execute:
            result = start(config={}, out=root, ask=lambda _: next(responses), progress=messages.append)
        execute.assert_not_called()
        self.assertEqual(json.loads((root/'summary.json').read_text()), result)
        return result, engine, targets, messages

    def test_stopping_preserves_specific_error_and_never_retries(self):
        result, engine, _, messages = self.workflow(['1', ''], [unavailable()])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'native_accessibility_unavailable')
        self.assertEqual(engine.call_count, 1)
        self.assertIn('Cannot read accessibility controls.', '\n'.join(messages))

    def test_retry_keeps_exact_target_and_new_artifact_directory(self):
        result, engine, targets, _ = self.workflow(['1', 'retry', ''], [unavailable(), unavailable()])
        self.assertEqual(engine.call_count, 2); self.assertEqual(targets.call_count, 1)
        first, second = [call.args[1] for call in engine.call_args_list]
        self.assertEqual(first['target'], second['target'])
        self.assertNotEqual(first['out'], second['out'])
        self.assertEqual(len(result['discovery_attempts']), 2)

    def test_choose_refreshes_list_and_requires_new_explicit_selection(self):
        result, engine, targets, _ = self.workflow(['1', 'choose', '1', ''], [unavailable(), unavailable()],
                                                   listings=[WINDOWS, list(reversed(WINDOWS))])
        self.assertEqual(targets.call_count, 2)
        self.assertEqual(engine.call_args_list[0].args[1]['target'], {'pid': 123, 'window_id': 456})
        self.assertEqual(engine.call_args_list[1].args[1]['target'], {'pid': 789, 'window_id': 101})

    def test_explicit_retries_are_bounded_and_failure_history_is_retained(self):
        result, engine, _, messages = self.workflow(['1', 'retry', 'retry'], [unavailable() for _ in range(3)])
        self.assertEqual(engine.call_count, 3)
        self.assertEqual(len(result['discovery_attempts']), 3)
        self.assertIn('Stopped after three', '\n'.join(messages))

    def test_recovered_capture_reaches_field_entry_without_executing(self):
        from test_guided import native_observed
        observed = native_observed()
        prepared = {'result': {'scope': {'kind': 'native', **observed['target']},
                                'target_label': 'Synthetic native target', 'observation': observed}}
        result, engine, _, messages = self.workflow(['1', 'retry', ''], [unavailable(), prepared])
        self.assertEqual(result['status'], 'canceled')
        self.assertEqual(result['reason'], 'no_changes_selected')
        self.assertEqual([a['status'] for a in result['discovery_attempts']], ['unavailable', 'observed'])
        self.assertIn('Observed target: Synthetic app: Panel', '\n'.join(messages))
        self.assertEqual(engine.call_count, 2)

    def test_generic_or_cleanup_failures_do_not_offer_recovery(self):
        result, engine, _, messages = self.workflow(['1'], [LocuaError('cleanup_failed', 'Owner remains', 'Inspect owner')])
        self.assertEqual(engine.call_count, 1)
        self.assertEqual(result['error']['code'], 'cleanup_failed')
        self.assertNotIn('Unavailable:', '\n'.join(messages))

    def test_public_library_preserves_native_failure_metadata(self):
        failure = NativeObservationUnavailable({'degraded': True}, {'pid': 123, 'window_id': 456}, 'abc')
        with patch('locua.engine_adapter.execute', side_effect=failure):
            with self.assertRaises(LocuaError) as caught:
                lib._engine('prepare_guided', {}, {})
        self.assertEqual(caught.exception.code, 'native_observation_degraded')
        self.assertEqual(caught.exception.details['target'], {'pid': 123, 'window_id': 456})


if __name__ == '__main__':
    unittest.main()
