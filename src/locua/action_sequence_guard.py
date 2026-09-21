"""Conservative captured-structure checks for optional native action sequences.

Pure inspection only. A successful check is not action authority, complete app
coverage, persistent AX identity, or proof that same-layout navigation did not
occur. The caller must preserve reviewed constraints and dispatch every step
through the ordinary fresh single-action guards.
"""
from __future__ import annotations

from copy import deepcopy
import time

from .engine.prototype.core import _identity, _signature
from .engine.prototype.perception import _native_address_bound, validate_observation
from .engine.prototype.regions import catalog_regions
from .goal_verification import _native_value_label_proven

VERSION = 'captured-native-sequence-guard-v1'
_SEMANTICS = ('title', 'description', 'help', 'identifier', 'value_description')
_EDITORS = {'AXTextArea', 'AXTextField', 'AXSearchField', 'AXComboBox'}
_READOUTS = {'AXStaticText', 'AXHeading'}
_NAVIGATION = {'AXMenu', 'AXMenuItem', 'AXMenuBar', 'AXMenuBarItem', 'AXMenuButton',
               'AXPopUpButton', 'AXTabGroup', 'AXTab', 'AXToolbar', 'AXOutline',
               'AXBrowser', 'AXSheet', 'AXDialog', 'AXWindow', 'AXLink', 'AXRadioButton'}
_NAV_REGION_KINDS = {'dialog', 'menu_bar', 'menu_root', 'toolbar', 'navigation'}


class SequenceGuardError(ValueError):
    """A bounded refusal; callers must not retry an already issued action."""


def _validated(observation, target, *, fresh=False):
    try:
        validate_observation(observation, expected_target=target,
                             now_ns=time.time_ns(), max_age_s=30 if fresh else None)
        if observation.get('kind') != 'native_window_state':
            raise ValueError('native only')
        coverage = observation.get('coverage', {})
        if coverage.get('parse_warnings') or coverage.get('unresolved_parent_ids'):
            raise ValueError('ambiguous rendered capture')
        catalog = catalog_regions(observation)
        if catalog['coverage']['flat_fallback'] or catalog['coverage']['warnings']:
            raise ValueError('unresolved hierarchy')
        controls = observation['controls']
        positions = {c['id']: i for i, c in enumerate(controls)}
        roots = [c for c in controls if c.get('role') == 'AXWindow' and c.get('parent') is None]
        if len(roots) != 1:
            raise ValueError('one captured native window required')
        return controls, positions, catalog
    except (ValueError, KeyError, TypeError) as error:
        raise SequenceGuardError('sequence_observation_invalid_or_unbound') from error


def remap_control(original_observation, original_control_id, latest_observation):
    """Uniquely resolve an unchanged original action target in retained evidence.

    No review-age expiry: this is a selector, not dispatch authorization. The
    next ordinary _act must still recapture and validate current capabilities.
    """
    target = original_observation.get('target')
    original, positions, _ = _validated(original_observation, target)
    latest, _, _ = _validated(latest_observation, target)
    if latest_observation['observed_at_ns'] < original_observation['observed_at_ns']:
        raise SequenceGuardError('sequence_capture_regressed')
    if (latest_observation['snapshot_id'] == original_observation['snapshot_id']
            and latest_observation != original_observation):
        raise SequenceGuardError('sequence_snapshot_content_changed')
    if original_control_id not in positions:
        raise SequenceGuardError('sequence_original_control_absent')
    control = original[positions[original_control_id]]
    if not _native_address_bound(control, original_observation.get('handles', {}).get(control['id'])):
        raise SequenceGuardError('sequence_original_handle_unbound')
    identity, signature = _identity(control, original_observation), _signature(control, original_observation)
    # Match identity first, not signature/value, so changed twins cannot make
    # an ambiguous semantic target appear unique.
    peers = [c for c in latest if _identity(c, latest_observation) == identity]
    if len(peers) != 1:
        raise SequenceGuardError('sequence_target_absent_or_ambiguous')
    peer = peers[0]
    if (_signature(peer, latest_observation) != signature
            or not _native_address_bound(peer, latest_observation.get('handles', {}).get(peer['id']))):
        raise SequenceGuardError('sequence_target_signature_or_handle_changed')
    return peer['id']


