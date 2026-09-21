"""Explicit candidate format/integrity tests; no MLX or model inference."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

from locua.amplifier_provider import model_limits,native_model_pins,validate_limits,decoding_for,native_request
from locua.engine.prototype.qwen38_runtime import (DECODING,model_pin,verify_snapshot,parse_tool_output)
from locua.engine.prototype.tool_chat_worker import generate_chat

TOOLS=[{'type':'function','function':{'name':'sample','parameters':{'type':'object',
    'properties':{'text':{'type':'string'},'count':{'type':'integer'},'flag':{'type':'boolean'},
                  'data':{'type':'object'},'items':{'type':'array'}},'required':['text']}}}]

def call(parameters,name='sample'):
    return '<tool_call>\n<function='+name+'>\n'+''.join('<parameter='+key+'>\n'+value+'\n</parameter>\n' for key,value in parameters)+'\n</function>\n</tool_call>'

class ParserTests(unittest.TestCase):
    def test_exact_strings_types_and_multiline_arguments(self):
        text=' leading\nUnicode Ω\n\ntrailing  '
        raw=call([('text',text),('count','12'),('flag','false'),('data','{"x":true,"v":" Ω "}'),('items','[1,"two"]')])
        parsed=parse_tool_output(raw,TOOLS)
        self.assertEqual(parsed,[{'type':'tool_call','name':'sample','arguments':{
            'text':text,'count':12,'flag':False,'data':{'x':True,'v':' Ω '},'items':[1,'two']}}])

    def test_null_and_xml_entities_in_string_are_not_coerced(self):
        for text in ('null','','&amp;','\nkeep LF\n'):
            with self.subTest(text=text):self.assertEqual(parse_tool_output(call([('text',text)]),TOOLS)[0]['arguments']['text'],text)

    def test_no_python_literal_boolean_guess_duplicate_or_wrong_scalar_type(self):
        for args in ([('text','x'),('data',"{'x':1}")],[('text','x'),('flag','yes')],
                     [('text','x'),('count','1.0')],[('text','x'),('count','true')],
                     [('text','x'),('data','{"x":1,"x":2}')],
                     [('text','one'),('text','two')],[('text','x'),('unknown','y')],
                     [('text','x'),('data','{"x":NaN}')]):
            with self.subTest(args=args),self.assertRaises(ValueError):parse_tool_output(call(args),TOOLS)

    def test_unknown_name_required_omission_incomplete_and_ambiguous_markup_refuse(self):
        for raw in (call([('text','x')],name='other'),call([]),call([('text','x')])[:-5],
                    call([('text','x</parameter>bad')]),call([('text','<function=evil>')]),
                    '<think>unrequested reasoning</think>'+call([('text','x')])):
            with self.subTest(raw=raw),self.assertRaises(ValueError):parse_tool_output(raw,TOOLS)
        with self.assertRaises(ValueError):parse_tool_output(call([('text','x')]),TOOLS,'length')

    def test_unmatched_prefix_or_inter_call_markup_refuses(self):
        good=call([('text','x')])
        for bad in ('</tool_call>', '<function=x>', '</function>', '<parameter=x>', '</parameter>'):
            for raw in (bad+good, good+bad+good):
                with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'Unmatched'):
                    parse_tool_output(raw,TOOLS)

    def test_ambiguous_union_scalar_is_not_inferred(self):
        schemas=deepcopy(TOOLS);schemas[0]['function']['parameters']['properties']['text']['type']=['string','boolean']
        with self.assertRaisesRegex(ValueError,'Ambiguous'):parse_tool_output(call([('text','true')]),schemas)

    def test_plain_final_answer_and_text_before_call_preserved(self):
        self.assertEqual(parse_tool_output('Observed result.',TOOLS),[{'type':'text','text':'Observed result.'}])
        blocks=parse_tool_output('Read the current result.\n'+call([('text','x')]),TOOLS)
        self.assertEqual(blocks[0]['text'],'Read the current result.\n');self.assertEqual(blocks[1]['type'],'tool_call')

class CandidateConfigurationTests(unittest.TestCase):
    def test_only_explicit_candidate_has_larger_generation_and_memory_bounds(self):
        self.assertEqual(model_limits('comparator'),{'max_output_tokens':1024,'generation_timeout_s':60,'memory_limit_bytes':10*1024**3})
        self.assertEqual(model_limits('baseline'),model_limits('comparator'))
        self.assertEqual(model_limits('qwen38'),{'max_output_tokens':2048,'generation_timeout_s':120,'memory_limit_bytes':32*1024**3})
        validate_limits(24576,2048,120,model='qwen38')
        with self.assertRaises(ValueError):validate_limits(24576,2048,120,model='comparator')
        self.assertEqual(native_model_pins()['qwen38']['revision'],'10c35caafbb80f7dc6a7a432cdd11af10a6d4818')
        self.assertEqual(decoding_for('qwen38'),DECODING)

    def test_worker_explicitly_passes_nonthinking_template_arguments(self):
        tokenizer=Mock();tokenizer.apply_chat_template.return_value=[1,2,3]
        class Runtime:
            model_key='qwen38';decoding=DECODING;template_kwargs={'enable_thinking':False,'preserve_thinking':False}
            def stream(self,*args,**kwargs):yield types.SimpleNamespace(text='Answer',generation_tokens=1,finish_reason='stop')
            def info(self):return {'test_double':True}
            def clear_idle_cache(self):pass
        runtime=Runtime();runtime.tokenizer=tokenizer
        result=generate_chat(runtime,[{'role':'user','content':'Read the result'}],TOOLS,max_output_tokens=2048,generation_timeout_s=120)
        self.assertIs(tokenizer.apply_chat_template.call_args.kwargs['enable_thinking'],False)
        self.assertIs(tokenizer.apply_chat_template.call_args.kwargs['preserve_thinking'],False)
        self.assertEqual(result['decoding'],DECODING);self.assertEqual(result['generation_limit_seconds'],120)

    def test_late_system_role_is_refused_not_dropped_by_stricter_candidate_template(self):
        request={'messages':[{'role':'user','content':'Task'},{'role':'developer','content':'New restriction'}]}
        with self.assertRaisesRegex(ValueError,'no role history rewritten'):native_request(request,model='qwen38')
        self.assertEqual(len(native_request(request,model='comparator')['messages']),2)

class SnapshotTests(unittest.TestCase):
    def fixture(self,root,*,config_changes=None,manifest_change=None):
        pin=model_pin();snapshot=root/'hub'/('models--'+pin['model_id'].replace('/','--'))/'snapshots'/pin['revision'];snapshot.mkdir(parents=True)
        config={'model_type':'qwen3_5','text_config':{'model_type':'qwen3_5_text'}};config.update(config_changes or {})
        contents={'config.json':json.dumps(config),'tokenizer.json':'{}','tokenizer_config.json':'{}',
            'chat_template.jinja':'template','model.safetensors.index.json':json.dumps({'weight_map':{'weight':'model-1.safetensors'}}),
            'model-1.safetensors':'synthetic-nonmodel-bytes'}
        files=[]
        for name,value in contents.items():
            data=value.encode();(snapshot/name).write_bytes(data);files.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        manifest={'model_id':pin['model_id'],'revision':pin['revision'],'files':files}
        if manifest_change:manifest_change(manifest)
        manifest_path=root/'manifest.json';manifest_path.write_text(json.dumps(manifest))
        return snapshot,manifest_path

    def test_every_local_file_verified_and_no_extra_shard_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);snapshot,manifest=self.fixture(root)
            self.assertEqual(verify_snapshot(root,manifest)[0],snapshot)
            (snapshot/'model-extra.safetensors').write_bytes(b'not allowed')
            with self.assertRaisesRegex(RuntimeError,'Unexpected/missing'):verify_snapshot(root,manifest)

    def test_pin_digest_traversal_and_custom_code_loader_refuse(self):
        for mutate,config in ((lambda m:m.update(revision='wrong'),{}),
                             (lambda m:m['files'][0].update(path='../escape'),{}),
                             (lambda m:m['files'][0].update(sha256='0'*64),{}),
                             (None,{'model_file':'remote.py'})):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);_,manifest=self.fixture(root,config_changes=config,manifest_change=mutate)
                with self.assertRaises(RuntimeError):verify_snapshot(root,manifest)

if __name__=='__main__':unittest.main()
