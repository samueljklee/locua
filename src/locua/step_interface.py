"""Typed, step-scoped model choices over the unchanged guarded desktop owner.

References address captured facts, never permissions. Plans are model claims.
The adapter does not select a region, outcome, value, or next application action.
"""
from collections import OrderedDict
from copy import deepcopy
import unicodedata

from .amplifier_contracts import BOOL, ID, PERSISTENCE, S, TEXT, array, choice, obj, _errors
from .engine.prototype.cli import private_json
from .goal_verification import _DISPLAY, _SELECTED, _TEXT, _display
from .model_interface import InterfaceError, ModelInterface, SPECS as BASE_SPECS, _json
from .semantic_projection import SemanticModelInterface
from .plan_progress import reviewed_plan_progress


VERSION = 'step-v1'
MODEL_CLAIM_BYTES = 4096
NOTE = {'type': 'string', 'minLength': 1, 'maxLength': 600, 'pattern': r'\S'}
DECISION = obj({'plan': array(NOTE, maximum=12), 'current_step': NOTE,
                'constraints': array(NOTE, maximum=16), 'unresolved': array(NOTE, maximum=16)},
               ('plan', 'current_step', 'constraints', 'unresolved'))
OUTCOME_REF = {**ID, 'pattern': r'^o[1-9][0-9]*$', 'description':
    'Copy an issued o reference VALUE from control.outcomes, never its capability key, control target or prose.'}
PRESERVE_REF = {**OUTCOME_REF, 'description':
    'Copy an issued o reference VALUE from control.outcomes for an actual observed property to preserve; owner captures its value.'}
INPUT_REF = {**ID, 'pattern': r'^i[1-9][0-9]*$', 'description':
    'Copy one bare issued i reference VALUE from control.inputs, never its operation key, control target or prose.'}
PRESS_REF = {**INPUT_REF, 'description':
    'Copy one bare issued i reference VALUE from control.inputs.press; purpose belongs in summary or decision.plan, not this string.'}
REFERENCE_SCHEMAS = {'outcome': OUTCOME_REF, 'preserve': PRESERVE_REF,
                     'input': INPUT_REF, 'press': PRESS_REF}
GOAL = obj({'outcome': OUTCOME_REF, 'value': {'type': ['string', 'boolean'], 'description':
                'Requested original expression string for calculation (never a predicted answer), exact buffer string for text, or boolean for state. Displayed-value outcomes are preservation-only.'},
            'persistence_requirement': {**PERSISTENCE, 'description':
                'Required for exact text only; omit for calculation or state. Report the actual user requirement, never invent it: not_requested means no backing-file constraint. Absence of Save does not prove unchanged bytes; backing-file outcomes require separate trusted evidence.'}},
           ('outcome', 'value'))


def _reference_fields(name, args, schemas=REFERENCE_SCHEMAS):
    """Locate typed fields for precise feedback, never extract or repair values."""
    if not isinstance(args, dict): return {}
    fields = {}
    if name == 'locua_review':
        if isinstance(args.get('goals'), list):
            for index, goal in enumerate(args['goals']):
                if isinstance(goal, dict) and 'outcome' in goal:
                    fields[f'$.goals[{index}].outcome'] = schemas['outcome']
        for key, schema in (('preserves', schemas['preserve']), ('navigation', schemas['press'])):
            if isinstance(args.get(key), list):
                fields.update({f'$.{key}[{index}]': schema for index in range(len(args[key]))})
    elif name == 'locua_act' and 'input' in args:
        fields['$.input'] = schemas['input']
    elif name == 'locua_act_sequence' and isinstance(args.get('inputs'), list):
        fields.update({f'$.inputs[{index}]': schemas['press'] for index in range(len(args['inputs']))})
    return fields


def _with_decision(schema):
    schema = deepcopy(schema)
    schema['properties']['decision'] = deepcopy(DECISION)
    for branch in schema.get('oneOf', []):
        branch['properties']['decision'] = deepcopy(DECISION)
    return schema


