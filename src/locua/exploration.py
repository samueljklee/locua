"""Small, read-only exploration ledger for the existing tool loop.

Records only exposed evidence. It neither chooses actions nor revives snapshot
authority. Equivalent recaptures do not turn a repeated search into progress.
"""
from copy import deepcopy
import hashlib
import json

from .progressive_ui import VERSION, exposed_controls


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _preview(value):
    if isinstance(value, str) and len(value) > 160:
        return {'preview': value[:160], 'complete': False, 'characters': len(value)}
    return deepcopy(value)


def _progress_capabilities(value):
    """Ignore capture-address churn only in the repetition comparison.

    Keep capability availability, requirements, reasons and proof contracts.
    Within a route's evidence.address, a valid opaque token/index is not UI
    progress. Missing, empty or wrongly typed address fields remain distinct,
    as do address kind and target identity. The original evidence is untouched;
    this projection is never used to bind or authorize an action.
    """
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result
    for route in result.values():
        evidence = route.get('evidence') if isinstance(route, dict) else None
        address = evidence.get('address') if isinstance(evidence, dict) else None
        if not isinstance(address, dict):
            continue
        for key in ('snapshot_id', 'element_token', 'control_id', 'ref'):
            if key in address:
                raw = address[key]
                address[key] = ({'opaque_address_type': 'nonempty_string'}
                                if isinstance(raw, str) and raw else {'invalid_address_value': raw})
        if 'element_index' in address:
            raw = address['element_index']
            address['element_index'] = ({'opaque_address_type': 'nonnegative_integer'}
                                       if type(raw) is int and raw >= 0 else {'invalid_address_value': raw})
    return result


