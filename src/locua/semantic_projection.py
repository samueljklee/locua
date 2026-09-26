"""Opt-in semantic views over the existing guarded, snapshot-bound interface.

This module projects captured facts; it does not select controls, infer section
membership, create driver handles, change approvals, or verify task outcomes.
All requests still pass through ModelInterface and the canonical owner tools.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib

from .model_interface import ModelInterface, InterfaceError, _json
from .progressive_ui import _fragments


VERSION = 'semantic-v1'
PAGE_BYTES = 8000
FIELD_BYTES = 256
NEIGHBOR_RADIUS = 3
EXPLORED_CONTROLS = 12
EXPLORED_REGIONS = 8


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class SemanticModelInterface(ModelInterface):
    """Same public inputs and private guards; semantic outputs and retained notes."""

    version = VERSION

    def __init__(self, owner):
        super().__init__(owner)
        self._projection_page = None
        self._explored_controls = OrderedDict()
        self._explored_regions = OrderedDict()
        self._region_catalogs = {}

    def _regions(self, o):
        from .engine.prototype.regions import catalog_regions
        sid = o['snapshot_id']
        if sid not in self._region_catalogs:
            self._region_catalogs[sid] = catalog_regions(o)
        return self._region_catalogs[sid]

    def _field(self, value, target, *, limit=FIELD_BYTES):
        encoded = _json(value).encode()
        if len(encoded) <= limit:
            return deepcopy(value)
        return {'deferred': True, 'json_bytes': len(encoded),
                'exact_value_in_this_view': False,
                'next': {'view': self.ref('v', self.resolve(target, 'c')[0]), 'target': target}}

    def _position(self, o, c):
        result = {'captured_index': next(n for n, row in enumerate(o['controls']) if row['id'] == c['id'])}
        source = c.get('source') or {}
        if source.get('kind') in ('native', 'native_markdown'):
            line = source.get('markdown_line_number', source.get('line_number'))
            if type(line) is int and line > 0:
                result['rendered_line'] = line
        return result

    def _semantic_fields(self, o, cid):
        c = self.owner._find(o, cid); sid = o['snapshot_id']
        target = self.ref('c', [sid, cid]); sem = c.get('semantics') or {}
        result = {'target': target, 'role': c['role'], 'name': deepcopy(c.get('name')),
                  'value': deepcopy(c.get('value')), 'states': deepcopy(c.get('states', {})),
                  'operations': sorted({a['kind'] for a in self.owner._actions[sid].values() if a['control_id'] == cid})}
        for key in ('help', 'identifier', 'title', 'description', 'value_description'):
            if sem.get(key) is not None and (key in ('help', 'identifier') or sem[key] != c.get('name')):
                result[key] = deepcopy(sem[key])
        parent = next((p for p in o['controls'] if p['id'] == c.get('parent')), None)
        if parent is not None:
            result['parent'] = {'target': self.ref('c', [sid, parent['id']]),
                                'role': parent['role'], 'name': deepcopy(parent.get('name'))}
        result['position'] = self._position(o, c)
        region = self._regions(o)['memberships'].get(cid)
        if region:
            result['region'] = self.ref('r', [sid, region])
        if c.get('bounds') is not None:
            result['bounds'] = {k: deepcopy(c['bounds'][k]) for k in
                                ('x', 'y', 'width', 'height', 'coordinate_space') if k in c['bounds']}
        proof = c.get('value_evidence') or {}
        if proof:
            result['value_evidence'] = {k: deepcopy(proof[k]) for k in
                ('plane', 'precision', 'exact_value_proven') if k in proof}
        editor = c.get('editor') or {}
        if editor:
            result['editor'] = {k: deepcopy(editor[k]) for k in
                ('focused', 'value_settable', 'selected_range', 'selected_text') if k in editor}
        return result

    def _control(self, o, cid):
        fields = self._semantic_fields(o, cid)
        target = fields['target']
        return {k: self._field(v, target) for k, v in fields.items()}

    def _neighborhood(self, o, cid):
        c = self.owner._find(o, cid)
        peers = [p for p in o['controls'] if p.get('parent') == c.get('parent')]
        positions = {p['id']: self._position(o, p) for p in peers}
        ordered = peers
        basis = 'captured_control_array_order'
        unknown_order = 0
        if 'rendered_line' in positions[cid]:
            ordered = [p for p in peers if 'rendered_line' in positions[p['id']]]
            unknown_order = len(peers) - len(ordered)
            ordered.sort(key=lambda p: (positions[p['id']]['rendered_line'], positions[p['id']]['captured_index']))
            basis = 'captured_rendered_tree_order'
        index = next(n for n, p in enumerate(ordered) if p['id'] == cid)
        selected = [(p, n-index) for n, p in enumerate(ordered)
                    if n != index and abs(n-index) <= NEIGHBOR_RADIUS]
        rows = []
        for p, offset in selected:
            full = self._semantic_fields(o, p['id']); target = full['target']
            row = {'kind': 'neighbor', 'offset': offset}
            row.update({k: self._field(full[k], target, limit=160) for k in
                        ('target', 'role', 'name', 'value', 'help', 'position', 'bounds') if k in full})
            rows.append(row)
        return {'basis': basis, 'same_captured_parent': True,
                'association_inferred': False, 'section_membership_proven': False,
                'order_is_not_visual_distance': True, 'unknown_order_count': unknown_order,
                'other_siblings_available': max(0, len(peers)-1-len(rows)),
                'next': 'Inspect a neighbor target or the parent; unfiltered region lists retain other controls.'}, rows

    def _detail(self, o, cid):
        fields = self._semantic_fields(o, cid); target = fields['target']
        summary = {'kind': 'control', **self._control(o, cid)}
        neighborhood, neighbors = self._neighborhood(o, cid)
        items = [summary, *neighbors]
        children = [self.ref('c', [o['snapshot_id'], p['id']]) for p in o['controls'] if p.get('parent') == cid]
        if children:
            fields['children'] = children
            summary['children'] = self._field(children, target)
        # Exact semantic values follow the summary, with lossless continuation.
        # Internal raw editor/provenance/capability copies are never streamed.
        for field, value in fields.items():
            if len(_json(value).encode()) > FIELD_BYTES:
                items.extend(_fragments(field, value))
        return {'status': 'ok', 'view': self.ref('v', o['snapshot_id']), 'target': target,
                'retained_state': True, 'action_requires_fresh_checks': True,
                'neighborhood': neighborhood, 'items': items,
                'detail_semantics': 'Named captured facts. Join all json_fragment parts for a field and JSON-decode before using its exact value.',
                'private_evidence': 'Full capture and execution guards remain with the owner; this is not raw driver provenance.',
                'coverage': {'source_complete': o.get('coverage', {}).get('complete'),
                             'matched_total': len(items), 'returned_count': len(items), 'remaining_count': 0,
                             'enumeration_complete': True, 'count_unit': 'semantic_items'}}

    def _translate(self, name, args):
        self._projection_page = None
        if name == 'locua_inspect' and 'cursor' in args:
            o = self.observation(args['view'])
            saved = self.resolve(args['cursor'], 'p')
            if saved.get('semantic_projection') == self.version:
                if saved.get('snapshot_id') != o['snapshot_id']:
                    raise InterfaceError('stale_cursor', 'This semantic continuation belongs to another view.')
                if set(args) != {'view', 'cursor'}:
                    raise InterfaceError('cursor_with_filters', 'Continue with view and cursor only.')
                self._projection_page = deepcopy(saved)
                return deepcopy(saved['arguments'])
        return super()._translate(name, args)

    def _paginate(self, result, args):
        """Split projected items without dropping the owner's later pages."""
        items = result['items']; content_hash = _digest(items)
        start = 0
        if self._projection_page is not None:
            saved = self._projection_page
            if saved['content_sha256'] != content_hash or saved['arguments'] != args:
                raise InterfaceError('stale_cursor', 'Retained semantic page changed; inspect the target/view again.')
            start = saved['offset']
            if type(start) is not int or not 0 < start < len(items):
                raise InterfaceError('stale_cursor', 'Invalid retained semantic page offset.')
        original_coverage = deepcopy(result.get('coverage', {}))
        def build(end):
            projected = {k: deepcopy(v) for k, v in result.items() if k != 'items'}
            projected['projection_version'] = self.version
            projected['items'] = deepcopy(items[start:end])
            coverage = projected.setdefault('coverage', {})
            coverage.update(returned_count=end-start,
                remaining_count=original_coverage.get('remaining_count', 0)+len(items)-end,
                enumeration_complete=bool(original_coverage.get('enumeration_complete') and start == 0 and end == len(items)))
            if end < len(items):
                saved = {'semantic_projection': self.version, 'snapshot_id': args['snapshot_id'],
                         'arguments': deepcopy(args), 'offset': end, 'content_sha256': content_hash}
                cursor = self.ref('p', saved)
                coverage.update(cursor=cursor, continue_with={'view': result['view'], 'cursor': cursor})
            return projected
        end = len(items)
        projected = build(end)
        while len(_json(projected).encode()) > PAGE_BYTES and end > start+1:
            end -= 1
            projected = build(end)
        if len(_json(projected).encode()) > PAGE_BYTES:
            raise InterfaceError('semantic_item_too_large', 'A complete semantic item exceeds the page budget; no item was clipped or skipped.')
        return projected

    def _remember(self, result):
        view = result.get('view')
        for row in result.get('items', []):
            if not isinstance(row, dict):
                continue
            if 'target' in row and row.get('role'):
                target = row['target']
                if not isinstance(target, str):
                    continue
                note = {'view': view, 'potentially_stale': True,
                        'seen_as': row.get('kind', 'listed_control')}
                for key in ('target', 'role', 'name', 'value', 'states', 'parent', 'position', 'region'):
                    if key in row:
                        value = row[key]
                        note[key] = (deepcopy(value) if isinstance(value, dict) and value.get('deferred')
                                     else self._field(value, target, limit=128))
                self._explored_controls.pop(target, None); self._explored_controls[target] = note
                while len(self._explored_controls) > EXPLORED_CONTROLS:
                    self._explored_controls.popitem(last=False)
            if 'region' in row and row.get('kind') and 'target' not in row:
                region = row['region']
                self._explored_regions.pop(region, None)
                name = row.get('name')
                self._explored_regions[region] = {'view': view, 'region': region,
                    'name': deepcopy(name) if len(_json(name).encode()) <= FIELD_BYTES else {'deferred': True, 'next': {'view': view, 'region': region}},
                    'kind': row['kind'], 'seen_as': 'overview', 'potentially_stale': True}
                while len(self._explored_regions) > EXPLORED_REGIONS:
                    self._explored_regions.popitem(last=False)

    def _page(self, page, original_args=None):
        sid = page.get('snapshot_id'); o = self.owner._observations.get(sid)
        if o is None:
            return deepcopy(page)
        args = deepcopy(original_args or {'snapshot_id': sid, 'operation': page.get('operation', 'overview')})
        result = (self._detail(o, page['control_id']) if page.get('operation') == 'control'
                  else super()._page(page, args))
        result['context_semantics'] = 'Parents and order are captured relationships; adjacency does not establish a labeled section or requested target.'
        result = self._paginate(result, args)
        self._remember(result)
        if args.get('region_id'):
            region = self.ref('r', [sid, args['region_id']])
            current = self._explored_regions.get(region, {'view': result['view'], 'region': region})
            current.update(seen_as='inspected_region', potentially_stale=True)
            self._explored_regions.pop(region, None); self._explored_regions[region] = current
            while len(self._explored_regions) > EXPLORED_REGIONS:
                self._explored_regions.popitem(last=False)
        return result

    def _project(self, name, raw, args=None):
        result = super()._project(name, raw, args)
        result['projection_version'] = self.version
        # Remove arithmetic-only witness noise only with an actual reviewed,
        # non-arithmetic scope. Unknown/mixed/calculation scopes retain it.
        scope = self.owner._scopes.get(raw.get('scope_id'))
        if (scope and scope.get('status') in ('approved', 'reconciled_verified') and scope.get('goals')
                and all(g.get('kind') in ('text', 'state') for g in scope['goals'])):
            result.pop('arithmetic_input', None)
        return result

    def state(self, review=None, cursor=None):
        result = super().state(review, cursor)
        result['projection_version'] = self.version
        if review is None and cursor is None:
            result['explored'] = {'controls': deepcopy(list(self._explored_controls.values())),
                'regions': deepcopy(list(self._explored_regions.values())),
                'recent_limits': {'controls': EXPLORED_CONTROLS, 'regions': EXPLORED_REGIONS},
                'complete_history': False, 'potentially_stale': True, 'action_authority': False,
                'next': 'Use current_views for current reference selection; these notes do not refresh old targets.'}
        return result
