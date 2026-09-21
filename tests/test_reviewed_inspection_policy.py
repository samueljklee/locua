"""CPU-only policy/guard tests. Scripted selectors are not accuracy evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from locua.engine.prototype.architecture import execute_plan
from locua.engine.prototype.context import compact_reviewed_view, reviewed_goal
from locua.engine.prototype.core import assess, build_candidates, run_loop
from locua.engine.prototype.observation_loop import _reviewed_route, choose_with_tools
from locua.engine.prototype.regions import catalog_regions, RegionError
from locua.engine.prototype.simulation import SimulatedDriver


def setup(padding=0):
    fixture = {'id': 'policy-fixture', 'controls': [
        {'key': 'root', 'role': 'main', 'name': 'Workspace'},
        {'key': 'protected', 'role': 'group', 'name': 'Protected', 'parent': 'root'},
        {'key': 'competitor', 'role': 'textbox', 'name': 'Label', 'parent': 'protected',
         'value': ' 00028\r\nΩ ', 'actions': ['type']},
        {'key': 'requested', 'role': 'group', 'name': 'Requested', 'parent': 'root'},
        *[{'key': 'extra'+str(i), 'role': 'textbox', 'name': 'Extra '+str(i), 'parent': 'requested',
           'value': str(i), 'actions': ['type']} for i in range(padding)],
        {'key': 'field', 'role': 'textbox', 'name': 'Label', 'parent': 'requested',
         'value': 'old', 'actions': ['type']},
        {'key': 'elsewhere', 'role': 'group', 'name': 'Elsewhere', 'parent': 'root'},
        {'key': 'other', 'role': 'checkbox', 'name': 'Other flag', 'parent': 'elsewhere',
         'states': {'checked': False}, 'actions': ['click']}]}
    task = {'id': 'reviewed', 'goal': 'Change only the reviewed field. Keep unrelated UI intact.',
        'target': {'target_id': 'simulated-browser', 'tab_id': 'policy-fixture'},
        'intents': [{'id': 'edit', 'kind': 'set_text', 'selector': {'role': 'textbox', 'name': 'Label',
            'ancestor': {'role': 'group', 'name': 'Requested'}}, 'value': ' 00037\r\nβ '}],
        'invariants': [{'selector': {'role': 'textbox', 'name': 'Label',
            'ancestor': {'role': 'group', 'name': 'Protected'}}, 'property': 'value', 'value': ' 00028\r\nΩ '}]}
    return SimulatedDriver(fixture), task


class ScriptedSelector:
    def __init__(self, override=None):
        self.calls = []; self.override = override
    def choose(self, **request):
        self.calls.append(deepcopy(request))
        choices = request['candidates']
        if self.override:
            selected = self.override(request)
        elif request['observation_summary'].startswith('UNTRUSTED UI overview'):
            selected = next(c['id'] for c in choices if c['description'] == 'Inspect region Requested')
        else:
            selected = next(c['id'] for c in choices if c['description'].startswith('Replace text of textbox "Label"'))
        return {'selected_id': selected, 'abstained': selected is None,
                'stages': [{'full_input_tokens': 101}], 'synthetic_selector': True}


class NativeSimulation:
    """Synthetic native handles only, with intentionally incomplete coverage."""
    def __init__(self):
        self.inner, self.task = setup()
        self.target = {'pid': 101, 'window_id': 202}
        self.task['target'] = self.target
        for item in [*self.task['intents'], *self.task['invariants']]:
            item['selector']['role'] = 'AXTextField'
            item['selector']['ancestor']['role'] = 'AXGroup'
        self.actions = []
    def observe(self):
        obs = self.inner.observe(); sid = 's' + str(self.inner.sequence).zfill(8)
        mapping = {c['id']: 'native:' + sid + ':' + c['id'].rsplit(':',1)[1] for c in obs['controls']}
        handles = {}
        for c in obs['controls']:
            c['id'] = mapping[c['id']]; c['parent'] = mapping.get(c['parent'])
            c['role'] = {'main':'AXWindow','group':'AXGroup','textbox':'AXTextField','checkbox':'AXCheckBox'}[c['role']]
            if 'type' in c['actions']:
                c['states'].update(value_settable=True, editable=True)
            c['actions'] = ['AXPress'] if 'click' in c['actions'] else []
            c['source'] = {'kind':'native','node':{}}
            if c['role'] in ('AXTextField','AXCheckBox'):
                token = c['id'].split(':',1)[1]
                handles[c['id']] = {'kind':'native',**self.target,'snapshot_id':sid,
                    'element_token':token,'element_index':int(token.split(':')[1])}
        obs.update(kind='native_window_state', target=self.target, snapshot_id=sid, handles=handles)
        obs['coverage']['complete'] = False
        return obs
    def dispatch(self, action):
        self.actions.append(deepcopy(action))
        self.inner.controls[action['handle']['element_index']]['value'] = action['value']
        return {'effect':'unverifiable','synthetic':True}


def choose(obs, task, selector, *, policy='reviewed_target_first', attempts=4):
    events = []
    result = choose_with_tools(obs, build_candidates(obs, task), selector,
        goal='Exact caller goal', history=[], emit=events.append, interrupted=lambda: None,
        max_inspections=attempts, search_labels=['Label'], inspection_policy=policy,
        reviewed_task=task, ledger={})
    return result, events


class ReviewedInspectionTests(unittest.TestCase):
    def test_same_policy_works_for_partial_native_capture_without_new_authority(self):
        driver = NativeSimulation()
        selector = ScriptedSelector(lambda request: next(c['id'] for c in request['candidates']
            if c['description'].startswith('Replace text of AXTextField "Label"')))
        result = run_loop(driver.task, driver, selector, regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'],'complete'); self.assertEqual(len(driver.actions),1)
        self.assertEqual(driver.actions[0]['handle']['kind'],'native')
        route = next(e for e in result['events'] if e['type']=='inspection_route')
        self.assertEqual(route['uniqueness_scope'],'captured_controls')
        self.assertFalse(route['source_complete'])

    def test_direct_inspection_removes_region_model_call_and_keeps_scope_guards(self):
        driver, task = setup(); selector = ScriptedSelector()
        result = run_loop(task, driver, selector, regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(selector.calls), 1)
        self.assertEqual(len(driver.actions), 1)
        self.assertEqual(driver.actions[0]['signature']['name'], 'Label')
        self.assertEqual(driver.controls[2]['value'], ' 00028\r\nΩ ')
        self.assertEqual(result['model_usage']['full_input_tokens'], 101)
        self.assertEqual(result['model_usage']['calls_started'], 1)
        self.assertEqual(result['model_usage']['inspection_requests'], 0)
        route = next(e for e in result['events'] if e['type'] == 'inspection_route')
        self.assertEqual(route['route'], 'direct_inspect')
        self.assertFalse(route['authorizes_action']); self.assertFalse(route['expected_values_used_for_routing'])

    def test_explicit_model_led_matches_legacy_default_inputs(self):
        requests = []
        for explicit in (False, True):
            driver, task = setup(); selector = ScriptedSelector()
            result = run_loop(task, driver, selector, regions=True, observation_tools=True,
                              **({'inspection_policy': 'model_led'} if explicit else {}))
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(len(selector.calls), 2)
            requests.append(selector.calls)
        self.assertEqual(requests[0], requests[1])
        self.assertTrue(requests[0][0]['observation_summary'].startswith('UNTRUSTED UI overview'))

    def test_target_page_jump_retains_earlier_later_and_other_region_tools(self):
        driver, task = setup(padding=35); obs = driver.observe()
        selector = ScriptedSelector(lambda request: None)
        _, events = choose(obs, task, selector)
        route = next(e for e in events if e['type'] == 'inspection_route')
        self.assertGreater(route['page_offset'], 0)
        request = selector.calls[0]; view = json.loads(request['observation_summary'].split('\n', 1)[1])
        self.assertTrue(any(r.get('name') == 'Label' and r.get('membership') == 'primary' for r in view['items']))
        ids = {c['id'] for c in request['candidates']}
        self.assertIn('first_page', ids); self.assertIn('overview', ids)
        self.assertGreater(view['coverage']['previous_page_count'], 0)
        self.assertFalse(view['page_is_not_global_uniqueness_or_absence_proof'] is False)
        # Return to page one, then continue normally. No rows are permanently pruned.
        sequence = iter(['first_page', 'next_page', None])
        selector = ScriptedSelector(lambda request: next(sequence))
        choose(obs, task, selector, attempts=3)
        offsets = [json.loads(c['observation_summary'].split('\n',1)[1])['coverage']['previous_page_count'] for c in selector.calls]
        self.assertEqual(offsets[1:], [0, 16])

    def test_competitors_and_unrelated_primary_actions_remain_in_view_and_guarded(self):
        driver, task = setup(padding=1); obs = driver.observe(); before = deepcopy(obs)
        selector = ScriptedSelector(lambda request: None); choose(obs, task, selector)
        view = json.loads(selector.calls[0]['observation_summary'].split('\n',1)[1])
        labels = [r for r in view['items'] if r.get('name') == 'Label']
        self.assertEqual({r['membership'] for r in labels}, {'primary', 'context'})
        self.assertTrue(view['competitors_in_full_region'])
        self.assertTrue(any('Extra 0' in c['description'] for c in selector.calls[0]['candidates']))
        self.assertEqual(obs, before)
        bad = ScriptedSelector(lambda request: next(c['id'] for c in request['candidates'] if 'Extra 0' in c['description']))
        driver, task = setup(padding=1)
        result = run_loop(task, driver, bad, regions=True, observation_tools=True, inspection_policy='reviewed_target_first')
        self.assertEqual(result['reason'], 'outside_pending_task_scope_or_dependency')
        self.assertFalse(driver.actions)

    def test_stale_foreign_and_unbound_observations_use_discovery_without_cached_binding(self):
        mutations = [lambda o: o.update(observed_at_ns=o['observed_at_ns']-31_000_000_000),
                     lambda o: o['target'].update(tab_id='foreign'),
                     lambda o: o['handles'].clear()]
        for mutate in mutations:
            driver, task = setup(); obs = driver.observe(); mutate(obs)
            selector = ScriptedSelector(lambda request: None)
            _, events = choose(obs, task, selector)
            self.assertTrue(selector.calls[0]['observation_summary'].startswith('UNTRUSTED UI overview'))
            self.assertEqual(next(e for e in events if e['type']=='inspection_route')['route'], 'discovery')
        driver, task = setup(); obs = driver.observe(); stale_catalog = catalog_regions(obs)
        obs['observed_at_ns'] += 1
        route, reason = _reviewed_route(obs, task, {}, stale_catalog)
        self.assertIsNone(route); self.assertEqual(reason['reason'], 'stale_region_catalog')

    def test_ambiguous_target_never_resolves_by_desired_value(self):
        driver, task = setup()
        duplicate = deepcopy(next(c for c in driver.controls if c['key']=='field'))
        duplicate.update(key='twin', value=task['intents'][0]['value'])
        driver.controls.append(duplicate)
        selector = ScriptedSelector(lambda request: None)
        _, events = choose(driver.observe(), task, selector)
        self.assertEqual(next(e for e in events if e['type']=='inspection_route')['route'], 'discovery')
        self.assertTrue(selector.calls[0]['observation_summary'].startswith('UNTRUSTED UI overview'))
        driver.sequence = 0
        result = run_loop(task, driver, ScriptedSelector(), regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'], 'blocked'); self.assertFalse(driver.actions)

    def test_flat_or_ambiguous_region_returns_to_discovery(self):
        driver, task = setup(); obs = driver.observe()
        with patch('locua.engine.prototype.observation_loop.revalidate_region', side_effect=RegionError('ambiguous')):
            result, basis = _reviewed_route(obs, task, {}, catalog_regions(obs))
        self.assertIsNone(result); self.assertEqual(basis['reason'], 'region_not_uniquely_stable')
        for c in obs['controls']: c['parent'] = None
        task['intents'][0]['selector'] = {'role':'textbox','name':'Extra unique'}
        target = next(c for c in obs['controls'] if c['name']=='Label'); target['name'] = 'Extra unique'
        task['invariants'] = []
        result, basis = _reviewed_route(obs, task, {}, catalog_regions(obs))
        self.assertIsNone(result); self.assertEqual(basis['reason'], 'no_bound_structural_region')

    def test_changed_target_after_selection_cannot_dispatch(self):
        driver, task = setup(); base_observe = driver.observe
        def observe():
            obs = base_observe()
            if driver.sequence == 2:
                next(c for c in obs['controls'] if c['name']=='Label' and c['value']=='old')['name'] = 'Changed'
            return obs
        driver.observe = observe
        result = run_loop(task, driver, ScriptedSelector(), regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'], 'blocked'); self.assertFalse(driver.actions)

    def test_unverified_effect_stops_without_retry(self):
        driver, task = setup(); driver.uncertain_effect = True
        result = run_loop(task, driver, ScriptedSelector(), regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['reason'], 'effect_unverified_no_retry')
        self.assertEqual(len(driver.actions), 1)

    def test_missing_token_telemetry_is_unknown_and_failed_call_is_counted(self):
        driver, task = setup()
        class Fail:
            def choose(self, **request): raise RuntimeError('mock failure before reply')
        result = run_loop(task, driver, Fail(), regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['model_usage']['calls_started'], 1)
        self.assertEqual(result['model_usage']['calls_completed'], 0)
        self.assertIsNone(result['model_usage']['full_input_tokens'])
        self.assertFalse(driver.actions)

    def test_compact_context_is_lossless_for_shared_unknown_states_and_ancestry(self):
        view = {'items': [{'id': str(i), 'role':'textbox','name':name,'value': ' 00028\r\nΩ ',
                          'states': {'enabled':None}, 'actions':['type'],
                          'semantics': {'description':'same','ancestors':[{'role':'group','name':'Repeated'}]}}
                         for i,name in enumerate(['same','same','different'])],
                'competitors_in_full_region':['0','1'], 'coverage':{'complete':False}}
        original = deepcopy(view); compact = compact_reviewed_view(view)
        for row in compact['items']:
            row.update(compact['state_actions'][row.pop('state_actions_ref')])
            row['semantics'].update(compact['ancestor_sets'][row['semantics'].pop('ancestors_ref')])
        self.assertEqual(compact['items'], original['items']); self.assertEqual(view, original)
        self.assertEqual(compact['coverage'], view['coverage'])

    def test_reviewed_goal_preserves_authority_exact_values_and_current_id_once(self):
        driver, task = setup(); obs = driver.observe(); state = assess(task, obs)
        goal = reviewed_goal(task, state, task['intents'][0], {})
        data = json.loads(goal.split('\n',1)[1])
        self.assertEqual(data['request'], task['goal']); self.assertEqual(data['intents'], task['intents'])
        self.assertEqual(data['invariants'], task['invariants']); self.assertEqual(data['current_intent_id'], 'edit')
        self.assertNotIn('EXTERNAL TASK STATE', goal)

    def test_later_requests_reference_verified_intent_and_keep_literal_in_authority(self):
        driver, task = setup(padding=1)
        task['intents'].append({'id':'second','kind':'set_text','selector':{'role':'textbox','name':'Extra 0'},
                                'value':'other exact\r\nΩ ', 'requires':['edit']})
        def select(request):
            wanted = '"Label"' if not request['history'] else '"Extra 0"'
            literal = task['intents'][0 if not request['history'] else 1]['value']
            return next(c['id'] for c in request['candidates'] if c['description'].startswith('Replace text')
                        and wanted in c['description'] and 'with exact '+json.dumps(literal,ensure_ascii=False) in c['description'])
        selector = ScriptedSelector(select)
        result = run_loop(task, driver, selector, regions=True, observation_tools=True,
                          inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'],'complete'); self.assertEqual(len(selector.calls),2)
        history = selector.calls[1]['history']
        self.assertEqual(len(history),1)
        self.assertIn('reviewed intent edit',history[0]['action'])
        self.assertIn('fresh snapshot',history[0]['outcome'])
        self.assertNotIn(task['intents'][0]['value'],json.dumps(history,ensure_ascii=False))
        record = json.JSONDecoder().raw_decode(selector.calls[1]['goal'].split('\n',1)[1])[0]
        self.assertEqual(record['intents'],task['intents'])
        self.assertEqual(record['invariants'],task['invariants'])
        self.assertEqual(record['current_intent_id'],'second')

    def test_execute_plan_propagates_explicit_policy(self):
        driver, task = setup()
        plan = {'version':'locua-task-plan-v1','request':task['goal'],
            'scope':{'kind':'browser','url':'http://localhost/synthetic'},
            'outcomes':[{'id':'edit','subject':task['intents'][0]['selector'],'property':'value',
                         'value':task['intents'][0]['value'],'requires':[], 'evidence_plane':'exact_editor',
                         'source_text':task['goal']}], 'constraints':[], 'unknowns':[]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch('locua.engine.prototype.architecture.ground_plan', return_value=(task, {})):
            result = execute_plan(plan, driver, ScriptedSelector(), out=Path(tmp), cancel=threading.Event(),
                                  progress=lambda _:None, inspection_policy='reviewed_target_first')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['inspection_policy'],'reviewed_target_first')
        self.assertEqual(result['model_usage']['calls_started'],1)

    def test_invalid_policy_cannot_be_silently_ignored(self):
        driver, task = setup()
        with self.assertRaisesRegex(ValueError,'Unsupported inspection policy'):
            run_loop(task,driver,ScriptedSelector(),inspection_policy='auto')
        with self.assertRaisesRegex(ValueError,'observation tools'):
            run_loop(task,driver,ScriptedSelector(),inspection_policy='reviewed_target_first')


if __name__ == '__main__': unittest.main()
