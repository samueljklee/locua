#!/usr/bin/env python3
"""Bounded installed-CLI evaluation; setup and independent capture stay external.

Only ``run`` starts a CLI/model. Review decisions are explicit, hash-bound
receipts, not blanket stdin approval. No oracle, control ID or task schema is
passed to Locua. ``check_outcome`` accepts separately captured normalized AX
observations and rechecks bindings; it is intentionally not a success-from-log
shortcut. Driver effects still require comparison_audit or independent review.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
from loop_v9_eval import pending_prompt, read, write, sha, bounded_arithmetic
from comparison_audit import (count_metrics, _editor_exact, number, application_proof,
                              resolve_native_input, READS, SETUP, WRITES)

MODELS = ('baseline', 'comparator', 'qwen38')
PROFILES = {'tool_profile': 'continuity-v1', 'instruction_profile': 'continuity-v1'}
TOOL_PROFILES = ('continuity-v1', 'semantic-v1', 'semantic-v2', 'step-v1', 'step-v2')
REVIEW_FLAGS = ('request_faithful', 'restrictions_preserved', 'effects_within_authorization')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def default_cases():
    """Evaluation fixtures only; no executor imports this table."""
    title = 'Locua-v10-transfer-draft.txt'
    cases = [
        {'id': 'calculator-regression', 'workflow': 'calculator', 'role': 'regression',
         'request': 'Use Calculator to work out 192*231-100.', 'expression': '192*231-100',
         'app_name': 'Calculator', 'setup': 'Clear the disposable calculator state; capture the initial readout and layout.'},
        {'id': 'textedit-regression', 'workflow': 'textedit', 'role': 'regression',
         'request': f"In TextEdit, replace the entire text in '{title}' with 'Status: reviewed locally.'. Do not save.",
         'app_name': 'TextEdit', 'document': title, 'value': 'Status: reviewed locally.',
         'setup': 'Open only the named disposable buffer; capture exact initial buffer and file hash. No personal document edits.'},
        {'id': 'settings-regression', 'workflow': 'settings', 'role': 'regression',
         'request': 'In System Settings, enable Dark Appearance. Do not change the icon style or any other settings.',
         'app_name': 'System Settings', 'appearance': 'Dark',
         'setup': 'Capture original Appearance and the explicitly selected icon choice; ensure Light before this task; restore the original Appearance afterward.'},
        {'id': 'calculator-heldout', 'workflow': 'calculator', 'role': 'heldout',
         'request': 'Use Calculator to calculate (38 + 14) * 6.', 'expression': '(38+14)*6',
         'app_name': 'Calculator', 'setup': 'Same disposable calculator reset; do not pre-enter expression or result.'},
        {'id': 'textedit-heldout', 'workflow': 'textedit', 'role': 'heldout',
         'request': f"In TextEdit, replace the entire buffer of '{title}' with exactly these two lines, with no final newline:\nReview α: ready\nNext: local test\nDo not save.",
         'app_name': 'TextEdit', 'document': title, 'value': 'Review α: ready\nNext: local test',
         'setup': 'Same disposable buffer reset and original-file hash check.'},
        {'id': 'settings-heldout', 'workflow': 'settings', 'role': 'heldout',
         'request': 'In System Settings, switch the overall Appearance to Light. Keep the icon style and every other setting unchanged.',
         'app_name': 'System Settings', 'appearance': 'Light',
         'setup': 'Capture original Appearance and explicitly selected icon choice; ensure Dark before this task; restore original Appearance afterward.'}]
    return cases


def _load_protocol(root):
    root = Path(root).resolve(); manifest = read(root / 'protocol.json')
    if sha(root / 'protocol.json') != read(root / 'freeze.json')['protocol_sha256']:
        raise ValueError('Frozen protocol changed')
    return manifest


def _freshness_root(root, manifest=None):
    """All revisions inherit one exposure ledger; a new directory cannot reset it."""
    root = Path(root).resolve(); manifest = manifest or _load_protocol(root); seen = set()
    while manifest.get('case_source'):
        if root in seen: raise ValueError('Frozen case lineage is cyclic')
        seen.add(root); source = manifest['case_source']; previous = Path(source['freeze']).resolve()
        parent = _load_protocol(previous)
        if (sha(previous / 'protocol.json') != source['protocol_sha256']
                or digest(parent['cases']) != digest(manifest['cases'])):
            raise ValueError('Inherited frozen case definitions changed')
        root, manifest = previous, parent
    return root


def prepare(out, deadline, candidates=None, *, cases=None, cleanup_reserve_s=30,
            cases_from=None, tool_profile=None, instruction_profile='continuity-v1',
            require_candidate_freeze=False):
    root = Path(out); root.mkdir(parents=True, exist_ok=False, mode=0o700)
    parsed = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('Explicit deadline timezone required')
    end = parsed.timestamp()
    now = time.time()
    if not now < end <= now + 6 * 3600 + 1:
        raise ValueError('Deadline must be future, timezone-qualified, and within six hours')
    if candidates is not None and tool_profile: raise ValueError('Choose candidates or tool profiles, not both')
    profiles = tool_profile or []
    if any(p not in TOOL_PROFILES for p in profiles):
        raise ValueError('Unknown explicit CLI tool profile')
    if instruction_profile != 'continuity-v1': raise ValueError('This comparison keeps continuity-v1 instructions fixed')
    if profiles:
        candidates = [{'candidate_id': p + ':' + m, 'model': m, 'tool_profile': p,
                       'instruction_profile': instruction_profile} for p in profiles for m in MODELS]
    candidates = candidates or [{'model': m, **PROFILES} for m in MODELS]
    if set(c['model'] for c in candidates) != set(MODELS):
        raise ValueError('Freeze all three installed model aliases before scoring')
    if not 30 <= cleanup_reserve_s <= 3600: raise ValueError('Cleanup reserve must be 30..3600 seconds')
    candidate_ids = [c.get('candidate_id', c['model']) for c in candidates]
    if len(candidate_ids) != len(set(candidate_ids)): raise ValueError('Unique candidate IDs required')
    source = None; inherited = None
    if cases_from:
        if cases is not None: raise ValueError('Inherited frozen cases cannot be overridden')
        inherited = _load_protocol(cases_from)
        if list(_freshness_root(cases_from, inherited).glob('heldout-exposure-*.json')):
            raise ValueError('Heldout was already exposed; a revision cannot reset global freshness')
        cases = deepcopy(inherited['cases'])
        source = {'freeze': str(Path(cases_from).resolve()),
                  'protocol_sha256': sha(Path(cases_from) / 'protocol.json')}
    manifest = {'version': 'locua-continuity-cli-v1', 'created_at_ns': time.time_ns(),
        'deadline_epoch': end, 'deadline_utc': deadline, 'cases': cases or default_cases(),
        'cleanup_reserve_s': cleanup_reserve_s,
        'candidates': candidates, 'pilot_timeout_s': 300, 'qualification_attempts_per_workflow': 10,
        'required_verified': 8, 'typical_limit_s': 120, 'required_unintended_changes': 0,
        'heldout_rule': 'Freeze every candidate before any held-out exposure. Tuning after exposure starts a new development campaign; exposed cells are not fresh.',
        'helper_setup_not_model_completion': True, 'runner_sha256': sha(__file__)}
    if require_candidate_freeze:
        manifest['candidate_source_freeze_required_later'] = True
    if source:
        manifest.update(version='locua-continuity-cli-v3', case_source=source,
            candidate_source_freeze_required_later=True,
            qualification_slots=deepcopy(inherited.get('qualification_slots', [
                {'slot': n, 'case_role': 'regression' if n <= 5 else 'heldout'} for n in range(1, 11)])),
            qualification_build_rule='One exact installed build and profile pair across all workflows/models; no pilot pooling.')
    write(root / 'protocol.json', manifest)
    write(root / 'freeze.json', {'protocol_sha256': sha(root / 'protocol.json')})
    return manifest


def load_case(freeze, case_id, model=None, candidate_id=None):
    root = Path(freeze); manifest = _load_protocol(root)
    _freshness_root(root, manifest)
    cases = [c for c in manifest['cases'] if c['id'] == case_id]
    if len(cases) != 1: raise ValueError('Unknown/duplicate frozen case')
    if model is not None and len([c for c in manifest['candidates'] if c['model'] == model
            and (candidate_id is None or c.get('candidate_id', c['model']) == candidate_id)]) != 1:
        raise ValueError('Unknown/duplicate model candidate')
    return manifest, cases[0]


def _evaluator_identity():
    return {name: sha(ROOT / name) for name in ('tools/continuity_cli_eval.py',
        'tools/continuity_capture.py', 'tools/comparison_audit.py', 'tools/loop_v9_eval.py')}


def freeze_candidate(freeze, cli, out, config=None):
    """Freeze one installed build for all qualification cells, without launching it."""
    root = Path(freeze); manifest = _load_protocol(root)
    identity = installed_identity(cli)
    if not identity['installed_source_identity_proven']:
        raise ValueError('Installed package identity is unavailable')
    freshness_root = _freshness_root(root, manifest)
    exposures = list(freshness_root.glob('heldout-exposure-*.json'))
    if exposures: raise ValueError('Heldout was already exposed; no new candidate freeze')
    result = {'version': 'locua-cli-candidate-freeze-v1', 'frozen_at_ns': time.time_ns(),
        'protocol_sha256': sha(root / 'protocol.json'), 'installed': identity,
        'candidates': manifest['candidates'], 'before_any_heldout_exposure': True,
        'evaluator_files': _evaluator_identity(),
        'global_freshness_root': str(freshness_root),
        'configuration': {'path': str(Path(config).absolute()), 'sha256': sha(config)} if config else None}
    path = Path(out)
    with path.open('x') as handle: json.dump(result, handle, indent=2, ensure_ascii=False)
    path.chmod(0o400)
    return result


def _check_candidate_freeze(path, cli, protocol_sha256, candidate, started_at_ns, config=None):
    record = read(path)
    actual = installed_identity(cli)
    if (record.get('protocol_sha256') != protocol_sha256
            or record.get('before_any_heldout_exposure') is not True
            or type(record.get('frozen_at_ns')) is not int
            or record['frozen_at_ns'] >= started_at_ns
            or candidate not in record.get('candidates', [])
            or record.get('installed') != actual
            or record.get('evaluator_files') != _evaluator_identity()
            or (record.get('configuration') is not None and (config is None
                or record['configuration'] != {'path': str(Path(config).absolute()), 'sha256': sha(config)}))
            or not actual['installed_source_identity_proven']):
        raise ValueError('Candidate freeze does not prove this exact installed build/profile before exposure')
    return {'path': str(Path(path).resolve()), 'sha256': sha(path),
        'frozen_at_ns': record['frozen_at_ns'], 'verified_before_launch': True}


def approve(root, number, *, decision, reviewer, reason, checks):
    """Called only after the evaluator reads the concrete emitted review."""
    root = Path(root); launch = read(root / 'launch.json')
    path = root / 'cli/desktop' / f'review-{number}.json'; review = read(path)
    if review.get('original_request') != launch['request']:
        raise ValueError('Review changed the original request')
    if not reviewer.strip() or not reason.strip() or decision not in ('approve', 'reject'):
        raise ValueError('Explicit named reviewer, fidelity reason and decision required')
    if decision == 'approve' and (set(checks) != set(REVIEW_FLAGS) or not all(checks[k] is True for k in REVIEW_FLAGS)):
        raise ValueError('Approval requires all three explicit fidelity checks')
    receipt = {'review_sha256': sha(path), 'request_sha256': digest(launch['request']),
        'decision': decision, 'reviewer': reviewer, 'reason': reason, 'checks': checks,
        'assistance': 'independent evaluator supplies routine scoped review only', 'at_ns': time.time_ns()}
    write(root / f'review-{number}-decision.json', receipt)
    return receipt


def review_gate(review, spec, oracle, *, live_observation=None, prose_attestation=None):
    """Private evaluator gate; never instructions, an action plan, or a model tool.

    Structured checks are necessary but not sufficient for arbitrary model prose.
    Unknown prose requires a named independent, hash-bound judgment. This gate
    never dispatches input, and retained capture agreement never replaces the
    engine's fresh predispatch checks. Partial faithful scopes remain possible.
    """
    reasons = []; prose = []
    controls = spec.get('controls', [])
    by_key = {c['key']: c for c in controls}
    by_identifier = {c.get('identifier'): c for c in controls}
    if len(by_key) != len(controls) or len(by_identifier) != len(controls) or None in by_identifier:
        reasons.append('oracle_control_identities_ambiguous')
    if review.get('original_request') != spec['request']: reasons.append('original_request_changed')
    observation = live_observation or {}
    capture = review.get('review_capture', {})
    if (not observation or review.get('target') != observation.get('target')
            or capture.get('snapshot_id') != observation.get('snapshot_id')
            or capture.get('observed_at_ns') != observation.get('observed_at_ns')
            or capture.get('fresh_capture_required_before_input') is not True):
        reasons.append('review_observation_identity_unproved')
    source = {}
    for c in observation.get('controls', []):
        key = c.get('semantics', {}).get('identifier')
        source.setdefault(key, []).append(c)

    def identity(descriptor):
        identifier = descriptor.get('semantics', {}).get('identifier')
        expected = by_identifier.get(identifier)
        observed = source.get(identifier, [])
        if expected is None or len(observed) != 1:
            reasons.append('unknown_or_ambiguous_observed_binding'); return None
        control = observed[0]
        # Compare real observed identity, not a convenient same-named fixture.
        for field in ('role', 'name'):
            if descriptor.get(field) != control.get(field): reasons.append('observed_identity_changed')
        for field in ('identifier', 'help'):
            if descriptor.get('semantics', {}).get(field) != control.get('semantics', {}).get(field):
                reasons.append('observed_identity_changed')
        parent_id = control.get('parent')
        parent = next((c for c in observation.get('controls', []) if c.get('id') == parent_id), None)
        expected_parent = by_key.get(expected.get('parent'))
        if expected_parent and (parent is None or parent.get('semantics', {}).get('identifier') != expected_parent['identifier']):
            reasons.append('control_parent_identity_unproved')
        ancestors = descriptor.get('ancestors', [])
        if parent and (not ancestors or ancestors[0].get('role') != parent.get('role')
                or ancestors[0].get('semantics', {}).get('identifier') != parent.get('semantics', {}).get('identifier')):
            reasons.append('review_parent_association_unproved')
        return expected['key']

    expected_goals = {g['key']: g for g in oracle.get('goals', [])}
    proposed = {}; id_to_key = {}; bindings = review.get('observed_bindings', {})
    for goal in review.get('goals', []):
        descriptor = bindings.get(goal.get('id'), {})
        key = identity(descriptor)
        if key in proposed or goal.get('id') in id_to_key: reasons.append('duplicate_goal')
        proposed[key] = goal; id_to_key[goal.get('id')] = key
        expected = expected_goals.get(key)
        if expected is None:
            reasons.append('goal_is_not_requested'); continue
        for field in ('kind', 'value', 'property', 'expression', 'evidence_plane'):
            if digest(goal.get(field)) != digest(expected.get(field)):
                reasons.append('requested_goal_' + field + '_changed')
        if goal.get('target') not in (descriptor.get('name'), descriptor.get('semantics', {}).get('identifier')):
            prose.append('goal_target_description')
    if not proposed: reasons.append('no_requested_goal_in_review')
    full = review.get('coverage_declaration') == 'This scope covers the entire original request; please reject if anything is missing.'
    missing = set(expected_goals) - set(proposed)
    if full and (missing or review.get('unresolved_requirements')): reasons.append('whole_request_coverage_not_faithful')
    if missing and not review.get('unresolved_requirements'): reasons.append('missing_goal_not_retained_as_unresolved')
    if review.get('unresolved_requirements'): prose.append('unresolved_requirement_description')

    preserved = {}
    for predicate in review.get('preserves', []):
        descriptor = predicate.get('observed', {})
        key = identity(descriptor)
        prop = descriptor.get('property')
        if (key, prop) in preserved: reasons.append('duplicate_preservation')
        preserved[key, prop] = predicate.get('value')
        rows = source.get(descriptor.get('semantics', {}).get('identifier'), [])
        if len(rows) == 1:
            actual = rows[0].get('value') if prop == 'value' else rows[0].get('states', {}).get(prop)
            if digest(actual) != digest(predicate.get('value')): reasons.append('preserved_value_not_observed')
        if predicate.get('target') != str(descriptor.get('name')): prose.append('preservation_target_description')
    for expected in oracle.get('preserves', []):
        key = (expected['key'], expected['property'])
        if key not in preserved: reasons.append('required_preservation_missing')
        elif digest(preserved[key]) != digest(expected['value']): reasons.append('required_preservation_changed')
    # Extra observed, true preservation predicates are safe; extra writes are not.
    effect_keys = set()
    for effect in review.get('effects', []):
        if effect.get('kind') == 'goal':
            key = id_to_key.get(effect.get('goal_id'))
        elif effect.get('kind') == 'press':
            key = identity(effect.get('identity', {}))
            if key not in proposed or proposed[key].get('kind') != 'state':
                reasons.append('supported_press_is_not_requested')
            purpose = effect.get('purpose', '')
            generated = ('Attempt the requested selected=True; initial state is unknown, fresh verification required',)
            if purpose not in generated: prose.append('press_purpose')
        else:
            key = None; reasons.append('unknown_review_effect')
        if key is None or key not in proposed: reasons.append('effect_outside_requested_scope')
        if key == 'save' and oracle.get('no_save'): reasons.append('unrequested_save_effect')
        effect_keys.add(key)
    if not set(proposed) <= effect_keys: reasons.append('review_does_not_authorize_requested_goal')
    if review.get('summary') != spec['request']: prose.append('summary')
    trusted_limits = {'Native app coverage can be incomplete; only listed predicates are checked.',
        'Display/editor evidence is not committed-document or saved-file evidence.'}
    if any(x not in trusted_limits for x in review.get('limits', [])): prose.append('additional_caveat')
    if review.get('coverage_declaration') not in (
            'This scope covers the entire original request; please reject if anything is missing.',
            'Partial scope only; other requested outcomes remain unproved.'):
        prose.append('coverage_declaration')
    attested = (isinstance(prose_attestation, dict)
        and prose_attestation.get('review_sha256') == digest(review)
        and prose_attestation.get('request_sha256') == digest(spec['request'])
        and prose_attestation.get('approved') is True
        and prose_attestation.get('contradictory_claims') is False
        and bool(str(prose_attestation.get('reviewer', '')).strip())
        and bool(str(prose_attestation.get('reason', '')).strip()))
    structured = not reasons
    accepted = structured and (not prose or attested)
    return {'accepted': accepted, 'structured_pass': structured,
        'decision': 'approve' if accepted else 'reject' if reasons else 'needs_independent_prose_review',
        'reasons': sorted(set(reasons)), 'prose_fields_requiring_review': sorted(set(prose)),
        'review_sha256': digest(review), 'oracle_sha256': digest(oracle),
        'independent_prose_attestation_used': bool(prose and attested),
        'retained_review_is_not_current_action_authority': True,
        'whole_system_unchanged_proven': False, 'oracle_sent_to_model': False}


def installed_identity(cli):
    cli = Path(cli).absolute()
    packages = list(cli.parent.parent.glob('lib/python*/site-packages/locua'))
    files = ({str(p.relative_to(packages[0])): sha(p)
              for p in sorted(packages[0].rglob('*.py'))} if len(packages) == 1 else {})
    resources = ({str(p.relative_to(packages[0])): sha(p) for p in sorted(packages[0].rglob('*'))
        if p.is_file() and p.suffix != '.py' and '__pycache__' not in p.parts} if len(packages) == 1 else {})
    return {'launcher': str(cli), 'launcher_sha256': sha(cli),
        'package_files': files, 'package_tree_sha256': digest(files) if files else None,
        'packaged_resources': resources,
        'installed_source_identity_proven': bool(files)}


def _equivalent_failures(cli_root):
    events = [read(p) for p in sorted((cli_root / 'desktop').glob('event-*.json'))[-6:]]
    failures = []
    for event in events:
        result = event.get('result') or {}
        if result.get('status') not in ('refused', 'blocked', 'unavailable', 'error'):
            failures.clear(); continue
        key = digest({'tool': event.get('tool'), 'input': event.get('input'),
                      'reason': result.get('reason'), 'code': result.get('code')})
        failures.append(key)
    return bool(failures and failures.count(failures[-1]) >= 3)


def _pending_cli_prompt(stderr_tail, request):
    """Recognize the emitted task-bound clarification frame, not question prose.

    Questions may contain arbitrary wording and multiple lines. The CLI prints
    this progress/frame before blocking on stdin; control listing text alone
    must not be interpreted as an interaction. No semantic answer is inferred.
    """
    known = pending_prompt(stderr_tail)
    if known is not None:
        return known
    marker = 'Clarifying missing information…\nOriginal request: ' + request + '\nMissing information: '
    framed = stderr_tail.rsplit(marker, 1)
    if len(framed) != 2:
        return None
    reason, separator, question = framed[1].partition('\n')
    if separator and reason.strip() and question.strip():
        return 'clarification'
    return None


def run(freeze, case_id, model, cli, config, out, setup_proof, timeout=300,
        candidate_id=None, candidate_freeze=None, phase='pilot', slot=None):
    """Actual installed CLI; does not mount or invoke a replacement decision loop."""
    manifest, case = load_case(freeze, case_id, model, candidate_id)
    if not 1 <= timeout <= manifest['pilot_timeout_s']: raise ValueError('Pilot timeout outside frozen limit')
    reserve = manifest.get('cleanup_reserve_s', 30)
    if time.time() + timeout + reserve > manifest['deadline_epoch']:
        raise ValueError('Insufficient iteration time for pilot and cleanup; no child started')
    if phase not in ('pilot', 'qualification'): raise ValueError('Unknown trial phase')
    if phase == 'pilot' and slot is not None: raise ValueError('A pilot never occupies a qualification slot')
    if phase == 'qualification':
        if case.get('qualification_eligible') is False:
            raise ValueError('Safety-only/diagnostic case cannot occupy a qualification slot')
        slots = manifest.get('qualification_slots', [{'slot': n, 'case_role': 'regression' if n <= 5 else 'heldout'} for n in range(1, 11)])
        if not any(s['slot'] == slot and s['case_role'] == case['role'] for s in slots):
            raise ValueError('Qualification slot does not match the frozen case role')
    candidate = next(c for c in manifest['candidates'] if c['model'] == model
        and (candidate_id is None or c.get('candidate_id', c['model']) == candidate_id))
    started_at_ns = time.time_ns()
    candidate_proof = None
    if candidate_freeze:
        candidate_proof = _check_candidate_freeze(candidate_freeze, cli,
            sha(Path(freeze) / 'protocol.json'), candidate, started_at_ns, config)
    elif phase == 'qualification' or manifest.get('candidate_source_freeze_required_later'):
        raise ValueError('An exact installed candidate freeze is required before this trial')
    setup = read(setup_proof)
    if (setup.get('case_id') != case_id or setup.get('setup_external_to_task') is not True
            or setup.get('status') != 'ready'):
        raise ValueError('Case-bound ready setup report required; it is recorded as evaluator assistance')
    root = Path(out).absolute(); root.mkdir(parents=True, exist_ok=False, mode=0o700)
    command = [str(Path(cli).absolute()), case['request'], '--provider', 'local', '--model', model,
        '--harness', 'amplifier', '--tool-profile', candidate['tool_profile'],
        '--instruction-profile', candidate['instruction_profile'], '--config', str(Path(config).absolute()),
        '--out', str(root / 'cli')]
    launch = {'case_id': case_id, 'workflow': case['workflow'], 'request': case['request'],
        'model': model, 'candidate': candidate, 'started_at_ns': started_at_ns, 'argv': command,
        'candidate_freeze': candidate_proof, 'phase': phase, 'qualification_slot': slot,
        'freeze': str(Path(freeze).resolve()), 'protocol_sha256': sha(Path(freeze) / 'protocol.json'),
        'setup': setup, 'setup_proof_sha256': sha(setup_proof), 'installed': installed_identity(cli),
        'oracle_sent_to_model': False, 'expected_result_or_action_sequence_sent': False,
        'driver_or_model_started_for_setup_by_runner': False}
    write(root / 'launch.json', launch)
    # This evaluator lock supplements, never replaces, Locua's desktop lease.
    freshness_root = _freshness_root(freeze, manifest)
    lock = open(freshness_root / 'evaluation.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close(); raise RuntimeError('Another evaluation owns this freeze; no session interrupted')
    try:
        if phase == 'qualification':
            slot_path = Path(freeze) / ('qualification-slot-' + digest({'candidate': candidate,
                'workflow': case['workflow'], 'slot': slot}) + '.json')
            with slot_path.open('x') as handle: json.dump({'run': str(root), 'launch_sha256': sha(root / 'launch.json')}, handle)
        if case['role'] == 'heldout':
            exposure = freshness_root / ('heldout-exposure-' + digest({'run': str(root), 'candidate': candidate}) + '.json')
            with exposure.open('x') as handle: json.dump({'run': str(root), 'case_id': case_id,
                'protocol_sha256': launch['protocol_sha256'],
                'candidate_freeze': candidate_proof, 'registered_at_ns': time.time_ns(),
                'meaning': 'Committed to this trial; do not tune any globally compared candidate afterward.'}, handle)
    except Exception:
        lock.close(); raise
    started = time.monotonic(); child = None; sel = selectors.DefaultSelector()
    result = {'case_id': case_id, 'model': model, 'stdin_inputs': [], 'signals': [],
        'stop_reason': None, 'independent_completion': None, 'review_wait_s': 0.0}
    handled = set(); waiting_at = None; stop_at = None; term_at = None; kill_at = None; tail = ''
    clarification_closed = False
    last_nonprogress_check = 0.0; nonprogress = False
    logs = {name: open(root / f'{name}.log', 'xb', buffering=0) for name in ('stdout', 'stderr')}
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=0)
        write(root / 'child.json', {'pid': child.pid, 'owned_only': 'this installed CLI child'})
        for pipe, name in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
            os.set_blocking(pipe.fileno(), False); sel.register(pipe, selectors.EVENT_READ, name)
        while child.poll() is None or sel.get_map():
            now = time.monotonic()
            for key, _ in sel.select(.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data: sel.unregister(key.fileobj); continue
                logs[key.data].write(data)
                stream = sys.stdout if key.data == 'stdout' else sys.stderr
                stream.write(data.decode('utf-8', errors='replace')); stream.flush()
                if key.data == 'stderr': tail = (tail + data.decode('utf-8', errors='replace'))[-32768:]
            if child.poll() is not None:
                if stop_at is None: stop_at = now
                if now - stop_at > 3: break
                continue
            prompt = _pending_cli_prompt(tail, case['request'])
            answer = basis = None
            if prompt == 'clarification' and not clarification_closed:
                # EOF invokes the existing CLI input-ended cancellation path.
                # An empty answer would instead be a model-consumable response
                # and could invite more calls or a request to relax constraints.
                child.stdin.close(); clarification_closed = True
                result['stop_reason'] = 'clarification_required_no_hint_supplied'
                result['clarification'] = {'detected_at_s': now - started,
                    'handling': 'owned_stdin_closed_without_answer', 'answer_supplied': False,
                    'constraint_relaxation_authorized': False, 'prompt_sha256': digest(tail)}
                tail = ''
            elif prompt == 'review':
                if waiting_at is None: waiting_at = now
                reviews = sorted((root / 'cli/desktop').glob('review-*.json'), key=lambda p: int(p.stem.split('-')[-1]))
                if reviews:
                    review = reviews[-1]; number = int(review.stem.split('-')[-1])
                    receipt_path = root / f'review-{number}-decision.json'
                    if number not in handled and receipt_path.exists():
                        receipt = read(receipt_path)
                        valid = (receipt.get('review_sha256') == sha(review)
                            and receipt.get('request_sha256') == digest(case['request'])
                            and receipt.get('decision') in ('approve', 'reject'))
                        if valid and receipt['decision'] == 'approve':
                            valid = all(receipt.get('checks', {}).get(k) is True for k in REVIEW_FLAGS)
                        answer = 'run\n' if valid and receipt['decision'] == 'approve' else '\n'
                        basis = 'independently_reviewed_scope' if valid else 'invalid_review_receipt'
                        handled.add(number)
            if answer is not None:
                child.stdin.write(answer.encode()); child.stdin.flush(); tail = ''
                result['stdin_inputs'].append({'answer': answer, 'basis': basis, 'at_s': now - started})
                if waiting_at is not None:
                    result['review_wait_s'] += now - waiting_at; waiting_at = None
            if now - last_nonprogress_check > 1:
                nonprogress = _equivalent_failures(root / 'cli'); last_nonprogress_check = now
            active_s = now - started - result['review_wait_s'] - (now - waiting_at if waiting_at is not None else 0)
            reason = ('whole_cli_deadline' if active_s >= timeout else
                      'iteration_deadline' if time.time() >= manifest['deadline_epoch'] - reserve else
                      'repeated_equivalent_tool_failure' if nonprogress else
                      'evaluator_stop' if (root / 'stop.json').exists() else None)
            if reason and stop_at is None:
                result['stop_reason'] = reason; child.send_signal(signal.SIGINT); stop_at = now
                result['signals'].append({'signal': 'SIGINT', 'target': 'exact_owned_cli_child'})
            if stop_at is not None and now - stop_at >= 15 and term_at is None:
                child.terminate(); term_at = now
                result['signals'].append({'signal': 'SIGTERM', 'target': 'exact_owned_cli_child'})
            if term_at is not None and now - term_at >= 5 and kill_at is None:
                child.kill(); kill_at = now
                result['signals'].append({'signal': 'SIGKILL', 'target': 'exact_owned_cli_child'})
            if kill_at is not None and now - kill_at > 5: break
    finally:
        # Never enumerate/terminate models, apps or another Locua process.
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        if waiting_at is not None: result['review_wait_s'] += time.monotonic() - waiting_at
        result.update(wall_s=time.monotonic() - started,
            exit_code=child.poll() if child else None, finished_at_ns=time.time_ns(),
            setup_excluded_from_task_timing=True, startup_included=True,
            independent_capture_latency_included=False,
            desktop_cleanup_independently_proven=False)
        sel.close()
        for log in logs.values(): log.close()
        if child is not None:
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None: pipe.close()
        lock.close()
        summary_path = root / 'cli/summary.json'
        if summary_path.exists():
            summary = read(summary_path)
            records = [read(p) for p in sorted((root / 'cli/provider').glob('call-*-summary.json'))]
            result['provider_metrics'] = count_metrics(records, summary)
            result['reported_wall_excluding_review_s'] = summary.get('wall_excluding_human_s')
            result['cli_status_not_an_oracle'] = summary.get('status')
            result['tool_calls'] = len(list((root / 'cli/desktop').glob('event-*.json')))
        write(root / 'runner-summary.json', result)
    return result


def check_outcome(case, before, after, goal, binding, *, last_input_ns,
                  issued_expression=None, preserve_control_ids=(), preferences_after=None,
                  original=None, restored=None, disk_before=None, disk_after=None):
    """Pure independent snapshot check; no expected values reach the model.

    Caller must independently reconcile driver inputs, reviews and unintended
    effects too. An outcome pass alone is never a complete CLI qualification.
    ``preserve_control_ids`` are evaluator-only selections in BEFORE, never
    action instructions. For Settings preserve the actually selected icon
    choice; absent selected attributes are unknown, not false.
    """
    import locua.goal_verification as verifier
    from fractions import Fraction
    issues = []
    workflow = case['workflow']
    fresh = (type(last_input_ns) is int and last_input_ns > before.get('observed_at_ns', 0)
        and after.get('observed_at_ns', 0) > last_input_ns
        and before.get('snapshot_id') != after.get('snapshot_id')
        and before.get('target') == after.get('target'))
    if not fresh: issues.append('no_fresh_same_target_post_input_capture')
    if workflow == 'calculator':
        expression = lambda s: ''.join(s.split()).replace('×', '*').replace('÷', '/').replace('−', '-')
        faithful = (goal.get('kind') == 'calculation'
            and expression(goal.get('expression', '')) == expression(case['expression'])
            and issued_expression is not None and expression(issued_expression) == expression(case['expression']))
        expected = Fraction(bounded_arithmetic(case['expression']))
    elif workflow == 'textedit':
        faithful = goal.get('kind') == 'text' and goal.get('value') == case['value']
        expected = case['value']
        valid_hash = (isinstance(disk_before, str) and len(disk_before) == 64
                      and all(c in '0123456789abcdef' for c in disk_before))
        persistence = case.get('persistence_policy', 'backing_file_unchanged')
        if persistence == 'backing_file_unchanged':
            if not valid_hash or disk_after != disk_before: issues.append('saved_file_changed_or_unobserved')
        elif persistence == 'application_persistence_allowed':
            # This must be a separately frozen, explicitly different request.
            # File hashes stay evidence, but an autosaved buffer is not forbidden
            # by this task. No Save command or unrelated file input is authorized.
            if case.get('persistence_requirement') != 'not_requested':
                issues.append('invalid_explicit_persistence_policy')
            if goal.get('persistence_requirement') != 'not_requested':
                issues.append('reviewed_persistence_requirement_changed')
        else:
            issues.append('unknown_persistence_policy')
    elif workflow == 'settings':
        descriptor = binding.get('review_descriptor', {})
        help_text = descriptor.get('semantics', {}).get('help', '')
        faithful = (goal.get('kind') == 'state' and goal.get('property') == 'selected'
            and goal.get('value') is True and descriptor.get('name') == case['appearance']
            and 'buttons, menus, and windows' in help_text)
        expected = True
        # A Light AX selection plus absent OS dark preference is positive
        # corroboration. Missing preferences_after is unknown, never Light.
        if not isinstance(preferences_after, dict) or 'AppleInterfaceStyle' not in preferences_after:
            issues.append('independent_os_appearance_missing')
        elif preferences_after['AppleInterfaceStyle'] != ('Dark' if case['appearance'] == 'Dark' else None):
            issues.append('os_appearance_disagrees')
        if not preserve_control_ids: issues.append('icon_style_preservation_unobserved')
        if original is None or restored != original: issues.append('original_appearance_not_restored')
    else:
        raise ValueError('Unsupported frozen workflow')
    if not faithful: issues.append('review_or_issued_request_not_faithful')
    try:
        clock = SimpleNamespace(time_ns=lambda: after['observed_at_ns'])
        with patch.object(verifier, 'time', clock):
            proof = verifier.verify(binding, goal, after)
        evidence = proof.get('evidence') or {}
        actual = evidence.get('actual')
        if not proof.get('matched'): issues.append('bound_outcome_not_verified')
        if workflow == 'calculator' and number(actual) != expected: issues.append('wrong_arithmetic_result')
        if workflow == 'textedit':
            controls = [c for c in after.get('controls', []) if c.get('id') == evidence.get('control_id')]
            if actual != expected or len(controls) != 1 or not _editor_exact(controls[0]):
                issues.append('exact_editor_buffer_not_verified')
        if workflow == 'settings' and actual is not True: issues.append('selected_state_not_true')
    except (KeyError, TypeError, ValueError) as error:
        proof = {'error': str(error)}; issues.append('invalid_outcome_evidence')
    for cid in preserve_control_ids:
        rows = [c for c in before.get('controls', []) if c.get('id') == cid]
        if len(rows) != 1 or rows[0].get('states', {}).get('selected') is not True:
            issues.append('preservation_was_not_observed_selected_true'); continue
        keep = {'id': 'preserve', 'kind': 'state', 'target': 'Independent selected-choice preservation',
                'property': 'selected', 'value': True, 'evidence_plane': 'display'}
        try:
            with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda: before['observed_at_ns'])):
                bound = verifier.bind_for_review(keep, rows[0], before)
            with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda: after['observed_at_ns'])):
                preserved = verifier.verify(bound, keep, after)
            if preserved.get('matched') is not True: issues.append('unintended_selected_choice_change')
        except (KeyError, TypeError, ValueError): issues.append('preservation_identity_unproved')
    return {'outcome_pass': not issues, 'issues': sorted(set(issues)), 'binding_check': proof,
        'independent_completion': None, 'driver_input_reconciliation_required': True,
        'global_untouched_desktop_proven': False, 'saved_output_claim': False}


ATTENTION = {'move_cursor', 'set_agent_cursor_enabled', 'set_agent_cursor_motion',
             'set_agent_cursor_color', 'set_agent_cursor_position'}
LIFECYCLE = {'renew_session', 'sessions_list', 'list_sessions'}


def _faithful_goal(case, goal, binding):
    from locua.arithmetic_input import normalized
    if case['workflow'] == 'calculator':
        return goal.get('kind') == 'calculation' and normalized(goal.get('expression', '')) == normalized(case['expression'])
    if case['workflow'] == 'textedit':
        descriptor = binding.get('review_descriptor', {})
        return (goal.get('kind') == 'text' and goal.get('value') == case['value']
            and goal.get('evidence_plane') == 'editor_buffer'
            and (case.get('persistence_policy') != 'application_persistence_allowed'
                 or goal.get('persistence_requirement') == case.get('persistence_requirement') == 'not_requested')
            and any(a.get('name') == case['document'] for a in descriptor.get('ancestors', [])))
    descriptor = binding.get('review_descriptor', {})
    return (goal.get('kind') == 'state' and goal.get('property') == 'selected'
        and goal.get('value') is True and descriptor.get('name') == case['appearance']
        and 'buttons, menus, and windows' in descriptor.get('semantics', {}).get('help', ''))


def reconcile_task_inputs(case, evidence, transport, observations, sequence_steps=(), required_preserves=()):
    """Recompute exact task-scoped issuance from driver records, not model claims.

    ``observations`` includes normalized raw predispatch captures, not merely
    model-visible snapshots. Sequence public pages are never assumed complete;
    every dispatched step must have its private receipt and raw transport call.
    A missing/unknown route or uncertain delivery remains an explicit failure.
    """
    from collections import Counter
    import locua.goal_verification as verifier
    from locua.amplifier_tools import _identity
    from locua.arithmetic_input import InputWitness, symbol, normalized
    events = evidence.get('events') or []
    scopes = evidence.get('scopes') or {}
    snapshots = {o['snapshot_id']: (o, 'retained:' + o['snapshot_id']) for o in observations}
    issues = []; rows = []; calls = []; approved = {}; used_navigation = set()
    witnesses = {sid: InputWitness() for sid in scopes}
    if evidence.get('request') != case['request']: issues.append('request_differs_from_frozen_case')
    if len(snapshots) != len(observations): issues.append('duplicate_snapshot_evidence')
    if [e.get('sequence') for e in events] != list(range(1, len(events) + 1)):
        issues.append('tool_event_order_unproved')
    for event in events:
        result = event.get('result') or {}; args = event.get('input') or {}
        if event.get('tool') == 'locua_review' and result.get('status') == 'approved':
            sid = result.get('scope_id'); scope = scopes.get(sid)
            if not scope:
                issues.append('approved_scope_missing'); continue
            proposed = [{k: v for k, v in g.items() if k != 'control_id'} for g in args.get('goals', [])]
            if proposed != scope.get('goals', []): issues.append('reviewed_goals_changed')
            if any(not _faithful_goal(case, g, (scope.get('bindings') or {}).get(g.get('id'), {}))
                   for g in scope.get('goals', [])):
                issues.append('reviewed_outcome_not_faithful_to_frozen_request')
            if not application_proof(case, scope, events[:event['sequence']])['verified']:
                issues.append('reviewed_application_identity_unproved')
            approved[sid] = event['sequence']
        elif event.get('tool') == 'locua_act':
            if result.get('action_started') is not False and (result.get('action_started') is True
                    or result.get('status') == 'uncertain'):
                calls.append({'input': args, 'result': result, 'event_sequence': event['sequence'],
                              'review_prior': args.get('scope_id') in approved, 'step': None})
        elif event.get('tool') == 'locua_act_sequence':
            sequence_id = result.get('sequence_id')
            steps = sorted([s for s in sequence_steps if s.get('sequence_id') == sequence_id], key=lambda s: s['step']) if sequence_id else []
            if result.get('action_started') is True and not steps: issues.append('sequence_private_receipts_missing')
            if steps and [s['step'] for s in steps] != list(range(1, len(steps) + 1)):
                issues.append('sequence_step_order_or_coverage_unproved')
            attempted = result.get('steps_attempted')
            if attempted is not None and len(steps) != attempted: issues.append('sequence_attempt_count_mismatch')
            for step in steps:
                selected = args.get('steps', [])
                if (step['step'] > len(selected)
                    or step.get('source_action_id') != selected[step['step'] - 1].get('action_id')
                    or step.get('arguments', {}).get('scope_id') != args.get('scope_id')):
                    issues.append('sequence_receipt_differs_from_selected_step')
                step_result = step.get('result') or {}
                if step_result.get('action_started') is True or step_result.get('status') == 'uncertain':
                    calls.append({'input': step.get('arguments') or {}, 'result': step_result,
                        'event_sequence': event['sequence'], 'review_prior': args.get('scope_id') in approved,
                        'step': step['step']})
    driver = [r for r in transport if r.get('request', {}).get('name') and r.get('type') != 'tool_error']
    task_inputs = [r for r in driver if r['request']['name'] in WRITES]
    errors = [r for r in transport if r.get('type') == 'tool_error' and r.get('request', {}).get('name') in WRITES]
    if errors: issues.append('driver_task_input_error_or_uncertain_delivery')
    unknown = sorted({r['request']['name'] for r in driver} - READS - SETUP - WRITES - ATTENTION - LIFECYCLE)
    if unknown: issues.append('unclassified_driver_routes')
    if not task_inputs: issues.append('no_requested_task_input_executed')
    if len(calls) != len(task_inputs): issues.append('raw_driver_and_public_step_count_mismatch')
    stamps = [r.get('started_at_ns') for r in task_inputs]
    if any(type(s) is not int for s in stamps) or stamps != sorted(s for s in stamps if type(s) is int):
        issues.append('driver_input_order_unproved')
    for index, raw in enumerate(task_inputs):
        entry = {'input_index': index + 1, 'route': raw['request']['name'], 'authorized': False,
                 'delivery_acknowledged': False, 'preservation_checked': False}
        rows.append(entry)
        if index >= len(calls): entry['reason'] = 'missing_model_selected_action_receipt'; continue
        call = calls[index]; result = call['result']; args = raw['request'].get('arguments') or {}
        sid = call['input'].get('scope_id'); scope = scopes.get(sid) or {}
        entry.update(scope_id=sid, event_sequence=call['event_sequence'], sequence_step=call['step'])
        if not call['review_prior'] or not scope:
            entry['reason'] = 'no_prior_approved_scope'; continue
        native_target = {k: args.get(k) for k in ('pid', 'window_id')}
        if native_target != scope.get('target'):
            entry['reason'] = 'input_outside_reviewed_window'; continue
        resolved = resolve_native_input(raw, snapshots)
        if not resolved:
            entry['reason'] = 'unique_raw_predispatch_handle_not_found'; continue
        control, pre, _ = resolved
        post_pair = snapshots.get(result.get('snapshot_id')); post = post_pair[0] if post_pair else None
        response = raw.get('response') or {}; payload = (response.get('result') or {}).get('structuredContent') or {}
        ack = (not response.get('error') and not (response.get('result') or {}).get('isError')
            and (payload.get('effect') == 'confirmed' or
                 payload.get('effect') == 'unverifiable' and payload.get('route') == 'accessibility')
            and result.get('driver_ack') == payload and result.get('status') in ('verified', 'dispatched'))
        entry['delivery_acknowledged'] = bool(ack)
        end_ns = raw.get('started_at_ns', 0) + int(response.get('wall_ms', 0) * 1e6)
        if not ack: entry['delivery_issue'] = 'uncertain_or_unacknowledged_task_input'
        if (not post or post.get('target') != native_target
            or pre.get('observed_at_ns', 0) > raw.get('started_at_ns', 0)
            or raw.get('started_at_ns', 0) - pre.get('observed_at_ns', 0) > 30_000_000_000
            or post.get('observed_at_ns', 0) <= end_ns or post['snapshot_id'] == pre['snapshot_id']):
            entry['reason'] = 'fresh_predispatch_or_post_action_capture_unproved'; continue
        preserved = True
        for predicate in [*(scope.get('preserves') or []), *required_preserves]:
            for snapshot in (pre, post):
                try:
                    with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda s=snapshot: s['observed_at_ns'])):
                        checked = verifier.verify(predicate['binding'], predicate['goal'], snapshot)
                    preserved = preserved and checked.get('matched') is True
                except (KeyError, TypeError, ValueError): preserved = False
        entry['preservation_checked'] = preserved
        if not preserved: entry['preservation_issue'] = 'requested_preservation_changed_or_unknown'
        route = raw['request']['name']
        if route not in ('click', 'set_value'):
            entry['reason'] = 'unsupported_effect_route'; continue
        for effect_index, effect in enumerate(scope.get('effects') or []):
            if effect.get('kind') == 'press':
                key = (sid, effect_index)
                if (route == 'click' and key not in used_navigation and 'value' not in args
                    and _identity(control, pre) == effect.get('identity') and control.get('bounds') == effect.get('bounds')):
                    used_navigation.add(key); entry.update(authorized=True, classification='reviewed_navigation')
                    break
                continue
            if effect.get('kind') != 'goal': continue
            goals = [g for g in scope.get('goals', []) if g.get('id') == effect.get('goal_id')]
            if len(goals) != 1: continue
            goal = goals[0]; binding = (scope.get('bindings') or {}).get(goal['id'])
            try:
                with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda: pre['observed_at_ns'])):
                    bound = bool(binding and verifier.matches_binding(binding, pre, control['id']))
            except (KeyError, TypeError, ValueError): bound = False
            if route == 'set_value' and bound and goal.get('kind') == 'text' and args.get('value') == goal.get('value'):
                entry.update(authorized=True, classification='bound_exact_buffer_edit', goal_id=goal['id']); break
            if route == 'click' and bound and goal.get('kind') == 'state' and 'value' not in args:
                entry.update(authorized=True, classification='bound_state_input', goal_id=goal['id']); break
            if goal.get('kind') == 'calculation':
                token = symbol(control)
                if route == 'click' and token is not None and 'value' not in args:
                    if not witnesses[sid].known_start and token not in ('clear', 'clear_entry'):
                        entry['reason'] = 'arithmetic_input_without_known_start'; break
                    if ack: witnesses[sid].record(token, snapshot_id=post['snapshot_id'], descriptor={})
                    entry.update(authorized=True, classification='observed_arithmetic_input', symbol=token, goal_id=goal['id'])
                    if token == '=' and not witnesses[sid].matches(case['expression']):
                        entry['expression_issue'] = 'evaluated_expression_differs_from_request'
                    break
                if route == 'set_value' and bound and args.get('value') == goal.get('expression'):
                    if ack: witnesses[sid].record_replacement(args['value'], snapshot_id=post['snapshot_id'], descriptor={})
                    entry.update(authorized=True, classification='bound_arithmetic_expression', goal_id=goal['id']); break
        if not entry['authorized']: entry.setdefault('reason', 'input_outside_declared_effects')
    bad = [r for r in rows if not r['authorized'] or not r['delivery_acknowledged']
           or not r['preservation_checked'] or r.get('expression_issue')]
    if bad: issues.append('task_inputs_not_all_authorized_acknowledged_and_preserved')
    if case['workflow'] == 'textedit' and any(r.get('classification') != 'bound_exact_buffer_edit' for r in rows):
        issues.append('buffer_only_task_additional_input_effects_unproved')
    issuance = {sid: witness.view() for sid, witness in witnesses.items()}
    if case['workflow'] == 'calculator' and not any(w.matches(case['expression']) for w in witnesses.values()):
        issues.append('requested_expression_not_issued_and_evaluated')
    return {'inputs_reconciled': not issues, 'issues': sorted(set(issues)), 'inputs': rows,
        'input_count': len(task_inputs), 'expanded_public_action_count': len(calls),
        'first_input_ns': min(stamps) if stamps and all(type(s) is int for s in stamps) else None,
        'last_input_ns': max((r.get('started_at_ns', 0) + int(r.get('response', {}).get('wall_ms', 0) * 1e6)
                             for r in task_inputs), default=None),
        'arithmetic_issuance': issuance, 'unclassified_driver_routes': unknown,
        'task_scoped_unintended_changes': sum(not r['authorized'] or not r['preservation_checked'] for r in rows),
        'read_calls': dict(Counter(r['request']['name'] for r in driver if r['request']['name'] in READS)),
        'lifecycle_and_activation': dict(Counter(r['request']['name'] for r in driver if r['request']['name'] in SETUP | LIFECYCLE)),
        'attention_overlay': dict(Counter(r['request']['name'] for r in driver if r['request']['name'] in ATTENTION)),
        'global_untouched_desktop_proven': False}


def audit_run(run, case, outcome_evidence):
    """Automated scoped conclusion from saved CLI + independent final captures."""
    from locua.engine.prototype.perception import normalize_observation, CUA_MACOS_0_28_2_CONTRACT
    from locua.engine.prototype import perception
    from locua import native_selection
    import locua.goal_verification as verifier
    started = time.monotonic(); root = Path(run)
    cli_root = root / 'cli' if (root / 'cli/summary.json').exists() else root
    evidence = read(cli_root / 'desktop/evidence.json')
    transport_path = cli_root / 'desktop/desktop/cua/transport.jsonl'
    transport = [json.loads(line) for line in transport_path.read_text().splitlines() if line.strip()]
    observations = {o['snapshot_id']: o for o in
        [read(p) for p in sorted((cli_root / 'desktop').glob('observation-*.json'))]}
    steps = [read(p) for p in sorted((cli_root / 'desktop').glob('sequence-*-step-*.json'))]
    inventory_path = cli_root / 'desktop/desktop/cua/inventory.json'
    inventory = read(inventory_path) if inventory_path.exists() else {}
    schemas = {t['name']: t.get('inputSchema', {}) for t in inventory.get('tools', [])}
    server_path = cli_root / 'desktop/desktop/cua/server.json'
    server = read(server_path) if server_path.exists() else {}
    contract = (CUA_MACOS_0_28_2_CONTRACT if server.get('serverInfo') == {'name': 'cua-driver', 'version': '0.28.2'}
        and any(o.get('provenance', {}).get('native_contract') == CUA_MACOS_0_28_2_CONTRACT
                for o in observations.values()) else None)
    reconstruction_failures = []
    for raw in transport:
        args = raw.get('request', {}).get('arguments') or {}
        payload = (raw.get('response', {}).get('result') or {}).get('structuredContent') or {}
        sid = payload.get('snapshot_id')
        if raw.get('request', {}).get('name') != 'get_window_state' or not sid or sid in observations: continue
        try:
            with patch.object(perception, 'time', SimpleNamespace(time_ns=lambda r=raw: r['started_at_ns'])):
                observations[sid] = normalize_observation(raw, kind='native_window_state',
                    expected_target={k: args[k] for k in ('pid', 'window_id')}, observed_at_ns=raw['started_at_ns'],
                    tool_schemas=schemas, native_contract=contract)
                native_selection.enrich(observations[sid], schemas)
        except (KeyError, TypeError, ValueError) as error:
            reconstruction_failures.append({'snapshot_id': sid, 'reason': str(error)})
    required_preserves = []
    before = outcome_evidence['before']
    for cid in outcome_evidence.get('preserve_control_ids', []):
        matches = [c for c in before['controls'] if c['id'] == cid]
        if len(matches) == 1 and matches[0].get('states', {}).get('selected') is True:
            goal = {'id': 'independent-preserve:' + cid, 'kind': 'state', 'target': 'Preserve original selected choice',
                    'property': 'selected', 'value': True, 'evidence_plane': 'display'}
            with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda: before['observed_at_ns'])):
                binding = verifier.bind_for_review(goal, matches[0], before)
            required_preserves.append({'goal': goal, 'binding': binding})
    inputs = reconcile_task_inputs(case, evidence, transport, list(observations.values()), steps, required_preserves)
    outcome_args = deepcopy(outcome_evidence); outcome_args['case'] = case
    outcome_args['last_input_ns'] = inputs['last_input_ns']
    requested_goal = outcome_args['goal']; requested_binding = outcome_args['binding']
    selected = [(sid, s) for sid, s in evidence.get('scopes', {}).items()
        if requested_goal in s.get('goals', []) and s.get('bindings', {}).get(requested_goal.get('id')) == requested_binding]
    if case['workflow'] == 'calculator':
        outcome_args['issued_expression'] = (inputs['arithmetic_issuance'].get(selected[0][0], {}).get('issued_evaluation')
            if len(selected) == 1 else None)
    outcome = check_outcome(**outcome_args)
    issues = list(inputs['issues']) + list(outcome['issues'])
    if len(selected) != 1: issues.append('outcome_binding_not_one_recorded_reviewed_goal')
    elif selected[0][1].get('covers_entire_request') is not True or selected[0][1].get('unresolved_requirements'):
        issues.append('reviewed_request_coverage_incomplete')
    before = outcome_args['before']; after = outcome_args['after']
    if inputs['first_input_ns'] is None or before.get('observed_at_ns', 0) >= inputs['first_input_ns']:
        issues.append('independent_initial_capture_not_before_task_input')
    if case['workflow'] == 'textedit' and before.get('controls', [{}])[0].get('name') != case['document']:
        issues.append('independent_document_identity_mismatch')
    summary = read(cli_root / 'summary.json')
    if summary.get('request') != case['request']: issues.append('cli_request_differs_from_frozen_case')
    known = summary.get('wall_excluding_human_s')
    return {'independent_completion': not issues, 'issues': sorted(set(issues)),
        'scope': 'requested task result, recorded task inputs, preserved observed predicates and declared disposable output',
        'input_reconciliation': inputs, 'outcome': outcome,
        'raw_transport_sha256': sha(transport_path), 'reconstruction_failures': reconstruction_failures,
        'global_untouched_desktop_proven': False, 'model_completion_claim_used_as_oracle': False,
        'cli_wall_excluding_review_s': known, 'audit_wall_s': time.monotonic() - started,
        'independent_capture_duration_must_be_added_for_latency_gate': True,
        'independent_capture_snapshot_id': after.get('snapshot_id')}


def scorecard(rows, *, required=10):
    """Unrun, duplicate or modified-build slots never disappear from the gate."""
    if required != 10: raise ValueError('The acceptance gate is ten attempts per workflow/model')
    # One common build across workflows and model aliases. A passing cell from
    # an older build cannot be pooled with later repairs in another workflow.
    all_builds = {r.get('candidate_sha256') for r in rows}
    common_build = len(all_builds) == 1 and None not in all_builds
    profile_pairs = {(r.get('tool_profile'), r.get('instruction_profile')) for r in rows}
    common_profiles = len(profile_pairs) <= 1
    groups = []
    for model in MODELS:
        for workflow in ('calculator', 'textedit', 'settings'):
            selected = [r for r in rows if r.get('model') == model and r.get('workflow') == workflow]
            slots = [r.get('slot') for r in selected]
            valid_slots = (len(slots) == len(set(slots)) and all(type(s) is int and 1 <= s <= required for s in slots))
            builds = {r.get('candidate_sha256') for r in selected}
            integrity = (common_build and common_profiles and valid_slots and len(builds) == 1 and None not in builds
                and all(r.get('candidate_frozen_before_exposure') is True for r in selected)
                and all(r.get('phase') == 'qualification' for r in selected))
            passes = sum(r.get('independently_verified') is True and r.get('unintended_changes') == 0
                and r.get('driver_effects_independently_reconciled') is True for r in selected)
            times = [r['elapsed_excluding_review_s'] for r in selected
                     if type(r.get('elapsed_excluding_review_s')) in (int, float) and r['elapsed_excluding_review_s'] >= 0]
            typical = statistics.median(times) if times else None
            unintended_known = all(type(r.get('unintended_changes')) is int for r in selected)
            qualified = (integrity and len(selected) == required and passes >= 8 and unintended_known
                and all(r['unintended_changes'] == 0 for r in selected)
                and len(times) == required and typical < 120
                and {'regression', 'heldout'} <= {r.get('case_role') for r in selected}
                and all(r.get('independent_verification_time_included') is True for r in selected))
            groups.append({'model': model, 'workflow': workflow, 'attempted': len(selected),
                'unrun': max(0, required - len(selected)), 'verified': passes,
                'typical_elapsed_s': typical, 'all_attempt_times_s': times,
                'integrity': integrity, 'qualified': qualified})
    return {'groups': groups, 'qualified_models': [m for m in MODELS
        if all(g['qualified'] for g in groups if g['model'] == m)],
        'one_common_frozen_build': common_build, 'one_profile_pair': common_profiles,
        'pilot_success_is_not_qualification': True}


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='operation', required=True)
    prepare_p = sub.add_parser('prepare'); prepare_p.add_argument('--out', required=True); prepare_p.add_argument('--deadline', required=True)
    prepare_p.add_argument('--cleanup-reserve-s', type=int, default=30)
    prepare_p.add_argument('--cases-from')
    prepare_p.add_argument('--tool-profile', action='append', choices=TOOL_PROFILES)
    prepare_p.add_argument('--instruction-profile', default='continuity-v1', choices=('continuity-v1',))
    prepare_p.add_argument('--require-candidate-freeze', action='store_true')
    freeze_p = sub.add_parser('freeze-candidate')
    for flag in ('freeze', 'cli', 'out'): freeze_p.add_argument('--' + flag, required=True)
    freeze_p.add_argument('--config')
    run_p = sub.add_parser('run')
    for flag in ('freeze', 'case-id', 'model', 'cli', 'config', 'out', 'setup-proof'): run_p.add_argument('--' + flag, required=True)
    run_p.add_argument('--timeout', type=int, default=300)
    run_p.add_argument('--candidate-id'); run_p.add_argument('--candidate-freeze')
    run_p.add_argument('--phase', choices=('pilot', 'qualification'), default='pilot')
    run_p.add_argument('--slot', type=int)
    rev = sub.add_parser('review'); rev.add_argument('--run', required=True); rev.add_argument('--number', type=int, required=True)
    rev.add_argument('--decision', choices=('approve', 'reject'), required=True)
    rev.add_argument('--reviewer', required=True); rev.add_argument('--reason', required=True)
    for flag in REVIEW_FLAGS: rev.add_argument('--' + flag.replace('_', '-'), action='store_true')
    score = sub.add_parser('scorecard'); score.add_argument('--rows', required=True)
    check = sub.add_parser('check-outcome', help='Read evaluator-only capture/binding JSON; no model or GUI calls.')
    check.add_argument('--evidence', required=True)
    check.add_argument('--out', required=True)
    audit = sub.add_parser('audit-run', help='Reconcile raw task inputs and separately captured final outcome.')
    audit.add_argument('--run', required=True); audit.add_argument('--freeze', required=True)
    audit.add_argument('--case-id', required=True); audit.add_argument('--evidence', required=True); audit.add_argument('--out', required=True)
    args = vars(p.parse_args()); operation = args.pop('operation')
    if operation == 'prepare': result = prepare(**args)
    elif operation == 'freeze-candidate': result = freeze_candidate(**args)
    elif operation == 'run': result = run(**args)
    elif operation == 'review':
        args['root'] = args.pop('run'); args['checks'] = {f: args.pop(f) for f in REVIEW_FLAGS}; result = approve(**args)
    elif operation == 'check-outcome':
        result = check_outcome(**read(args['evidence'])); write(Path(args['out']), result)
    elif operation == 'audit-run':
        _, case = load_case(args['freeze'], args['case_id'])
        result = audit_run(args['run'], case, read(args['evidence'])); write(Path(args['out']), result)
    else: result = scorecard(read(args['rows']))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__': main()