SPECS = {
    'locua_apps': ('Discover applications by query. Inspect an application reference to find its windows; use a returned page reference to continue. Do not supply an inventory identifier.',
                   obj({'query': S}, ('query',))),
    'locua_inspect': ('Inspect one issued reference: application lists windows; window captures fresh UI; view shows retained overview; region lists retained controls; control reads detail; page continues its original query. Inspection does not launch, activate, navigate or edit. Retained references never refresh themselves.',
                      obj({'reference': ID}, ('reference',))),
    'locua_search': ('Search captured controls within one view or region reference, optionally by query or role. Empty filters list all controls there. Other controls and regions remain discoverable; a match does not prove the intended target.',
                     obj({'reference': ID, 'query': S, 'role': S}, ('reference',))),
    'locua_launch': ('Explicitly open/reopen an observed application. Inspect that app reference afterwards for windows.', obj({'app': ID}, ('app',))),
    'locua_activate': ('Explicitly reactivate an observed window when necessary for observation. Then inspect its window reference for fresh UI.', obj({'window': ID}, ('window',))),
    'locua_review': ('Request readable human review using issued map VALUES: o references from control.outcomes for goals/preserves, i references from control.inputs.press for navigation. Map keys describe capabilities, never addresses: text=exact editor buffer; calculation=original expression checked against display, not a predicted answer; value=displayed-value preservation only; selected/checked=boolean; selected_unknown/checked_unknown=future predicates requiring one-time press, not readable preservation. Eligibility does not prove target identity. Calculation goals authorize supported arithmetic inputs without listing a button recipe here. Navigation purposes belong in summary. covers_request requires every requested outcome/restriction; partial navigation may precede final bindings with covers_request=false and unresolved needs. Plans are not approvals.',
                     obj({'summary': TEXT, 'goals': array(GOAL, maximum=16),
                          'preserves': array(PRESERVE_REF, maximum=64), 'navigation': array(PRESS_REF, maximum=32),
                          'covers_request': BOOL, 'unresolved': array(NOTE, maximum=16)},
                         ('summary', 'goals', 'covers_request'))),
    'locua_act': ('Execute one reference from a control inputs map after actual review: press takes no value; set_text requires the exact reviewed string. Multiple references for one operation remain ambiguous until owner checks pass. Code selects only a unique matching approval and freshly checks all guards. Uncertain/no_retry input must never be replayed.',
                  obj({'input': INPUT_REF, 'value': S, 'review': ID}, ('input',))),
    'locua_act_sequence': ('Execute a short ordered sequence of observed press input references after review, only when no intervening discovery is required. Each step is freshly guarded; partial or uncertain issuance must not be replayed. Enter the requested expression, not a precomputed answer. Verification is separate.',
                           obj({'inputs': array(PRESS_REF, minimum=1, maximum=32), 'review': ID}, ('inputs',))),
    'locua_verify': BASE_SPECS['locua_verify'],
    'locua_status': BASE_SPECS['locua_status'],
    'locua_clarify': BASE_SPECS['locua_clarify'],
}
SPECS['locua_review'] = (SPECS['locua_review'][0], _with_decision(SPECS['locua_review'][1]))
SPECS['locua_status'] = ('Read pending/current/completed reviewed outcomes and their verification. Optionally record a fallible decision (plan/current_step/constraints/unresolved). Discovery may precede planning; a readable review establishes actual goals and constraints. Only owner evidence completes reviewed predicates; model prose never grants approval or proves language coverage. Review/cursor reads exact criteria.',
    choice([obj({'decision': DECISION}), obj({'review': ID, 'cursor': ID}, ('review',))]))


