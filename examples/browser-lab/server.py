"""Disposable local web UI for repeatable Locua runs. No external services.

Input events persist each form's exact strings to the explicit --out directory.
That independent receipt is useful for checking a run; the harness only claims
the evidence plane requested in its task. This fixture is not an app adapter.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile

STYLE = '<style>body{font:16px system-ui;margin:35px;max-width:1000px}main{display:flex;gap:40px}fieldset{padding:24px}label{display:block;margin:18px 0}input,button{font:inherit;padding:8px}dialog{padding:35px}nav{margin:24px 0}[hidden]{display:none!important}</style>'
PAGES = {
    '/contact': '''<h1>Office contacts</h1><nav aria-label="Workspace commands"><button>Export roster</button><button>Archive office</button><button>Help</button></nav><main>
    <fieldset aria-label="Shipping office"><legend>Shipping office</legend><label>Email<input name="shipping.email" value="shipping@example.test"></label><label>Cost centre<input name="shipping.cost" value="701"></label></fieldset>
    <fieldset aria-label="Billing office"><legend>Billing office</legend><label>Cost centre<input name="billing.cost" value="0046"></label><label>Email<input name="billing.email" value="billing@example.test"></label><label><input type="checkbox" name="billing.news">Receive news</label></fieldset></main>''',
    '/badge': '''<h1>Visitor management</h1><nav aria-label="Visitor commands"><button>Print register</button><button id="menu-button" aria-haspopup="menu" aria-expanded="false">Badge options</button><button>Help</button></nav>
    <div id="menu" role="menu" aria-label="Badge options menu" hidden><button role="menuitem" id="editor-button">Edit badge</button><button role="menuitem">Delete badge</button></div>
    <section aria-label="Default badge"><h2>Default badge</h2><label>Badge caption<input name="default.caption" value="Visitor"></label></section>
    <dialog id="editor" aria-label="Visitor badge"><h2>Visitor badge</h2><label>Badge caption<input name="visitor.caption" value="Guest"></label><label>Badge colour<input name="visitor.colour" value="Blue"></label><button id="close-button">Close badge</button></dialog>
    <script>document.querySelector('#menu-button').onclick=()=>{document.querySelector('#menu').hidden=false;document.querySelector('#menu-button').setAttribute('aria-expanded','true')};document.querySelector('#editor-button').onclick=()=>{document.querySelector('#menu').hidden=true;document.querySelector('#editor').showModal()};document.querySelector('#close-button').onclick=()=>document.querySelector('#editor').close();</script>'''
}
SCRIPT = '''<script>
document.addEventListener('input',async()=>{const data={};for(const input of document.querySelectorAll('input'))data[input.name]=input.type==='checkbox'?input.checked:input.value;await fetch('/record'+location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});});
</script>'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path)
            if body is None:
                self.send_error(404)
                return
            raw = ('<!doctype html><meta charset="utf-8"><title>Locua local fixture</title>' + STYLE + body + SCRIPT).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            key = self.path.removeprefix('/record')
            if key not in PAGES or not self.path.startswith('/record/'):
                self.send_error(404)
                return
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= 65536:
                self.send_error(400)
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or any(not isinstance(v, (str, bool)) for v in data.values()):
                self.send_error(400)
                return
            fd, name = tempfile.mkstemp(dir=args.out, prefix='.receipt-')
            with os.fdopen(fd, 'w') as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, args.out / (key[1:] + '.json'))
            self.send_response(204)
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'receipts': str(args.out.resolve())}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
