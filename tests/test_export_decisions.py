from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('export_decisions',ROOT/'tools/export_decisions.py')
exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)


def request(history=None):
    return {'goal':'Keep  0042 Ω\n','observation_summary':'UNTRUSTED UI:  supplied context\n',
        'candidates':[{'id':'z','description':'Set  0042 Ω\n'},{'id':'a','description':'Return to overview'}],
        'history':[] if history is None else history}


def event(value=None):
    value=request() if value is None else value
    return {'type':'decision_request','schema':'locua.decision_request.v1','phase':'inspect','attempt':1,
        'max_context_tokens':8192,'request_sha256':exporter.digest(value),'request':deepcopy(value)}


class ExportDecisionTests(unittest.TestCase):
    def test_preserves_fields_order_history_and_never_infers_gold(self):
        value=request([{'action':'Inspected  other\n','outcome':'No desktop effect Ω'}]);captured=event(value)
        events=[{'type':'oracle','expected_id':'a'},captured,
                {'type':'decision','phase':'inspect','request_sha256':captured['request_sha256'],
                 'response':{'selected_id':'z','correct':True,'expected_id':'a'}}]
        before=deepcopy(events)
        bundle,manifest=exporter.convert_events(events)
        self.assertEqual(events,before)
        self.assertEqual(set(bundle),{'kind','cases'})
        case=bundle['cases'][0]
        self.assertEqual({k:case[k] for k in exporter.INPUT_KEYS},value)
        self.assertEqual([x['id'] for x in case['candidates']],['z','a'])
        self.assertEqual(set(case),exporter.INPUT_KEYS|{'id'})
        self.assertEqual(manifest['scored_cases'],0)
        self.assertEqual(manifest['requests_with_response'],1)
        self.assertNotIn('expected_id',json.dumps(bundle))

    def test_unreturned_and_identical_requests_retained_deterministically(self):
        events=[event(),event()]
        first,manifest=exporter.convert_events(events)
        second,_=exporter.convert_events(deepcopy(events))
        self.assertEqual(first,second)
        self.assertEqual(len({c['id'] for c in first['cases']}),2)
        self.assertEqual(manifest['requests_without_response'],2)

    def test_missing_history_hidden_gold_invalid_hash_and_legacy_refuse(self):
        for mutation in ('history','gold','sha','legacy'):
            events=[event()]
            if mutation=='history':del events[0]['request']['history']
            elif mutation=='gold':events[0]['request']['expected_id']='z'
            elif mutation=='sha':events[0]['request_sha256']='bad'
            else:events.append({'type':'decision','response':{'context':request()}})
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):exporter.convert_events(events)

    def test_old_evaluator_cannot_drop_history_silently(self):
        value=request([{'action':'inspected','outcome':'observed'}])
        with patch.object(exporter,'_decision_cases',return_value=([],[{**value,'history':[]}])):
            with self.assertRaisesRegex(ValueError,'changes captured'):exporter.convert_events([event(value)])
        with patch.object(exporter,'_decision_cases',side_effect=ValueError('unknown history')):
            with self.assertRaisesRegex(ValueError,'history support'):exporter.convert_events([event(value)])

    def test_private_new_output_and_no_overwrite_or_partial_invalid_export(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'loop.jsonl';out=Path(directory)/'export'
            source.write_text(json.dumps(event())+'\n')
            exporter.export(source,out)
            self.assertEqual(out.stat().st_mode & 0o777,0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777==0o600 for p in out.iterdir()))
            with self.assertRaises(FileExistsError):exporter.export(source,out)
            source.write_text('{"type":"decision","response":{}}\n')
            with self.assertRaises(ValueError):exporter.export(source,Path(directory)/'invalid')
            self.assertFalse((Path(directory)/'invalid').exists())

    def test_no_partial_export_when_more_than_eval_capacity(self):
        with self.assertRaisesRegex(ValueError,'100 requests'):exporter.convert_events([event() for _ in range(101)])


if __name__=='__main__':unittest.main()
