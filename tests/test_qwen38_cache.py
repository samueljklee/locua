"""Exact recurrent-state checkpoint lifecycle with CPU fakes, no model load."""
from copy import deepcopy
import sys
import types
import unittest
from unittest.mock import patch

from locua.engine.prototype.qwen38_runtime import ExactPrefixCheckpoint,Qwen38Runtime,CACHE_POLICY
from locua.amplifier_provider import ToolChatService

class Layer:
    def __init__(self):self.state=[]
    @property
    def nbytes(self):return len(self.state)*4

class PrefixTests(unittest.TestCase):
    def setUp(self):self.checkpoint=ExactPrefixCheckpoint();self.prefills=[]
    def prepare(self,tokens,identity='same'):
        def prefill(tokens,cache):self.prefills.append(tokens[:]);cache[0].state.extend(tokens)
        return self.checkpoint.prepare(tokens,identity,make_cache=lambda:[Layer()],prefill=prefill,check_deadline=lambda:None)
    def test_exact_prefix_suffix_only_and_decode_clone_does_not_mutate_checkpoint(self):
        active,first=self.prepare([1,2]);active[0].state.append(999)
        second,stats=self.prepare([1,2,3,4])
        self.assertEqual(self.prefills,[[1,2],[3,4]])
        self.assertEqual(second[0].state,[1,2,3,4]);self.assertEqual(stats['reused_input_tokens'],2)
        self.assertEqual(self.checkpoint.cache[0].state,[1,2,3,4])
    def test_changed_prefix_shorter_history_or_identity_recomputes_without_trim(self):
        for tokens,identity in (([1,9,3],'same'),([1],'same'),([1,2,3],'new-model-template')):
            self.setUp();self.prepare([1,2]);active,stats=self.prepare(tokens,identity)
            self.assertEqual(self.prefills[-1],tokens);self.assertEqual(stats['reused_input_tokens'],0)
            self.assertEqual(active[0].state,tokens)
    def test_identical_input_needs_no_prefill_and_ledger_is_immutable(self):
        source=[1,2];self.prepare(source);source[0]=7
        active,stats=self.prepare([1,2]);self.assertEqual(self.prefills,[[1,2]])
        self.assertEqual(stats['new_checkpoint_tokens'],0);self.assertEqual(active[0].state,[1,2])
    def test_partial_prefill_failure_invalidates_poisoned_recurrent_state(self):
        self.prepare([1,2])
        def fail(tokens,cache):cache[0].state.extend(tokens);raise RuntimeError('partial GPU failure')
        with self.assertRaises(RuntimeError):self.checkpoint.prepare([1,2,3],'same',make_cache=lambda:[Layer()],prefill=fail,check_deadline=lambda:None)
        self.assertIsNone(self.checkpoint.cache);self.assertEqual(self.checkpoint.tokens,())
        active,stats=self.prepare([1,2,3]);self.assertEqual(stats['reused_input_tokens'],0)
    def test_invalid_tokens_or_post_clone_deadline_invalidate(self):
        self.prepare([1,2])
        with self.assertRaises(ValueError):self.prepare([1,True])
        self.assertIsNone(self.checkpoint.cache)
        checks=[]
        def deadline():
            checks.append(1)
            if len(checks)==3:raise TimeoutError('expired after snapshot')
        with self.assertRaises(TimeoutError):self.checkpoint.prepare([1],'same',make_cache=lambda:[Layer()],prefill=lambda t,c:c[0].state.extend(t),check_deadline=deadline)
        self.assertIsNone(self.checkpoint.cache)
    def test_cache_opt_in_is_explicit_and_qwen38_only_before_process_start(self):
        for model,flag in (('baseline',True),('comparator',True),('qwen38','yes')):
            with self.subTest(model=model),self.assertRaises(ValueError):ToolChatService(model,qwen38_prompt_cache=flag)
        for options in ({'qwen38_stable_prefix_cache':True},
                        {'qwen38_prompt_cache':True,'qwen38_stable_prefix_cache':'yes'}):
            with self.subTest(options=options),self.assertRaises(ValueError):ToolChatService('qwen38',**options)

