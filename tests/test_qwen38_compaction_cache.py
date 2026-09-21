"""Candidate volatile-suffix checkpointing; CPU state fakes, no model/GUI."""
import sys
import unittest
from unittest.mock import patch
import test_qwen38_cache as fixtures
from locua.engine.prototype.qwen38_runtime import checkpoint_boundary,Qwen38Runtime,VOLATILE_SUFFIX_POLICY


class BoundaryTests(unittest.TestCase):
    def test_default_and_missing_marker_keep_original_boundary(self):
        self.assertEqual(checkpoint_boundary([1,2,3]),(2,'before_final_input_token'))
        self.assertEqual(checkpoint_boundary([1,2,3],[8,9]),(2,'marker_absent'))

    def test_unique_framed_marker_leaves_entire_suffix_unchanged(self):
        tokens=[1,2,8,9,6,7];original=tokens[:]
        n,reason=checkpoint_boundary(tokens,[8,9])
        self.assertEqual((n,reason),(2,'before_compaction_notice'))
        self.assertEqual(tokens[:n]+tokens[n:],original)
        self.assertEqual(tokens,original)

    def test_ambiguous_marker_falls_back_instead_of_guessing(self):
        self.assertEqual(checkpoint_boundary([1,8,9,2,8,9,3],[8,9]),(6,'marker_ambiguous'))

    def test_invalid_or_boolean_tokens_are_rejected(self):
        for tokens,marker in (([],None),([True],None),([1,2],[]),([1,2],[False]),((1,2),None)):
            with self.subTest(tokens=tokens,marker=marker),self.assertRaises(ValueError):checkpoint_boundary(tokens,marker)

    def test_candidate_must_be_explicit_boolean_and_requires_cache(self):
        for options in ({'volatile_suffix_checkpoint_enabled':True},
                        {'prompt_cache_enabled':True,'volatile_suffix_checkpoint_enabled':'yes'}):
            with self.assertRaises(ValueError):Qwen38Runtime(None,**options)