class StepModelInterface(SemanticModelInterface):
    version = VERSION

    def __init__(self, owner):
        super().__init__(owner)
        self._base_specs = self.specs
        self.specs = deepcopy(SPECS)
        self._reference_schemas = deepcopy(REFERENCE_SCHEMAS)
        self._model_decision = None
        self._model_claims = {'constraints': [], 'unresolved': []}
        self._decision_revision = 0
        self._focus = None
        self._readable = OrderedDict()
        self._readable_evicted = 0
        self._public_calls = 0
        self._retained_overview = False

    def _semantic_fields(self, o, cid):
        fields = super()._semantic_fields(o, cid)
        c = self.owner._find(o, cid); sid = o['snapshot_id']
        target = fields['target']; proof = c.get('value_evidence') or {}
        outcomes = {}
        observed_actions = [a for a in self.owner._actions[sid].values() if a['control_id'] == cid]

        def outcome(kind, prop, plane, current, *, goal=True, preserve=True):
            reference = self.ref('o', {'target': target, 'kind': kind, 'property': prop, 'plane': plane})
            key = (prop+('' if current else '_unknown') if kind == 'state'
                   else 'value' if kind == 'display_value' else kind)
            outcomes[key] = reference

        if (c['role'] in _TEXT and isinstance(c.get('value'), str)
                and proof.get('precision') == 'exact' and proof.get('exact_value_proven') is True
                and proof.get('plane') == 'editor_buffer'):
            outcome('text', 'value', 'editor_buffer', True)
        if c['role'] in _DISPLAY and isinstance(_display(c), str):
            outcome('calculation', 'numeric_display', 'display', True, preserve=False)
        if c['role'] not in _TEXT and isinstance(c.get('value'), str):
            outcome('display_value', 'value', 'display', True, goal=False)
        states = c.get('states') or {}
        for prop in ('selected', 'checked'):
            conventional = ((prop == 'selected' and c['role'] in _SELECTED)
                            or (prop == 'checked' and c['role'] == 'AXCheckBox'))
            one_time_press = any(a['kind'] == 'press' for a in observed_actions)
            if type(states.get(prop)) is bool or (states.get(prop) is None and (prop in states or conventional or one_time_press)):
                outcome('state', prop, 'display', type(states.get(prop)) is bool,
                        preserve=type(states.get(prop)) is bool)
        inputs = {}
        for action in observed_actions:
            if action['kind'] in ('press', 'set_text'):
                reference = self.ref('i', {'target': target, 'operation': action['kind'],
                                          'action_id': action['id']})
                old = inputs.get(action['kind'])
                inputs[action['kind']] = reference if old is None else [*(old if isinstance(old, list) else [old]), reference]
        fields.pop('operations', None)
        fields.update(outcomes=outcomes, inputs=inputs)
        return fields

    def _field(self, value, target, *, limit=256):
        # Capability lists contain only bounded issued references and types;
        # deferring these small lists would hide the actual public contract.
        result = super()._field(value, target, limit=limit)
        if isinstance(result, dict) and result.get('deferred'):
            result['next'] = {'reference': target}
        return result

    def _control(self, o, cid):
        fields = self._semantic_fields(o, cid)
        return {key: (deepcopy(value) if key in ('outcomes', 'inputs')
                      else self._field(value, fields['target'])) for key, value in fields.items()}

    def _routes(self, result):
        """Change addresses only; do not interpret ordinary UI strings."""
        result = deepcopy(result)
        coverage = result.get('coverage')
        if isinstance(coverage, dict):
            continuation = coverage.get('continue_with')
            if isinstance(continuation, dict) and 'cursor' in continuation and 'view' in continuation:
                coverage['continue_with'] = {'reference': continuation['cursor']}
        for item in result.get('items', []):
            if not isinstance(item, dict):
                continue
            if 'region' in item and 'target' not in item:
                item['inspect'] = {'reference': item['region']}
        view = result.get('view')
        if view:
            result['routes'] = {'overview': {'reference': view},
                                'search_all': {'reference': view}}
        result.pop('projection_version', None)
        result.pop('private_evidence', None)
        result.pop('detail_semantics', None)
        return result

    def _page(self, page, original_args=None):
        result = self._routes(super()._page(page, original_args))
        result['capabilities_are_eligibility_not_binding'] = True
        return result

    def _detail(self, o, cid):
        result = super()._detail(o, cid)
        result['items'] = [row for row in result['items'] if not (
            row.get('kind') == 'json_fragment' and row.get('field') in ('outcomes', 'inputs'))]
        result['coverage'].update(matched_total=len(result['items']), returned_count=len(result['items']))
        return result

    def _apps_page(self, raw, args):
        result = super()._apps_page(raw, args)
        if result.get('next_start') is not None:
            reference = self.ref('p', {'step_application_page': True,
                'query': args.get('query', ''), 'inventory_id': result['inventory_id'],
                'start': result['next_start'], 'limit': args.get('limit', 32)})
            result['continue_with'] = {'reference': reference}
        for key in ('inventory_id', 'next_start', 'start', 'raw_inventory_count'):
            result.pop(key, None)
        for row in result['items']:
            row['inspect'] = {'reference': row['app_id']}
        return result

    def _project(self, name, raw, args=None):
        result = self._routes(super()._project(name, raw, args))
        if 'overview' in result:
            result['overview'] = self._routes(result['overview'])
        for row in result.get('windows', []):
            row['inspect'] = {'reference': row['window_id']}
        if name == 'locua_launch':
            result['next'] = 'Inspect the application reference for its windows.'
        return result

    def _translate(self, name, args):
        if self._retained_overview and name == 'locua_inspect':
            self._projection_page = None
            o = self.observation(args['view'])
            return {'snapshot_id': o['snapshot_id'], 'operation': 'overview'}
        return super()._translate(name, args)

    def _review(self, args):
        contract = super()._review(args)
        observation = self.owner._observation(contract['snapshot_id'])
        for index, effect in enumerate(contract['effects']):
            if effect['kind'] != 'goal': continue
            goal = next(g for g in contract['goals'] if g['id'] == effect['goal_id'])
            if goal['kind'] != 'state': continue
            control = self.owner._find(observation, goal['control_id'])
            if control.get('states', {}).get(goal['property']) is None:
                # Missing and explicitly null are equally unknown. Preserve
                # the future predicate, but request the existing one-time press
                # effect, never a toggle justified by an invented false value.
                contract['effects'][index] = {'kind': 'press', 'control_id': control['id'],
                    'purpose': 'Attempt the requested '+goal['property']+'='+str(goal['value'])+
                               '; initial state is unknown, fresh verification required'}
        return contract

    def _kind(self, reference):
        row = self.refs.get(reference)
        if row is None:
            raise InterfaceError('unknown_reference', 'Use an issued reference; no identifier may be invented.')
        return row[0]

    def _typed_reference(self, reference, kind, path, schema):
        try:
            return self.resolve(reference, kind)
        except InterfaceError as error:
            if error.code != 'unknown_reference': raise
            raise InterfaceError(error.code, path+': '+schema['description']+
                                 ' The supplied reference was not issued for this type.') from error

    def _input(self, reference, path='$.input'):
        row = self._typed_reference(reference, 'i', path, self._reference_schemas['input'])
        o, c = self.control(row['target'])
        action = self.owner._actions[o['snapshot_id']].get(row['action_id'])
        if not action or action['control_id'] != c['id'] or action['kind'] != row['operation']:
            raise InterfaceError('input_unavailable', 'The retained input is unavailable; inspect current UI.')
        return row

    def _outcome(self, reference, path='$.outcome'):
        row = self._typed_reference(reference, 'o', path, self._reference_schemas['outcome'])
        self.control(row['target'])  # Immutable view validity, not binding/approval.
        return row

    def _compile(self, name, args):
        if name == 'locua_apps':
            return name, args
        if name == 'locua_launch':
            return name, {'app_id': args['app']}
        if name == 'locua_activate':
            return name, {'window_id': args['window']}
        if name in ('locua_inspect', 'locua_search'):
            ref = args['reference']; kind = self._kind(ref)
            if name == 'locua_search':
                if kind not in ('v', 'r'):
                    raise InterfaceError('search_scope_required', 'Search a view or region reference; inspect other reference types.')
                sid = self.resolve(ref, kind) if kind == 'v' else self.resolve(ref, kind)[0]
                call = {'view': self.ref('v', sid)}
                if kind == 'r': call['region'] = ref
                call.update({k: args[k] for k in ('query', 'role') if k in args})
                return 'locua_inspect', call
            if kind == 'a': return 'locua_windows', {'app_id': ref}
            if kind == 'w': return 'locua_observe', {'window_id': ref}
            if kind == 'v':
                self._retained_overview = True
                return 'locua_inspect', {'view': ref}
            if kind in ('r', 'c'):
                sid = self.resolve(ref, kind)[0]
                return 'locua_inspect', {'view': self.ref('v', sid), 'region' if kind == 'r' else 'target': ref}
            if kind == 'p':
                saved = self.resolve(ref, kind)
                if saved.get('step_application_page'):
                    return 'locua_apps', {k: saved[k] for k in ('query', 'inventory_id', 'start', 'limit')}
                if 'review' in saved:
                    return 'locua_status', {'review': saved['review'], 'cursor': ref}
                return 'locua_inspect', {'view': self.ref('v', saved['snapshot_id']), 'cursor': ref}
            raise InterfaceError('inspection_reference_type', 'Inspect application, window, view, region, control or page. Outcome and input references are for review/action.')
        if name == 'locua_review':
            goals, preserves, presses = [], [], []
            for index, selected in enumerate(args['goals']):
                path = f'$.goals[{index}]'
                cap = self._outcome(selected['outcome'], path+'.outcome')
                if cap['kind'] == 'display_value':
                    raise InterfaceError('preservation_only', path+'.outcome: This displayed-value reference supports preserves[], not a goal. Choose an eligible observed outcome; a calculation value is the original requested expression, not a predicted result.')
                goal = {'kind': cap['kind'], 'target': cap['target']}
                goal['expression' if cap['kind'] == 'calculation' else 'value'] = selected['value']
                if cap['kind'] == 'state': goal['property'] = cap['property']
                if 'persistence_requirement' in selected:
                    if cap['kind'] != 'text':
                        raise InterfaceError('persistence_requires_text', path+'.persistence_requirement: Omit this field for calculation/state outcomes. It belongs only to exact text/editor goals and must reflect the actual user requirement.')
                    goal['persistence_requirement'] = selected['persistence_requirement']
                goals.append(goal)
            for index, reference in enumerate(args.get('preserves', [])):
                cap = self._outcome(reference, f'$.preserves[{index}]')
                if cap['kind'] == 'calculation':
                    raise InterfaceError('preservation_capability_required', 'Choose the displayed-value or exact editor/state preservation reference, not a calculation reference.')
                preserves.append({'target': cap['target'], 'property': cap['property']})
            for index, reference in enumerate(args.get('navigation', [])):
                cap = self._input(reference, f'$.navigation[{index}]')
                if cap['operation'] != 'press':
                    raise InterfaceError('navigation_requires_press', 'Navigation requires an observed press input; review editor input as an exact text goal.')
                presses.append({'target': cap['target'], 'purpose': args['summary']})
            return name, {'summary': args['summary'], 'goals': goals, 'preserve': preserves,
                          'presses': presses, 'covers_request': args['covers_request'],
                          'unresolved': args.get('unresolved', [])}
        if name == 'locua_act':
            cap = self._input(args['input'])
            return name, {'target': cap['target'], 'operation': cap['operation'],
                          **{k: args[k] for k in ('value', 'review') if k in args}}
        if name == 'locua_act_sequence':
            steps = []
            for index, reference in enumerate(args['inputs']):
                cap = self._input(reference, f'$.inputs[{index}]')
                if cap['operation'] != 'press':
                    raise InterfaceError('sequence_requires_press', 'This sequence accepts press inputs only. Use act with the exact reviewed value for an editor.')
                steps.append({'target': cap['target'], 'operation': 'press'})
            return name, {'steps': steps, **({'review': args['review']} if 'review' in args else {})}
        return name, args

    def _remember_focus(self, name, args, result):
        if result.get('status') not in ('ok', 'observed'):
            return
        if name not in ('locua_inspect', 'locua_search'):
            return
        page = result.get('overview', result)
        if not page.get('view') or not isinstance(page.get('items'), list):
            return
        self._focus = {'selected_reference': args['reference'], 'view': page['view'],
                       'items': deepcopy(page['items']), 'coverage': deepcopy(page.get('coverage', {})),
                       'routes': deepcopy(page.get('routes', {})),
                       'potentially_stale': True, 'action_authority': False}
        for row in page['items']:
            if not isinstance(row, dict) or not isinstance(row.get('target'), str): continue
            if row.get('role') not in _DISPLAY: continue
            target = row['target']
            self._readable.pop(target, None)
            self._readable[target] = {k: deepcopy(row[k]) for k in
                ('target', 'role', 'name', 'value', 'parent', 'outcomes', 'value_evidence') if k in row}
            while len(self._readable) > 8:
                self._readable.popitem(last=False); self._readable_evicted += 1

    def state(self, review=None, cursor=None):
        # Preserve canonical safety state without copying semantic-v1's last
        # twelve full control records again beside the selected focus page.
        state = ModelInterface.state(self, review, cursor)
        if review is None and cursor is None:
            state['model_hypotheses'] = {'revision': self._decision_revision,
                'decision': deepcopy(self._model_decision), 'authority': False,
                'previous_declarations': {key: [value for value in values
                    if value not in (self._model_decision or {}).get(key, [])]
                    for key, values in self._model_claims.items()},
                'declarations_are_fallible_not_exhaustive': True,
                'completion_claims_are_not_verification': True}
            state['plan_progress'] = reviewed_plan_progress(self.owner, lambda sid: self.ref('q', sid))
            state['focus'] = deepcopy(self._focus)
            focused = {row.get('target') for row in (self._focus or {}).get('items', []) if isinstance(row, dict)}
            state['readable_evidence'] = {'items': deepcopy([row for key, row in self._readable.items() if key not in focused]),
                'also_in_focus': [key for key in self._readable if key in focused],
                'potentially_stale': True, 'action_authority': False, 'complete_history': False,
                'retained_limit': 8, 'evicted_records': self._readable_evicted,
                'other_evidence': 'Inspect current view/regions or search; retained notes are not exhaustive.'}
            state['explored_routes'] = {
                'controls': [{'reference': key, 'view': row['view']} for key, row in self._explored_controls.items()],
                'regions': [{'reference': key, 'view': row['view']} for key, row in self._explored_regions.items()],
                'complete_history': False, 'potentially_stale': True}
        return state

    def _nonprogress(self, name, args, result):
        # Issuing new typed aliases or changing the model's plan is not new
        # desktop evidence. The original counter still sees semantic target
        # positions, operation types, values and canonical scope issuance.
        def without_aliases(value):
            if isinstance(value, dict):
                return {k: without_aliases(v) for k, v in value.items()
                        if k not in ('outcome', 'input', 'routes', 'inspect', 'model_hypotheses', 'plan_progress')}
            if isinstance(value, list): return [without_aliases(v) for v in value]
            return value
        compared = without_aliases(result)
        def typed_maps(value):
            if isinstance(value, dict):
                return {k: (sorted(v) if k in ('outcomes', 'inputs') and isinstance(v, dict) else typed_maps(v))
                        for k, v in value.items()}
            if isinstance(value, list): return [typed_maps(v) for v in value]
            return value
        compared = typed_maps(compared)
        super()._nonprogress(name, args, compared)
        for field in ('exploration_feedback', 'nonprogress', 'execution_stopped'):
            if field in compared: result[field] = deepcopy(compared[field])

    def state_text(self):
        state = self.state()
        # The full original request is already protected in the system message.
        state.pop('original_request', None)
        return ('Current decision frame. Original request is authoritative; model hypotheses and retained UI are not approval or fresh verification.\n'
                + _json(state))

    def _record_decision(self, decision):
        claims = deepcopy(self._model_claims)
        for key in claims:
            claims[key] = list(dict.fromkeys([*claims[key], *decision[key]]))
        retained = {'decision': decision, 'previous_declarations': {
            key: [value for value in values if value not in decision[key]] for key, values in claims.items()}}
        if len(_json(retained).encode()) > MODEL_CLAIM_BYTES:
            raise InterfaceError('model_claim_budget', 'Model hypotheses exceed the retained claim budget; no declarations were dropped. Use a shorter plan or continue without a decision update. Full request and owner restrictions remain unchanged.')
        self._model_claims = claims
        self._model_decision = deepcopy(decision)
        self._decision_revision += 1

    def call(self, name, args):
        with self.owner._lock:
            if self.owner._cancellation is not None:
                return deepcopy(self.owner._cancellation)
            self._public_calls += 1
            public_args = deepcopy(args); base_name = None; base_args = None; result = None
            delegated = False
            self._retained_overview = False
            try:
                if name not in self.specs:
                    raise InterfaceError('unknown_tool', 'Use a registered step interface tool.')
                errors = _errors(self.specs[name][1], args)
                reference_fields = _reference_fields(name, args, self._reference_schemas)
                deferred = []
                for error in errors:
                    if error['code'] == 'pattern' and error['path'] in reference_fields:
                        error['message'] = reference_fields[error['path']]['description']
                        deferred.append(error)
                structural = [error for error in errors if error not in deferred]
                if structural:
                    raise InterfaceError('argument_contract_invalid', '; '.join(e['path']+': '+e['message'] for e in structural))
                args = deepcopy(args)
                decision = args.pop('decision', None)
                # Exact issued types distinguish incompatible fields from actual
                # text requirements. Unknown references still retain declarations
                # before binding; known nontext must not invent a text requirement.
                declarations, nontext_persistence = [], []
                for index, goal in enumerate(args.get('goals', [])):
                    if 'persistence_requirement' not in goal:
                        continue
                    issued = self.refs.get(goal['outcome'])
                    if issued and issued[0] == 'o' and issued[1]['kind'] != 'text':
                        nontext_persistence.append(index)
                    else:
                        declarations.append({'kind': 'text', 'target': goal['outcome'],
                                             'persistence_requirement': goal['persistence_requirement']})
                persistence = (self.owner._text_persistence(declarations, stage='before_step_reference_binding')
                               if name == 'locua_review' else None)
                if persistence is not None and not persistence['all_requirements_satisfied']:
                    result = self.owner._persistence_refusal(persistence)
                    self.owner._write_evidence()
                else:
                    if nontext_persistence:
                        index = nontext_persistence[0]
                        raise InterfaceError('persistence_requires_text',
                            f'$.goals[{index}].persistence_requirement: Omit this field for the issued nontext outcome. '
                            'It belongs only to exact text/editor goals and must reflect the actual user requirement. '
                            'No requirement was removed from any earlier text declaration.')
                    # New reference shape checks must not bypass retention of
                    # a structurally valid, unsupported persistence declaration.
                    if deferred:
                        raise InterfaceError('argument_contract_invalid', '; '.join(e['path']+': '+e['message'] for e in deferred))
                    if decision is not None:
                        self._record_decision(decision)
                    base_name, base_args = self._compile(name, args)
                    public_specs = self.specs
                    try:
                        self.specs = self._base_specs
                        delegated = True
                        result = super().call(base_name, base_args)
                    finally:
                        self.specs = public_specs
                    self._remember_focus(name, args, result)
                    if name in ('locua_review', 'locua_act', 'locua_act_sequence', 'locua_verify'):
                        result['plan_progress'] = reviewed_plan_progress(self.owner, lambda sid: self.ref('q', sid))
            except InterfaceError as error:
                result = {'status': 'refused', 'code': error.code, 'reason': str(error),
                          'action_started': False, **deepcopy(error.detail)}
            except Exception as error:
                result = self._stop(error, result, delegated)
            finally:
                self._retained_overview = False
            if not delegated and self.owner._cancellation is None:
                try: self._nonprogress(name, public_args, result)
                except Exception as error: result = self._stop(error, result, False)
            record = {'sequence': self._public_calls, 'tool': name, 'input': public_args,
                      'compiled_tool': base_name, 'compiled_arguments': base_args,
                      'model_decision_revision': self._decision_revision, 'result': deepcopy(result)}
            try:
                private_json(self.owner.out/f'step-{self._public_calls:03d}.json', record)
            except Exception as error:
                result = self._stop(error, result, delegated)
            return result

    def _stop(self, error, receipt, dispatched):
        receipt = receipt if isinstance(receipt, dict) else {}
        result = {'status': 'blocked', 'code': 'interface_result_unavailable',
                  'reason': 'Step interface representation/recording failed: '+str(error),
                  'action_started': receipt.get('action_started', None if dispatched else False),
                  'operation_status': receipt.get('status'), 'no_retry': True,
                  'execution_stopped': True, 'authority_revoked': True, 'task_complete': False,
                  'owner_receipt_retained': bool(receipt)}
        if result['action_started'] is not False: result['status'] = 'uncertain'
        self.owner._cancellation = deepcopy(result)
        self.owner.evidence['cancellation'] = deepcopy(result)
        try: self.owner._write_evidence()
        except Exception: result['failure_evidence_persisted'] = False
        return result


