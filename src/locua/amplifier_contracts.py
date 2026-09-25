"""Published native tool arguments and their matching, non-coercing validator.

This module validates representation only. Observed identity, authority, current
state, and user-reviewed request coverage remain the tool implementation's job.
"""
from copy import deepcopy
import json
import math
import re


S = {'type': 'string'}
ID = {'type': 'string', 'minLength': 1}
TEXT = {'type': 'string', 'pattern': r'\S'}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 128}
START = {'type': 'integer', 'minimum': 0}
BOOL = {'type': 'boolean'}
PERSISTENCE = {'type': 'string', 'enum': ['not_requested', 'backing_file_unchanged', 'saved_output_required'],
    'description': 'Explicit user requirement, not a capability claim. not_requested means no backing-file constraint. Unchanged bytes or saved output require separate trusted evidence; no Save call is not unchanged-file proof.'}


def obj(properties, required=(), **extra):
    return {'type': 'object', 'properties': deepcopy(properties),
            'required': list(required), 'additionalProperties': False, **extra}


def _schema_wire_types(schema):
    """Resolve transport types from authored schema, never generated values."""
    declared = schema.get('type')
    if isinstance(declared, str): return {declared}
    if isinstance(declared, list) and declared and all(isinstance(x, str) for x in declared):
        return set(declared)
    literals = [schema['const']] if 'const' in schema else schema.get('enum')
    if not isinstance(literals, list) or not literals: return None
    types = {type(None):'null', bool:'boolean', int:'integer', float:'number',
             str:'string', list:'array', dict:'object'}
    if any(type(value) not in types for value in literals): return None
    return {types[type(value)] for value in literals}


def choice(branches):
    # Native XML has no scalar type tags. Preserve explicit transport types for
    # the flattened properties, while oneOf keeps every branch restriction.
    # Different const/enum values are not different transport types. Mixed
    # types remain an explicit union, never guessed from generated content.
    variants = {}
    for branch in branches:
        for key, value in branch['properties'].items():
            variants.setdefault(key, []).append(value)
    properties = {}
    for key, values in variants.items():
        # Python equality conflates schema literals false/0 and true/1.
        same = len({json.dumps(v, sort_keys=True, allow_nan=False) for v in values}) == 1
        result = deepcopy(values[0]) if same else {}
        types = [_schema_wire_types(v) for v in values]
        if all(t is not None for t in types):
            union = sorted(set().union(*types))
            result['type'] = union[0] if len(union) == 1 else union
        properties[key] = result
    common_required = set(branches[0].get('required', ()))
    for branch in branches[1:]: common_required.intersection_update(branch.get('required', ()))
    return obj(properties, sorted(common_required), oneOf=deepcopy(branches))


def array(items, *, minimum=0, maximum=None, description=None):
    result = {'type': 'array', 'items': deepcopy(items), 'minItems': minimum}
    if maximum is not None: result['maxItems'] = maximum
    if description is not None: result['description'] = description
    return result


_GOAL_COMMON = {'id': ID, 'target': TEXT, 'control_id': ID}
GOAL = choice([
    obj({**_GOAL_COMMON, 'kind': {'const': 'text'}, 'value': S,
         'evidence_plane': {'const': 'editor_buffer'}},
        ('id', 'kind', 'target', 'control_id', 'value', 'evidence_plane'),
        description='Change and verify the exact editor buffer. Naming a document or replacing its entire text does not authorize an additional Save operation. Add saving only when explicitly requested by the user; saved output requires separate evidence and cannot be proved by this buffer predicate.'),
    obj({**_GOAL_COMMON, 'kind': {'const': 'state'}, 'value': BOOL,
         'property': {'type': 'string', 'enum': ['checked', 'selected'], 'description':
             'Explicit display attribute to verify, including selected for choice buttons. '
             'An unknown initial value remains unknown. To act without known state, separately review '
             'one observed press; a goal-toggle effect still requires known fresh mismatch. '
             'Completion requires fresh explicit boolean readback of this property.'},
         'evidence_plane': {'const': 'display'}},
        ('id', 'kind', 'target', 'control_id', 'value', 'evidence_plane')),
    obj({**_GOAL_COMMON, 'kind': {'const': 'calculation'}, 'expression': TEXT,
         'evidence_plane': {'const': 'display'}},
        ('id', 'kind', 'target', 'control_id', 'expression', 'evidence_plane'),
        description='Bind the readable result control, not its window/container or an input button. Verification requires an issued observed full reset or whole-expression replacement, then expression evaluation and fresh result readback. A fresh zero display or Clear Entry does not prove a known start.'),
])
EFFECT = choice([
    obj({'kind': {'const': 'goal'}, 'goal_id': ID}, ('kind', 'goal_id'),
        description='Authorize only this goal. Calculation permits observed arithmetic inputs, including full reset or whole-expression replacement to establish a known start; fresh zero alone is insufficient. Inspect each action before act. No control_id, purpose or button recipe.'),
    obj({'kind': {'const': 'press'}, 'control_id': ID, 'purpose': TEXT},
        ('kind', 'control_id', 'purpose'), description='One explicitly reviewed observed press for navigation or a requested change. Separate from outcome verification; never authorizes automatic retries.'),
])
PRESERVE = choice([
    obj({'control_id': ID, 'property': {'const': 'value'}, 'value': S},
        ('control_id', 'property', 'value')),
    obj({'control_id': ID, 'property': {'enum': ['checked', 'selected']}, 'value': BOOL},
        ('control_id', 'property', 'value')),
])


