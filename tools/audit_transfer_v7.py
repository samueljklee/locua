#!/usr/bin/env python3
"""Read-only independent v7 fixture output check. Never a model input or repair.

Checks frozen fixture files first. A matching saved artifact is one oracle gate;
model/guard/action provenance, exact target identity, assistance and cleanup still
need the separate run audit. This tool neither waits for nor generates success.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT/'examples/transfer-v7'
MAX_BYTES = 1024 * 1024


def pairs_unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate JSON key')
        out[key] = value
    return out


def read_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('expected an ordinary existing file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError('artifact exceeds 1 MiB bound')
    return raw


def read_json(path):
    return json.loads(read_bytes(path).decode('utf-8'), object_pairs_hook=pairs_unique)


def verify_freeze():
    freeze = read_json(FIXTURE/'freeze.json')
    if freeze.get('schema') != 'locua.transfer-v7.freeze.v1':
        raise ValueError('unknown freeze schema')
    for relative, expected in freeze['files'].items():
        path = ROOT/relative
        if path.resolve().is_relative_to(ROOT.resolve()) is False:
            raise ValueError('freeze path outside repository')
        if hashlib.sha256(read_bytes(path)).hexdigest() != expected:
            raise ValueError('frozen source changed: '+relative)
    return {'sha256':hashlib.sha256(read_bytes(FIXTURE/'freeze.json')).hexdigest(),
            'file_count':len(freeze['files'])}


def audit(case, path):
    frozen = verify_freeze()
    gold = read_json(FIXTURE/'oracle.json')['cases']
    if case not in gold:
        raise ValueError('unknown frozen case')
    expected = gold[case]
    raw = read_bytes(path)
    result = {'schema':'locua.transfer-v7.output-audit.v1','case':case,'path':str(Path(path).resolve()),
              'freeze':frozen,'actual_sha256':hashlib.sha256(raw).hexdigest(),
              'evidence_plane':expected['evidence_plane'],'gui_calls':0,'model_calls':0,
              'task_execution_proven':False,'cleanup_proven':False,'oracle_only':True}
    if expected['kind'] == 'browser':
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs_unique)
        if not isinstance(value, dict):
            raise ValueError('receipt must be an object')
        target = expected['fields']
        missing, extra = sorted(set(target)-set(value)), sorted(set(value)-set(target))
        mismatch = sorted(k for k in target if k in value and
                          (type(target[k]) is not type(value[k]) or target[k] != value[k]))
        result.update(status='pass' if not (missing or extra or mismatch) else 'fail',
                      missing_fields=missing,extra_fields=extra,mismatched_fields=mismatch,
                      expected_field_count=len(target),actual_field_count=len(value))
    else:
        target = read_bytes(FIXTURE/expected['expected_file'])
        result.update(status='pass' if raw == target else 'fail',
                      exact_bytes_equal=raw == target,expected_sha256=hashlib.sha256(target).hexdigest(),
                      expected_bytes=len(target),actual_bytes=len(raw),
                      explicit_save_mechanism_proven=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=('project','release','field-survey','release-note'))
    parser.add_argument('--file', type=Path, required=True, help='Recorded receipt JSON or intended saved text file.')
    parser.add_argument('--out', type=Path, help='Optional new private JSON result; never overwritten.')
    args = parser.parse_args()
    try:
        result = audit(args.case, args.file)
        if args.out:
            with os.fdopen(os.open(args.out, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600),'w') as stream:
                json.dump(result,stream,indent=2);stream.write('\n')
        print(json.dumps(result))
        return 0 if result['status']=='pass' else 1
    except (OSError,ValueError,KeyError) as error:
        print(json.dumps({'status':'error','error':str(error),'oracle_only':True}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
