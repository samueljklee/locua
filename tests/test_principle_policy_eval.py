"""Regression evaluation must reject plausible but wrong calls without UI I/O."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from principle_policy_eval import CASES, evaluate, rubric_for, transform


class PrincipleEvalTests(unittest.TestCase):
    def fixture(self, cid):
        _, _, path, call = next(c for c in CASES if c[0] == cid)
        run = ROOT/'artifacts'/path
        source = run/'provider'/f'call-{call:03d}-input.json'
        if not source.exists(): self.skipTest('private retained run not distributed')
        request = json.loads(source.read_text())
        return request, rubric_for(cid, run, request, call)

    def call(self, name, **args):
        return {'tool_calls': [{'name': name, 'arguments': args}]}

    def test_transform_preserves_all_history_schemas_and_full_original_request(self):
        for cid, *_ in CASES:
            with self.subTest(cid=cid):
                request, _ = self.fixture(cid)
                for profile in ('concise-v1', 'principles-v1'):
                    candidate, _ = transform(request, profile)
                    self.assertEqual(candidate['tools'], request['tools'])
                    self.assertEqual(candidate['messages'][1:], request['messages'][1:])
                    marker = '\nORIGINAL USER REQUEST (retain throughout):\n'
                    self.assertEqual(candidate['messages'][0]['content'].split(marker)[1:],
                                     request['messages'][0]['content'].split(marker)[1:])

    def test_discovery_accepts_multiple_grounded_paths_rejects_scope_confusion(self):
        req, r = self.fixture('region-discovery')
        for args in ({'region_id': r['region']}, {'query': r['query']}):
            response = self.call('locua_inspect', snapshot_id=r['snapshot'], operation='list', **args)
            self.assertTrue(evaluate('region-discovery', req, r, response)['passed'])
        response = self.call('locua_status', operation='goals', scope_id=r['snapshot'])
        self.assertFalse(evaluate('region-discovery', req, r, response)['passed'])

    def test_tool_help_changes_only_existing_descriptions_in_historical_inventory(self):
        for cid, *_ in CASES:
            with self.subTest(cid=cid):
                request,_=self.fixture(cid)
                framework,_=transform(request,'principles-v1')
                guided,_=transform(request,'principles-help-v1',allow_historical_inventory=True)
                self.assertEqual(framework['messages'],guided['messages'])
                self.assertEqual([(t['name'],t['parameters']) for t in framework['tools']],
                                 [(t['name'],t['parameters']) for t in guided['tools']])
                self.assertNotEqual(framework['tools'],guided['tools'])
        request,_=self.fixture('exact-editor')
        request['tools'][0]['name']='unrecognized_tool'
        with self.assertRaises(ValueError):transform(request,'principles-help-v1',allow_historical_inventory=True)

    def test_semantic_review_rejects_competitor_and_unsupported_toggle(self):
        req, r = self.fixture('semantic-target')
        response = self.call('locua_review', snapshot_id=r['snapshot'], summary='Change only requested appearance.',
            goals=[{'id':'g1','kind':'state','target':'Appearance','control_id':r['control'],
                    'property':'selected','value':True,'evidence_plane':'display'}],
            effects=[{'kind':'press','control_id':r['control'],'purpose':'Set appearance'}],
            covers_entire_request=True)
        self.assertTrue(evaluate('semantic-target',req,r,response)['passed'])
        bad = deepcopy(response); bad['tool_calls'][0]['arguments']['goals'][0]['control_id']=r['competitors'][0]
        self.assertFalse(evaluate('semantic-target',req,r,bad)['passed'])
        bad = deepcopy(response); bad['tool_calls'][0]['arguments']['effects']=[{'kind':'goal','goal_id':'g1'}]
        self.assertFalse(evaluate('semantic-target',req,r,bad)['passed'])

    def test_exact_text_and_verification_not_acknowledgement(self):
        req, r = self.fixture('exact-editor')
        response = {'tool_calls':[deepcopy(r['expected'])]}
        self.assertTrue(evaluate('exact-editor',req,r,response)['passed'])
        response['tool_calls'][0]['arguments']['value'] = r['literal'].strip()
        self.assertFalse(evaluate('exact-editor',req,r,response)['passed'])
        req,r=self.fixture('verify-editor')
        self.assertTrue(evaluate('verify-editor',req,r,self.call('locua_verify',all=True))['passed'])
        self.assertFalse(evaluate('verify-editor',req,r,{'content':'Done'})['passed'])

    def test_recorded_wrong_arithmetic_is_not_a_pass(self):
        req,r=self.fixture('arithmetic-identity')
        run=ROOT/'artifacts/action-sequence-v11-001/live-calculator-local-2'
        response=json.loads((run/'provider/call-011-summary.json').read_text())['response']
        self.assertFalse(evaluate('arithmetic-identity',req,r,response)['passed'])


if __name__ == '__main__': unittest.main()
