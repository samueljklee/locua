"""Published schemas through the unchanged Qwen XML bridge; CPU only."""
from copy import deepcopy
import json
import unittest

from locua.amplifier_contracts import (
    ArgumentContractError, SPECS, choice, obj, validate_tool_arguments,
)
from locua.engine.prototype.qwen38_runtime import parse_tool_output


TOOLS=[{'type':'function','function':{'name':name,'description':description,'parameters':schema}}
       for name,(description,schema) in SPECS.items()]


def xml_call(name,arguments):
    parameters=[]
    for key,value in arguments.items():
        wire=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        parameters.append(f'<parameter={key}>\n{wire}\n</parameter>')
    return f'<tool_call>\n<function={name}>\n'+ '\n'.join(parameters)+'\n</function>\n</tool_call>'


def review():
    return {'snapshot_id':'s1','summary':'Change only the reviewed surfaces.',
        'goals':[
            {'id':'text','kind':'text','target':'Owned editor','control_id':'c1',
             'value':'  00017\r\nΩ  ','evidence_plane':'editor_buffer'},
            {'id':'state','kind':'state','target':'Observed checkbox','control_id':'c2',
             'value':False,'evidence_plane':'display'},
            {'id':'calc','kind':'calculation','target':'Observed display','control_id':'c3',
             'expression':'2+3','evidence_plane':'display'}],
        'effects':[{'kind':'goal','goal_id':'text'},{'kind':'goal','goal_id':'state'},
                   {'kind':'goal','goal_id':'calc'},
                   {'kind':'press','control_id':'button1','purpose':'Open observed panel'}],
        'preserves':[{'control_id':'p1','property':'value','value':'protected\n'},
                     {'control_id':'p2','property':'checked','value':True},
                     {'control_id':'p3','property':'selected','value':False}],
        'limitations':['Editor buffer only.'], 'unresolved_requirements':[],
        'covers_entire_request':True}


