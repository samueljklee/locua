"""Explicit Excel object-model adapter; no UI recipes or implicit app discovery.

The native transport targets one already-running PID and exact workbook identity.
Cell values are committed-document evidence. Neither editor focus nor disk save
is inferred from them. A separate saved OOXML read proves file serialization.
Automation permission is independent of Cua Accessibility/Screen Recording.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
import uuid

SCHEMA = 'locua.excel.events.v1'
TARGET_FIELDS = {'pid', 'executable', 'workbook_name', 'workbook_full_name', 'sheet'}


class ExcelError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _position(address):
    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', address) if isinstance(address, str) else None
    if match is None:
        raise ExcelError('Require an explicit A1 cell address')
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    row = int(match[2])
    if row > 1048576 or column > 16384:
        raise ExcelError('Cell outside worksheet limits')
    return row, column


def _address(row, column):
    letters = ''
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters + str(row)


def range_addresses(reference):
    if not isinstance(reference, str):
        raise ExcelError('Explicit A1 range required')
    parts = reference.split(':')
    if len(parts) not in (1, 2):
        raise ExcelError('Only one contiguous A1 range is supported')
    (r1, c1), (r2, c2) = _position(parts[0]), _position(parts[-1])
    if r2 < r1 or c2 < c1 or (r2-r1+1)*(c2-c1+1) > 64:
        raise ExcelError('Range is inverted or exceeds the explicit 64-cell bound')
    return [_address(r, c) for r in range(r1, r2+1) for c in range(c1, c2+1)]


def _typed(value, *, desired=False):
    if not isinstance(value, dict) or set(value) != {'type', 'value'}:
        raise ExcelError('Require an explicit typed scalar')
    kind, scalar = value['type'], value['value']
    valid = (kind == 'string' and type(scalar) is str
             or kind == 'boolean' and type(scalar) is bool
             or kind == 'number' and type(scalar) in (int, float) and math.isfinite(scalar)
             or not desired and kind == 'blank' and scalar is None
             or desired and kind == 'formula' and type(scalar) is str and scalar.startswith('='))
    if not valid:
        raise ExcelError('Unsupported typed scalar; unknown/date/error/clear need a separate capability')
    if kind == 'number' and (abs(scalar) > 2**53 if type(scalar) is int else False):
        raise ExcelError('Integer cannot be represented exactly by Excel binary64 transport')
    if desired and kind == 'string' and scalar.startswith('='):
        raise ExcelError('Formula-like literal has no proven text-only route')
    return deepcopy(value)


def _boolean(fact, expected):
    return isinstance(fact, dict) and fact.get('type') == 'boolean' and fact.get('value') is expected


class AppleEventExecutor:
    """Bounded explicit helper, with a pinned binary; never builds or grants."""
    def __init__(self, helper_path, sha256):
        self.path = Path(helper_path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ExcelError('Explicit nonsymlink helper path required')
        self.sha256 = sha256

    def __call__(self, request):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ExcelError('Excel transport binary differs from configured fingerprint')
        if len(_canonical(request).encode()) > 1_048_576:
            raise ExcelError('Excel transport request exceeds bound')
        try:
            result = subprocess.run([str(self.path)], input=_canonical(request), text=True,
                                    capture_output=True, timeout=14, check=False)
        except subprocess.TimeoutExpired:
            return {'schema': SCHEMA, 'status': 'effect_unknown' if request['operation'] in ('set_cell', 'save') else 'unavailable',
                    'action_started': None, 'reason': 'transport_timeout_no_retry'}
        if result.returncode or len(result.stdout.encode()) > 1_048_576:
            return {'schema': SCHEMA, 'status': 'effect_unknown' if request['operation'] in ('set_cell', 'save') else 'unavailable',
                    'action_started': None, 'reason': 'transport_failed_no_retry'}
        try:
            response = json.loads(result.stdout)
            _canonical(response)
        except (ValueError, TypeError):
            return {'schema':SCHEMA, 'status':'effect_unknown' if request['operation'] in ('set_cell','save') else 'unavailable', 'reason':'malformed_response_no_retry'}
        if not isinstance(response, dict) or response.get('schema') != SCHEMA:
            return {'schema':SCHEMA, 'status':'effect_unknown' if request['operation'] in ('set_cell','save') else 'unavailable', 'reason':'unsupported_response_no_retry'}
        return response


class ExcelAdapter:
    """Caller-supplied exact scope. Executor can be native or a recorded test peer."""
    def __init__(self, target, executor, *, max_age_ns=30_000_000_000):
        if not isinstance(target, dict) or set(target) != TARGET_FIELDS:
            raise ExcelError('Exact PID, executable, workbook name/full name and sheet required')
        if type(target['pid']) is not int or not 0 < target['pid'] < 2**31:
            raise ExcelError('Invalid Excel PID')
        if any(type(target[k]) is not str or not target[k] or '\x00' in target[k] for k in TARGET_FIELDS-{'pid'}):
            raise ExcelError('Target identity fields must be nonempty strings')
        if not Path(target['executable']).is_absolute():
            raise ExcelError('Absolute Excel executable required')
        self.target, self.executor = deepcopy(target), executor
        self.max_age_ns = max_age_ns
        self._observations = {}

    def doctor(self):
        # Same helper identity performs the no-prompt check and later Apple events.
        return self.executor({'operation': 'doctor', 'pid': self.target['pid'], 'executable': self.target['executable']})

    def _request(self, operation, reference, **extra):
        range_addresses(reference)
        return self.executor({'operation': operation, **deepcopy(self.target), 'range': reference, **extra})

    def _normalize(self, raw, reference, *, operation='observe'):
        if raw.get('schema') != SCHEMA or raw.get('status') != 'observed' or raw.get('operation') != operation:
            raise ExcelError('Excel observation unavailable: ' + str(raw.get('status')))
        if any(_canonical(raw.get(k)) != _canonical(v) for k, v in self.target.items()) or raw.get('range') != reference:
            raise ExcelError('Excel response crossed the exact target or range')
        expected = range_addresses(reference)
        cells = raw.get('cells')
        if not isinstance(cells, list) or len(cells) != len(expected) or any(not isinstance(c, dict) for c in cells) or [c.get('address') for c in cells] != expected:
            raise ExcelError('Excel returned incomplete, reordered or duplicate cell identities')
        for cell in cells:
            if cell.get('plane') != 'committed_document':
                raise ExcelError('Cell evidence plane mismatch')
        capture_time, capture_finish = raw.get('capture_started_at_ns'), raw.get('capture_finished_at_ns')
        receipt = time.time_ns()
        if (type(capture_time) is not int or type(capture_finish) is not int
                or not 0 < capture_time <= capture_finish <= receipt
                or capture_finish - capture_time > 14_000_000_000
                or receipt - capture_time > self.max_age_ns):
            raise ExcelError('Excel source capture times are missing, stale, unordered or outside bounds')
        observation = {'schema': 'locua.excel.cells.v1', 'observation_id': uuid.uuid4().hex,
                       'observed_at_ns': capture_time, 'target': deepcopy(self.target),
                       'range': reference, 'cells': deepcopy(cells),
                       'coverage': {'complete': True, 'scope': reference},
                       'plane': 'committed_document', 'atomic_capture': False,
                       'focus_proven': False, 'editor_buffer_proven': False, 'saved_file_proven': False,
                       'source': deepcopy(raw)}
        self._observations[observation['observation_id']] = deepcopy(observation)
        while len(self._observations) > 16:
            self._observations.pop(next(iter(self._observations)))
        return observation

    def observe(self, reference):
        return self._normalize(self._request('observe', reference), reference)

    def _owned(self, observation):
        if not isinstance(observation, dict) or _canonical(self._observations.get(observation.get('observation_id'))) != _canonical(observation):
            raise ExcelError('Observation is foreign, altered or already consumed')
        age = time.time_ns() - observation['observed_at_ns']
        if not 0 <= age <= self.max_age_ns:
            raise ExcelError('Excel observation is stale')

    def resolve_label(self, observation, label, relation='right'):
        """Caller chooses a structural relation within the entire observed range."""
        self._owned(observation)
        if type(label) is not str or not label or relation not in ('right', 'below', 'unique_nonblank_right'):
            raise ExcelError('Explicit label and supported structural relation required')
        cells = observation['cells']
        if any(c.get('value_stable') is not True or c.get('value2', {}).get('type') == 'unknown'
               or not (_boolean(c.get('has_formula'), True) or _boolean(c.get('has_formula'), False)) for c in cells):
            raise ExcelError('Unknown cell evidence prevents unique label binding in this range')
        labels = [c for c in cells if _canonical(c.get('value2')) == _canonical({'type': 'string', 'value': label})
                  and c.get('value_stable') is True and _boolean(c.get('has_formula'), False)]
        if len(labels) != 1:
            raise ExcelError('Label is missing or ambiguous in the declared range')
        row, column = _position(labels[0]['address'])
        if relation == 'unique_nonblank_right':
            peers = [c for c in cells if _position(c['address'])[0] == row and _position(c['address'])[1] > column]
            if any(c.get('value_stable') is not True or c.get('value2', {}).get('type') == 'unknown' for c in peers):
                raise ExcelError('Unknown peer prevents unique structural binding')
            candidates = [c for c in peers if _boolean(c.get('has_formula'), True)
                          or c.get('value2', {}).get('type') != 'blank'
                          and c.get('value2') != {'type':'string','value':''}]
            if len(candidates) != 1:
                raise ExcelError('Rightward populated cell is missing or ambiguous')
            return candidates[0]['address']
        result = _address(row + (relation == 'below'), column + (relation == 'right'))
        if result not in {c['address'] for c in cells}:
            raise ExcelError('Adjacent cell lies outside declared observation scope')
        return result

    def set_cell(self, observation, address, desired, *, authorize_write=False):
        self._owned(observation)
        if authorize_write is not True:
            raise ExcelError('A cell write must be explicitly authorized')
        desired = _typed(desired, desired=True)
        cells = [c for c in observation['cells'] if c['address'] == address]
        if len(cells) != 1 or cells[0].get('value_stable') is not True:
            raise ExcelError('One stable observed cell is required')
        source = observation['source']
        if not _boolean(source.get('workbook_read_only'), False) or not _boolean(source.get('sheet_protected'), False):
            raise ExcelError('Workbook/sheet writability is not positively established')
        if not _boolean(cells[0].get('merged'), False) or not _boolean(cells[0].get('array_formula_member'), False):
            raise ExcelError('Merged/array cell membership is not safely writable as one cell')
        before = {k: deepcopy(cells[0][k]) for k in ('value2', 'formula', 'has_formula')}
        _typed(before['value2']); _typed(before['formula']); _typed(before['has_formula'])
        if not (_boolean(before['has_formula'], True) or _boolean(before['has_formula'], False)):
            raise ExcelError('Cell formula membership is unknown')
        # Consume before dispatch; unknown effects require a new independent observation.
        del self._observations[observation['observation_id']]
        raw = self._request('set_cell', address, desired=desired, expected_before=before, authorize_write=True)
        if raw.get('status') != 'observed':
            return {'status': raw.get('status', 'effect_unknown'), 'source': raw, 'retry_allowed': False}
        try:
            after = self._normalize(raw, address, operation='set_cell')
        except ExcelError as exc:
            return {'status':'effect_unknown', 'reason':str(exc), 'source':raw, 'retry_allowed':False}
        cell = after['cells'][0]
        if desired['type'] == 'formula':
            matched = _boolean(cell.get('has_formula'), True) and cell.get('formula') == {'type': 'string', 'value': desired['value']}
        else:
            actual = cell.get('value2', {})
            same = (actual.get('type') == desired['type'] == 'number' and type(actual.get('value')) in (int, float) and actual['value'] == desired['value']
                    or _canonical(actual) == _canonical(desired))
            matched = _boolean(cell.get('has_formula'), False) and same
        return {'status': 'committed_verified' if matched and cell.get('value_stable') is True else 'effect_unverified',
                'observation': after, 'saved_file_proven': False, 'editor_buffer_proven': False, 'retry_allowed': False}

    def save(self, observation, *, authorize_save_workbook=False):
        self._owned(observation)
        if authorize_save_workbook is not True:
            raise ExcelError('Saving all pending changes in this workbook requires explicit authorization')
        del self._observations[observation['observation_id']]
        raw = self._request('save', observation['range'], authorize_save_workbook=True)
        identity_ok = (raw.get('schema') == SCHEMA and raw.get('operation') == 'save'
                       and all(_canonical(raw.get(k)) == _canonical(v) for k, v in self.target.items())
                       and raw.get('range') == observation['range'])
        return {'status': 'save_acknowledged' if identity_ok and raw.get('status') == 'observed' and raw.get('action_started') is True else 'effect_unknown',
                'source': raw, 'saved_file_proven': False, 'retry_allowed': False}


def verify_saved_addresses(path, sheet, expected):
    """Independent read-only OOXML evidence; no live/editor identity promotion."""
    from .prototype.spreadsheets import snapshot_workbook
    snapshot = snapshot_workbook(path)
    sheets = [s for s in snapshot['sheets'] if s['name'] == sheet]
    if len(sheets) != 1 or not isinstance(expected, dict) or not expected:
        raise ExcelError('Exact sheet and nonempty addressed expectations required')
    results = []
    for address, value in expected.items():
        _position(address); value = _typed(value, desired=value.get('type') == 'formula')
        cell = sheets[0]['cells'].get(address)
        if value['type'] == 'formula':
            passed = bool(cell and cell.get('formula') and cell['formula']['text'] == value['value'][1:])
        else:
            stored = cell.get('stored') if cell else {'type': 'blank', 'value': None}
            if cell and cell.get('formula') is not None:
                passed = False
            elif value['type'] == 'number' and stored and stored['type'] == 'number':
                passed = Decimal(stored['value']) == Decimal(str(value['value']))
            else:
                passed = _canonical(stored) == _canonical(value)
        results.append({'address': address, 'status': 'pass' if passed else 'fail'})
    return {'status': 'pass' if all(r['status']=='pass' for r in results) else 'fail', 'plane': 'saved_file',
            'sha256': snapshot['sha256'], 'results': results, 'formula_results_proven': False,
            'editor_buffer_proven': False, 'native_cell_identity_proven': False}
