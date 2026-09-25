"""Isolated explicit Qwen3.8 text-only candidate; no changes to RLCD runtimes."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import resource
import time

ROOT=Path(__file__).resolve().parents[1]
PIN_PATH=ROOT/'probes/qwen38-model.json'
MANIFEST_PATH=ROOT/'probes/qwen38-files.json'
DECODING='ordinary_greedy_native_qwen38_tools_nonthinking_v1'
THINKING_DECODING='ordinary_greedy_native_qwen38_tools_bounded_thinking_v1'
THINKING_REDACTION='native_implicit_think_final_only_v1'
PARSER='strict_qwen3_coder_xml_v2_boolean_lexemes'


def model_pin():return json.loads(PIN_PATH.read_text())


def thinking_configuration(enabled,*,prompt_cache_enabled=False,volatile_suffix_checkpoint_enabled=False):
    """Explicit mode; existing nonthinking cache parity does not cover thinking."""
    if type(enabled)is not bool:raise ValueError('Qwen3.8 thinking option must be boolean')
    if enabled and (prompt_cache_enabled or volatile_suffix_checkpoint_enabled):
        raise ValueError('Thinking prompt cache requires separate parity evidence; keep caching disabled')
    return {'decoding':THINKING_DECODING if enabled else DECODING,
            'template_kwargs':{'enable_thinking':enabled,'preserve_thinking':False}}


def redact_thinking_output(raw):
    """Remove private reasoning before worker IPC, logs, or tool parsing.

    The pinned template already ends in ``<think>\\n``. Generated text must
    supply exactly one closing delimiter; no inferred delimiter or parse repair.
    Both private reasoning and final output consume the same generation budget.
    """
    if not isinstance(raw,str):raise ValueError('Generated thinking output must be text')
    valid=raw.count('</think>')==1 and '<think>' not in raw
    reasoning,final=raw.split('</think>',1) if valid else (raw,'')
    metadata={'policy':THINKING_REDACTION,'status':'closed' if valid else 'invalid_or_unclosed',
        'reasoning_text_recorded':False,'reasoning_characters_removed':len(reasoning),
        'reasoning_sha256':hashlib.sha256(reasoning.encode()).hexdigest(),
        'generated_text_sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'final_text_sha256':hashlib.sha256(final.encode()).hexdigest(),
        'reasoning_token_count':None,'token_accounting':'usage.output_tokens includes reasoning, delimiter and final output'}
    return final,metadata


def verify_snapshot(cache,manifest_path=MANIFEST_PATH):
    """Verify all loaded local resources, with no download or remote code."""
    pin=model_pin();record=json.loads(Path(manifest_path).read_text())
    if any(record.get(k)!=pin[k] for k in ('model_id','revision')):
        raise RuntimeError('Qwen3.8 file manifest differs from independent model pin')
    files=record.get('files');required={'config.json','tokenizer.json','tokenizer_config.json','chat_template.jinja','model.safetensors.index.json'}
    if not isinstance(files,list) or not files:raise RuntimeError('Qwen3.8 file manifest is empty')
    names=[f.get('path') for f in files]
    if (any(not isinstance(n,str) or Path(n).name!=n for n in names)
            or len(names)!=len(set(names)) or not required<=set(names)
            or not any(n.endswith('.safetensors') for n in names)):
        raise RuntimeError('Qwen3.8 manifest has missing, duplicate or unsafe paths')
    snapshot=Path(cache)/'hub'/('models--'+pin['model_id'].replace('/','--'))/'snapshots'/pin['revision']
    for entry in files:
        path=snapshot/entry['path']
        if path.stat().st_size!=entry['bytes']:raise RuntimeError('Qwen3.8 local file size changed: '+entry['path'])
        with path.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
        if actual!=entry['sha256']:raise RuntimeError('Qwen3.8 local file digest changed: '+entry['path'])
    # mlx-lm loads every matching shard. An unmanifested local shard cannot join.
    if {p.name for p in snapshot.glob('model*.safetensors')}!={n for n in names if n.endswith('.safetensors')}:
        raise RuntimeError('Unexpected/missing Qwen3.8 weight shard')
    config=json.loads((snapshot/'config.json').read_text())
    tokenizer=json.loads((snapshot/'tokenizer_config.json').read_text())
    if (config.get('model_type')!='qwen3_5' or config.get('text_config',{}).get('model_type')!='qwen3_5_text'
            or config.get('model_file') or tokenizer.get('chat_template_type')):
        raise RuntimeError('Unexpected model architecture or custom executable loader')
    index=json.loads((snapshot/'model.safetensors.index.json').read_text())
    if set(index.get('weight_map',{}).values())!={n for n in names if n.endswith('.safetensors')}:
        raise RuntimeError('Qwen3.8 shard index differs from verified files')
    return snapshot,record,config


def _parameter(raw,schema):
    """Follow the native formatter's one-LF framing, with no value repair."""
    from .planner import strict_json
    value=raw[1:] if raw.startswith('\n') else raw
    value=value[:-1] if value.endswith('\n') else value
    kind=schema.get('type')
    if kind=='string':return value  # 'null', spaces and extra LF remain literal.
    if not isinstance(kind,str):raise ValueError('Ambiguous/unspecified XML parameter type; no inferred coercion')
    if kind not in ('boolean','integer','number','object','array','null'):
        raise ValueError('Unsupported XML parameter schema type')
    # Qwen's native XML parser accepts case-insensitive boolean lexemes.
    # Add only those two exact tokens; preserve all other strict JSON parsing.
    if kind=='boolean' and value.lower() in ('true','false'):
        return value.lower()=='true'
    parsed=strict_json(value)
    valid={'boolean':lambda x:type(x)is bool,'integer':lambda x:type(x)is int,
        'number':lambda x:type(x)in (int,float),'object':lambda x:isinstance(x,dict),
        'array':lambda x:isinstance(x,list),'null':lambda x:x is None}[kind](parsed)
    if not valid:raise ValueError('XML parameter value does not match its declared JSON type')
    return parsed