def _semantic(control):
    return {'name': control.get('name'),
            **{k: control.get('semantics', {}).get(k) for k in _SEMANTICS}}


def _states(control):
    return {k: deepcopy(v) for k, v in control.get('states', {}).items() if k != 'focused'}


def _geometry(control):
    return {'bounds': control.get('bounds'), 'frame': control.get('source', {}).get('node', {}).get('frame')}


def _capability(control, observation):
    node = control.get('source', {}).get('node', {})
    return {'actions': control.get('actions', []),
            'addressed': control['id'] in observation.get('handles', {}),
            'visibility': node.get('visibility'),
            'enabled': control.get('states', {}).get('enabled'),
            'editable': control.get('states', {}).get('editable'),
            'value_settable': control.get('states', {}).get('value_settable'),
            'value_precision': control.get('value_evidence', {}).get('precision'),
            'exact_value_proven': control.get('value_evidence', {}).get('exact_value_proven')}


def _region_shape(catalog, positions):
    by_id = {r['id']: r for r in catalog['regions']}
    def root(rid):
        return positions.get(by_id[rid]['root_control_id']) if rid is not None else None
    return [{'kind': r['kind'], 'root': positions.get(r['root_control_id']),
             'parent': root(r['parent_region_id']),
             'children': [root(rid) for rid in r['child_region_ids']],
             'members': [positions[cid] for cid in r['control_ids']]}
            for r in catalog['regions']]