class Exploration:
    def __init__(self):
        self.snapshots = {}
        self.pages = []
        self.controls = {}
        self.counts = {}
        self.exposures = set()
        self.needs = []
        self.blockers = []
        self.failed_reads = {}

    def retain(self, observation):
        controls = observation['controls']
        indices = {c['id']: i for i, c in enumerate(controls)}
        semantic = []
        for c in controls:
            semantics = c.get('semantics', {})
            proof = c.get('value_evidence', {})
            editor = c.get('editor', {})
            semantic.append({k: deepcopy(c.get(k)) for k in ('role', 'name', 'value', 'states')} | {
                'parent': indices.get(c.get('parent')),
                'actions': deepcopy(c.get('actions')),
                'capabilities': _progress_capabilities(c.get('capabilities')),
                'value_evidence': {k: deepcopy(proof.get(k)) for k in
                    ('kind', 'precision', 'exact_value_proven', 'plane', 'atomic_capture',
                     'committed_document_proven', 'saved_output_proven', 'structured_value_trimmed',
                     'possible_placeholder', 'markdown_is_exact_attribute_read', 'markdown_value')},
                'editor': {k: ({f: deepcopy(value.get(f)) for f in ('status', 'value', 'reason')}
                    if isinstance(value, dict) and ('status' in value or 'value' in value)
                    else deepcopy(value)) for k, value in editor.items()
                    if k in ('plane', 'atomic_capture', 'coherence', 'committed_document_proven',
                        'saved_file_proven', 'focused', 'value_settable', 'selected_range',
                        'selected_text', 'raw_value', 'raw_value_recheck')},
                'semantics': {k: deepcopy(semantics.get(k)) for k in
                              ('title', 'description', 'help', 'identifier', 'value_description')}})
        state = _hash({'target': observation['target'], 'controls': semantic,
                       'text': observation.get('text'), 'coverage': observation.get('coverage')})
        self.snapshots[observation['snapshot_id']] = {
            'state': state, 'indices': indices, 'target': deepcopy(observation['target']),
            'observed_at_ns': observation['observed_at_ns']}

    def set_needs(self, needs):
        if (not isinstance(needs, list) or len(needs) > 12 or
                any(not isinstance(n, str) or not n.strip() or len(n) > 256 for n in needs)):
            raise ValueError('needs must be at most12 nonempty strings, each at most256 characters')
        self.needs = deepcopy(needs)

    def record(self, name, args, result):
        if result.get('status') in ('refused', 'unavailable', 'uncertain'):
            self.blockers.append({'tool': name, 'code': result.get('code'),
                                  'reason': _preview(str(result.get('reason', 'Unknown failure')))})
        if name in ('locua_observe', 'locua_inspect', 'locua_windows', 'locua_apps'):
            key = _hash([name, args])
            if result.get('status') in ('refused', 'unavailable', 'uncertain'):
                failure = _hash([result.get('status'), result.get('code'), result.get('reason')])
                old = self.failed_reads.get(key, {})
                count = old.get('count', 0) + 1 if old.get('failure') == failure else 1
                self.failed_reads[key] = {'failure': failure, 'count': count}
                if count > 1:
                    return {'code': 'repeated_failed_read', 'equivalent_failure_count': count,
                        'new_information': False, 'current_state_proven': False,
                        'action_authority': False, 'reason':
                        'The same read has returned the same failure repeatedly. No new UI state is known. '
                        'Do not repeat this search indefinitely or infer that controls are absent. '
                        'Use an available explicit recovery operation only when its guards permit; '
                        'if activation is uncertain/refused or no supported recovery remains, report '
                        'the concrete blocker or ask for the necessary window-state change. '
                        'Do not retry uncertain task input.'}
            else:
                self.failed_reads.pop(key, None)
        page = result.get('overview', result)
        if not isinstance(page, dict) or page.get('version') != VERSION:
            return None
        snapshot = page['snapshot_id']
        saved = self.snapshots[snapshot]
        scope = deepcopy(page['coverage']['scope'])
        # Region IDs are content descriptors; native control IDs include the
        # capture ID. Normalize the latter only for progress comparison.
        normalized = deepcopy(scope)
        if 'control_id' in normalized:
            normalized['control_id'] = saved['indices'].get(normalized['control_id'])
        # Region IDs include the capture ID too. Use its captured root/ordinal
        # supplied in the view when available; otherwise strip only that exact
        # snapshot substring. This is a progress key, never a target binding.
        if isinstance(normalized.get('region_id'), str):
            normalized['region_id'] = normalized['region_id'].replace(snapshot, '<capture>')
        start = page['coverage']['previous_page_count']
        signature = _hash([saved['state'], page['operation'], normalized, start])
        self.counts[signature] = self.counts.get(signature, 0) + 1
        entries = []
        for index, item in enumerate(page.get('items', []), start):
            # Page indices are stable for the same semantic query. Include the
            # operation so control detail is new information after a list.
            entries.append(_hash([saved['state'], page['operation'], normalized, index]))
        if not entries:
            entries = [signature]  # A repeated empty search is still repetition.
        repeated = all(entry in self.exposures for entry in entries)
        self.exposures.update(entries)
        discovered = exposed_controls(page)
        reference = ({'window_id': result['window_id']}
                     if isinstance(result.get('window_id'), str) else {})
        for c in discovered:
            key = (saved['state'], saved['indices'].get(c['id']))
            previous = self.controls.get(key, {})
            self.controls[key] = previous | {k: _preview(c[k]) for k in
                ('id', 'role', 'name', 'value', 'region_id', 'parent') if k in c} | {
                'snapshot_id': snapshot, 'observed_at_ns': saved['observed_at_ns'],
                'potentially_stale': True, 'action_authority': False,
                'detail_inspected': previous.get('detail_inspected', False) or page['operation'] == 'control'} | reference
        row = {'snapshot_id': snapshot, 'operation': page['operation'], 'scope': scope,
               'page_start': start, 'returned_count': page['coverage']['returned_count'],
               'remaining_count': page['coverage']['remaining_count'],
               'continuation': page['coverage']['continuation'],
               'observed_at_ns': saved['observed_at_ns'], 'potentially_stale': True,
               'action_authority': False, 'equivalent_inspection_count': self.counts[signature]} | reference
        self.pages.append(row)
        feedback = {'equivalent_inspection_count': self.counts[signature],
                    'new_information': not repeated, 'potentially_stale': True,
                    'action_authority': False, 'unresolved_needs_count': len(self.needs),
                    'continuity': 'locua_status retrieves inspected pages, discovered controls and unresolved needs'}
        if repeated:
            feedback.update(code='repeated_inspection', reason=
                'This equivalent page was already inspected without new UI evidence. '
                'Use its continuation, a different region/role, control detail, or a supported '
                'reviewed navigation action. If evidence is still insufficient, record the '
                'specific need with locua_status operation=needs or report the blocker. '
                'Recapturing unchanged state does not discover another page.')
        return feedback

    def rows(self, operation):
        if operation == 'exploration':
            return deepcopy(self.pages)
        if operation == 'controls':
            return deepcopy(list(self.controls.values()))
        raise ValueError('Unknown exploration section')

    def summary(self):
        recent = []
        for row in self.pages[-2:]:
            recent.append({k: deepcopy(v) for k, v in row.items() if k != 'continuation'} |
                          {'has_continuation': row['continuation'] is not None})
            recent[-1]['scope'] = {k: _preview(v) for k, v in row['scope'].items()}
        return {'inspected_pages': len(self.pages), 'discovered_controls': len(self.controls),
                'recent': recent, 'unresolved_needs': deepcopy(self.needs),
                'needs_author': 'model; not a replacement for the original request',
                'recent_blockers': deepcopy(self.blockers[-3:]),
                'potentially_stale': True, 'action_authority': False,
                'more': 'locua_status operation=exploration or controls; paginate with start/limit'}
