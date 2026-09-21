"""Synthetic file-only evaluator checks. No model, driver or API calls."""
import importlib.util
import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('comparison_audit', ROOT / 'tools/comparison_audit.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
from locua.goal_verification import bind, verify


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.run = self.root / 'run'; self.now = time.time_ns()
        self.inventory_name = None
        self.request = "In TextEdit, replace the entire text in 'Example.txt' with 'Done.  '."
        self.task = {'id': 'textedit', 'request': self.request, 'initial_buffer': 'Before\n', 'oracle_buffer': 'Done.  '}
        self.protocol = {'created_at_ns': self.now - 100_000_000, 'tasks': [self.task], 'practical_local_latency_gate_s': 120}
        self.goal = {'id': 'goal', 'kind': 'text', 'target': 'Contents of Example.txt', 'value': 'Done.  ', 'evidence_plane': 'editor_buffer'}
        self.before = self.observation('s1', 'Before\n', self.now)
        self.after = self.observation('s2', 'Done.  ', self.now + 20_000_000)
        self.binding = bind(self.goal, self.before['controls'][1], self.before)
        # bind() uses the record's original clock; all test times stay near now.
        self.scope = {'status': 'approved', 'target': self.before['target'], 'goals': [self.goal],
            'bindings': {'goal': self.binding}, 'effects': [{'kind': 'goal', 'goal_id': 'goal'}], 'preserves': [], 'covers_entire_request': True}
        self.proof = self.verified_after()
        self.events = [
            {'sequence': 1, 'tool': 'locua_review', 'input': {'goals': [{**self.goal, 'control_id': 's1:1'}]}, 'result': {'status': 'approved'}},
            {'sequence': 2, 'tool': 'locua_act', 'input': {'scope_id': 'scope'}, 'result': {'status': 'verified', 'action_started': True}},
            {'sequence': 3, 'tool': 'locua_verify', 'input': {}, 'result': {'status': 'verified', 'fresh_refresh': True,
                'scopes': {'scope': {'goals': [self.proof]}}}}]
        self.evidence = {'request': self.request, 'request_sha256': a.sha(a.canonical(self.request).encode()),
            'events': self.events, 'scopes': {'scope': self.scope}, 'verification': {'scope': {'goals': [self.proof]}}}
        self.driver = [{'started_at_ns': self.now + 1_000_000, 'request': {'name': 'set_value',
            'arguments': {'pid': 1, 'window_id': 2, 'element_token': 's1:1', 'value': 'Done.  '}},
            'response': {'wall_ms': 1, 'result': {'structuredContent': {'effect': 'confirmed'}}}}]
        self.summary = {'request': self.request, 'status': 'verified_reviewed_scope', 'tool_interface': 'tools-v6.2',
            'metrics': {'provider_requests': 1, 'reported_usage': {'input_tokens': 40, 'output_tokens': 8}},
            'wall_excluding_human_s': 4, 'full_workflow_wall_s': 7, 'human_wait_s': 3, 'human_interactions': [{}]}
        self.record = {'call': 1, 'complete_generation_count': 1, 'generation': {'generation_calls': 1,
            'usage': {'input_tokens': 40, 'output_tokens': 8}, 'timing': {'generation_ms': 1000}}}

    def observation(self, sid, value, now):
        editor = {'plane': 'editor_buffer', 'coherence': {'value_stable': True},
                  'raw_value': {'status': 'ok', 'value': value}, 'raw_value_recheck': {'status': 'ok', 'value': value}}
        return {'kind': 'native_window_state', 'target': {'pid': 1, 'window_id': 2}, 'snapshot_id': sid,
            'observed_at_ns': now, 'provenance': {'observed_at_ns': now}, 'coverage': {'complete': False}, 'handles': {},
            'controls': [{'id': sid+':0', 'role': 'AXWindow', 'name': 'Example.txt', 'semantics': {}, 'parent': None},
              {'id': sid+':1', 'role': 'AXTextArea', 'name': 'Contents', 'parent': sid+':0', 'semantics': {},
               'value': value, 'states': {}, 'editor': editor,
               'source': {'node': {'element_token': sid+':1'}},
               'value_evidence': {'precision': 'exact', 'exact_value_proven': True, 'plane': 'editor_buffer'},
               'bounds': {'x': 1, 'y': 2, 'width': 200, 'height': 100}}]}

    def verified_after(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import locua.goal_verification as module
        with patch.object(module, 'time', SimpleNamespace(time_ns=lambda: self.after['observed_at_ns'])):
            result = verify(self.binding, self.goal, self.after)
        self.assertTrue(result['matched'], result)
        return {'goal_id': 'goal', **result}

    def write(self, path, value, lines=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(x) for x in value) if lines else json.dumps(value))

    def run_audit(self):
        self.write(self.root/'protocol.json', self.protocol)
        self.write(self.run/'summary.json', self.summary)
        evidence = deepcopy(self.evidence)
        name = self.inventory_name or ('Calculator' if self.task['id'] == 'calculator' else 'TextEdit')
        prefix = [{'tool': 'locua_apps', 'input': {}, 'result': {'items': [{'app_id': 'app:test', 'name': name, 'bundle_id': 'test.synthetic'}]}},
                  {'tool': 'locua_windows', 'input': {'app_id': 'app:test'}, 'result': {'windows': [{'target': {'pid': 1, 'window_id': 2}, 'identity_proven': True}]}}]
        evidence['events'] = prefix + evidence['events']
        for i, event in enumerate(evidence['events'], 1): event['sequence'] = i
        self.write(self.run/'desktop/evidence.json', evidence)
        self.write(self.run/'desktop/desktop/cua/transport.jsonl', self.driver, lines=True)
        self.write(self.run/'provider/call-001-summary.json', self.record)
        self.write(self.run/'desktop/observation-before.json', self.before)
        self.write(self.run/'desktop/observation-after.json', self.after)
        return a.audit(self.run, self.root/'protocol.json', self.task['id'])

    def test_exact_buffer_action_and_later_capture_pass_not_save_or_autonomy(self):
        result = self.run_audit()
        self.assertTrue(result['functional_pass'], result['outcomes'])
        self.assertTrue(result['execution']['no_explicit_save_in_recorded_task_inputs'])
        self.assertFalse(result['execution']['autosave_or_disk_unchanged_proven'])
        self.assertFalse(result['request_coverage']['autonomous_language_fidelity_proven'])

    def test_whitespace_mismatch_not_repaired(self):
        self.after['controls'][1]['value'] = 'Done.'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_receipt_claim_without_matching_raw_recheck_fails(self):
        self.after['controls'][1]['editor']['raw_value_recheck']['value'] = 'Different'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_old_timestamp_even_new_snapshot_is_not_independent_outcome(self):
        self.after['observed_at_ns'] = self.now
        self.after['provenance']['observed_at_ns'] = self.now
        self.proof['evidence']['observed_at_ns'] = self.now
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_wrong_target_or_document_not_pass(self):
        self.after['target']['window_id'] = 88
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_setup_mismatch_fails_even_successful_edit(self):
        self.task['initial_buffer'] = 'Required initial buffer'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_launch_and_final_success_text_are_not_execution(self):
        self.driver[0]['request'] = {'name': 'launch_app', 'arguments': {'bundle_id': 'fake'}}
        self.events[1]['result']['action_started'] = False
        self.summary['assistant_response'] = 'Successfully completed all work.'
        result = self.run_audit()
        self.assertFalse(result['functional_pass'])
        self.assertEqual(result['execution']['task_input_requests'], 0)

    def test_extra_save_route_fails_textedit_no_save(self):
        self.driver.append({**deepcopy(self.driver[0]), 'request': {'name': 'press_key', 'arguments': {'key': 'Cmd+s'}}})
        result = self.run_audit()
        self.assertFalse(result['functional_pass'])
        self.assertFalse(result['execution']['no_explicit_save_in_recorded_task_inputs'])

    def test_unknown_or_uncertain_route_never_silently_read_only(self):
        self.driver[0]['request']['name'] = 'future_mutation_tool'
        result = self.run_audit(); self.assertFalse(result['functional_pass'])
        self.assertIn('unclassified_driver_routes', result['integrity_issues'])

    def test_unresolved_preserves_require_independent_audit(self):
        self.scope['preserves'] = [{'value': 'other'}]
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_interrupted_request_unknown_usage_not_zero(self):
        self.summary['metrics']['provider_requests'] = 2
        self.summary['metrics']['unknown_generation_usage'] = True
        result = self.run_audit()['metrics']
        self.assertIsNone(result['model_calls']); self.assertEqual(result['known_input_tokens'], 40)
        self.assertEqual(result['unrecorded_requested_calls'], 1)

    def test_historical_captures_never_count_as_fresh_comparison(self):
        self.protocol['created_at_ns'] = self.now + 50_000_000
        self.assertFalse(self.run_audit()['comparison_eligibility']['eligible'])

    def test_first_refusal_report_not_invented_model_reasoning(self):
        self.events[1]['result'] = {'status': 'refused', 'reason': 'target_mismatch', 'action_started': False}
        self.driver = []
        result = self.run_audit()
        self.assertEqual(result['first_consequential_failure']['sequence'], 4)

    def test_repeated_inspection_nonprogress_recorded_when_no_refusal(self):
        self.events[1] = {'sequence': 2, 'tool': 'locua_observe', 'result': {'status': 'observed'},
             'model_result': {'exploration_feedback': {'new_information': False, 'code': 'repeated_inspection', 'equivalent_inspection_count': 2}}}
        self.driver = []
        self.assertEqual(self.run_audit()['first_consequential_failure']['sequence'], 4)

    def calculation_fixture(self, *, expression='3*5', result='15', keys=('clear', '3', '*', '5', '=')):
        self.request = f'Use Calculator to work out {expression}.'
        self.task = {'id': 'calculator', 'request': self.request, 'oracle_display': int(result)}
        self.protocol['tasks'] = [self.task]
        self.goal = {'id': 'goal', 'kind': 'calculation', 'target': 'Result', 'expression': expression, 'evidence_plane': 'display'}
        for obs, value in ((self.before, '0'), (self.after, result)):
            obs['controls'][1].update(role='AXStaticText', name='Result', value=value)
        self.binding = bind(self.goal, self.before['controls'][1], self.before)
        self.proof = self.verified_after()
        self.scope.update(goals=[self.goal], bindings={'goal': self.binding},
            witness={'known_start': True, 'issued_evaluation': expression})
        self.evidence.update(request=self.request, request_sha256=a.sha(a.canonical(self.request).encode()))
        self.evidence['verification'] = {'scope': {'goals': [self.proof]}}
        self.summary['request'] = self.request
        self.events[:] = [{'sequence': 1, 'tool': 'locua_review', 'input': {}, 'result': {'status': 'approved'}}]
        self.driver = []
        for i, key in enumerate(keys, 2):
            control = {'id': f's1:{i}', 'parent': 's1:0', 'role': 'AXButton', 'name': 'All Clear' if key == 'clear' else key,
                'states': {}, 'semantics': {}, 'source': {'node': {'element_token': f's1:{i}'}}}
            self.before['controls'].append(control)
            self.driver.append({'started_at_ns': self.now + i*1_000_000,
                'request': {'name': 'click', 'arguments': {'pid': 1, 'window_id': 2, 'element_token': f's1:{i}'}},
                'response': {'wall_ms': .1, 'result': {'structuredContent': {'effect': 'confirmed'}}}})
            self.events.append({'sequence': i, 'tool': 'locua_act', 'input': {'scope_id': 'scope'}, 'result': {'status': 'verified', 'action_started': True}})

    def locale_fixture(self):
        from test_number_format import probe
        from locua.number_format import evidence_from_probe
        from unittest.mock import patch
        from types import SimpleNamespace
        import locua.goal_verification as verifier
        import locua.number_format as number_format
        self.calculation_fixture(expression='3*5000', result='15000', keys=('clear', '3', '*', '5', '0', '0', '0', '='))
        self.after['controls'][1]['value'] = '\u200e15,000'
        raw = probe(); raw.update(bundle_id='test.synthetic', capture_started_at_ns=self.now,
            capture_finished_at_ns=self.now+1_000_000)
        self.locale = evidence_from_probe(raw, bundle_id='test.synthetic', target=self.after['target'],
            received_at_ns=self.now+2_000_000)
        clock = SimpleNamespace(time_ns=lambda: self.after['observed_at_ns'])
        with patch.object(verifier, 'time', clock), patch.object(number_format, 'time', clock):
            self.proof = {'goal_id': 'goal', **verify(self.binding, self.goal, self.after, number_format=self.locale)}
        self.assertTrue(self.proof['matched'], self.proof)
        self.evidence['verification']['scope']['goals'] = [self.proof]
        self.summary['tool_interface'] = self.protocol['tool_interface'] = 'tools-v6.3'
        self.save_locale()

    def save_locale(self):
        self.write(self.run/'desktop/desktop/desktop.jsonl', [{'operation': 'number_format',
            'target': self.after['target'], 'evidence': self.locale}], lines=True)

    def test_locale_backed_grouped_display_is_explicitly_conditional(self):
        self.locale_fixture(); result = self.run_audit()
        self.assertTrue(result['functional_pass'], result['outcomes'])
        self.assertTrue(result['conditional_on_application_following_observed_locale'])
        row = result['outcomes'][0]['fresh_outcomes'][0]
        self.assertFalse(row['application_formatter_proven'])
        self.assertEqual(row['numeric_interpretation']['numerator'], 15000)
        self.assertEqual(row['numeric_interpretation']['denominator'], 1)
        self.assertTrue(row['locale_evidence_path'].endswith('#record=1'))

    def test_locale_receipt_requires_full_original_probe_evidence(self):
        self.locale_fixture(); (self.run/'desktop/desktop/desktop.jsonl').unlink()
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_locale_probe_tampering_does_not_fallback_to_other_numeric_grammar(self):
        self.locale_fixture(); self.locale['format']['decimal_separator'] = ','; self.save_locale()
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_locale_proof_cannot_change_parsed_fraction(self):
        self.locale_fixture(); self.proof['evidence']['numeric_interpretation']['numerator'] = 15
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_foreign_or_stale_locale_evidence_has_no_outcome_credit(self):
        from test_number_format import probe
        from locua.number_format import evidence_from_probe
        self.locale_fixture()
        for bundle, stamp in [('other.application', self.now), ('test.synthetic', self.now-70_000_000_000)]:
            with self.subTest(bundle=bundle, stamp=stamp):
                raw = probe(); raw.update(bundle_id=bundle, capture_started_at_ns=stamp,
                    capture_finished_at_ns=stamp+1_000_000)
                self.locale = evidence_from_probe(raw, bundle_id=bundle, target=self.after['target'], received_at_ns=stamp+2_000_000)
                self.proof['evidence']['numeric_interpretation']['locale_evidence_sha256'] = self.locale['evidence_sha256']
                self.save_locale(); self.assertFalse(self.run_audit()['functional_pass'])

    def rebind_fixture(self):
        from locua.goal_verification import bind_for_review
        import locua.goal_verification as verifier
        from types import SimpleNamespace
        from unittest.mock import patch
        self.calculation_fixture(); old = deepcopy(self.binding)
        self.after['controls'][1]['name'] = 'Replacement readout'
        self.selected = deepcopy(self.after); self.checked = deepcopy(self.after)
        for obs, sid, offset in ((self.selected, 'selected', 10_000_000), (self.checked, 'checked', 12_000_000)):
            obs['snapshot_id'] = sid; obs['observed_at_ns'] = self.now + offset
            obs['provenance']['observed_at_ns'] = obs['observed_at_ns']
            for c in obs['controls']:
                c['id'] = c['id'].replace('s2', sid)
                if c.get('parent'): c['parent'] = c['parent'].replace('s2', sid)
        with patch.object(verifier, 'time', SimpleNamespace(time_ns=lambda: self.selected['observed_at_ns'])):
            self.binding = bind_for_review(self.goal, self.selected['controls'][1], self.selected)
        self.scope['bindings']['goal'] = self.binding
        self.revision = {'kind': 'read_only_calculation_result', 'goal_id': 'goal',
            'selected_snapshot_id': 'selected', 'selected_control_id': 'selected:1',
            'checked_snapshot_id': 'checked', 'previous_binding': old, 'replacement_binding': self.binding,
            'input_authority_changed': False, 'reviewed_goal_sha256': a.sha(a.canonical(self.goal).encode()),
            'witness': deepcopy(self.scope['witness']), 'selection_before_value_comparison': True}
        self.scope['binding_revisions'] = [self.revision]
        self.proof = self.verified_after(); self.evidence['verification']['scope']['goals'] = [self.proof]
        self.write(self.run/'desktop/observation-selected.json', self.selected)
        self.write(self.run/'desktop/observation-checked.json', self.checked)

    def test_read_only_rebind_retains_original_review_without_claiming_human_selected_replacement(self):
        self.rebind_fixture(); result = self.run_audit()
        self.assertTrue(result['functional_pass'], result['outcomes'])
        audit = result['outcomes'][0]['binding_revision_proof']
        self.assertTrue(audit['valid']); self.assertEqual(audit['count'], 1)
        self.assertTrue(audit['original_goal_human_review_preserved'])
        self.assertFalse(audit['replacement_target_selected_by_human'])
        self.assertEqual(result['outcomes'][0]['fresh_outcomes'][0]['binding_origin'], 'model_selected_read_only_recovery')

    def test_rebind_cannot_change_goal_window_input_witness_or_authority(self):
        self.rebind_fixture(); original = deepcopy(self.revision)
        for field, value in [('reviewed_goal_sha256', 'wrong'), ('input_authority_changed', True),
                             ('witness', {'known_start': False, 'issued_evaluation': '3*5'}),
                             ('selection_before_value_comparison', False)]:
            with self.subTest(field=field):
                self.scope['binding_revisions'] = [{**deepcopy(original), field: value}]
                self.assertFalse(self.run_audit()['functional_pass'])
        self.scope['binding_revisions'] = [deepcopy(original)]
        self.scope['binding_revisions'][0]['replacement_binding']['target']['window_id'] = 99
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_rebind_refuses_old_binding_still_present_or_ambiguous(self):
        self.rebind_fixture(); base = deepcopy(self.checked)
        for count in (1, 2):
            checked = deepcopy(base)
            for i in range(count):
                c = deepcopy(checked['controls'][1]); c.update(id=f'checked:old{i}', name='Result')
                checked['controls'].append(c)
            self.write(self.run/'desktop/observation-checked.json', checked)
            self.assertFalse(self.run_audit()['functional_pass'])

    def test_rebind_requires_separate_final_capture_after_selection_and_check(self):
        self.rebind_fixture()
        self.proof['evidence']['snapshot_id'] = self.checked['snapshot_id']
        self.proof['evidence']['observed_at_ns'] = self.checked['observed_at_ns']
        self.proof['evidence']['control_id'] = self.checked['controls'][1]['id']
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_v63_does_not_silently_satisfy_v62_protocol(self):
        self.summary['tool_interface'] = 'tools-v6.3'
        self.protocol['harness_semantics'] = 'tools-v6.2, fixed policies'
        result = self.run_audit()
        self.assertTrue(result['comparison_eligibility']['supported_tool_interface'])
        self.assertFalse(result['comparison_eligibility']['required_tool_interface_matches'])

    def test_v64_requires_explicit_matching_protocol_interface(self):
        self.summary['tool_interface'] = 'tools-v6.4'
        self.protocol['tool_interface'] = 'tools-v6.3'
        result = self.run_audit()
        self.assertTrue(result['comparison_eligibility']['supported_tool_interface'])
        self.assertFalse(result['comparison_eligibility']['required_tool_interface_matches'])
        self.protocol['tool_interface'] = 'tools-v6.4'
        self.assertTrue(self.run_audit()['comparison_eligibility']['required_tool_interface_matches'])

    def test_v65_requires_explicit_matching_protocol_interface(self):
        self.summary['tool_interface'] = 'tools-v6.5'
        self.protocol['tool_interface'] = 'tools-v6.4'
        result = self.run_audit()
        self.assertTrue(result['comparison_eligibility']['supported_tool_interface'])
        self.assertFalse(result['comparison_eligibility']['required_tool_interface_matches'])
        self.protocol['tool_interface'] = 'tools-v6.5'
        self.assertTrue(self.run_audit()['comparison_eligibility']['required_tool_interface_matches'])

    def test_unverifiable_axpress_ack_requires_independent_outcome_not_effect_guess(self):
        self.calculation_fixture()
        for row, event in zip(self.driver, self.events[1:]):
            ack = {'effect': 'unverifiable', 'route': 'accessibility', 'delivery': {'mode': 'background'}}
            row['response']['result']['structuredContent'] = deepcopy(ack)
            event['result'].update(status='dispatched', driver_ack=deepcopy(ack))
        result = self.run_audit()
        self.assertTrue(result['functional_pass'], result['execution'])
        self.assertEqual(result['execution']['uncertain_task_input_indices'], [])
        self.assertEqual(result['execution']['acknowledgements_without_effect_proof'], [1, 2, 3, 4, 5])
        self.after['controls'][1]['value'] = '14'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_uncertain_or_refused_dispatch_not_promoted_by_matching_later_display(self):
        self.calculation_fixture()
        for status, effect in [('uncertain', 'confirmed'), ('dispatched', 'refused'), ('dispatched', 'unknown')]:
            with self.subTest(status=status, effect=effect):
                self.events[1]['result']['status'] = status
                self.driver[0]['response']['result']['structuredContent']['effect'] = effect
                result = self.run_audit()
                self.assertFalse(result['functional_pass'])
                self.assertIn(1, result['execution']['uncertain_task_input_indices'])

    def test_calculation_requires_exact_issued_expression_and_fresh_numeric_display(self):
        self.calculation_fixture()
        result = self.run_audit()
        self.assertTrue(result['functional_pass'], result)
        self.assertEqual(result['execution']['arithmetic_issuance_recomputed']['scope']['issued_evaluation'], '3*5')

    def test_generic_clear_cannot_make_recorded_full_reset_witness_sound(self):
        self.calculation_fixture()
        self.before['controls'][2]['name'] = 'Clear'
        result = self.run_audit()
        self.assertFalse(result['functional_pass'])
        self.assertFalse(result['execution']['arithmetic_issuance_recomputed']['scope']['known_start'])
        self.assertFalse(result['execution']['arithmetic_witness_consistency']['scope']['matches'])
        self.assertTrue(result['execution']['arithmetic_witness_consistency']['scope']['recorded']['known_start'])

    def test_fabricated_witness_without_correct_actual_key_inputs_fails(self):
        self.calculation_fixture()
        self.before['controls'][3]['name'] = '4'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_calculation_wrong_window_is_not_same_task(self):
        self.calculation_fixture(); self.driver[0]['request']['arguments']['window_id'] = 99
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_numeric_oracle_on_unrelated_readout_does_not_pass(self):
        self.calculation_fixture(); self.after['controls'][1]['name'] = 'History'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_privacy_wrapper_failure_precedes_latched_followups_and_final_answer(self):
        self.driver = []; self.events = self.evidence['events'] = []
        posts = []
        for tool, code in [('locua_observe', 'task_disclosure_projection_failed'), ('locua_activate', 'privacy_scope_blocked')]:
            posts.append({'event': 'tool:post', 'data': {'tool_name': tool, 'tool_call_id': tool,
                'result': {'output': {'status': 'refused', 'code': code, 'no_retry': True}}}})
        self.write(self.run/'session/session.json', {'events': posts})
        result = self.run_audit()
        self.assertEqual(result['first_consequential_failure']['classification'], 'harness_observation_disclosure_projection_failure')
        self.assertFalse(result['first_consequential_failure']['model_decision_failure_primary'])
        self.assertEqual(result['tool_calls'], 2)
        self.assertEqual(len(result['privacy']['blocked_attempts_after_latch']), 1)

    def test_reviewed_navigation_does_not_corrupt_arithmetic_witness(self):
        from locua.amplifier_tools import _identity
        self.calculation_fixture()
        button = {'id': 's1:9', 'parent': 's1:0', 'role': 'AXMenuItem', 'name': 'Mode',
            'states': {}, 'semantics': {}, 'bounds': {'x': 2, 'y': 3, 'width': 5, 'height': 5},
            'source': {'node': {'element_token': 's1:9'}}}
        self.before['controls'].append(button)
        self.scope['effects'].append({'kind': 'press', 'purpose': 'Navigate observed interface',
            'identity': _identity(button, self.before), 'bounds': button['bounds']})
        self.driver.insert(0, {'started_at_ns': self.now + 1_000_000,
            'request': {'name': 'click', 'arguments': {'pid': 1, 'window_id': 2, 'element_token': 's1:9'}},
            'response': {'wall_ms': .1, 'result': {'structuredContent': {'effect': 'confirmed'}}}})
        self.events.insert(1, {'tool': 'locua_act', 'input': {'scope_id': 'scope'}, 'result': {'status': 'dispatched', 'action_started': True}})
        for i, row in enumerate(self.events, 1): row['sequence'] = i
        result = self.run_audit()
        self.assertTrue(result['functional_pass'], result)
        self.assertEqual(result['input_effect_reconciliation'][0]['classification'], 'reviewed_navigation')

    def test_multiple_valid_bound_text_writes_are_not_a_sequence_failure(self):
        self.driver.append(deepcopy(self.driver[0]))
        self.events.insert(2, deepcopy(self.events[1]))
        for i, row in enumerate(self.events, 1): row['sequence'] = i
        self.assertTrue(self.run_audit()['functional_pass'])

    def test_unsupported_typing_route_is_manual_audit_not_model_error(self):
        self.driver[0]['request']['name'] = 'type_text'
        result = self.run_audit()
        self.assertEqual(result['status'], 'manual_audit_required')
        self.assertEqual(result['first_consequential_failure']['classification'], 'auditor_route_coverage_incomplete')

    def test_known_multiple_dispatch_response_keeps_usage_known(self):
        self.record['generation']['generation_calls'] = 2
        self.record['complete_generation_count'] = 2
        result = self.run_audit()['metrics']
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['completed_calls_with_known_usage'], 1)
        self.assertEqual(result['known_input_tokens'], 40)

    def test_cache_input_counts_use_provider_normalization_without_alias_double_count(self):
        self.assertEqual(a.gross_input_tokens({'input_tokens': 153, 'cache_write_tokens': 607, 'cache_creation_input_tokens': 607}, 'anthropic'), 760)
        self.assertEqual(a.gross_input_tokens({'input_tokens': 2469, 'cache_read_tokens': 2466, 'cache_write_tokens': 15}, 'openai'), 2484)
        self.assertEqual(a.gross_input_tokens({'input_tokens': 40}, 'local'), 40)

    def test_correct_display_with_unproved_reset_is_retained_but_not_strict_pass(self):
        self.calculation_fixture()
        self.scope['witness'] = {'known_start': False, 'issued_evaluation': None}
        self.evidence['verification']['scope']['goals'] = [{'goal_id': 'goal', 'matched': False,
            'evidence': None, 'reason': 'Requested arithmetic input/evaluation issuance is unproved', 'display_readback': self.proof}]
        result = self.run_audit()
        self.assertFalse(result['functional_pass'])
        self.assertTrue(result['outcomes'][0]['independently_verified_display_or_buffer'])
        self.assertEqual(result['first_consequential_failure']['classification'], 'arithmetic_issuance_unproved_despite_correct_bound_display')

    def test_right_number_in_wrong_application_cannot_complete(self):
        self.calculation_fixture(); self.inventory_name = 'Other application'
        result = self.run_audit()
        self.assertFalse(result['functional_pass'])
        self.assertFalse(result['outcomes'][0]['requested_application']['verified'])

    def test_right_document_name_in_wrong_application_cannot_complete(self):
        self.inventory_name = 'Other editor'
        self.assertFalse(self.run_audit()['functional_pass'])

    def test_duplicate_and_nonfinite_json_are_refused(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError): a.strict_json(value)

    def test_numeric_display_never_evaluates_expression(self):
        self.assertEqual(a.number('\u200e44252\u200f'), 44252)
        self.assertEqual(a.number('−13'), -13)
        for value in ('192*231-100', '44,252', 'Result 44252', True):
            self.assertIsNone(a.number(value))


if __name__ == '__main__':
    unittest.main()