class VolatileRuntimeTests(unittest.TestCase):
    def setup_runtime(self,enabled=True):
        self.fixture=fixtures.RuntimeLifecycleTests()
        r=self.fixture.runtime();r.volatile_suffix_checkpoint_enabled=enabled;r.compaction_marker_tokens=[8,9]
        return r

    def modules(self,*,incomplete=False):
        from copy import deepcopy
        import types
        self.invocations=[];self.decoded=[]
        def prefill(tokens,model,*,prefill_step_size=2048,**kw):
            self.assertEqual(kw['max_tokens'],0)
            state=kw['prompt_cache'][0];total=len(tokens);processed=0
            cb=kw['prompt_progress_callback'];cb(0,total)
            while len(tokens)>1:
                n=min(prefill_step_size,len(tokens)-1)
                self.invocations.append(('prefill',len(state.state),n))
                state.state.extend(tokens[:n]);tokens=tokens[n:];processed+=n;cb(processed,total)
            self.invocations.append(('prefill-final',len(state.state),len(tokens)))
            state.state.extend(tokens);cb(total,total)
            return iter(())
        def stream(model,tokenizer,prompt,**kw):
            self.assertEqual(len(prompt),1)  # Original decoding call, not50-token suffix.
            state=kw['prompt_cache'][0];self.invocations.append(('decode-input',len(state.state),1))
            state.state.extend(prompt);self.decoded.append(deepcopy(state.state));state.state.append(777)
            yield types.SimpleNamespace(text='x',generation_tokens=1,finish_reason=None if incomplete else 'stop',
                prompt_tokens=1,prompt_tps=100.0,generation_tps=20.0)
        return {'mlx_lm':types.SimpleNamespace(stream_generate=stream),
            'mlx_lm.generate':types.SimpleNamespace(generate_step=prefill),
            'mlx_lm.models.cache':types.SimpleNamespace(make_prompt_cache=lambda m:[fixtures.Layer()]),
            'mlx_lm.sample_utils':types.SimpleNamespace(make_sampler=lambda temp:('greedy',temp))}

    def prompts(self):
        return ([1]*8192+[2]*1054+[8,9]+[6]*48, [1]*8192+[2]*1546+[8,9]+[6]*48)

    def test_cold_and_warm_preserve_original_model_call_partition(self):
        first,second=self.prompts();original=self.setup_runtime(False)
        with patch.dict(sys.modules,self.modules()):
            list(original.stream(first,max_tokens=4,check_deadline=lambda *_:None));cold=list(self.invocations)
            self.invocations.clear()
            list(original.stream(second,max_tokens=4,check_deadline=lambda *_:None));cold_second=list(self.invocations)
        r=self.setup_runtime()
        with patch.dict(sys.modules,self.modules()):
            list(r.stream(first,max_tokens=4,check_deadline=lambda *_:None));self.assertEqual(self.invocations,cold)
            self.invocations.clear()
            list(r.stream(second,max_tokens=4,check_deadline=lambda *_:None))
            self.assertEqual(self.invocations,[step for step in cold_second if step[1]>=8192])
            self.assertEqual(self.decoded,[first,second])
        self.assertEqual(r.checkpoint.cache[0].state,second[:8192])
        m=r.last_generation_metrics
        self.assertEqual(m['cache_policy'],VOLATILE_SUFFIX_POLICY)
        self.assertEqual(m['cache_lookup'],'exact_prefix')
        self.assertEqual(m['reused_input_tokens'],8192)
        self.assertEqual(m['processed_input_tokens'],len(second)-8192)
        self.assertEqual(m['generation_suffix_tokens'],1)
        self.assertEqual(m['transient_prefill_tokens'],len(second)-8193)
        self.assertTrue(m['full_prompt_preserved'])

    def test_changed_history_recomputes_without_recurrent_trim(self):
        r=self.setup_runtime();first,second=self.prompts();second[0]=55
        with patch.dict(sys.modules,self.modules()):
            list(r.stream(first,max_tokens=4,check_deadline=lambda *_:None));self.invocations.clear()
            list(r.stream(second,max_tokens=4,check_deadline=lambda *_:None))
        self.assertEqual(self.invocations[0],('prefill',0,2048))
        self.assertEqual(self.decoded,[first,second])
        self.assertEqual(r.last_generation_metrics['reused_input_tokens'],0)

    def test_abort_discards_checkpoint_and_next_call_starts_fresh(self):
        r=self.setup_runtime();first,second=self.prompts()
        with patch.dict(sys.modules,self.modules(incomplete=True)):
            stream=r.stream(first,max_tokens=4,check_deadline=lambda *_:None);next(stream);stream.close()
        self.assertIsNone(r.checkpoint.cache)
        with patch.dict(sys.modules,self.modules()):
            list(r.stream(second,max_tokens=4,check_deadline=lambda *_:None))
        self.assertEqual(r.last_generation_metrics['reused_input_tokens'],0)
        self.assertEqual(r.last_generation_metrics['cache_lookup'],'empty')

    def test_extended_stable_grid_captures_new_boundary_without_mutating_prior(self):
        r=self.setup_runtime();first,_=self.prompts();second=[1]*8192+[2]*3100+[8,9]+[6]*48
        with patch.dict(sys.modules,self.modules()):
            list(r.stream(first,max_tokens=4,check_deadline=lambda *_:None));old=r.checkpoint.cache
            list(r.stream(second,max_tokens=4,check_deadline=lambda *_:None))
        self.assertEqual(old[0].state,first[:8192])
        self.assertEqual(r.checkpoint.cache[0].state,second[:10240])
        self.assertEqual(r.last_generation_metrics['new_checkpoint_tokens'],2048)

    def test_prefill_exception_drops_saved_state(self):
        r=self.setup_runtime();first,second=self.prompts()
        with patch.dict(sys.modules,self.modules()):list(r.stream(first,max_tokens=4,check_deadline=lambda *_:None))
        count=[0]
        def check(*_):
            count[0]+=1
            if count[0]>3:raise TimeoutError('synthetic prefill cancellation')
        with patch.dict(sys.modules,self.modules()),self.assertRaises(TimeoutError):
            list(r.stream(second,max_tokens=4,check_deadline=check))
        self.assertIsNone(r.checkpoint.cache)
