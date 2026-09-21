"""Resident pinned MLX native tool-chat worker. No desktop or network API."""
import argparse
from copy import deepcopy
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import time

from ...amplifier_provider import (VERSION,DECODING,MAX_BYTES,MAX_INPUT_TOKENS,MAX_OUTPUT_TOKENS,
    MAX_GENERATION_SECONDS,COUNT_SECONDS,validate_limits,_finite_json,_hash,model_limits,MODEL_KEYS)
from .planner import strict_json
from .planner_worker import LocalRuntime,GenerationDeadline,hard_watchdog
from .decision import MemoryConfig


class InputBudgetRefusal(ValueError):
    """Positive tokenizer-count evidence that runtime.stream was never called."""
    def __init__(self,input_tokens,input_limit_tokens,request_sha256):
        super().__init__(f'Native chat input {input_tokens} exceeds {input_limit_tokens}; no history truncated')
        self.details={'code':'input_token_budget_exceeded','stage':'pre_generation',
            'generation_calls':0,'input_tokens':input_tokens,'output_tokens':0,
            'input_limit_tokens':input_limit_tokens,'request_sha256':request_sha256}


def tokenize_native(runtime,messages,tools):
    """Single native-template path used by both count and actual generation."""
    _finite_json({'messages':messages,'tools':tools})
    native_bytes=len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False,separators=(',',':')).encode())
    if native_bytes>MAX_BYTES:raise ValueError('Canonical native messages/tools exceed256KiB; no history truncated')
    if (not isinstance(messages,list) or not messages or not isinstance(tools,list)
            or any(not isinstance(m,dict) or m.get('role')not in ('system','user','assistant','tool')
                   or not isinstance(m.get('content'),str) for m in messages)):
        raise ValueError('Native messages and complete tool schemas required')
    # Use the unmodified, pinned checkpoint template, including its native tools
    # preamble and assistant/tool history. No Locua goal schema or action labels.
    tokens=runtime.tokenizer.apply_chat_template(messages,tools=tools or None,tokenize=True,add_generation_prompt=True,
        **getattr(runtime,'template_kwargs',{}))
    if not isinstance(tokens,list) or any(type(t)is not int for t in tokens):raise ValueError('Tokenizer did not return one explicit token list')
    if not tokens:raise ValueError('Native chat tokenizer returned empty input')
    return tokens


def count_chat(runtime,messages,tools,*,watchdog_factory=None):
    """Exact CPU tokenizer measurement. No stream, prefill, cache or model call."""
    started=time.perf_counter();watchdog=watchdog_factory(COUNT_SECONDS) if watchdog_factory else None
    if watchdog is not None:watchdog.start()
    try:
        tokens=tokenize_native(runtime,messages,tools)
        elapsed=(time.perf_counter()-started)*1000
        if elapsed>COUNT_SECONDS*1000:raise TimeoutError('Native token counting deadline exceeded')
        return {'input_tokens':len(tokens),'output_tokens':0,'generation_calls':0,'tokenizer_only':True,
            'request_sha256':_hash({'messages':messages,'tools':tools}),
            'history_truncated':False,'tokenization_ms':elapsed,'model_info':runtime.info()}
    finally:
        if watchdog is not None:watchdog.cancel()