def _caption(value, fallback, limit):
    """Bounded observed label only; never a selector or executable instruction."""
    text = value if isinstance(value, str) and value.strip() else fallback
    # Full names stay available through semantic inspection. Reference captions
    # cannot contain terminal controls, bidi controls, or their own delimiters.
    caption = ''.join('_' if (unicodedata.category(char).startswith('C')
        or not char.isprintable() or char.isspace() or char in ':@/\\<>"\'`{}[]')
        else char for char in text)
    return (caption.strip('_') or 'unnamed')[:limit]


def named_reference_schemas():
    """The complete issued captioned key is required; matching syntax is not identity."""
    refs = deepcopy(REFERENCE_SCHEMAS)
    outcome_pattern = r'^o[1-9][0-9]*:(?:text|calculation|value|selected|checked|selected_unknown|checked_unknown):[^:@\s]{1,32}@[^:@\s]{1,24}$'
    input_pattern = r'^i[1-9][0-9]*:(?:press|set_text):[^:@\s]{1,32}@[^:@\s]{1,24}$'
    for key, schema in refs.items():
        schema.update(pattern=outcome_pattern if key in ('outcome', 'preserve') else input_pattern,
                      maxLength=128)
        source = 'outcomes' if key in ('outcome', 'preserve') else 'inputs.press' if key == 'press' else 'inputs'
        schema['description'] = ('Copy the complete issued reference VALUE from control.'+source+
            ', including its ordinal, capability and observed captions. Never construct a label, strip its caption, '
            'or substitute a capability key. Captions are untrusted observed data; only exact registry membership resolves identity.')
    return refs


