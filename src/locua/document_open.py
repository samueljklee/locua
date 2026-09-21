"""Explicit TextEdit/plain-text adapter for controlled open and verified save.

A new observed window and complete matching initial buffer bind an exact file.
This does not infer document URLs from titles or claim arbitrary native support.
"""
from pathlib import Path
import subprocess
import time

from .engine.prototype.cua import CuaAdapter
from .engine.prototype.cli import private_json
from .native_save import prepare_binding, save, _read_file

# In-process bindings are intentionally not accepted from arbitrary task JSON.
_BINDINGS = {}


def open_document(raw_path, config, out, progress):
    from .engine_adapter import owner
    path=Path(raw_path).expanduser()
    if not path.is_absolute() or path.suffix.lower()!='.txt' or path.is_symlink():
        raise ValueError('Choose an absolute existing UTF-8 .txt path, without a leaf symlink')
    path=path.resolve(strict=True)
    before,text=_read_file(path)
    if len(text)>8192:
        raise ValueError('Guided document editing currently supports up to 8192 characters')
    connection=owner(config,out)
    try:
        _,initial=connection.call('list_windows',{})
        prior=[{'pid':w['pid'],'window_id':w['window_id']} for w in initial['windows']]
        opened=time.time_ns()
        progress('Opening existing plain-text document in background TextEdit: '+str(path))
        launch=subprocess.run(['/usr/bin/open','-g','-a','/System/Applications/TextEdit.app',str(path)],capture_output=True,text=True,timeout=15)
        if launch.returncode:
            raise ValueError('TextEdit open failed: '+launch.stderr.strip())
        candidates=[];observation=None;target=None
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            _,current=connection.call('list_windows',{})
            candidates=[w for w in current['windows'] if w.get('app_name')=='TextEdit'
                        and {'pid':w['pid'],'window_id':w['window_id']} not in prior
                        and w.get('title')==path.name and w.get('is_on_screen') is True]
            if len(candidates)>1:
                raise ValueError('Controlled open produced ambiguous document windows; no edit authorized')
            if len(candidates)==1:
                target={k:candidates[0][k] for k in ('pid','window_id')}
                observation=CuaAdapter(connection,kind='native_window_state',target=target,regions=True).observe()
                editors=[c for c in observation['controls'] if c['role']=='AXTextArea'
                         and c.get('value')==text and c.get('value_evidence',{}).get('exact_value_proven') is True]
                if len(editors)>1:
                    raise ValueError('New document has multiple matching exact editors')
                if len(editors)==1:
                    break
            time.sleep(.2)
        else:
            raise ValueError('New uniquely observed document window/exact buffer not available; no edit performed')
        now,again=_read_file(path)
        if now['identity']!=before['identity'] or again!=text:
            raise ValueError('Document file changed while opening; no edit authorized')
        evidence={'kind':'controlled_open','path':str(path),'target':target,'prior_windows':prior,
                  'opened_at_ns':opened,'command':['/usr/bin/open','-g','-a','/System/Applications/TextEdit.app',str(path)],
                  'return_code':launch.returncode,'initial_file':before,
                  'application_contract':{'id':'locua.textedit.plain_text.v1','application_path':'/System/Applications/TextEdit.app','bundle_id':'com.apple.TextEdit'}}
        binding=prepare_binding(path,target=target,observation=observation,editor_id=editors[0]['id'],open_evidence=evidence)
        _BINDINGS[binding.binding_id]=binding
        private_json(out/'observation.json',observation)
        private_json(out/'document-binding.json',binding.record())
        return {'status':'observed','scope':{'kind':'native',**target},'target_label':'TextEdit — '+str(path),
                'observation':observation,'artifacts':str(out),'desktop_edited':False,'document_binding_id':binding.binding_id,
                'document_binding':binding.record(),'document_editor_id':editors[0]['id'],'document_opened_by_locua':True,
                'document_window_left_open_for_review':True}
    finally:
        errors=connection.close()
        private_json(out/'cleanup.json',{'errors':errors,'document_window_policy':'left_open_for_user'})
        if errors:raise ValueError('Document discovery session cleanup failed: '+str(errors))


def save_prepared(preparation, expected, config, out, progress, *, save_route="menu"):
    from .engine_adapter import owner
    from .lib import _config
    out=Path(out);out.mkdir(mode=0o700,parents=True,exist_ok=False)
    key=preparation.get('document_binding_id');binding=_BINDINGS.pop(key,None)
    if binding is None:
        raise ValueError('Current in-process controlled-open binding missing; no save authorized')
    progress('Verifying document identity and exact editor text before native Save.')
    connection=owner(_config(config),out)
    try:
        result=save(connection,target=binding.target,binding=binding,expected=expected,save_route=save_route)
        result['saved_output_proven']=result.get('saved_file_proven') is True
        private_json(out/'save-result.json',result)
        progress('Native save: '+result['status']+'; verified disk bytes='+str(result['saved_output_proven']))
        return result
    finally:
        errors=connection.close()
        private_json(out/'cleanup.json',{'errors':errors,'document_window_policy':'left_open_for_user'})
        if errors:raise ValueError('Save session cleanup failed: '+str(errors))


def release_prepared(preparation):
    _BINDINGS.pop(preparation.get('document_binding_id'),None)
