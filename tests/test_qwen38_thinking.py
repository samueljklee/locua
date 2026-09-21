"""Bounded same-model thinking mode, CPU doubles only; no desktop/model load."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

from locua import amplifier_provider as provider
from locua.engine.prototype.qwen38_runtime import (DECODING,THINKING_DECODING,THINKING_REDACTION,
    PARSER,Qwen38Runtime,thinking_configuration,redact_thinking_output,parse_tool_output)
from locua.engine.prototype.tool_chat_worker import generate_chat,count_chat,serve
from locua.engine.prototype.decision import ProtocolError

SECRET='PRIVATE_SYNTHETIC_REASONING_NOT_FOR_LOGS'
FINAL='<tool_call>\n<function=sample>\n<parameter=text>\n exact literal \n</parameter>\n</function>\n</tool_call>'
TOOLS=[{'type':'function','function':{'name':'sample','parameters':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}}]
MESSAGES=[{'role':'user','content':'Read the current sample.'}]


class Runtime:
    model_key='qwen38';decoding=THINKING_DECODING;enable_thinking=True
    template_kwargs={'enable_thinking':True,'preserve_thinking':False}
    def __init__(self,raw=None,reason='stop'):
        self.raw=SECRET+'</think>\n\n'+FINAL if raw is None else raw;self.reason=reason;self.calls=0
        self.tokenizer=Mock();self.tokenizer.apply_chat_template.return_value=[1,2,3,4]
    def info(self):return {'test_double':True,'enable_thinking':self.enable_thinking,'decoder':self.decoding}
    def clear_idle_cache(self):pass
    def stream(self,*args,**kwargs):
        self.calls+=1;kwargs['check_deadline']()
        middle=len(self.raw)//2
        yield types.SimpleNamespace(text=self.raw[:middle],generation_tokens=6,finish_reason=None)
        yield types.SimpleNamespace(text=self.raw[middle:],generation_tokens=12,finish_reason=self.reason)


class ThinkingConfigurationTests(unittest.TestCase):
    def test_default_unchanged_and_explicit_mode_preserves_same_model_limits(self):
        self.assertEqual(thinking_configuration(False),{'decoding':DECODING,'template_kwargs':{'enable_thinking':False,'preserve_thinking':False}})
        self.assertEqual(provider.decoding_for('qwen38'),DECODING)
        self.assertEqual(provider.decoding_for('qwen38',thinking=True),THINKING_DECODING)
        self.assertEqual(provider.model_limits('qwen38'),{'max_output_tokens':2048,'generation_timeout_s':120,'memory_limit_bytes':32*1024**3})
        self.assertEqual(Qwen38Runtime.template_kwargs,{'enable_thinking':False,'preserve_thinking':False})

    def test_wrong_model_nonboolean_and_unvalidated_cache_refuse_before_process(self):
        with patch('locua.amplifier_provider.subprocess.Popen') as popen:
            for model,kw in [('baseline',{'qwen38_thinking':True}),('comparator',{'qwen38_thinking':True}),
                             ('qwen38',{'qwen38_thinking':1}),('qwen38',{'qwen38_thinking':True,'qwen38_prompt_cache':True}),
                             ('qwen38',{'qwen38_thinking':True,'qwen38_prompt_cache':True,'qwen38_stable_prefix_cache':True})]:
                with self.subTest(model=model,kw=kw),self.assertRaises(ValueError):provider.ToolChatService(model,**kw)
                with self.assertRaises(ValueError):provider.LocalAmplifierProvider(model=model,**kw)
            popen.assert_not_called()
        for flag in (0,1,None,'true'):
            with self.assertRaises(ValueError):thinking_configuration(flag)
        with self.assertRaises(ValueError):thinking_configuration(True,prompt_cache_enabled=True)

    def test_worker_identity_refuses_wrong_thinking_mode_or_history_policy(self):
        service=provider.ToolChatService.__new__(provider.ToolChatService)
        service.model_key='qwen38';service.model_pin=provider.native_model_pins()['qwen38']
        service.qwen38_prompt_cache=False;service.qwen38_stable_prefix_cache=False;service.qwen38_thinking=True
        worker=Path(provider.__file__).parent/'engine/prototype/tool_chat_worker.py'
        info={'service':provider.VERSION,'decoder':THINKING_DECODING,'model_key':'qwen38','model_pin':service.model_pin,
            'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(),'provider_sha256':hashlib.sha256(Path(provider.__file__).read_bytes()).hexdigest(),
            'runtime_sha256':hashlib.sha256(worker.with_name('qwen38_runtime.py').read_bytes()).hexdigest(),
            'offline_libraries':True,'logits_constraint':'none','trust_remote_code':False,'enable_thinking':True,
            'template_kwargs':{'enable_thinking':True,'preserve_thinking':False},'text_only':True,'tool_parser':PARSER,
            'prompt_cache_policy':'off','volatile_suffix_checkpoint_enabled':False}
        service._validate_info(info)
        for changes in ({'enable_thinking':False},{'decoder':DECODING},{'template_kwargs':{'enable_thinking':True,'preserve_thinking':True}}):
            with self.assertRaises(ProtocolError):service._validate_info({**info,**changes})


class ThinkingWorkerTests(unittest.TestCase):
    def test_count_and_generation_use_same_enabled_template_without_reasoning_history(self):
        runtime=Runtime();count=count_chat(runtime,MESSAGES,TOOLS)
        self.assertEqual(runtime.calls,0)
        generated=generate_chat(runtime,MESSAGES,TOOLS,max_output_tokens=2048,generation_timeout_s=120)
        calls=runtime.tokenizer.apply_chat_template.call_args_list
        self.assertEqual(calls[0],calls[1]);self.assertTrue(calls[0].kwargs['enable_thinking']);self.assertFalse(calls[0].kwargs['preserve_thinking'])
        self.assertEqual(count['input_tokens'],generated['usage']['input_tokens'])
        self.assertEqual(generated['usage']['output_tokens'],12)
        self.assertEqual(generated['reasoning_redaction']['token_accounting'],'usage.output_tokens includes reasoning, delimiter and final output')

    def test_reasoning_removed_before_ipc_and_final_exact_tool_value_preserved(self):
        runtime=Runtime();request={'id':'1','op':'complete','messages':MESSAGES,'tools':TOOLS,'max_output_tokens':2048,'generation_timeout_s':120}
        output=io.StringIO();serve(runtime,io.StringIO(json.dumps(request)+'\n'),output,max_output_tokens=2048,generation_timeout_s=120)
        self.assertNotIn(SECRET,output.getvalue())
        result=json.loads(output.getvalue().splitlines()[1])['completion']
        self.assertEqual(result['raw_output'],'\n\n'+FINAL)
        blocks=parse_tool_output(result['raw_output'],TOOLS,result['finish_reason'])
        self.assertEqual(blocks[-1]['arguments']['text'],' exact literal ')
        self.assertEqual(result['reasoning_redaction']['status'],'closed')
        self.assertFalse(result['reasoning_redaction']['reasoning_text_recorded'])
        self.assertEqual(result['reasoning_redaction']['reasoning_sha256'],hashlib.sha256(SECRET.encode()).hexdigest())

    def test_tools_inside_private_reasoning_never_become_calls(self):
        final,metadata=redact_thinking_output(SECRET+FINAL+'</think>Observed result.')
        self.assertEqual(parse_tool_output(final,TOOLS),[{'type':'text','text':'Observed result.'}])
        self.assertEqual(metadata['status'],'closed')

    def test_missing_nested_or_multiple_reasoning_boundaries_redact_everything(self):
        for raw in (SECRET,SECRET+FINAL,SECRET+'</think>'+FINAL+'</think>', '<think>'+SECRET+'</think>'+FINAL):
            with self.subTest(raw=raw):
                result=generate_chat(Runtime(raw),MESSAGES,TOOLS,max_output_tokens=2048,generation_timeout_s=120)
                self.assertEqual(result['raw_output'],'');self.assertEqual(result['finish_reason'],'error')
                self.assertEqual(result['generation_error']['type'],'ThinkingBoundaryError')
                self.assertNotIn(SECRET,json.dumps(result))

    def test_output_exhaustion_is_configuration_failure_not_repaired_tool(self):
        for raw in (SECRET,SECRET+'</think>'+FINAL):
            with self.subTest(raw=raw):
                result=generate_chat(Runtime(raw,'length'),MESSAGES,TOOLS,max_output_tokens=12,generation_timeout_s=120)
                self.assertEqual(result['finish_reason'],'length');self.assertEqual(result['usage']['output_tokens'],12)
                self.assertEqual(result['generation_error']['type'],'ThinkingOutputBudgetReached')
                self.assertTrue(result['reasoning_redaction']['output_token_budget_exhausted'])
                self.assertNotIn(SECRET,json.dumps(result))
                with self.assertRaises(ValueError):parse_tool_output(result['raw_output'],TOOLS,'length')

    def test_strict_final_xml_parser_is_not_relaxed(self):
        for final in ('<tool_call><function=wrong></function></tool_call>',FINAL.replace(' exact literal ','x</parameter>junk')):
            visible,_=redact_thinking_output(SECRET+'</think>'+final)
            with self.assertRaises(ValueError):parse_tool_output(visible,TOOLS)
        with self.assertRaisesRegex(ValueError,'Unexpected thinking'):parse_tool_output('<think>'+SECRET+'</think>'+FINAL,TOOLS)

    def test_nonthinking_generation_raw_bytes_unchanged(self):
        runtime=Runtime(FINAL);runtime.enable_thinking=False;runtime.decoding=DECODING
        runtime.template_kwargs={'enable_thinking':False,'preserve_thinking':False}
        result=generate_chat(runtime,MESSAGES,TOOLS,max_output_tokens=2048,generation_timeout_s=120)
        self.assertEqual(result['raw_output'],FINAL);self.assertNotIn('reasoning_redaction',result)
        self.assertEqual(result['decoding'],DECODING)


class ThinkingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_only_response_and_artifacts_no_private_reasoning(self):
        result=generate_chat(Runtime(),MESSAGES,TOOLS,max_output_tokens=2048,generation_timeout_s=120);result['request_id']='test-request'
        service=Mock();service.info.return_value={'decoder':THINKING_DECODING};service.generate.return_value=result
        request={'messages':MESSAGES,'tools':[{'name':'sample','parameters':TOOLS[0]['function']['parameters']}]}
        with tempfile.TemporaryDirectory() as tmp,patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()):
            out=Path(tmp)/'provider';factory=Mock(return_value=service)
            p=provider.LocalAmplifierProvider(model='qwen38',qwen38_thinking=True,out=out,service_factory=factory)
            response=await p.complete(request)
            self.assertTrue(factory.call_args.kwargs['qwen38_thinking'])
            self.assertEqual(response.tool_calls[0].arguments,{'text':' exact literal '})
            self.assertEqual(response.metadata['decoding'],THINKING_DECODING)
            self.assertNotIn(SECRET,response.model_dump_json())
            self.assertNotIn(SECRET,json.dumps(p.records))
            for path in out.glob('*.json'):self.assertNotIn(SECRET,path.read_text())
            await p.close()

    async def test_exhausted_budget_records_complete_usage_without_releasing_calls(self):
        result=generate_chat(Runtime(SECRET,'length'),MESSAGES,TOOLS,max_output_tokens=12,generation_timeout_s=120);result['request_id']='test-request'
        service=Mock();service.info.return_value={};service.generate.return_value=result
        with patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()):
            p=provider.LocalAmplifierProvider(model='qwen38',qwen38_thinking=True,service_factory=lambda **kw:service)
            with self.assertRaisesRegex(ValueError,'thinking output token budget exhausted'):
                await p.complete({'messages':MESSAGES,'tools':[{'name':'sample','parameters':TOOLS[0]['function']['parameters']}]})
            self.assertEqual(p.records[0]['complete_generation_count'],1)
            self.assertEqual(p.records[0]['generation']['usage']['output_tokens'],12)
            await p.close()

if __name__=='__main__':unittest.main()