class PublishedWireTests(unittest.TestCase):
    def roundtrip(self,name,args):
        original=deepcopy(args)
        parsed=parse_tool_output(xml_call(name,args),TOOLS)
        self.assertEqual(parsed,[{'type':'tool_call','name':name,'arguments':args}])
        validate_tool_arguments(name,parsed[0]['arguments'])
        self.assertEqual(args,original)
        return parsed[0]['arguments']

    def test_every_registered_tool_has_unambiguous_top_level_wire_types(self):
        self.assertEqual(len(TOOLS),12)
        for name,(_description,schema) in SPECS.items():
            for key,parameter in schema['properties'].items():
                with self.subTest(tool=name,parameter=key):
                    self.assertIsInstance(parameter.get('type'),str)
                    self.assertIn(parameter['type'],{'string','integer','number','boolean','object','array','null'})

    def test_all_registered_tools_roundtrip_and_cover_every_wire_parameter(self):
        cases={
            'locua_apps':[{'query':'surface','start':0,'limit':4,'inventory_id':'inventory1'}],
            'locua_windows':[{'app_id':'app1'}],
            'locua_launch':[{'app_id':'app1'}],
            'locua_activate':[{'window_id':'window1'}],
            'locua_observe':[{'window_id':'window1'}],
            'locua_status':[{'operation':'goals','scope_id':'scope1','start':0,'limit':4},
                            {'operation':'needs','needs':['Read actual target.']}],
            'locua_inspect':[{'operation':'list','snapshot_id':'s1','region_id':'r1','role':'AXButton',
                             'query':'Choice','cursor':'cursor1','limit':128},
                            {'operation':'control','snapshot_id':'s1','control_id':'c1','cursor':'part1'}],
            'locua_review':[review()],
            'locua_act':[{'scope_id':'scope1','snapshot_id':'s1','action_id':'a1','value':'  text\r\nΩ  '}],
            'locua_act_sequence':[{'scope_id':'scope1','snapshot_id':'s1','steps':[{'action_id':'a1'},{'action_id':'a2','value':'  text\r\nΩ  '}]}],
            'locua_verify':[{'scope_id':'scope1','goal_id':'text','all':False},{'all':True},
                            {'scope_id':'scope1','reconcile':True},
                            {'scope_id':'scope1','goal_id':'calc','snapshot_id':'s2','control_id':'result2'}],
            'locua_clarify':[{'question':'Which observed target?','reason':'Two captured choices match.'}],
        }
        self.assertEqual(set(cases),set(SPECS))
        for name,variants in cases.items():
            seen=set()
            for args in variants:
                with self.subTest(tool=name,args=args):self.roundtrip(name,args)
                seen.update(args)
            self.assertEqual(seen,set(SPECS[name][1]['properties']),name)

    def test_every_inspect_and_status_operation_branch_roundtrips(self):
        for operation in ('overview','list','control'):
            args={'operation':operation,'snapshot_id':'s1','cursor':'cursor1'}
            if operation=='control':args['control_id']='c1'
            else:args['limit']=128
            self.roundtrip('locua_inspect',args)
        self.roundtrip('locua_status',{})
        for operation in ('summary','windows','scopes','clarifications','exploration','controls'):
            self.roundtrip('locua_status',{'operation':operation,'start':0,'limit':64})
        for operation in ('goals','effects','preserves','witness'):
            self.roundtrip('locua_status',{'operation':operation,'scope_id':'scope1','start':0,'limit':64})
        self.roundtrip('locua_status',{'operation':'needs','needs':[]})

    def test_exact_retained_call005_is_valid_and_integer_stays_integer(self):
        # Exact raw response from do-3526997d807e/provider/call-005-raw.json.
        raw='<tool_call>\n<function=locua_inspect>\n<parameter=operation>\nlist\n</parameter>\n<parameter=snapshot_id>\ns00000049\n</parameter>\n<parameter=region_id>\nregion:1c42d8c779124ab790f4:0\n</parameter>\n<parameter=role>\nAXButton\n</parameter>\n<parameter=limit>\n128\n</parameter>\n</function>\n</tool_call>'
        args=parse_tool_output(raw,TOOLS)[0]['arguments']
        self.assertEqual(args,{'operation':'list','snapshot_id':'s00000049',
            'region_id':'region:1c42d8c779124ab790f4:0','role':'AXButton','limit':128})
        self.assertIs(type(args['limit']),int)
        validate_tool_arguments('locua_inspect',args)

    def test_oneof_branch_constraints_survive_typed_transport(self):
        invalid=[('locua_inspect',{'operation':'overview','snapshot_id':'s1','region_id':'r1'}),
                 ('locua_inspect',{'operation':'list','snapshot_id':'s1','limit':129}),
                 ('locua_inspect',{'operation':'unknown','snapshot_id':'s1'}),
                 ('locua_verify',{'all':True,'scope_id':'scope1'}),
                 ('locua_status',{'operation':'needs','needs':[],'scope_id':'scope1'})]
        for name,args in invalid:
            with self.subTest(tool=name,args=args):
                parsed=parse_tool_output(xml_call(name,args),TOOLS)[0]['arguments']
                self.assertEqual(parsed,args)
                with self.assertRaises(ArgumentContractError):validate_tool_arguments(name,parsed)
        args=review();args['effects'][0]['control_id']='unexpected'
        parsed=parse_tool_output(xml_call('locua_review',args),TOOLS)[0]['arguments']
        with self.assertRaises(ArgumentContractError):validate_tool_arguments('locua_review',parsed)
        args=review();args['goals'][1]['value']='false'
        parsed=parse_tool_output(xml_call('locua_review',args),TOOLS)[0]['arguments']
        self.assertIs(type(parsed['goals'][1]['value']),str)
        with self.assertRaises(ArgumentContractError):validate_tool_arguments('locua_review',parsed)

    def test_schema_authored_literals_determine_types_not_generated_lexemes(self):
        schemas=choice([obj({'mode':{'const':'true'},'flag':{'const':True}},('mode','flag')),
                        obj({'mode':{'enum':['false','null']},'flag':{'const':False}},('mode','flag'))])
        tools=[{'type':'function','function':{'name':'example','parameters':schemas}}]
        parsed=parse_tool_output(xml_call('example',{'mode':'true','flag':True}),tools)[0]['arguments']
        self.assertEqual(parsed,{'mode':'true','flag':True})
        self.assertIs(type(parsed['mode']),str);self.assertIs(type(parsed['flag']),bool)

    def test_mixed_or_unspecified_wire_types_still_refuse_without_guessing(self):
        for variants in ([obj({'x':{'const':'true'}}),obj({'x':{'const':True}})],
                         [obj({'x':{'const':False}}),obj({'x':{'const':0}})],
                         [obj({'x':{'type':'string'}}),obj({'x':{}})]):
            schema=choice(variants)
            self.assertEqual(schema['oneOf'],variants)
            self.assertNotIn('const',schema['properties']['x'])
            tools=[{'type':'function','function':{'name':'example','parameters':schema}}]
            with self.assertRaisesRegex(ValueError,'Ambiguous/unspecified'):
                parse_tool_output(xml_call('example',{'x':'true'}),tools)

    def test_duplicate_parameters_and_wrong_native_scalar_types_still_refuse(self):
        for raw in (
            xml_call('locua_verify',{'all':'yes'}),
            xml_call('locua_inspect',{'snapshot_id':'s1','operation':'list','limit':'128.0'}),
            xml_call('locua_inspect',{'snapshot_id':'s1','operation':'list','limit':True}),
            xml_call('locua_status',{'needs':[]} ).replace('</function>',
                '<parameter=needs>\n[]\n</parameter>\n</function>'),
        ):
            with self.assertRaises(ValueError):parse_tool_output(raw,TOOLS)


if __name__=='__main__':unittest.main()
