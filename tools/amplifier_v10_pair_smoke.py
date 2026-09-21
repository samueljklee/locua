"""Run the two original frozen read-count/arithmetic tool-loop smokes."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import time

from locua.amplifier_provider import LocalAmplifierProvider
from locua.amplifier_session import execute_session
from locua.engine.prototype.cli import private_json
from locua.goal_planner import verify_arithmetic
from locua.lib import _config


async def one(root,case):
    from amplifier_core.models import ToolResult
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    started=time.monotonic();events=[];count=10+secrets.randbelow(81)
    unavailable=case['id']=='tool-error-no-success'
    private_json(root/'oracle.json',{'count':count,'unavailable':unavailable})
    private_json(root/'frozen-request.json',case)
    class Read:
        name='read_sample_count';description='Read the current local sample count. May return an unavailable error.'
        input_schema={'type':'object','properties':{},'additionalProperties':False}
        async def execute(self,args):
            if args:output={'status':'refused','reason':'No arguments accepted'}
            elif unavailable:output={'status':'unavailable','reason':'The local sample source is unavailable; no count was read.'}
            else:output={'status':'ok','count':count}
            events.append({'tool':self.name,'arguments':args,'result':output})
            return ToolResult(success=output['status']=='ok',output=output)
    class Calculate:
        name='calculate';description='Evaluate a supplied arithmetic expression locally. Does not read sample data.'
        input_schema={'type':'object','properties':{'expression':{'type':'string'}},'required':['expression'],'additionalProperties':False}
        async def execute(self,args):
            try:
                if set(args)!={'expression'}:raise ValueError('Supply only expression')
                value=verify_arithmetic(args['expression'])
                output={'status':'ok','calculation':value}
            except Exception as error:output={'status':'refused','reason':str(error)}
            events.append({'tool':self.name,'arguments':args,'result':output})
            return ToolResult(success=output['status']=='ok',output=output)
    provider=LocalAmplifierProvider(model='comparator',runtime_config=_config(None),out=root/'provider',max_calls=6)
    report={'case':case['id'],'rlcd_used':False,'desktop_calls':0}
    try:
        result=await asyncio.wait_for(execute_session(case['request'],provider,[Read(),Calculate()],out=root/'session',
            system='Use the supplied local tools to fulfil the user request. Make one tool call at a time. Wait for its actual result before choosing the next action. Never invent a tool result or an identifier. Report the actual outcome.',
            max_iterations=5,progress=lambda s:print(s,flush=True)),120)
        report['assistant_response']=result['response'];report['session_cleanup']=result['session_cleanup']
        names=[e['tool'] for e in events]
        if unavailable:
            passed=names==['read_sample_count']
        else:
            # Evaluator calculates from the private oracle, independently of
            # model text and the calculation tool's claimed success.
            calculated=events[-1].get('result',{}).get('calculation',{}) if events else {}
            passed=(names==['read_sample_count','calculate'] and calculated.get('numerator')==count+7
                and calculated.get('denominator')==1)
        report['tool_behavior_passed']=passed
        report['final_semantics_requires_manager_audit']=True
    except Exception as error:report.update(tool_behavior_passed=False,error=type(error).__name__+': '+str(error))
    finally:
        await provider.close();private_json(root/'tool-events.json',events)
        report['provider_closed']=provider._closed and (provider._service is None or provider._service.closed)
        report['model_calls']=sum(r.get('complete_generation_count') or 0 for r in provider.records)
        report['input_tokens']=sum(r.get('generation',{}).get('usage',{}).get('input_tokens',0) for r in provider.records)
        report['output_tokens']=sum(r.get('generation',{}).get('usage',{}).get('output_tokens',0) for r in provider.records)
        report['wall_s']=time.monotonic()-started
        private_json(root/'summary.json',report);print(json.dumps(report,indent=2),flush=True)


async def main(out):
    source=Path(__file__).resolve().parents[1]/'artifacts/amplifier-v10-freeze/requests.json'
    for case in json.loads(source.read_text())['tool_smoke_requests']:await one(Path(out)/case['id'],case)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True)
    asyncio.run(main(parser.parse_args().out))
