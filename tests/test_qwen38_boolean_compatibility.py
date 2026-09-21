"""CPU-only native XML boolean compatibility; no model or desktop IO."""
from copy import deepcopy
import unittest

from locua.engine.prototype.qwen38_runtime import parse_tool_output

TOOLS=[{'type':'function','function':{'name':'sample','parameters':{'type':'object',
    'properties':{'flag':{'type':'boolean'},'text':{'type':'string'},
                  'count':{'type':'integer'},'data':{'type':'object'},'items':{'type':'array'}},
    'required':[],'additionalProperties':False}}}]


def call(parameters):
    return '<tool_call>\n<function=sample>\n'+''.join(
        '<parameter='+key+'>\n'+value+'\n</parameter>\n' for key,value in parameters
    )+'</function>\n</tool_call>'


class BooleanLexemeTests(unittest.TestCase):
    def test_exact_case_insensitive_boolean_lexemes(self):
        for text,value in [('true',True),('True',True),('TRUE',True),('tRuE',True),
                           ('false',False),('False',False),('FALSE',False),('fAlSe',False)]:
            with self.subTest(text=text):
                parsed=parse_tool_output(call([('flag',text)]),TOOLS)
                self.assertIs(parsed[0]['arguments']['flag'],value)

    def test_recorded_verify_true_is_accepted_without_other_argument_changes(self):
        tools=[{'type':'function','function':{'name':'locua_verify','parameters':{
            'type':'object','properties':{'all':{'type':'boolean'}},'additionalProperties':False}}}]
        raw='<tool_call>\n<function=locua_verify>\n<parameter=all>\nTrue\n</parameter>\n</function>\n</tool_call>'
        original=deepcopy(tools)
        self.assertEqual(parse_tool_output(raw,tools),[
            {'type':'tool_call','name':'locua_verify','arguments':{'all':True}}])
        self.assertEqual(tools,original)

    def test_unknown_quoted_numeric_null_or_padded_boolean_is_refused(self):
        for text in ('yes','no','1','0','null','None','', 'truthy', 'truefalse',
                     '"true"', "'True'",' True','False ',
                     'true #comment','false or true','[true]','{}'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse_tool_output(call([('flag',text)]),TOOLS)

    def test_preexisting_json_boolean_whitespace_remains_supported(self):
        for text,value in [(' true ',True),('\tfalse',False),('true\n',True)]:
            self.assertIs(parse_tool_output(call([('flag',text)]),TOOLS)[0]['arguments']['flag'],value)

    def test_string_literals_are_byte_exact_and_other_types_stay_strict_json(self):
        for text in ('True','FALSE',' true ', '\nFalse\n'):
            self.assertEqual(parse_tool_output(call([('text',text)]),TOOLS)[0]['arguments']['text'],text)
        for name,text in [('count','True'),('count','true'),('data',"{'flag': True}"),
                          ('data','{"flag":True}'),('items','[True]')]:
            with self.subTest(name=name,text=text),self.assertRaises(ValueError):
                parse_tool_output(call([(name,text)]),TOOLS)
        self.assertEqual(parse_tool_output(call([('data','{"flag":true}')]),TOOLS)[0]['arguments']['data'],{'flag':True})

    def test_duplicates_unknown_fields_and_ambiguous_schema_remain_refused(self):
        for parameters in ([('flag','True'),('flag','False')],[('unknown','True')],
                           [('data','{"flag":true,"flag":false}')]):
            with self.subTest(parameters=parameters),self.assertRaises(ValueError):
                parse_tool_output(call(parameters),TOOLS)
        tools=deepcopy(TOOLS);tools[0]['function']['parameters']['properties']['flag']['type']=['string','boolean']
        with self.assertRaises(ValueError):parse_tool_output(call([('flag','True')]),tools)

    def test_bad_framing_and_truncated_output_remain_refused(self):
        good=call([('flag','True')])
        for raw in (good[:-5],good.replace('</parameter>','',1),good+'<parameter=extra>'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):parse_tool_output(raw,TOOLS)
        with self.assertRaises(ValueError):parse_tool_output(good,TOOLS,finish_reason='length')


if __name__=='__main__':unittest.main()