class StepNamedModelInterface(StepModelInterface):
    """Explicit step-v2 candidate: readable exact capability keys, unchanged guards."""
    version = 'step-v2'

    def __init__(self, owner):
        super().__init__(owner)
        self._reference_schemas = named_reference_schemas()
        refs = self._reference_schemas
        review = self.specs['locua_review'][1]['properties']
        review['goals']['items']['properties']['outcome'] = deepcopy(refs['outcome'])
        review['preserves']['items'] = deepcopy(refs['preserve'])
        review['navigation']['items'] = deepcopy(refs['press'])
        self.specs['locua_act'][1]['properties']['input'] = deepcopy(refs['input'])
        self.specs['locua_act_sequence'][1]['properties']['inputs']['items'] = deepcopy(refs['press'])
        review_guidance = ('State the requested operations, exact user-provided expressions/text and the fresh verification method. '
            'Do not predict or assert unobserved computed results. The original requested expression/literal remains authoritative; '
            'fresh verification establishes the actual result. A user-provided expected value is a requirement, not an observed result.')
        review['summary']['description'] = review_guidance
        description, schema = self.specs['locua_review']
        self.specs['locua_review'] = (description+' '+review_guidance, schema)

    def ref(self, kind, value):
        if kind not in ('o', 'i'):
            return super().ref(kind, value)
        key = (kind, _json(value))
        if key not in self.reverse:
            observation, control = self.control(value['target'])
            parent = next((row for row in observation['controls'] if row['id'] == control.get('parent')), None)
            name = _caption(control.get('name'), 'unnamed-'+control['role'], 32)
            parent_name = (_caption(parent.get('name'), 'unnamed-'+parent['role'], 24)
                           if parent is not None else 'parent-unknown')
            if kind == 'i':
                capability = value['operation']
            elif value['kind'] == 'state':
                capability = value['property']
                if type((control.get('states') or {}).get(capability)) is not bool:
                    capability += '_unknown'
            else:
                capability = {'text': 'text', 'calculation': 'calculation', 'display_value': 'value'}[value['kind']]
            number = self.counts.get(kind, 0) + 1
            label = f'{kind}{number}:{capability}:{name}@{parent_name}'
            if len(label) > 128:
                raise InterfaceError('reference_budget', 'Issued capability reference exceeds its bounded contract.')
            # The suffix is never parsed on input; it is part of the exact key.
            self.counts[kind] = number
            self.reverse[key] = label
            self.refs[label] = (kind, deepcopy(value))
        return self.reverse[key]
