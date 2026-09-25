"""Exploration continuity and contract integration; no GUI/model processes."""
from copy import deepcopy
import unittest
import test_amplifier_tools as fixtures
from locua.exploration import Exploration
from locua.progressive_ui import listing


class CapabilityProgressTests(unittest.TestCase):
    def setUp(self):
        self.observation = fixtures.Desktop().observe({'pid': 41, 'window_id': 52})['observation']
        # Same path/shape as recorded native_text_capabilities evidence. No real
        # handle is used or created, and this ledger cannot authorize input.
        self.observation['controls'][1]['capabilities'] = {
            'set_value': {'eligible': True, 'addressed': True, 'schema_supported': True,
                'addressing': {'element_token': True, 'snapshot_index': True},
                'exact_readback_supported': True, 'reason': 'observed writable value',
                'requires': ['fresh_exact_handle', 'independent_exact_string_readback'],
                'evidence': {'writability': {'value': True, 'basis': 'same_handle_AXIsAttributeSettable',
                    'contract': {'source_fingerprint_sha256': 'source-version'}},
                    'address': {'kind': 'native', 'pid': 41, 'window_id': 52,
                        'snapshot_id': 's1', 'element_token': 's1:1', 'element_index': 1}}},
            'type': {'eligible': None, 'observed_focus': False}}

    def recapture(self):
        result = deepcopy(self.observation)
        result['snapshot_id'] = 'recaptured'
        result['observed_at_ns'] += 1
        result['provenance']['observed_at_ns'] = result['observed_at_ns']
        for control in result['controls']:
            control['id'] = control['id'].replace('s1:', 'recaptured:')
            if control.get('parent'):
                control['parent'] = control['parent'].replace('s1:', 'recaptured:')
        address = result['controls'][1]['capabilities']['set_value']['evidence']['address']
        address.update(snapshot_id='recaptured', element_token='new-opaque-token', element_index=91)
        return result

    @staticmethod
    def state(observation):
        ledger = Exploration()
        ledger.retain(observation)
        return ledger.snapshots[observation['snapshot_id']]['state']

    def test_address_churn_is_repeated_inspection_with_evidence_and_reference_retained(self):
        before, after = self.observation, self.recapture()
        originals = deepcopy((before, after))
        ledger = Exploration()
        for observation, expected_new in ((before, True), (after, False)):
            ledger.retain(observation)
            page = listing(observation, role='AXTextField')
            page['window_id'] = 'window:public-reference'
            feedback = ledger.record('locua_inspect', {'snapshot_id': observation['snapshot_id'],
                'operation': 'list', 'role': 'AXTextField'}, page)
            self.assertEqual(feedback['new_information'], expected_new)
            self.assertFalse(feedback['action_authority'])
        self.assertEqual(feedback['code'], 'repeated_inspection')
        self.assertEqual((before, after), originals)
        self.assertTrue(all(r['window_id'] == 'window:public-reference' for r in ledger.pages))
        self.assertTrue(all(r['window_id'] == 'window:public-reference' for r in ledger.controls.values()))
        self.assertEqual({r['snapshot_id'] for r in ledger.controls.values()}, {'recaptured'})
        self.assertEqual(len(ledger.controls), 2)

    def test_real_capability_and_proof_changes_are_new_progress(self):
        expected = self.state(self.observation)
        changes = [(('set_value', 'eligible'), False), (('set_value', 'eligible'), None),
            (('set_value', 'schema_supported'), False), (('set_value', 'addressed'), False),
            (('set_value', 'reason'), 'read-only'), (('set_value', 'exact_readback_supported'), False),
            (('set_value', 'requires'), ['additional editor evidence']),
            (('set_value', 'addressing', 'element_token'), False),
            (('set_value', 'evidence', 'writability', 'value'), False),
            (('set_value', 'evidence', 'writability', 'basis'), 'unknown'),
            (('set_value', 'evidence', 'writability', 'contract', 'source_fingerprint_sha256'), 'other-version'),
            (('type', 'eligible'), False), (('type', 'observed_focus'), True)]
        for path, value in changes:
            after = self.recapture()
            target = after['controls'][1]['capabilities']
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value):
                self.assertNotEqual(self.state(after), expected)
        after = self.recapture()
        del after['controls'][1]['capabilities']['type']
        self.assertNotEqual(self.state(after), expected)

    def test_address_loss_invalidity_and_scope_changes_remain_distinct(self):
        expected = self.state(self.observation)
        for key, value in [('snapshot_id', ''), ('element_token', None), ('element_token', ''),
                           ('element_token', {'opaque_address_type': 'nonempty_string'}),
                           ('element_index', -1), ('element_index', True), ('element_index', '91'),
                           ('pid', 42), ('window_id', 53), ('kind', 'browser')]:
            after = self.recapture()
            after['controls'][1]['capabilities']['set_value']['evidence']['address'][key] = value
            with self.subTest(key=key, value=value):
                self.assertNotEqual(self.state(after), expected)
        for value in (None, {}):
            after = self.recapture()
            after['controls'][1]['capabilities']['set_value']['evidence']['address'] = value
            self.assertNotEqual(self.state(after), expected)

    def test_optional_address_refs_are_normalized_only_at_the_address_path(self):
        before = deepcopy(self.observation)
        before['controls'][1]['capabilities']['set_value']['evidence']['address'].update(
            control_id='old-control', ref='old-ref')
        after = deepcopy(before)
        route = after['controls'][1]['capabilities']['set_value']
        route['evidence']['address'].update(control_id='new-control', ref='new-ref')
        self.assertEqual(self.state(before), self.state(after))
        # A same-named field outside the known address path is not erased.
        route['evidence']['snapshot_id'] = 'new evidence field'
        self.assertNotEqual(self.state(before), self.state(after))


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tools = self.fixture.tools
        self.call = self.fixture.call
        self.sid = self.fixture.sid

    def test_equivalent_capture_is_not_new_information(self):
        first = self.call('inspect', snapshot_id=self.sid, operation='list', role='AXTextField')
        self.assertTrue(first['exploration_feedback']['new_information'])
        sid = self.call('observe', window_id=self.fixture.window)['snapshot_id']
        repeated = self.call('inspect', snapshot_id=sid, operation='list', role='AXTextField')
        self.assertEqual(repeated['exploration_feedback']['code'], 'repeated_inspection')
        self.assertFalse(repeated['exploration_feedback']['new_information'])
        self.fixture.desktop.value = 'changed externally'
        sid = self.call('observe', window_id=self.fixture.window)['snapshot_id']
        changed = self.call('inspect', snapshot_id=sid, operation='list', role='AXTextField')
        self.assertTrue(changed['exploration_feedback']['new_information'])

    def test_pages_and_details_are_progress_without_whole_enumeration(self):
        first = self.call('inspect', snapshot_id=self.sid, operation='list', limit=1)
        self.assertTrue(first['exploration_feedback']['new_information'])
        second = self.call('inspect', snapshot_id=self.sid, operation='list', limit=1,
                           cursor=first['coverage']['continuation'])
        self.assertTrue(second['exploration_feedback']['new_information'])
        expanded = self.call('inspect', snapshot_id=self.sid, operation='list', limit=3)
        self.assertTrue(expanded['exploration_feedback']['new_information'])
        again = self.call('inspect', snapshot_id=self.sid, operation='list', limit=2)
        self.assertFalse(again['exploration_feedback']['new_information'])
        detail = self.call('inspect', snapshot_id=self.sid, operation='control', control_id=self.sid+':1')
        self.assertTrue(detail['exploration_feedback']['new_information'])

    def test_changed_evidence_or_unbound_text_is_new_information(self):
        self.call('inspect', snapshot_id=self.sid, operation='control', control_id=self.sid+':1')
        for index, change in enumerate(('proof', 'editor', 'text')):
            o = deepcopy(self.tools._observations[self.sid])
            o['snapshot_id'] = 'changed-'+str(index)
            if change == 'proof': o['controls'][1]['value_evidence']['precision'] = 'unknown'
            if change == 'editor': o['controls'][1]['editor'] = {'value_settable': {'status':'ok','value':True}}
            if change == 'text': o['text'] = 'New unbound dialog message'
            self.tools._retain(o)
            result = self.call('inspect', snapshot_id=o['snapshot_id'], operation='control', control_id=self.sid+':1')
            self.assertTrue(result['exploration_feedback']['new_information'], change)

    def test_retained_status_recovers_needs_discovery_without_read_authority(self):
        self.call('inspect', snapshot_id=self.sid, operation='list', role='AXTextField')
        self.call('status', operation='needs', needs=['Determine the requested editor from observed controls.'])
        calls = deepcopy(self.fixture.desktop.calls)
        status = self.call('status')
        self.assertEqual(status['original_request'], self.tools.request)
        self.assertTrue(status['exploration']['potentially_stale'])
        self.assertFalse(status['exploration']['action_authority'])
        self.assertEqual(len(status['exploration']['unresolved_needs']), 1)
        controls = self.call('status', operation='controls', limit=1)
        self.assertGreaterEqual(controls['total'], 2)  # overview readouts remain too
        self.assertTrue(controls['items'][0]['potentially_stale'])
        self.assertFalse(controls['items'][0]['action_authority'])
        pages = self.call('status', operation='exploration')
        self.assertEqual(pages['total'], 2)  # automatic overview and one list
        self.assertEqual(self.fixture.desktop.calls, calls)
        self.assertFalse(self.fixture.desktop.executions)

    def test_caveat_does_not_impose_save_and_unmet_requirement_blocks_coverage(self):
        args = dict(snapshot_id=self.sid, summary='Replace the requested editor buffer',
            goals=[dict(id='g', kind='text', target='Entry', control_id=self.sid+':1',
                        value='exact', evidence_plane='editor_buffer')],
            effects=[dict(kind='goal', goal_id='g')], covers_entire_request=True,
            limitations=['Saved output is not proved; saving was not requested.'])
        blocked = self.call('review', **args, unresolved_requirements=['User requested saving.'])
        self.assertEqual(blocked['code'], 'argument_contract_invalid')
        self.assertFalse(self.fixture.reviews)
        approved = self.call('review', **args)
        self.assertEqual(approved['status'], 'approved')
        self.assertIn('Informational caveat:', self.fixture.reviews[-1])
        self.assertFalse(self.fixture.desktop.executions)

    def test_incompatible_inspection_fields_have_actionable_feedback(self):
        for args in [dict(operation='overview', region_id='r'),
                     dict(operation='control', control_id=self.sid+':1', limit=2),
                     dict(operation='list', query=''), dict(operation='all')]:
            result = self.call('inspect', snapshot_id=self.sid, **args)
            self.assertEqual(result['code'], 'argument_contract_invalid')
            self.assertFalse(result['arguments_rewritten'])
            self.assertIn('argument contract', result['reason'])

    def test_unavailable_observation_repetition_survives_status_and_resets_after_success(self):
        self.fixture.desktop.unavailable = True
        first = self.call('observe', window_id=self.fixture.window)
        self.assertNotIn('exploration_feedback', first)
        self.call('status')
        second = self.call('observe', window_id=self.fixture.window)
        feedback = second['exploration_feedback']
        self.assertEqual(feedback['code'], 'repeated_failed_read')
        self.assertEqual(feedback['equivalent_failure_count'], 2)
        self.assertFalse(feedback['current_state_proven'])
        self.assertFalse(feedback['action_authority'])
        from locua.amplifier_session import _tool_progress
        printed = _tool_progress({'tool_name':'locua_observe', 'result':{'success':False,'output':second}})
        self.assertTrue(any('Repeated read failure' in line for line in printed))
        self.fixture.desktop.unavailable = False
        self.call('observe', window_id=self.fixture.window)
        self.fixture.desktop.unavailable = True
        last = self.call('observe', window_id=self.fixture.window)
        self.assertNotIn('exploration_feedback', last)
        self.assertFalse(self.fixture.desktop.executions)

    def test_region_as_cursor_has_read_only_recovery_without_window_activation(self):
        overview = self.call('inspect', snapshot_id=self.sid, operation='overview')
        region = next(row['region_id'] for row in overview['items'] if row.get('kind') == 'region')
        before = deepcopy(self.fixture.desktop.calls)
        args = dict(snapshot_id=self.sid, operation='overview', cursor=region, limit=10)
        for _ in range(2):
            refused = self.call('inspect', **args)
            self.assertEqual(refused['status'], 'refused')
            self.assertIn("operation='list'", refused['reason'])
        feedback = refused['exploration_feedback']
        self.assertEqual(feedback['failure_stage'], 'retained_inspection')
        self.assertFalse(feedback['action_authority'])
        from locua.amplifier_session import _tool_progress
        printed = _tool_progress({'tool_name':'locua_inspect', 'result':{'success':False,'output':refused}})
        self.assertTrue(any('correct the operation' in line for line in printed))
        incompatible = self.call('inspect', **args, region_id=region)
        self.assertFalse(incompatible['arguments_rewritten'])
        self.assertIn("region_id requires operation='list'", incompatible['reason'])
        recovered = self.call('inspect', snapshot_id=self.sid, operation='list', region_id=region, limit=10)
        self.assertEqual(recovered['status'], 'ok')
        self.assertEqual(self.fixture.desktop.calls, before)
        self.assertFalse(self.fixture.desktop.executions)


if __name__ == '__main__':
    unittest.main()
