"""Frozen step-interface screening using the existing Amplifier evaluator.

Only synthetic desktop adapters are mounted. Oracles, review judgments and final
state audits are private. The informed one-decision screen cannot earn task
completion credit. All actual inference is explicit, local and lease-protected.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import inspect
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

import continuity_eval as base
import continuity_cli_eval as reviews
import semantic_policy_eval as sem
from locua.amplifier_contracts import TEXT_PERSISTENCE_CONTRACT, _errors
from locua.amplifier_session import CONTEXT_CONFIG, execute_session
from locua.amplifier_tools import DesktopToolset
from locua.engine.prototype.cli import private_json
from locua.instruction_policy import apply_tool_help, instruction_policy

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'step-policy-eval-v5'
PROFILES = ('semantic-v1', 'step-v1', 'step-v2')
STEP_PROFILES = frozenset(('step-v1', 'step-v2'))
REVISION_FACTORS = ['capability_reference_representation', 'persistence_contract_correction']
MODELS = base.MODELS
INSTRUCTIONS = 'continuity-v1'
START_CUTOFF = '2026-09-23T09:47:13Z'
DEADLINE = '2026-09-23T10:02:13Z'
FACTORS = ['typed_operation_and_outcome_references', 'simplified_tool_contracts',
           'focused_current_decision_context_with_fallible_model_plan', 'refreshed_tail_reminder']


def read(path): return json.loads(Path(path).read_text())
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def epoch(value): return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class StagedDesktop(sem.SemanticDesktop):
    """Fixture-owned navigation graph; no workflow or scoring oracle is read."""
    def observe(self, target):
        all_controls = self.spec['controls']; stage = self.state.get('ui_stage')
        visible = [c for c in all_controls if 'visible_stages' not in c or stage in c['visible_stages']]
        keys = {c['key'] for c in visible}
        if any(c.get('parent') is not None and c['parent'] not in keys for c in visible):
            raise ValueError('Frozen child visibility lacks its observed parent')
        self.spec['controls'] = visible
        try: return super().observe(target)
        finally: self.spec['controls'] = all_controls

    def execute(self, action, observation):
        control = next(c for c in observation['controls'] if c['id'] == action['control_id'])
        spec = next(c for c in self.spec['controls'] if c['identifier'] == control['semantics']['identifier'])
        transition = self.spec.get('transitions', {}).get(spec['key'])
        if transition is None: return super().execute(action, observation)
        if action['kind'] != 'press' or set(transition) != {'state_key', 'value'}:
            raise ValueError('Synthetic navigation requires one declared observed press transition')
        before = deepcopy(self.state); start = time.time_ns()
        self.state[transition['state_key']] = deepcopy(transition['value'])
        self.executions.append({'key': spec['key'], 'action': deepcopy(action), 'before': before,
            'after': deepcopy(self.state), 'at_ns': start, 'completed_at_ns': time.time_ns(),
            'source_snapshot_id': observation['snapshot_id'], 'source_target': deepcopy(observation['target']),
            'source_control_id': control['id'], 'source_identifier': control['semantics']['identifier'],
            'fixture_navigation': True})
        return {'status': 'dispatched', 'action_started': True, 'driver_ack': {'effect': 'unverifiable'},
                'observation': self.observe(observation['target'])['observation']}


def canonical_oracle(oracle):
    """Documented private defaults, never a mutation of frozen source bytes."""
    result = deepcopy(oracle)
    for goal in result.get('goals', []):
        goal.setdefault('evidence_plane', 'editor_buffer' if goal['kind'] == 'text' else 'display')
    return result


def _attested(review, spec, attestation):
    return (isinstance(attestation, dict) and attestation.get('review_sha256') == base.digest(review)
        and attestation.get('request_sha256') == base.digest(spec['request'])
        and attestation.get('approved') is True and attestation.get('contradictory_claims') is False
        and bool(str(attestation.get('reviewer', '')).strip()) and bool(str(attestation.get('reason', '')).strip()))


_ORIGINAL_REVIEW_GATE = reviews.review_gate


def single_digit_purpose(purpose):
    """Recognize only a complete single-control claim, never a whole plan.

    Longer text remains subject to the existing independent prose attestation;
    returning None is not approval or evidence that the prose is faithful.
    """
    match = re.fullmatch(r'\s*(?:(?:enter|press|type|input)(?:\s+(?:the\s+)?digit)?\s+|digit\s+)([0-9])(?:\s+(?:key|button))?\s*[.!]?\s*', purpose, re.I)
    return match.group(1) if match else None


def review_gate(review, spec, oracle, *, live_observation=None, prose_attestation=None):
    """Private semantic oracle; partial navigation never becomes outcome proof."""
    oracle = canonical_oracle(oracle)
    if review.get('goals') or not oracle.get('allowed_navigation_keys'):
        compared = deepcopy(review); press_reasons = []; arithmetic_purposes = False
        if any(g['kind'] == 'calculation' for g in oracle.get('goals', [])):
            from locua.arithmetic_input import symbol
            # A correct calculation goal already authorizes observed arithmetic
            # capabilities. Redundant explicit press declarations are optional;
            # inspect each claim rather than requiring an expected inventory.
            kept = []
            for effect in compared.get('effects', []):
                if effect.get('kind') != 'press': kept.append(effect); continue
                arithmetic_purposes = True
                observed = [c for c in (live_observation or {}).get('controls', []) if c.get('id') == effect.get('control_id')]
                c = observed[0] if len(observed) == 1 else {}; descriptor = effect.get('identity', {})
                token = symbol(c)
                if token is None or any(descriptor.get(k) != c.get(k) for k in ('name', 'role')) or (
                        descriptor.get('semantics', {}).get('identifier') != c.get('semantics', {}).get('identifier')):
                    press_reasons.append('unobserved_or_nonarithmetic_press_claim')
                digit = single_digit_purpose(effect.get('purpose', ''))
                if digit is not None and digit != token: press_reasons.append('explicit_digit_purpose_contradicts_observed_control')
            compared['effects'] = kept
        result = _ORIGINAL_REVIEW_GATE(compared, spec, oracle,
            live_observation=live_observation, prose_attestation=prose_attestation)
        # The base gate saw a private formal view only. Any judgment must bind
        # the original unmodified review, including every optional press claim.
        if arithmetic_purposes:
            result['review_sha256'] = base.digest(review)
            result['prose_fields_requiring_review'] = sorted(set(result['prose_fields_requiring_review'] + ['arithmetic_press_purposes']))
            attested = _attested(review, spec, prose_attestation)
            result.update(accepted=result['structured_pass'] and attested,
                decision='reject' if not result['structured_pass'] else 'approve' if attested else 'needs_independent_prose_review',
                independent_prose_attestation_used=attested)
        reasons = list(result['reasons']) + press_reasons
        expected = {g['key']: g for g in oracle.get('goals', [])}
        by_id = {c['identifier']: c['key'] for c in spec['controls']}
        for goal in review.get('goals', []):
            descriptor = review.get('observed_bindings', {}).get(goal.get('id'), {})
            key = by_id.get(descriptor.get('semantics', {}).get('identifier'))
            wanted = expected.get(key)
            if wanted and goal['kind'] == 'text' and goal.get('persistence_requirement') != wanted.get('persistence_requirement'):
                reasons.append('requested_persistence_requirement_changed')
        if reasons:
            result.update(accepted=False, structured_pass=False, decision='reject', reasons=sorted(set(reasons)))
        return result
    observation = live_observation or {}; reasons = []; effects = review.get('effects', [])
    capture = review.get('review_capture', {}); full = review.get('coverage_declaration', '').startswith('This scope covers')
    if review.get('original_request') != spec['request']: reasons.append('original_request_changed')
    if (review.get('target') != observation.get('target') or capture.get('snapshot_id') != observation.get('snapshot_id')
            or capture.get('observed_at_ns') != observation.get('observed_at_ns')
            or capture.get('fresh_capture_required_before_input') is not True):
        reasons.append('review_observation_identity_unproved')
    if full or not review.get('unresolved_requirements'): reasons.append('navigation_does_not_retain_unresolved_goal')
    by_id = {c['identifier']: c for c in spec['controls']}; seen = set()
    for effect in effects:
        descriptor = effect.get('identity', {}); identifier = descriptor.get('semantics', {}).get('identifier')
        item = by_id.get(identifier); observed = [c for c in observation.get('controls', [])
                                               if c.get('semantics', {}).get('identifier') == identifier]
        if (effect.get('kind') != 'press' or item is None or item['key'] not in oracle['allowed_navigation_keys']
                or len(observed) != 1 or item['key'] in seen):
            reasons.append('navigation_effect_outside_frozen_scope'); continue
        seen.add(item['key']); c = observed[0]
        if (effect.get('control_id') != c.get('id')
                or any(descriptor.get(k) != c.get(k) for k in ('name', 'role'))
                or any(descriptor.get('semantics', {}).get(k) != c.get('semantics', {}).get(k) for k in ('identifier', 'help'))):
            reasons.append('navigation_observed_identity_changed')
        parent = next((r for r in observation['controls'] if r['id'] == c.get('parent')), None)
        ancestors = descriptor.get('ancestors', [])
        if parent and (not ancestors or ancestors[0].get('role') != parent.get('role')
                or ancestors[0].get('semantics', {}).get('identifier') != parent.get('semantics', {}).get('identifier')):
            reasons.append('navigation_parent_unproved')
    if not effects: reasons.append('navigation_requires_observed_effect')
    attested = _attested(review, spec, prose_attestation)
    # Arbitrary navigation prose must receive independent, hash-bound judgment;
    # fixture knowledge cannot silently approve a misleading summary/purpose.
    result = {'accepted': not reasons and attested, 'structured_pass': not reasons,
        'decision': 'reject' if reasons else 'approve' if attested else 'needs_independent_prose_review',
        'reasons': sorted(set(reasons)), 'prose_fields_requiring_review': ['navigation_summary_purpose_and_unresolved_requirements'],
        'review_sha256': base.digest(review), 'oracle_sha256': base.digest(oracle),
        'independent_prose_attestation_used': attested, 'oracle_sent_to_model': False,
        'partial_navigation_only': True, 'retained_review_is_not_current_action_authority': True}
    return result


def audit_state(desktop, oracle, final, events):
    if not oracle.get('allowed_navigation_keys'): return _ORIGINAL_AUDIT(desktop, oracle, final, events)
    # Reuse the established exact readback auditor on the task effects, while
    # checking every removed navigation transition independently and explicitly.
    nav = set(oracle['allowed_navigation_keys']); navigation = []; errors = []
    source = {o['snapshot_id']: o for o in desktop.observations}; specs = {c['key']: c for c in desktop.spec['controls']}
    previous = deepcopy(desktop.spec['initial']); normalized = []; stage_keys = set()
    for event in desktop.executions:
        if base.digest(previous) != base.digest(event['before']): errors.append('execution_state_chain_changed')
        previous = deepcopy(event['after'])
        if event['key'] not in nav:
            normalized.append(deepcopy(event)); continue
        navigation.append(event); transition = desktop.spec.get('transitions', {}).get(event['key'])
        o = source.get(event.get('source_snapshot_id'), {}); c = next((c for c in o.get('controls', []) if c['id'] == event.get('source_control_id')), {})
        if transition is None: errors.append('undeclared_navigation_transition'); continue
        state_key = transition['state_key']; stage_keys.add(state_key)
        expected_after = {**event['before'], state_key: transition['value']}
        action = event['action']
        valid = (event.get('fixture_navigation') is True and action.get('kind') == 'press'
            and event['after'] == expected_after and c.get('semantics', {}).get('identifier') == specs[event['key']]['identifier']
            and action.get('target') == o.get('target') == event.get('source_target')
            and action.get('snapshot_id') == o.get('snapshot_id')
            and action.get('control_id') == c.get('id')
            and o.get('observed_at_ns', 2**64) <= event['at_ns'] <= event['completed_at_ns'])
        if not valid: errors.append('invalid_navigation_issuance')
    if base.digest(previous) != base.digest(desktop.state): errors.append('final_state_chain_changed')
    # Projection for the reused audit excludes only declared UI navigation state;
    # actual exact final state and all navigation evidence remain separately required.
    def without_stages(value): return {k: deepcopy(v) for k, v in value.items() if k not in stage_keys}
    proxy = deepcopy(desktop); proxy.state = without_stages(desktop.state)
    proxy.spec['initial'] = without_stages(desktop.spec['initial'])
    for event in normalized:
        event['before'] = without_stages(event['before']); event['after'] = without_stages(event['after'])
    proxy.executions = normalized; reduced = deepcopy(oracle)
    reduced['expected_state'] = without_stages(oracle['expected_state'])
    reduced['minimum_dispatches'] = len(oracle['required_effect_keys']); reduced['maximum_dispatches'] = len(oracle['required_effect_keys'])
    result = _ORIGINAL_AUDIT(proxy, reduced, final, events)
    exact = desktop.state == oracle['expected_state']
    dispatch_bounds = oracle['minimum_dispatches'] <= len(desktop.executions) <= oracle['maximum_dispatches']
    result.update(navigation_dispatches=len(navigation), navigation_evidence_errors=errors,
        total_dispatches=len(desktop.executions), dispatches=len(desktop.executions), independent_state_exact=exact,
        passed=bool(result['passed'] and exact and dispatch_bounds and navigation and not errors))
    if errors or not exact: result['unintended_changes'] += 1
    return result


_ORIGINAL_AUDIT = sem.audit_state


def owner_for(spec, out, profile, ask=lambda *_: ''):
    return DesktopToolset({}, out, spec['request'], ask, desktop=StagedDesktop(spec),
                          tool_profile=profile, persistence_contract=TEXT_PERSISTENCE_CONTRACT)


def informed_setup(owner):
    """Evaluator read-only setup, separately reported from model autonomy."""
    ui = owner.model_interface; outputs = []
    result = ui.call('locua_apps', {'query': 'Layout Lab'}); outputs.append(result)
    app = result['items'][0]['app_id']
    if owner.tool_profile in STEP_PROFILES:
        result = ui.call('locua_inspect', {'reference': app}); outputs.append(result)
        window = result['windows'][0]['window_id']
        result = ui.call('locua_inspect', {'reference': window}); outputs.append(result)
        view = result['view']; result = ui.call('locua_search', {'reference': view})
    else:
        result = ui.call('locua_windows', {'app_id': app}); outputs.append(result)
        window = result['windows'][0]['window_id']
        result = ui.call('locua_observe', {'window_id': window}); outputs.append(result)
        view = result['view']; result = ui.call('locua_inspect', {'view': view})
    outputs.append(result)
    while result.get('coverage', {}).get('continue_with'):
        args = result['coverage']['continue_with']
        result = ui.call('locua_inspect', args); outputs.append(result)
        if len(outputs) > 32: raise ValueError('Bounded informed setup could not finish available control pages')
    return outputs


def request_render(owner, outputs, *, seeded_approval=False):
    from amplifier_core.message_models import ChatRequest
    tools = apply_tool_help(owner.tools(), INSTRUCTIONS)
    request = {'messages': [
        {'role': 'system', 'content': instruction_policy(INSTRUCTIONS) + '\nORIGINAL USER REQUEST (retain throughout):\n' + owner.request},
        {'role': 'user', 'content': owner.request},
        {'role': 'user', 'content': ('Evaluator setup: these are actual synthetic tool observations. The last receipt is an explicitly evaluator-seeded reviewed calculation scope; it supplies approval but no action sequence or result.\n' if seeded_approval else 'Evaluator read-only setup: these are actual synthetic tool observations, not approvals or instructions. No action has been approved.\n') + json.dumps(outputs, ensure_ascii=False)},
        {'role': 'user', 'content': owner.model_interface.state_text()}],
        'tools': [{'name': t.name, 'description': t.description, 'parameters': t.input_schema} for t in tools]}
    return ChatRequest.model_validate(request).model_dump()


def seed_calculation_review(owner, spec, oracle, outputs):
    """Explicit evaluator assistance for a separate action-mapping-only screen."""
    if len(oracle.get('goals', [])) != 1 or oracle['goals'][0]['kind'] != 'calculation':
        raise ValueError('Only the frozen calculation goal may be evaluator-seeded')
    goal = oracle['goals'][0]; identifier = next(c['identifier'] for c in spec['controls'] if c['key'] == goal['key'])
    rows = [r for output in outputs for r in output.get('items', [])
            if isinstance(r, dict) and r.get('identifier') == identifier]
    if len(rows) != 1: raise ValueError('Faithful calculation readout not uniquely observed')
    row = rows[0]
    if owner.tool_profile in STEP_PROFILES:
        choices = [row['outcomes']['calculation']] if row['outcomes'].get('calculation') else []
        if len(choices) != 1: raise ValueError('Calculation outcome eligibility not unique')
        goals = [{'outcome': choices[0], 'value': goal['expression']}]
    else: goals = [{'kind': 'calculation', 'target': row['target'], 'expression': goal['expression']}]
    original_ask = owner.ask
    owner.ask = lambda *_: 'run'
    try: result = owner.model_interface.call('locua_review', {'summary': spec['request'], 'goals': goals, 'covers_request': True})
    finally: owner.ask = original_ask
    if result.get('status') != 'approved': raise ValueError('Evaluator-seeded review refused: ' + str(result))
    return result


def parse_allowed_trials(values, cases):
    if values is None: return None
    trials = []
    for value in values:
        parts = value.split('/')
        if len(parts) != 4: raise ValueError('Allowed trial must be model/profile/case/mode')
        model, profile, case, mode = parts
        if model not in MODELS or profile not in PROFILES or case not in cases or mode != 'warm-loop':
            raise ValueError('Unknown allowed trial selection: ' + value)
        trial = dict(model=model, profile=profile, case=case, mode=mode)
        if trial in trials: raise ValueError('Duplicate allowed trial tuple')
        trials.append(trial)
    if not trials: raise ValueError('Explicit trial cohort cannot be empty')
    return trials


def cohort_check(phase, model, profile, case, mode, phase_path=None):
    allowed = phase.get('allowed_trials')
    if allowed is not None and dict(model=model, profile=profile, case=case, mode=mode) not in allowed:
        raise ValueError('Trial is outside the frozen authorized initial cohort; no lane reset or implicit promotion')
    prerequisite = phase.get('mapping_prerequisite')
    if prerequisite and case != prerequisite:
        if phase_path is None: raise ValueError('Mapping progression requires a frozen phase identity')
        digest = sha(Path(phase_path) / 'phase.json')
        events = [e for e in read(phase['exposure_ledger'])['events'] if e.get('event') == 'finished'
            and e.get('phase_sha256') == digest and e.get('model') == model and e.get('profile') == profile
            and e.get('case') == prerequisite and e.get('mode') == 'warm-loop']
        if not events or events[-1].get('verified_arithmetic_mapping') is not True:
            raise ValueError('Frozen progression requires this model/profile arithmetic mapping pass before follow-up cases')


def configuration_identity(config=None):
    from locua.config import selected_path
    path = selected_path(config).resolve()
    if not path.is_file(): raise ValueError('Selected local configuration file is required for a frozen model phase')
    return {'path': str(path), 'sha256': sha(path)}


def prepare(cases_root, out, config=None, supersedes=None, previous_phase=None, causal_change=None,
            candidate_profile='step-v1', allowed_trial=None, mapping_prerequisite=None, candidate_factor=None):
    from locua.amplifier_provider import native_model_pins
    root = Path(out).resolve(); root.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest, rows = sem.read_cases(cases_root); renderings = {}; inventories = {}
    if candidate_profile not in STEP_PROFILES: raise ValueError('Candidate must be an explicit supported step profile')
    allowed_trials = parse_allowed_trials(allowed_trial, {spec['id'] for spec, _ in rows})
    if mapping_prerequisite is not None and (allowed_trials is None or mapping_prerequisite != 'arithmetic-development'):
        raise ValueError('Mapping prerequisite requires an explicit frozen cohort and the development arithmetic case')
    for spec, oracle in rows:
        for profile in PROFILES:
            setup_clock_ns = time.time_ns()
            with patch('time.time_ns', return_value=setup_clock_ns):
                owner = owner_for(spec, root / 'render-tools' / spec['id'] / profile, profile)
                try:
                    outputs = informed_setup(owner); payload = request_render(owner, outputs)
                    path = root / 'renderings' / (spec['id'] + '--' + profile + '.json'); path.parent.mkdir(exist_ok=True)
                    private_json(path, {'request': payload, 'reference_map': owner.model_interface.refs,
                        'fixture_clock': fixture_clock(setup_clock_ns),
                        'case_id': spec['id'], 'profile': profile, 'scope': 'isolated_informed_decision_only'})
                    renderings[spec['id'] + '--' + profile] = {'path': str(path.relative_to(root)), 'sha256': sha(path)}
                    inventory = payload['tools']
                    if profile in inventories and inventories[profile] != inventory: raise ValueError('Case-conditioned tool inventory is prohibited')
                    inventories[profile] = inventory
                    if spec['id'].startswith('arithmetic-'):
                        approval = seed_calculation_review(owner, spec, canonical_oracle(oracle), outputs)
                        payload = request_render(owner, outputs + [approval], seeded_approval=True)
                        path = root / 'renderings' / (spec['id'] + '--' + profile + '--action.json')
                        private_json(path, {'request': payload, 'reference_map': owner.model_interface.refs,
                            'fixture_clock': fixture_clock(setup_clock_ns),
                            'case_id': spec['id'], 'profile': profile, 'scope': 'isolated_action_mapping_only',
                            'evaluator_seeded_faithful_calculation_review': True})
                        renderings[spec['id'] + '--' + profile + '--action'] = {'path': str(path.relative_to(root)), 'sha256': sha(path)}
                finally: owner.close()
    sources = base.source_hashes()
    for path in (Path(__file__), ROOT / 'tools/semantic_policy_eval.py', ROOT / 'tools/continuity_cli_eval.py'):
        sources[str(path.relative_to(ROOT))] = sha(path)
    phase = {'version': VERSION, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'cases_root': str(Path(cases_root).resolve()), 'case_manifest_sha256': sha(Path(cases_root) / 'cases.json'),
        'cases': [s['id'] for s, _ in rows], 'models': list(MODELS), 'profiles': list(PROFILES),
        'source_hashes': sources, 'renderings': renderings, 'tool_specs': inventories,
        'system': instruction_policy(INSTRUCTIONS), 'instruction_profile': INSTRUCTIONS,
        'candidate_factors': candidate_factor if candidate_factor is not None else REVISION_FACTORS if candidate_profile == 'step-v2' else FACTORS,
        'candidate_profile': candidate_profile, 'allowed_trials': allowed_trials,
        'mapping_prerequisite': mapping_prerequisite,
        'evaluator_changes': ['pure_nonmutating_lane_regime', 'explicit_profile_dispatch',
            'frozen_cohort_with_mapping_before_followups', 'historical_results_preserved',
            'anchored_single_control_purpose_not_whole_plan_regex'],
        'initial_cohort_rule': 'Only explicitly frozen tuples may start; no inherited smaller-model retry or heldout credit.',
        'baseline_reminder_mode': 'persist', 'candidate_reminder_mode': 'tail',
        'context': deepcopy(CONTEXT_CONFIG), 'focus_context':focus_context_identity(), 'persistence_contract': TEXT_PERSISTENCE_CONTRACT,
        'provider': 'local', 'protocol_recovery': True, 'rlcd': False,
        'configuration': configuration_identity(config), 'model_pins': native_model_pins(),
        'weights_runtime_decoder_cache_changed': False, 'max_calls': 18,
        'evaluation_mode':'warm-loop','allowed_modes':['warm-loop'],'real_model_call_limit':4,
        'warm_loop_timeout_s':{'baseline':120,'comparator':120,'qwen38':300},
        'practical_latency_threshold_s':120,
        'message_construction':{'version':'actual_amplifier_warm_start_tool_pairs_v1',
            'implementation_sha256':base.digest(inspect.getsource(WarmStartProvider))},
        'setup_assistance':'Evaluator generic read-only app/window/overview/list discovery through actual Amplifier tools; all visible pages, no review/plan/oracle target selection.',
        'timeout_s': {'baseline': 120, 'comparator': 120, 'qwen38': 180}, 'isolated_timeout_s': 120,
        'start_cutoff_utc': START_CUTOFF, 'deadline_utc': DEADLINE,
        'exposure_ledger': str((Path(cases_root).parent / 'exposure-ledger.json').resolve()),
        'heldout_rule': manifest['heldout_policy'], 'scope': 'Synthetic component evidence; no desktop task credit'}
    phase['causal_factors']=causal_factors(phase)
    phase['causal_regime']=base.digest(phase['causal_factors'])
    if previous_phase is not None:
        previous_path=Path(previous_phase).resolve()/'phase.json'
        phase.update(causal_reset(read(previous_path),phase,causal_change))
        phase['previous_phase']={'path':str(previous_path),'sha256':sha(previous_path)}
    elif causal_change:raise ValueError('Causal changes require a frozen previous phase')
    warm_renderings={}
    for spec,oracle in rows:
        for profile in PROFILES:
            capture_root=root/'warm-renderings'/(spec['id']+'--'+profile)
            captured=asyncio.run(capture_warm_request(spec,canonical_oracle(oracle),capture_root,profile=profile))
            path=capture_root/'warm-start/first-real-provider-request.json'
            warm_renderings[spec['id']+'--'+profile]={'path':str(path.relative_to(root)),'sha256':sha(path),
                'capture_proof':captured}
    phase['warm_renderings']=warm_renderings
    if supersedes is not None:
        prior = Path(supersedes).resolve() / 'phase.json'
        phase['supersedes'] = {'path': str(prior), 'sha256': sha(prior),
            'reason': 'Evaluator preflight cursor reconstruction repair. Prior attempt made zero provider calls; original phase, attempt and ledger remain intact.'}
    private_json(root / 'phase.json', phase); return phase


def load_phase(path, model, profile, case, config=None):
    from locua.amplifier_provider import native_model_pins
    path = Path(path).resolve(); phase = read(path / 'phase.json')
    for key, value in (('models', model), ('profiles', profile), ('cases', case)):
        if value not in phase[key]: raise ValueError('Selection was not frozen: ' + value)
    if sha(Path(phase['cases_root']) / 'cases.json') != phase['case_manifest_sha256']: raise ValueError('Frozen case manifest changed')
    for name, digest in phase['source_hashes'].items():
        if sha(ROOT / name) != digest: raise ValueError('Frozen source changed: ' + name)
    if configuration_identity(config) != phase['configuration']: raise ValueError('Selected local configuration differs from frozen phase')
    if native_model_pins() != phase['model_pins']: raise ValueError('Native model identity/template policy pins changed')
    if 'focus_context' in phase and focus_context_identity(phase.get('profiles',PROFILES))!=phase['focus_context']:
        raise ValueError('Measured focus-context adapter/session/upstream source identity changed')
    for rendering in [*phase['renderings'].values(),*phase.get('warm_renderings',{}).values()]:
        if sha(path / rendering['path']) != rendering['sha256']: raise ValueError('Frozen rendering changed')
    if 'causal_regime' in phase:
        if phase['causal_regime']!=base.digest(causal_factors(phase)):
            raise ValueError('Frozen causal identity differs from actual declared factors')
        previous=phase.get('previous_phase')
        if previous:
            if sha(previous['path'])!=previous['sha256']:raise ValueError('Frozen predecessor phase changed')
            reset=causal_reset(read(previous['path']),phase,phase.get('declared_changes'))
            if reset['causal_regime']!=phase['causal_regime']:raise ValueError('Causal reset proof does not match phase')
        elif any(e.get('phase_sha256')!=sha(path/'phase.json') for e in read(phase['exposure_ledger'])['events']):
            raise ValueError('An existing experiment requires an explicit predecessor and declared causal change before a new model lane')
    _, rows = sem.read_cases(phase['cases_root']); spec, oracle = next(r for r in rows if r[0]['id'] == case)
    return phase, spec, canonical_oracle(oracle)


def exposure(phase_path, phase, model, profile, spec, mode, report=None):
    ledger_path = Path(phase['exposure_ledger']); lock = ledger_path.with_suffix('.lock'); lock.touch(mode=0o600, exist_ok=True)
    with lock.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX); ledger = read(ledger_path); events = ledger['events']; phase_hash = sha(Path(phase_path) / 'phase.json')
        regime=lane_regime(phase,model,profile)
        same = [e for e in events if e['case'] == spec['id']]
        if report is None:
            if spec['split'] in ('heldout', 'held-out') and any(e['phase_sha256'] != phase_hash for e in same):
                raise ValueError('Held-out case exposed in another phase; not globally fresh')
            if any(e['phase_sha256'] == phase_hash and e['model'] == model and e['profile'] == profile
                   and e['mode'] == mode and e['event'] == 'started' for e in same):
                raise ValueError('Frozen lane/case already attempted; no silent retry')
            latest = {}
            for event in events:
                if (event['event'] == 'finished' and event['model'] == model and event['profile'] == profile
                        and event.get('causal_regime',regime if event['phase_sha256']==phase_hash else None)==regime):
                    key = (event['phase_sha256'], event['case'], event['mode'])
                    latest.pop(key, None); latest[key] = event
            counts=Counter(e['failure_signature'] for e in latest.values() if e.get('informed_failure'))
            if any(count>=2 for count in counts.values()):
                raise ValueError('Lane stopped after two equivalent informed failures in unchanged causal regime')
        row = {'event': 'started' if report is None else 'finished', 'at_ns': time.time_ns(), 'mode': mode,
            'case': spec['id'], 'split': spec['split'], 'phase_sha256': phase_hash, 'causal_regime':regime, 'model': model, 'profile': profile,
            'globally_exposed_before_attempt': bool(same)}
        if report is not None:
            category = report['failure_category']; row.update(status=report['status'], failure_signature=category,
                informed_failure=category in INFORMED_FAILURES,
                verified_synthetic_completion=bool(report.get('outcome_achieved') and report.get('audit',{}).get('passed')
                    and report.get('normal_session_completion')),
                verified_arithmetic_mapping=bool(report.get('arithmetic_mapping_verified')),
                evaluator_setup_assistance=bool(report.get('evaluator_read_only_discovery_assistance')))
        events.append(row); sem.replace_json(ledger_path, ledger)


def promotion_check(phase_path, phase, model, spec, mode):
    if spec['split'] not in ('heldout', 'held-out') and mode != 'multistep': return
    phase_hash = sha(Path(phase_path) / 'phase.json'); latest = {}
    source_mode='warm-loop' if mode=='warm-loop' else 'isolated'
    for event in read(phase['exposure_ledger'])['events']:
        if (event['event'] == 'finished' and event['phase_sha256'] == phase_hash
                and event['model'] == model and event['profile'] == phase.get('candidate_profile','step-v1') and event['mode'] == source_mode):
            latest[event['case']] = event
    required = [case for case in phase['cases'] if case.endswith('-development')]
    if source_mode=='warm-loop':
        def accepted(case):
            row=latest.get(case,{})
            return row.get('verified_arithmetic_mapping') is True if case.startswith('arithmetic-') else row.get('verified_synthetic_completion') is True
        if not required or not all(accepted(case) for case in required):
            raise ValueError('Held-out warm-loop gate requires faithful arithmetic mapping and independently verified editor/appearance development completion for this model')
    elif not required or any(latest.get(case, {}).get('status') != 'passed' for case in required):
        raise ValueError('Held-out/multistep model gate requires all three faithful informed candidate development decisions for this model')


class DecisionBudgetExhausted(RuntimeError):
    pass


class MappingDiagnosticComplete(RuntimeError):
    pass


class WarmStartProvider:
    """Evaluator-scripted generic reads, then unchanged ordinary model calls.

    Every setup operation executes through the real Amplifier tool loop. The
    setup never selects a goal/control using an oracle or seeds a review/plan.
    """
    def __init__(self, provider, *, profile, out, decision_limit=4):
        self.provider=provider; self.profile=profile; self.out=Path(out)
        self.stage='apps'; self.setup_calls=[]; self.real_decisions=0
        self.decision_limit=decision_limit; self.mapping_receipts=[]; self.first_model_request=None

    def __getattr__(self,key): return getattr(self.provider,key)

    async def mount(self,coordinator):
        await coordinator.mount('providers',self,name=self.name)

    def _scripted_call(self,output):
        step=self.profile in STEP_PROFILES
        if self.stage=='apps':
            self.stage='windows'; return 'locua_apps',{'query':'Layout Lab'}
        if self.stage=='windows':
            self.stage='observe'; app=output['items'][0]['app_id']
            return ('locua_inspect',{'reference':app}) if step else ('locua_windows',{'app_id':app})
        if self.stage=='observe':
            self.stage='list'; window=output['windows'][0]['window_id']
            return ('locua_inspect',{'reference':window}) if step else ('locua_observe',{'window_id':window})
        if self.stage=='list':
            self.stage='pages'; view=output['view']
            return ('locua_search',{'reference':view}) if step else ('locua_inspect',{'view':view})
        if self.stage=='pages':
            continuation=output.get('coverage',{}).get('continue_with')
            if continuation:return 'locua_inspect',continuation
            self.stage='model'
        return None

    async def complete(self,request,**kwargs):
        if self.mapping_receipts and self.mapping_receipts[-1].get('decision_acceptable'):
            raise MappingDiagnosticComplete('Evaluator arithmetic mapping-only boundary reached; no display completion simulated')
        if self.stage!='model':
            from amplifier_core.message_models import ChatResponse,ToolCall,ToolCallBlock,Usage
            from locua.amplifier_provider import native_request
            native=native_request(request,model=self.model,structured_tool_results=self._structured_tool_results)
            tool=next((json.loads(m['content']) for m in reversed(native['messages']) if m['role']=='tool'),None)
            output=tool.get('output') if tool else None
            if isinstance(output,str):output=json.loads(output)
            if isinstance(output,dict) and 'output' in output:output=output['output']
            selected=self._scripted_call(output)
            if selected is not None:
                if len(self.setup_calls)>=32:raise ValueError('Evaluator read-only setup page budget exhausted')
                name,args=selected; call_id='evaluator-read-'+str(len(self.setup_calls)+1)
                print('Evaluator read-only setup (not a model decision): '+name,flush=True)
                self.setup_calls.append({'id':call_id,'name':name,'arguments':deepcopy(args),
                    'previous_result':deepcopy(output),'evaluator_assistance':True})
                self.out.mkdir(parents=True,exist_ok=True)
                private_json(self.out/(call_id+'.json'),self.setup_calls[-1])
                return ChatResponse(content=[ToolCallBlock(id=call_id,name=name,input=args)],
                    tool_calls=[ToolCall(id=call_id,name=name,arguments=args)],
                    usage=Usage(input_tokens=0,output_tokens=0,total_tokens=0))
        if len(getattr(self.provider,'records',[]))>=self.decision_limit or self.real_decisions>=self.decision_limit:
            raise DecisionBudgetExhausted('Model decision budget exhausted; further discovery may be needed, no completion claim')
        if self.first_model_request is None:
            self.first_model_request=deepcopy(request.model_dump())
            self.out.mkdir(parents=True,exist_ok=True)
            private_json(self.out/'first-real-provider-request.json',self.first_model_request)
        self.real_decisions+=1
        return await self.provider.complete(request,**kwargs)


class MappingOnlyTool:
    """Keep published action contract, but no arithmetic simulator is implied."""
    def __init__(self,tool,owner,spec,oracle,provider):
        self.tool=tool;self.owner=owner;self.spec=spec;self.oracle=oracle;self.provider=provider
        self.name=tool.name;self.description=tool.description;self.input_schema=deepcopy(tool.input_schema)
    async def execute(self,arguments):
        from amplifier_core.models import ToolResult
        result=score_action(self.owner,{'name':self.name,'arguments':arguments},self.spec,self.oracle)
        self.provider.mapping_receipts.append(deepcopy(result))
        private_json(self.owner.out/('mapping-only-'+str(len(self.provider.mapping_receipts))+'.json'),
            {'tool':self.name,'arguments':arguments,'result':result})
        public={'status':'diagnostic_mapping_verified' if result['decision_acceptable'] else 'refused',
            'action_started':False,'task_completed':False,'arithmetic_display_simulated':False,
            'reason':'Evaluator scored observed arithmetic mapping without dispatch; actual Calculator is required for outcome proof.'}
        # Private oracle/witness details never become instructions or a correct
        # target/action sequence in the model-visible result.
        if not result['decision_acceptable']:
            public['reason']='Arithmetic execution is not simulated. The proposed action mapping was not demonstrated; no input was dispatched.'
        return ToolResult(success=result['decision_acceptable'],output=public)


def arithmetic_mapping_audit(desktop,oracle,final,events):
    exact=base.digest(desktop.state)==base.digest(desktop.spec['initial'])
    return {'passed':False,'independent_state_exact':exact,'requested_action_executed':False,
        'unintended_changes':int(not exact),'fresh_reviewed_predicates_verified':False,
        'independent_post_input_ui_verified':False,'dispatches':len(desktop.executions),
        'input_evidence_errors':[],'real_desktop_calls':0,'desktop_completion_credit':False}


def outcome_category(report,provider):
    """Evidence-based classification independent of arbitrary exception labels."""
    records=getattr(provider,'records',[])
    generation=next((r.get('generation') for r in reversed(records) if r.get('generation')),None)
    if generation and generation.get('finish_reason')=='length':return 'output_budget_exhausted'
    error=report.get('error') or ''
    if 'DecisionBudgetExhausted' in error or 'provider_call_budget_exhausted' in error:return 'decision_budget_inconclusive'
    if 'TimeoutError' in error or 'execution budget exhausted' in error.lower():return 'execution_time_budget'
    if report.get('failure_category') in ('argument_contract','reference_or_grounding','review_semantic_mismatch','repeated_nonprogress'):
        return report['failure_category']
    if not report.get('outcome_achieved') and records:
        response=records[-1].get('response') or {}
        if records[-1].get('status')=='completed' and not response.get('tool_calls'):
            return 'no_tool_progress'
    return report.get('failure_category','unknown')


INFORMED_FAILURES=frozenset({'argument_contract','reference_or_grounding','review_semantic_mismatch',
    'repeated_nonprogress','no_tool_progress','output_budget_exhausted','tool_protocol'})


def focus_context_identity(profiles=PROFILES):
    from locua.focus_context import SOURCE_PINS,module_compatibility
    compatible,observed=module_compatibility()
    return {'enabled_by_profile':{profile:profile in STEP_PROFILES for profile in profiles},
        'adapter_source_sha256':sha(ROOT/'src/locua/focus_context.py'),
        'session_source_sha256':sha(ROOT/'src/locua/amplifier_session.py'),
        'upstream_source_pins':deepcopy(SOURCE_PINS),'compatible':compatible,
        'observed_upstream':observed,'unknown_upstream_behavior':'Keep full context, never silently deduplicate'}


def causal_factors(phase):
    """Administrative phase/scoring changes cannot masquerade as model treatment."""
    relevant=('src/locua/model_interface.py','src/locua/semantic_projection.py','src/locua/step_interface.py',
        'src/locua/progressive_ui.py','src/locua/amplifier_contracts.py','src/locua/instruction_policy.py','src/locua/plan_progress.py')
    return {'model_runtime_decoder':{'model_pins':phase.get('model_pins'),'configuration':phase.get('configuration')},
        'system_policy':phase.get('system'),
        'tool_contract_and_projection':{'tools':phase.get('tool_specs'),
            'source':{k:phase.get('source_hashes',{}).get(k) for k in relevant}},
        'context':{'config':phase.get('context'),'measured_view':phase.get('focus_context',{'enabled_by_profile':{'semantic-v1':False,'step-v1':False}})},
        'reminder_lifecycle':{'baseline':phase.get('baseline_reminder_mode'),'candidate':phase.get('candidate_reminder_mode')},
        'message_construction':phase.get('message_construction','isolated_user_observation_bundle_v2')}


def lane_regime(phase,model,profile):
    """Unrelated model/profile edits cannot reset an unchanged lane."""
    factors=deepcopy(causal_factors(phase))
    model_factors=factors['model_runtime_decoder']
    pins=model_factors.get('model_pins')
    if isinstance(pins,dict):model_factors['model_pins']=pins.get(model)
    tool_factors=factors['tool_contract_and_projection']
    if isinstance(tool_factors.get('tools'),dict):tool_factors['tools']=tool_factors['tools'].get(profile)
    if profile=='semantic-v1':
        tool_factors['source'].pop('src/locua/step_interface.py',None)
        tool_factors['source'].pop('src/locua/plan_progress.py',None)
    view=factors['context']['measured_view']
    if view.get('enabled_by_profile',{}).get(profile) is not True:
        factors['context']['measured_view']={'enabled':False}
    else:
        view['enabled']=True;view.pop('enabled_by_profile',None)
    factors['reminder_lifecycle']=factors['reminder_lifecycle'].get('candidate' if profile in STEP_PROFILES else 'baseline')
    return base.digest(factors)


def causal_reset(previous,phase,declared_changes):
    before=causal_factors(previous);after=causal_factors(phase)
    changed=sorted(k for k in after if before.get(k)!=after[k])
    if not changed or set(changed)!=set(declared_changes or []):
        raise ValueError('Exact causal changes must be declared; phase name/classification fixes alone cannot reset stopped lanes: '+str(changed))
    return {'factors':after,'causal_regime':base.digest(after),'declared_changes':changed,
        'previous_causal_regime':previous.get('causal_regime',base.digest(before)),
        'reset_basis':'Verified declared factor delta; no heldout freshness reset'}


async def run_case(spec, oracle, out, provider, *, profile, max_calls=18, timeout_s=180, review_wait_s=180, warm_start=False, decision_limit=4):
    """Reuse the existing real-session runner, guards, bounds and evidence writer."""
    arithmetic=any(g['kind']=='calculation' for g in oracle.get('goals',[]))
    if arithmetic and not warm_start:raise ValueError('Arithmetic is decision-only; actual Calculator supplies execution evidence')
    oracle = canonical_oracle(oracle); owners = []
    actual_provider=provider
    if warm_start:provider=WarmStartProvider(provider,profile=profile,out=Path(out)/'warm-start',decision_limit=decision_limit)
    class Desktop(StagedDesktop):
        def execute(self,action,observation):
            if arithmetic:raise AssertionError('Arithmetic mapping diagnostic cannot dispatch inputs')
            return super().execute(action,observation)
    def owner_factory(*args, **kwargs):
        kwargs['persistence_contract'] = TEXT_PERSISTENCE_CONTRACT
        owner = DesktopToolset(*args, **kwargs); owners.append(owner)
        if arithmetic:
            original_tools=owner.tools
            owner.tools=lambda:[MappingOnlyTool(t,owner,spec,oracle,provider) if t.name in ('locua_act','locua_act_sequence') else t for t in original_tools()]
        return owner
    async def session(*args, **kwargs):
        return await execute_session(*args, **kwargs, execution_facts_mode='tail' if profile in STEP_PROFILES else 'persist',
            focus_dedup=profile in STEP_PROFILES)
    # All substitutions are evaluator-owned dependencies, scoped to this serial
    # fixture execution. No production globals, guards or model methods change.
    with ExitStack() as stack:
        stack.enter_context(patch.object(sem, 'SemanticDesktop', Desktop))
        stack.enter_context(patch.object(sem, 'DesktopToolset', owner_factory))
        stack.enter_context(patch.object(sem, 'execute_session', session))
        stack.enter_context(patch.object(sem, 'audit_state', arithmetic_mapping_audit if arithmetic else audit_state))
        stack.enter_context(patch.object(reviews, 'review_gate', review_gate))
        report = await sem.run_case(spec, oracle, out, provider, profile=profile,
            max_calls=max_calls, timeout_s=timeout_s, review_wait_s=review_wait_s)
    if profile in STEP_PROFILES:
        public = [read(p) for p in sorted((Path(out) / 'tools').glob('step-*.json'))]
        schemas = {t.name: t.input_schema for t in owners[0].tools()}
        invalid = [{'sequence': r['sequence'], 'tool': r['tool'], 'errors': _errors(schemas[r['tool']], r['input'])}
                   for r in public if r['tool'] in schemas and _errors(schemas[r['tool']], r['input'])]
        report['first_pass'].update(schema_errors=invalid, all_published_arguments_valid=not invalid,
            first_refusal=next((r for r in public if r['result'].get('status') in ('refused', 'blocked', 'uncertain', 'canceled')), None))
        report['public_model_choices'] = len(public)
    session_receipt=read(Path(out)/'session/session.json')
    focus_events=[event['data'] for event in session_receipt.get('events',[]) if event['event']=='context:focus_dedup']
    report.update(focus_dedup_requested=profile in STEP_PROFILES,
        focus_dedup_enabled=session_receipt.get('focus_dedup_enabled',False),
        focus_context_events=focus_events,
        focus_deduplicated_views=sum(event.get('stage')=='selected' and event.get('deduplicated') is True for event in focus_events))
    report['failure_category']=outcome_category(report,actual_provider)
    if warm_start:
        report.update(evaluator_read_only_discovery_assistance=True,
            evaluator_setup_tool_calls=len(provider.setup_calls), real_model_decision_requests=provider.real_decisions,
            model_decision_limit=decision_limit, mapping_receipts=provider.mapping_receipts,
            arithmetic_mapping_verified=bool(provider.mapping_receipts and provider.mapping_receipts[-1].get('decision_acceptable')),
            model_started_from_user_request_alone=False, practical_under_120s=report['elapsed_s']<120)
        if report['failure_category']=='decision_budget_inconclusive':report['status']='inconclusive'
        if arithmetic:
            failed=next((r for r in provider.mapping_receipts if not r.get('decision_acceptable')),None)
            report['first_mapping_failure']=failed
            if failed and not report['arithmetic_mapping_verified']:report['failure_category']=failed['category']
            report.update(task_completed=False,independent_display_result_verified=False,task_completion_credit=False,
                evidence_level='arithmetic_mapping_only',outcome_achieved=False)
            if report['arithmetic_mapping_verified']:
                report.update(status='mapping_verified',failure_category='arithmetic_mapping_verified')
    report.update(version=VERSION, evidence_level='arithmetic_mapping_only' if arithmetic else 'multistep_synthetic_execution',
        candidate_factors=REVISION_FACTORS if profile=='step-v2' else FACTORS if profile in STEP_PROFILES else [],
        reminder_mode='tail' if profile in STEP_PROFILES else 'persist', persistence_contract=TEXT_PERSISTENCE_CONTRACT,
        task_completion_credit=False if arithmetic else 'synthetic_only', **metrics(actual_provider))
    private_json(Path(out) / 'step-summary.json', report); return report


async def capture_warm_request(spec,oracle,out,*,profile):
    """Freeze a representative real-framework request; CPU only, no inference."""
    from amplifier_core.message_models import ChatResponse,TextBlock,Usage
    from locua.amplifier_provider import LocalAmplifierProvider
    class Capture(LocalAmplifierProvider):
        request_budget=None
        async def complete(self,request,**kwargs):
            response=ChatResponse(content=[TextBlock(text='Evaluator CPU capture boundary; no model decision was generated.')],
                usage=Usage(input_tokens=0,output_tokens=0,total_tokens=0))
            self.records.append({'status':'completed','request':request.model_dump(),'response':response.model_dump(),
                'cpu_capture_not_model_generation':True})
            return response
    provider=Capture(model='comparator')
    try:
        report=await run_case(spec,oracle,out,provider,profile=profile,warm_start=True,
            decision_limit=1,max_calls=40,timeout_s=15,review_wait_s=0)
    finally:await provider.close()
    path=Path(out)/'warm-start/first-real-provider-request.json'
    request=read(path);roles=[m['role'] for m in request['messages']]
    if 'tool' not in roles or report['audit']['dispatches']:
        raise AssertionError('Actual tool receipts and zero inputs required for frozen discovery capture')
    return {'model_generations':0,'cpu_only':True,'evaluator_setup_tool_calls':report['evaluator_setup_tool_calls'],
        'roles':roles,'application_inputs':0,'session_cleanup':report['session_cleanup'],
        'next_model_tool_prescribed':False,'snapshot_timestamps_may_differ_in_fresh_run':True}


def metrics(provider):
    records = getattr(provider, 'records', []); budgets = getattr(provider, 'budget_records', [])
    generations = [r['generation'] for r in records if isinstance(r.get('generation'), dict)]
    timings = [g.get('timing', {}) for g in generations]
    values = {key: sum(t.get(key, 0) for t in timings if type(t.get(key)) in (int, float))
              for key in sorted({k for t in timings for k in t if type(t[k]) in (int, float)})}
    return {'provider_returned_calls': sum(r.get('status') == 'completed' for r in records),
        'provider_attempts': len(records), 'token_measurements': len(budgets),
        'known_usage': {k: sum(g.get('usage', {}).get(k, 0) for g in generations) for k in ('input_tokens', 'output_tokens')},
        'generation_timing_totals': values, 'per_call_timing': timings,
        'generation_metrics': [g.get('generation_metrics') for g in generations],
        'worker_startup_s': getattr(provider, '_step_startup_s', None),
        'context_measurement_wall_ms': sum(r.get('service_wall_ms', 0) for r in budgets),
        'context_measurements': [{'service_wall_ms': r.get('service_wall_ms'), 'status': r.get('status')}
                                 for r in budgets],
        'timing_limit': 'Native timing fields retained verbatim. Context measurement wall may include initial worker startup and must not be added to startup as disjoint time.',
        'actual_model_configuration': [{k: generations[0].get('model_info', {}).get(k) for k in
            ('model_key', 'model_pin', 'decoder', 'template_kwargs', 'prompt_cache_policy', 'loaded_snapshot', 'load_ms', 'file_integrity_check_ms')}] if generations else []}


def instrument_startup(provider):
    original = provider._factory; provider._step_startup_s = []
    def factory(*args, **kwargs):
        start = time.monotonic()
        try: return original(*args, **kwargs)
        finally: provider._step_startup_s.append(time.monotonic() - start)
    provider._factory = factory


def score_isolated(owner, call, spec, oracle):
    """Compile a selected review without allowing desktop edits; score canonical evidence."""
    name = call.get('name'); args = call.get('arguments', {}); ui = owner.model_interface
    schemas = {t.name: t.input_schema for t in owner.tools()}
    if name not in schemas or _errors(schemas[name], args):
        return {'decision_acceptable': False, 'category': 'argument_contract', 'action_executed': False}
    if name not in ('locua_review', 'locua_inspect', 'locua_search', 'locua_status', 'locua_apps', 'locua_windows'):
        return {'decision_acceptable': False, 'category': 'unapproved_or_unnecessary_action', 'action_executed': False}
    result = ui.call(name, args)
    if name != 'locua_review':
        return {'decision_acceptable': False, 'safe_discovery': result.get('status') in ('ok', 'observed'),
                'category': 'safe_discovery_only' if result.get('status') in ('ok', 'observed') else 'reference_or_grounding',
                'result': result, 'action_executed': False}
    files = sorted(owner.out.glob('review-*.json'))
    if not files:
        return {'decision_acceptable': False, 'category': 'argument_contract' if result.get('code') == 'argument_contract_invalid' else 'reference_or_grounding',
                'result': result, 'action_executed': False}
    review = read(files[-1]); observed = owner._observation(review['review_capture']['snapshot_id'])
    gate = review_gate(review, spec, oracle, live_observation=observed)
    return {'decision_acceptable': gate['accepted'], 'structured_semantics_pass': gate['structured_pass'],
        'category': 'informed_review_verified' if gate['accepted'] else 'review_audit_pending' if gate['structured_pass'] else 'review_semantic_mismatch',
                'review_gate': gate, 'review_path': str(files[-1]), 'result': result, 'action_executed': False}


def score_action(owner, call, spec, oracle):
    """Pure existing compilation/preflight plus copied issuance witness; no input."""
    from locua.action_sequence import _freeze
    from locua.model_interface import ModelInterface
    name = call.get('name'); args = call.get('arguments', {}); ui = owner.model_interface
    schemas = {t.name: t.input_schema for t in owner.tools()}
    if name not in schemas or _errors(schemas[name], args):
        return {'decision_acceptable': False, 'category': 'argument_contract', 'action_executed': False}
    if name != 'locua_act_sequence':
        return {'decision_acceptable': False, 'category': 'action_mapping_not_fully_demonstrated',
                'action_executed': False, 'scope': 'Single input/read may be safe but cannot prove full expression mapping'}
    witness_before = {k: deepcopy(s['witness'].view()) for k, s in owner._scopes.items()}
    try:
        if owner.tool_profile in STEP_PROFILES: name, args = ui._compile(name, deepcopy(args))
        translated = ModelInterface._translate(ui, name, args)
        original, frozen, _ = _freeze(owner, **translated)
        scope = deepcopy(owner._scopes[translated['scope_id']]); witness = scope['witness']
        for item in frozen:
            permit = owner._permit(scope, original, item['action'], item.get('value'))
            if permit['kind'] != 'arithmetic': raise ValueError('Mapping selected a non-arithmetic effect')
            witness.record(permit['symbol'], snapshot_id=original['snapshot_id'], descriptor=item['action']['description'])
        matched = witness.matches(oracle['goals'][0]['expression'])
        result = {'decision_acceptable': matched, 'category': 'arithmetic_mapping_verified' if matched else 'arithmetic_mapping_incomplete',
            'copied_issuance_witness': witness.view(), 'result_computed': False,
            'preflight_steps': len(frozen), 'evaluator_seeded_approval': True}
    except Exception as error:
        result = {'decision_acceptable': False, 'category': 'reference_or_grounding', 'reason': type(error).__name__ + ': ' + str(error)}
    unchanged = witness_before == {k: s['witness'].view() for k, s in owner._scopes.items()}
    if not unchanged or owner.desktop.executions: raise AssertionError('Isolated action scorer changed issuance or dispatched input')
    return {**result, 'actual_witness_unchanged': unchanged, 'action_executed': False,
            'task_completed': False, 'independent_display_result_verified': False}


def fixture_clock(setup_clock_ns):
    return {'setup_clock_ns': setup_clock_ns,
        'scope': 'Synthetic isolated read-only fixture construction and reference reconstruction only. Provider generation, decision scoring and all multistep/action freshness checks use the real clock.',
        'reason': 'Private retained pagination cursors hash captured observation timestamps; reconstruct the exact frozen fixture without weakening reference equality.'}


def reconstruct_isolated(owner, spec, oracle, rendering, *, action_mode=False):
    clock = rendering.get('fixture_clock', {}).get('setup_clock_ns')
    if type(clock) is not int or clock <= 0:
        raise ValueError('Frozen isolated fixture requires a positive setup_clock_ns')
    with patch('time.time_ns', return_value=clock):
        outputs = informed_setup(owner)
        # Preserve the same alias-allocation path as prepare before seeding scope.
        request_render(owner, outputs)
        if action_mode:
            approval = seed_calculation_review(owner, spec, oracle, outputs)
            request_render(owner, outputs + [approval], seeded_approval=True)
        if base.digest(owner.model_interface.refs) != base.digest(rendering['reference_map']):
            raise ValueError('Actual isolated reference map differs from frozen rendering')
    # No clock substitution extends to the model or its selected decision.


async def run_isolated(spec, oracle, out, provider, *, profile, rendering, timeout_s=120, action_mode=False):
    from amplifier_core.message_models import ChatRequest
    root = Path(out); root.mkdir(parents=True, mode=0o700, exist_ok=False)
    owner = owner_for(spec, root / 'tools', profile); start = time.monotonic(); report = {}; error = None; provider_started = False
    try:
        reconstruct_isolated(owner, spec, oracle, rendering, action_mode=action_mode)
        request = rendering['request']; private_json(root / 'request.json', request)
        provider_started = True
        response = await asyncio.wait_for(provider.complete(ChatRequest.model_validate(request)), timeout_s)
        body = response.model_dump(); private_json(root / 'response.json', body); calls = body.get('tool_calls') or []
        scored = (score_action if action_mode else score_isolated)(owner, calls[0], spec, oracle) if len(calls) == 1 else {
            'decision_acceptable': False, 'category': 'no_tool_progress' if not calls else 'batch_requires_guarded_loop', 'action_executed': False}
        if scored['category'] == 'review_audit_pending':
            review = read(scored['review_path']); observation = owner._observation(review['review_capture']['snapshot_id'])
            private_json(root / 'review-audit-input.json', {'review': review, 'observation': observation, 'spec': spec, 'oracle': oracle})
            sem.replace_json(root / 'review-pending.json', {'review_path': str(Path(scored['review_path']).resolve()),
                'review_sha256': base.digest(review), 'request_sha256': base.digest(spec['request']),
                'request': spec['request'], 'decision_path': str((root / 'review-decision.json').resolve()),
                'status': 'pending_independent_prose_review', 'gate': scored['review_gate']})
        report.update(status='passed' if scored['decision_acceptable'] else 'pending_review' if scored['category'] == 'review_audit_pending' else 'inconclusive' if scored.get('safe_discovery') else 'failed',
                      failure_category=scored['category'], decision=scored)
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        report.update(status='failed', failure_category='provider_availability_or_time_budget' if provider_started else 'evaluator_preflight', error=error)
        if provider_started:report['failure_category']=outcome_category(report,provider)
    finally:
        cleanup = owner.close(); report.update(version=VERSION, evidence_level='isolated_informed_decision',
            task_completed=False, independent_task_verification=False, application_inputs=len(owner.desktop.executions),
            real_desktop_calls=0, evaluator_read_only_setup=True, evaluator_seeded_approval=action_mode,
            fixture_clock=rendering.get('fixture_clock'), provider_started=provider_started, elapsed_s=time.monotonic()-start,
            candidate_factors=REVISION_FACTORS if profile=='step-v2' else FACTORS if profile in STEP_PROFILES else [], cleanup=cleanup, **metrics(provider))
        private_json(root / 'summary.json', report)
    return report


async def run(phase, out, *, model, profile, case, mode='multistep', config=None):
    from provider_connection import make_provider
    from locua.desktop_session_lock import acquire_desktop_session
    frozen, spec, oracle = load_phase(phase, model, profile, case, config=config)
    if 'allowed_modes' in frozen and mode not in frozen['allowed_modes']:
        raise ValueError('This frozen phase permits only '+str(frozen['allowed_modes'])+'; retained legacy renderings are comparison artifacts')
    cohort_check(frozen, model, profile, case, mode, phase_path=phase)
    promotion_check(phase, frozen, model, spec, mode)
    now = time.time()
    if now >= epoch(frozen['start_cutoff_utc']): raise ValueError('No new experiments after frozen cutoff')
    limit = frozen['isolated_timeout_s'] if mode.startswith('isolated') else frozen['warm_loop_timeout_s'][model] if mode=='warm-loop' else frozen['timeout_s'][model]
    if now + limit + 60 >= epoch(frozen['deadline_utc']): raise ValueError('Insufficient bounded cleanup reserve')
    root = Path(out); root.mkdir(parents=True, mode=0o700, exist_ok=False); report = None; backend = None
    # Match desktop/model ownership even though this adapter is synthetic; never
    # terminate another Locua session to acquire the lease.
    with acquire_desktop_session(purpose='step-policy-evaluation'):
        exposure(phase, frozen, model, profile, spec, mode)
        try:
            backend = make_provider('local', model, root / 'provider', config=config, max_calls=frozen['real_model_call_limit'] if mode=='warm-loop' else 18)
            backend.protocol_recovery = True
            instrument_startup(backend)
            if mode.startswith('isolated'):
                rendering = frozen['renderings'][case+'--'+profile+('--action' if mode == 'isolated-action' else '')]
                report = await run_isolated(spec, oracle, root / 'case', backend, profile=profile,
                    rendering=read(Path(phase) / rendering['path']), timeout_s=limit, action_mode=mode == 'isolated-action')
            else:
                report = await run_case(spec, oracle, root / 'case', backend, profile=profile,
                    max_calls=frozen['max_calls'], timeout_s=limit, warm_start=mode=='warm-loop',
                    decision_limit=frozen.get('real_model_call_limit',4), review_wait_s=min(180, max(0, epoch(frozen['deadline_utc'])-time.time()-limit-60)))
        finally:
            if backend is not None: await backend.close()
            if report is None: report = {'status': 'failed', 'failure_category': 'runner_or_provider_initialization'}
            report.update(model=model, profile=profile, mode=mode, case=case, provider='local', rlcd=False,
                candidate_factors=frozen.get('candidate_factors', []), evaluator_changes=frozen.get('evaluator_changes', []),
                phase_path=str(Path(phase).resolve()),
                provider_closed=bool(backend is None or backend._closed), phase_sha256=sha(Path(phase) / 'phase.json'))
            exposure(phase, frozen, model, profile, spec, mode, report=report); private_json(root / 'summary.json', report)
    return report


def audit_isolated(run):
    """Apply an existing hash-bound human prose judgment without new inference."""
    root = Path(run); prior = read(root / 'summary.json'); inputs = read(root / 'case/review-audit-input.json')
    decision = read(root / 'case/review-decision.json')
    gate = review_gate(inputs['review'], inputs['spec'], inputs['oracle'],
        live_observation=inputs['observation'], prose_attestation=decision)
    report = {**prior, 'status': 'passed' if gate['accepted'] else 'failed',
        'failure_category': 'informed_review_verified' if gate['accepted'] else 'review_semantic_mismatch',
        'independent_prose_audit': gate, 'model_rerun': False, 'original_result_preserved': True}
    private_json(root / 'review-audit.json', report)
    phase = read(Path(prior['phase_path']) / 'phase.json')
    exposure(prior['phase_path'], phase, prior['model'], prior['profile'], inputs['spec'], prior['mode'], report=report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--cases-root', required=True); p.add_argument('--out', required=True); p.add_argument('--config'); p.add_argument('--supersedes'); p.add_argument('--previous-phase'); p.add_argument('--causal-change',action='append'); p.add_argument('--candidate-profile',choices=sorted(STEP_PROFILES),default='step-v1'); p.add_argument('--allowed-trial',action='append',help='Exact model/profile/case/mode tuple; repeated to permit only a bounded cohort'); p.add_argument('--mapping-prerequisite',choices=('arithmetic-development',)); p.add_argument('--candidate-factor',action='append',help='Explicit model-facing delta from predecessor; separate from evaluator corrections')
    p = sub.add_parser('run'); p.add_argument('--phase', required=True); p.add_argument('--out', required=True)
    p.add_argument('--model', choices=MODELS, required=True); p.add_argument('--profile', choices=PROFILES, required=True)
    p.add_argument('--case', required=True); p.add_argument('--mode', choices=('isolated', 'isolated-action', 'multistep','warm-loop'), default='warm-loop'); p.add_argument('--config')
    p = sub.add_parser('attest'); p.add_argument('--pending', required=True); p.add_argument('--decision', choices=('approve','reject'), required=True)
    p.add_argument('--reviewer', required=True); p.add_argument('--reason', required=True)
    p = sub.add_parser('audit-isolated'); p.add_argument('--run', required=True)
    args = vars(parser.parse_args()); command = args.pop('command')
    result = prepare(**args) if command == 'prepare' else sem.attest(**args) if command == 'attest' else audit_isolated(**args) if command == 'audit-isolated' else asyncio.run(run(**args))
    if command == 'prepare':
        print(json.dumps({'status': 'prepared', 'phase': str(Path(args['out']).resolve() / 'phase.json'),
                          'phase_sha256': sha(Path(args['out']) / 'phase.json'),
                          'cases': result['cases'], 'profiles': result['profiles'], 'renderings': len(result['renderings'])}, indent=2))
    else: print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
