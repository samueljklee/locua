"""Presentation-only evidence receipts: no driver, model, or runtime requests."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import ast
import io
import inspect
import json
import unittest
from unittest.mock import patch

from locua.cli import incomplete_goal_lines, main
from locua.amplifier_session import _tool_progress
from locua import amplifier_session


def report(*, timeout=False):
    value = {
        'status': 'blocked', 'reason': 'TimeoutError: ' if timeout else 'No verified coverage of the complete request',
        'request': 'Evaluate (18 - 5) / 2.', 'artifacts': '/synthetic/task',
        'tool_evidence': {'scopes': {'scope:a': {
            'goals': [{'id': 'result', 'kind': 'calculation', 'expression': '(18 - 5) / 2',
                       'target': 'A model-guessed answer SECRET_GUESS', 'evidence_plane': 'display'}],
            'witness': {'issued_expression_since_clear': '18-5/2', 'issued_evaluation': None, 'known_start': True},
        }}, 'events': [{'reasoning': 'PRIVATE_REASONING', 'tree': 'UNRELATED_UI'}]},
        'assistant_response': 'PRIVATE_REASONING',
    }
    if not timeout:
        value['verification'] = {'status': 'blocked', 'verification': {
            'fresh_refresh': True, 'scopes': {'scope:a': {'goals': [{
                'goal_id': 'result', 'matched': False,
                'reason': 'Requested arithmetic input/evaluation issuance is unproved',
                'evidence': None, 'display_readback': {'matched': False, 'evidence': {
                    'actual': '\u200e15.5', 'plane': 'display', 'saved_output_proven': False}},
            }], 'preserves': []}}}}
    return value


class OutcomePresentationTests(unittest.TestCase):
    def text(self, value):
        original = deepcopy(value)
        result = '\n'.join(incomplete_goal_lines(value))
        self.assertEqual(value, original)
        return result

    def test_bound_readback_and_issuance_are_separate_from_requested_expression(self):
        text = self.text(report())
        for part in ('Evaluate "(18 - 5) / 2"', 'Last recorded readback: "\\u200e15.5" (display)',
                     'Last recorded issued input: "18-5/2"', 'Evaluation from a known start: not recorded',
                     'Unmet: Requested arithmetic input/evaluation issuance is unproved'):
            self.assertIn(part, text)
        for private in ('SECRET_GUESS', 'PRIVATE_REASONING', 'UNRELATED_UI', '6.5'):
            self.assertNotIn(private, text)

    def test_timeout_uses_recorded_input_without_fabricated_current_result_or_duration(self):
        value = report(timeout=True)
        value['tool_evidence']['scopes']['scope:a']['witness']['issued_expression_since_clear'] = '18'
        text = self.text(value)
        for part in ('Session time limit reached', 'No fresh final verification', 'Last recorded issued input: "18"',
                     'Bound result readback: unavailable', 'No matching verification receipt'):
            self.assertIn(part, text)
        for unsupported in ('600', 'current value', 'Last recorded readback:', '15.5'):
            self.assertNotIn(unsupported, text)

    def test_retained_receipt_is_not_presented_as_fresh_or_task_completion(self):
        value = report()
        value['tool_evidence']['verification'] = value.pop('verification')['verification']
        row = value['tool_evidence']['verification']['scopes']['scope:a']['goals'][0]
        row['matched'] = True
        text = self.text(value)
        self.assertIn('No fresh final verification', text)
        self.assertIn('matched at its recorded check; full request completion remains unproved', text)
        self.assertIn('Last recorded readback', text)

    def test_wrong_scope_or_goal_readback_is_not_reused(self):
        value = report()
        value['verification']['verification']['scopes']['scope:a']['goals'][0]['goal_id'] = 'different'
        text = self.text(value)
        self.assertIn('Bound result readback: unavailable', text)
        self.assertNotIn('15.5', text)

    def test_exact_whitespace_empty_and_false_are_not_erased(self):
        value = report()
        scope = value['tool_evidence']['scopes']['scope:a']
        scope['goals'] = [{'id': 'result', 'kind': 'text', 'target': 'Reviewed editor', 'value': ' x\r\nΩ '}]
        row = value['verification']['verification']['scopes']['scope:a']['goals'][0]
        row.update(reason='bound_predicate_not_met', evidence={'actual': '', 'plane': 'editor_buffer'})
        text = self.text(value)
        self.assertIn('" x\\r\\n\\u03a9 " (editor buffer)', text)
        self.assertIn('Last recorded readback: "" (editor_buffer)', text)
        self.assertIn('The bound control did not match the requested value', text)
        self.assertNotIn('issued input', text)
        scope['goals'][0].update(kind='state', value=False)
        row['evidence'] = {'actual': False, 'plane': 'display'}
        self.assertIn('Last recorded readback: false', self.text(value))

    def test_unknown_reset_and_evaluation_receipt_remain_separate(self):
        value = report()
        witness = value['tool_evidence']['scopes']['scope:a']['witness']
        witness.update(known_start=False, issued_expression_since_clear='', issued_evaluation=None)
        self.assertIn('A full reset or known starting expression is unproved', self.text(value))
        witness.update(known_start=True, issued_expression_since_clear='(18-5)/2', issued_evaluation='(18-5)/2')
        text = self.text(value)
        self.assertIn('Evaluation issued for: "(18-5)/2" (issuance only)', text)
        self.assertIn('Unmet:', text)

    def test_bounded_output_marks_truncation_and_additional_goals_and_preserves(self):
        value = report()
        goal = value['tool_evidence']['scopes']['scope:a']['goals'][0]
        goal.update(expression='1' * 10000 + '\x1b[31m\n')
        value['tool_evidence']['scopes']['scope:a']['goals'] = [deepcopy(goal) for _ in range(9)]
        value['verification']['verification']['scopes']['scope:a']['preserves'] = [{'matched': False}]
        text = self.text(value)
        self.assertIn('[truncated]', text)
        self.assertIn('5 additional reviewed outcomes', text)
        self.assertIn('Preservation checks not established: 1', text)
        self.assertNotIn('\x1b', text)
        self.assertLess(len(text), 2600)

    def test_no_scope_and_success_are_honest(self):
        value = report(timeout=True)
        value['tool_evidence']['scopes'] = {}
        self.assertIn('No reviewed outcome was established', self.text(value))
        value['status'] = 'verified_reviewed_scope'
        self.assertEqual(self.text(value), '')
        self.assertEqual(self.text({'status': 'blocked', 'reason': 'Missing configuration'}), '')

    def test_uncertain_input_reports_recorded_cause_without_dumping_capture(self):
        value = report(timeout=True)
        value['tool_evidence']['scopes']['scope:a']['status'] = 'blocked_uncertain'
        value['tool_evidence']['events'].append({'tool': 'locua_act', 'result': {
            'scope_id': 'scope:a', 'uncertain_action': True,
            'raw_action_result': {'observation': 'PRIVATE_CAPTURE', 'verification': {
                'readback': {'reason': 'bound_target_absent'}}}},
            'model_result': {'status': 'uncertain', 'reason': 'bound_target_absent', 'no_retry': True}})
        text = self.text(value)
        self.assertIn('An earlier input remains uncertain', text)
        self.assertIn('another write is disabled', text)
        self.assertIn('previously bound result control', text)
        self.assertNotIn('PRIVATE_CAPTURE', text)

    def test_text_cli_prints_evidence_without_changing_library_result_or_arguments(self):
        value = {'ok': False, 'result': report(timeout=True)}
        before = deepcopy(value)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('locua.cli.lib.do', return_value=value) as operation, redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['do', 'Evaluate (18 - 5) / 2.'])
        self.assertEqual(code, 6)
        self.assertEqual(value, before)
        self.assertEqual(operation.call_args.kwargs['request'], before['result']['request'])
        self.assertIn('Last recorded issued input', stdout.getvalue())
        self.assertIn('Results: /synthetic/task/summary.json', stdout.getvalue())

    def test_json_cli_retains_exact_original_library_result(self):
        value = {'ok': False, 'result': report(timeout=True)}
        stdout = io.StringIO()
        with patch('locua.cli.lib.do', return_value=value), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(['do', 'Evaluate (18 - 5) / 2.', '--json'])
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(stdout.getvalue()), value)


class ActionProgressTests(unittest.TestCase):
    def progress(self, output, name='locua_act'):
        data = {'tool_name': name, 'input': {'secret': 'not for printing'},
                'result': {'success': True, 'output': output}}
        original = deepcopy(data)
        lines = _tool_progress(data)
        self.assertEqual(data, original)
        return '\n'.join(lines)

    def test_reviewed_progress_does_not_claim_whole_task_or_echo_plan_prose(self):
        text = self.progress({'status': 'verified', 'plan_progress': {
            'items': [{'status': 'completed'}, {'status': 'current', 'description': 'PRIVATE_PLAN'}]}},
            name='locua_verify')
        self.assertIn('1/2 scopes verified', text)
        self.assertIn('final task verification remains separate', text)
        self.assertNotIn('PRIVATE_PLAN', text)
        self.assertNotIn('task completed', text)

    def test_sequence_progress_reports_partial_issuance_and_separate_verification(self):
        text=self.progress({'status':'refused','steps_completed':3,'steps_planned':8,
            'action_started':True,'reason':'Observed navigation changed the layout'},name='locua_act_sequence')
        self.assertIn('3/8 steps completed',text)
        self.assertIn('outcome verification separate',text)
        self.assertIn('navigation changed',text)
        self.assertNotIn('task completed',text)

    def test_sequence_preflight_names_bad_step_without_dumping_values(self):
        text=self.progress({'status':'refused','code':'sequence_preflight_refused',
            'failed_step':7,'steps_attempted':0,'steps_completed':0,'steps_planned':11,
            'selected_control':{'name':'Mode','action_kind':'press'},
            'reason':'Action outside reviewed effects'},name='locua_act_sequence')
        self.assertIn('Rejected step 7: Mode (press)',text)
        self.assertIn('No sequence input was attempted',text)

    def test_arithmetic_receipt_reports_issuance_not_result(self):
        text = self.progress({'status': 'dispatched', 'action_started': True, 'arithmetic_input': {
            'issued_expression_since_clear': '18-5/2', 'issued_evaluation': '18-5/2', 'known_start': True}})
        self.assertIn('Issued input: "18-5/2"', text)
        self.assertIn('Evaluation issued for: "18-5/2" (issuance only)', text)
        self.assertIn('result not yet verified', text)

    def test_editor_receipt_confirms_only_exact_buffer_without_leaking_text(self):
        text = self.progress({'status': 'verified', 'action_started': True,
            'arithmetic_input': {'issued_expression_since_clear': '', 'issued_evaluation': None, 'known_start': False},
            'verification': {'plane': 'editor_buffer', 'exact_value_proven': True,
                             'readback': {'evidence': {'actual': 'PRIVATE_BUFFER'}}}})
        self.assertIn('Exact editor-buffer readback confirmed', text)
        self.assertIn('committed content and saving are not proved', text)
        self.assertNotIn('PRIVATE_BUFFER', text)
        self.assertNotIn('Issued input:', text)

    def test_refused_uncertain_unstarted_or_unproven_edits_never_get_success_message(self):
        base = {'status': 'verified', 'action_started': True,
                'verification': {'plane': 'editor_buffer', 'exact_value_proven': True}}
        for changes in ({'status': 'refused'}, {'status': 'uncertain'}, {'action_started': False},
                        {'verification': {'plane': 'editor_buffer', 'exact_value_proven': False}},
                        {'verification': {'plane': 'display', 'exact_value_proven': True}}):
            with self.subTest(changes=changes):
                self.assertNotIn('readback confirmed', self.progress({**base, **changes}))

    def test_progress_literals_are_bounded_escaped_and_only_from_act_receipts(self):
        output = {'status': 'dispatched', 'action_started': True, 'arithmetic_input': {
            'issued_expression_since_clear': '1\n\x1b' + '2' * 10000, 'known_start': True}}
        text = self.progress(output)
        self.assertNotIn('\x1b', text)
        self.assertLess(len(text), 270)
        self.assertNotIn('Issued input:', self.progress(output, name='locua_inspect'))

    def test_uncertain_receipt_explains_read_only_recovery_without_claiming_success(self):
        text = self.progress({'status': 'uncertain', 'reason': 'bound_target_absent',
            'action_started': True, 'no_retry': True,
            'reconciliation': {'available': True, 'read_only': True}})
        self.assertIn('bound_target_absent', text)
        self.assertIn('repeating it is disabled', text)
        self.assertIn('Fresh read-only verification', text)
        self.assertNotIn('readback confirmed', text)

    def test_reconciliation_progress_does_not_claim_delivery_or_renew_input(self):
        result = {'status': 'verified', 'scope_status': 'reconciled_verified',
            'reconciliation': {'status': 'current_predicates_verified',
                               'input_authority_restored': False}}
        text = self.progress(result, name='locua_verify')
        self.assertIn('input remains disabled', text)
        self.assertIn('Delivery of the earlier action is still unproved', text)
        result['status'] = 'unverified'
        self.assertNotIn('outcomes match', self.progress(result, name='locua_verify'))


class CostPresentationTests(unittest.TestCase):
    def test_cost_receipt_labels_this_run_and_shared_unknowns_separately(self):
        # Execute only the existing terminal-rendering expression, without a
        # provider/session/network or duplication of its format in the test.
        tree = ast.parse(inspect.getsource(amplifier_session.run))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'progress'
                 and n.args and isinstance(n.args[0], ast.JoinedStr)
                 and any(isinstance(v, ast.Constant) and isinstance(v.value, str)
                         and v.value.startswith('API estimate for this run:') for v in n.args[0].values)]
        self.assertEqual(len(calls), 1)
        code = compile(ast.Expression(body=calls[0].args[0]), '<cost presentation>', 'eval')
        cost = {'per_run_known_charge_upper_usd': .2, 'charged_upper_bound_usd': 11.5377,
                'cap_usd': 15, 'per_run_unknown_reservation_count': 0, 'unknown_reservations': 1}
        original = deepcopy(cost)
        text = eval(code, {'__builtins__': {}}, {'cost': cost})
        self.assertIn('0 this run; 1 shared', text)
        self.assertIn('shared budget $11.5377 / $15.00', text)
        self.assertEqual(cost, original)


if __name__ == '__main__':
    unittest.main()
