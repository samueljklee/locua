#!/usr/bin/env python3
"""Fresh read-only evaluator captures and automatic acceptance-evidence assembly.

The capture command acquires Locua's existing exclusive lease and invokes only
DesktopTools.observe on the caller's exact target. It never launches, activates,
edits, selects a model target, or invokes inference. Settings-specific selectors
below are independent evaluation oracles, never imported by the agent loop.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
from continuity_cli_eval import read, write, sha, load_case, _faithful_goal
from locua.desktop_tools import DesktopTools
from locua.desktop_session_lock import acquire_desktop_session


def cli_summary_path(run):
    root = Path(run).resolve()
    path = root / 'cli/summary.json' if (root / 'cli/summary.json').is_file() else root / 'summary.json'
    summary = read(path)
    if summary.get('status') in (None, 'starting', 'running'):
        raise ValueError('Final CLI summary is required before independent after-capture')
    return path


def read_settings_preferences():
    """Read only these two global appearance keys, retaining raw command proof."""
    result = {'values': {}, 'reads': {}, 'status': 'observed'}
    for key in ('AppleInterfaceStyle', 'AppleInterfaceStyleSwitchesAutomatically'):
        argv = ['/usr/bin/defaults', 'read', '-g', key]
        start = time.monotonic()
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
        raw = {'argv': argv, 'exit_code': completed.returncode, 'stdout': completed.stdout,
               'stderr': completed.stderr, 'wall_s': time.monotonic() - start}
        result['reads'][key] = raw
        if completed.returncode == 0:
            value = completed.stdout.strip()
            if key == 'AppleInterfaceStyle':
                if value != 'Dark': result['status'] = 'unavailable'; continue
            else:
                if value not in ('0', '1'): result['status'] = 'unavailable'; continue
                value = value == '1'
            result['values'][key] = value
        elif completed.returncode == 1 and 'does not exist' in completed.stderr:
            result['values'][key] = None
        else:
            result['status'] = 'unavailable'
    return result


def capture(out, pid, window_id, *, phase, config=None, after_run=None,
            settings_preferences=False, document=None):
    if phase not in ('before', 'after', 'original', 'restored'):
        raise ValueError('Explicit capture phase required')
    if type(pid) is not int or type(window_id) is not int or min(pid, window_id) <= 0:
        raise ValueError('Exact positive native pid and window_id required')
    if phase in ('after', 'restored') and after_run is None:
        raise ValueError('After/restored captures require --after-run final CLI artifacts')
    summary_path = cli_summary_path(after_run) if after_run else None
    root = Path(out).resolve(); root.mkdir(parents=True, exist_ok=False, mode=0o700)
    began = time.monotonic(); started_ns = time.time_ns()
    if summary_path and started_ns <= summary_path.stat().st_mtime_ns:
        raise ValueError('Independent capture must start after the final CLI summary')
    report = {'phase': phase, 'status': 'starting', 'target': {'pid': pid, 'window_id': window_id},
        'capture_started_at_ns': started_ns, 'capture_started_monotonic_s': began,
        'model_calls': 0, 'application_input': False, 'launch_or_activation': False,
        'fresh_independent_session': True,
        'after_cli_summary': str(summary_path) if summary_path else None,
        'after_cli_summary_sha256': sha(summary_path) if summary_path else None}
    try:
        with acquire_desktop_session(purpose='Independent read-only Locua evaluation capture'):
            desktop = DesktopTools(config, root / 'desktop')
            try:
                result = desktop.observe(report['target'])
                if result.get('status') != 'observed' or not isinstance(result.get('observation'), dict):
                    raise RuntimeError('Independent observation failed: ' + str(result.get('reason', result.get('status'))))
                observation = result['observation']
                if (observation.get('target') != report['target']
                    or observation.get('observed_at_ns', 0) < started_ns):
                    raise RuntimeError('Independent capture returned an old or different target')
                write(root / 'observation.json', observation)
                report.update(snapshot_id=observation['snapshot_id'], observation_sha256=sha(root / 'observation.json'),
                    observation_path=str(root / 'observation.json'), observed_at_ns=observation['observed_at_ns'])
                if settings_preferences:
                    report['settings_preferences'] = read_settings_preferences()
                if document:
                    path = Path(document).expanduser().resolve(strict=True)
                    report['document'] = {'path': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size,
                                          'read_at_ns': time.time_ns(), 'read_only': True}
                report['status'] = 'observed'
            finally:
                report['cleanup'] = desktop.close()
    except Exception as error:
        report.update(status='failed', reason=type(error).__name__ + ': ' + str(error))
    finally:
        report.update(capture_finished_at_ns=time.time_ns(), capture_wall_s=time.monotonic() - began,
            capture_finished_monotonic_s=time.monotonic())
        report['source_files'] = {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*')) if p.is_file()}
        report['raw_driver_transport_paths'] = [str(root / name) for name in report['source_files'] if name.endswith('transport.jsonl')]
        write(root / 'capture.json', report)
    return report


def load_capture(path, expected_phase=None):
    path = Path(path).resolve(); path = path / 'capture.json' if path.is_dir() else path
    report = read(path)
    if report.get('status') != 'observed' or report.get('model_calls') != 0 or report.get('application_input') is not False:
        raise ValueError('Successful independent read-only capture required')
    if expected_phase and report.get('phase') != expected_phase:
        raise ValueError('Independent capture phase mismatch')
    for name, expected_hash in report['source_files'].items():
        file = (path.parent / name).resolve()
        if not file.is_relative_to(path.parent) or sha(file) != expected_hash:
            raise ValueError('Independent raw capture source changed')
    observation_path = path.parent / 'observation.json'
    if sha(observation_path) != report['observation_sha256']:
        raise ValueError('Independent normalized observation changed')
    observation = read(observation_path)
    if observation['target'] != report['target'] or observation['observed_at_ns'] < report['capture_started_at_ns']:
        raise ValueError('Independent capture identity or freshness mismatch')
    return report, observation


def appearance_state(report, observation):
    selected = [c for c in observation['controls']
        if c.get('role') == 'AXButton' and c.get('states', {}).get('selected') is True
        and isinstance(c.get('semantics', {}).get('help'), str)
        and all(word in c.get('semantics', {}).get('help', '').lower() for word in ('appearance', 'buttons', 'menus', 'windows'))]
    preferences = report.get('settings_preferences') or {}
    if len(selected) != 1 or preferences.get('status') != 'observed':
        raise ValueError('Explicit selected Appearance and independent OS preferences required')
    return {'appearance': selected[0].get('name'), 'preferences': preferences['values']}


def icon_preservation_ids(observation):
    """Evaluator-only grouping from visible section labels and source order.

    This deliberately returns unknown if the icon section or explicit selected
    choice is absent. It never assigns false to missing selected attributes.
    """
    def line(c):
        source = c.get('source', {})
        return source.get('markdown_line_number', source.get('line_number'))
    labels = [c for c in observation['controls'] if c.get('role') in ('AXStaticText', 'AXHeading')
        and re.search(r'\bicon\b.*\bstyle\b', str(c.get('value') or c.get('name') or ''), re.I)
        and type(line(c)) is int]
    if len(labels) != 1: raise ValueError('Unique observed icon-style section label unavailable')
    label = labels[0]; start = line(label)
    boundaries = [line(c) for c in observation['controls']
        if c.get('parent') == label.get('parent') and c.get('role') in ('AXStaticText', 'AXHeading')
        and type(line(c)) is int and line(c) > start]
    if not boundaries: raise ValueError('Observed icon-style section boundary unavailable')
    end = min(boundaries)
    selected = [c['id'] for c in observation['controls'] if c.get('parent') == label.get('parent')
        and c.get('role') in ('AXButton', 'AXRadioButton') and type(line(c)) is int and start < line(c) < end
        and c.get('states', {}).get('selected') is True]
    if len(selected) != 1: raise ValueError('Exactly one explicitly selected icon choice required')
    return selected


def assemble(freeze, case_id, run, before, after, out, *, original=None, restored=None):
    _, case = load_case(freeze, case_id)
    summary_path = cli_summary_path(run); cli_root = summary_path.parent
    summary = read(summary_path); evidence = read(cli_root / 'desktop/evidence.json')
    if summary.get('request') != case['request'] or evidence.get('request') != case['request']:
        raise ValueError('CLI request differs from frozen case')
    before_report, before_obs = load_capture(before, 'before')
    after_report, after_obs = load_capture(after, 'after')
    if (after_report.get('after_cli_summary_sha256') != sha(summary_path)
        or after_report.get('after_cli_summary') != str(summary_path)
        or after_report['capture_started_at_ns'] <= summary_path.stat().st_mtime_ns
        or before_obs['target'] != after_obs['target']
        or after_obs['observed_at_ns'] <= before_obs['observed_at_ns']):
        raise ValueError('After-capture is not fresh and bound to this completed CLI run')
    candidates = [(sid, goal, scope['bindings'][goal['id']])
        for sid, scope in evidence.get('scopes', {}).items() for goal in scope.get('goals', [])
        if _faithful_goal(case, goal, scope.get('bindings', {}).get(goal.get('id'), {}))]
    report = {'case_id': case_id, 'run': str(cli_root), 'status': 'blocked', 'independent_completion': False,
              'human_or_model_target_hint_sent': False, 'independent_capture_wall_s': after_report['capture_wall_s']}
    if len(candidates) != 1:
        report['reason'] = 'no_faithful_reviewed_goal' if not candidates else 'ambiguous_faithful_reviewed_goals'
        write(Path(out).with_suffix('.assembly.json'), report)
        return report
    scope_id, goal, binding = candidates[0]
    outcome = {'before': before_obs, 'after': after_obs, 'goal': goal, 'binding': binding}
    if case['workflow'] == 'textedit':
        a, b = before_report.get('document') or {}, after_report.get('document') or {}
        if not a.get('path') or a.get('path') != b.get('path') or Path(a['path']).name != case['document']:
            raise ValueError('Same named disposable document file hashes are required')
        outcome.update(disk_before=a['sha256'], disk_after=b['sha256'])
    if case['workflow'] == 'settings':
        if original is None or restored is None:
            raise ValueError('Separate original and restored Settings captures required')
        original_report, original_obs = load_capture(original, 'original')
        restored_report, restored_obs = load_capture(restored, 'restored')
        if (restored_report.get('after_cli_summary_sha256') != sha(summary_path)
            or restored_obs['observed_at_ns'] <= after_obs['observed_at_ns']):
            raise ValueError('Restoration must be freshly checked after this task outcome')
        outcome.update(preserve_control_ids=icon_preservation_ids(before_obs),
            preferences_after=appearance_state(after_report, after_obs)['preferences'],
            original=appearance_state(original_report, original_obs),
            restored=appearance_state(restored_report, restored_obs))
        report['restoration_capture_wall_s_separate'] = restored_report['capture_wall_s']
    write(Path(out), outcome)
    report.update(status='ready', independent_completion=None, scope_id=scope_id,
        outcome_evidence_path=str(Path(out).resolve()), outcome_evidence_sha256=sha(out),
        cli_wall_excluding_review_s=summary.get('wall_excluding_human_s'),
        elapsed_with_independent_capture_s=(summary['wall_excluding_human_s'] + after_report['capture_wall_s']
            if type(summary.get('wall_excluding_human_s')) in (int, float) else None),
        next='Run continuity_cli_eval audit-run; assembly does not itself award completion.')
    write(Path(out).with_suffix('.assembly.json'), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='operation', required=True)
    cap = sub.add_parser('capture'); cap.add_argument('--out', required=True)
    cap.add_argument('--pid', required=True, type=int); cap.add_argument('--window-id', required=True, type=int)
    cap.add_argument('--phase', required=True, choices=('before', 'after', 'original', 'restored'))
    cap.add_argument('--config'); cap.add_argument('--after-run'); cap.add_argument('--document')
    cap.add_argument('--settings-preferences', action='store_true')
    asm = sub.add_parser('assemble')
    for flag in ('freeze', 'case-id', 'run', 'before', 'after', 'out'): asm.add_argument('--' + flag, required=True)
    asm.add_argument('--original'); asm.add_argument('--restored')
    args = vars(parser.parse_args()); operation = args.pop('operation')
    result = capture(**args) if operation == 'capture' else assemble(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('status') in ('ready', 'observed') else 1


if __name__ == '__main__': raise SystemExit(main())