def check_transition(before, after, *, acted_control_id, planned_control_ids=()):
    """Check a completed step against full captured structure and future targets.

    planned_control_ids refer to still-pending targets in BEFORE, including any
    repeated future target. Their identity and complete signature cannot change.
    Nonplanned, nonstructural leaf semantic replacement is allowed only in the
    same role/slot/geometry/capability; it is reported, never identity proof.
    """
    evidence = {'version': VERSION, 'creates_authority': False,
                'global_coverage_proven': False, 'same_layout_navigation_excluded': False,
                'changes': []}
    def fail(reason, index=None):
        return {'matched': False, 'reason': reason, 'evidence': {
            **evidence, **({'control_position': index} if index is not None else {})}}
    try:
        target = before.get('target')
        old, old_pos, old_catalog = _validated(before, target)
        new, new_pos, new_catalog = _validated(after, target, fresh=acted_control_id is not None)
        if (after['snapshot_id'] == before['snapshot_id']
                or after['observed_at_ns'] <= before['observed_at_ns']):
            return fail('sequence_post_capture_not_independent')
        if acted_control_id is not None and acted_control_id not in old_pos:
            return fail('sequence_acted_control_absent')
        if acted_control_id is not None and not _native_address_bound(
                old[old_pos[acted_control_id]], before.get('handles', {}).get(acted_control_id)):
            return fail('sequence_acted_handle_unbound')
        if (not isinstance(planned_control_ids, (list, tuple, set, frozenset))
                or any(not isinstance(cid, str) or cid not in old_pos for cid in planned_control_ids)):
            return fail('sequence_planned_control_unbound')
        if len(old) != len(new):
            return fail('sequence_control_topology_changed')
        coverage_keys = ('complete', 'scope', 'hierarchy_scope', 'structured_actionable_only', 'static_markdown_retained')
        if any(before.get('coverage', {}).get(k) != after.get('coverage', {}).get(k) for k in coverage_keys):
            return fail('sequence_capture_coverage_changed')
        # Ordered structural slots are a conservative continuity check only;
        # action targets are independently remapped by immutable identity.
        topology = lambda rows, pos: [(c['role'], pos.get(c.get('parent'))) for c in rows]
        if topology(old, old_pos) != topology(new, new_pos):
            return fail('sequence_control_topology_changed')
        if _region_shape(old_catalog, old_pos) != _region_shape(new_catalog, new_pos):
            return fail('sequence_region_topology_changed')
        if acted_control_id is None:
            # Predispatch seam: no mutation has been authorized yet. Repeated
            # captures and focus alone may differ; no value/label/state change
            # is explained by an input that has not happened.
            if any(_signature(a, before) != _signature(b, after) for a, b in zip(old, new)):
                return fail('sequence_predispatch_state_changed')
            return {'matched': True, 'reason': 'captured_predispatch_state_unchanged',
                    'evidence': {**evidence, 'before_snapshot_id': before['snapshot_id'],
                                 'after_snapshot_id': after['snapshot_id']}}
        children = {c.get('parent') for c in old if c.get('parent') is not None}
        roots = {r['root_control_id'] for r in old_catalog['regions']}
        region_kinds = {r['id']: r['kind'] for r in old_catalog['regions']}
        planned = set(planned_control_ids)
        for index, (a, b) in enumerate(zip(old, new)):
            if _geometry(a) != _geometry(b):
                return fail('sequence_geometry_changed', index)
            if _capability(a, before) != _capability(b, after):
                return fail('sequence_capability_changed', index)
            if a['id'] in planned:
                try:
                    if remap_control(before, a['id'], after) != b['id']:
                        return fail('sequence_planned_target_moved', index)
                except SequenceGuardError:
                    return fail('sequence_planned_target_changed', index)
            structural = a['id'] in children or a['id'] in roots
            navigation = (a['role'] in _NAVIGATION or
                region_kinds[old_catalog['memberships'][a['id']]] in _NAV_REGION_KINDS)
            acted = a['id'] == acted_control_id
            sa, sb = _semantic(a), _semantic(b)
            state_changed = _states(a) != _states(b)
            value_changed = (a.get('value'), a.get('display_value')) != (b.get('value'), b.get('display_value'))
            if structural or navigation:
                if sa != sb or state_changed or value_changed:
                    return fail('sequence_structural_or_navigation_state_changed', index)
                continue
            if sa != sb:
                if a['role'] in _EDITORS:
                    # Only the acted editor may change its proven value label.
                    if not (acted and a['id'] not in planned
                            and _native_value_label_proven(a, before)
                            and _native_value_label_proven(b, after)
                            and {k:v for k,v in sa.items() if k != 'name'} ==
                                {k:v for k,v in sb.items() if k != 'name'}):
                        return fail('sequence_editor_identity_changed', index)
                elif a['id'] in planned or state_changed or value_changed:
                    return fail('sequence_leaf_identity_changed', index)
                evidence['changes'].append({'position': index, 'kind': 'acted_editor_value_label' if a['role'] in _EDITORS else 'unplanned_leaf_semantic_replacement',
                                            'same_ax_object_proven': False})
            if state_changed:
                keys = {k for k in set(_states(a)) | set(_states(b)) if _states(a).get(k) != _states(b).get(k)}
                # Expanded/visibility/selection changes may signal navigation
                # even when the new surface was not captured. Only the direct
                # checkbox state effect has a narrowly supported exception.
                if not (acted and a['role'] == 'AXCheckBox' and keys <= {'checked'}):
                    return fail('sequence_acted_state_changed' if acted else 'sequence_unacted_state_changed', index)
            if value_changed and not (acted or a['role'] in _READOUTS and not a.get('actions')):
                return fail('sequence_unacted_value_changed', index)
            if value_changed:
                evidence['changes'].append({'position': index, 'kind': 'acted_value' if acted else 'read_only_display_value'})
            if state_changed:
                evidence['changes'].append({'position': index, 'kind': 'acted_state'})
        evidence.update(before_snapshot_id=before['snapshot_id'], after_snapshot_id=after['snapshot_id'],
                        control_count=len(old), region_count=len(old_catalog['regions']))
        return {'matched': True, 'reason': 'captured_structure_and_planned_targets_unchanged', 'evidence': evidence}
    except (SequenceGuardError, ValueError, KeyError, TypeError):
        return fail('sequence_observation_invalid_or_unbound')
