#!/usr/bin/env python3
"""Export exact logged progressive-loop requests to unscored local eval cases.

Input is one private loop.jsonl with decision_request events. Old logs cannot be
reconstructed from response context. No labels are derived from responses,
actions or fixture oracles. Requests with no observed response are retained.
Requires an evaluator that preserves explicit history; unsupported replay fails
before output creation. No model, network, driver or GUI calls.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from locua.config import strict_json
from locua.engine.prototype.decision import validate_request
from locua.engine_adapter import _decision_cases

INPUT_KEYS = {'goal', 'observation_summary', 'candidates', 'history'}
MAX_BYTES = 100 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def convert_events(events):
    cases, evidence, waiting = [], [], {}
    for index, event in enumerate(events):
        if not isinstance(event, dict): raise ValueError('Trace events must be JSON objects')
        if event.get('type') == 'decision_request':
            request = event.get('request')
            if event.get('schema') != 'locua.decision_request.v1' or not isinstance(request, dict) or set(request) != INPUT_KEYS:
                raise ValueError('Exact goal, observation summary, full candidates and explicit history are required')
            validate_request(**request, max_context_tokens=event.get('max_context_tokens',8192))
            sha = digest(request)
            if event.get('request_sha256') != sha: raise ValueError('Logged request hash mismatch')
            if len(cases) == 100: raise ValueError('One export supports at most 100 requests; none were omitted')
            identity = f'decision-{len(cases)+1:04d}-{sha[:16]}'
            cases.append({'id':identity, **deepcopy(request)})
            row={'id':identity,'source_event_index':index,'phase':event.get('phase'),
                 'request_sha256':sha,'response_observed':False,
                 'original_max_context_tokens':event.get('max_context_tokens',8192)}
            evidence.append(row); waiting.setdefault(sha,[]).append(row)
        elif event.get('type') == 'decision':
            sha=event.get('request_sha256')
            if not isinstance(sha,str) or not waiting.get(sha):
                raise ValueError('Decision response has no matching exact request; legacy or mixed traces cannot be partially exported')
            row=waiting[sha].pop(0)
            if event.get('phase') != row['phase']: raise ValueError('Decision response/request phase mismatch')
            row['response_observed']=True
    if not cases: raise ValueError('No exact decision_request events; response context is insufficient')
    bundle={'kind':'decisions','cases':cases}
    try:
        _, replay_inputs=_decision_cases(bundle)
    except ValueError as error:
        raise ValueError('Current evaluator cannot accept the exact captured inputs; update replay history support rather than dropping fields') from error
    if replay_inputs != [{key:deepcopy(case[key]) for key in INPUT_KEYS} for case in cases]:
        raise ValueError('Evaluator changes captured request inputs; refusing semantic drift')
    manifest={'schema':'locua.decision_export.v1','case_count':len(cases),
              'requests_with_response':sum(x['response_observed'] for x in evidence),
              'requests_without_response':sum(not x['response_observed'] for x in evidence),
              'unreturned_requests_included':True,'expected_answers_inferred':False,
              'scored_cases':0,'model_calls':0,'gui_calls':0,
              'scope':'Exact model input fields; runtime/model configuration is chosen separately by eval',
              'requests':evidence}
    return bundle,manifest


def private_json(path, value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,allow_nan=False,indent=2);handle.write('\n')


def export(source, out):
    source, out=Path(source),Path(out)
    with source.open('rb') as stream: raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES: raise ValueError('Trace exceeds 100 MiB bound; nothing exported')
    lines=raw.decode('utf-8').splitlines()
    if any(not line.strip() for line in lines): raise ValueError('Blank trace records are not accepted')
    events=[strict_json(line) for line in lines]
    bundle,manifest=convert_events(events)
    manifest.update(source_sha256=hashlib.sha256(raw).hexdigest(),source_event_count=len(events))
    out.mkdir(parents=True,exist_ok=False,mode=0o700);os.chmod(out,0o700)
    private_json(out/'cases.json',bundle)
    private_json(out/'export-manifest.json',manifest)
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',required=True,type=Path,help='One private loop.jsonl')
    parser.add_argument('--out',required=True,type=Path,help='New private output directory')
    args=parser.parse_args(argv)
    result=export(args.events,args.out)
    print(json.dumps({k:result[k] for k in ('case_count','requests_with_response','requests_without_response','scored_cases')}))
    return 0


if __name__=='__main__':raise SystemExit(main())