def inspect_branch(operation, fields, required=()):
    return obj({'snapshot_id': ID, 'operation': {'const': operation}, **fields},
               ('snapshot_id', 'operation', *required))


INSPECTION_CURSOR = {**ID, 'description':
    'Pagination only: copy coverage.continuation exactly from the same snapshot, operation and filters. '
    'Omit on the first page. Never put a region ID, control ID or search text here.'}
INSPECT_SCHEMA = choice([
    inspect_branch('overview', {'cursor': INSPECTION_CURSOR, 'limit': LIMIT}),
    inspect_branch('list', {'region_id': {**ID, 'description':
                                'Observed region_id from the overview. Use operation=list; omit cursor to start this region.'},
                            'role': ID, 'query': {**S, 'minLength':1, 'maxLength':1024},
                            'cursor': INSPECTION_CURSOR, 'limit': LIMIT}),
    inspect_branch('control', {'control_id': ID, 'cursor': INSPECTION_CURSOR}, ('control_id',)),
])

STATUS_SCHEMA = choice([
    obj({'operation': {'enum': ['summary', 'windows', 'scopes', 'clarifications', 'exploration', 'controls']},
         'start': START, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 64}}),
    obj({'operation': {'enum': ['goals', 'effects', 'preserves', 'witness']},
         'scope_id': ID, 'start': START,
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 64}}, ('operation', 'scope_id')),
    obj({'operation': {'const': 'needs'},
         'needs': array({**TEXT, 'minLength': 1, 'maxLength': 256}, maximum=12,
                        description='Replace model-authored unresolved needs; no action authority.')},
        ('operation', 'needs')),
])
REVIEW_SCHEMA = obj({
    'snapshot_id': {**ID, 'description':'Use the latest retained snapshot. Age alone does not require recapture for review; act checks fresh state.'},
    'summary': TEXT, 'goals': array(GOAL, maximum=16),
    'effects': array(EFFECT, minimum=1, maximum=32),
    'preserves': array(PRESERVE, maximum=64),
    'limitations': array(S, description='Informational evidence/capability caveats, not extra requested work. Saving is not implied.'),
    'unresolved_requirements': array(TEXT, description='Explicit user-requested outcomes or restrictions this scope cannot fulfill. Nonempty means partial coverage.'),
    'covers_entire_request': {**BOOL, 'description':'Human must check against the complete original request. Requires goals and no unresolved_requirements.'},
}, ('snapshot_id', 'summary', 'goals', 'effects'), allOf=[{
    'if': {'properties': {'covers_entire_request': {'const': True}},
           'required': ['covers_entire_request']},
    'then': {'properties': {'goals': {'minItems': 1},
                            'unresolved_requirements': {'maxItems': 0}}},
}])
VERIFY_SCHEMA = choice([
    obj({'all': {'const': True}}, ('all',)),
    obj({'scope_id': ID, 'goal_id': ID, 'all': {'const': False}}, ('scope_id',)),
    obj({'scope_id': ID, 'reconcile': {'const': True}}, ('scope_id','reconcile'),
        description='Read-only reconciliation of the original uncertain text/state action. Freshly check every original goal and preserve without rebinding. Success grants no further input authority or delivery proof; recorded preservation failure and navigation/arithmetic uncertainty cannot be cleared.'),
    obj({'scope_id': ID, 'goal_id': ID, 'snapshot_id': ID, 'control_id': ID},
        ('scope_id', 'goal_id', 'snapshot_id', 'control_id'),
        description='Explicitly rebind a vanished read-only calculation result after a layout change, then freshly verify. The goal, input witness, window, constraints and input authority cannot change. Inspect and select the new readout first; no expected-value filtering.'),
])
SPECS = {
    'locua_apps': ('Discover observed apps; page/search a retained inventory. Other apps remain discoverable.',
        obj({'query': S, 'start': START, 'limit': {'type':'integer','minimum':1,'maximum':64}, 'inventory_id': ID})),
    'locua_windows': ('List exact windows of an observed app.', obj({'app_id': ID}, ('app_id',))),
    'locua_launch': ('Explicitly launch/reopen an observed app; not task completion.', obj({'app_id': ID}, ('app_id',))),
    'locua_activate': ('Explicitly activate one exact observed window; does not prove readability.', obj({'window_id': ID}, ('window_id',))),
    'locua_observe': ('Capture an exact observed window and return a compact overview. Recapture for changed/unknown UI, not merely to inspect or review retained evidence; act independently checks fresh state.', obj({'window_id': ID}, ('window_id',))),
    'locua_status': ('Recover original request, reviewed scopes and exploration after compaction. summary includes recent inspected pages and unresolved needs; exploration and controls page all retained discoveries with start/limit. Records are potentially stale, never action authority. operation=needs replaces model-authored unresolved needs without replacing the original request. No app read or verification.', STATUS_SCHEMA),
    'locua_inspect': ('Read the latest retained snapshot; age alone does not require observe before inspect/review. Never opens menus, switches tabs or scrolls. overview lists regions/readouts/role counts; list filters by observed role, region or semantic query; control reads exact detail. Follow coverage.continuation. Navigation requires review/act. Other controls stay discoverable; no expected-answer filtering.', INSPECT_SCHEMA),
    'locua_review': ('Review a plan from retained evidence; do not recapture just to review. act independently fresh-checks. Calculation goals bind a readable result control, not a window/container. A goal effect authorizes supported arithmetic inputs; no need to enumerate buttons before review. Inspect each action before act. Press effects are for navigation, not a redundant arithmetic recipe. Text goals authorize editor-buffer changes only: naming a document, replacing all its text or informational caveats do not imply saving. Add Save only when the user explicitly requests persistence; buffer verification cannot prove saved output. unresolved_requirements prevent full coverage. No task input.', REVIEW_SCHEMA),
    'locua_act': ('Issue one observed action within a reviewed scope; all fresh guards still apply. Calculation needs an issued observed full reset or whole-expression replacement before expression entry; fresh zero and Clear Entry are insufficient. A generic Clear/C control is conservatively entry-clearing, not a proven full reset. It may be issued within the calculation scope, but further arithmetic requires a known start; inspect the fresh controls for an explicit full reset when needed. Successful input returns a fresh overview. Uncertain input must not be repeated; follow its read-only reconciliation option when available. Inspect newly available controls or verify; inspection alone never changes app state.',
        obj({'scope_id': ID, 'snapshot_id': ID, 'action_id': ID, 'value': S}, ('scope_id', 'snapshot_id', 'action_id'))),
    'locua_act_sequence': ('Execute a short sequence you choose from action IDs already listed in one retained snapshot, within one approved scope. Prefer this to separate model turns when the next actions are already known and require no intervening discovery. Supply steps in intended order; repeated stable controls are allowed. Every step is independently remapped and freshly checked by the same guarded executor. No invented controls, coordinates, keyboard input, computed answers or authority expansion. Calculation still needs an issued full reset/replacement and the requested expression, then evaluation. Stops on the first uncertainty, changed target/control, navigation/layout change or refusal; inspect the returned fresh state before choosing further work. Partial receipts say exactly which inputs were attempted; never replay an entire partially issued sequence. Sequence completion is not task completion: separately verify the original reviewed scope.',
        obj({'scope_id': ID, 'snapshot_id': ID,
             'steps': array(obj({'action_id': ID, 'value': S}, ('action_id',)), minimum=1, maximum=32)},
            ('scope_id', 'snapshot_id', 'steps'))),
    'locua_verify': ('Freshly verify one reviewed scope/goal, or use only all=true for all reviewed goals. For an eligible uncertain text/state action, scope_id plus reconcile=true checks every original predicate without input or rebinding; success proves current predicates, not delivery, and never restores input authority. Preservation failures remain blocked. If a calculation result binding disappears after layout changes, inspect the new read-only result and supply scope_id, goal_id, snapshot_id, control_id to explicitly rebind it. This is read-only recovery, never new input authority; an existing mismatching result cannot be replaced to search for the expected answer.', VERIFY_SCHEMA),
    'locua_clarify': ('Ask for genuinely missing user information; answer grants no input authority.',
        obj({'question': {**TEXT, 'maxLength':1024}, 'reason': {**TEXT, 'maxLength':1024}}, ('question', 'reason'))),
}


