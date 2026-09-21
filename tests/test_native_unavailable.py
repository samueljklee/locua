"""Degraded native captures retain failure evidence and cannot mint action handles."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from locua.engine.prototype.perception import (NativeObservationUnavailable,
    ObservationError, normalize_observation)
from locua.engine.prototype.cua import CuaAdapter

TARGET = {'pid': 123, 'window_id': 456}


def degraded():
    return {**TARGET, 'degraded': True,
            'degraded_reason': 'ax_window_unresolved: no matching AX window',
            'elements': [], 'elements_complete': False,
            'background_input': {'exact_window': {**TARGET, 'status': 'ax_unresolved'},
                                 'routes': [{'route': 'accessibility', 'status': 'refused'}]}}


def normalize(payload):
    return normalize_observation(payload, kind='native', expected_target=TARGET,
                                 observed_at_ns=time.time_ns())


class NativeUnavailableTests(unittest.TestCase):
    def test_degraded_capture_reports_limitation_before_snapshot_requirement(self):
        source = degraded()
        with self.assertRaises(NativeObservationUnavailable) as caught:
            normalize(source)
        error = caught.exception
        self.assertEqual(error.code, 'native_accessibility_unavailable')
        self.assertEqual(error.details['target'], TARGET)
        self.assertEqual(error.details['degraded_reason'], source['degraded_reason'])
        self.assertEqual(len(error.details['raw_sha256']), 64)
        self.assertNotIn('snapshot_id', source)
        self.assertNotIn('snapshot_id', str(error))

    def test_identity_mismatch_is_not_a_recoverable_accessibility_failure(self):
        source = degraded(); source['pid'] += 1
        with self.assertRaises(ObservationError) as caught:
            normalize(source)
        self.assertNotIsInstance(caught.exception, NativeObservationUnavailable)

    def test_degraded_flag_cannot_pass_with_a_plausible_snapshot(self):
        source = degraded(); source['snapshot_id'] = 'old-snapshot'
        source['elements'] = [{'element_index': 0, 'role': 'AXButton', 'label': 'Other surface'}]
        with self.assertRaises(NativeObservationUnavailable):
            normalize(source)

    def test_unmarked_malformed_capture_is_not_misclassified_as_ax_unavailable(self):
        with self.assertRaises(ObservationError) as caught:
            normalize({**TARGET, 'elements': []})
        self.assertNotIsInstance(caught.exception, NativeObservationUnavailable)

    def test_failed_refresh_invalidates_previous_dispatch_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Mock(); owner.directory = Path(directory)
            owner.inventory = {'tools': []}; owner.server = {}
            payload = degraded()
            owner.call.return_value = (payload, payload)
            adapter = CuaAdapter(owner, kind='native_window_state', target=TARGET, execute=True)
            adapter.latest = {'snapshot_id': 'prior', 'handles': {'button': {'kind': 'native'}}}
            with self.assertRaises(NativeObservationUnavailable):
                adapter.observe()
            self.assertIsNone(adapter.latest)
            with self.assertRaisesRegex(ValueError, 'latest observation'):
                adapter.dispatch({'snapshot_id': 'prior', 'control_id': 'button'})
            self.assertEqual(owner.call.call_count, 1)
            self.assertEqual(owner.call.call_args.args[0], 'get_window_state')


if __name__ == '__main__':
    unittest.main()
