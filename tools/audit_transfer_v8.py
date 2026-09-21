#!/usr/bin/env python3
"""Saved-output audit for the frozen v8 fixtures; no models or desktop calls.

Oracle implementation erratum: OOXML t="str" is string content, like inlineStr
and sharedStrings. Frozen verifier-v1 retains that raw storage tag; normalize it
here before exact typed-cell comparison. Requests, layout and expected values
are unchanged. The original verifier and freeze remain preserved.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];FIXTURE=ROOT/'examples/transfer-v8'
spec=importlib.util.spec_from_file_location('frozen_v8_verifier',FIXTURE/'verify.py')
frozen=importlib.util.module_from_spec(spec);spec.loader.exec_module(frozen)
verify_browser=frozen.verify_browser

def read_cells(path):
    return {k:('string',v) if t=='str' else (t,v) for k,(t,v) in frozen.read_cells(path).items()}

def verify_workbook(path):
    initial=read_cells(FIXTURE/'sample-register.xlsx');actual=read_cells(path)
    expected={**initial,'F7':('string','Ready — lot 09')}
    changed=[k for k in sorted(set(expected)|set(actual)) if expected.get(k)!=actual.get(k)]
    return {'schema':'locua.transfer-v8.workbook-oracle.v2','pass':not changed,'mismatched_cells':changed,
            'checked_cells':len(expected),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'evidence_plane':'saved_file','explicit_save_proven':False,
            'oracle_erratum':'Normalize OOXML t=str to the same string type as inlineStr/sharedStrings; no literal/layout change.',
            'limits':'Exact populated typed-cell/formula serialization only; no live cell binding, explicit Save, formatting or causality proof.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    b=s.add_parser('browser');b.add_argument('--case',choices=['dispatch-board','access-dialog'],required=True);b.add_argument('--receipts',type=Path,required=True)
    w=s.add_parser('workbook');w.add_argument('path',type=Path)
    a=p.parse_args();r=verify_browser(a.case,a.receipts) if a.command=='browser' else verify_workbook(a.path)
    print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(0 if r['pass'] else 1)

if __name__=='__main__':main()
