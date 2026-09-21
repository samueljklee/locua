"""Offline fixture/oracle tests. No socket, browser, Cua or model operations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
def load(name):
    spec=importlib.util.spec_from_file_location('context_check_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
server=load('server');auditor=load('verify')


class FixtureTests(unittest.TestCase):
    def test_frozen_task_and_separate_oracle_do_not_enter_html(self):
        self.assertEqual(auditor.verify_freeze()['files'],7)
        page=server.render().decode();desired=json.loads((HERE/'oracle.json').read_text())['expected']['recording.room']
        self.assertNotIn(desired,page);self.assertNotIn('oracle.json',page);self.assertNotIn('task.txt',page)
        self.assertEqual(page.count('<label>Room<input'),2);self.assertEqual(page.count('>Send reminder</label>'),2)
        for label in ('Soundcheck','Recording','Coordination'):self.assertIn('aria-label="'+label+'"',page)

    def test_initial_state_is_not_pass_and_two_legitimate_changes_are(self):
        with tempfile.TemporaryDirectory() as td:
            store=server.ReceiptStore(Path(td)/'receipts')
            self.assertFalse(auditor.verify(store.out)['passed'])
            state=dict(server.INITIAL);store.record(state)
            self.assertFalse(auditor.verify(store.out)['passed'])
            gold=json.loads((HERE/'oracle.json').read_text())['expected']
            state['recording.room']=gold['recording.room'];store.record(state)
            state['recording.reminder']=True;store.record(state)
            result=auditor.verify(store.out);self.assertTrue(result['passed']);self.assertEqual(result['checked_fields'],5)

    def test_transient_competitor_change_fails_even_when_final_restored(self):
        with tempfile.TemporaryDirectory() as td:
            store=server.ReceiptStore(Path(td)/'receipts');bad=dict(server.INITIAL)
            bad['soundcheck.room']='unexpected';store.record(bad)
            store.record(json.loads((HERE/'oracle.json').read_text())['expected'])
            result=auditor.verify(store.out)
            self.assertTrue(result['final_exact']);self.assertFalse(result['passed'])
            self.assertFalse(result['preserved_every_recorded_event'])

    def test_strict_fields_types_duplicate_keys_and_fresh_receipt_directory(self):
        for bad in ({},dict(server.INITIAL,extra='x'),dict(server.INITIAL,**{'recording.reminder':1})):
            with self.assertRaises(ValueError):server.validate(bad)
        with self.assertRaises(ValueError):json.loads('{"x":1,"x":2}',object_pairs_hook=server.unique_object)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileExistsError):server.ReceiptStore(td)


if __name__=='__main__':unittest.main()