class RuntimeLifecycleTests(unittest.TestCase):
    def runtime(self,enabled=True):
        r=Qwen38Runtime.__new__(Qwen38Runtime);r.prompt_cache_enabled=enabled;r.checkpoint=ExactPrefixCheckpoint()
        r.calls=0;r.last_generation_metrics=None;r.model=object();r.tokenizer=object()
        r.mx=types.SimpleNamespace(array=lambda x:x,eval=lambda x:None)
        r.metadata={'model_pin':{'revision':'pinned'},'native_chat_template_sha256':'template','runtime_sha256':'source'}
        return r
    def modules(self,*,incomplete=False):
        self.processed=[];self.decoded=[]
        def prefill(tokens,model,**kw):
            self.assertEqual(kw['max_tokens'],0);self.processed.append(tokens[:]);kw['prompt_cache'][0].state.extend(tokens)
            return iter(())
        def stream(model,tokenizer,prompt,**kw):
            state=kw.get('prompt_cache',[Layer()]);state[0].state.extend(prompt);self.decoded.append(deepcopy(state[0].state))
            # Model output mutates active state, but must not join checkpoint.
            state[0].state.append(777)
            yield types.SimpleNamespace(text='x',generation_tokens=1,finish_reason=None if incomplete else 'stop',
                prompt_tokens=len(prompt),prompt_tps=100.0,generation_tps=20.0)
        return {'mlx_lm':types.SimpleNamespace(stream_generate=stream),
                'mlx_lm.generate':types.SimpleNamespace(generate_step=prefill),
                'mlx_lm.models.cache':types.SimpleNamespace(make_prompt_cache=lambda m:[Layer()]),
                'mlx_lm.sample_utils':types.SimpleNamespace(make_sampler=lambda temp:('greedy',temp))}
    def test_completed_stream_preserves_exact_checkpoint_and_new_suffix_state(self):
        r=self.runtime()
        with patch.dict(sys.modules,self.modules()):
            list(r.stream([1,2,3],max_tokens=4,check_deadline=lambda:None))
            list(r.stream([1,2,3,4,5],max_tokens=4,check_deadline=lambda:None))
        self.assertEqual(self.processed,[[1,2],[3,4]]);self.assertEqual(self.decoded,[[1,2,3],[1,2,3,4,5]])
        self.assertEqual(r.checkpoint.cache[0].state,[1,2,3,4]);m=r.last_generation_metrics
        self.assertEqual(m['reused_input_tokens'],2);self.assertEqual(m['full_input_tokens'],5)
        self.assertEqual(m['processed_input_tokens'],3);self.assertTrue(m['checkpoint_retained'])
        self.assertGreater(m['prefill_ms'],0);self.assertEqual(m['decode_ms'],50.0)
    def test_consumer_cancellation_invalidates_checkpoint(self):
        r=self.runtime()
        with patch.dict(sys.modules,self.modules(incomplete=True)):
            stream=r.stream([1,2,3],max_tokens=4,check_deadline=lambda:None);next(stream);stream.close()
        self.assertIsNone(r.checkpoint.cache);self.assertFalse(r.last_generation_metrics['completed'])
    def test_uncached_lane_keeps_full_prompt_and_reports_zero_reuse(self):
        r=self.runtime(False)
        with patch.dict(sys.modules,self.modules()):list(r.stream([1,2,3],max_tokens=4,check_deadline=lambda:None))
        self.assertEqual(self.processed,[]);self.assertEqual(self.decoded,[[1,2,3]])
        self.assertEqual(r.last_generation_metrics['cache_policy'],'off');self.assertEqual(r.last_generation_metrics['prefill_ms'],30.0)
