"""CPU-only capture boundary and evidence assembly; fake desktop, no real lease."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('continuity_capture', ROOT / 'tools/continuity_capture.py')
cap = importlib.util.module_from_spec(spec); spec.loader.exec_module(cap)
from tests.test_continuity_cli_eval import observation
from locua.goal_verification import bind_for_review
import continuity_cli_eval as evaluation


class FakeDesktop:
    value = 'Initial'
    calls = []
    def __init__(self, config, out):
        self.out = Path(out); (self.out / 'cua').mkdir(parents=True)
    def observe(self, target):
        self.calls.append(('observe', target))
        o = observation(str(time.time_ns()), self.value, time.time_ns())
        o['target'] = target
        (self.out / 'cua/transport.jsonl').write_text(json.dumps({'request': {'name': 'get_window_state'}, 'response': {'snapshot_id': o['snapshot_id']}}) + '\n')
        return {'status': 'observed', 'observation': o}
    def close(self):
        self.calls.append(('close', None)); return {'status': 'closed'}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); FakeDesktop.calls = []; FakeDesktop.value = 'Initial'
        self.addCleanup(patch.stopall)
        patch.object(cap, 'DesktopTools', FakeDesktop).start()
        patch.object(cap, 'acquire_desktop_session', return_value=nullcontext()).start()

    def capture(self, name, phase='before', **kwargs):
        return cap.capture(self.root / name, 4, 5, phase=phase, **kwargs)

    def test_capture_only_observes_exact_target_and_retains_raw_source(self):
        report = self.capture('before')
        self.assertEqual(report['status'], 'observed')
        self.assertEqual(FakeDesktop.calls, [('observe', {'pid': 4, 'window_id': 5}), ('close', None)])
        self.assertGreaterEqual(report['observed_at_ns'], report['capture_started_at_ns'])
        self.assertGreater(report['capture_wall_s'], 0)
        self.assertEqual(len(report['raw_driver_transport_paths']), 1)
        self.assertFalse(report['application_input'])
        self.assertEqual(report['model_calls'], 0)
        loaded, _ = cap.load_capture(self.root / 'before', 'before')
        self.assertEqual(loaded['observation_sha256'], report['observation_sha256'])

    def test_after_requires_completed_run_and_creates_new_capture(self):
        with self.assertRaisesRegex(ValueError, 'after-run'): self.capture('bad', 'after')
        run = self.root / 'run'; run.mkdir(); cap.write(run / 'summary.json', {'status': 'blocked'})
        report = self.capture('after', 'after', after_run=run)
        self.assertEqual(report['status'], 'observed')
        self.assertGreater(report['capture_started_at_ns'], (run / 'summary.json').stat().st_mtime_ns)
        self.assertEqual(report['after_cli_summary_sha256'], cap.sha(run / 'summary.json'))

    def test_running_run_and_reused_capture_are_rejected(self):
        run = self.root / 'run'; run.mkdir(); cap.write(run / 'summary.json', {'status': 'starting'})
        with self.assertRaisesRegex(ValueError, 'Final CLI summary'): self.capture('after', 'after', after_run=run)
        self.capture('before')
        with self.assertRaisesRegex(ValueError, 'phase mismatch'): cap.load_capture(self.root / 'before', 'after')

    def test_mutated_normalized_or_raw_capture_is_rejected(self):
        self.capture('one'); file = self.root / 'one/observation.json'; file.write_text(file.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'source changed'): cap.load_capture(self.root / 'one')
        self.capture('two'); file = self.root / 'two/desktop/cua/transport.jsonl'; file.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'source changed'): cap.load_capture(self.root / 'two')

    def test_busy_lease_does_not_create_or_interrupt_desktop(self):
        with patch.object(cap, 'acquire_desktop_session', side_effect=RuntimeError('busy')):
            report = self.capture('busy')
        self.assertEqual(report['status'], 'failed'); self.assertEqual(FakeDesktop.calls, [])

    def test_assembly_uses_recorded_goal_and_fresh_independent_captures(self):
        evaluation.prepare(self.root / 'freeze', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        _, case = evaluation.load_case(self.root / 'freeze', 'textedit-regression')
        document = self.root / case['document']; document.write_text('Original saved file')
        self.capture('before', document=document)
        _, before = cap.load_capture(self.root / 'before')
        goal = {'id': 'g', 'kind': 'text', 'target': 'Contents', 'value': case['value'], 'evidence_plane': 'editor_buffer'}
        binding = bind_for_review(goal, before['controls'][1], before)
        run = self.root / 'run'; (run / 'desktop').mkdir(parents=True)
        cap.write(run / 'summary.json', {'status': 'verified_reviewed_scope', 'request': case['request'], 'wall_excluding_human_s': 40})
        cap.write(run / 'desktop/evidence.json', {'request': case['request'], 'scopes': {'scope:1': {'goals': [goal], 'bindings': {'g': binding}}}})
        FakeDesktop.value = case['value']; self.capture('after', 'after', after_run=run, document=document)
        result = cap.assemble(self.root / 'freeze', case['id'], run, self.root / 'before', self.root / 'after', self.root / 'outcome.json')
        self.assertEqual(result['status'], 'ready', result)
        self.assertIsNone(result['independent_completion'])
        outcome = cap.read(self.root / 'outcome.json')
        self.assertEqual(outcome['goal'], goal); self.assertEqual(outcome['binding'], binding)
        self.assertEqual(outcome['disk_before'], outcome['disk_after'])
        self.assertGreater(result['elapsed_with_independent_capture_s'], 40)

    def test_preferences_distinguish_absent_key_from_read_error(self):
        missing = subprocess.CompletedProcess([], 1, '', 'The domain/default pair does not exist')
        with patch.object(cap.subprocess, 'run', return_value=missing): report = cap.read_settings_preferences()
        self.assertEqual(report['status'], 'observed'); self.assertIsNone(report['values']['AppleInterfaceStyle'])
        denied = subprocess.CompletedProcess([], 1, '', 'Permission denied')
        with patch.object(cap.subprocess, 'run', return_value=denied): report = cap.read_settings_preferences()
        self.assertEqual(report['status'], 'unavailable'); self.assertNotIn('AppleInterfaceStyle', report['values'])


class SettingsOracleTests(unittest.TestCase):
    def test_selected_controls_without_help_do_not_mask_appearance(self):
        data = {'controls': [
            {'role': 'AXButton', 'name': 'Default', 'states': {'selected': True},
             'semantics': {'help': None}},
            {'role': 'AXButton', 'name': 'Light', 'states': {'selected': True},
             'semantics': {'help': 'Use a light appearance for buttons, menus, and windows.'}}]}
        report = {'settings_preferences': {'status': 'observed', 'values': {'AppleInterfaceStyle': None}}}
        self.assertEqual(cap.appearance_state(report, data)['appearance'], 'Light')
        data['controls'][1]['semantics']['help'] = None
        with self.assertRaisesRegex(ValueError, 'Explicit selected Appearance'):
            cap.appearance_state(report, data)

    def controls(self):
        return {'controls': [
            {'id': 'label', 'role': 'AXStaticText', 'value': 'Icon & widget style', 'parent': 'w', 'source': {'line_number': 10}},
            {'id': 'choice-a', 'role': 'AXButton', 'name': 'Default', 'parent': 'w', 'states': {'selected': True}, 'source': {'markdown_line_number': 11}},
            {'id': 'choice-b', 'role': 'AXButton', 'name': 'Dark', 'parent': 'w', 'states': {}, 'source': {'markdown_line_number': 12}},
            {'id': 'next', 'role': 'AXStaticText', 'value': 'Folder color', 'parent': 'w', 'source': {'line_number': 13}},
            {'id': 'outside', 'role': 'AXButton', 'name': 'Dark', 'parent': 'w', 'states': {'selected': True}, 'source': {'markdown_line_number': 20}}]}

    def test_icon_oracle_uses_actual_selected_choice_with_semantic_section(self):
        self.assertEqual(cap.icon_preservation_ids(self.controls()), ['choice-a'])

    def test_unknown_selection_or_ambiguous_section_blocks_oracle(self):
        data = self.controls(); data['controls'][1]['states'] = {}
        with self.assertRaisesRegex(ValueError, 'explicitly selected'): cap.icon_preservation_ids(data)
        data = self.controls(); data['controls'].append(deepcopy(data['controls'][0]))
        with self.assertRaisesRegex(ValueError, 'Unique observed'): cap.icon_preservation_ids(data)


if __name__ == '__main__': unittest.main()
