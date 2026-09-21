"""Pure local inference replay: fixed nonce inputs, uncached then cached Qwen38.

Never dispatches tools or feeds newly generated outputs into subsequent inputs.
A passing result establishes parity only for these three retained requests.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from locua.amplifier_provider import ToolChatService
from locua.engine_adapter import runtime_environment
from locua.engine.prototype.cli import private_json
from locua.engine.prototype.qwen38_runtime import parse_tool_output
from locua.lib import _config


def run(out,source,config=None,*,service_factory=ToolChatService):
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    source=Path(source);inputs=[];sources={}
    for number in (1,2,3):
        p=source/f'call-{number:03d}-native.json';raw=p.read_bytes();native=json.loads(raw)
        if not isinstance(native.get('messages'),list) or not isinstance(native.get('tools'),list):
            raise ValueError('Expected retained canonical native request')
        destination=root/f'input-{number:03d}.json';destination.write_bytes(raw);os.chmod(destination,0o600)
        inputs.append(native);sources[p.name]=hashlib.sha256(raw).hexdigest()
    import locua.amplifier_provider as provider
    from locua.engine.prototype import qwen38_runtime,tool_chat_worker
    paths=[Path(__file__),Path(provider.__file__),Path(qwen38_runtime.__file__),Path(tool_chat_worker.__file__)]
    freeze={'source_requests':sources,'source_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        'order':['uncached','cached'],'requests_per_lane':3,'model':'qwen38',
        'fixed_inputs_no_generated_history':True,'desktop_dispatches':0,'default_cache_enabled':False}
    private_json(root/'freeze.json',freeze)
    report={'status':'running','lanes':{},'comparisons':[],'desktop_dispatches':0,
            'scope':'Three retained inputs; no accuracy or general cache-parity claim'}
    try:
        with runtime_environment(_config(config)):
            for lane,enabled in (('uncached',False),('cached',True)):
                lane_root=root/lane;lane_root.mkdir(mode=0o700);service=None
                entry={'calls':[],'worker_closed':False};report['lanes'][lane]=entry
                started=time.perf_counter()
                try:
                    service=service_factory('qwen38',qwen38_prompt_cache=enabled)
                    private_json(lane_root/'worker-ready.json',service.info())
                    entry['load_wall_ms']=(time.perf_counter()-started)*1000
                    for number,native in enumerate(inputs,1):
                        tick=time.perf_counter()
                        generation=service.generate(deepcopy(native['messages']),deepcopy(native['tools']))
                        private_json(lane_root/f'call-{number:03d}-raw.json',generation)
                        parsed=parse_tool_output(generation['raw_output'],native['tools'],generation['finish_reason'])
                        record={'call':number,'raw_output':generation['raw_output'],'parsed_blocks':parsed,
                            'usage':generation['usage'],'timing':generation['timing'],
                            'generation_metrics':generation.get('generation_metrics'),
                            'memory':generation['model_info'].get('memory'),
                            'wall_ms':(time.perf_counter()-tick)*1000,'finish_reason':generation['finish_reason']}
                        entry['calls'].append(record);private_json(lane_root/f'call-{number:03d}-summary.json',record)
                        print(lane,number,'tokens',record['usage'],'ms',record['timing']['generation_ms'],flush=True)
                finally:
                    if service is not None:
                        service.close();entry['worker_closed']=service.closed and service.process.poll() is not None
                    entry['total_wall_ms']=(time.perf_counter()-started)*1000
                    private_json(lane_root/'summary.json',entry)
        for a,b in zip(report['lanes']['uncached']['calls'],report['lanes']['cached']['calls'],strict=True):
            metrics=b['generation_metrics'] or {}
            checks={'raw_output_equal':a['raw_output']==b['raw_output'],'parsed_blocks_equal':a['parsed_blocks']==b['parsed_blocks'],
                    'full_token_counts_equal':a['usage']==b['usage'],'both_stopped':a['finish_reason']==b['finish_reason']=='stop',
                    'full_context_count_retained':metrics.get('full_input_tokens')==b['usage']['input_tokens']}
            report['comparisons'].append({'call':a['call'],'checks':checks,'passed':all(checks.values())})
        report['passed']=(len(report['comparisons'])==3 and all(x['passed'] for x in report['comparisons'])
            and all(x['worker_closed'] and len(x['calls'])==3 for x in report['lanes'].values())
            and any((x.get('generation_metrics') or {}).get('reused_input_tokens',0)>0 for x in report['lanes']['cached']['calls']))
        report['status']='passed' if report['passed'] else 'failed'
    except BaseException as error:
        report['status']='failed';report['passed']=False;report['error']=type(error).__name__+': '+str(error)
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
    finally:private_json(root/'summary.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True);p.add_argument('--config')
    p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]/'artifacts/amplifier-v10-qwen38-nonce-001/provider'))
    args=p.parse_args();result=run(args.out,args.source,args.config)
    print(json.dumps({'status':result['status'],'passed':result.get('passed'),'out':args.out},indent=2))
    raise SystemExit(0 if result.get('passed') else 1)