def parse_tool_output(raw,tools,finish_reason='stop'):
    """Strict native XML syntax, based on installed mlx_lm qwen3_coder format.

    Unlike that permissive parser, duplicate fields/unknown names, Python-literal
    fallback, invalid boolean coercion and incomplete suffixes are refused.
    No XML entities are decoded: the pinned template writes string values raw.
    Required-field validation belongs to the tool boundary: an unambiguous call
    with missing arguments must receive actionable tool feedback, not terminate
    the agent loop. Missing values are never filled in here.
    """
    from ...amplifier_provider import MAX_BYTES,MAX_TOOL_CALLS
    if not isinstance(raw,str) or len(raw.encode())>MAX_BYTES:raise ValueError('Invalid Qwen3.8 output type/size')
    if finish_reason!='stop':raise ValueError('Qwen3.8 generation did not finish normally')
    if '<think>' in raw or '</think>' in raw:raise ValueError('Unexpected thinking markup in explicit nonthinking response')
    schemas={t['function']['name']:t['function'].get('parameters',{}) for t in tools}
    if len(schemas)!=len(tools):raise ValueError('Duplicate tool schemas')
    pos=0;blocks=[];calls=0
    while pos<len(raw):
        start=raw.find('<tool_call>',pos)
        if start<0:
            tail=raw[pos:]
            if any(tag in tail for tag in ('</tool_call>','<function=','</function>','<parameter=','</parameter>')):
                raise ValueError('Unmatched native tool markup')
            if tail:blocks.append({'type':'text','text':tail})
            break
        text=raw[pos:start]
        if any(tag in text for tag in ('</tool_call>','<function=','</function>','<parameter=','</parameter>')):
            raise ValueError('Unmatched native tool markup')
        if text:blocks.append({'type':'text','text':text})
        at=start+len('<tool_call>')
        while at<len(raw) and raw[at].isspace():at+=1
        match=re.match(r'<function=([A-Za-z_][A-Za-z0-9_.:-]{0,127})>',raw[at:])
        if not match or match[1] not in schemas:raise ValueError('Missing/unknown native XML function')
        name=match[1];at+=len(match[0]);schema=schemas[name];properties=schema.get('properties',{})
        if not isinstance(properties,dict):raise ValueError('Tool properties require an object')
        args={}
        while True:
            while at<len(raw) and raw[at].isspace():at+=1
            if raw.startswith('</function>',at):at+=len('</function>');break
            parameter=re.match(r'<parameter=([A-Za-z_][A-Za-z0-9_.:-]{0,127})>',raw[at:])
            if not parameter:raise ValueError('Malformed native XML parameter')
            key=parameter[1]
            if key in args or key not in properties:raise ValueError('Duplicate or undeclared XML parameter')
            at+=len(parameter[0]);end=raw.find('</parameter>',at)
            if end<0:raise ValueError('Unclosed native XML parameter')
            value=raw[at:end]
            if any(tag in value for tag in ('<tool_call>','</tool_call>','<function=','</function>','<parameter=')):
                raise ValueError('Ambiguous unescaped native XML value')
            args[key]=_parameter(value,properties[key]);at=end+len('</parameter>')
        while at<len(raw) and raw[at].isspace():at+=1
        if not raw.startswith('</tool_call>',at):raise ValueError('Unclosed native XML function call')
        calls+=1
        if calls>MAX_TOOL_CALLS:raise ValueError('Too many native XML tool calls')
        blocks.append({'type':'tool_call','name':name,'arguments':args});pos=at+len('</tool_call>')
    if not raw.strip():raise ValueError('Empty Qwen3.8 output')
    return blocks


