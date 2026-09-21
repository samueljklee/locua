#!/usr/bin/env python3
"""Disposable v7 browser fixtures. Setup only; never a Locua action adapter.

Own a NEW output directory. No expected answers or model/runtime imports.
Only input events POST complete observed field state; receipts are independent
of Locua's completion decision. No generic file-serving or gold endpoint.
"""
import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

INITIAL = {
    'project': {'workspace.headline': 'General project', 'workspace.reference': 'TEMPLATE-09',
                'workspace.approval': False, 'project.headline': 'Evening mapping trial',
                'project.reference': 'PR-204'},
    'release': {'channel.name': 'General updates', 'channel.public': False,
                'release.name': 'Internal build', 'release.public': True,
                'release.owner': 'North team'},
}
STYLE = '''<style>
*{box-sizing:border-box}body{font:17px system-ui;margin:0;color:#1e293b;background:#f5f7fa}
header{padding:24px 36px;background:#193549;color:white}h1{margin:0;font-size:28px}
main{padding:30px;max-width:1150px;margin:auto}label{display:block;margin:20px 0}
input:not([type=checkbox]),textarea{display:block;font:inherit;padding:10px;margin-top:7px;width:100%;border:1px solid #718096;border-radius:4px}
textarea{min-height:90px;resize:vertical}input[type=checkbox]{width:19px;height:19px;margin-right:10px}
fieldset,section{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:24px}
legend,h2{font-weight:650}button{font:inherit;padding:9px 15px;margin:8px 8px 8px 0}
.project-layout{display:grid;grid-template-columns:1fr 1.4fr;gap:26px}.release-layout{display:grid;grid-template-columns:270px 1fr;gap:35px}
aside{background:#e9eef4;padding:20px;border-radius:8px}aside fieldset{padding:15px}
.banner{font-size:14px;margin:12px 0;color:#526175}[role=status]{padding:18px 30px}
</style>'''
PAGES = {
    'project': '''<header><h1>Fieldwork project workspace</h1></header><main>
    <p class="banner">Local disposable project fixture. Changes are recorded automatically.</p>
    <div class="project-layout"><fieldset aria-label="Workspace defaults"><legend>Workspace defaults</legend>
    <label>Headline<input name="workspace.headline" value="General project"></label>
    <label>Reference<input name="workspace.reference" value="TEMPLATE-09"></label>
    <label><input type="checkbox" name="workspace.approval">Approval required</label></fieldset>
    <section role="group" aria-label="Project brief"><h2>Project brief</h2>
    <label>Reference<input name="project.reference" value="PR-204"></label>
    <label>Headline<textarea name="project.headline">Evening mapping trial</textarea></label>
    </section></div></main>''',
    'release': '''<header><h1>Research release console</h1></header><main>
    <p class="banner">Local disposable release fixture. Changes are recorded automatically.</p>
    <div class="release-layout"><aside aria-label="Channel sidebar"><h2>Channel overview</h2>
    <fieldset aria-label="Channel defaults"><legend>Channel defaults</legend>
    <label>Display name<input name="channel.name" value="General updates"></label>
    <label><input type="checkbox" name="channel.public">Public listing</label></fieldset></aside>
    <section role="group" aria-label="Release settings"><h2>Release settings</h2>
    <label><input type="checkbox" name="release.public" checked>Public listing</label>
    <label>Owner<input name="release.owner" value="North team"></label>
    <label>Display name<input name="release.name" value="Internal build"></label>
    </section></div></main>''',
}
SCRIPT = '''<div role="status" aria-live="polite">No changes recorded</div><script>
let pending=Promise.resolve();
document.addEventListener('input',()=>{
  const values={}; for(const input of document.querySelectorAll('input,textarea'))
    values[input.name]=input.type==='checkbox'?input.checked:input.value;
  // Queue each event's captured state so earlier requests cannot overwrite later ones.
  const body=JSON.stringify(values);
  pending=pending.then(async()=>{
    const response=await fetch('/record'+location.pathname,{method:'POST',
      headers:{'Content-Type':'application/json'},body});
    if(!response.ok)throw Error('recording rejected');
    document.querySelector('[role=status]').textContent='Changes recorded';
  }).catch(()=>{document.querySelector('[role=status]').textContent='Recording failed';});
});
</script>'''


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def validate_state(case, data):
    initial = INITIAL[case]
    if not isinstance(data, dict) or set(data) != set(initial):
        raise ValueError('all declared fields required')
    for key, value in data.items():
        if type(value) is not type(initial[key]):
            raise ValueError('field type differs')
        if isinstance(value, str) and len(value.encode('utf-8')) > 8192:
            raise ValueError('field too long')
    return deepcopy(data)


def render(case):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<title>Locua v7 local transfer fixture</title>' + STYLE + PAGES[case] + SCRIPT + '</html>').encode()


class ReceiptStore:
    def __init__(self, out):
        self.out = Path(out).expanduser().resolve()
        self.out.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(self.out, 0o700)
        self.lock = threading.Lock()
        self.sequence = 0

    def record(self, case, payload):
        values = validate_state(case, payload)
        with self.lock:
            self.sequence += 1
            entry = {'case': case, 'sequence': self.sequence,
                     'received_at_ns': time.time_ns(), 'values': values}
            with os.fdopen(os.open(self.out/'events.jsonl', os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600), 'a', encoding='utf-8') as file:
                file.write(json.dumps(entry, ensure_ascii=False, allow_nan=False)+'\n')
                file.flush(); os.fsync(file.fileno())
            fd, temporary = tempfile.mkstemp(dir=self.out, prefix='.receipt-')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as file:
                    json.dump(values, file, ensure_ascii=False, allow_nan=False, indent=2)
                    file.write('\n'); file.flush(); os.fsync(file.fileno())
                os.replace(temporary, self.out/(case+'.json'))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return entry


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            case = self.path.removeprefix('/')
            if self.path != '/'+case or case not in PAGES:
                self.send_error(404); return
            body = render(case)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            case = self.path.removeprefix('/record/')
            if self.path != '/record/'+case or case not in PAGES:
                self.send_error(404); return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 65536 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('bounded JSON body required')
                data = json.loads(self.rfile.read(length).decode('utf-8'), object_pairs_hook=unique_object)
                store.record(case, data)
            except (ValueError, UnicodeError):
                self.send_error(400); return
            self.send_response(204); self.end_headers()
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--out', type=Path, required=True, help='New private receipt directory; never reuse a prior run.')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    store = ReceiptStore(args.out)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(store))
    print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}', 'receipts':str(store.out)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
