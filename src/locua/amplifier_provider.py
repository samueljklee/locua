"""Pinned local Qwen native tool chat, separate from the RLCD decision service.

Imports and metadata inspection do not load Amplifier, MLX, or model weights.
Tool calls are proposals; the mounted tools retain all authorization/IO guards.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

VERSION = 'locua-native-tool-chat-v1'
DECODING = 'ordinary_greedy_native_qwen_tool_chat'
MAX_INPUT_TOKENS = 24576
MAX_OUTPUT_TOKENS = 1024
MAX_GENERATION_SECONDS = 60
MAX_BYTES = 256 * 1024
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_TOOL_CALLS = 16
COUNT_SECONDS = 10
MODEL_KEYS = ('baseline','comparator','qwen38')


def native_model_pins():
    from .engine.prototype.planner import model_pins
    from .engine.prototype.qwen38_runtime import model_pin
    return {**model_pins(),'qwen38':model_pin()}


def model_limits(model):
    if model not in MODEL_KEYS:raise ValueError('Select an explicit pinned local model')
    return {'max_output_tokens':2048 if model=='qwen38' else MAX_OUTPUT_TOKENS,
            'generation_timeout_s':120 if model=='qwen38' else MAX_GENERATION_SECONDS,
            'memory_limit_bytes':(32 if model=='qwen38' else 10)*1024**3}


def decoding_for(model,*,thinking=False):
    if type(thinking)is not bool or (thinking and model!='qwen38'):
        raise ValueError('Thinking is an explicit Qwen3.8-only boolean option')
    if model=='qwen38':
        from .engine.prototype.qwen38_runtime import DECODING as native_decoding,THINKING_DECODING
        return THINKING_DECODING if thinking else native_decoding
    return DECODING


def _json(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def _hash(value):return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(value):
    if hasattr(value,'model_dump'):value=value.model_dump(mode='json',exclude_none=True)
    if not isinstance(value,dict):raise ValueError('Expected a message/request/schema object')
    return deepcopy(value)


def _finite_json(value):
    from .engine.prototype.planner import _json_data
    _json_data(value)


def validate_limits(max_input_tokens,max_output_tokens,generation_timeout_s,*,model='comparator'):
    limits=model_limits(model)
    if type(max_input_tokens)is not int or not 1<=max_input_tokens<=MAX_INPUT_TOKENS:
        raise ValueError('Native tool chat input limit must be 1..24576; no truncation')
    if type(max_output_tokens)is not int or not 1<=max_output_tokens<=limits['max_output_tokens']:
        raise ValueError('Native tool chat output limit must be1..'+str(limits['max_output_tokens']))
    if type(generation_timeout_s)not in (int,float) or not math.isfinite(generation_timeout_s) or not 0<generation_timeout_s<=limits['generation_timeout_s']:
        raise ValueError('Native tool chat generation deadline must be positive and at most'+str(limits['generation_timeout_s'])+' seconds')


def native_request(request,*,model='comparator',structured_tool_results=None):
    """Preserve text, complete tool history and schemas in native Qwen form.

    Qwen's pinned template has no developer/function roles and omits call IDs.
    Developer messages become explicitly labeled system messages. Tool replies
    carry their original call ID, name and output inside a JSON data envelope.
    Unsupported media/reasoning blocks fail rather than disappearing.
    """
    request=_object(request);_finite_json(request)
    envelope_bytes=len(_json(request).encode())
    if envelope_bytes>MAX_ENVELOPE_BYTES:raise ValueError('Raw chat envelope exceeds2MiB limit; no truncation')
    rows=request.get('messages')
    if not isinstance(rows,list) or not rows:raise ValueError('Nonempty full conversation history required')
    tools=[];names=set()
    for source in request.get('tools') or []:
        item=_object(source);name=item.get('name');schema=item.get('parameters')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,127}',name) or name in names:
            raise ValueError('Tool names must be unique valid function names')
        if not isinstance(schema,dict):raise ValueError('Tool parameters require a JSON Schema object')
        names.add(name)
        tools.append({'type':'function','function':{'name':name,'description':item.get('description') or '',
                                                   'parameters':deepcopy(schema)}})
    if len(tools)>128:raise ValueError('Tool inventory exceeds128; no tools dropped')
    native=[];calls={};answered=set();role_mappings=[];deduplicated_calls=0
    structured_tool_results=deepcopy(structured_tool_results or {})
    structured_translations=[];context_notices=[]
    def canonical_call(item):
        # Amplifier's standard loop stores `tool`; content blocks use `name`,
        # and OpenAI-shaped history wraps `name`/`arguments` in `function`.
        # Reconcile all supplied aliases; no preferred alias may hide conflict.
        item=_object(item);sources=[item]
        if 'function' in item:sources.append(_object(item['function']))
        names=[row[key] for row in sources for key in ('name','tool') if key in row]
        arguments=[row[key] for row in sources for key in ('arguments','input') if key in row]
        if not names or any(not isinstance(name,str) or not name for name in names) or len(set(names))!=1:
            raise ValueError('Missing or conflicting assistant tool name aliases')
        if not arguments or any(not isinstance(value,dict) for value in arguments):
            raise ValueError('Assistant tool call requires object arguments')
        canonical=lambda value:json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))
        if len({canonical(value) for value in arguments})!=1:
            raise ValueError('Conflicting assistant tool argument aliases')
        return {'id':item.get('id'),'name':names[0],'arguments':deepcopy(arguments[0])}
    def text_content(content):
        if isinstance(content,str):return content
        if not isinstance(content,list):raise ValueError('Message content must be text or content blocks')
        texts=[]
        for block in content:
            block=_object(block)
            if block.get('type')!='text' or not isinstance(block.get('text'),str):
                raise ValueError('Unsupported nontext content; no block is omitted')
            texts.append(block['text'])
        return ''.join(texts)
    def tool_reply(call_id,output,name=None):
        if not isinstance(call_id,str) or call_id not in calls or call_id in answered:
            raise ValueError('Tool result needs one unmatched prior assistant call ID')
        expected=calls[call_id]['function']['name']
        if name is not None and name!=expected:raise ValueError('Tool result name differs from original call')
        answered.add(call_id)
        # Only a trusted framework-side registration supplies structure authority.
        # JSON-looking arbitrary tool text remains a string, even if well-formed.
        approved=structured_tool_results.get(call_id)
        if approved is not None and isinstance(output,str):
            from .engine.prototype.planner import strict_json
            try:parsed=strict_json(output)
            except (ValueError,TypeError):parsed=None
            canonical=lambda v:json.dumps(v,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))
            matched=(isinstance(parsed,(dict,list))
                and any(output==json.dumps(v,allow_nan=False) and canonical(parsed)==canonical(v) for v in approved))
            if matched:
                structured_translations.append({'tool_call_id':call_id,
                    'source':'registered_framework_tool_result','encoding':'json_object_or_array',
                    'original_text_sha256':hashlib.sha256(output.encode()).hexdigest(),
                    'original_text_utf8_bytes':len(output.encode()),'structured_value_sha256':_hash(parsed),
                    'compact_utf8_bytes':len(_json(parsed).encode()),'all_values_preserved':True})
                output=parsed
        native.append({'role':'tool','content':_json({'tool_call_id':call_id,'name':expected,'output':output})})
    for row_index,row in enumerate(rows):
        row=_object(row);role=row.get('role');content=row.get('content','')
        if isinstance(row.get('metadata'),dict) and row['metadata'].get('source')=='context-compaction':
            context_notices.append({'source_message':row_index, 'role':role,
                'source':'context-compaction','role_preserved':True,
                'content_sha256':_hash(content)})
        if role in ('tool','function'):
            if isinstance(content,list) and content and all(_object(b).get('type')=='tool_result' for b in content):
                for b in content:
                    b=_object(b);tool_reply(b.get('tool_call_id'),b.get('output'),row.get('name'))
            else:tool_reply(row.get('tool_call_id'),text_content(content),row.get('name'))
            if role=='function':role_mappings.append('function_to_tool_with_verified_call_id')
            continue
        if role=='user' and isinstance(content,list) and any(_object(b).get('type')=='tool_result' for b in content):
            # Preserve mixed user text/tool-result blocks in their original order.
            for b in content:
                b=_object(b)
                if b.get('type')=='tool_result':tool_reply(b.get('tool_call_id'),b.get('output'))
                elif b.get('type')=='text' and isinstance(b.get('text'),str):native.append({'role':'user','content':b['text']})
                else:raise ValueError('Unsupported mixed user block')
            continue
        if role=='assistant':
            text=[];tool_calls=[];call_seen=False
            if isinstance(content,str):text=[content]
            elif isinstance(content,list):
                for b in content:
                    b=_object(b)
                    if b.get('type')=='text' and isinstance(b.get('text'),str):
                        if call_seen and b['text']:role_mappings.append('assistant_text_factored_before_calls_by_native_template')
                        text.append(b['text'])
                    elif b.get('type')=='tool_call':
                        call_seen=True;tool_calls.append(canonical_call(b))
                    else:raise ValueError('Unsupported assistant block; no thinking/media data dropped')
            else:raise ValueError('Invalid assistant content')
            extra=row.get('tool_calls') or []
            if not isinstance(extra,list):raise ValueError('Assistant tool_calls must be a list')
            tool_calls.extend(canonical_call(item) for item in extra)
            prepared=[];message_calls={}
            for item in tool_calls:
                cid=item['id'];name=item['name'];args=item['arguments']
                if not isinstance(cid,str) or not cid:raise ValueError('Assistant tool call IDs must be nonempty strings')
                canonical=json.dumps(item,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))
                if cid in message_calls:
                    if message_calls[cid]!=canonical:raise ValueError('Conflicting assistant tool call representations for same ID')
                    deduplicated_calls+=1;continue
                if cid in calls:raise ValueError('Assistant tool call IDs must be unique across history')
                message_calls[cid]=canonical
                current={'id':cid,'type':'function','function':{'name':name,'arguments':deepcopy(args)}}
                calls[cid]=current;prepared.append(current)
            current={'role':'assistant','content':''.join(text)}
            if prepared:current['tool_calls']=prepared
            native.append(current);continue
        if role not in ('system','developer','user'):raise ValueError('Unsupported chat role: '+str(role))
        text=text_content(content)
        if role=='developer':
            role_mappings.append('developer_to_labeled_system');role='system';text='[Developer message]\n'+text
        native.append({'role':role,'content':text})
    if not native:raise ValueError('Empty native conversation')
    if model=='qwen38' and any(row['role']=='system' for row in native[1:]):
        raise ValueError('Qwen3.8 native template permits only initial system message; no role history rewritten')
    # Prevent user/tool strings from creating a native chat role delimiter.
    # Refusal is explicit; original artifacts retain the exact source string.
    for row in native:
        if any(token in row['content'] for token in ('<|im_start|>','<|im_end|>','<|endoftext|>')):
            raise ValueError('Message contains reserved native role delimiter; no escaping/truncation applied')
    native_bytes=len(_json({'messages':native,'tools':tools}).encode())
    if native_bytes>MAX_BYTES:raise ValueError('Canonical native messages/tools exceed256KiB; no history truncated')
    return {'messages':native,'tools':tools,'translation':{'full_history_preserved':True,
        'scope':'supplied Amplifier context view','framework_may_have_compacted_history':True,
        'raw_envelope_bytes':envelope_bytes,'native_messages_tools_bytes':native_bytes,
        'message_metadata_rendered':False,'original_metadata_retained_in_request_artifact':True,
        'source_messages':len(rows),'native_messages':len(native),'role_mappings':role_mappings,
        'tool_result_ids_preserved_as_data':True,'deduplicated_tool_call_representations':deduplicated_calls,
        'structured_tool_results':structured_translations,'structured_result_count':len(structured_translations),
        'context_compaction_notices':context_notices,'unregistered_tool_text_interpreted':False,
        'unsupported_blocks':'refuse','omitted_messages':0}}


def parse_native_output(raw,tool_names,finish_reason='stop'):
    """Parse native tool JSON without eval, repair, or delimiter regex guesses."""
    from .engine.prototype.planner import strict_json
    if not isinstance(raw,str) or len(raw.encode())>MAX_BYTES:raise ValueError('Invalid native response size/type')
    if finish_reason!='stop':raise ValueError('Generation did not finish normally: '+str(finish_reason))
    blocks=[];pos=0;count=0
    while pos<len(raw):
        start=raw.find('<tool_call>',pos)
        if start<0:
            text=raw[pos:]
            if '</tool_call>' in text:raise ValueError('Unmatched tool-call closing tag')
            if text:blocks.append({'type':'text','text':text})
            break
        text=raw[pos:start]
        if '</tool_call>' in text:raise ValueError('Unmatched tool-call closing tag')
        if text:blocks.append({'type':'text','text':text})
        value_start=start+len('<tool_call>')
        while value_start<len(raw) and raw[value_start].isspace():value_start+=1
        _,end=json.JSONDecoder().raw_decode(raw,value_start)
        value=strict_json(raw[value_start:end])
        closing=end
        while closing<len(raw) and raw[closing].isspace():closing+=1
        if not raw.startswith('</tool_call>',closing):raise ValueError('Native tool call lacks exact closing tag')
        if not isinstance(value,dict) or set(value)!={'name','arguments'}:
            raise ValueError('Native tool call requires exactly name and arguments')
        if value['name'] not in tool_names or not isinstance(value['arguments'],dict):
            raise ValueError('Unknown tool name or non-object arguments')
        _finite_json(value)
        count+=1
        if count>MAX_TOOL_CALLS:raise ValueError('Too many tool calls in one response')
        blocks.append({'type':'tool_call','name':value['name'],'arguments':value['arguments']})
        pos=closing+len('</tool_call>')
    if not raw.strip():raise ValueError('Empty native response')
    return blocks


class ToolChatService:
    """One persistent offline worker. Protocol/timeout failure terminates it."""
    def __init__(self,model='comparator',*,max_input_tokens=MAX_INPUT_TOKENS,max_output_tokens=None,
                 generation_timeout_s=None,cache_limit_bytes=256*1024**2,
                 memory_limit_bytes=None,startup_timeout_s=90,on_started=None,qwen38_prompt_cache=False,
                 qwen38_stable_prefix_cache=False,qwen38_thinking=False):
        from .engine.prototype.decision import LAB,MemoryConfig,ProtocolError
        from .engine.runtime_paths import runtime_python,worker_environment
        if type(qwen38_prompt_cache)is not bool or (qwen38_prompt_cache and model!='qwen38'):
            raise ValueError('Prefix cache is an explicit Qwen3.8-only boolean option')
        if type(qwen38_stable_prefix_cache)is not bool or (qwen38_stable_prefix_cache and not qwen38_prompt_cache):
            raise ValueError('Stable-prefix checkpoint requires explicit Qwen3.8 prompt caching')
        decoding_for(model,thinking=qwen38_thinking)
        if qwen38_thinking and (qwen38_prompt_cache or qwen38_stable_prefix_cache):
            raise ValueError('Thinking prompt cache requires separate parity evidence; keep caching disabled')
        self.qwen38_thinking=qwen38_thinking
        self.qwen38_prompt_cache=qwen38_prompt_cache
        self.qwen38_stable_prefix_cache=qwen38_stable_prefix_cache
        defaults=model_limits(model)
        max_output_tokens=defaults['max_output_tokens'] if max_output_tokens is None else max_output_tokens
        generation_timeout_s=defaults['generation_timeout_s'] if generation_timeout_s is None else generation_timeout_s
        memory_limit_bytes=defaults['memory_limit_bytes'] if memory_limit_bytes is None else memory_limit_bytes
        validate_limits(max_input_tokens,max_output_tokens,generation_timeout_s,model=model)
        if model=='qwen38' and memory_limit_bytes>defaults['memory_limit_bytes']:raise ValueError('Qwen3.8 memory guideline exceeds32GiB')
        if sys.platform!='darwin':raise RuntimeError('Pinned local MLX backend requires macOS; no fallback')
        if type(startup_timeout_s)not in (int,float) or not math.isfinite(startup_timeout_s) or not 0<startup_timeout_s<=300:raise ValueError('Invalid startup deadline')
        self.model_key=model;self.model_pin=native_model_pins()[model]
        self.max_input_tokens=max_input_tokens;self.max_output_tokens=max_output_tokens
        self.generation_timeout_s=generation_timeout_s;self.timeout_s=generation_timeout_s+5
        self.memory_config=MemoryConfig(cache_limit_bytes,memory_limit_bytes)
        self.closed=False;self.poisoned=False;self._lock=threading.Lock();self._messages=queue.Queue();self._seen_ids=set()
        command=['/usr/bin/sandbox-exec','-f',str(LAB/'probes/offline-macos.sb'),runtime_python(),
            '-m','locua.engine.prototype.tool_chat_worker','--model',model,
            '--max-input-tokens',str(max_input_tokens),'--max-output-tokens',str(max_output_tokens),
            '--generation-timeout-s',str(generation_timeout_s),'--cache-limit-bytes',str(cache_limit_bytes),
            '--memory-limit-bytes',str(memory_limit_bytes)]
        if qwen38_prompt_cache:command.append('--qwen38-prompt-cache')
        if qwen38_stable_prefix_cache:command.append('--qwen38-stable-prefix-cache')
        if qwen38_thinking:command.append('--qwen38-thinking')
        self.process=subprocess.Popen(command,cwd=LAB,env=worker_environment(dict(os.environ)),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr)
        os.set_blocking(self.process.stdin.fileno(),False)
        self._reader=threading.Thread(target=self._read,daemon=True);self._reader.start()
        if on_started:on_started(self)
        try:
            ready=self._receive(time.monotonic()+startup_timeout_s);info=ready.get('info',{})
            self._validate_info(info)
            if ready.get('type')!='ready':raise ProtocolError('Native chat worker did not establish readiness')
            self.ready_info=info
        except BaseException:
            self.poisoned=True;self.close();raise

    def _validate_info(self,info):
        from .engine.prototype.decision import ProtocolError
        worker=Path(__file__).parent/'engine/prototype/tool_chat_worker.py'
        thinking=getattr(self,'qwen38_thinking',False)
        if (not isinstance(info,dict) or info.get('service')!=VERSION or info.get('decoder')!=decoding_for(self.model_key,thinking=thinking)
                or info.get('model_key')!=self.model_key or info.get('model_pin')!=self.model_pin
                or info.get('worker_sha256')!=hashlib.sha256(worker.read_bytes()).hexdigest()
                or info.get('provider_sha256')!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or info.get('offline_libraries')is not True or info.get('logits_constraint')!='none'):
            raise ProtocolError('Native tool-chat worker identity/pins/decoder mismatch')
        if self.model_key=='qwen38':
            from .engine.prototype.qwen38_runtime import PARSER,CACHE_POLICY,VOLATILE_SUFFIX_POLICY
            runtime=worker.with_name('qwen38_runtime.py')
            stable=getattr(self,'qwen38_stable_prefix_cache',False)
            policy=(VOLATILE_SUFFIX_POLICY if stable else CACHE_POLICY) if self.qwen38_prompt_cache else 'off'
            if (info.get('runtime_sha256')!=hashlib.sha256(runtime.read_bytes()).hexdigest()
                    or info.get('trust_remote_code')is not False or info.get('enable_thinking')is not thinking
                    or (thinking and info.get('template_kwargs')!={'enable_thinking':True,'preserve_thinking':False})
                    or info.get('text_only')is not True or info.get('tool_parser')!=PARSER
                    or info.get('prompt_cache_policy')!=policy
                    or info.get('volatile_suffix_checkpoint_enabled',False) is not stable):
                raise ProtocolError('Qwen3.8 thinking-mode/text-only/parser contract mismatch')

    # Reuse the serial, deadline-checked transport, not its RLCD implementation.
    def _read(self):
        from .engine.prototype.planner import PlannerService
        return PlannerService._read(self)
    def _receive(self,deadline):
        from .engine.prototype.decision import ModelService
        return ModelService._receive(self,deadline)
    def _send(self,payload,deadline):
        from .engine.prototype.decision import ModelService
        return ModelService._send(self,payload,deadline)
    def _request(self,op,payload=None):
        from .engine.prototype.decision import ModelService,ProtocolError,DecisionError
        if op!='count':return ModelService._request(self,op,payload)
        with self._lock:
            if self.closed or self.poisoned:raise ProtocolError('Native worker closed/poisoned')
            rid=str(uuid.uuid4());self._seen_ids.add(rid);deadline=time.monotonic()+COUNT_SECONDS+5
            try:
                self._send({'id':rid,'op':op,**(payload or {})},deadline);response=self._receive(deadline)
                if response.get('id')!=rid or type(response.get('ok'))is not bool:
                    raise ProtocolError('Unmatched/malformed count reply')
                if not response['ok']:raise DecisionError(json.dumps(response.get('error',{})))
                return rid,response
            except BaseException:
                self.poisoned=True;self.close();raise

    def close(self):
        from .engine.prototype.decision import ModelService
        return ModelService.close(self)
    def info(self):return deepcopy(self.ready_info)

    def count(self,messages,tools):
        from .engine.prototype.decision import ProtocolError
        payload={'messages':deepcopy(messages),'tools':deepcopy(tools)}
        try:
            rid,response=self._request('count',payload);value=response['measurement']
            self._validate_info(value['model_info'])
            if (value.get('request_sha256')!=_hash(payload) or value.get('tokenizer_only')is not True
                    or type(value.get('input_tokens'))is not int or value['input_tokens']<=0
                    or type(value.get('generation_calls'))is not int or value['generation_calls']!=0
                    or type(value.get('output_tokens'))is not int or value['output_tokens']!=0
                    or value.get('history_truncated')is not False
                    or type(value.get('tokenization_ms'))not in(int,float)
                    or not math.isfinite(value['tokenization_ms']) or not 0<=value['tokenization_ms']<=COUNT_SECONDS*1000):
                raise ProtocolError('Invalid exact native token measurement')
            return {**value,'request_id':rid}
        except BaseException:
            self.poisoned=True;self.close();raise

    def generate(self,messages,tools,*,max_output_tokens=None,generation_timeout_s=None):
        from .engine.prototype.decision import ProtocolError
        maximum=self.max_output_tokens if max_output_tokens is None else max_output_tokens
        timeout=self.generation_timeout_s if generation_timeout_s is None else generation_timeout_s
        validate_limits(self.max_input_tokens,maximum,timeout,model=self.model_key)
        if maximum>self.max_output_tokens or timeout>self.generation_timeout_s:raise ValueError('Per-call limits exceed configured bounds')
        payload={'messages':deepcopy(messages),'tools':deepcopy(tools),'max_output_tokens':maximum,'generation_timeout_s':timeout}
        try:
            rid,response=self._request('complete',payload)
        except Exception as error:
            # Only a typed, correlated worker proof establishes zero generation.
            # Runtime crashes or a human-readable error message remain unknown.
            from .engine.prototype.decision import DecisionError
            from .engine.prototype.planner import strict_json
            if isinstance(error,DecisionError):
                try:refusal=strict_json(str(error))
                except (ValueError,TypeError):refusal=None
                if (isinstance(refusal,dict) and refusal.get('type')=='InputBudgetRefusal'
                        and refusal.get('code')=='input_token_budget_exceeded'
                        and refusal.get('stage')=='pre_generation'
                        and type(refusal.get('generation_calls'))is int and refusal['generation_calls']==0
                        and type(refusal.get('input_tokens'))is int and refusal['input_tokens']>self.max_input_tokens
                        and type(refusal.get('output_tokens'))is int and refusal['output_tokens']==0
                        and type(refusal.get('input_limit_tokens'))is int and refusal['input_limit_tokens']==self.max_input_tokens
                        and refusal.get('request_sha256')==_hash(payload)):
                    error.pre_generation_refusal=deepcopy(refusal)
            raise
        try:
            result=response['completion'];self._validate_info(result['model_info'])
            usage=result['usage'];timing=result['timing']
            if (result.get('request_sha256')!=_hash(payload) or result.get('dispatched')is not False
                    or result.get('decoding')!=decoding_for(self.model_key,thinking=getattr(self,'qwen38_thinking',False)) or type(result.get('generation_calls'))is not int or result['generation_calls']not in (0,1)
                    or type(usage.get('input_tokens'))is not int or not 0<usage['input_tokens']<=self.max_input_tokens
                    or type(usage.get('output_tokens'))is not int or not 0<=usage['output_tokens']<=maximum
                    or any(type(timing.get(k))not in (int,float) or not math.isfinite(timing[k]) or timing[k]<0 for k in ('generation_ms','worker_total_ms'))
                    or not isinstance(result.get('raw_output'),str) or len(result['raw_output'].encode())>MAX_BYTES):
                raise ProtocolError('Native tool-chat response bounds/identity violated')
            if result['finish_reason']=='stop' and (result['generation_calls']!=1 or timing['generation_ms']>timeout*1000):raise ProtocolError('Invalid completed generation')
            if getattr(self,'qwen38_thinking',False):
                from .engine.prototype.qwen38_runtime import THINKING_REDACTION
                redaction=result.get('reasoning_redaction',{})
                if (result.get('raw_output_scope')!='final_after_native_thinking_delimiter'
                        or redaction.get('policy')!=THINKING_REDACTION or redaction.get('reasoning_text_recorded')is not False
                        or redaction.get('final_text_sha256')!=hashlib.sha256(result['raw_output'].encode()).hexdigest()
                        or (result['finish_reason']=='stop' and redaction.get('status')!='closed')):
                    raise ProtocolError('Thinking output lacks worker-side final-only boundary proof')
            if result['finish_reason'] in ('timeout','error','incomplete'):
                self.poisoned=True;self.close()
            return {**result,'request_id':rid}
        except (KeyError,TypeError,AttributeError,ProtocolError) as error:
            self.poisoned=True;self.close();raise ProtocolError('Invalid native chat worker response') from error


class LocalAmplifierProvider:
    """Amplifier provider with a lazily loaded resident local model per session."""
    name='locua-local-qwen'
    def __init__(self,*,model='comparator',runtime_config=None,out=None,service_factory=None,max_calls=48,**limits):
        if model not in MODEL_KEYS:raise ValueError('Select explicit pinned baseline, comparator or qwen38')
        if type(max_calls)is not int or not 1<=max_calls<=128:raise ValueError('Provider max_calls must be an integer1..128')
        self.max_calls=max_calls
        self.qwen38_thinking=limits.get('qwen38_thinking',False)
        decoding_for(model,thinking=self.qwen38_thinking)
        if self.qwen38_thinking and (limits.get('qwen38_prompt_cache') or limits.get('qwen38_stable_prefix_cache')):
            raise ValueError('Thinking prompt cache requires separate parity evidence; keep caching disabled')
        self.model=model;self.runtime_config=runtime_config;self.out=Path(out) if out is not None else None
        self._factory=service_factory or ToolChatService;self._limits=limits;self._service=None
        self._closed=False;self._serial=threading.Lock();self._calls=0;self.records=[]
        self._structured_tool_results={};self.budget_records=[];self._last_measurement=None
        if self.out is not None:self.out.mkdir(parents=True,mode=0o700,exist_ok=False)

    def register_structured_tool_result(self,call_id,result):
        """Called only by the framework's trusted tool:post hook, never model UI.

        Retain the complete ToolResult envelope plus its structured output as
        possible standard-loop serializations. A later message must match one
        exactly in standard-loop serialized bytes AND values/types before JSON is
        rendered without double encoding. Different formatting remains literal.
        """
        if not isinstance(call_id,str) or not call_id:raise ValueError('Framework tool call ID required')
        result=_object(result);_finite_json(result)
        if (set(result)!={'success','output','error'} or type(result['success'])is not bool
                or result['error'] is not None and not isinstance(result['error'],dict)):
            raise ValueError('Exact framework ToolResult envelope required')
        choices=[result]
        if isinstance(result['output'],(dict,list)):choices.append(deepcopy(result['output']))
        with self._serial:
            if self._closed:raise RuntimeError('Local tool-chat provider is closed')
            previous=self._structured_tool_results.get(call_id)
            if previous is not None:
                if _json(previous)!=_json(choices):raise ValueError('Conflicting framework result registration for tool call')
                return
            updated={**self._structured_tool_results,call_id:choices}
            if len(_json(updated).encode())>MAX_ENVELOPE_BYTES:
                raise ValueError('Framework structured-result registry exceeds2MiB; no results dropped')
            self._structured_tool_results=updated;sequence=len(updated)
        self._save(f'framework-tool-result-{sequence:03d}.json',{'tool_call_id':call_id,
            'source':'trusted_tool_post','accepted_structured_serializations':choices})

    def get_info(self):
        from amplifier_core.models import ProviderInfo
        return ProviderInfo(id=self.name,display_name='Local pinned Qwen native tool chat',capabilities=['tools','request_budget:provider_count'],
            defaults={'temperature':0,'max_output_tokens':model_limits(self.model)['max_output_tokens'],'model':self.model,'max_retries':0})

    async def list_models(self):
        from amplifier_core.models import ModelInfo
        return [ModelInfo(id=key,display_name=pin['model_id'],context_window=MAX_INPUT_TOKENS,
            max_output_tokens=model_limits(key)['max_output_tokens'],capabilities=['tools'],defaults={'temperature':0})
            for key,pin in native_model_pins().items()]

    def parse_tool_calls(self,response):return list(response.tool_calls or [])

    def _save(self,name,value):
        if self.out is not None:
            from .engine.prototype.cli import private_json
            private_json(self.out/name,value)

    def _ensure_service(self):
        from .engine_adapter import runtime_environment
        if self._service is None:
            def on_started(service):
                self._service=service
                if self._closed:service.close()
            with runtime_environment(self.runtime_config):
                service=self._factory(model=self.model,on_started=on_started,**self._limits)
            self._service=service;self._save('worker-ready.json',service.info())
        if self._closed:raise RuntimeError('Provider canceled during worker startup')
        return self._service

    def _request_budget(self,request,context_estimate,request_options):
        with self._serial:
            if self._closed:raise RuntimeError('Local tool-chat provider is closed')
            sequence=len(self.budget_records)+1;started=time.perf_counter()
            record={'measurement':sequence,'generation_calls':0,'output_tokens':0,
                    'status':'started','model_generation_started':False,'worker_count_started':False}
            self.budget_records.append(record)
            try:
                if type(context_estimate)is not int or context_estimate<0:raise ValueError('Nonnegative context_estimate required')
                if request_options:raise ValueError('Unknown provider request_options; exact count cannot ignore overrides')
                original=_object(request);record['request']=original;record['request_sha256']=_hash(original)
                if original.get('model')not in(None,self.model,native_model_pins()[self.model]['model_id']):
                    raise ValueError('Per-request model substitution is not allowed')
                native=native_request(original,model=self.model,structured_tool_results=self._structured_tool_results)
                choice=original.get('tool_choice','auto')
                if choice not in(None,'auto','required','none') and not isinstance(choice,dict):raise ValueError('Unsupported tool_choice')
                tools=[] if choice=='none' else native['tools']
                payload={'messages':native['messages'],'tools':tools};record['native_request']=native
                maximum=original.get('max_output_tokens')
                configured_cap=self._limits.get('max_output_tokens')
                configured_cap=model_limits(self.model)['max_output_tokens'] if configured_cap is None else configured_cap
                maximum=configured_cap if maximum is None else maximum
                if type(maximum)is not int or maximum>configured_cap:raise ValueError('Output cap exceeds configured worker bounds')
                limit=self._limits.get('max_input_tokens',MAX_INPUT_TOKENS)
                validate_limits(limit,maximum,self._limits.get('generation_timeout_s') or model_limits(self.model)['generation_timeout_s'],model=self.model)
                service=self._ensure_service();record['worker_count_started']=True
                measured=service.count(payload['messages'],payload['tools']);record['worker_measurement']=measured
                if self._closed:raise RuntimeError('Canceled token measurement discarded')
                count=measured['input_tokens'];self._last_measurement={'payload_sha256':_hash(payload),'input_tokens':count,'sequence':sequence}
                # Legacy context target only; exact full-template count above is
                # the measured-view authority. Never label the heuristic as N.
                target=max(0,min(limit,context_estimate+limit-count))
                decision={'estimated_input_tokens':count,'input_limit_tokens':limit,'context_token_budget':target,
                    'max_output_tokens':maximum,'measurement':{'kind':'provider_count','input_tokens':count,
                        'source':'locua-native-template-tokenizer-v1'},'generation_calls':0,
                    'native_request_sha256':_hash(payload),'measurement_sequence':sequence}
                record.update(status='completed',budget_decision=decision);return decision
            except BaseException as error:
                record.update(status='failed',error={'type':type(error).__name__,'message':str(error)})
                raise
            finally:
                record['service_wall_ms']=(time.perf_counter()-started)*1000
                self._save(f'budget-{sequence:03d}.json',record)

    async def request_budget(self,request,*,context_estimate,request_options=None):
        pending=asyncio.create_task(asyncio.to_thread(self._request_budget,request,context_estimate,request_options))
        try:return await asyncio.shield(pending)
        except asyncio.CancelledError:
            self._closed=True
            if self._service is not None:await asyncio.to_thread(self._service.close)
            try:await asyncio.shield(pending)
            except BaseException:pass
            raise

    def _complete(self,request,kwargs):
        from .engine_adapter import runtime_environment
        with self._serial:
            if self._closed:raise RuntimeError('Local tool-chat provider is closed')
            self._calls+=1;sequence=self._calls;started=time.perf_counter()
            original=_object(request);record={'call':sequence,'request':original,'request_sha256':_hash(original),
                'status':'started','dispatched':False,'inference_started':False,'complete_generation_count':0,
                'call_budget':{'max_calls':self.max_calls,'request_number':sequence,'refused':sequence>self.max_calls}}
            self.records.append(record);self._save(f'call-{sequence:03d}-input.json',original)
            try:
                if sequence>self.max_calls:raise RuntimeError('provider_call_budget_exhausted; no further generation started')
                if kwargs:raise ValueError('Unsupported provider call overrides: '+','.join(sorted(kwargs)))
                if original.get('model') not in (None,self.model,native_model_pins()[self.model]['model_id']):raise ValueError('Per-request model substitution is not allowed')
                if original.get('temperature') not in (None,0,0.0) or original.get('top_p') not in (None,1,1.0):raise ValueError('This provider uses declared deterministic greedy generation only')
                if original.get('stream') or original.get('stop') or original.get('reasoning_effort'):raise ValueError('Streaming, custom stop and reasoning overrides are unsupported')
                if (original.get('response_format') or {}).get('type','text')!='text':raise ValueError('No alternate JSON/goal schema is imposed on native tool chat')
                native=native_request(original,model=self.model,structured_tool_results=self._structured_tool_results);record['native_request']=native;record['native_request_sha256']=_hash(native)
                self._save(f'call-{sequence:03d}-native.json',native)
                choice=original.get('tool_choice','auto')
                required=None
                if choice in (None,'auto','required','none'):pass
                elif isinstance(choice,dict) and choice.get('type')=='function' and isinstance(choice.get('function',{}).get('name'),str):required=choice['function']['name']
                else:raise ValueError('Unsupported tool_choice')
                names={t['function']['name'] for t in native['tools']}
                if required is not None and required not in names:raise ValueError('Required tool is absent')
                self._ensure_service()
                record.update(inference_started=True,complete_generation_count=None)
                result=self._service.generate(native['messages'],[] if choice=='none' else native['tools'],
                    max_output_tokens=original.get('max_output_tokens'),generation_timeout_s=original.get('timeout'))
                record['generation']=result;record['complete_generation_count']=result.get('generation_calls')
                measured=self._last_measurement
                payload_hash=_hash({'messages':native['messages'],'tools':[] if choice=='none' else native['tools']})
                if measured is not None and measured['payload_sha256']==payload_hash:
                    record['exact_budget_measurement']=deepcopy(measured)
                    if result['usage']['input_tokens']!=measured['input_tokens']:
                        self._service.poisoned=True;self._service.close()
                        raise ValueError('Generation full-input count differs from exact preflight measurement')
                self._save(f'call-{sequence:03d}-raw.json',result)
                if self._closed:raise RuntimeError('Canceled completion discarded; no tool calls released')
                if self.qwen38_thinking and result.get('finish_reason')=='length':
                    raise ValueError('Bounded thinking output token budget exhausted; final result incomplete, no tools released')
                if self.model=='qwen38':
                    from .engine.prototype.qwen38_runtime import parse_tool_output
                    blocks=parse_tool_output(result['raw_output'],[] if choice=='none' else native['tools'],result['finish_reason'])
                else:blocks=parse_native_output(result['raw_output'],set() if choice=='none' else names,result['finish_reason'])
                calls=[b for b in blocks if b['type']=='tool_call']
                if choice=='required' and not calls:raise ValueError('Required tool call missing')
                if required is not None and (not calls or any(c['name']!=required for c in calls)):raise ValueError('Required named tool call not produced')
                from amplifier_core.message_models import ChatResponse,TextBlock,ToolCallBlock,ToolCall,Usage
                content=[];tool_calls=[]
                for block in blocks:
                    if block['type']=='text':content.append(TextBlock(text=block['text']))
                    else:
                        cid='locua-'+uuid.uuid4().hex
                        content.append(ToolCallBlock(id=cid,name=block['name'],input=block['arguments']))
                        tool_calls.append(ToolCall(id=cid,name=block['name'],arguments=block['arguments']))
                usage=result['usage'];record.update(status='completed',parsed_blocks=blocks)
                response=ChatResponse(content=content,tool_calls=tool_calls or None,
                    usage=Usage(input_tokens=usage['input_tokens'],output_tokens=usage['output_tokens'],
                        total_tokens=usage['input_tokens']+usage['output_tokens']),
                    finish_reason='tool_calls' if tool_calls else 'stop',
                    metadata={'provider':VERSION,'decoding':decoding_for(self.model,thinking=self.qwen38_thinking),'model':self.model,'local_only':True,
                        'raw_output':result['raw_output'],'timing':result['timing'],'request_id':result['request_id'],
                        'worker_model_info':result['model_info'],'artifact_call':sequence,'dispatched':False})
                record['response']=response.model_dump(mode='json',exclude_none=True)
                return response
            except BaseException as error:
                if hasattr(error,'pre_generation_refusal'):
                    record['pre_generation_refusal']=deepcopy(error.pre_generation_refusal)
                    record['complete_generation_count']=0
                record.update(status='failed',error={'type':type(error).__name__,'message':str(error)})
                raise
            finally:
                record['service_wall_ms']=round((time.perf_counter()-started)*1000,3)
                self._save(f'call-{sequence:03d}-summary.json',record)

    async def complete(self,request,**kwargs):
        pending=asyncio.create_task(asyncio.to_thread(self._complete,request,kwargs))
        try:return await asyncio.shield(pending)
        except asyncio.CancelledError:
            self._closed=True
            if self._service is not None:await asyncio.to_thread(self._service.close)
            # Drain the bounded worker call so no late response is released or
            # artifact writer remains after the session cancellation returns.
            try:await asyncio.shield(pending)
            except BaseException:pass
            raise

    async def close(self):
        self._closed=True
        if self._service is not None:await asyncio.to_thread(self._service.close)

    async def __aenter__(self):return self
    async def __aexit__(self,*_):await self.close()