CACHE_POLICY='exact_token_prefix_checkpoint_v1'
VOLATILE_SUFFIX_POLICY='exact_chunk_aligned_prefix_before_volatile_native_suffix_v3'
PREFILL_STEP_SIZE=2048
# This identifies only a cache boundary. It does not classify instructions,
# change message roles, or remove text; every token still reaches the model.
COMPACTION_MARKER='<|im_start|>user\n<system-reminder source="context-compaction">'
# The pinned nonthinking template appends this exact generation suffix. When
# that turn is replayed with preserve_thinking=False, the empty thinking block
# disappears. Saving state beyond it therefore defeats exact cross-turn reuse.
# This marker only selects a checkpoint; it never changes the native prompt.
NATIVE_GENERATION_SUFFIX='<|im_start|>assistant\n<think>\n\n</think>\n\n'


def checkpoint_boundary(tokens, marker_tokens=None, *, generation_suffix_tokens=None):
    """Choose an exact saved prefix, leaving every remaining token for decode.

    Optional exact markers identify a unique compaction notice or the terminal
    native generation suffix. Use the earlier recognized boundary. Unrecognized
    markers keep the original before-final-token checkpoint. UI text resembling
    a marker can only reduce reuse: it cannot alter or omit the model input.
    """
    if not isinstance(tokens,list) or not tokens or any(type(t)is not int for t in tokens):
        raise ValueError('Nonempty exact integer input tokens required')
    boundary=len(tokens)-1;basis='before_final_input_token'
    if marker_tokens is not None:
        if not isinstance(marker_tokens,list) or not marker_tokens or any(type(t)is not int for t in marker_tokens):
            raise ValueError('Nonempty exact integer marker tokens required')
        matches=[i for i in range(1,len(tokens)-len(marker_tokens)+1)
                 if tokens[i:i+len(marker_tokens)]==marker_tokens]
        if len(matches)==1:boundary=matches[0];basis='before_compaction_notice'
        else:basis='marker_absent' if not matches else 'marker_ambiguous'
    if generation_suffix_tokens is not None:
        if (not isinstance(generation_suffix_tokens,list) or not generation_suffix_tokens
                or any(type(t)is not int for t in generation_suffix_tokens)):
            raise ValueError('Nonempty exact integer generation suffix tokens required')
        start=len(tokens)-len(generation_suffix_tokens)
        if 0<=start<boundary and tokens[start:]==generation_suffix_tokens:
            boundary=start;basis='before_native_generation_suffix'
    return boundary,basis



