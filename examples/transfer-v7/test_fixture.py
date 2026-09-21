"""Fixture/oracle checks only. No socket, desktop, model or engine calls."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


server = module('v7_fixture_server', HERE/'server.py')
auditor = module('v7_fixture_auditor', ROOT/'tools/audit_transfer_v7.py')


class FixtureChecks(unittest.TestCase):
    def test_frozen_inputs_and_no_oracle_in_served_pages(self):
        self.assertGreater(auditor.verify_freeze()['file_count'], 8)
        gold = json.loads((HERE/'oracle.json').read_text())['cases']
        for case in ('project','release'):
            page = server.render(case).decode()
            for key, value in gold[case]['fields'].items():
                if type(value) is str and value != server.INITIAL[case][key]:
                    self.assertNotIn(value, page)
            self.assertNotIn('oracle.json', page)
        self.assertEqual(server.PAGES['project'].count('>Headline<'), 2)
        self.assertEqual(server.PAGES['release'].count('>Display name<'), 2)
        self.assertEqual(server.PAGES['release'].count('Public listing'), 2)

    def test_records_actual_wrong_values_and_new_directory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'new'; store = server.ReceiptStore(path)
            self.assertFalse((path/'project.json').exists())
            values = deepcopy(server.INITIAL['project']); values['project.reference']='WRONG'
            store.record('project', values)
            self.assertEqual(json.loads((path/'project.json').read_text()), values)
            result = auditor.audit('project', path/'project.json')
            self.assertEqual(result['status'], 'fail')
            with self.assertRaises(FileExistsError): server.ReceiptStore(path)

    def test_complete_fields_strict_types_and_duplicate_json(self):
        initial = deepcopy(server.INITIAL['release'])
        del initial['channel.name']
        with self.assertRaises(ValueError): server.validate_state('release', initial)
        initial = deepcopy(server.INITIAL['release']); initial['release.public']=0
        with self.assertRaises(ValueError): server.validate_state('release', initial)
        with self.assertRaises(ValueError):
            json.loads('{"x":1,"x":2}', object_pairs_hook=server.unique_object)

    def test_exact_native_bytes_and_no_save_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'document.txt'; raw=(HERE/'expected/field-survey.txt').read_bytes()
            path.write_bytes(raw)
            result=auditor.audit('field-survey', path)
            self.assertEqual(result['status'],'pass')
            self.assertFalse(result['explicit_save_mechanism_proven'])
            self.assertFalse(result['task_execution_proven'])
            path.write_bytes(raw.strip())
            self.assertEqual(auditor.audit('field-survey', path)['status'],'fail')

    def test_extra_fields_and_numeric_bool_do_not_pass_oracle(self):
        gold=json.loads((HERE/'oracle.json').read_text())['cases']['release']['fields']
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'receipt.json'
            for data in [dict(gold, unexpected='extra'),dict(gold, **{'release.public':0})]:
                path.write_text(json.dumps(data))
                self.assertEqual(auditor.audit('release',path)['status'],'fail')

    def test_event_journal_agrees_with_final_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=server.ReceiptStore(Path(tmp)/'new')
            for text in ['Unfinished','Actual typed data']:
                state=deepcopy(server.INITIAL['project']);state['project.headline']=text
                store.record('project',state)
            events=[json.loads(s) for s in (store.out/'events.jsonl').read_text().splitlines()]
            self.assertEqual([e['sequence'] for e in events],[1,2])
            self.assertEqual(events[-1]['values'],json.loads((store.out/'project.json').read_text()))


if __name__ == '__main__':
    unittest.main()