class ArgumentContractError(ValueError):
    def __init__(self, tool, errors, accepted=()):
        self.tool, self.errors, self.accepted = tool, deepcopy(errors), sorted(accepted)
        super().__init__(tool + ' argument contract: ' + '; '.join(e['path'] + ': ' + e['message'] for e in errors))

    def as_result(self):
        return {'status':'refused', 'code':'argument_contract_invalid', 'reason':str(self),
                'errors':deepcopy(self.errors), 'accepted_arguments':self.accepted,
                'unknown_arguments':[e['path'][2:] for e in self.errors if e['code']=='unknown_argument'],
                'missing_required_arguments':[e['path'][2:] for e in self.errors if e['code']=='required'],
                'arguments_rewritten':False, 'action_started':False, 'task_complete':False}


def _same(a, b):
    return type(a) is type(b) and a == b


def _errors(schema, value, path='$'):
    """The small JSON Schema subset used above; unknown values are never fixed."""
    errors = []
    def fail(code, message, where=path): errors.append({'path':where, 'code':code, 'message':message})
    kind = schema.get('type')
    valid = {'object':lambda: isinstance(value,dict) and all(isinstance(k,str) for k in value),
             'array':lambda:isinstance(value,list), 'string':lambda:isinstance(value,str),
             'integer':lambda:type(value)is int, 'boolean':lambda:type(value)is bool,
             'number':lambda:type(value) in (int,float), 'null':lambda:value is None}
    if kind is not None:
        kinds = kind if isinstance(kind,list) else [kind]
        if not any(t in valid and valid[t]() for t in kinds):
            fail('type', 'Expected '+ ' or '.join(kinds)+'; no conversion is applied.'); return errors
    if 'const' in schema and not _same(value, schema['const']): fail('const', 'Expected '+repr(schema['const'])+'.')
    if 'enum' in schema and not any(_same(value,x) for x in schema['enum']): fail('enum', 'Choose one of '+repr(schema['enum'])+'.')
    if isinstance(value,str):
        for key, op in [('minLength',lambda a,b:a<b),('maxLength',lambda a,b:a>b)]:
            if key in schema and op(len(value),schema[key]): fail(key, key+' is '+str(schema[key])+'.')
        if 'pattern' in schema and re.search(schema['pattern'],value) is None: fail('pattern','Non-whitespace text is required.')
    if type(value)in(int,float):
        if type(value) is float and not math.isfinite(value): fail('finite','Non-finite numbers are unsupported.')
        for key, op in [('minimum',lambda a,b:a<b),('maximum',lambda a,b:a>b)]:
            if key in schema and op(value,schema[key]): fail(key,key+' is '+str(schema[key])+'.')
    if isinstance(value,list):
        for key, op in [('minItems',lambda a,b:a<b),('maxItems',lambda a,b:a>b)]:
            if key in schema and op(len(value),schema[key]): fail(key,key+' is '+str(schema[key])+'.')
        if 'items' in schema:
            for i,item in enumerate(value): errors += _errors(schema['items'],item,f'{path}[{i}]')
    if isinstance(value,dict):
        props = schema.get('properties',{})
        for key in schema.get('required',[]):
            if key not in value: fail('required','Required argument is missing.',path+'.'+key)
        for key,item in value.items():
            if key in props: errors += _errors(props[key],item,path+'.'+key)
            elif schema.get('additionalProperties') is False:
                fail('unknown_argument','Not accepted here. Accepted arguments: '+', '.join(sorted(props))+'.',path+'.'+key)
    if 'oneOf' in schema:
        branches = schema['oneOf']; results = [_errors(b,value,path) for b in branches]
        good = [r for r in results if not r]
        if len(good)!=1:
            selected=[]
            if isinstance(value,dict):
                for i,b in enumerate(branches):
                    discriminators={k:s for k,s in b.get('properties',{}).items() if 'const'in s or 'enum'in s}
                    if discriminators and any(k in value for k in discriminators) and all(k not in value or not _errors(s,value[k]) for k,s in discriminators.items()): selected.append(i)
            if len(selected)==1: errors += results[selected[0]]
            else: fail('oneOf','Arguments must match exactly one documented variant; check kind/operation and its required fields.')
    for branch in schema.get('allOf',[]): errors += _errors(branch,value,path)
    if 'if' in schema and not _errors(schema['if'],value,path): errors += _errors(schema.get('then',{}),value,path)
    # Top-level and selected branch can report the same issue; keep one copy.
    return [e for i,e in enumerate(errors) if e not in errors[:i]]


