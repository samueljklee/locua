"""Opt-in small model interface over the existing guarded DesktopToolset.

References name immutable captured objects. This adapter does not choose a target,
infer approval, retry an input, or change the driver. It compiles explicit model
choices to the owner's existing contracts and projects named semantic records.
"""
from copy import deepcopy
import hashlib
import json

from .amplifier_contracts import obj, array, choice, ID, S, BOOL, TEXT, persistence_specs, _errors
from .engine.prototype.cli import private_json


VERSION = 'continuity-v1'
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 32}
GOAL = choice([
    obj({'kind': {'const': 'text'}, 'target': ID, 'value': S}, ('kind', 'target', 'value')),
    obj({'kind': {'const': 'state'}, 'target': ID, 'property': {'enum': ['selected', 'checked'], 'type': 'string'},
         'value': BOOL}, ('kind', 'target', 'property', 'value')),
    obj({'kind': {'const': 'calculation'}, 'target': ID, 'expression': TEXT}, ('kind', 'target', 'expression')),
])
STEP = obj({'target': ID, 'operation': {'enum': ['press', 'set_text'], 'type': 'string'}, 'value': S},
           ('target', 'operation'))
SPECS = {
    'locua_apps': ('Find applications by name. Proven aliases of the same installed app share one app_id; processes are metadata. Distinct or unproven identities stay separate. Pages count applications, not processes. An app is not a window.',
        obj({'query': S, 'start': {'type': 'integer', 'minimum': 0}, 'limit': LIMIT, 'inventory_id': ID})),
    'locua_windows': ('List all windows belonging to the observed installed application across its processes. Prefer the relevant visible window.', obj({'app_id': ID}, ('app_id',))),
    'locua_launch': ('Open/reopen an observed application when needed, then list its windows.', obj({'app_id': ID}, ('app_id',))),
    'locua_activate': ('Activate an exact observed window. Use when needed for observation, not to repair invalid tool arguments.', obj({'window_id': ID}, ('window_id',))),
    'locua_observe': ('Read an exact window. Returns a view, regions, readouts and navigation. Use inspect to read more without recapturing.', obj({'window_id': ID}, ('window_id',))),
    'locua_inspect': ('Read retained UI; no app change. Supply view and optional region/query/role to list controls; target reads one control. Cursor continues its original query: omit other filters. Empty arguments except view list all controls. Query searches captured labels, help, identifiers and string values, including unnamed readouts. Names/help/parents distinguish competitors. Query semantics reports unbound-text coverage; matches do not prove visibility or exactness. Other regions remain available.',
        choice([
            obj({'view': ID, 'region': ID, 'query': S, 'role': S, 'limit': LIMIT}, ('view',)),
            obj({'view': ID, 'target': ID}, ('view', 'target')),
            obj({'view': ID, 'cursor': ID}, ('view', 'cursor'))])),
    'locua_review': ('Present a readable plan before input. goals name observed target references: text=value (exact buffer, not saving); state=property/value; calculation=expression with readable result target. Preserve unchanged controls with target/property; their actual captured values are checked by code. presses authorize observed navigation controls with a purpose when goals cannot yet bind. covers_request is true only if every user outcome and restriction is covered. Code manages approval identifiers; you cannot invent approval.',
        obj({'summary': TEXT, 'goals': array(GOAL, maximum=16),
             'preserve': array(obj({'target': ID, 'property': {'enum': ['value', 'selected', 'checked'], 'type': 'string'}}, ('target', 'property')), maximum=64),
             'presses': array(obj({'target': ID, 'purpose': TEXT}, ('target', 'purpose')), maximum=32),
             'covers_request': BOOL, 'unresolved': array(TEXT)}, ('summary', 'goals', 'covers_request'))),
    'locua_act': ('Execute an observed operation on target after review. No scope/snapshot/action IDs needed; code resolves one actual matching approval and freshly checks it. For press omit value; set_text requires exact reviewed value. Refused means inspect the reason; uncertain/no_retry means do not repeat input.',
        obj({**STEP['properties'], 'review': ID}, ('target', 'operation'))),
    'locua_act_sequence': ('Execute a short sequence of observed target/operation choices in order, after review. Use only when no intervening discovery is needed. All targets must belong to one view and one approval. Each step is freshly guarded. Stop on changed layout or uncertainty; never replay a partial sequence. For presses omit value. Calculation requires reset, requested expression, and evaluate; do not enter a precomputed result.',
        obj({'steps': array(STEP, minimum=1, maximum=32), 'review': ID}, ('steps',))),
    'locua_verify': ('Freshly check all reviewed goals with {}, or one review with review alone. It does not edit. If uncertain input offers reconciliation, supply that review with reconcile=true. If a calculation result vanished, explicitly choose a newly inspected readout with review, goal, target; this preserves the requested expression and grants no input authority.',
        choice([obj({}), obj({'review': ID}, ('review',)),
            obj({'review': ID, 'reconcile': {'type': 'boolean', 'const': True}}, ('review', 'reconcile')),
            obj({'review': ID, 'goal': ID, 'target': ID}, ('review', 'goal', 'target'))])),
    'locua_status': ('Read retained progress with {}. Supply review for complete paged approval details; continue using its returned cursor. No app input or fresh verification. Use after losing context, not instead of acting on available evidence.', choice([obj({}), obj({'review': ID, 'cursor': ID}, ('review',))])),
    'locua_clarify': ('Ask only for genuinely missing information or unresolved ambiguity. An answer is not approval.',
        obj({'question': TEXT, 'reason': TEXT}, ('question', 'reason'))),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class InterfaceError(ValueError):
    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code, self.detail = code, detail


class Tool:
    def __init__(self, interface, name):
        self.interface, self.name = interface, name
        self.description, schema = interface.specs[name]
        self.input_schema = deepcopy(schema)

    async def execute(self, args):
        from amplifier_core.models import ToolResult
        async with self.interface.owner._async_lock:
            output = self.interface.call(self.name, args)
        return ToolResult(success=output.get('status') not in ('refused', 'unavailable', 'uncertain', 'canceled', 'blocked'), output=output)


class ModelInterface:
    def __init__(self, owner):
        self.owner = owner
        self.specs = persistence_specs(SPECS, contract=owner.persistence_contract)
        self.refs, self.reverse, self.counts = {}, {}, {}
        self.cursors, self.events = {}, []
        self._repeats, self._last_generation = {}, None
        self._app_pages, self._app_routes = {}, {}

    def tools(self):
        return [Tool(self, name) for name in self.specs]

    def ref(self, kind, value):
        key = (kind, _json(value))
        if key not in self.reverse:
            n = self.counts.get(kind, 0) + 1
            self.counts[kind] = n
            label = kind + str(n)
            self.reverse[key] = label
            self.refs[label] = (kind, deepcopy(value))
        return self.reverse[key]

    def resolve(self, label, kind):
        row = self.refs.get(label) if isinstance(label, str) else None
        if not row or row[0] != kind:
            raise InterfaceError('unknown_reference', 'Use an observed '+kind+' reference from a tool result; identifiers cannot substitute for one another.')
        return deepcopy(row[1])

    def observation(self, view):
        sid = self.resolve(view, 'v')
        try:
            return self.owner._observation(sid)
        except ValueError as error:
            raise InterfaceError('stale_view', str(error), current_views=self._views()) from error

    def control(self, target):
        sid, cid = self.resolve(target, 'c')
        o = self.observation(self.ref('v', sid))
        return o, self.owner._find(o, cid)

    def _bounded(self, value):
        if len(_json(value).encode()) <= 700:
            return deepcopy(value)
        return {'deferred': True, 'bytes': len(_json(value).encode()),
                'sha256': hashlib.sha256(_json(value).encode()).hexdigest(),
                'next': 'Inspect this target for complete paged details; this is not an exact value.'}

    def _control(self, o, cid):
        c = self.owner._find(o, cid)
        sid = o['snapshot_id']; sem = c.get('semantics') or {}
        label = c.get('name') or sem.get('description') or sem.get('title')
        row = {'target': self.ref('c', [sid, cid]), 'role': c['role'], 'name': self._bounded(label),
               'value': self._bounded(c.get('value')), 'states': self._bounded(c.get('states', {})),
               'operations': sorted({a['kind'] for a in self.owner._actions[sid].values() if a['control_id'] == cid})}
        if sem.get('help'):
            row['help'] = self._bounded(sem['help'])
        if sem.get('identifier'):
            row['identifier'] = self._bounded(sem['identifier'])
        parent = next((p for p in o['controls'] if p['id'] == c.get('parent')), None)
        if parent:
            row['parent'] = {'target': self.ref('c', [sid, parent['id']]),
                             'name': self._bounded(parent.get('name')), 'role': parent['role']}
        evidence = c.get('value_evidence', {})
        if evidence:
            row['value_evidence'] = {k: evidence[k] for k in ('plane', 'precision', 'exact_value_proven') if k in evidence}
        if c.get('editor'):
            row['editor'] = {k: self._bounded(c['editor'][k]) for k in ('focused', 'value_settable', 'selected_range', 'selected_text') if k in c['editor']}
        return row

    def _page(self, page, original_args=None):
        sid = page.get('snapshot_id')
        o = self.owner._observations.get(sid)
        if o is None:
            return deepcopy(page)
        view = self.ref('v', sid)
        result = {'status': page.get('status', 'ok'), 'view': view,
                  'retained_state': True, 'action_requires_fresh_checks': True, 'items': []}
        for item in page.get('items', []):
            if isinstance(item, list):
                row = self._control(o, item[0]); row['membership'] = item[8]
                result['items'].append(row)
            elif isinstance(item, dict) and item.get('kind') == 'region':
                result['items'].append({'region': self.ref('r', [sid, item['region_id']]), 'name': item.get('label'),
                    'kind': item.get('region_kind'), 'counts': item.get('counts'),
                    'parent': self.ref('r', [sid, item['parent_region_id']]) if item.get('parent_region_id') else None,
                    'children': [self.ref('r', [sid, r]) for r in item.get('child_region_ids', [])]})
            elif isinstance(item, dict) and item.get('id') in {c['id'] for c in o['controls']}:
                result['items'].append(self._control(o, item['id']))
            elif page.get('operation') == 'control' and isinstance(item, dict):
                result['items'].extend(self._detail_item(o, page['control_id'], item))
            else:
                result['items'].append(deepcopy(item))
        if 'control_id' in page and page.get('operation') == 'control':
            # Detail fragments retain their documented encoding and join rules.
            result['target'] = self.ref('c', [sid, page['control_id']])
        coverage = page.get('coverage', {})
        result['coverage'] = {k: deepcopy(coverage[k]) for k in ('source_complete', 'matched_total', 'returned_count',
            'remaining_count', 'outside_scope_count', 'enumeration_complete') if k in coverage}
        if page.get('counts'):
            result['counts'] = deepcopy(page['counts'])
        if page.get('query_semantics'):
            result['query_semantics'] = deepcopy(page['query_semantics'])
        cursor = coverage.get('continuation')
        if cursor:
            args = deepcopy(original_args or {'snapshot_id': sid, 'operation': page.get('operation', 'overview')})
            args['cursor'] = cursor
            continuation = self.ref('p', args)
            result['coverage']['cursor'] = continuation
            result['coverage']['continue_with'] = {'view': view, 'cursor': continuation}
        return result

    def _detail_item(self, o, cid, item):
        """Keep exact semantic detail while replacing nonpublic address fields.

        Large mapped collections use the same lossless JSON fragment format.
        A source collection may span pages, so only its first source fragment
        emits the replacement; other semantic fragments pass through unchanged.
        """
        from .progressive_ui import _fragments
        sid = o['snapshot_id']; c = self.owner._find(o, cid)
        field = item.get('field')
        mapped = {
            'id': ('target', self.ref('c', [sid, cid])),
            'parent': ('parent', self.ref('c', [sid, c['parent']]) if c.get('parent') else None),
            'children': ('children', [self.ref('c', [sid, x['id']]) for x in o['controls'] if x.get('parent') == cid]),
            'actions': ('operations', self._control(o, cid)['operations']),
        }
        if field == 'region_id' and item.get('kind') == 'attribute':
            mapped[field] = ('region', self.ref('r', [sid, item['value']]) if item.get('value') else None)
        if field == 'capabilities':
            # Native route addresses are private implementation metadata, not
            # public callable references. Preserve supported/reason/proof fields.
            def without_addresses(v):
                if isinstance(v, dict):
                    return {k: without_addresses(x) for k, x in v.items()
                            if k not in ('address', 'handle', 'driver_descriptor')}
                if isinstance(v, list):
                    return [without_addresses(x) for x in v]
                return deepcopy(v)
            mapped[field] = ('capabilities', without_addresses(c.get('capabilities')))
        if field not in mapped:
            return [deepcopy(item)]
        if item.get('kind') == 'json_fragment' and item.get('part', 0) != 0:
            return []
        public_field, value = mapped[field]
        if len(_json(value).encode()) <= 768:
            return [{'kind': 'attribute', 'field': public_field, 'value': value}]
        return list(_fragments(public_field, value))

    def _views(self):
        rows = []
        for sid in self.owner._latest.values():
            o = self.owner._observations[sid]
            wins = [wid for wid, w in self.owner._window_records.items() if w['target'] == o['target']]
            rows.append({'view': self.ref('v', sid),
                         'window_id': self.ref('w', wins[-1]) if wins else None})
        return rows

    def _reviews(self, *, complete=False):
        rows = []
        for sid, s in self.owner._scopes.items():
            review = self.ref('q', sid)
            def bounded(value):
                if complete:
                    return deepcopy(value)
                result = self._bounded(value)
                if isinstance(result, dict) and result.get('deferred'):
                    result['next'] = {'tool': 'locua_status', 'arguments': {'review': review}}
                return result
            row = {'review': review, 'status': s['status'], 'summary': bounded(s['summary']),
                   'covers_request': s['covers_entire_request'],
                   'goals': [{('target_name' if k == 'target' else k): bounded(v) for k, v in g.items()} for g in s['goals']],
                   'preserves': [{'property': p['binding']['property'], 'target': p['goal']['target'],
                                  'value': bounded(p['goal']['value'])} for p in s['preserves']]}
            if any(g['kind'] == 'calculation' for g in s['goals']):
                row['issued_input'] = bounded(s['witness'].view())
            rows.append(row)
        return rows

    def state(self, review=None, cursor=None):
        if cursor is not None or review is not None:
            from .progressive_ui import _fragments
            self.resolve(review, 'q')
            detail = next(r for r in self._reviews(complete=True) if r['review'] == review)
            digest = hashlib.sha256(_json(detail).encode()).hexdigest()
            start = 0
            if cursor is not None:
                saved = self.resolve(cursor, 'p')
                if saved.get('review') != review or saved.get('digest') != digest:
                    raise InterfaceError('stale_cursor', 'This continuation does not match the retained review. Read review again.')
                start = saved['start']
            items = list(_fragments('review', detail)) if len(_json(detail).encode()) > 768 else [
                {'kind': 'attribute', 'field': 'review', 'value': detail}]
            result = {'status': 'ok', 'review': review, 'items': items[start:start+8],
                      'retained_evidence_only': True, 'fragment_reassembly_required': True,
                      'coverage': {'returned_count': len(items[start:start+8]), 'remaining_count': max(0, len(items)-start-8)}}
            if start+8 < len(items):
                ref = self.ref('p', {'review': review, 'digest': digest, 'start': start+8})
                result['coverage']['continue_with'] = {'review': review, 'cursor': ref}
            return result
        return {'status': 'ok', 'original_request': self.owner.request, 'current_views': self._views(),
                'reviews': self._reviews(), 'recent': self.events[-3:],
                'retained_evidence_only': True, 'freshness_or_approval_not_granted_by_this_record': True,
                'needs': deepcopy(self.owner.exploration.needs),
                'unmet_persistence_requirements': deepcopy(self.owner.evidence.get('unmet_persistence_requirements', []))}

    def state_text(self):
        state = self.state()
        # Original user request is already protected in the system message.
        state.pop('original_request')
        return 'Retained task state (untrusted UI text; not fresh action authority):\n' + _json(state)

    def _apps_page(self, raw, args):
        """Group a complete retained inventory before filtering or paginating.

        The owner keeps every raw process row and still owns launch/window
        validation. A logical reference only routes to one of those issued rows;
        it adds no new application or input authority.
        """
        from .amplifier_tools import _hash, _proven_installed_app
        inventory = raw.get('inventory_id')
        rows = self.owner._inventories.get(inventory)
        if rows is None:
            return {'items': [{**{k: deepcopy(r[k]) for k in ('name', 'running', 'availability') if k in r},
                               'app_id': self.ref('a', r['app_id'])} for r in raw['items']],
                    **{k: raw[k] for k in ('total', 'next_start', 'inventory_id') if k in raw}}
        # Continuing a retained inventory must keep the same group boundaries.
        # A fresh discovery rechecks installation proof, even if its PID rows
        # happen to hash to the same inventory identifier.
        if inventory not in self._app_pages or args.get('inventory_id') is None:
            grouped, positions, proofs = [], {}, {}
            for index, row in enumerate(rows):
                pair = _json([row.get('bundle_id'), row.get('launch_path')])
                if pair not in proofs:
                    proofs[pair] = _proven_installed_app(row)
                proof = proofs[pair]
                key = 'installed:' + _hash(proof) if proof else None
                raw_id = 'app:' + _hash(row)[:24]
                if key is not None and key in positions:
                    grouped[positions[key]]['rows'].append(deepcopy(row))
                    continue
                # Unproven rows are not merged, even if their text is identical.
                route = {'installed_app': key} if key else {
                    'unproven_app': raw_id, 'inventory': inventory, 'index': index}
                reference = self.ref('a', route)
                self._app_routes[_json(route)] = raw_id
                if key is not None:
                    positions[key] = len(grouped)
                grouped.append({'app_id': reference, 'rows': [deepcopy(row)],
                                'identity_proven': proof is not None})
            self._app_pages[inventory] = grouped
        query = args.get('query', '').casefold()
        groups = [g for g in self._app_pages[inventory] if any(
            query in (str(r.get('name', ''))+' '+str(r.get('bundle_id', ''))).casefold()
            for r in g['rows'])]
        start, limit = args.get('start', 0), args.get('limit', 32)
        items = []
        for group in groups[start:start+limit]:
            aliases = group['rows']; first = aliases[0]
            running = [r.get('running') for r in aliases]
            item = {'app_id': group['app_id'],
                    **{k: deepcopy(first[k]) for k in ('name', 'bundle_id', 'launch_path') if k in first},
                    'identity_proven': group['identity_proven'],
                    'running': True if any(v is True for v in running) else False if all(v is False for v in running) else None,
                    'processes': [{k: deepcopy(r[k]) for k in ('pid', 'running', 'name') if k in r} for r in aliases]}
            names = list(dict.fromkeys(r.get('name') for r in aliases))
            if len(names) > 1:
                item['observed_names'] = names
            items.append(item)
        return {'items': items, 'total': len(groups), 'start': start,
                'next_start': start+limit if start+limit < len(groups) else None,
                'inventory_id': inventory, 'raw_inventory_count': len(rows),
                'other_apps_discoverable': True}

    def _project(self, name, raw, args=None):
        # The legacy representation budget is distinct from execution. The
        # compact adapter can represent an approved large value by paging its
        # exact retained contract, without asking for approval a second time.
        if (name == 'locua_review' and raw.get('code') == 'model_response_too_large'
                and raw.get('operation_status') == 'approved'
                and raw.get('scope_id') in self.owner._scopes
                and self.owner._scopes[raw['scope_id']]['status'] == 'approved'):
            raw = {**raw, 'status': 'approved'}
            raw.pop('code', None)
            raw.pop('reason', None)
        result = {k: deepcopy(raw[k]) for k in ('status', 'code', 'reason', 'action_started', 'no_retry',
            'uncertain_action', 'operation_status', 'steps_completed', 'steps_planned', 'steps_attempted',
            'arithmetic_input', 'failed_step', 'fresh_refresh', 'saved_output_proven', 'persistence', 'unmet_persistence_requirements') if k in raw}
        if 'window_id' in raw:
            result['window_id'] = self.ref('w', raw['window_id'])
        if raw.get('snapshot_id') in self.owner._observations:
            result['view'] = self.ref('v', raw['snapshot_id'])
        if raw.get('scope_id') in self.owner._scopes:
            result['review'] = self.ref('q', raw['scope_id'])
        if name == 'locua_apps' and 'items' in raw:
            result.update(self._apps_page(raw, args or {}))
        if name == 'locua_windows' and 'windows' in raw:
            result['windows'] = [{**{k: deepcopy(w[k]) for k in ('title', 'is_on_screen', 'on_current_space') if k in w},
                                  'window_id': self.ref('w', w['window_id'])} for w in raw['windows']]
            result['availability'] = raw.get('availability')
            result['unresolved_process_count'] = len(raw.get('unresolved_pids', []))
        if 'overview' in raw:
            result['overview'] = self._page(raw['overview'])
        if name == 'locua_inspect' and raw.get('status') == 'ok':
            result = self._page(raw, args)
        if name == 'locua_review' and raw.get('status') == 'approved':
            result['approved'] = self._reviews()[-1]
            result['next'] = 'Execute observed target operations within this review, then verify. Approval alone is not completion.'
        if name == 'locua_launch':
            result['next'] = 'List application windows.'
        if name == 'locua_verify' and 'scopes' in raw:
            result['checks'] = []
            for sid, scope in raw['scopes'].items():
                for g in scope.get('goals', []) + scope.get('preserves', []):
                    e = g.get('evidence') or {}
                    check = {'review': self.ref('q', sid), 'goal': g.get('goal_id', g.get('predicate_id')),
                             'matched': g.get('matched'), 'reason': g.get('reason'),
                             'actual': self._bounded(e.get('actual')), 'plane': e.get('plane')}
                    if g.get('recovery'):
                        check['recovery'] = 'Inspect fresh readouts, then verify with this review, goal, and the new target. No input authority is added.'
                    result['checks'].append(check)
            result['covers_request'] = any(s['covers_entire_request'] for s in self.owner._scopes.values())
        if raw.get('reconciliation'):
            result['reconciliation'] = deepcopy(raw['reconciliation'])
            arguments = result['reconciliation'].get('arguments')
            if arguments and arguments.get('scope_id') in self.owner._scopes:
                result['reconciliation']['arguments'] = {
                    'review': self.ref('q', arguments['scope_id']), 'reconcile': True}
        if raw.get('exploration_feedback'):
            feedback = raw['exploration_feedback']
            result['exploration_feedback'] = {k: deepcopy(feedback[k]) for k in (
                'code', 'equivalent_inspection_count', 'equivalent_failure_count',
                'new_information', 'current_state_proven', 'action_authority') if k in feedback}
            result['exploration_feedback']['next'] = (
                'Use the returned continuation, inspect another relevant region or target, or read locua_status with {}. '
                'Do not repeat an equivalent inspection or treat retained state as fresh action authority.')
        if raw.get('status') in ('refused', 'unavailable', 'unverified'):
            result['current_views'] = self._views()
            result['approved_reviews'] = [r['review'] for r in self._reviews() if r['status'] == 'approved']
        return result

    def _review(self, args):
        targets = [g['target'] for g in args['goals']] + [p['target'] for p in args.get('preserve', [])] + [p['target'] for p in args.get('presses', [])]
        if not targets:
            raise InterfaceError('review_needs_targets', 'Inspect an outcome or navigation target before review.')
        found = [self.control(t) for t in targets]
        if len({o['snapshot_id'] for o, _ in found}) != 1:
            raise InterfaceError('mixed_views', 'Review targets must be from the same current view. Inspect that view again.')
        o = found[0][0]; goals, effects, preserves = [], [], []
        for n, g in enumerate(args['goals'], 1):
            _, c = self.control(g['target'])
            goal = {'id': 'g'+str(n), 'kind': g['kind'], 'control_id': c['id'],
                    'target': c.get('name') or c.get('semantics', {}).get('description') or c['role']}
            if g['kind'] == 'calculation':
                goal.update(expression=g['expression'], evidence_plane='display')
            else:
                goal.update(value=g['value'], evidence_plane='editor_buffer' if g['kind'] == 'text' else 'display')
            if g['kind'] == 'text' and 'persistence_requirement' in g:
                goal['persistence_requirement'] = g['persistence_requirement']
            if g['kind'] == 'state':
                goal['property'] = g['property']
            goals.append(goal)
            if g['kind'] == 'state' and g['property'] not in c.get('states', {}):
                effects.append({'kind': 'press', 'control_id': c['id'],
                    'purpose': 'Attempt the requested '+g['property']+'='+str(g['value'])+'; initial state is unknown, fresh verification required'})
            else:
                effects.append({'kind': 'goal', 'goal_id': goal['id']})
        for p in args.get('presses', []):
            _, c = self.control(p['target'])
            effects.append({'kind': 'press', 'control_id': c['id'], 'purpose': p['purpose']})
        for p in args.get('preserve', []):
            _, c = self.control(p['target']); prop = p['property']
            value = c.get('value') if prop == 'value' else c.get('states', {}).get(prop)
            if value is None:
                raise InterfaceError('preservation_unknown', 'The preserved property is not observed. Inspect a control with explicit state/value; do not assume false.')
            preserves.append({'control_id': c['id'], 'property': prop, 'value': deepcopy(value)})
        return {'snapshot_id': o['snapshot_id'], 'summary': args['summary'], 'goals': goals, 'effects': effects,
                'preserves': preserves, 'covers_entire_request': args['covers_request'],
                'unresolved_requirements': args.get('unresolved', [])}

    def _action(self, step):
        o, c = self.control(step['target'])
        operation = step['operation']; value = step.get('value')
        if operation == 'press' and 'value' in step:
            raise InterfaceError('press_has_value', 'A press takes no value. Omit value; no input occurred.')
        if operation == 'set_text' and 'value' not in step:
            raise InterfaceError('text_value_missing', 'set_text requires the exact reviewed value.')
        actions = [a for a in self.owner._actions[o['snapshot_id']].values() if a['control_id'] == c['id'] and a['kind'] == operation]
        if len(actions) != 1:
            raise InterfaceError('operation_unavailable', 'Choose one of this target\'s observed operations.')
        return o, actions[0], value

    def _approval(self, actions, review=None):
        from .goal_verification import matches_retained_identity
        from .engine.prototype.core import _identity
        from .arithmetic_input import symbol
        def candidate(scope, o, a, value):
            # Select an existing review by historical semantics only. The
            # canonical owner evaluates _permit on a new capture before input.
            c = self.owner._find(o, a['control_id'])
            for index, effect in enumerate(scope['effects']):
                if effect['kind'] == 'press':
                    if (index not in scope['issued_press_effects'] and a['kind'] == 'press' and value is None
                            and _identity(c, o) == effect['identity'] and c.get('bounds') == effect['bounds']):
                        return True
                    continue
                goal = next(g for g in scope['goals'] if g['id'] == effect['goal_id'])
                binding = scope['bindings'][goal['id']]
                if goal['kind'] == 'calculation' and a['kind'] == 'press' and value is None and symbol(c) is not None:
                    return True
                if not matches_retained_identity(binding, o, c['id']):
                    continue
                if goal['kind'] == 'state' and a['kind'] == 'press' and value is None:
                    return True
                expected = goal.get('expression') if goal['kind'] == 'calculation' else goal.get('value')
                if goal['kind'] in ('text', 'calculation') and a['kind'] == 'set_text' and value == expected:
                    return True
            return False
        candidates = []; refusals = []
        sid_filter = self.resolve(review, 'q') if review is not None else None
        for sid, scope in self.owner._scopes.items():
            if sid_filter is not None and sid != sid_filter:
                continue
            if scope['status'] != 'approved':
                continue
            try:
                for o, a, value in actions:
                    if o['target'] != scope['target']:
                        raise ValueError('wrong window')
                    if not candidate(scope, o, a, value):
                        raise ValueError('The observed target/operation/value is outside this reviewed contract')
            except ValueError as error:
                refusals.append({'review': self.ref('q', sid), 'reason': str(error)})
                continue
            candidates.append(sid)
        if len(candidates) != 1:
            raise InterfaceError('approval_required' if not candidates else 'ambiguous_approval',
                'No unique actual approval covers this operation. Review the intended change first; do not invent a review.' if not candidates else
                'Several real reviews cover this operation; select one with review.',
                reviews=[self.ref('q', s) for s in candidates], approval_checks=refusals)
        return candidates[0]

    def _translate(self, name, args):
        if name in ('locua_windows', 'locua_launch'):
            app = self.resolve(args['app_id'], 'a')
            if isinstance(app, dict):
                app = self._app_routes.get(_json(app))
                if app not in self.owner._app_records:
                    raise InterfaceError('unknown_application', 'Discover the application again; its retained identity is unavailable.')
            return {'app_id': app}
        if name in ('locua_activate', 'locua_observe'):
            return {'window_id': self.resolve(args['window_id'], 'w')}
        if name == 'locua_inspect':
            o = self.observation(args['view']); sid = o['snapshot_id']
            if 'cursor' in args:
                if set(args) - {'view', 'cursor'}:
                    raise InterfaceError('cursor_with_filters', 'Cursor continues the original query; omit target, region, query and role.')
                call = self.resolve(args['cursor'], 'p')
                if call['snapshot_id'] != sid:
                    raise InterfaceError('stale_cursor', 'Cursor belongs to another view.')
                return call
            if 'target' in args:
                if set(args) - {'view', 'target'}:
                    raise InterfaceError('target_with_filters', 'A target detail request takes only view and target.')
                target_o, c = self.control(args['target'])
                if target_o['snapshot_id'] != sid:
                    raise InterfaceError('mixed_views', 'Target belongs to another view.')
                return {'snapshot_id': sid, 'operation': 'control', 'control_id': c['id']}
            call = {'snapshot_id': sid, 'operation': 'list', 'limit': args.get('limit', 16)}
            if 'region' in args:
                rsid, rid = self.resolve(args['region'], 'r')
                if rsid != sid:
                    raise InterfaceError('mixed_views', 'Region belongs to another view.')
                call['region_id'] = rid
            for key in ('query', 'role'):
                if key in args:
                    call[key] = args[key]
            return call
        if name == 'locua_review':
            return self._review(args)
        if name in ('locua_act', 'locua_act_sequence'):
            steps = args['steps'] if name == 'locua_act_sequence' else [args]
            actions = [self._action(s) for s in steps]
            if len({o['snapshot_id'] for o, _, _ in actions}) != 1:
                raise InterfaceError('mixed_views', 'Sequence targets must come from one current view.')
            scope = self._approval(actions, args.get('review'))
            values = [{'action_id': a['id'], **({'value': v} if a['kind'] == 'set_text' else {})} for _, a, v in actions]
            call = {'scope_id': scope, 'snapshot_id': actions[0][0]['snapshot_id']}
            return {**call, 'steps': values} if name == 'locua_act_sequence' else {**call, **values[0]}
        if name == 'locua_verify':
            if not args:
                return {'all': True}
            if set(args) == {'review'}:
                return {'scope_id': self.resolve(args['review'], 'q')}
            if args.get('reconcile') is True and set(args) == {'review', 'reconcile'}:
                return {'scope_id': self.resolve(args['review'], 'q'), 'reconcile': True}
            if set(args) == {'review', 'goal', 'target'}:
                o, c = self.control(args['target'])
                return {'scope_id': self.resolve(args['review'], 'q'), 'goal_id': args['goal'],
                        'snapshot_id': o['snapshot_id'], 'control_id': c['id']}
            raise InterfaceError('verification_arguments', 'Use {} or review alone for ordinary verification; recovery requires exactly review/reconcile=true or review/goal/target.')
        return deepcopy(args)

    def _nonprogress(self, name, args, result):
        # Capturing the same UI under a new snapshot is not semantic progress.
        semantic = []
        for sid in self.owner._latest.values():
            o = self.owner._observations[sid]
            positions = {c['id']: n for n, c in enumerate(o['controls'])}
            semantic.append([o['target'], [
                [*[c.get(k) for k in ('role', 'name', 'value', 'states', 'semantics', 'bounds')],
                 positions.get(c.get('parent'))] for c in o['controls']]])
        generation = _json([semantic, [(s, r['status'], len(r['issued_press_effects']), r['witness'].view()) for s, r in self.owner._scopes.items()]])
        if generation != self._last_generation:
            self._last_generation = generation
            self._repeats.clear()
        if result.get('status') in ('refused', 'unavailable'):
            key = name + ':' + str(result.get('code') or result.get('reason'))
        elif name == 'locua_status' and not args:
            key = name
        elif name in ('locua_observe', 'locua_inspect', 'locua_status'):
            # Includes delivered semantic evidence, so a continuation/new detail
            # that adds information does not count as an equivalent inspection.
            # Public references change on recapture. Their captured positions
            # distinguish competitors without making new aliases look like new
            # evidence. Never interpret ordinary UI text as a reference.
            region_cache = {}
            def reference(label):
                row = self.refs.get(label) if isinstance(label, str) else None
                if row is None:
                    return label
                kind, value = row
                if kind == 'w':
                    return {'window': self.owner._window_records.get(value, {}).get('target')}
                if kind == 'v':
                    return {'window': self.owner._observations.get(value, {}).get('target')}
                if kind not in ('c', 'r'):
                    return label
                sid, identifier = value
                observation = self.owner._observations.get(sid)
                if observation is None:
                    return label
                positions = {c['id']: n for n, c in enumerate(observation['controls'])}
                if kind == 'c':
                    return {'window': observation['target'], 'control_position': positions.get(identifier)}
                if sid not in region_cache:
                    from .engine.prototype.regions import catalog_regions
                    region_cache[sid] = {r['id']: r for r in catalog_regions(observation)['regions']}
                region = region_cache[sid].get(identifier)
                if region is None:
                    return label
                return {'window': observation['target'], 'region_root_position': positions.get(region['root_control_id']),
                        'region_kind': region['kind'], 'region_label': region['label']}

            ignored = {'exploration_feedback', 'nonprogress', 'capture_age_seconds', 'observed_at_ns',
                       'cursor', 'continuation', 'continue_with', 'current_views', 'previous_page_count'}
            reference_fields = {'view', 'target', 'region', 'window_id', 'parent', 'children'}
            def normalize(v, field=None):
                if isinstance(v, dict):
                    return {k: normalize(x, k) for k, x in v.items() if k not in ignored}
                if isinstance(v, list):
                    return [normalize(x, field) for x in v]
                return reference(v) if field in reference_fields else v
            key = name + ':' + _json(normalize(result))
        else:
            return
        self._repeats[key] = self._repeats.get(key, 0) + 1
        count = self._repeats[key]
        if name == 'locua_inspect' and result.get('status') == 'ok':
            # One canonical retained query may produce several public pages.
            # Reuse the existing delivered-evidence counter for model feedback;
            # canonical query repetition is not repetition of a public page.
            feedback = result.setdefault('exploration_feedback', {})
            feedback.update(equivalent_inspection_count=count, new_information=count == 1,
                            action_authority=False, basis='delivered_public_page')
            if count >= 2:
                feedback['code'] = 'repeated_inspection'
            elif feedback.get('code') == 'repeated_inspection':
                feedback.pop('code')
        if count >= 2:
            result['nonprogress'] = {'equivalent_attempts': count, 'next': 'Choose a different supported operation using the error or current evidence; repeating this does not advance the task.'}
        if count >= 3:
            self.owner._cancellation = {'status': 'blocked', 'reason': 'nonprogress_limit',
                'detail': result.get('reason', 'Repeated equivalent inspection without new information'),
                'execution_stopped': True, 'action_started': False, 'task_complete': False,
                'authority_revoked': True}
            result['execution_stopped'] = True

    def call(self, name, args):
        with self.owner._lock:
            raw = None; translated = None; owner_started = False
            owner_event_start = len(self.owner.evidence['events'])
            failure = None

            def stop_after_failure(error, stage):
                """An interface failure cannot prove that an input never started."""
                nonlocal raw, failure
                if raw is None and len(self.owner.evidence['events']) > owner_event_start:
                    # The owner may have appended its full receipt before an
                    # artifact write failed. Preserve that known outcome.
                    raw = deepcopy(self.owner.evidence['events'][-1].get('result'))
                receipt = raw if isinstance(raw, dict) else {}
                input_possible = owner_started and name in (
                    'locua_act', 'locua_act_sequence', 'locua_launch', 'locua_activate')
                started = receipt.get('action_started', None if input_possible else False)
                result = {'status': 'uncertain' if started is not False else 'blocked',
                    'code': 'interface_result_unavailable', 'reason': 'Interface '+stage+' failed: '+str(error),
                    'operation_status': receipt.get('status'), 'action_started': started,
                    'no_retry': True, 'execution_stopped': True, 'authority_revoked': True,
                    'task_complete': False, 'owner_receipt_retained': bool(receipt),
                    'next': 'The session is stopped. Do not repeat input; inspect the retained owner receipt and independently check the outcome.'}
                # Even if projection failed, references to existing records can
                # identify the operation without granting any new authority.
                for source, kind, dest, records in (
                    ('scope_id', 'q', 'review', self.owner._scopes),
                    ('snapshot_id', 'v', 'view', self.owner._observations),
                    ('window_id', 'w', 'window_id', self.owner._window_records)):
                    value = receipt.get(source)
                    if isinstance(value, str) and value in records:
                        result[dest] = self.ref(kind, value)
                failure = {'tool': name, 'stage': stage, 'error_type': type(error).__name__,
                    'message': str(error), 'owner_started': owner_started,
                    'owner_event': len(self.owner.evidence['events']) if len(self.owner.evidence['events']) > owner_event_start else None,
                    'owner_receipt': deepcopy(raw), 'result': deepcopy(result)}
                self.owner.evidence.setdefault('interface_failures', []).append(failure)
                self.owner._cancellation = deepcopy(result)
                self.owner.evidence['cancellation'] = deepcopy(result)
                return result

            try:
                if self.owner._cancellation is not None:
                    return deepcopy(self.owner._cancellation)
                if name not in self.specs:
                    raise InterfaceError('unknown_tool', 'Use a registered tool.')
                errors = _errors(self.specs[name][1], args)
                if errors:
                    raise InterfaceError('argument_contract_invalid', '; '.join(e['path']+': '+e['message'] for e in errors))
                # A valid restriction survives an invalid target reference.
                # Record it before resolving references, without granting input.
                persistence = (self.owner._text_persistence(args['goals'], stage='before_reference_binding')
                    if name == 'locua_review' else None)
                if persistence is not None and not persistence['all_requirements_satisfied']:
                    result = self.owner._persistence_refusal(persistence)
                    self.owner._write_evidence()
                elif name == 'locua_status':
                    result = self.state(**args)
                else:
                    translated = self._translate(name, args)
                    owner_started = True
                    raw = self.owner.call(name, translated)
                    result = self._project(name, raw, translated)
            except InterfaceError as error:
                if owner_started:
                    result = stop_after_failure(error, 'projection or owner recording')
                else:
                    result = {'status': 'refused', 'code': error.code, 'reason': str(error),
                              'action_started': False, **deepcopy(error.detail), 'current_views': self._views()}
            except (ValueError, KeyError, TypeError) as error:
                if owner_started:
                    result = stop_after_failure(error, 'projection or owner recording')
                else:
                    result = {'status': 'refused', 'code': 'interface_contract', 'reason': str(error), 'action_started': False}
            except Exception as error:
                result = stop_after_failure(error, 'projection or owner recording')
            if failure is None:
                try:
                    self._nonprogress(name, args, result)
                except Exception as error:
                    result = stop_after_failure(error, 'progress recording')
            event = {'sequence': len(self.events)+1, 'tool': name, 'input': deepcopy(args),
                     'result': deepcopy(result), 'translated_arguments': translated,
                     'owner_event': len(self.owner.evidence['events']) if len(self.owner.evidence['events']) > owner_event_start else None}
            try:
                private_json(self.owner.out/f'interface-{event["sequence"]:03d}.json', event)
            except Exception as error:
                result = stop_after_failure(error, 'artifact persistence')
                result['interface_event_persisted'] = False
                self.owner._cancellation = deepcopy(result)
                self.owner.evidence['cancellation'] = deepcopy(result)
            if failure is not None:
                # Owner events already retain any original receipt. Persist the
                # trusted stop latch through the existing evidence writer when
                # possible, and explicitly disclose a failed evidence write.
                try:
                    self.owner._write_evidence()
                except Exception as error:
                    result['failure_evidence_persisted'] = False
                    result['persistence_error'] = str(error)
                    self.owner._cancellation = deepcopy(result)
                    self.owner.evidence['cancellation'] = deepcopy(result)
            # Retain a compact event summary, not recursively nested status output.
            self.events.append({'tool': name, 'status': result.get('status'), 'code': result.get('code'),
                'reason': self._bounded(result.get('reason')), 'action_started': result.get('action_started'),
                'nonprogress': result.get('nonprogress')})
            return result
