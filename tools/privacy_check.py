#!/usr/bin/env python3
"""Offline, redacted privacy check for tracked/index files or reachable Git history.

This is a conservative source-publication check, not a guarantee against every
secret format or personal fact. It never prints matched values or commit emails.
"""
import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

TOKENS = re.compile(r'(?<![\w-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{25,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{15,})')
KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')
HOME = re.compile(r'(?:/Users/|/home/|[A-Z]:\\Users\\)([A-Za-z0-9_.-]+)')
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
EXAMPLE_USERS = {'private', 'user', 'username', 'example', 'test', 'runner', 'alice', 'bob'}
PRIVATE_DIRS = {'artifacts', 'results', 'logs', 'captures', 'screenshots', 'recordings', 'models', 'model-cache', '.venv', '.cache'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.safetensors', '.gguf'}


def safe_email_domain(domain):
    return domain in {'example.com', 'example.org', 'example.net', 'users.noreply.github.com'} or domain.endswith(('.test', '.invalid', '.example'))


def inspect_content(path, data, *, nested=False):
    findings = []
    def add(kind, line=None):
        findings.append({'path': path, 'kind': kind, **({'line': line} if line else {})})
    p = PurePosixPath(path)
    if not nested and (p.parts[0] in PRIVATE_DIRS or p.suffix in PRIVATE_SUFFIXES or
            p.name in {'keys.env', '.env', 'credentials.json', 'secrets.json'} or p.name.startswith('.env.') or
            path == 'config.json'):
        add('private_file_class')
    if data.startswith(b'PK\x03\x04') and not nested:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 20_000_000:
                    add('archive_requires_manual_review'); return findings
                for info in archive.infolist():
                    if not info.is_dir():
                        findings.extend(inspect_content(path + '::' + info.filename, archive.read(info), nested=True))
        except (OSError, ValueError, zipfile.BadZipFile):
            add('archive_requires_manual_review')
        return findings
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError:
        add('binary_requires_manual_review'); return findings
    for n, line in enumerate(content.splitlines(), 1):
        if TOKENS.search(line): add('credential_like_token', n)
        if KEY.search(line): add('private_key', n)
        if any(m.group(1).lower() not in EXAMPLE_USERS for m in HOME.finditer(line)):
            add('literal_personal_home_path', n)
        if any(not safe_email_domain(m.group(1).lower()) and m.group(0) not in {'noreply@github.com', 'codex@openai.com'} for m in EMAIL.finditer(line)):
            add('non_example_email_requires_review', n)
        if nested and p.name in {'core.xml', 'app.xml'} and re.search(r'<(?:dc:creator|cp:lastModifiedBy|Company)>[^<]+<', line):
            add('document_author_metadata_requires_review', n)
    return findings


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def scan(root, *, staged=False, history=False):
    findings = []; files = {}
    if history:
        for commit in git(root, 'rev-list', '--all').decode().splitlines():
            for row in git(root, 'ls-tree', '-rz', commit).split(b'\0'):
                if not row: continue
                meta, path = row.split(b'\t', 1); _, kind, oid = meta.decode().split()
                if kind == 'blob': files[(path.decode(), oid)] = None
            emails = git(root, 'show', '-s', '--format=%ae%n%ce', commit).decode().splitlines()
            if any(e not in {'noreply@github.com', 'codex@openai.com'} and
                   not e.endswith('@users.noreply.github.com') for e in emails):
                findings.append({'commit': commit, 'kind': 'personal_commit_email_requires_review'})
    else:
        for row in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
            if not row: continue
            meta, path = row.split(b'\t', 1); _, oid, stage = meta.decode().split()
            if stage != '0': raise ValueError('Resolve the unmerged index before checking publication')
            files[(path.decode(), oid)] = None
    for path, oid in files:
        data = git(root, 'cat-file', 'blob', oid) if staged or history else (root / path).read_bytes()
        rows = inspect_content(path, data)
        if history:
            for row in rows: row['blob'] = oid
        findings.extend(rows)
    return {'scope': 'reachable-history' if history else 'index' if staged else 'tracked-working-files',
            'file_versions_scanned': len(files), 'passed': not findings, 'findings': findings,
            'limitation': 'Heuristic source check; manual review remains necessary. Matches contain no secret values.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--staged', action='store_true', help='Read the exact complete index, including force-added ignored files.')
    mode.add_argument('--history', action='store_true', help='Include reachable past blobs and author/committer email checks.')
    args = parser.parse_args(argv)
    report = scan(args.root.resolve(), staged=args.staged, history=args.history)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
