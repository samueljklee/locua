"""One-shot native save for a caller-bound existing UTF-8 plain-text document.

The frozen driver has no AXDocument URL. A caller must open the exact file,
record the prior windows, and bind the newly observed window and complete text
buffer. This is an explicit document adapter, not generic document discovery.
No file writes, automatic route fallback, application launch, model calls or retries.
An observed buffer, a native save acknowledgment and saved bytes are distinct.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .engine.prototype.perception import validate_observation, _native_value_evidence

MAX_BYTES = 1024 * 1024
TEXTEDIT_APPLICATION_CONTRACT = {
    'id':'locua.textedit.plain_text.v1',
    'application_path':'/System/Applications/TextEdit.app',
    'bundle_id':'com.apple.TextEdit',
}
TEXTEDIT_SAVE_SHORTCUT_SOURCE = 'https://support.apple.com/en-us/102650'


class NativeSaveError(ValueError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return {'device':info.st_dev,'inode':info.st_ino,'uid':info.st_uid}


def _read_file(path, parent_identity=None):
    """Stable strict UTF-8 read of one ordinary path; never follow a leaf symlink."""
    if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
        raise NativeSaveError('Document path changed or includes a symlink alias')
    parent = _identity(path.parent.stat())
    if parent_identity is not None and parent != parent_identity:
        raise NativeSaveError('Document parent directory identity changed')
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise NativeSaveError('Require one ordinary, unlinked-alias-free document of at most 1 MiB')
        with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read(MAX_BYTES+1)
        after=os.fstat(fd)
        keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if len(data)>MAX_BYTES or any(getattr(before,k)!=getattr(after,k) for k in keys):
            raise NativeSaveError('Document changed during readback')
        at_path=path.lstat()
        if not stat.S_ISREG(at_path.st_mode) or _identity(at_path)!=_identity(after):
            raise NativeSaveError('Document path was replaced during readback')
        if _identity(path.parent.stat())!=parent:
            raise NativeSaveError('Document parent changed during readback')
        text=data.decode('utf-8',errors='strict')
        return {'path':str(path),'identity':_identity(after),'parent_identity':parent,
                'bytes':len(data),'sha256':_hash(data),'mtime_ns':after.st_mtime_ns,
                'observed_at_ns':time.time_ns()},text
    finally:
        os.close(fd)


def _target(value):
    if (not isinstance(value,dict) or set(value)-{'pid','window_id','session'}
            or any(type(value.get(k)) is not int or value[k]<=0 for k in ('pid','window_id'))
            or ('session' in value and (not isinstance(value['session'],str) or not value['session']))):
        raise NativeSaveError('Exact native PID/window target required')
    return deepcopy(value)


def _validate_snapshot(observation,target):
    validate_observation(observation,expected_target=target,now_ns=time.time_ns(),max_age_s=30)
    if observation.get('kind')!='native_window_state':raise NativeSaveError('Native observation required')
    if observation.get('provenance',{}).get('raw_metadata',{}).get('degraded') is True:
        raise NativeSaveError('Degraded native window cannot bind a document')


def _editor_descriptor(control,observation):
    # The editor's own name may fall back to AXValue and change during editing.
    # Ancestor names, especially the document window title, must stay bound:
    # the same window can switch documents without changing editor geometry.
    by_id={c['id']:c for c in observation['controls']};ancestors=[];seen={control['id']};parent=control.get('parent')
    while parent is not None:
        if parent in seen or parent not in by_id:raise NativeSaveError('Editor ancestry is incomplete or cyclic')
        seen.add(parent);node=by_id[parent]
        ancestors.append({'role':node['role'],'name':node.get('name'),
                          'identifier':node.get('semantics',{}).get('identifier')})
        parent=node.get('parent')
    return {'role':control['role'],'identifier':control.get('semantics',{}).get('identifier'),
            'description':control.get('semantics',{}).get('description'),
            'help':control.get('semantics',{}).get('help'),'bounds':deepcopy(control.get('bounds')),
            'ancestors':ancestors}


def _exact_editor(control,observation):
    if control.get('role')!='AXTextArea':raise NativeSaveError('Plain-text document needs an addressed AXTextArea')
    handle=observation.get('handles',{}).get(control['id'])
    if not isinstance(handle,dict) or not isinstance(handle.get('element_token'),str):
        raise NativeSaveError('Document editor has no current native token')
    evidence=_native_value_evidence(control)
    source=control.get('source',{}).get('node',{}).get('editor',{})
    raw=source.get('raw_value',{});recheck=source.get('raw_value_recheck',{})
    if (evidence.get('precision')!='exact' or evidence.get('plane')!='editor_buffer'
            or control.get('value_evidence')!=evidence or control.get('value')!=raw.get('value')
            or raw.get('status')!='ok' or not isinstance(raw.get('value'),str)
            or recheck.get('status')!='ok' or recheck.get('value')!=raw['value']):
        raise NativeSaveError('Stable exact raw editor-buffer evidence required')
    return raw['value']


@dataclass
class PlainTextBinding:
    """In-process controlled-open receipt. Never deserialize this from a plan."""
    path: Path
    target: dict
    file: dict
    editor_descriptor: dict
    open_evidence: dict
    initial_snapshot_id: str
    binding_id: str=field(default_factory=lambda:'native-document-'+uuid.uuid4().hex)
    consumed: bool=False

    def record(self):
        return {'schema':'locua.plain_text_binding.v1','binding_id':self.binding_id,'path':str(self.path),
                'target':deepcopy(self.target),'initial_file':deepcopy(self.file),
                'editor_descriptor':deepcopy(self.editor_descriptor),'initial_snapshot_id':self.initial_snapshot_id,
                'open_evidence':deepcopy(self.open_evidence),'binding_basis':'caller_controlled_open_and_exact_initial_buffer',
                'driver_document_url_proven':False,'consumed':self.consumed}


def prepare_binding(path, *, target, observation, editor_id, open_evidence):
    """Bind an existing file BEFORE editing, after the caller's controlled open.

    open_evidence must contain kind='controlled_open', canonical path, target,
    prior_windows=[{pid,window_id},...], and opened_at_ns. This is caller evidence,
    not something a model, matching title, or arbitrary task JSON can assert.
    """
    target=_target(target);_validate_snapshot(observation,target)
    path=Path(path)
    if not path.is_absolute():raise NativeSaveError('Document path must be absolute')
    if path.is_symlink():raise NativeSaveError('Document leaf must not be a symlink')
    path=path.resolve(strict=True)
    if not isinstance(open_evidence,dict):raise NativeSaveError('Controlled-open evidence required')
    prior=open_evidence.get('prior_windows');opened=open_evidence.get('opened_at_ns')
    if (open_evidence.get('kind')!='controlled_open' or open_evidence.get('path')!=str(path)
            or open_evidence.get('target')!=target or not isinstance(prior,list)
            or type(opened) is not int or not 0<opened<=observation['observed_at_ns']
            or any(not isinstance(w,dict) or set(w)!={'pid','window_id'}
                   or any(type(w.get(k)) is not int or w[k]<=0 for k in ('pid','window_id')) for w in prior)):
        raise NativeSaveError('Controlled-open path, target or timing evidence is invalid')
    if any(w=={k:target[k] for k in ('pid','window_id')} for w in prior):
        raise NativeSaveError('Controlled open did not produce a new bound window')
    controls=[c for c in observation['controls'] if c['id']==editor_id]
    if len(controls)!=1:raise NativeSaveError('Initial document editor is not uniquely addressed')
    value=_exact_editor(controls[0],observation);descriptor=_editor_descriptor(controls[0],observation)
    competitors=[c for c in observation['controls'] if c.get('role')=='AXTextArea'
                 and _editor_descriptor(c,observation)==descriptor]
    if len(competitors)!=1:raise NativeSaveError('Initial document editor descriptor is ambiguous')
    file,text=_read_file(path)
    if text!=value:raise NativeSaveError('Initial raw editor buffer differs from the existing UTF-8 document')
    return PlainTextBinding(path,target,file,descriptor,deepcopy(open_evidence),observation['snapshot_id'])


def _observed_menu(observation,path):
    if (not isinstance(path,list) or not 2<=len(path)<=16
            or any(not isinstance(s,str) or not s or s!=s.strip() for s in path)
            or path[-1] not in ('Save','Save…','Save...')):
        raise NativeSaveError('Explicit observed Save path required; Save As and other commands unsupported')
    by_id={c['id']:c for c in observation['controls']};matches=[]
    for control in observation['controls']:
        if control.get('role')!='AXMenuItem' or control.get('name')!=path[-1]:continue
        current=control;labels=[];seen=set();menu_bar=False;enabled=True
        while current is not None:
            if current['id'] in seen:raise NativeSaveError('Menu hierarchy cycle')
            seen.add(current['id'])
            if current.get('states',{}).get('enabled') is False:enabled=False
            if current['role'] in ('AXMenuItem','AXMenuBarItem'):
                labels.append(current.get('name'))
            if current['role']=='AXMenuBar':menu_bar=True;break
            current=by_id.get(current.get('parent'))
        if menu_bar and list(reversed(labels))==path:matches.append((control,enabled))
    if len(matches)!=1 or not matches[0][1]:
        raise NativeSaveError('Save menu path absent, ambiguous or explicitly disabled in fresh observation')
    # Closed menu items often have markdown-only identities. No writable handle
    # is invented: invoke_menu re-resolves every live immediate child itself.
    return {'control_id':matches[0][0]['id'],'path':list(path),'enabled_observed':matches[0][0].get('states',{}).get('enabled')}


def _fresh_editor(observe,target,binding,expected,*,after_ns=None):
    observation=observe();_validate_snapshot(observation,target)
    if after_ns is not None and observation['observed_at_ns']<after_ns:
        raise NativeSaveError('Post-focus observation must be newly captured after activation')
    matches=[c for c in observation['controls'] if c.get('role')=='AXTextArea'
             and _editor_descriptor(c,observation)==binding.editor_descriptor]
    if len(matches)!=1:raise NativeSaveError('Bound document editor changed or became ambiguous')
    if _exact_editor(matches[0],observation)!=expected:
        raise NativeSaveError('Fresh exact editor buffer does not equal requested complete document')
    return observation,matches[0]


def _focus_verified(payload,target):
    """Pinned bring_to_front output, not request acceptance or app-only focus."""
    if not isinstance(payload,dict):return False
    effect=payload.get('exact_window_effect');observed=payload.get('observed')
    return (payload.get('status')=='activated'
            and payload.get('code')=='bring_to_front_exact_window_verified'
            and payload.get('pid')==target['pid'] and payload.get('window_id')==target['window_id']
            and payload.get('activated') is True and payload.get('process_activated') is True
            and isinstance(effect,dict)
            and all(effect.get(k) is True for k in ('verified','focused','frontmost_ordinary','target_visible_ordinary'))
            and isinstance(observed,dict) and observed.get('frontmost_pid')==target['pid']
            and observed.get('focused_window_id')==target['window_id']
            and observed.get('frontmost_ordinary_window_id')==target['window_id'])


def _save_acknowledged(payload,save_route):
    # invoke_menu's frozen success branch is AX final-action acceptance,
    # published as an unverifiable accessibility action delivered foreground.
    # This does not prove semantic Save or that Save caused persisted bytes.
    effects=('unverifiable',) if save_route=='menu' else ('unverifiable','confirmed')
    route='accessibility' if save_route=='menu' else 'global_input'
    return (isinstance(payload,dict) and payload.get('effect') in effects
            and payload.get('route')==route
            and isinstance(payload.get('delivery'),dict)
            and payload['delivery'].get('mode')=='foreground'
            and payload.get('status')!='refused' and not payload.get('refusal'))


def _textedit_contract(binding):
    evidence=binding.open_evidence
    contract=evidence.get('application_contract')
    app=TEXTEDIT_APPLICATION_CONTRACT['application_path']
    commands=(['/usr/bin/open','-g','-a',app,str(binding.path)],
              ['/usr/bin/open','-g','-n','-a',app,str(binding.path)])
    if (not isinstance(contract,dict)
            or any(contract.get(k)!=v for k,v in TEXTEDIT_APPLICATION_CONTRACT.items())
            or evidence.get('command') not in commands
            or binding.path.suffix.lower()!='.txt'):
        raise NativeSaveError('TextEdit shortcut requires an exact background controlled-open application contract')


def _refusal(error):
    from .engine.prototype.cua import CuaRefusal
    if not isinstance(error,CuaRefusal):return None
    payload=error.payload
    if not isinstance(payload,dict):return None
    if payload.get('status')=='refused' or payload.get('effect')=='refused' or payload.get('refusal'):
        return deepcopy(payload)
    # E.g. action_outcome_mismatch may follow execution: do not turn it into
    # a claim that the driver refused before action.
    return None


def _finish_readback(owner,report,binding,expected,before,timeout_s,poll_interval_s):
    deadline=time.monotonic()+timeout_s
    while True:
        try:
            after,text=_read_file(binding.path,binding.file['parent_identity'])
            report['file_after']=after
            if text==expected:
                acknowledged=report['save_command_dispatched'] and report['post_buffer_proven']
                report.update(status='saved' if acknowledged else 'partial',saved_file_proven=True,
                              saved_plane='saved_file',explicit_save_proven=acknowledged,
                              atomic_path_replacement_observed=after['identity']!=before['identity'])
                if not acknowledged:report.setdefault('reason','saved_bytes_observed_but_explicit_save_unproved')
                break
            if after['sha256']!=before['sha256']:
                report.update(status='unknown_effect',reason='unexpected_saved_bytes');break
        except FileNotFoundError:
            report['readback_temporarily_absent']=True
        except (OSError,UnicodeError,NativeSaveError) as error:
            report.update(status='unknown_effect',reason='file_identity_or_readback_failed',readback_error_type=type(error).__name__);break
        if time.monotonic()>=deadline:
            status='refused' if report.get('save_command_effect')=='refused' or report.get('focus_effect')=='refused' else 'unknown_effect'
            report.update(status=status)
            report.setdefault('reason','expected_bytes_not_observed');break
        time.sleep(min(poll_interval_s,max(0,deadline-time.monotonic())))
    trace=getattr(owner,'trace',None)
    if callable(trace):trace({'type':'native_save_result','result':deepcopy(report)})
    return report


def save(owner, *, target, binding, expected, menu_path=None, observe=None, timeout_s=3, poll_interval_s=.1,
         prefer_command=True,save_route='menu'):
    """Validate, focus one exact window, reobserve, issue one Save, read bytes.

    `observe` is a read-only test/adapter seam; default uses the owner's exact
    native target without screenshots. Expected is the complete UTF-8 document.
    The menu route explicitly prepares persistent exact-window focus. The
    separately selected TextEdit shortcut uses the driver's temporary guarded
    foreground key route; it never falls back from menu or leaves focus as a
    deliberate preparation step. `explicit_save_proven` means the selected
    native operation was acknowledged, a fresh exact buffer still matched,
    and exact saved bytes were observed. Shortcut acknowledgment proves a HID
    post, not that a menu handler consumed it. Causality remains unproved.
    """
    target=_target(target)
    if not isinstance(binding,PlainTextBinding) or binding.target!=target or binding.consumed:
        raise NativeSaveError('Matching unconsumed controlled-open binding required')
    if save_route not in ('menu','textedit_shortcut'):
        raise NativeSaveError('Choose an explicit supported native Save route')
    if save_route=='textedit_shortcut':_textedit_contract(binding)
    if not isinstance(expected,str) or len(expected.encode('utf-8'))>MAX_BYTES:
        raise NativeSaveError('Expected complete plain-text UTF-8 document must fit 1 MiB')
    if type(prefer_command) is not bool:raise NativeSaveError('prefer_command must be boolean')
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (timeout_s,poll_interval_s)) or not .1<=timeout_s<=5 or not .02<=poll_interval_s<=.5:
        raise NativeSaveError('Persistence polling bounds are invalid')
    if observe is None:
        from .engine.prototype.cua import CuaAdapter
        observe=CuaAdapter(owner,kind='native_window_state',target=target,execute=False,ocr=False).observe
    observation,editor=_fresh_editor(observe,target,binding,expected)
    before,text=_read_file(binding.path,binding.file['parent_identity'])
    expected_hash=_hash(expected.encode('utf-8'))
    report={'schema':'locua.native_save.v1','binding_id':binding.binding_id,'target':target,'path':str(binding.path),
            'buffer':{'plane':'editor_buffer','snapshot_id':observation['snapshot_id'],'control_id':editor['id'],
                      'exact_value_proven':True,'sha256':expected_hash,'observed_at_ns':observation['observed_at_ns']},
            'file_before':before,'dispatch_count':0,'focus_dispatch_count':0,'retry_allowed':False,'committed_document_proven':False,
            'saved_file_proven':False,'driver_document_url_proven':False,'save_causality_proven':False,
            'save_command_dispatched':False,'explicit_save_proven':False,'save_command_effect':'not_issued',
            'save_route':save_route,'save_shortcut_posted':False,'menu_dispatch_proven':False,
            'post_buffer_proven':False,'foreground_restoration_proven':False,
            'explicit_save_basis':('acknowledged_native_menu_dispatch_and_independent_saved_bytes' if save_route=='menu'
                                   else 'acknowledged_foreground_shortcut_post_and_independent_saved_bytes'),
            'binding_basis':'caller_controlled_open_and_exact_initial_buffer','deterministic_adapter_action':True,
            'model_selected_save':False}
    already_persisted=text==expected
    path=['File','Save'] if menu_path is None else list(menu_path)
    menu=None
    if already_persisted:
        if prefer_command:
            try:menu=_observed_menu(observation,path)
            except NativeSaveError:pass
        # A disabled, missing or unknown-enabled menu never needs invocation
        # merely to claim an effect already proved independently on disk.
        if menu is None or menu['enabled_observed'] is not True:
            binding.consumed=True
            return {**report,'status':'already_persisted','saved_file_proven':True,'file_after':before,
                    'saved_plane':'saved_file','save_command_effect':'not_issued'}
    if not already_persisted and (before['identity']!=binding.file['identity'] or before['sha256']!=binding.file['sha256']):
        raise NativeSaveError('File changed unexpectedly before Save; reconcile instead of overwriting')
    report['menu_evidence']=menu or _observed_menu(observation,path)
    report['persistence_preceded_command']=already_persisted
    # Prove the bound document first; then request persistent, exact focus once.
    # A focus request is a desktop effect, so consume the receipt before it.
    _validate_snapshot(observation,target)
    final,text=_read_file(binding.path,binding.file['parent_identity'])
    if final['identity']!=before['identity'] or final['sha256']!=before['sha256']:
        raise NativeSaveError('Document changed during Save preflight; no action issued')
    binding.consumed=True
    trace=getattr(owner,'trace',None)
    if save_route=='menu':
        report['focus_dispatch_count']=1
        if callable(trace):trace({'type':'native_save_focus_issued','binding_id':binding.binding_id,
                                  'arguments':target,'snapshot_id':observation['snapshot_id']})
        try:
            _,focus=owner.call('bring_to_front',target)
            report['focus_evidence']=deepcopy(focus)
            if not _focus_verified(focus,target):
                report.update(focus_effect='unverified',reason='exact_window_focus_unproved')
                return _finish_readback(owner,report,binding,expected,before,timeout_s,poll_interval_s)
            report['focus_effect']='verified'
        except Exception as error:
            refusal=_refusal(error)
            report.update(focus_effect='refused' if refusal else 'unknown',
                          focus_error_type=type(error).__name__,reason='exact_window_focus_unproved')
            if refusal is not None:report['focus_refusal']=refusal
            return _finish_readback(owner,report,binding,expected,before,timeout_s,poll_interval_s)
    else:
        report['focus_effect']='temporary_exact_activation_owned_by_press_key'
        report['shortcut_contract']={'application':deepcopy(TEXTEDIT_APPLICATION_CONTRACT),
                                     'key':'s','modifiers':['cmd'],'mapping_source':TEXTEDIT_SAVE_SHORTCUT_SOURCE,
                                     'mapping_observed_from_menu':False,'custom_shortcuts_verified':False}
    focus_finished=time.time_ns()
    observation,editor=_fresh_editor(observe,target,binding,expected,after_ns=focus_finished)
    report['buffer'].update(snapshot_id=observation['snapshot_id'],control_id=editor['id'],
                            observed_at_ns=observation['observed_at_ns'])
    before,text=_read_file(binding.path,binding.file['parent_identity'])
    report['file_before']=before
    already_persisted=text==expected
    report['persistence_preceded_command']=already_persisted
    if not already_persisted and (before['identity']!=binding.file['identity'] or before['sha256']!=binding.file['sha256']):
        raise NativeSaveError('File changed unexpectedly during focus; no Save issued')
    if already_persisted:
        try:menu=_observed_menu(observation,path)
        except NativeSaveError:menu=None
        if menu is None or menu['enabled_observed'] is not True:
            result={**report,'status':'already_persisted','saved_file_proven':True,'file_after':before,
                    'saved_plane':'saved_file','save_command_effect':'not_issued'}
            if callable(trace):trace({'type':'native_save_result','result':deepcopy(result)})
            return result
    report['menu_evidence']=_observed_menu(observation,path)
    # Recheck time and file identity immediately before the single Save.
    _validate_snapshot(observation,target)
    final,text=_read_file(binding.path,binding.file['parent_identity'])
    if final['identity']!=before['identity'] or final['sha256']!=before['sha256']:
        raise NativeSaveError('Document changed during focused Save preflight; no Save issued')
    if save_route=='menu':
        tool='invoke_menu';arguments={**target,'path':path}
    else:
        tool='press_key';handle=observation['handles'][editor['id']]
        arguments={**target,'snapshot_id':observation['snapshot_id'],'element_token':handle['element_token'],
                   'key':'s','modifiers':['cmd'],'delivery_mode':'foreground'}
    report['dispatch_count']=1;report['command_tool']=tool
    if callable(trace):trace({'type':'native_save_issued','binding_id':binding.binding_id,'arguments':arguments,
                              'tool':tool,'save_route':save_route,
                              'expected_sha256':expected_hash,'snapshot_id':observation['snapshot_id']})
    try:
        _,ack=owner.call(tool,arguments)
        report['save_command_effect']=ack.get('effect','unknown') if isinstance(ack,dict) else 'unknown'
        report['save_command_acknowledgment']=deepcopy(ack)
        report['save_command_dispatched']=_save_acknowledged(ack,save_route)
        report['save_shortcut_posted']=report['save_command_dispatched'] and save_route=='textedit_shortcut'
        report['menu_dispatch_proven']=report['save_command_dispatched'] and save_route=='menu'
        if isinstance(ack,dict) and (ack.get('status')=='refused' or ack.get('effect')=='refused' or ack.get('refusal')):
            report.update(save_command_effect='refused',save_command_refusal=deepcopy(ack))
    except Exception as error:
        refusal=_refusal(error)
        report['save_command_effect']='refused' if refusal else 'unknown'
        report['command_error_type']=type(error).__name__
        if refusal is not None:report['save_command_refusal']=refusal
    # A key post must not hide an unexpected literal or focus/document change.
    # This is another read, never a corrective write or retry.
    action_finished=time.time_ns()
    try:
        post,post_editor=_fresh_editor(observe,target,binding,expected,after_ns=action_finished)
        report['post_buffer_proven']=True
        report['post_buffer']={'plane':'editor_buffer','snapshot_id':post['snapshot_id'],
                               'control_id':post_editor['id'],'exact_value_proven':True,
                               'sha256':expected_hash,'observed_at_ns':post['observed_at_ns']}
    except Exception as error:
        report.update(post_buffer_error_type=type(error).__name__,reason='post_save_editor_not_verified')
    return _finish_readback(owner,report,binding,expected,before,timeout_s,poll_interval_s)