TEXT_PERSISTENCE_CONTRACT = 'text-persistence-v1'
LEGACY_PERSISTENCE_CONTRACT = 'legacy-buffer-only-comparison'
TEXT_PERSISTENCE_GUIDANCE = ('Every text goal must explicitly declare persistence_requirement: not_requested, '
    'backing_file_unchanged, or saved_output_required. Exact buffer evidence does not prove unchanged backing '
    'bytes; the application may autosave. Required backing-file outcomes are unsupported by this generic '
    'adapter and stop before edits.')


def persistence_specs(specs, *, contract=None):
    """Derive matching published and accepted schemas for one internal contract."""
    if contract not in (None, TEXT_PERSISTENCE_CONTRACT):
        raise ValueError('Unknown internal text persistence contract')
    selected=deepcopy(specs)
    if contract is None:return selected
    description,schema=selected['locua_review']
    goal=schema['properties']['goals']['items']
    goal['properties']['persistence_requirement']=deepcopy(PERSISTENCE)
    text=next(branch for branch in goal['oneOf'] if branch['properties']['kind'].get('const')=='text')
    text['properties']['persistence_requirement']=deepcopy(PERSISTENCE)
    text['required'].append('persistence_requirement')
    selected['locua_review']=(description+' '+TEXT_PERSISTENCE_GUIDANCE,schema)
    return selected


