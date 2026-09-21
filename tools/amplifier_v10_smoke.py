"""Frozen causal smoke with the real local provider and standard Amplifier loop."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import secrets
import time
import uuid

from locua.amplifier_provider import LocalAmplifierProvider
from locua.amplifier_session import execute_session
from locua.engine.prototype.cli import private_json
from locua.lib import _config


async def smoke(out, config=None, model='comparator', qwen38_prompt_cache=False):
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    frozen=Path(__file__).resolve().parents[1]/'artifacts/amplifier-v10-freeze/nonce-record-smoke'
    request=json.loads((frozen/'request.json').read_text())
    protocol=json.loads((frozen/'protocol.json').read_text())
    private_json(root/'frozen-request.json',request)
    private_json(root/'frozen-protocol.json',protocol)
    state={};events=[];started=time.monotonic()
    provider=LocalAmplifierProvider(model=model,runtime_config=_config(config),out=root/'provider',max_calls=4,
        qwen38_prompt_cache=qwen38_prompt_cache)

    class Tool:
        def __init__(self,spec):
            self.name=spec['name'];self.description=spec['description'];self.input_schema=spec['parameters']
        async def execute(self,args):
            from amplifier_core.models import ToolResult
            event={'name':self.name,'arguments':deepcopy(args),'at_ns':time.time_ns()}
            events.append(event)
            if self.name=='read_sample':
                if args or 'sample' in state:
                    output={'status':'refused','reason':'Duplicate read or unexpected arguments'}
                else:
                    sample={'sample_id':str(uuid.uuid4()),'nonce':str(uuid.uuid4()),'count':10+secrets.randbelow(81)}
                    state['sample']=sample;private_json(root/'private-oracle.json',sample)
                    output={'status':'ok',**sample}
            else:
                sample=state.get('sample',{})
                valid=(set(args)=={'sample_id','nonce','value'} and sample
                    and args['sample_id']==sample['sample_id'] and args['nonce']==sample['nonce']
                    and type(args['value']) is int and args['value']==sample['count']+7
                    and 'receipt' not in state)
                if valid:
                    receipt={'receipt_id':str(uuid.uuid4()),**args}
                    state['receipt']=receipt;private_json(root/'private-receipt.json',receipt)
                    output={'status':'recorded',**receipt}
                else:output={'status':'refused','reason':'Arguments do not match the single read sample or receipt already exists'}
            event['result']=output;private_json(root/f'tool-event-{len(events):03d}.json',event)
            return ToolResult(success=output['status']!='refused',output=output)

    report={'status':'started','model':model,'rlcd_used':False,'desktop_calls':0,'network_calls':0,
            'qwen38_prompt_cache':qwen38_prompt_cache}
    try:
        result=await asyncio.wait_for(execute_session(request['request'],provider,[Tool(x) for x in request['tools']],
            out=root/'session',system='Use the supplied local tools to fulfil the user request. Make one tool call at a time. Wait for its actual result before choosing the next action. Never invent a tool result or an identifier. Report the actual outcome.',
            max_iterations=protocol['bounds']['max_model_turns'],progress=lambda s:print(s,flush=True)),120)
        report['assistant_response']=result['response'];report['session_cleanup']=result['session_cleanup']
        sample=json.loads((root/'private-oracle.json').read_text()) if (root/'private-oracle.json').exists() else {}
        receipt=json.loads((root/'private-receipt.json').read_text()) if (root/'private-receipt.json').exists() else {}
        calls=provider.records
        tool_names=[[b['name'] for b in r.get('parsed_blocks',[]) if b['type']=='tool_call'] for r in calls]
        checks={
            'exact_call_sequence':tool_names==[['read_sample'],['record_sample'],[]],
            'exact_tool_sequence':[e['name'] for e in events]==['read_sample','record_sample'],
            'independent_receipt_match':bool(sample and receipt and receipt.get('sample_id')==sample['sample_id']
                and receipt.get('nonce')==sample['nonce'] and receipt.get('value')==sample['count']+7),
            'fresh_values_absent_initial_prompt':bool(sample) and all(sample[k] not in json.dumps(calls[0]['native_request']) for k in ('sample_id','nonce')),
            'read_result_in_second_request':len(calls)>=2 and sample.get('nonce','MISSING') in json.dumps(calls[1]['native_request']),
            'receipt_in_final_request':len(calls)>=3 and receipt.get('receipt_id','MISSING') in json.dumps(calls[2]['native_request']),
            'session_closed':result['session_cleanup']=='closed',
        }
        report.update(checks=checks,status='verified_tool_chain' if all(checks.values()) else 'failed',
            final_response_semantics='Manager must review final text against receipt; no string heuristic is completion authority.')
    except Exception as error:report.update(status='failed',error=type(error).__name__+': '+str(error))
    finally:
        await provider.close()
        report['provider_closed']=provider._closed and (provider._service is None or provider._service.closed)
        report['model_calls']=len(provider.records)
        report['input_tokens']=sum(r.get('generation',{}).get('usage',{}).get('input_tokens',0) for r in provider.records)
        report['output_tokens']=sum(r.get('generation',{}).get('usage',{}).get('output_tokens',0) for r in provider.records)
        report['wall_s']=time.monotonic()-started
        if not report['provider_closed']:report['status']='failed'
        private_json(root/'summary.json',report)
        print(json.dumps(report,indent=2),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);parser.add_argument('--config')
    parser.add_argument('--model',choices=('baseline','comparator','qwen38'),default='comparator')
    parser.add_argument('--qwen38-prompt-cache',action='store_true')
    args=parser.parse_args();result=asyncio.run(smoke(args.out,args.config,args.model,args.qwen38_prompt_cache))
    raise SystemExit(0 if result['status']=='verified_tool_chain' else 1)
