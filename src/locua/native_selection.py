"""Opt-in native collection selection for the desktop preview, not RLCD.

The pinned Cua macOS click implementation selects list-like AX containers and
checks AXSelected. A schema or a role alone does not establish that route.
Original AX actions and descendants remain intact and discoverable.
"""
from copy import deepcopy

from .engine.prototype.perception import CUA_MACOS_0_28_2_CONTRACT, _native_address_bound
from .engine.prototype.core import _ancestors, _semantic_identity

ROLES = {'AXRow', 'AXCell', 'AXListItem', 'AXImage'}
CONTRACT = 'locua.native.collection_selection.v1'


def enrich(observation, schemas):
    click = schemas.get('click', {}).get('properties', {})
    if not {'pid', 'window_id', 'element_token'} <= set(click):
        return observation
    children = {}
    for c in observation['controls']:
        children.setdefault(c.get('parent'), []).append(c)
    for c in observation['controls']:
        source = c.get('source', {})
        if (source.get('native_contract') != CUA_MACOS_0_28_2_CONTRACT
                or not _native_address_bound(c, observation['handles'].get(c['id']))
                or c.get('role') not in ROLES or 'AXPress' in c.get('actions', [])
                or type(c.get('states', {}).get('selected')) is not bool):
            continue
        # Derive only an unambiguous direct static label, with its provenance.
        # Never derive a row identity from a mutable editor or a test answer.
        labels = [child for child in children.get(c['id'], [])
                  if child.get('role') == 'AXStaticText' and child.get('name')]
        if not c.get('name') and len(labels) == 1 and len(labels[0]['name']) <= 512:
            c['name'] = labels[0]['name']
            c['name_evidence'] = {'kind': 'single_direct_static_child',
                'control_id': labels[0]['id'], 'original_name': source.get('node', {}).get('label')}
        c.setdefault('capabilities', {})['selection'] = {
            'contract': CONTRACT, 'route': 'cua_click_collection_selection',
            'eligible': c.get('states', {}).get('enabled') is not False
                        and c.get('states', {}).get('disabled') is not True,
            'source': 'platform-macos/src/input/ax_actions.rs:select_nearest_container',
            'dispatch': 'press', 'property': 'selected', 'value': True,
            'requires': ['fresh_exact_target', 'reviewed_scope', 'independent_selected_readback'],
        }
    return observation


def eligible(control):
    cap = control.get('capabilities', {}).get('selection', {})
    states = control.get('states', {})
    return (cap.get('contract') == CONTRACT and cap.get('eligible') is True
            and control.get('role') in ROLES and type(states.get('selected')) is bool
            and states.get('enabled') is not False and states.get('disabled') is not True)


def readback_identity(control, observation):
    # Navigation may change a window title. The target pid/window is separately
    # fixed; retain all other row and ancestry semantics for independent readback.
    ancestors = []
    for parent in _ancestors(control, observation):
        if parent.get('role') == 'AXWindow':
            ancestors.append({'role': 'AXWindow', 'identifier': parent.get('semantics', {}).get('identifier')})
        else:
            ancestors.append(_semantic_identity(parent))
    return {'control': deepcopy(_semantic_identity(control)), 'ancestors': ancestors}
