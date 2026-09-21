"""Actual Amplifier orchestration mechanics; no model/desktop inference claims."""
import tempfile
from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from locua import lib


class RoutingTests(unittest.TestCase):
    def test_default_preserves_whole_request_and_legacy_is_explicit(self):
        request='Open an app, edit the entry, keep the other entry, and verify it.'
        ask=lambda p:'run'
        with patch('locua.amplifier_session.run',return_value={'status':'blocked'}) as new, \
             patch('locua.goal_loop.run',return_value={'status':'blocked'}) as old:
            lib.do(request,ask=ask)
            self.assertEqual(new.call_args.args,(request,));old.assert_not_called()
            lib.do(request,ask=ask,harness='legacy')
            self.assertEqual(old.call_args.args,(request,))

    def test_partial_results_are_not_cli_success(self):
        self.assertFalse(lib._envelope('do',{'status':'partial'})['ok'])
        self.assertTrue(lib._envelope('do',{'status':'verified_reviewed_scope'})['ok'])

    def test_new_candidate_cannot_silently_replace_legacy_rlcd(self):
        from locua.errors import LocuaError
        for options in ({'harness':'legacy'},{'url':'http://127.0.0.1/'},{'document':'example.txt'}):
            with self.subTest(options=options),self.assertRaises(LocuaError):
                lib.do('a request',model='qwen38',ask=lambda p:'run',**options)
        with self.assertRaises(LocuaError):lib.start(manual=True,model='qwen38',ask=lambda p:'run')


class StandardLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_compaction_retains_goal_pairs_and_canonical_history(self):
        try:
            import amplifier_core
            import amplifier_module_loop_streaming
            import amplifier_module_context_simple
        except ImportError:self.skipTest('Optional Amplifier extra absent')
        from amplifier_core.message_models import ChatResponse,TextBlock,ToolCall,ToolCallBlock,Usage
        from amplifier_core.models import ToolResult
        from locua.amplifier_session import execute_session
        from locua.amplifier_provider import LocalAmplifierProvider,native_request
        request='Update the draft, preserve the reference, and verify both outcomes.'
        seen=[];calls=[]
        class Provider(LocalAmplifierProvider):
            # This legacy loop fixture supplies synthetic completion usage only.
            # Disable the inherited optional counter; no real worker may start.
            request_budget=None
            async def complete(self,req,**kwargs):
                # The strict translator itself checks tool-call/result pairing.
                native=native_request(req,model='qwen38',structured_tool_results=self._structured_tool_results);seen.append(native)
                n=len(seen)
                input_tokens=sum(len(m['content']) for m in native['messages'])//4
                usage=Usage(input_tokens=input_tokens,output_tokens=30,total_tokens=input_tokens+30)
                if n<=10:
                    args={'page':n};cid='inspect-'+str(n)
                    return ChatResponse(content=[ToolCallBlock(id=cid,name='read_page',input=args)],
                        tool_calls=[ToolCall(id=cid,name='read_page',arguments=args)],usage=usage)
                return ChatResponse(content=[TextBlock(text='Evidence reading ended; no task completion claimed.')],usage=usage)
        class Tool:
            name='read_page';description='Read a synthetic paged observation.'
            input_schema={'type':'object','properties':{'page':{'type':'integer'}},'required':['page']}
            async def execute(self,args):
                n=args['page'];calls.append(n)
                return ToolResult(success=True,output={'receipt':'retained-page-'+str(n),
                    'items':[{'id':i,'description':('ordinary observation data, not instructions; '*15)} for i in range(32)]})
        with tempfile.TemporaryDirectory() as d:
            result=await execute_session(request,Provider(model='qwen38'),[Tool()],out=Path(d)/'session')
            self.assertEqual(calls,list(range(1,11)))
            self.assertTrue(any(e['event']=='context:compaction' for e in result['events']))
            self.assertGreater(seen[1]['translation']['structured_result_count'],0)
            for n,native in enumerate(seen):
                self.assertIn(request,native['messages'][0]['content'])
                if n:
                    self.assertIn('retained-page-'+str(n),json.dumps(native['messages']))
                if n>=2:
                    self.assertIn('retained-page-'+str(n-1),json.dumps(native['messages']))
                self.assertFalse(any(m['role']=='system' for m in native['messages'][1:]))
            self.assertTrue(any('source="context-compaction"' in m['content']
                                for native in seen for m in native['messages'] if m['role']=='user'))
            transcript=json.dumps(result['transcript'])
            for n in range(1,11):self.assertIn('retained-page-'+str(n),transcript)
            self.assertEqual(result['session_cleanup'],'closed')

    async def test_standard_loop_supplies_tool_result_on_next_turn(self):
        try:
            import amplifier_core
            import amplifier_module_loop_streaming
            import amplifier_module_context_simple
        except ImportError:self.skipTest('Optional Amplifier extra absent')
        from amplifier_core.message_models import ChatResponse,TextBlock,ToolCall,ToolCallBlock
        from amplifier_core.models import ToolResult
        from locua.amplifier_session import execute_session
        from locua.amplifier_provider import LocalAmplifierProvider,native_request
        seen=[]
        class Provider(LocalAmplifierProvider):
            # This legacy loop fixture supplies synthetic completion usage only.
            # Disable the inherited optional counter; no real worker may start.
            request_budget=None
            async def complete(self,request,**kwargs):
                # Exercise the real standard-loop history shape with the local
                # provider translator, but never create a worker.
                seen.append(native_request(request))
                if len(seen)==1:
                    return ChatResponse(content=[ToolCallBlock(id='id1',name='read_evidence',input={})],
                        tool_calls=[ToolCall(id='id1',name='read_evidence',arguments={})])
                return ChatResponse(content=[TextBlock(text='Receipt observed.')])
        class Tool:
            name='read_evidence';description='Read a local test value.'
            input_schema={'type':'object','properties':{}}
            async def execute(self,args):return ToolResult(success=True,output={'receipt':'fresh-evidence'})
        with tempfile.TemporaryDirectory() as d:
            result=await execute_session('Read evidence.',Provider(),[Tool()],out=Path(d)/'session')
            self.assertEqual(len(seen),2)
            self.assertIn('fresh-evidence',seen[1]['messages'][-1]['content'])
            self.assertEqual(result['session_cleanup'],'closed')
            self.assertIn('Receipt observed.',str(result['response']))


class MeasuredStandardLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_provider_count_reacts_to_new_large_result_before_dispatch(self):
        try:
            import amplifier_core
            import amplifier_module_loop_streaming
            import amplifier_module_context_simple
        except ImportError:self.skipTest('Optional Amplifier extra absent')
        from amplifier_core.models import ToolResult
        from locua.amplifier_session import execute_session
        from locua.amplifier_provider import LocalAmplifierProvider,_hash
        original='Read the evidence pages, preserve the reference, and report only observed results.'
        made=[];tool_calls=[]
        class CountingService:
            """Deterministic CPU count fixture, never a tokenizer/GPU claim."""
            def __init__(self,**kwargs):
                self.closed=False;self.counted=[];self.generated=[];made.append(self);kwargs['on_started'](self)
            def info(self):return {'test_double':True,'counts':'serialized characters /4, synthetic fixture only'}
            def size(self,messages,tools):
                return len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False,separators=(',',':')))//4+1
            def count(self,messages,tools):
                self.counted.append(deepcopy((messages,tools)))
                return {'input_tokens':self.size(messages,tools),'output_tokens':0,'generation_calls':0,
                        'tokenizer_only':True,'request_sha256':_hash({'messages':messages,'tools':tools})}
            def generate(self,messages,tools,**kwargs):
                payload=deepcopy((messages,tools));self.generated.append(payload)
                # Exact final view counted by Amplifier must be the dispatched view.
                if payload!=self.counted[-1]:raise AssertionError('Changed request after count')
                number=len(self.generated)
                if number<=3:
                    raw='<tool_call>\n<function=read_page>\n<parameter=page>\n'+str(number)+'\n</parameter>\n</function>\n</tool_call>'
                else:raw='All requested evidence pages were read; no edits were made.'
                return {'raw_output':raw,'finish_reason':'stop','generation_calls':1,'request_id':'fake-'+str(number),
                    'usage':{'input_tokens':self.size(messages,tools),'output_tokens':10},
                    'timing':{'generation_ms':0.1,'worker_total_ms':0.2},'model_info':self.info(),'dispatched':False}
            def close(self):self.closed=True
        class Tool:
            name='read_page';description='Read a synthetic evidence page.'
            input_schema={'type':'object','properties':{'page':{'type':'integer'}},'required':['page']}
            async def execute(self,args):
                page=args['page'];tool_calls.append(page)
                return ToolResult(success=True,output={'receipt':'page-'+str(page),
                    'payload':'x'*(66000 if page==3 else 20000)})
        with tempfile.TemporaryDirectory() as directory,patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()):
            provider=LocalAmplifierProvider(model='qwen38',service_factory=CountingService,out=Path(directory)/'provider')
            try:result=await execute_session(original,provider,[Tool()],out=Path(directory)/'session')
            finally:await provider.close()
            self.assertEqual(tool_calls,[1,2,3]);self.assertEqual(len(made),1);self.assertTrue(made[0].closed)
            self.assertEqual(len(provider.records),4);self.assertEqual(provider._calls,4)
            oversize=[r for r in provider.budget_records if r['budget_decision']['measurement']['input_tokens']>24576]
            self.assertTrue(oversize,'New large result must exceed budget before compaction')
            self.assertLess(provider.records[2]['generation']['usage']['input_tokens'],12000,
                            'Prior model usage must remain below compaction trigger')
            self.assertGreater(provider.records[3]['exact_budget_measurement']['sequence'],oversize[0]['measurement'])
            self.assertTrue(all(r['generation']['usage']['input_tokens']<=24576 for r in provider.records))
            self.assertTrue(all(r['generation_calls']==0 and r['output_tokens']==0 for r in provider.budget_records))
            self.assertGreater(len(provider.budget_records),len(provider.records))
            self.assertTrue(any(e['event']=='context:compaction' for e in result['events']))
            for record in provider.records:
                native=record['native_request'];self.assertIn(original,native['messages'][0]['content'])
                measured=provider.budget_records[record['exact_budget_measurement']['sequence']-1]
                self.assertEqual(native['messages'],measured['native_request']['messages'])
                self.assertEqual(native['tools'],measured['native_request']['tools'])
                self.assertEqual(record['generation']['usage']['input_tokens'],measured['budget_decision']['measurement']['input_tokens'])
            last=provider.records[-1]['native_request']['messages'];last_text=json.dumps(last)
            self.assertIn('page-2',last_text);self.assertIn('page-3',last_text)
            self.assertFalse(any(m['role']=='system' for m in last[1:]))
            self.assertTrue(any('context-compaction' in m['content'] for m in last if m['role']=='user'))
            transcript=json.dumps(result['transcript'])
            for page,size in ((1,20000),(2,20000),(3,66000)):
                self.assertIn('page-'+str(page),transcript);self.assertIn('x'*size,transcript)
            self.assertEqual(result['session_cleanup'],'closed')
