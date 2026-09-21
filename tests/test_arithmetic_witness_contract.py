"""Published arithmetic proof prerequisites; no model or desktop calls."""
from copy import deepcopy
import unittest

from locua.arithmetic_input import InputWitness, symbol
from locua.amplifier_contracts import GOAL, EFFECT, SPECS, validate_tool_arguments


class ArithmeticWitnessContractTests(unittest.TestCase):
    def issue(self, witness, tokens):
        for token in tokens:
            witness.record(token, snapshot_id='observed-snapshot', descriptor='observed capability')

    def test_initial_view_explains_why_visible_zero_is_not_issuance_evidence(self):
        witness = InputWitness()
        view = witness.view()
        self.assertFalse(view['known_start'])
        self.assertEqual(view['missing_issuance_prerequisites'], ['known_start', 'evaluation_from_known_start'])
        rule = view['verification_prerequisites']['known_start']
        self.assertIn('All Clear/AC or whole-expression replacement', rule)
        self.assertIn('fresh zero display, generic Clear/C or Clear Entry is insufficient', rule)
        # The only captured application fact might be zero. It supplies no
        # witness event and must not silently authorize an arbitrary start.
        self.issue(witness, '8*7=')
        self.assertFalse(witness.matches('8*7'))
        self.assertIn('known_start', witness.view()['missing_issuance_prerequisites'])

    def test_full_reset_then_evaluation_removes_only_issuance_gaps(self):
        witness = InputWitness()
        self.issue(witness, [symbol({'role':'AXButton', 'name':'All Clear'})])
        self.assertEqual(witness.view()['missing_issuance_prerequisites'], ['evaluation_from_known_start'])
        self.issue(witness, '8*7=')
        self.assertTrue(witness.matches('8*7'))
        self.assertEqual(witness.view()['missing_issuance_prerequisites'], [])
        self.assertIn('Independent fresh bound UI readback', witness.view()['verification_prerequisites']['result'])
        self.assertNotIn('task_complete', witness.view())

    def test_whole_expression_replacement_is_alternative_but_still_needs_evaluation(self):
        witness = InputWitness()
        witness.record_replacement('8 * 7', snapshot_id='replacement', descriptor='observed editor')
        self.assertEqual(witness.view()['missing_issuance_prerequisites'], ['evaluation_from_known_start'])
        self.assertFalse(witness.matches('8*7'))
        self.issue(witness, '=')
        self.assertTrue(witness.matches('8*7'))

    def test_clear_entry_and_unissued_reset_never_satisfy_published_known_start(self):
        for token, issued in [('clear_entry', True), ('clear', False)]:
            with self.subTest(token=token, issued=issued):
                witness = InputWitness()
                witness.record(token, snapshot_id='capture', descriptor='observed', issued=issued)
                self.issue(witness, '8*7=')
                self.assertIn('known_start', witness.view()['missing_issuance_prerequisites'])
                self.assertFalse(witness.matches('8*7'))

    def test_view_is_read_only_and_explains_requirements_before_any_action(self):
        witness = InputWitness(); before = deepcopy(witness.__dict__)
        view = witness.view(); view['verification_prerequisites']['known_start'] = 'modified caller copy'
        self.assertEqual(witness.__dict__, before)
        self.assertNotEqual(witness.view()['verification_prerequisites']['known_start'], 'modified caller copy')
        calculation = next(branch for branch in GOAL['oneOf'] if branch['properties']['kind']['const']=='calculation')
        goal_effect = next(branch for branch in EFFECT['oneOf'] if branch['properties']['kind']['const']=='goal')
        for description in (calculation['description'], goal_effect['description'], SPECS['locua_act'][0]):
            self.assertIn('full reset or whole-expression replacement', description)
            self.assertIn('fresh zero', description.lower())
        validate_tool_arguments('locua_review', {'snapshot_id':'capture', 'summary':'Reviewed arithmetic',
            'goals':[{'id':'g','kind':'calculation','target':'observed readout','control_id':'readout',
                      'expression':'8*7','evidence_plane':'display'}],
            'effects':[{'kind':'goal','goal_id':'g'}]})


if __name__=='__main__': unittest.main()
