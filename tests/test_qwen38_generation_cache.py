"""Native generation-suffix checkpoints with CPU state fakes; no inference."""
import sys
import unittest
from unittest.mock import patch

import test_qwen38_compaction_cache as fixtures
from locua.engine.prototype.qwen38_runtime import checkpoint_boundary


class GenerationBoundaryTests(unittest.TestCase):
    def test_only_exact_terminal_suffix_selects_boundary_without_changing_input(self):
        tokens=[1,2,7,8,9];original=tokens[:]
        self.assertEqual(checkpoint_boundary(tokens,generation_suffix_tokens=[7,8,9]),
                         (2,'before_native_generation_suffix'))
        self.assertEqual(tokens,original)
        for prompt in ([1,7,8,9,4],[1,7,8],[1,7,8,0]):
            self.assertEqual(checkpoint_boundary(prompt,generation_suffix_tokens=[7,8,9]),
                             (len(prompt)-1,'before_final_input_token'))

    def test_earlier_compaction_boundary_wins_and_ambiguous_notice_does_not_guess(self):
        self.assertEqual(checkpoint_boundary([1,4,5,2,7,8,9],[4,5],generation_suffix_tokens=[7,8,9]),
                         (1,'before_compaction_notice'))
        self.assertEqual(checkpoint_boundary([1,4,5,2,4,5,7,8,9],[4,5],generation_suffix_tokens=[7,8,9]),
                         (6,'before_native_generation_suffix'))

    def test_invalid_suffix_tokens_fail_explicitly(self):
        for marker in ([],[True],(7,8,9),'suffix'):
            with self.subTest(marker=marker),self.assertRaises(ValueError):
                checkpoint_boundary([1,2,3],generation_suffix_tokens=marker)


class GenerationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.VolatileRuntimeTests()
        self.runtime=self.fixture.setup_runtime()
        self.runtime.generation_suffix_tokens=[7,8,9]
        # The old call's generation suffix is replaced by native assistant/tool
        # history in the next call, exactly as preserve_thinking=False does.
        self.first=[1]*3020+[7,8,9]
        self.second=[1]*3020+[70,80]+[2]*500+[7,8,9]

    def run_stream(self,runtime,prompt):
        return list(runtime.stream(prompt,max_tokens=4,check_deadline=lambda *_:None))

    def test_replayed_suffix_reuses_exact_prefix_preserving_all_tokens_and_partitions(self):
        baseline=self.fixture.setup_runtime(False)
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(baseline,self.first)
            self.assertEqual(baseline.last_generation_metrics['reused_input_tokens'],0)
            cold_first=list(self.fixture.invocations);self.fixture.invocations.clear()
            self.run_stream(baseline,self.second)
            cold_second=list(self.fixture.invocations)
            self.assertEqual(baseline.last_generation_metrics['reused_input_tokens'],0)
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(self.runtime,self.first)
            self.assertEqual(self.fixture.invocations,cold_first)
            prior=self.runtime.checkpoint.cache
            self.fixture.invocations.clear()
            self.run_stream(self.runtime,self.second)
            self.assertEqual(self.fixture.invocations,[s for s in cold_second if s[1]>=2048])
            self.assertEqual(self.fixture.decoded,[self.first,self.second])
        metrics=self.runtime.last_generation_metrics
        self.assertEqual(metrics['checkpoint_boundary_basis'],'before_native_generation_suffix_aligned_2048')
        self.assertEqual(metrics['reused_input_tokens'],2048)
        self.assertEqual(metrics['processed_input_tokens'],len(self.second)-2048)
        self.assertEqual(metrics['generation_suffix_tokens'],1)
        self.assertTrue(metrics['full_prompt_preserved'])
        self.assertEqual(prior[0].state,self.first[:2048])
        self.assertEqual(self.runtime.checkpoint.cache[0].state,self.second[:2048])

    def test_changed_or_compacted_history_cannot_reuse_stale_recurrent_state(self):
        for changed in ([3]+self.second[1:],[3]*2500+[7,8,9]):
            self.setUp()
            with patch.dict(sys.modules,self.fixture.modules()):
                self.run_stream(self.runtime,self.first)
                self.run_stream(self.runtime,changed)
                self.assertEqual(self.fixture.decoded,[self.first,changed])
            self.assertEqual(self.runtime.last_generation_metrics['reused_input_tokens'],0)
            self.assertEqual(self.runtime.last_generation_metrics['cache_lookup'],'identity_or_prefix_mismatch')

    def test_compaction_notice_precedes_terminal_generation_suffix(self):
        self.runtime.compaction_marker_tokens=[4,5]
        first=[1]*2500+[4,5]+[2]*1800+[7,8,9]
        second=[1]*2500+[4,5]+[3]*1900+[7,8,9]
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(self.runtime,first)
            self.run_stream(self.runtime,second)
            self.assertEqual(self.fixture.decoded,[first,second])
        self.assertEqual(self.runtime.last_generation_metrics['checkpoint_boundary_basis'],
                         'before_compaction_notice_aligned_2048')
        self.assertEqual(self.runtime.last_generation_metrics['reused_input_tokens'],2048)

    def test_missing_terminal_suffix_returns_to_full_prefix_and_separate_identity(self):
        changed=self.second[:-3]+[10,11,12]
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(self.runtime,self.first)
            self.run_stream(self.runtime,changed)
            self.assertEqual(self.fixture.decoded,[self.first,changed])
        self.assertEqual(self.runtime.last_generation_metrics['checkpoint_boundary_basis'],'marker_absent')
        self.assertEqual(self.runtime.last_generation_metrics['reused_input_tokens'],0)
        self.assertEqual(self.runtime.checkpoint.tokens,tuple(changed[:-1]))

    def test_short_prompts_keep_every_token_even_with_zero_saved_tokens(self):
        first=[1,7,8,9];second=[1,2,7,8,9]
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(self.runtime,first);self.run_stream(self.runtime,second)
            self.assertEqual(self.fixture.decoded,[first,second])
        self.assertEqual(self.runtime.last_generation_metrics['checkpoint_tokens'],0)
        self.assertEqual(self.runtime.last_generation_metrics['reused_input_tokens'],0)

    def test_aborted_generation_invalidates_saved_native_suffix_checkpoint(self):
        with patch.dict(sys.modules,self.fixture.modules(incomplete=True)):
            stream=self.runtime.stream(self.first,max_tokens=4,check_deadline=lambda *_:None)
            next(stream);stream.close()
        self.assertIsNone(self.runtime.checkpoint.cache)
        with patch.dict(sys.modules,self.fixture.modules()):
            self.run_stream(self.runtime,self.second)
        self.assertEqual(self.runtime.last_generation_metrics['reused_input_tokens'],0)


if __name__=='__main__':unittest.main()
