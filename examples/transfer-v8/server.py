#!/usr/bin/env python3
"""Disposable loopback UI; never imports task requests, gold or runtime code."""
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

INITIAL = {
    'dispatch-board': {'morning.destination':'Harbor 02', 'morning.notice':False,
                       **{f'reference.{i:02}':f'EV-{i:03}' for i in range(1,21)},
                       'evening.destination':'Depot 11', 'evening.notice':False},
    'access-dialog': {'staff.badge':'Staff only','staff.downloads':False,
                      'visitor.badge':'Guest pending','visitor.downloads':True},
}
ROUTES = {'/dispatch':'dispatch-board', '/access':'access-dialog'}


def field(case, key, label):
    value=INITIAL[case][key]
    if type(value) is bool:
        return f'<label><input name="{key}" type="checkbox"'+(' checked' if value else '')+f'>{label}</label>'
    return f'<label>{label}<input name="{key}" value="{escape(value,quote=True)}"></label>'


def render(case):
    if case=='dispatch-board':
        body='<h1>Dispatch desk</h1><main><fieldset aria-label="Morning dispatch"><legend>Morning dispatch</legend>'
        body+=field(case,'morning.destination','Destination')+field(case,'morning.notice','Send notice')+'</fieldset>'
        body+='<fieldset aria-label="Evening dispatch"><legend>Evening dispatch</legend><div class="references">'
        body+=''.join(field(case,f'reference.{i:02}',f'Reference {i:02}') for i in range(1,21))+'</div>'
        body+=field(case,'evening.destination','Destination')+field(case,'evening.notice','Send notice')+'</fieldset></main>'
        script="document.addEventListener('input',()=>record('input'));"
    elif case=='access-dialog':
        body='<h1>Collection access</h1><main><fieldset aria-label="Staff collection"><legend>Staff collection</legend>'
        body+=field(case,'staff.badge','Badge text')+field(case,'staff.downloads','Allow downloads')+'</fieldset>'
        body+='<section role="group" aria-label="Visitor collection"><h2>Visitor collection</h2><button id="options" aria-haspopup="menu" aria-expanded="false">Access options</button><div id="menu" role="menu" hidden><button role="menuitem" id="edit">Edit access</button></div></section></main>'
        body+='<dialog aria-label="Visitor access"><h2>Visitor access</h2>'
        body+=field(case,'visitor.badge','Badge text')+field(case,'visitor.downloads','Allow downloads')+'<button id="save">Save access</button><p id="saved" role="status"></p></dialog>'
        script="""document.querySelector('#options').onclick=()=>{document.querySelector('#menu').hidden=false;document.querySelector('#options').setAttribute('aria-expanded','true');};
document.querySelector('#edit').onclick=()=>{document.querySelector('#menu').hidden=true;document.querySelector('#options').setAttribute('aria-expanded','false');document.querySelector('dialog').showModal();};
document.addEventListener('input',()=>record('input'));
document.querySelector('#save').onclick=()=>record('save').then(()=>document.querySelector('#saved').textContent='Visitor access saved');"""
    else:raise ValueError('unknown case')
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><title>'''+('Dispatch desk' if case=='dispatch-board' else 'Collection access')+'''</title>
<style>body{font:16px system-ui;color:#23313a;background:#f4f5f6;margin:22px;max-width:1180px}main{display:grid;grid-template-columns:300px 1fr;gap:20px}fieldset,section,dialog{background:white;border:1px solid #9ba5aa;padding:16px}label{display:block;margin:9px 0}input:not([type=checkbox]){display:block;width:94%;padding:5px;font:inherit}input[type=checkbox]{width:18px;height:18px;margin-right:8px}.references{display:grid;grid-template-columns:repeat(4,1fr);gap:4px 12px}.references label{font-size:12px;margin:4px 0}.references input{font-size:12px;padding:3px}button{font:inherit;padding:8px;margin:8px}dialog{width:440px}dialog::backdrop{background:#2228}</style>'''+body+'''<p id="transport" role="status">No changes recorded</p><script>
let pending=Promise.resolve();
function record(kind){const values={};for(const f of document.querySelectorAll('input[name]'))values[f.name]=f.type==='checkbox'?f.checked:f.value;
const body=JSON.stringify({kind,values});pending=pending.then(async()=>{const r=await fetch('/record/'''+case+'''',{method:'POST',headers:{'Content-Type':'application/json'},body});if(!r.ok)throw Error('record failed');document.querySelector('#transport').textContent='Changes recorded';});return pending;}
'''+script+'</script></html>'
    return html.encode('utf-8')


def unique_object(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise ValueError('duplicate field')
        out[key]=value
    return out


class Store:
    def __init__(self,out):
        self.out=Path(out).absolute();self.out.mkdir(mode=0o700,parents=True,exist_ok=False)
        os.chmod(self.out,0o700);self.lock=threading.Lock();self.counts={key:0 for key in INITIAL}

    def record(self,case,payload):
        if case not in INITIAL or not isinstance(payload,dict) or set(payload)!={'kind','values'}:raise ValueError('invalid payload')
        values=payload['values'];kind=payload['kind']
        if kind not in ('input','save') or (kind=='save' and case!='access-dialog'):raise ValueError('invalid event kind')
        if not isinstance(values,dict) or set(values)!=set(INITIAL[case]):raise ValueError('complete field set required')
        for k,v in values.items():
            if type(v) is not type(INITIAL[case][k]) or (isinstance(v,str) and len(v.encode())>8192):raise ValueError('invalid value')
        with self.lock:
            self.counts[case]+=1
            entry={'case_id':case,'sequence':self.counts[case],'kind':kind,'received_at_ns':time.time_ns(),'values':deepcopy(values)}
            with os.fdopen(os.open(self.out/(case+'.events.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a') as f:
                f.write(json.dumps(entry,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
            if case=='dispatch-board' or kind=='save':
                fd,tmp=tempfile.mkstemp(dir=self.out,prefix='.receipt-')
                try:
                    with os.fdopen(fd,'w') as f:json.dump(entry,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
                    os.replace(tmp,self.out/(case+'.json'))
                finally:
                    if os.path.exists(tmp):os.unlink(tmp)


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            if self.path not in ROUTES:self.send_error(404);return
            body=render(ROUTES[self.path]);self.send_response(200)
            for key,value in [('Content-Type','text/html; charset=utf-8'),('Cache-Control','no-store'),('Content-Length',str(len(body))),('X-Content-Type-Options','nosniff')]:self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
        def do_POST(self):
            try:
                if not self.path.startswith('/record/'):raise ValueError('unknown endpoint')
                length=int(self.headers.get('Content-Length','-1'))
                if not 0<=length<=65536 or self.headers.get_content_type()!='application/json':raise ValueError('bounded JSON required')
                store.record(self.path.removeprefix('/record/'),json.loads(self.rfile.read(length).decode(),object_pairs_hook=unique_object))
            except (ValueError,UnicodeError):self.send_error(400);return
            self.send_response(204);self.end_headers()
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--port',type=int,default=0);a=p.parse_args()
    if not 0<=a.port<=65535:p.error('invalid port')
    store=Store(a.out);server=ThreadingHTTPServer(('127.0.0.1',a.port),handler_for(store))
    print(json.dumps({'urls':{case:f'http://127.0.0.1:{server.server_port}{route}' for route,case in ROUTES.items()},'receipts':str(store.out)}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
