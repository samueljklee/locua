"""Fixed-input local parity: old checkpoint vs opt-in checkpoint before notice.

Replays retained native requests 007/008/009 exactly. No tool is dispatched and
new model output is never fed into another request. Each lane is a fresh model
process under the same OS offline policy and original generation bounds.
"""
import argparse
import contextlib
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from locua.engine.prototype.cli import private_json

NUMBERS=(7,8,9)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_freeze(root,expected):
    path=root/'freeze.json'
    if digest(path)!=expected:raise ValueError('Parity freeze changed')
    freeze=json.loads(path.read_text())
    for name,want in freeze['source_hashes'].items():
        if digest(name)!=want:raise ValueError('Frozen inference source changed: '+name)
    for name,want in freeze['input_hashes'].items():
        if digest(root/name)!=want:raise ValueError('Frozen native request changed: '+name)
    return freeze


def child(root,lane,freeze_hash):
    from locua.engine.prototype.decision import MemoryConfig
    from locua.engine.prototype.qwen38_runtime import Qwen38Runtime,parse_tool_output
    from locua.engine.prototype.planner_worker import hard_watchdog
    from locua.engine.prototype.tool_chat_worker import generate_chat
    verify_freeze(root,freeze_hash)
    folder=root/lane;folder.mkdir(mode=0o700,exist_ok=False)
    started=time.perf_counter();runtime=None
    report={'lane':lane,'calls':[],'desktop_dispatches':0,'runtime_released':False}
    try:
        # The original runtime/model/policy limits are identical between lanes.
        with contextlib.redirect_stdout(sys.stderr):
            runtime=Qwen38Runtime(MemoryConfig(256*1024**2,32*1024**3),prompt_cache_enabled=True,
                volatile_suffix_checkpoint_enabled=lane=='stable_suffix')
        report['load_ms']=(time.perf_counter()-started)*1000
        private_json(folder/'worker-ready.json',runtime.info())
        for number in NUMBERS:
            verify_freeze(root,freeze_hash)
            native=json.loads((root/f'input-{number:03d}.json').read_text())
            generation=generate_chat(runtime,deepcopy(native['messages']),deepcopy(native['tools']),
                max_input_tokens=24576,max_output_tokens=2048,generation_timeout_s=120,
                watchdog_factory=hard_watchdog)
            private_json(folder/f'call-{number:03d}-raw.json',generation)
            parsed=parse_tool_output(generation['raw_output'],native['tools'],generation['finish_reason'])
            record={'source_call':number,'raw_output':generation['raw_output'],'parsed_blocks':parsed,
                'usage':generation['usage'],'timing':generation['timing'],
                'generation_metrics':generation['generation_metrics'],
                'memory':generation['model_info']['memory'],'finish_reason':generation['finish_reason'],
                'request_sha256':generation['request_sha256'],'generation_calls':generation['generation_calls'],
                'dispatched':generation['dispatched']}
            report['calls'].append(record)
            private_json(folder/f'call-{number:03d}-summary.json',record)
            print(lane,number,json.dumps({'usage':record['usage'],'timing':record['timing'],
                'cache':record['generation_metrics']['cache_lookup']}),flush=True)
        report['status']='completed'
    except BaseException as error:
        report['status']='failed';report['error']=type(error).__name__+': '+str(error)
        raise
    finally:
        if runtime is not None:
            runtime.checkpoint.invalidate();runtime.clear_idle_cache();del runtime
            report['runtime_released']=True
        report['wall_ms']=(time.perf_counter()-started)*1000
        private_json(folder/'summary.json',report)


def compare(old,new):
    rows=[]
    if [c['source_call'] for c in old]!=list(NUMBERS) or [c['source_call'] for c in new]!=list(NUMBERS):
        raise ValueError('Expected all three exact frozen calls in both lanes')
    for a,b in zip(old,new,strict=True):
        metrics=b['generation_metrics']
        checks={'raw_output_equal':a['raw_output']==b['raw_output'],
            'parsed_tool_calls_equal':a['parsed_blocks']==b['parsed_blocks'],
            'full_usage_equal':a['usage']==b['usage'],
            'native_request_hash_equal':a['request_sha256']==b['request_sha256'],
            'normal_stop':a['finish_reason']==b['finish_reason']=='stop',
            'one_generation':a['generation_calls']==b['generation_calls']==1,
            'no_dispatch':a['dispatched'] is False and b['dispatched'] is False,
            'full_context_counted':metrics['full_input_tokens']==b['usage']['input_tokens'],
            'full_prompt_preserved':metrics.get('full_prompt_preserved') is True,
            'before_notice_checkpoint':metrics.get('checkpoint_boundary_basis')=='before_compaction_notice',
            'later_calls_reuse':b['source_call']==7 or (metrics['cache_lookup']=='exact_prefix' and metrics['reused_input_tokens']>0)}
        rows.append({'source_call':a['source_call'],'checks':checks,'passed':all(checks.values()),
            'old_generation_ms':a['timing']['generation_ms'],'new_generation_ms':b['timing']['generation_ms'],
            'old_prefill_ms':a['generation_metrics'].get('prefill_ms'),'new_prefill_ms':metrics.get('prefill_ms'),
            'old_decode_ms':a['generation_metrics'].get('decode_ms'),'new_decode_ms':metrics.get('decode_ms'),
            'new_reused_input_tokens':metrics['reused_input_tokens']})
    return rows


