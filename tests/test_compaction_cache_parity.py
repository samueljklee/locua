"""Parity audit integrity checks only; no worker, tokenizer, or model load."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('cache_parity_probe',Path(__file__).resolve().parents[1]/'tools/qwen38_compaction_cache_parity_v2.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ParityChecks(unittest.TestCase):
    def records(self):
        return [{'source_call':n,'raw_output':'raw','parsed_blocks':[{'type':'text','text':'raw'}],
            'usage':{'input_tokens':100+n,'output_tokens':1},'request_sha256':'exact-request-'+str(n),
            'finish_reason':'stop','generation_calls':1,'dispatched':False,
            'timing':{'generation_ms':50},'generation_metrics':{'full_input_tokens':100+n,
                'full_prompt_preserved':True,'checkpoint_boundary_basis':'before_compaction_notice_aligned_2048',
                'generation_suffix_tokens':1,'checkpoint_tokens':8192,'prefill_chunk_size':2048,
                'cache_lookup':'empty' if n==7 else 'exact_prefix','reused_input_tokens':0 if n==7 else 50,
                'prefill_ms':20,'decode_ms':5}} for n in probe.NUMBERS]

    def test_exact_outputs_full_usage_and_later_reuse_are_required(self):
        rows=self.records()
        self.assertTrue(all(r['passed'] for r in probe.compare(rows,deepcopy(rows))))
        for field,value in (('raw_output','changed'),('parsed_blocks',[]),('usage',{'input_tokens':1,'output_tokens':1}),
                            ('request_sha256','different'),('finish_reason','length'),('dispatched',True),('generation_calls',0)):
            changed=deepcopy(rows);changed[1][field]=value
            with self.subTest(field=field):self.assertFalse(probe.compare(rows,changed)[1]['passed'])

    def test_exact_output_alone_cannot_pass_without_cache_reuse(self):
        rows=self.records();changed=deepcopy(rows)
        changed[2]['generation_metrics']['reused_input_tokens']=0
        self.assertFalse(probe.compare(rows,changed)[2]['passed'])

    def test_native_suffix_basis_and_requested_numbers_are_explicit(self):
        rows=self.records()
        for number,row in enumerate(rows,1):
            row['source_call']=number
            row['generation_metrics']['checkpoint_boundary_basis']='before_native_generation_suffix_aligned_2048'
        compared=probe.compare(rows,deepcopy(rows),numbers=(1,2,3),
            boundary_basis='before_native_generation_suffix_aligned_2048')
        self.assertTrue(all(row['passed'] for row in compared))
        changed=deepcopy(rows);changed[1]['generation_metrics']['reused_input_tokens']=0
        self.assertFalse(probe.compare(rows,changed,numbers=(1,2,3),
            boundary_basis='before_native_generation_suffix_aligned_2048')[1]['passed'])
        self.assertFalse(probe.compare(rows,rows,numbers=(1,2,3))[0]['passed'])

    def test_missing_duplicate_or_reordered_attempts_refused(self):
        rows=self.records()
        for bad in (rows[:2],list(reversed(rows)),[rows[0],rows[0],rows[2]]):
            with self.assertRaises(ValueError):probe.compare(rows,bad)

    def test_mixed_wrapper_and_suffix_bases_are_frozen_per_call(self):
        rows=self.records();numbers=(8,9,10)
        bases=['before_native_empty_thinking_wrapper_aligned_2048']*2+['before_native_generation_suffix_aligned_2048']
        for row,number,basis in zip(rows,numbers,bases,strict=True):
            row['source_call']=number;row['generation_metrics']['checkpoint_boundary_basis']=basis
        self.assertTrue(all(r['passed'] for r in probe.compare(rows,deepcopy(rows),numbers=numbers,boundary_bases=bases)))
        changed=deepcopy(rows)
        changed[1]['generation_metrics']['checkpoint_boundary_basis']=bases[2]
        self.assertFalse(probe.compare(rows,changed,numbers=numbers,boundary_bases=bases)[1]['passed'])
        changed=deepcopy(rows);changed[2]['generation_metrics']['reused_input_tokens']=0
        self.assertFalse(probe.compare(rows,changed,numbers=numbers,boundary_bases=bases)[2]['passed'])
        for invalid in (bases[:2],bases+['bad'],['bad']*3):
            with self.assertRaises(ValueError):probe.compare(rows,rows,numbers=numbers,boundary_bases=invalid)

    def test_source_and_exact_input_bytes_are_frozen(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.py';source.write_text('original')
            inp=root/'input-007.json';inp.write_text('{"messages":[]}')
            freeze=root/'freeze.json';freeze.write_text(json.dumps({'source_hashes':{str(source):probe.digest(source)},
                'input_hashes':{inp.name:probe.digest(inp)}}))
            expected=probe.digest(freeze);probe.verify_freeze(root,expected)
            inp.write_text('{ "messages":[] }')
            with self.assertRaisesRegex(ValueError,'native request'):probe.verify_freeze(root,expected)
            inp.write_text('{"messages":[]}');source.write_text('changed')
            with self.assertRaisesRegex(ValueError,'source'):probe.verify_freeze(root,expected)
            source.write_text('original');freeze.write_text(freeze.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'freeze'):probe.verify_freeze(root,expected)
