"""Observed native desktop capabilities, without model-written driver arguments.

This layer proves dispatch binding, not user intent or whole-task completion.
The caller decides launch versus reuse, approves scope, and evaluates each fresh
post-observation. Native apps remain open when this transport context closes.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import threading
import time

from .engine.prototype.cua import CuaAdapter, CuaRefusal, PrivateTrace
from .engine.prototype.core import action_available, _identity, _signature
from .engine.prototype.perception import (NativeObservationUnavailable,
    ObservationError, validate_observation)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _target(value):
    if (not isinstance(value, dict) or set(value) != {'pid', 'window_id'}
            or any(type(v) is not int or v <= 0 for v in value.values())):
        raise ValueError('An exact positive pid/window_id pair is required')
    return dict(value)


def _app_identity(app):
    return {key: app.get(key) for key in ('bundle_id', 'launch_path', 'name')}


def _bundle_executable(app):
    """Read only the selected installed bundle's declared main executable."""
    path, bundle = app.get('launch_path'), app.get('bundle_id')
    if (not isinstance(path,str) or not Path(path).is_absolute() or Path(path).suffix!='.app'
            or not isinstance(bundle,str) or not bundle):
        raise ValueError('Observed installed app bundle path and identifier are required')
    root=Path(path).resolve(strict=True)
    if not root.is_dir():raise ValueError('Installed bundle directory is unavailable')
    plist=root/'Contents'/'Info.plist'
    with plist.open('rb') as f:data=f.read(1024*1024+1)
    if len(data)>1024*1024:raise ValueError('Selected app Info.plist exceeds bounded size')
    metadata=plistlib.loads(data)
    if not isinstance(metadata,dict) or metadata.get('CFBundleIdentifier')!=bundle:
        raise ValueError('Selected app bundle identifier disagrees with its Info.plist')
    name=metadata.get('CFBundleExecutable')
    if (not isinstance(name,str) or not name or Path(name).name!=name or name in ('.','..')
            or any(ord(c)<32 for c in name)):
        raise ValueError('Selected app has no unambiguous declared executable')
    executable=(root/'Contents'/'MacOS'/name).resolve(strict=True)
    if not executable.is_file() or not executable.is_relative_to(root):
        raise ValueError('Declared executable escapes or is absent from selected bundle')
    stat=executable.stat()
    return {'bundle_id':bundle,'bundle_path':str(root),'executable_path':str(executable),
            'info_plist_sha256':hashlib.sha256(data).hexdigest(),
            'executable_stat':{'device':stat.st_dev,'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}}


def _process_identity(pid):
    """One listed PID only; no arguments, environment, descendants or scan."""
    if sys.platform!='darwin':raise ValueError('Process-to-app window joining is currently macOS-only')
    if type(pid) is not int or pid<=0:raise ValueError('Positive listed-window PID required')
    environment={**os.environ,'LC_ALL':'C','TZ':'UTC'}
    result=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','pid=,lstart=,comm='],
        capture_output=True,text=True,timeout=2,check=False,env=environment)
    if result.returncode!=0:raise ValueError('Listed-window process identity is unavailable')
    if len(result.stdout)>8192:raise ValueError('Process identity output exceeds bounded size')
    # lstart is fixed English/C-locale weekday/month/time/year, followed by the
    # executable path, not the command-line arguments. Read twice around the
    # second window inventory so a changed/exited/reused PID is not joined.
    match=re.fullmatch(r'\s*(\d+)\s+([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+([^\r\n]+)\n?',result.stdout)
    if not match or int(match[1])!=pid or not Path(match[3]).is_absolute():
        raise ValueError('Process identity output is ambiguous or nonabsolute')
    executable=Path(match[3]).resolve(strict=True)
    return {'pid':pid,'started_at_utc':' '.join(match[2].split()),'executable_path':str(executable)}


class DesktopTools:
    """One bounded Cua owner with observation-issued native action catalogs.

    ``actions`` entries are immutable except an added ``value`` for set_text.
    ``execute`` refreshes the same target and consumes its original observation;
    old descriptors cannot be replayed after a refused, failed or successful call.
    No key, coordinate, clipboard, process-kill or arbitrary-argument interface is
    exposed. A native snapshot may be incomplete: only positive evidence counts.
    """
    def __init__(self, config, out):
        from .config import load, validate
        from .engine_adapter import artifact_directory
        self.config = validate(config) if isinstance(config, dict) else load(config, required=True)[0]
        self.out = artifact_directory(out, 'desktop')
        self.trace = PrivateTrace(self.out / 'desktop.jsonl')
        self._owner = None
        self._closed = False
        self._lock = threading.RLock()
        self._apps = {}
        self._launch_attempts = set()
        self._activation_attempts = set()
        self._activation_state = {}
        self._failed_observations = {}
        self._known_windows = {}
        self._captures = {}
        self._adapters = {}
        self._catalogs = {}
        self._uncertain_targets = set()
        self.cleanup_result = None

    def _connection(self):
        if self._closed:
            raise ValueError('DesktopTools is closed')
        if self._owner is None:
            from .engine_adapter import owner
            self._owner = owner(self.config, self.out)
        self._owner.check()
        return self._owner

    def _result(self, operation, **value):
        result = {'operation': operation, 'action_started': False,
                  'task_complete': False, **value}
        if not self._closed:
            self.trace({'type': 'desktop_result', 'result': result})
        return result

    def _failure(self, operation, error, *, started=False):
        status, code, detail = 'uncertain' if started else 'unavailable', type(error).__name__, {}
        if isinstance(error, NativeObservationUnavailable):
            code, detail = error.code, {'details': error.details, 'remedy': error.remedy}
        elif isinstance(error, CuaRefusal):
            # A structured refusal is a refusal, but a launch timeout can still
            # mean an OS launch request was issued. Never turn it into retry advice.
            status, code = 'refused', error.code or 'driver_refused'
            detail = {'driver': deepcopy(error.payload)}
            launch = error.payload.get('launch_state', {})
            if launch.get('requested') is True or error.payload.get('action_started') is True:
                status, started = 'uncertain', True
            elif error.payload.get('action_started') is False:
                started = False
        elif isinstance(error, (ValueError, ObservationError)) and not started:
            status = 'refused'
        return self._result(operation, status=status, code=code,
                            reason=str(error), action_started=started, **detail)

    def apps(self):
        with self._lock:
            try:
                _, raw = self._connection().call('list_apps', {})
                apps = raw.get('apps')
                if not isinstance(apps, list) or any(not isinstance(a, dict) for a in apps):
                    raise ValueError('Driver returned an invalid app inventory')
                self._apps = {_digest(a): deepcopy(a) for a in apps}
                return self._result('apps', status='ok', apps=deepcopy(apps), raw=deepcopy(raw))
            except Exception as error:
                self._apps = {}
                return self._failure('apps', error)

    def number_format(self,app,target):
        """Read configured number conventions for this independently owned app.

        This observes locale preferences, not the app's private formatter and
        not a desired numeric answer. Full preference evidence remains local.
        """
        from .number_format import probe_app_number_format
        with self._lock:
            try:
                target=_target(target)
                identity=_app_identity(app)
                if not any(_app_identity(a)==identity for a in self._apps.values()):
                    raise ValueError('Application identity was not issued by apps()')
                installed=_bundle_executable(app);process=_process_identity(target['pid'])
                if process['executable_path']!=installed['executable_path']:
                    raise ValueError('Number convention app does not own the selected process')
                evidence=probe_app_number_format(app['bundle_id'],target,
                    cache_dir=self.out/'number-format-module-cache')
                if _process_identity(target['pid'])!=process:
                    raise ValueError('Selected process changed during locale capture')
                self.trace({'operation':'number_format','target':target,'evidence':evidence})
                return evidence
            except (ValueError,OSError,subprocess.SubprocessError):
                return {'status':'unknown','reason':'independent_application_number_format_unavailable',
                        'application_formatter_proven':False}

    def _windows(self, pid=None):
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError('pid must be a positive integer')
        _, raw = self._connection().call('list_windows', {} if pid is None else {'pid': pid})
        rows = raw.get('windows')
        if not isinstance(rows, list) or any(not isinstance(w, dict) for w in rows):
            raise ValueError('Driver returned an invalid window inventory')
        # Retain unaddressable/off-screen records as evidence, without inventing
        # a PID or treating off-screen as closed or absent.
        for w in rows:
            if type(w.get('pid')) is int and w['pid'] > 0 and type(w.get('window_id')) is int and w['window_id'] > 0:
                if pid is not None and w['pid'] != pid:
                    raise ValueError('Filtered window inventory returned a foreign PID')
        self._known_windows = {k:v for k,v in self._known_windows.items() if pid is not None and k[0] != pid}
        for w in rows:
            if type(w.get('pid')) is int and w['pid'] > 0 and type(w.get('window_id')) is int and w['window_id'] > 0:
                self._known_windows[(w['pid'], w['window_id'])] = deepcopy(w)
        return raw

    def windows(self, pid=None):
        with self._lock:
            try:
                raw = self._windows(pid)
                return self._result('windows', status='ok', windows=deepcopy(raw['windows']), raw=deepcopy(raw))
            except Exception as error:
                return self._failure('windows', error)

    def app_windows(self, app):
        """Join listed windows to a proven installed main executable, read-only.

        NSWorkspace running-state metadata can lag WindowServer. Keep its raw
        facts unchanged, and independently prove only executable-path ownership.
        Never join by window title, process display name, or a guessed old PID.
        Unknown process evidence is not proof that the application is closed.
        """
        with self._lock:
            try:
                if not isinstance(app,dict):raise ValueError('Observed installed application required')
                identity=_app_identity(app)
                if not any(_app_identity(a)==identity for a in self._apps.values()):
                    raise ValueError('Application identity was not issued by apps()')
                _,current=self._connection().call('list_apps',{})
                peers=[a for a in current.get('apps',[]) if a.get('bundle_id')==identity['bundle_id']]
                if not peers or any(_app_identity(a)!=identity for a in peers):
                    raise ValueError('Installed app identity changed or is ambiguous')
                installed=_bundle_executable(app)
                first=self._windows()
                pids=sorted({w['pid'] for w in first['windows'] if type(w.get('pid')) is int and w['pid']>0})
                if len(pids)>256:raise ValueError('Listed process identity budget exceeded; no windows omitted silently')
                verified={};unresolved=[];unrelated=[];started=time.monotonic()
                for pid in pids:
                    if time.monotonic()-started>15:
                        raise ValueError('App-window ownership deadline exceeded; refresh explicitly')
                    try:
                        process=_process_identity(pid)
                        if process['executable_path']==installed['executable_path']:verified[pid]=process
                        else:unrelated.append(pid)
                    except (ValueError,OSError,subprocess.SubprocessError):
                        unresolved.append({'pid':pid,'reason':'process_identity_unavailable'})
                # Reconcile target availability and PID incarnation after the
                # process reads; this remains a discovery hint, not dispatch.
                latest=self._windows();joined=[];stable={}
                if _bundle_executable(app)!=installed:
                    raise ValueError('Selected app bundle changed during ownership discovery')
                for pid,process in verified.items():
                    try:
                        if _process_identity(pid)!=process:
                            raise ValueError('Process incarnation changed')
                        stable[pid]=process
                    except (ValueError,OSError,subprocess.SubprocessError):
                        unresolved.append({'pid':pid,'reason':'process_identity_changed_during_discovery'})
                for window in latest['windows']:
                    pid=window.get('pid')
                    if pid not in stable or type(window.get('window_id')) is not int or window['window_id']<=0:
                        continue
                    evidence={'basis':'listed_window_pid_exact_declared_bundle_executable',
                        'process':deepcopy(stable[pid]),'installed_bundle':deepcopy(installed),
                        'observed_at_ns':time.time_ns(),'process_rechecked':True,
                        'name_matching_used':False,'code_signature_identity_proven':False,
                        'dispatch_authority':False}
                    joined.append({**deepcopy(window),'app_identity_evidence':evidence})
                newly_listed={w.get('pid') for w in latest['windows'] if type(w.get('pid')) is int and w['pid']>0}-set(pids)
                unresolved.extend({'pid':pid,'reason':'process_appeared_after_identity_inventory'} for pid in sorted(newly_listed))
                return self._result('app_windows',status='ok',windows=joined,raw_app=deepcopy(app),
                    current_app_inventory=deepcopy(peers),raw_windows=deepcopy(latest),
                    initial_raw_windows=deepcopy(first),unresolved_pids=unresolved,
                    nonmatching_process_pids=unrelated,all_listed_pids_resolved=not unresolved,
                    availability='owned_windows_observed' if joined else 'unknown' if unresolved else 'no_owned_windows_observed',
                    launch_or_reuse_selected=False,scope='selected_installed_bundle_main_executable_only')
            except Exception as error:
                return self._failure('app_windows',error)

    def launch(self, app):
        """Explicit normal launch/reopen choice; never chooses reuse for caller."""
        with self._lock:
            started = False
            try:
                if not isinstance(app, dict) or self._apps.get(_digest(app)) != app:
                    raise ValueError('Launch requires an unchanged app record from apps()')
                bundle = app.get('bundle_id')
                if not isinstance(bundle, str) or not bundle or any(ord(c) < 32 for c in bundle):
                    raise ValueError('Observed app lacks an unambiguous bundle identifier')
                identity = _app_identity(app)
                _, current = self._connection().call('list_apps', {})
                peers = [a for a in current.get('apps', []) if a.get('bundle_id') == bundle]
                if not peers or any(_app_identity(a) != identity for a in peers):
                    raise ValueError('Application identity changed or bundle inventory is ambiguous')
                key = _digest(identity)
                if key in self._launch_attempts:
                    raise ValueError('Launch already attempted; inspect current app/window state, do not replay')
                self._launch_attempts.add(key)
                started = True
                _, raw = self._connection().call('launch_app', {'bundle_id': bundle})
                if raw.get('bundle_id') != bundle or type(raw.get('pid')) is not int or raw['pid'] <= 0:
                    raise ValueError('Launch reply did not prove the requested app/process identity')
                state = raw.get('launch_state', {})
                if state.get('requested') is not True or state.get('process_running') is not True:
                    raise ValueError('Launch process readiness is not established')
                # Bind discovery to the returned process, never the old pid=0
                # inventory record or a guessed current app process.
                windows = self._windows(raw['pid'])['windows']
                return self._result('launch', status='launched' if windows else 'pending',
                    app={k:raw.get(k) for k in ('pid', 'bundle_id', 'name')},
                    windows=deepcopy(windows), launch_state=deepcopy(state), raw=deepcopy(raw),
                    action_started=True, created_new_instance=False,
                    effect='process_observed' if windows else 'window_not_observed')
            except Exception as error:
                return self._failure('launch', error, started=started)

    def activate(self, target):
        """Explicitly leave one known exact window in the foreground.

        Activation is an effectful lifecycle choice, never an automatic observe
        fallback. It does not prove accessibility/readability or task completion.
        At most three attempts per target are permitted. A repeat requires a
        verified prior activation, a later failed explicit observe, and fresh
        proof of the same process incarnation/window. Uncertain effects stay locked.
        """
        with self._lock:
            started=False;key=None
            try:
                target=_target(target);key=(target['pid'],target['window_id'])
                if key not in self._known_windows:
                    raise ValueError('Activation requires a window from prior windows() discovery')
                prior=self._activation_state.get(key)
                if key in self._uncertain_targets:
                    return self._result('activate',status='refused',code='target_effect_uncertain',target=target,
                        reason='An earlier activation or task write has an uncertain effect; reactivation cannot unlock it.')
                if prior is not None:
                    if prior['attempts']>=3:
                        return self._result('activate',status='refused',code='activation_recovery_budget_exhausted',target=target,
                            reason='Three explicit activation attempts already issued for this exact target.')
                    failure=self._failed_observations.get(key)
                    if prior.get('status')!='activated' or failure is None or failure['activation_attempt']!=prior['attempts']:
                        return self._result('activate',status='refused',code='activation_recovery_not_eligible',target=target,
                            reason='Repeat activation requires a new failed explicit observation after verified activation.')
                    if prior.get('process_identity') is None:
                        return self._result('activate',status='refused',code='activation_process_identity_unproved',target=target,
                            reason='Original activation process incarnation was not recorded; refresh cannot prove safe recovery.')

                owner=self._connection()
                inventory=owner.inventory.get('tools',[]) if isinstance(owner.inventory,dict) else []
                schemas=[t.get('inputSchema',{}) for t in inventory if t.get('name')=='bring_to_front']
                if len(schemas)!=1 or not {'pid','window_id'}<=set(schemas[0].get('properties',{})):
                    raise ValueError('Runtime lacks an exact-window activation tool schema')
                process=None
                try:process=_process_identity(target['pid'])
                except (ValueError,OSError,subprocess.SubprocessError):
                    if prior is not None:raise ValueError('Recovery process identity is unavailable')
                if prior is not None and process!=prior['process_identity']:
                    return self._result('activate',status='refused',code='activation_process_changed',target=target,
                        reason='PID incarnation/executable differs from the prior verified activation.')
                windows=self._windows(target['pid'])['windows']
                if prior is not None and _process_identity(target['pid'])!=process:
                    return self._result('activate',status='refused',code='activation_process_changed_during_reconciliation',target=target,
                        reason='Process identity changed around fresh exact-window inventory.')
                exact=[w for w in windows if w.get('pid')==target['pid'] and w.get('window_id')==target['window_id']]
                if len(exact)!=1:
                    raise ValueError('Known exact window is absent or ambiguous in fresh inventory')
                if exact[0].get('layer') not in (None,0):
                    raise ValueError('Only ordinary layer-zero windows may be activated')
                # Foreground changes may invalidate every old capture, including
                # another window's focus-dependent state. Reobserve explicitly.
                self._captures.clear()
                for adapter in self._adapters.values():adapter.latest=None
                attempt=(prior['attempts'] if prior else 0)+1
                self._activation_attempts.add(key)
                self._activation_state[key]={'attempts':attempt,'status':'pending','process_identity':deepcopy(process)}
                self._failed_observations.pop(key,None);started=True
                try:
                    _,raw=owner.call('bring_to_front',target)
                except CuaRefusal as error:
                    raw=error.payload
                expected=raw.get('exact_window_effect',{})
                observed=raw.get('observed',{})
                verified=(raw.get('code')=='bring_to_front_exact_window_verified'
                    and raw.get('status')=='activated' and raw.get('activated') is True
                    and raw.get('process_activated') is True
                    and all(raw.get(k)==v for k,v in target.items())
                    and all(expected.get(k) is True for k in ('verified','focused','frontmost_ordinary','target_visible_ordinary'))
                    and observed.get('frontmost_pid')==target['pid']
                    and observed.get('focused_window_id')==target['window_id']
                    and observed.get('frontmost_ordinary_window_id')==target['window_id'])
                no_request=(raw.get('request_accepted') is False and raw.get('activated') is False
                    and raw.get('code') in ('bring_to_front_pid_not_found','bring_to_front_window_not_found',
                        'bring_to_front_window_pid_mismatch','bring_to_front_window_not_ordinary',
                        'bring_to_front_pid_out_of_range','bring_to_front_window_id_out_of_range'))
                status='activated' if verified else 'refused' if no_request else 'uncertain'
                self._activation_state[key]['status']=status
                if status=='uncertain':self._uncertain_targets.add(key)
                return self._result('activate',status=status,action_started=not no_request,
                    target=deepcopy(target),driver_ack=deepcopy(raw),code=raw.get('code'),
                    exact_window_activation_proven=verified,foreground_persistent=verified,
                    accessibility_readability_proven=False,observation_required=True,
                    previous_captures_invalidated=True,automatic_fallback=False,
                    activation_attempt=attempt,activation_attempt_limit=3,explicit_recovery=prior is not None,
                    process_identity_reconciled=prior is not None,
                    process_identity_recorded=process is not None)
            except Exception as error:
                if started and key is not None:self._uncertain_targets.add(key)
                return self._failure('activate',error,started=started)

    def _capture(self, target):
        target = _target(target);key = (target['pid'], target['window_id'])
        # Clear stale authority before any failing refresh/listing attempt.
        self._captures.pop(key, None)
        adapter = self._adapters.get(key)
        if adapter is not None:
            adapter.latest = None
        rows = self._windows(target['pid'])['windows']
        matches = [w for w in rows if w.get('pid') == target['pid'] and w.get('window_id') == target['window_id']]
        if len(matches) != 1:
            raise ValueError('Exact target is absent or ambiguous in current window inventory')
        if adapter is None:
            adapter = CuaAdapter(self._connection(), kind='native_window_state', target=target, execute=True, regions=True)
            self._adapters[key] = adapter
        observation = adapter.observe()
        validate_observation(observation, expected_target=target, max_age_s=30)
        if observation.get('kind') != 'native_window_state':
            raise ValueError('Native observation required')
        self._captures[key] = deepcopy(observation)
        return observation

    def observe(self, target):
        with self._lock:
            try:
                value = self._capture(target)
                self._failed_observations.pop((target['pid'],target['window_id']),None)
                return self._result('observe', status='observed', observation=deepcopy(value))
            except Exception as error:
                try:
                    exact=_target(target);key=(exact['pid'],exact['window_id']);prior=self._activation_state.get(key)
                    if prior is not None and prior.get('status')=='activated' and key not in self._uncertain_targets:
                        self._failed_observations[key]={'activation_attempt':prior['attempts'],'at_ns':time.time_ns(),
                            'error_type':type(error).__name__,'reason':str(error)}
                except (ValueError,TypeError):pass
                return self._failure('observe', error)

    def _issued_observation(self, observation, max_age_s=30):
        if not isinstance(observation, dict):
            raise ValueError('Original full observation required')
        target = _target(observation.get('target'));key = (target['pid'],target['window_id'])
        if self._captures.get(key) != observation:
            raise ValueError('Observation was not issued here, was modified, or has been superseded/consumed')
        validate_observation(observation, expected_target=target, max_age_s=max_age_s)
        return key

    def actions(self, observation):
        with self._lock:
            try:
                self._issued_observation(observation)
                catalog, unavailable = [], []
                observation_hash = _digest(observation)
                for c in observation['controls']:
                    for kind in ('press', 'set_text'):
                        if not action_available(observation, c, kind):
                            continue
                        if kind == 'set_text' and c.get('value_evidence', {}).get('exact_value_proven') is not True:
                            unavailable.append({'control_id': c['id'], 'kind':kind, 'reason':'exact_editor_readback_unproved'})
                            continue
                        item = {'kind':kind, 'control_id':c['id'], 'snapshot_id':observation['snapshot_id'],
                                'target':deepcopy(observation['target']), 'name':c.get('name'), 'role':c.get('role'),
                                'description':('Press ' if kind=='press' else 'Replace exact editor text in ')+str(c.get('role'))+' '+json.dumps(c.get('name'),ensure_ascii=False),
                                'requires_value':kind=='set_text'}
                        item['id'] = _digest({'observation':observation_hash, 'action':item})[:32]
                        catalog.append(item)
                        self._catalogs[item['id']] = {'descriptor':deepcopy(item), 'observation_sha256':observation_hash,
                            'identity':_identity(c,observation), 'signature':_signature(c,observation)}
                return self._result('actions', status='ok', actions=catalog, unavailable=unavailable,
                    coverage=deepcopy(observation.get('coverage',{})),
                    limitations=['Positive captured controls only; native completeness/absence is not proved.',
                                 'Keypress, arbitrary menu paths, coordinates and clipboard routes are not exposed.'])
            except Exception as error:
                return self._failure('actions',error)

    def execute(self, action, observation):
        with self._lock:
            started = False;key = None
            try:
                key = self._issued_observation(observation, max_age_s=120)
                if key in self._uncertain_targets:
                    raise ValueError('Previous input effect is uncertain; stop and reconcile before another mutation')
                if not isinstance(action, dict):
                    raise ValueError('Issued action descriptor required')
                stored = self._catalogs.get(action.get('id'))
                descriptor = {k:v for k,v in action.items() if k!='value'}
                if not stored or descriptor != stored['descriptor'] or stored['observation_sha256'] != _digest(observation):
                    raise ValueError('Unknown, altered, or foreign-snapshot action descriptor')
                kind = descriptor['kind']
                if kind=='set_text':
                    if not isinstance(action.get('value'),str) or '\x00' in action['value']:
                        raise ValueError('set_text requires a literal string without NUL')
                elif 'value' in action:
                    raise ValueError('Press does not accept a value')
                fresh = self._capture(observation['target'])
                if fresh['snapshot_id']==observation['snapshot_id']:
                    raise ValueError('New snapshot required before input')
                peers=[c for c in fresh['controls'] if _identity(c,fresh)==stored['identity']]
                if len(peers)!=1 or _signature(peers[0],fresh)!=stored['signature']:
                    raise ValueError('Target control identity, state, geometry or ancestry changed/ambiguous')
                control=peers[0]
                if not action_available(fresh,control,kind):
                    raise ValueError('Fresh control no longer supports the selected capability')
                if kind=='set_text' and control.get('value_evidence',{}).get('exact_value_proven') is not True:
                    raise ValueError('Fresh exact editor readback is unavailable')
                # A value-derived editor name may change after replacement.
                # Bind its stable observed identity before sending input; this
                # refuses weak/ambiguous identities rather than dropping names
                # indiscriminately during post-action matching.
                if kind=='set_text':
                    from .goal_verification import bind as bind_value, verify as verify_value
                    value_goal={'id':'dispatched-editor-value','kind':'text','target':control.get('name'),
                                'value':action['value'],'evidence_plane':'editor_buffer'}
                    value_binding=bind_value(value_goal,control,fresh)
                handle=fresh['handles'][control['id']]
                dispatched={'kind':kind,'control_id':control['id'],'snapshot_id':fresh['snapshot_id'],
                            'handle':deepcopy(handle),'signature':_signature(control,fresh),'value':action.get('value')}
                # Consume before the potentially effectful call, including timeout.
                self._captures.pop(key,None)
                started=True
                ack=self._adapters[key].dispatch(dispatched)
                if ack.get('effect')=='refused':
                    raise CuaRefusal(ack)
                try:
                    post=self._capture(observation['target'])
                    if post['snapshot_id'] == fresh['snapshot_id']:
                        self._captures.pop(key, None)
                        raise ValueError('Post-action readback reused the pre-action snapshot')
                except Exception as error:
                    self._uncertain_targets.add(key)
                    return self._result('execute',status='uncertain',code='post_observation_unavailable',
                        reason=str(error),action_started=True,driver_ack=ack,fresh_action=dispatched)
                verification={'plane':'editor_buffer' if kind=='set_text' else 'observed_ui',
                              'exact_value_proven':False,'task_complete':False,
                              'committed_document_proven':False,'saved_output_proven':False}
                status='dispatched'
                if kind=='set_text':
                    checked=verify_value(value_binding,value_goal,post)
                    exact=checked['matched']
                    verification['readback']=checked
                    verification['exact_value_proven']=exact
                    status='verified' if exact else 'uncertain'
                    if not exact:self._uncertain_targets.add(key)
                return self._result('execute',status=status,action_started=True,driver_ack=ack,
                    fresh_action=dispatched,observation=deepcopy(post),verification=verification)
            except Exception as error:
                result=self._failure('execute',error,started=started)
                if key is not None and (result['status']=='uncertain'
                        or started and not isinstance(error,CuaRefusal)):
                    self._uncertain_targets.add(key)
                return result

    def close(self):
        with self._lock:
            if self.cleanup_result is not None:
                return deepcopy(self.cleanup_result)
            errors=[]
            if self._owner is not None:
                try:errors.extend(self._owner.close())
                except Exception as error:errors.append(str(error))
            self.cleanup_result=self._result('close',status='closed' if not errors else 'cleanup_failed',
                errors=errors,applications_left_open=True)
            self._closed=True;self._captures.clear();self._catalogs.clear();self.trace.close()
            return deepcopy(self.cleanup_result)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
