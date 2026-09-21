"""Short public references for an unchanged, privately issued action catalog.

Pure representation only: aliases neither select actions nor authorize input.
The owner supplies a unique monotonically increasing capture ordinal, retains
the returned private map with that capture, and uses the ordinary executor to
validate its original descriptor and observation before dispatch.
"""
from copy import deepcopy
import json


class ActionReferenceError(ValueError):
    pass


def _string(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def _native_target(target):
    if not isinstance(target, dict) or set(target) not in (
            {'pid', 'window_id'}, {'pid', 'window_id', 'session'}):
        return False
    if any(type(target[k]) is not int or target[k] <= 0 for k in ('pid', 'window_id')):
        return False
    return 'session' not in target or _string(target['session'])


def public_catalog(actions, snapshot_ref):
    """Return (public actions, alias -> original private descriptor).

    Only the public ``id`` field changes. Labels, values, kinds, order, target,
    control IDs and all competitors remain unchanged. This current DesktopTools
    adapter accepts native PID/window targets only. The private map must never
    replace the driver's issued-catalog authority or its original hashes.
    """
    if type(snapshot_ref) is not int or snapshot_ref <= 0:
        raise ActionReferenceError('snapshot_ref must be a positive integer capture ordinal')
    if not isinstance(actions, list):
        raise ActionReferenceError('actions must be a complete issued catalog array')
    seen = set()
    expected_snapshot = expected_target = None
    # Validate the complete catalog before publishing any aliases.
    for index, action in enumerate(actions, 1):
        if not isinstance(action, dict):
            raise ActionReferenceError(f'Action {index} must be an issued descriptor object')
        if any(not _string(action.get(k)) for k in ('id', 'kind', 'control_id', 'snapshot_id')):
            raise ActionReferenceError(f'Action {index} has missing or malformed identity fields')
        if action['id'] in seen:
            raise ActionReferenceError('Duplicate source action ID')
        seen.add(action['id'])
        if not _native_target(action.get('target')):
            raise ActionReferenceError(f'Action {index} has an invalid native target')
        if 'requires_value' in action and type(action['requires_value']) is not bool:
            raise ActionReferenceError(f'Action {index} has an invalid requires_value flag')
        try:
            json.dumps(action, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ActionReferenceError(f'Action {index} is not finite JSON evidence') from error
        if index == 1:
            expected_snapshot, expected_target = action['snapshot_id'], action['target']
        elif action['snapshot_id'] != expected_snapshot or action['target'] != expected_target:
            raise ActionReferenceError('Catalog mixes snapshots or exact native targets')
    public, private = [], {}
    for index, action in enumerate(actions, 1):
        alias = f'action:{snapshot_ref}:{index}'
        row = deepcopy(action)
        row['id'] = alias
        public.append(row)
        private[alias] = deepcopy(action)
    return public, private