def generate_chat(runtime,messages,tools,*,max_input_tokens=MAX_INPUT_TOKENS,
                  max_output_tokens=MAX_OUTPUT_TOKENS,generation_timeout_s=MAX_GENERATION_SECONDS,
                  watchdog_factory=None):
    validate_limits(max_input_tokens,max_output_tokens,generation_timeout_s,model=getattr(runtime,'model_key','comparator'))
    payload={'messages':messages,'tools':tools,'max_output_tokens':max_output_tokens,
             'generation_timeout_s':generation_timeout_s}
    _finite_json(payload)
    started=time.perf_counter()
    tokens=tokenize_native(runtime,messages,tools)
    if len(tokens)>max_input_tokens:raise InputBudgetRefusal(len(tokens),max_input_tokens,_hash(payload))
    if not tokens:raise ValueError('Native chat tokenizer returned empty input')
    tick=time.perf_counter();deadline=time.monotonic()+generation_timeout_s
    def check_deadline(*_):
        if time.monotonic()>=deadline:raise GenerationDeadline('Native tool-chat generation deadline reached; no retry')
    watchdog=watchdog_factory(generation_timeout_s) if watchdog_factory is not None else None
    if watchdog is not None:watchdog.start()
    chunks=[];count=0;byte_count=0;reason=None;error=None;stream=None;calls=0
    try:
        check_deadline();calls=1
        stream=runtime.stream(tokens,max_tokens=max_output_tokens,check_deadline=check_deadline)
        for response in stream:
            check_deadline()
            if not isinstance(response.text,str) or type(response.generation_tokens)is not int or not count<=response.generation_tokens<=max_output_tokens:
                raise ValueError('Generation stream violated token/text bounds')
            count=response.generation_tokens;chunks.append(response.text);byte_count+=len(response.text.encode())
            if byte_count>MAX_BYTES:raise ValueError('Generated text exceeds byte limit')
            if response.finish_reason is not None:
                if response.finish_reason not in ('stop','length'):raise ValueError('Unknown generation finish reason')
                reason=response.finish_reason;break
        check_deadline()
        if reason is None:reason='incomplete';error={'type':'IncompleteGeneration','message':'Stream ended without completion reason'}
    except GenerationDeadline as exc:
        reason='timeout';error={'type':type(exc).__name__,'message':str(exc)}
    except Exception as exc:
        reason='error';error={'type':type(exc).__name__,'message':str(exc)}
    finally:
        try:
            if stream is not None and hasattr(stream,'close'):stream.close()
            check_deadline()
        except Exception as exc:
            reason='timeout' if isinstance(exc,GenerationDeadline) else 'error'
            error={'type':type(exc).__name__,'message':str(exc)}
        finally:
            if watchdog is not None:watchdog.cancel()
            generation_ms=(time.perf_counter()-tick)*1000
            try:runtime.clear_idle_cache()
            except Exception as exc:
                reason='error';error={'type':type(exc).__name__,'message':str(exc)}
    raw=''.join(chunks);extra={}
    if getattr(runtime,'enable_thinking',False):
        from .qwen38_runtime import redact_thinking_output
        raw,redaction=redact_thinking_output(raw)
        redaction['output_token_budget_exhausted']=reason=='length'
        extra={'reasoning_redaction':redaction,'raw_output_scope':'final_after_native_thinking_delimiter'}
        if reason=='length':
            error={'type':'ThinkingOutputBudgetReached','message':'Combined reasoning and final output exhausted the bounded output budget; no tools released'}
        if redaction['status']!='closed' and reason=='stop':
            reason='error';error={'type':'ThinkingBoundaryError','message':'Thinking generation lacks one unambiguous native closing delimiter; no tools released'}
    return {'raw_output':raw,'finish_reason':reason,'generation_error':error,**extra,
        'generation_calls':calls,'decoding':getattr(runtime,'decoding',DECODING),'dispatched':False,
        'usage':{'input_tokens':len(tokens),'output_tokens':count},
        'timing':{'generation_ms':round(generation_ms,3),'worker_total_ms':round((time.perf_counter()-started)*1000,3)},
        'request_sha256':_hash(payload),'model_info':runtime.info(),
        'input_limit_tokens':max_input_tokens,'output_limit_tokens':max_output_tokens,
        'generation_limit_seconds':generation_timeout_s,'history_truncated':False,
        'generation_metrics':deepcopy(getattr(runtime,'last_generation_metrics',None))}


class ToolRuntime(LocalRuntime):
    def __init__(self,model,limits):
        self.model_key=model
        super().__init__(model,limits,decoder='compact_greedy')
        from ... import amplifier_provider
        template=getattr(self.tokenizer,'chat_template',None)
        if not isinstance(template,str) or '<tool_call>' not in template or '<tool_response>' not in template:
            raise RuntimeError('Pinned tokenizer lacks expected native tool-chat contract')
        self.metadata.update(service=VERSION,decoder=DECODING,decoder_version=VERSION,
            planning_mode='ordinary_native_tool_chat',proposal_version=None,logits_constraint='none',
            worker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            provider_sha256=hashlib.sha256(Path(amplifier_provider.__file__).read_bytes()).hexdigest(),
            native_chat_template_sha256=hashlib.sha256(template.encode()).hexdigest(),
            native_chat_template_source='unchanged pinned checkpoint tokenizer_config.json',
            source_prompt='caller full role history plus native tokenizer tool preamble',
            application_goal_schema=None,resident_weights=True,cross_turn_kv_cache=False)
        # Parent metadata fields from LocalRuntime describe its old planner
        # wrapper, not this worker's input. Keep only accurate source claims.
        for key in ('intent_parser_sha256','prompt_sha256'):
            self.metadata.pop(key,None)