class ExactPrefixCheckpoint:
    """One pre-generation hybrid-state checkpoint; never trim recurrent state.

    Input prefix must be token-identical under the same model/template identity.
    The saved state is cloned before decoding, so generation cannot mutate it.
    """
    def __init__(self):self.invalidate()

    def invalidate(self):
        self.tokens=();self.identity=None;self.cache=None

    def prepare(self,prefix,identity,*,make_cache,prefill,check_deadline):
        if not isinstance(prefix,list) or any(type(t)is not int for t in prefix):
            self.invalidate();raise ValueError('Exact integer prefix tokens required')
        check_deadline()
        reuse=(self.cache is not None and self.identity==identity
               and len(prefix)>=len(self.tokens) and tuple(prefix[:len(self.tokens)])==self.tokens)
        reused=len(self.tokens) if reuse else 0
        reason='exact_prefix' if reuse else ('empty' if self.cache is None else 'identity_or_prefix_mismatch')
        if not reuse:self.invalidate()
        try:
            working=self.cache if reuse else make_cache()
            suffix=prefix[reused:]
            if suffix:prefill(suffix,working)
            check_deadline()
            self.cache=working;self.tokens=tuple(prefix);self.identity=identity
            # The installed MLX prompt-cache manager uses deepcopy for the same
            # purpose. Includes ArraysCache recurrent states, not only KV layers.
            active=deepcopy(working)
            check_deadline()
            return active,{'reused_input_tokens':reused,'new_checkpoint_tokens':len(suffix),
                           'checkpoint_tokens':len(prefix),'cache_lookup':reason}
        except BaseException:
            self.invalidate();raise


    def prepare_aligned(self,prefix,boundary,identity,*,make_cache,prefill,check_deadline):
        """Snapshot only an already evaluated original prefill chunk boundary.

        ``prefill`` consumes ALL remaining prefix tokens using the unchanged
        generate_step(max_tokens=0) call. Its supported progress callback clones
        state at the boundary; it never splits a model call or trims a cache.
        """
        if (not isinstance(prefix,list) or not prefix or any(type(t)is not int for t in prefix)
                or type(boundary)is not int or boundary<0 or boundary%PREFILL_STEP_SIZE
                or (boundary>0 and boundary>=len(prefix)-1)):
            self.invalidate();raise ValueError('Checkpoint must precede the final prefill step on the original2048-token grid')
        check_deadline()
        reuse=(self.cache is not None and self.identity==identity and len(self.tokens)<=boundary
               and tuple(prefix[:len(self.tokens)])==self.tokens and len(self.tokens)%PREFILL_STEP_SIZE==0)
        reused=len(self.tokens) if reuse else 0
        reason='exact_prefix' if reuse else ('empty' if self.cache is None else 'identity_or_prefix_mismatch')
        if not reuse:self.invalidate()
        try:
            # The stored grid checkpoint must survive all transient prefill and
            # generation. Work on a clone even when the chosen boundary repeats.
            working=deepcopy(self.cache) if reuse else make_cache()
            saved=self.cache if reuse and boundary==reused else deepcopy(working) if boundary==0 else None
            captured_at=boundary if saved is not None else None
            def progress(processed,total):
                nonlocal saved,captured_at
                check_deadline()
                if type(processed)is not int or type(total)is not int or total!=len(prefix)-reused:
                    raise RuntimeError('Unexpected installed MLX prefill progress contract')
                absolute=reused+processed
                if absolute==boundary and saved is None:
                    saved=deepcopy(working);captured_at=absolute
                    check_deadline()
            suffix=prefix[reused:]
            prefill(suffix,working,progress)
            check_deadline()
            if saved is None or captured_at!=boundary:
                raise RuntimeError('Original prefill did not expose the requested stable chunk boundary')
            self.cache=saved;self.tokens=tuple(prefix[:boundary]);self.identity=identity
            return working,{'reused_input_tokens':reused,'new_checkpoint_tokens':boundary-reused,
                'checkpoint_tokens':boundary,'cache_lookup':reason,
                'prefill_chunk_size':PREFILL_STEP_SIZE,'prefill_partition_origin_tokens':0,
                'transient_prefill_tokens':len(prefix)-boundary,'checkpoint_captured_at_tokens':captured_at}
        except BaseException:
            self.invalidate();raise


