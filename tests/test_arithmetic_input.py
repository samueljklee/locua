import unittest
from locua.arithmetic_input import InputWitness, symbol


class ArithmeticInputTests(unittest.TestCase):
    def test_semantics_are_generic_and_do_not_authorize_other_buttons(self):
        self.assertEqual(symbol({'role':'AXButton','name':'Multiply'}),'*')
        self.assertEqual(symbol({'role':'AXButton','name':'7'}),'7')
        for role,name in [('AXMenuItem','7'),('AXButton','Clear History'),('AXButton','Delete'),('AXButton','Quit'),('AXButton','Clear Memory')]:
            self.assertIsNone(symbol({'role':role,'name':name}))
        self.assertIsNone(symbol({'role':'AXButton','name':'1','semantics':{'description':'2'}}))

    def test_matching_old_answer_is_not_an_input_witness(self):
        witness=InputWitness()
        for token in '12*3=':witness.record(token,snapshot_id='s',descriptor='observed')
        self.assertFalse(witness.matches('12*3'))
        witness.record('clear',snapshot_id='s',descriptor='observed')
        for token in '12*3=':witness.record(token,snapshot_id='s',descriptor='observed')
        self.assertTrue(witness.matches('12 * 3'))
        self.assertFalse(witness.matches('13*3'))
        witness.record('2',snapshot_id='s',descriptor='observed')
        self.assertFalse(witness.matches('12*3'))

    def test_full_replacement_still_needs_evaluation(self):
        witness=InputWitness();witness.record_replacement('12*3',snapshot_id='s',descriptor='editor')
        self.assertFalse(witness.matches('12*3'))
        witness.record('=',snapshot_id='s2',descriptor='Equals')
        self.assertTrue(witness.matches('12*3'))

    def test_clear_entry_does_not_prove_whole_expression_was_reset(self):
        witness=InputWitness()
        token=symbol({'role':'AXButton','name':'Clear Entry'})
        self.assertEqual(token,'clear_entry')
        witness.record(token,snapshot_id='s1',descriptor='Clear Entry')
        # The application could still hold a previous pending operator/left
        # operand. Matching issued keys must not establish a known full input.
        for token in '2+3=':witness.record(token,snapshot_id='s2',descriptor='observed')
        self.assertFalse(witness.known_start)
        self.assertIsNone(witness.evaluated)
        self.assertFalse(witness.matches('2+3'))
        self.assertEqual(witness.events[0]['input'],'clear_entry')

    def test_clear_entry_invalidates_an_existing_complete_witness(self):
        witness=InputWitness();witness.record_replacement('2+3',snapshot_id='s1',descriptor='editor')
        witness.record('=',snapshot_id='s2',descriptor='Equals')
        self.assertTrue(witness.matches('2+3'))
        witness.record('clear_entry',snapshot_id='s3',descriptor='Clear Entry')
        self.assertFalse(witness.matches('2+3'))
        self.assertIsNone(witness.view()['issued_evaluation'])
        for token in '2+3=':witness.record(token,snapshot_id='s4',descriptor='observed')
        self.assertFalse(witness.matches('2+3'))

    def test_explicit_all_clear_restores_reset_witness(self):
        for label in ('All Clear','AC'):
            with self.subTest(label=label):
                witness=InputWitness();witness.record('clear_entry',snapshot_id='s1',descriptor='Clear Entry')
                token=symbol({'role':'AXButton','name':label});self.assertEqual(token,'clear')
                witness.record(token,snapshot_id='s2',descriptor=label)
                for token in '2+3=':witness.record(token,snapshot_id='s3',descriptor='observed')
                self.assertTrue(witness.matches('2+3'))

    def test_conflicting_reset_semantics_refuse_and_unissued_event_keeps_witness(self):
        self.assertIsNone(symbol({'role':'AXButton','name':'Clear',
                                  'semantics':{'description':'All Clear'}}))
        witness=InputWitness();witness.record_replacement('2+3',snapshot_id='s1',descriptor='editor')
        witness.record('=',snapshot_id='s2',descriptor='Equals')
        witness.record('clear_entry',snapshot_id='s3',descriptor='Clear Entry',issued=False)
        self.assertTrue(witness.matches('2+3'))

    def test_generic_clear_labels_do_not_authorize_full_reset(self):
        for label in ('Clear','C','clear','Clear Entry'):
            with self.subTest(label=label):
                token=symbol({'role':'AXButton','name':label})
                self.assertEqual(token,'clear_entry')
                witness=InputWitness();witness.record_replacement('2+3',snapshot_id='before',descriptor='editor')
                witness.record(token,snapshot_id='after',descriptor=label)
                self.assertFalse(witness.known_start)
                for entered in '2+3=':witness.record(entered,snapshot_id='later',descriptor='observed')
                self.assertFalse(witness.matches('2+3'))
                self.assertIsNone(witness.evaluated)

    def test_recorded_clear_control_retaining_expression_prefix_invalidates_witness(self):
        # Scoped regression from v64-openai-fresh-1 event24: actual AX value
        # changed from 81-294 to 81-, with the following observed button fields.
        # No application identity, desired answer, or replayed GUI is involved.
        before={'role':'AXButton','name':'Clear','semantics':{
            'title':None,'description':'Clear','help':None,'identifier':'Clear'}}
        after={'role':'AXButton','name':'All Clear','semantics':{
            'title':None,'description':'All Clear','help':None,'identifier':'AllClear'}}
        witness=InputWitness();witness.record('clear',snapshot_id='initial',descriptor='All Clear')
        for token in '81-294':witness.record(token,snapshot_id='before',descriptor='observed')
        witness.record(symbol(before),snapshot_id='after-clear',descriptor='Clear')
        self.assertFalse(witness.known_start)
        self.assertIsNone(witness.evaluated)
        self.assertIn('known_start',witness.view()['missing_issuance_prerequisites'])
        self.assertIn('generic Clear/C',witness.view()['verification_prerequisites']['known_start'])
        # A separately selected newly observed All Clear can establish a start.
        # Merely discovering that new label has not changed the witness above.
        self.assertEqual(symbol(after),'clear')
        witness.record(symbol(after),snapshot_id='after-full-clear',descriptor='All Clear')
        self.assertTrue(witness.known_start)
        self.assertEqual(witness.entered,'')
        self.assertIsNone(witness.evaluated)

    def test_compatible_partial_clear_labels_remain_non_full_reset(self):
        self.assertEqual(symbol({'role':'AXButton','name':'C',
            'semantics':{'description':'Clear'}}),'clear_entry')
        self.assertEqual(symbol({'role':'AXButton','name':'Clear',
            'semantics':{'description':'Clear Entry'}}),'clear_entry')
        self.assertIsNone(symbol({'role':'AXButton','name':'AC',
            'semantics':{'description':'Clear'}}))


if __name__=='__main__':unittest.main()