def serve(runtime,input_stream,output_stream,*,max_input_tokens=MAX_INPUT_TOKENS,
          max_output_tokens=MAX_OUTPUT_TOKENS,generation_timeout_s=MAX_GENERATION_SECONDS,watchdog_factory=None):
    validate_limits(max_input_tokens,max_output_tokens,generation_timeout_s,model=getattr(runtime,'model_key','comparator'))
    def emit(value):
        output_stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n');output_stream.flush()
    emit({'type':'ready','info':runtime.info()});seen=set()
    while True:
        line=input_stream.readline(2*MAX_BYTES+4097)
        if not line:return
        if len(line.encode())>2*MAX_BYTES+4096 or not line.endswith('\n'):
            emit({'type':'fatal','error':'Native chat request line exceeds bounds or is incomplete'});return
        message={}
        try:
            message=strict_json(line)
            if not isinstance(message,dict) or not isinstance(message.get('id'),str) or not message['id'] or message['id'] in seen:
                raise ValueError('Unique request ID required')
            seen.add(message['id'])
            op=message.get('op')
            if op in ('info','shutdown'):
                if set(message)!={'id','op'}:raise ValueError('Unknown protocol fields')
                emit({'id':message['id'],'ok':True,**({'info':runtime.info()} if op=='info' else {})})
                if op=='shutdown':return
                continue
            if op=='count':
                if set(message)!={'id','op','messages','tools'}:raise ValueError('Unknown count fields')
                with contextlib.redirect_stdout(sys.stderr):
                    count=count_chat(runtime,message['messages'],message['tools'],watchdog_factory=watchdog_factory)
                emit({'id':message['id'],'ok':True,'measurement':count});continue
            if op!='complete' or set(message)!={'id','op','messages','tools','max_output_tokens','generation_timeout_s'}:
                raise ValueError('Unknown native chat operation/fields')
            maximum=message['max_output_tokens'];timeout=message['generation_timeout_s']
            validate_limits(max_input_tokens,maximum,timeout,model=getattr(runtime,'model_key','comparator'))
            if maximum>max_output_tokens or timeout>generation_timeout_s:raise ValueError('Per-call limits exceed resident worker bounds')
            with contextlib.redirect_stdout(sys.stderr):
                result=generate_chat(runtime,message['messages'],message['tools'],max_input_tokens=max_input_tokens,
                    max_output_tokens=maximum,generation_timeout_s=timeout,watchdog_factory=watchdog_factory)
            emit({'id':message['id'],'ok':True,'completion':result})
            if result['finish_reason'] in ('timeout','error','incomplete'):return
        except Exception as exc:
            emit({'id':message.get('id') if isinstance(message,dict) else None,'ok':False,
                  'error':{'type':type(exc).__name__,'message':str(exc),
                           **(exc.details if isinstance(exc,InputBudgetRefusal) else {})}})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',choices=MODEL_KEYS,default='comparator')
    parser.add_argument('--qwen38-prompt-cache',action='store_true')
    parser.add_argument('--qwen38-stable-prefix-cache',action='store_true')
    parser.add_argument('--qwen38-thinking',action='store_true')
    parser.add_argument('--max-input-tokens',type=int,default=MAX_INPUT_TOKENS)
    parser.add_argument('--max-output-tokens',type=int)
    parser.add_argument('--generation-timeout-s',type=float)
    parser.add_argument('--cache-limit-bytes',type=int,default=256*1024**2)
    parser.add_argument('--memory-limit-bytes',type=int)
    args=parser.parse_args();defaults=model_limits(args.model)
    args.max_output_tokens=defaults['max_output_tokens'] if args.max_output_tokens is None else args.max_output_tokens
    args.generation_timeout_s=defaults['generation_timeout_s'] if args.generation_timeout_s is None else args.generation_timeout_s
    args.memory_limit_bytes=defaults['memory_limit_bytes'] if args.memory_limit_bytes is None else args.memory_limit_bytes
    validate_limits(args.max_input_tokens,args.max_output_tokens,args.generation_timeout_s,model=args.model)
    if args.model=='qwen38' and args.memory_limit_bytes>defaults['memory_limit_bytes']:raise ValueError('Qwen3.8 memory guideline exceeds32GiB')
    if args.qwen38_prompt_cache and args.model!='qwen38':raise ValueError('Cache option is Qwen3.8-only')
    if args.qwen38_stable_prefix_cache and not args.qwen38_prompt_cache:
        raise ValueError('Stable-prefix checkpoint requires explicit Qwen3.8 prompt caching')
    if args.qwen38_thinking and args.model!='qwen38':raise ValueError('Thinking mode is Qwen3.8-only')
    with contextlib.redirect_stdout(sys.stderr):
        limits=MemoryConfig(args.cache_limit_bytes,args.memory_limit_bytes)
        if args.model=='qwen38':
            from .qwen38_runtime import Qwen38Runtime
            runtime=Qwen38Runtime(limits,prompt_cache_enabled=args.qwen38_prompt_cache,
                volatile_suffix_checkpoint_enabled=args.qwen38_stable_prefix_cache,enable_thinking=args.qwen38_thinking)
        else:runtime=ToolRuntime(args.model,limits)
    serve(runtime,sys.stdin,sys.stdout,max_input_tokens=args.max_input_tokens,max_output_tokens=args.max_output_tokens,
          generation_timeout_s=args.generation_timeout_s,watchdog_factory=hard_watchdog)


if __name__=='__main__':main()
