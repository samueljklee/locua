#!/usr/bin/env python3
"""One local development fixture. No models, Locua actions or expected outputs."""
import argparse
from copy import deepcopy
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
INITIAL = json.loads((HERE / 'initial.json').read_text())


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def validate(values):
    if not isinstance(values, dict) or set(values) != set(INITIAL):
        raise ValueError('complete declared state required')
    for key, value in values.items():
        if type(value) is not type(INITIAL[key]):
            raise ValueError('incorrect field type')
        if isinstance(value, str) and len(value.encode('utf-8')) > 8192:
            raise ValueError('text too long')
    return deepcopy(values)


def render():
    def room(prefix):
        return f'<label>Room<input name="{prefix}.room" value="{escape(INITIAL[prefix + ".room"], quote=True)}"></label>'
    def reminder(prefix):
        checked = ' checked' if INITIAL[prefix + '.reminder'] else ''
        return f'<label><input type="checkbox" name="{prefix}.reminder"{checked}>Send reminder</label>'
    html = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Studio booking board</title>
<style>body{font:18px system-ui;background:#f4f2ef;color:#243138;margin:30px auto;max-width:1000px}
main{display:grid;grid-template-columns:1fr 1fr;gap:24px}fieldset,section,aside{padding:22px;border:1px solid #8b969b;background:white}
label{display:block;margin:20px 0}input:not([type=checkbox]){display:block;font:inherit;padding:9px;width:90%;margin-top:7px}
input[type=checkbox]{width:19px;height:19px;margin-right:10px}h2,legend{font-weight:650}aside{margin-top:24px}</style>
<h1>Studio booking board</h1><p>Disposable local booking form. Changes are recorded automatically.</p><main>
<fieldset aria-label="Soundcheck"><legend>Soundcheck</legend>''' + room('soundcheck') + reminder('soundcheck') + '''</fieldset>
<section role="group" aria-label="Recording"><h2>Recording</h2>''' + reminder('recording') + room('recording') + '''</section></main>
<aside role="group" aria-label="Coordination"><h2>Coordination</h2><label>Coordination note<input name="admin.note" value="''' + escape(INITIAL['admin.note'], quote=True) + '''"></label></aside>
<p role="status" aria-live="polite">No changes recorded</p><script>
let pending=Promise.resolve();
document.addEventListener('input',()=>{
  const state={};for(const field of document.querySelectorAll('input'))
    state[field.name]=field.type==='checkbox'?field.checked:field.value;
  const body=JSON.stringify(state);
  pending=pending.then(async()=>{
    const response=await fetch('/record',{method:'POST',headers:{'Content-Type':'application/json'},body});
    if(!response.ok)throw Error('recording rejected');
    document.querySelector('[role=status]').textContent='Changes recorded';
  }).catch(()=>{document.querySelector('[role=status]').textContent='Recording failed';});
});
</script></html>'''
    return html.encode('utf-8')


class ReceiptStore:
    def __init__(self, out):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(self.out, 0o700)
        self.sequence = 0
        self.lock = threading.Lock()

    def record(self, payload):
        values = validate(payload)
        with self.lock:
            self.sequence += 1
            entry = {'sequence':self.sequence, 'received_at_ns':time.time_ns(), 'values':values}
            with os.fdopen(os.open(self.out/'events.jsonl', os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600), 'a') as file:
                file.write(json.dumps(entry, ensure_ascii=False)+'\n');file.flush();os.fsync(file.fileno())
            fd, temporary = tempfile.mkstemp(dir=self.out, prefix='.receipt-')
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(values, file, ensure_ascii=False, indent=2);file.write('\n');file.flush();os.fsync(file.fileno())
                os.replace(temporary, self.out/'receipt.json')
            finally:
                if os.path.exists(temporary):os.unlink(temporary)


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):pass
        def do_GET(self):
            if self.path != '/':self.send_error(404);return
            body = render();self.send_response(200)
            for key, value in [('Content-Type','text/html; charset=utf-8'),('Cache-Control','no-store'),
                               ('X-Content-Type-Options','nosniff'),('Content-Length',str(len(body)))]:
                self.send_header(key, value)
            self.end_headers();self.wfile.write(body)
        def do_POST(self):
            if self.path != '/record':self.send_error(404);return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 65536 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('bounded JSON required')
                store.record(json.loads(self.rfile.read(length).decode('utf-8'), object_pairs_hook=unique_object))
            except (ValueError, UnicodeError):self.send_error(400);return
            self.send_response(204);self.end_headers()
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--out', type=Path, required=True, help='New private receipt directory.')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:parser.error('port must be 0..65535')
    store = ReceiptStore(args.out)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(store))
    print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/','receipts':str(store.out)}), flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__ == '__main__':main()