class Qwen38Runtime:
    """Verified local weights, installed text-only MLX model, greedy generation."""
    model_key='qwen38'
    decoding=DECODING
    template_kwargs={'enable_thinking':False,'preserve_thinking':False}
    def __init__(self,limits,*,prompt_cache_enabled=False,volatile_suffix_checkpoint_enabled=False,enable_thinking=False):
        if type(prompt_cache_enabled)is not bool or type(volatile_suffix_checkpoint_enabled)is not bool:
            raise ValueError('Cache options must be boolean')
        if volatile_suffix_checkpoint_enabled and not prompt_cache_enabled:
            raise ValueError('Volatile suffix checkpoint requires explicit prompt caching')
        mode=thinking_configuration(enable_thinking,prompt_cache_enabled=prompt_cache_enabled,
                                    volatile_suffix_checkpoint_enabled=volatile_suffix_checkpoint_enabled)
        self.enable_thinking=enable_thinking;self.decoding=mode['decoding'];self.template_kwargs=mode['template_kwargs']
        self.volatile_suffix_checkpoint_enabled=volatile_suffix_checkpoint_enabled
        self.prompt_cache_enabled=prompt_cache_enabled;self.checkpoint=ExactPrefixCheckpoint()
        self.last_generation_metrics=None
        from ..runtime_paths import model_cache
        from ... import amplifier_provider
        from . import tool_chat_worker
        started=time.perf_counter();pin=model_pin();self.calls=0
        versions={}
        for line in (ROOT/'probes/requirements-rlcd.lock.txt').read_text().splitlines():
            if not line or line.startswith('#'):continue
            name,expected=line.split('==',1);actual=metadata.version(name)
            if actual!=expected:raise RuntimeError('Local Qwen3.8 dependency differs from existing pin: '+name)
            versions[name]=actual
        snapshot,manifest,config=verify_snapshot(model_cache());verified_ms=(time.perf_counter()-started)*1000
        import mlx.core as mx
        from mlx_lm import load
        if not mx.metal.is_available():raise RuntimeError('MLX Metal unavailable; no CPU/cloud fallback')
        self.mx=mx;mx.set_memory_limit(limits.memory_limit_bytes);mx.set_cache_limit(limits.cache_limit_bytes)
        self.model,self.tokenizer=load(str(snapshot),tokenizer_config={'trust_remote_code':False,'local_files_only':True},lazy=False)
        if type(self.model).__module__!='mlx_lm.models.qwen3_5':raise RuntimeError('Unexpected Qwen3.8 installed model class')
        template=getattr(self.tokenizer,'chat_template',None)
        if not isinstance(template,str) or '<function=' not in template or 'enable_thinking' not in template:
            raise RuntimeError('Verified tokenizer lacks expected native XML/nonthinking template')
        self.compaction_marker_tokens=None;self.generation_suffix_tokens=None
        if volatile_suffix_checkpoint_enabled:
            marker=self.tokenizer.encode(COMPACTION_MARKER,add_special_tokens=False)
            if not isinstance(marker,list) or not marker or any(type(t)is not int for t in marker):
                raise RuntimeError('Pinned tokenizer did not encode exact compaction marker')
            self.compaction_marker_tokens=marker
            suffix=self.tokenizer.encode(NATIVE_GENERATION_SUFFIX,add_special_tokens=False)
            if not isinstance(suffix,list) or not suffix or any(type(t)is not int for t in suffix):
                raise RuntimeError('Pinned tokenizer did not encode exact native generation suffix')
            self.generation_suffix_tokens=suffix
        self.metadata={'service':amplifier_provider.VERSION,'model_key':'qwen38','model_pin':pin,
            'backend':'mlx','decoder':self.decoding,'decoder_version':self.decoding,'logits_constraint':'none',
            'sampling':'greedy_argmax_temperature_0','enable_thinking':enable_thinking,'template_kwargs':deepcopy(self.template_kwargs),
            'tool_parser':PARSER,'text_only':True,'vision_weights_used':False,'trust_remote_code':False,
            'offline_libraries':True,'installed_dependencies':versions,'resident_weights':True,'cross_turn_kv_cache':prompt_cache_enabled,
            'prompt_cache_policy':(VOLATILE_SUFFIX_POLICY if volatile_suffix_checkpoint_enabled else CACHE_POLICY) if prompt_cache_enabled else 'off',
            'volatile_suffix_checkpoint_enabled':volatile_suffix_checkpoint_enabled,
            'prompt_cache_checkpoint':('snapshot at original2048-token prefill chunk boundary before exact terminal native generation suffix or earlier unique compaction notice; transient prefix uses a clone and original final-token decoding'
                if volatile_suffix_checkpoint_enabled else 'exact full-input prefix before final input token; generation receives a clone'),
            'memory_config':asdict(limits),'memory_limit_is_allocator_guideline_not_hard_rss_cap':True,
            'idle_cache_policy':'clear_after_load_and_generation','load_ms':round((time.perf_counter()-started)*1000,3),
            'file_integrity_check_ms':round(verified_ms,3),'verified_loaded_files':manifest['files'],
            'loaded_snapshot':str(snapshot),'loaded_model_class':type(self.model).__module__+'.'+type(self.model).__name__,
            'loaded_model_args':{k:config['text_config'].get(k) for k in ('num_hidden_layers','hidden_size','num_attention_heads','num_key_value_heads','max_position_embeddings')},
            'worker_sha256':hashlib.sha256(Path(tool_chat_worker.__file__).read_bytes()).hexdigest(),
            'provider_sha256':hashlib.sha256(Path(amplifier_provider.__file__).read_bytes()).hexdigest(),
            'runtime_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'native_chat_template_sha256':hashlib.sha256(template.encode()).hexdigest(),
            'native_chat_template_source':'unchanged pinned checkpoint chat_template.jinja','dispatched':False}
        self.clear_idle_cache()

    def info(self):
        return {**self.metadata,'generation_calls':self.calls,'memory':{'mlx_active_bytes':self.mx.get_active_memory(),
            'mlx_peak_bytes':self.mx.get_peak_memory(),'mlx_cache_bytes':self.mx.get_cache_memory(),
            'process_maxrss_bytes_macos':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'rss_is_not_complete_gpu_or_system_footprint':True}}

    def stream(self,prompt_tokens,*,max_tokens,check_deadline):
        from mlx_lm import stream_generate
        from mlx_lm.generate import generate_step
        from mlx_lm.models.cache import make_prompt_cache
        from mlx_lm.sample_utils import make_sampler
        self.calls+=1;started=time.perf_counter();stream=None;completed=False
        metrics={'cache_policy':(VOLATILE_SUFFIX_POLICY if getattr(self,'volatile_suffix_checkpoint_enabled',False) else CACHE_POLICY) if self.prompt_cache_enabled else 'off',
                 'full_input_tokens':len(prompt_tokens),'reused_input_tokens':0,
                 'new_checkpoint_tokens':0,'checkpoint_tokens':0,'cache_lookup':'disabled',
                 'checkpoint_prefill_ms':0.0,'checkpoint_cache_bytes':0,
                 'prefill_ms':None,'decode_ms':None,'first_token_ms':None,
                 'timing_basis':'checkpoint wall time plus MLX prompt/generation phase counters'}
        self.last_generation_metrics=metrics
        try:
            check_deadline();cache=None;remaining=prompt_tokens
            if self.prompt_cache_enabled:
                identity=(self.metadata['model_pin']['revision'],self.metadata['native_chat_template_sha256'],
                          self.metadata['runtime_sha256'],tuple(sorted(self.template_kwargs.items())))
                def prefill(tokens,state,progress=None):
                    # This is the supported mlx_lm.cache_prompt implementation:
                    # max_tokens=0 evaluates exactly input state, emits no output.
                    for _ in generate_step(self.mx.array(tokens),self.model,max_tokens=0,prompt_cache=state,
                                           sampler=make_sampler(temp=0.0),logits_processors=[],
                                           prompt_progress_callback=progress or check_deadline):
                        raise RuntimeError('Zero-output prompt prefill unexpectedly emitted a token')
                    self.mx.eval([c.state for c in state]);check_deadline()
                tick=time.perf_counter()
                boundary,basis=checkpoint_boundary(prompt_tokens,
                    getattr(self,'compaction_marker_tokens',None)
                    if getattr(self,'volatile_suffix_checkpoint_enabled',False) else None,
                    generation_suffix_tokens=(getattr(self,'generation_suffix_tokens',None)
                        if getattr(self,'volatile_suffix_checkpoint_enabled',False) else None))
                if (getattr(self,'volatile_suffix_checkpoint_enabled',False)
                        and basis in ('before_compaction_notice','before_native_generation_suffix') and len(prompt_tokens)>2):
                    # Verify the installed public default; do not silently choose
                    # a different floating-point kernel partition after an upgrade.
                    import inspect
                    if inspect.signature(generate_step).parameters['prefill_step_size'].default!=PREFILL_STEP_SIZE:
                        raise RuntimeError('Installed MLX prefill chunk default changed')
                    boundary=(min(boundary,len(prompt_tokens)-3)//PREFILL_STEP_SIZE)*PREFILL_STEP_SIZE
                    basis+='_aligned_2048'
                    cache,reuse=self.checkpoint.prepare_aligned(prompt_tokens[:-1],boundary,
                        (*identity,'original_chunk_grid_2048'),make_cache=lambda:make_prompt_cache(self.model),
                        prefill=prefill,check_deadline=check_deadline)
                else:
                    boundary=len(prompt_tokens)-1
                    cache,reuse=self.checkpoint.prepare(prompt_tokens[:-1],identity,
                        make_cache=lambda:make_prompt_cache(self.model),prefill=prefill,check_deadline=check_deadline)
                metrics.update(checkpoint_boundary_tokens=boundary,checkpoint_boundary_basis=basis,
                               generation_suffix_tokens=1,full_prompt_preserved=True)
                metrics.update(reuse);metrics['checkpoint_prefill_ms']=(time.perf_counter()-tick)*1000
                metrics['checkpoint_cache_bytes']=sum(c.nbytes for c in self.checkpoint.cache)
                remaining=prompt_tokens[-1:]
            metrics['processed_input_tokens']=len(prompt_tokens)-metrics['reused_input_tokens']
            stream=stream_generate(self.model,self.tokenizer,prompt=remaining,max_tokens=max_tokens,
                sampler=make_sampler(temp=0.0),logits_processors=[],prompt_progress_callback=check_deadline,
                **({'prompt_cache':cache} if cache is not None else {}))
            for response in stream:
                check_deadline()
                if metrics['first_token_ms'] is None:metrics['first_token_ms']=(time.perf_counter()-started)*1000
                pt=getattr(response,'prompt_tokens',None);ps=getattr(response,'prompt_tps',None)
                gt=getattr(response,'generation_tokens',None);gs=getattr(response,'generation_tps',None)
                if type(pt)is int and type(ps)in(int,float) and ps>0:
                    metrics['mlx_prompt_tokens']=pt;metrics['mlx_prompt_tps']=ps
                    metrics['prefill_ms']=metrics['checkpoint_prefill_ms']+pt/ps*1000
                if type(gt)is int and type(gs)in(int,float) and gs>0:
                    metrics['mlx_generation_tps']=gs;metrics['decode_ms']=gt/gs*1000
                if response.finish_reason in ('stop','length'):completed=True
                yield response
        finally:
            if stream is not None:stream.close()
            if not completed:self.checkpoint.invalidate()
            metrics['completed']=completed;metrics['runtime_total_ms']=(time.perf_counter()-started)*1000
            metrics['checkpoint_retained']=self.checkpoint.cache is not None

    def clear_idle_cache(self):self.mx.clear_cache()