def validate_tool_arguments(name, arguments, *, specs=None):
    """Return None or raise actionable errors; never rewrite or authorize input."""
    selected=SPECS if specs is None else specs
    if not isinstance(name, str) or name not in selected:
        raise ArgumentContractError(str(name), [{'path':'$', 'code':'unknown_tool','message':'Choose a registered tool name.'}])
    schema=selected[name][1]
    errors=_errors(schema,arguments)
    if (name == 'locua_inspect' and isinstance(arguments, dict)
            and 'operation' in arguments and arguments['operation'] not in ('overview', 'list', 'control')):
        errors.append({'path':'$.operation', 'code':'unsupported_operation',
            'message':'Supported operations: overview, list, control. Use list with region_id, role or query to narrow observed items; control requires control_id.'})
    if (name == 'locua_inspect' and isinstance(arguments, dict)
            and arguments.get('operation') in ('overview', 'control') and 'region_id' in arguments):
        errors.insert(0, {'path':'$.region_id', 'code':'incompatible_operation',
            'message':"region_id requires operation='list'. To start inspecting that region, omit cursor; it is pagination only."})
    if not errors and name=='locua_review':
        goals=arguments['goals']; ids=[g['id'] for g in goals]
        for i,goal in enumerate(goals):
            if goal['id'] in ids[:i]: errors.append({'path':f'$.goals[{i}].id','code':'duplicate_goal','message':'Goal IDs must be unique.'})
        for i,effect in enumerate(arguments['effects']):
            if effect['kind']=='goal' and effect['goal_id'] not in ids:
                errors.append({'path':f'$.effects[{i}].goal_id','code':'unbound_goal','message':'Reference an ID in this review\'s goals.'})
    if errors: raise ArgumentContractError(name,errors,schema.get('properties',{}))
