#!/usr/bin/env python3
"""Evaluator-only receipt comparison. Never imported by the fixture or model."""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def verify_freeze():
    manifest = json.loads((HERE/'freeze.json').read_text())
    for name, expected in manifest['files'].items():
        actual = hashlib.sha256((HERE/name).read_bytes()).hexdigest()
        if actual != expected:raise ValueError('Frozen source changed: '+name)
    return {'files':len(manifest['files']), 'freeze_sha256':hashlib.sha256((HERE/'freeze.json').read_bytes()).hexdigest()}


def exact(left, right):
    return (isinstance(left,dict) and set(left)==set(right)
            and all(type(left[key]) is type(value) and left[key]==value for key,value in right.items()))


def verify(receipts):
    freeze = verify_freeze()
    oracle = json.loads((HERE/'oracle.json').read_text())
    initial = json.loads((HERE/'initial.json').read_text())
    directory = Path(receipts)
    if not (directory/'receipt.json').is_file() or not (directory/'events.jsonl').is_file():
        return {'passed':False,'reason':'no_recorded_browser_input','freeze':freeze}
    final = json.loads((directory/'receipt.json').read_text())
    events = [json.loads(line) for line in (directory/'events.jsonl').read_text().splitlines()]
    journal_valid = bool(events) and all(
        type(event.get('sequence')) is int and event['sequence']==index
        and type(event.get('received_at_ns')) is int and event['received_at_ns']>0
        and isinstance(event.get('values'),dict) and set(event['values'])==set(initial)
        and all(type(event['values'][key]) is type(value) for key,value in initial.items())
        for index,event in enumerate(events,1))
    preserved = journal_valid and all(
        type(event['values'][key]) is type(initial[key]) and event['values'][key]==initial[key]
        for event in events for key in oracle['preserved_in_every_event'])
    final_matches = exact(final, oracle['expected'])
    last_matches = journal_valid and exact(final, events[-1]['values'])
    return {'passed':bool(final_matches and last_matches and preserved),
            'final_exact':final_matches,'journal_valid':journal_valid,'receipt_matches_last_event':bool(last_matches),
            'preserved_every_recorded_event':bool(preserved),'recorded_events':len(events),'checked_fields':len(initial),
            'classification':'separate_development_repair_validation','include_in_frozen_v7_denominator':False,
            'note':'Receipts prove recorded page state, not model choice, live dispatch or native saving.', 'freeze':freeze}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--receipts',type=Path,required=True)
    result=verify(parser.parse_args().receipts);print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