def run(root,source,config=None):
    from locua import amplifier_provider
    from locua.engine import runtime_paths
    from locua.engine.prototype import qwen38_runtime,tool_chat_worker
    from locua.engine_adapter import runtime_environment
    from locua.lib import _config
    root=Path(root).resolve();root.mkdir(mode=0o700,exist_ok=False)
    source=Path(source).resolve();inputs={}
    for n in NUMBERS:
        original=source/f'call-{n:03d}-native.json';raw=original.read_bytes();native=json.loads(raw)
        if not isinstance(native.get('messages'),list) or not isinstance(native.get('tools'),list):
            raise ValueError('Expected retained canonical native request')
        path=root/f'input-{n:03d}.json';path.write_bytes(raw);path.chmod(0o600);inputs[path.name]=digest(path)
    paths=[Path(__file__).resolve(),Path(qwen38_runtime.__file__),Path(tool_chat_worker.__file__),
           Path(amplifier_provider.__file__),Path(runtime_paths.__file__),qwen38_runtime.PIN_PATH,qwen38_runtime.MANIFEST_PATH]
    freeze={'input_hashes':inputs,'source_requests':str(source),'source_hashes':{str(p):digest(p) for p in paths},
        'lanes':['full_prefix','stable_suffix'],'source_calls':list(NUMBERS),'model':'qwen38',
        'input_limit':24576,'output_limit':2048,'generation_limit_seconds':120,
        'fixed_inputs_no_generated_history':True,'desktop_dispatches':0,'candidate_default_enabled':False}
    private_json(root/'freeze.json',freeze);freeze_hash=digest(root/'freeze.json')
    report={'status':'running','freeze_sha256':freeze_hash,'lanes':{},'desktop_dispatches':0,
        'scope':'Three fixed retained requests; no desktop completion or general parity claim'}
    try:
        with runtime_environment(_config(config)):
            for lane in freeze['lanes']:
                verify_freeze(root,freeze_hash)
                command=['/usr/bin/sandbox-exec','-f',str(runtime_paths.ROOT/'probes/offline-macos.sb'),
                    runtime_paths.runtime_python(),str(Path(__file__).resolve()),'--worker',lane,
                    '--out',str(root),'--freeze-sha256',freeze_hash]
                print('Starting',lane,'fixed retained calls007/008/009; no desktop dispatch',flush=True)
                log=root/(lane+'.log');process=None
                with log.open('x') as output:
                    log.chmod(0o600)
                    try:
                        process=subprocess.Popen(command,env=runtime_paths.worker_environment(os.environ),
                            stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
                        code=process.wait(timeout=480)
                    finally:
                        if process is not None and process.poll() is None:
                            process.terminate()
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                report['lanes'][lane]={'exit_code':code,'process_exited':process.poll() is not None}
                if code:raise RuntimeError(lane+' failed; see '+str(log))
                verify_freeze(root,freeze_hash)
                report['lanes'][lane]['result']=json.loads((root/lane/'summary.json').read_text())
        report['comparisons']=compare(report['lanes']['full_prefix']['result']['calls'],report['lanes']['stable_suffix']['result']['calls'])
        report['passed']=all(c['passed'] for c in report['comparisons']) and all(
            r['process_exited'] and r['result']['runtime_released'] for r in report['lanes'].values())
        report['status']='passed' if report['passed'] else 'failed'
    except BaseException as error:
        report.update(status='failed',passed=False,error=type(error).__name__+': '+str(error))
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
    finally:private_json(root/'summary.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True);parser.add_argument('--config')
    parser.add_argument('--source',default=str(Path(__file__).resolve().parents[1]/'artifacts/tool-schema-v5-2-001/textedit-regression/provider'))
    parser.add_argument('--worker',choices=['full_prefix','stable_suffix'],help=argparse.SUPPRESS)
    parser.add_argument('--freeze-sha256',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:child(Path(args.out),args.worker,args.freeze_sha256)
    else:
        result=run(args.out,args.source,args.config)
        print(json.dumps({'status':result['status'],'passed':result.get('passed'),'out':args.out}))
        raise SystemExit(0 if result.get('passed') else 1)
