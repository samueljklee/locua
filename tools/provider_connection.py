"""Bounded causal tool connection through the real Amplifier loop; no desktop I/O."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import secrets
import time
import uuid

from locua.amplifier_session import execute_session
from locua.engine.prototype.cli import private_json
from locua.lib import _config

SYSTEM = ('Use the supplied tools to fulfil the user request. Make one tool call at a time. '
          'Wait for its actual result before choosing the next action. Never invent a tool result '
          'or an identifier. Report the actual outcome.')


def make_provider(provider, model, out, *, config=None, thinking=False, budget_ledger=None, max_calls=48):
    if provider == 'local':
        from locua.amplifier_provider import LocalAmplifierProvider
        return LocalAmplifierProvider(model=model, runtime_config=_config(config), out=out,
            max_calls=max_calls, qwen38_thinking=thinking,
            qwen38_prompt_cache=model=='qwen38' and not thinking,
            qwen38_stable_prefix_cache=model=='qwen38' and not thinking)
    from locua.provider_selection import HostedAmplifierProvider
    if thinking: raise ValueError('Hosted reasoning is declared by the frozen provider configuration')
    return HostedAmplifierProvider(provider,model,out=out,budget_path=budget_ledger,max_calls=max_calls)


async def run(out, *, provider='local', model='qwen38', thinking=False, config=None, budget_ledger=None):
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    source=Path(__file__).resolve().parents[1]/'artifacts/amplifier-v10-freeze/nonce-record-smoke/request.json'
    spec=json.loads(source.read_text());private_json(root/'request.json',spec)
    backend=make_provider(provider,model,root/'provider',config=config,thinking=thinking,budget_ledger=budget_ledger,max_calls=6)
    state={};events=[];started=time.monotonic()
    class Tool:
        def __init__(self,spec):
            self.name=spec['name'];self.description=spec['description'];self.input_schema=spec['parameters']
        async def execute(self,args):
            from amplifier_core.models import ToolResult
            if self.name=='read_sample':
                if args or 'sample' in state:output={'status':'refused','reason':'Read once with empty arguments'}
                else:
                    sample={'sample_id':str(uuid.uuid4()),'nonce':str(uuid.uuid4()),'count':10+secrets.randbelow(81)}
                    state['sample']=sample;private_json(root/'oracle.json',sample);output={'status':'ok',**sample}
            else:
                sample=state.get('sample',{})
                valid=(set(args)=={'sample_id','nonce','value'} and sample and
                    args['sample_id']==sample['sample_id'] and args['nonce']==sample['nonce'] and
                    type(args['value']) is int and args['value']==sample['count']+7 and 'receipt' not in state)
                if valid:
                    receipt={'receipt_id':str(uuid.uuid4()),**args};state['receipt']=receipt
                    private_json(root/'receipt.json',receipt);output={'status':'recorded',**receipt}
                else:output={'status':'refused','reason':'Arguments do not match the single read sample'}
            events.append({'name':self.name,'arguments':deepcopy(args),'result':deepcopy(output)})
            private_json(root/f'tool-event-{len(events):03d}.json',events[-1])
            return ToolResult(success=output['status']!='refused',output=output)
    report={'provider':provider,'model':model,'thinking':thinking,'rlcd_used':False,'desktop_calls':0,'human_assistance':0}
    try:
        result=await asyncio.wait_for(execute_session(spec['request'],backend,[Tool(t) for t in spec['tools']],
            out=root/'session',system=SYSTEM,max_iterations=6,progress=lambda s:print(s,flush=True)),400)
        sample=state.get('sample',{});receipt=state.get('receipt',{})
        requests=[r.get('request',r.get('native_request',{})) for r in backend.records]
        checks={'actual_tools_executed':[e['name'] for e in events]==['read_sample','record_sample'],
            'receipt_matches_independent_sample':bool(sample and receipt and receipt['sample_id']==sample['sample_id']
                and receipt['nonce']==sample['nonce'] and receipt['value']==sample['count']+7),
            'nonce_not_in_initial_request':bool(sample and requests and sample['nonce'] not in json.dumps(requests[0],default=str)),
            'nonce_consumed_in_later_request':bool(sample and any(sample['nonce'] in json.dumps(r,default=str) for r in requests[1:])),
            'session_closed':result.get('session_cleanup')=='closed'}
        report.update(status='passed' if all(checks.values()) else 'failed',checks=checks,assistant_response=result.get('response'))
    except BaseException as error:
        report.update(status='failed',failure_category='provider integration or bounded configuration; inspect actual returned calls',error=type(error).__name__+': '+str(error))
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
    finally:
        await backend.close();report['provider_closed']=backend._closed
        report['returned_responses']=sum(r.get('status')=='completed' for r in backend.records)
        generations=[r['generation'] for r in backend.records if 'generation' in r]
        report['known_usage']={k:sum(g.get('usage',{}).get(k,0) for g in generations) for k in ('input_tokens','output_tokens')}
        report['known_generation_s']=sum(g.get('timing',{}).get('generation_ms',0) for g in generations)/1000
        report['tool_calls']=len(events);report['wall_s']=time.monotonic()-started
        if provider!='local':report.update(provider_configuration=backend.metadata,api_cost=backend.cost_report())
        private_json(root/'summary.json',report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('provider_configuration','assistant_response')},indent=2),flush=True)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--provider',choices=('local','openai','anthropic'),default='local')
    p.add_argument('--model',default='qwen38');p.add_argument('--thinking',action='store_true');p.add_argument('--out',required=True)
    p.add_argument('--config');p.add_argument('--budget-ledger');a=p.parse_args()
    r=asyncio.run(run(a.out,provider=a.provider,model=a.model,thinking=a.thinking,config=a.config,budget_ledger=a.budget_ledger))
    raise SystemExit(0 if r['status']=='passed' else 1)
