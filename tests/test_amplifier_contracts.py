"""Pure argument-boundary regressions; no desktop, subprocess, or model calls."""
from copy import deepcopy
import json
import unittest

from locua.amplifier_contracts import (
    ArgumentContractError, EFFECT, GOAL, INSPECT_SCHEMA, SPECS,
    validate_tool_arguments,
)


def text_review():
    return {
        'snapshot_id':'capture-17', 'summary':'Replace the reviewed editor buffer.',
        'goals':[{'id':'draft', 'kind':'text', 'target':'Owned draft editor',
                  'control_id':'capture-17:4', 'value':'Status: reviewed locally.',
                  'evidence_plane':'editor_buffer'}],
        'effects':[{'kind':'goal', 'goal_id':'draft'}],
        'covers_entire_request':True,
    }


class ArgumentContractTests(unittest.TestCase):
    def accepted(self, tool, args):
        original=deepcopy(args)
        self.assertIsNone(validate_tool_arguments(tool, args))
        self.assertEqual(args, original)

    def refused(self, tool, args):
        original=deepcopy(args)
        with self.assertRaises(ArgumentContractError) as caught:
            validate_tool_arguments(tool, args)
        self.assertEqual(args, original)
        result=caught.exception.as_result()
        self.assertFalse(result['action_started'])
        self.assertFalse(result['task_complete'])
        self.assertFalse(result['arguments_rewritten'])
        return result

    def test_published_goal_and_effect_are_discriminated_and_serializable(self):
        for name, (_description, schema) in SPECS.items():
            with self.subTest(name=name):
                self.assertEqual(json.loads(json.dumps(schema)), schema)
                self.assertFalse(schema['additionalProperties'])
        self.assertEqual({b['properties']['kind']['const'] for b in GOAL['oneOf']},
                         {'text','state','calculation'})
        self.assertEqual({b['properties']['kind']['const'] for b in EFFECT['oneOf']},
                         {'goal','press'})

    def test_literal_is_preserved_exactly_and_empty_is_valid(self):
        for value in ('  00019\r\nΩ  ', ''):
            args=text_review();args['goals'][0]['value']=value
            self.accepted('locua_review',args)
            self.assertEqual(args['goals'][0]['value'],value)

    def test_informational_save_caveat_does_not_add_requested_save(self):
        args=text_review()
        args['limitations']=['Editor buffer only; saved-file persistence is not established.']
        self.accepted('locua_review',args)
        self.assertNotIn('unresolved_requirements',args)

    def test_real_unresolved_user_requirement_prevents_full_coverage(self):
        args=text_review();args['unresolved_requirements']=['User explicitly requested a saved file.']
        result=self.refused('locua_review',args)
        self.assertTrue(any(e['path']=='$.unresolved_requirements' for e in result['errors']))
        args['covers_entire_request']=False
        self.accepted('locua_review',args)

    def test_navigation_only_scope_cannot_claim_whole_request(self):
        args=text_review();args['goals']=[]
        args['effects']=[{'kind':'press','control_id':'capture-17:6','purpose':'Open observed editor options'}]
        self.refused('locua_review',args)
        args['covers_entire_request']=False
        self.accepted('locua_review',args)

    def test_goal_effect_rejects_press_fields_with_exact_paths(self):
        args=text_review()
        args['effects'][0].update(control_id='capture-17:4',purpose='Replace text')
        error=self.refused('locua_review',args)
        unknown={e['path'] for e in error['errors'] if e['code']=='unknown_argument'}
        self.assertEqual(unknown,{'$.effects[0].control_id','$.effects[0].purpose'})
        self.assertIn('goal_id',error['reason'])

    def test_press_effect_requires_control_and_nonblank_purpose(self):
        args=text_review();args['covers_entire_request']=False
        for effect in ({'kind':'press','goal_id':'draft'},
                       {'kind':'press','control_id':'capture-17:6','purpose':'  '}):
            args['effects']=[effect];self.refused('locua_review',args)

    def test_goals_require_kind_specific_value_and_plane(self):
        args=text_review()
        cases=[{'value':True}, {'evidence_plane':'saved_file'},
               {'expression':'2+3'}, {'kind':'state'}, {'kind':'calculation'}]
        for changes in cases:
            with self.subTest(changes=changes):
                altered=deepcopy(args);altered['goals'][0].update(changes)
                self.refused('locua_review',altered)
        args['goals'][0]={'id':'total','kind':'calculation','target':'Reviewed display',
                         'control_id':'capture-17:9','expression':'192 * 231 - 100','evidence_plane':'display'}
        args['effects']=[{'kind':'goal','goal_id':'total'}]
        self.accepted('locua_review',args)

    def test_review_goal_references_and_ids_do_not_silently_drift(self):
        args=text_review();args['effects'][0]['goal_id']='foreign'
        self.assertIn('unbound_goal',[e['code'] for e in self.refused('locua_review',args)['errors']])
        args=text_review();args['goals'].append(deepcopy(args['goals'][0]))
        self.assertIn('duplicate_goal',[e['code'] for e in self.refused('locua_review',args)['errors']])

    def test_preserves_require_typed_observed_predicates(self):
        args=text_review()
        args['preserves']=[{'control_id':'other:1','property':'checked','value':False},
                           {'control_id':'other:2','property':'value','value':'  protected\n'}]
        self.accepted('locua_review',args)
        for prop,value in [('checked',0),('selected','false'),('value',False),('saved_file','x')]:
            args['preserves']=[{'control_id':'other:1','property':prop,'value':value}]
            self.refused('locua_review',args)

    def test_only_three_inspection_operations_published_or_accepted(self):
        self.assertEqual({b['properties']['operation']['const'] for b in INSPECT_SCHEMA['oneOf']},
                         {'overview','list','control'})
        for operation in ('all','region','search','roles','execute'):
            result=self.refused('locua_inspect',{'snapshot_id':'s','operation':operation})
            self.assertIn('Supported operations: overview, list, control',result['reason'])

    def test_all_supported_inspection_routes_and_cursor_are_lossless(self):
        cases=[{'operation':'overview','cursor':'bound-token','limit':3},
               {'operation':'list','region_id':'r','role':'AXTextArea','query':'Draft',
                'cursor':'bound-token','limit':128},
               {'operation':'control','control_id':'s:2','cursor':'detail-fragment-token'}]
        for fields in cases:
            self.accepted('locua_inspect',{'snapshot_id':'s',**fields})
        for query in ('','x'*1025):
            self.refused('locua_inspect',{'snapshot_id':'s','operation':'list','query':query})

    def test_operation_specific_ignored_fields_are_rejected(self):
        for operation, extra in (
            ('overview',{'control_id':'s:1'}),('overview',{'query':'Draft'}),
            ('overview',{'region_id':'r'}),('overview',{'role':'AXTextField'}),
            ('list',{'control_id':'s:1'}),('list',{'start':0}),
            ('control',{'limit':1}),('control',{'region_id':'r'}),
            ('control',{'query':'Draft'}),('control',{'role':'AXTextField'})):
            with self.subTest(operation=operation,extra=extra):
                args={'snapshot_id':'s','operation':operation,**extra}
                if operation=='control':args['control_id']='s:2'
                error=self.refused('locua_inspect',args)
                self.assertTrue(any(e['code']=='unknown_argument' for e in error['errors']))

    def test_missing_operation_control_and_unknown_keys_are_actionable(self):
        error=self.refused('locua_inspect',{'snapshot_id':'s','window_id':'w'})
        self.assertIn('operation',error['missing_required_arguments'])
        self.assertIn('window_id',error['unknown_arguments'])
        error=self.refused('locua_inspect',{'snapshot_id':'s','operation':'control'})
        self.assertIn('control_id',error['missing_required_arguments'])

    def test_needs_replace_is_bounded_and_not_a_scope_or_action(self):
        self.accepted('locua_status',{'operation':'needs','needs':[]})
        self.accepted('locua_status',{'operation':'needs','needs':['Inspect actual display.']})
        for fields in ({'needs':['x']*13},{'needs':['x'*257]},
                       {'needs':['x'],'scope_id':'scope:1'}, {'needs':['x'],'limit':1},
                       {'needs':[1]}, {'needs':['  \n']}, {'needs':['x'],'action_id':'a'}):
            self.refused('locua_status',{'operation':'needs',**fields})

    def test_status_and_verify_do_not_ignore_scope_or_all_combinations(self):
        self.accepted('locua_status',{})
        for operation in ('exploration','controls'):
            self.accepted('locua_status',{'operation':operation,'start':64,'limit':64})
            self.refused('locua_status',{'operation':operation,'scope_id':'review-1'})
        self.accepted('locua_status',{'operation':'goals','scope_id':'review-1'})
        self.refused('locua_status',{'scope_id':'review-1'})
        self.refused('locua_status',{'operation':'goals'})
        self.accepted('locua_verify',{'all':True})
        self.accepted('locua_verify',{'scope_id':'review-1','goal_id':'draft'})
        self.refused('locua_verify',{'all':True,'scope_id':'review-1'})

    def test_bools_and_extreme_numbers_do_not_pass_integer_limits(self):
        for limit in (True,False,0,129,1.0,float('nan'),10**1000):
            self.refused('locua_inspect',{'snapshot_id':'s','operation':'list','limit':limit})

    def test_sequence_is_bounded_and_rejects_ignored_fields_before_input(self):
        args={'scope_id':'scope','snapshot_id':'s','steps':[{'action_id':'a'},{'action_id':'b','value':'  Ω\\n'}]}
        self.accepted('locua_act_sequence',args)
        for steps in ([],[{'action_id':'a'}]*33,[{'action_id':'a','expected_result':'42'}],
                      [{'control_id':'c'}],[{'action_id':'a','value':True}],['a']):
            self.refused('locua_act_sequence',{**args,'steps':steps})
        self.refused('locua_act_sequence',{**args,'skip_checks':True})

    def test_nonobjects_and_unknown_tools_are_refused_without_coercion(self):
        for value in (None,[],False,'{}',{1:'invalid-key'}):
            self.refused('locua_status',value)
        for tool in ('unregistered',None,{}):self.refused(tool,{})


if __name__ == '__main__':
    unittest.main()
