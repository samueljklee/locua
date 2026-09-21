"""Comparison-only, task-scoped model disclosure. No desktop or model IO.

The caller retains original tool results/observations for guards and private
traces. This view cannot authorize input. All providers must use the same view.
"""
from __future__ import annotations
from copy import deepcopy
import re
import hashlib
import json
from pathlib import Path

VERSION = 'task-observation-scope-v1.1'
_RECENT = {'recent items', 'open recent', 'recent documents', 'recent files'}
_OMIT = {'source', 'provenance', 'raw', 'raw_observation', 'handles',
         'hierarchy', 'native_contract', 'native_editor_contract',
         'launch_path', 'file_path', 'path', 'loaded_snapshot', 'tree_markdown', 'raw_windows', 'raw_app',
         'full_response_ref', 'source_evidence_ref', 'sequence_evidence_ref',
         'source_implementation_metadata', 'inspection_provenance',
         'all_control_region_memberships', 'control_region_memberships'}


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _editor_summary(value):
    result={k:deepcopy(value[k]) for k in ('plane','atomic_capture','coherence',
        'committed_document_proven','saved_file_proven','started_at_ns','finished_at_ns') if k in value}
    for key in ('focused','focused_recheck','value_settable','selected_range','selected_text','raw_value','raw_value_recheck'):
        if isinstance(value.get(key),dict):
            result[key]={k:deepcopy(value[key][k]) for k in ('status','value') if k in value[key]}
    return result


def _value_summary(value):
    return {k:deepcopy(value[k]) for k in ('kind','precision','exact_value_proven','plane','atomic_capture',
        'committed_document_proven','saved_output_proven','structured_value_trimmed','possible_placeholder',
        'markdown_is_exact_attribute_read','markdown_value') if k in value}


class ScopeError(ValueError):
    pass


def _target(value):
    if not isinstance(value, dict): return None
    p, w = value.get('pid'), value.get('window_id')
    return (p, w) if type(p) is int and p > 0 and type(w) is int and w > 0 else None


class TaskObservationScope:
    """A narrow explicit-app/document comparison scope, not an intent planner.

    A title must be explicitly present in the request. With no document title,
    only an identity-proven app window whose title equals its app name is exposed.
    Unknown/ambiguous scopes fail closed rather than publishing inventories.
    """
    def __init__(self, request, app, *, document_titles=()):
        if not isinstance(request, str) or not request.strip(): raise ScopeError('Request required')
        if not isinstance(app, dict) or not isinstance(app.get('name'), str) or not app['name']:
            raise ScopeError('One explicit installed app required')
        if not re.search(r'(?<!\w)'+re.escape(app['name'])+r'(?!\w)',request,re.I):
            raise ScopeError('App name must be explicitly present in the request')
        if any(not isinstance(t,str) or not t or t not in request for t in document_titles):
            raise ScopeError('Document titles must be explicit request literals')
        self.request=request;self.app=deepcopy(app);self.document_titles=set(document_titles)
        self.app_ids=set();self.window_ids=set();self.targets=set();self.private_titles=set()
        self.blocked_controls=set();self.blocked_actions=set();self.blocked_regions=set()
        self.control_ids=set();self.action_ids=set();self.region_ids=set();self.snapshots=set()
        self.redacted_literals=set();self.window_records={};self.observations={}
        self.audit={'version':VERSION,'raw_kept_private':True,'observation_authority_unchanged':True,
                    'recent_descendant_controls':0,'foreign_document_controls':0,'unrelated_windows':0}
        if app.get('app_id'):self.app_ids.add(app['app_id'])

    @classmethod
    def from_request(cls, request, installed_apps, *, document_titles=()):
        matches=[a for a in installed_apps if isinstance(a,dict) and isinstance(a.get('name'),str)
                 and a['name'] and re.search(r'(?<!\w)'+re.escape(a['name'])+r'(?!\w)',request,re.I)]
        unique={(a.get('bundle_id'),a['name']):a for a in matches}
        if len(unique)!=1:raise ScopeError('Privacy scope requires one unambiguous explicitly named installed app')
        return cls(request,next(iter(unique.values())),document_titles=document_titles)

    def _app_allowed(self, row):
        return (row.get('name','').casefold()==self.app['name'].casefold()
                and (not self.app.get('bundle_id') or row.get('bundle_id')==self.app['bundle_id']))

    def register_apps(self, rows):
        for a in rows:
            if self._app_allowed(a) and isinstance(a.get('app_id'),str):self.app_ids.add(a['app_id'])

    def register_windows(self, rows, app_id=None):
        if app_id is not None and app_id not in self.app_ids:raise ScopeError('Foreign app window inventory')
        for w in rows:
            target=_target(w.get('target',w));title=w.get('title')
            allowed=(w.get('identity_proven') is True and target is not None and isinstance(title,str)
                     and (title in self.document_titles if self.document_titles else title==self.app['name']))
            public=w.get('window_id')
            if allowed:
                self.targets.add(target)
                if isinstance(public,str):self.window_ids.add(public);self.window_records[public]=deepcopy(w)
            else:
                if isinstance(title,str) and title:self.private_titles.add(title)
                self.audit['unrelated_windows']+=1

    def register_observation(self, observation, *, actions=(), regions=()):
        """Register unprojected evidence before releasing a model-visible page."""
        if _target(observation.get('target')) not in self.targets:raise ScopeError('Unscoped observation target')
        sid=observation.get('snapshot_id')
        if not isinstance(sid,str) or not sid:raise ScopeError('Exact snapshot identity required')
        controls=observation.get('controls')
        if not isinstance(controls,list):raise ScopeError('Full captured control inventory required')
        windows=[c for c in controls if c.get('role')=='AXWindow' and c.get('parent') is None]
        allowed_titles=self.document_titles or {self.app['name']}
        if len(windows)!=1 or windows[0].get('name') not in allowed_titles:
            raise ScopeError('Observed window title no longer matches explicit scope')
        by={c['id']:c for c in controls}
        if len(by)!=len(controls):raise ScopeError('Duplicate control identity')
        if sid in self.observations:
            if self.observations[sid]['hash']!=_digest(observation):raise ScopeError('Snapshot content changed under retained identity')
            return
        self.snapshots.add(sid);self.control_ids.update(by)
        recent=set();foreign=set()
        for c in controls:
            label=c.get('name')
            if c.get('role') in ('AXMenuItem','AXMenuBarItem','AXMenu') and isinstance(label,str) and label.casefold().strip(' .…') in _RECENT:
                recent.add(c['id'])
            if c.get('role') in ('AXMenuItem','AXMenuBarItem','AXMenu') and isinstance(label,str) and label in self.private_titles:foreign.add(c['id'])
        descendants=set();pending=set(recent|foreign)
        while pending:
            new={c['id'] for c in controls if c.get('parent') in pending}-descendants
            descendants.update(new);pending=new
        blocked=recent|foreign|descendants
        self.blocked_controls.update(blocked)
        # Roots remain readable navigation labels; their actions are blocked.
        redacted=(descendants|foreign)-recent
        for cid in redacted:
            c=by[cid]
            for v in (c.get('name'),c.get('value')):
                if isinstance(v,str) and len(v)>=4 and v not in (self.document_titles|{self.app['name']}):self.redacted_literals.add(v)
        for a in actions:
            if a.get('snapshot_id')!=sid or _target(a.get('target')) not in self.targets:
                raise ScopeError('Action inventory has foreign snapshot/target')
            self.action_ids.add(a['id'])
            if a.get('control_id') in blocked:self.blocked_actions.add(a['id'])
        if not regions:
            from .engine.prototype.regions import catalog_regions
            regions=catalog_regions(observation)['regions']
        for region in regions:
            rid=region.get('id',region.get('region_id'))
            if not isinstance(rid,str):raise ScopeError('Region identity required')
            self.region_ids.add(rid)
            if region.get('root_control_id') in blocked:self.blocked_regions.add(rid)
        self.observations[sid]={'redacted':redacted,'blocked':blocked,'target':observation['target'],
            'hash':_digest(observation),'controls':by}
        self.audit['recent_descendant_controls']=len(self.blocked_controls-foreign)
        self.audit['foreign_document_controls']=len(foreign)

    def check_call(self, tool, arguments):
        """Disclosure/effect scope only; existing task and action guards still run."""
        if not isinstance(arguments,dict):raise ScopeError('Tool arguments must be an object')
        a=arguments
        if tool=='locua_apps':
            query=a.get('query')
            if query not in (None,'') and (not isinstance(query,str) or query.casefold()!=self.app['name'].casefold()):
                raise ScopeError('App query lies outside the explicit comparison scope')
        if 'app_id' in a and (not isinstance(a['app_id'],str) or a['app_id'] not in self.app_ids):raise ScopeError('Unscoped app reference')
        if 'window_id' in a and (not isinstance(a['window_id'],str) or a['window_id'] not in self.window_ids):raise ScopeError('Unscoped window reference')
        if 'snapshot_id' in a and (not isinstance(a['snapshot_id'],str) or a['snapshot_id'] not in self.snapshots):raise ScopeError('Unscoped snapshot reference')
        for key,known,blocked in (('control_id',self.control_ids,self.blocked_controls),
                                  ('action_id',self.action_ids,self.blocked_actions),
                                  ('region_id',self.region_ids,self.blocked_regions)):
            if key in a and (not isinstance(a[key],str) or a[key] not in known or a[key] in blocked):raise ScopeError('Unknown or privacy-excluded '+key)
        if tool=='locua_review':
            rows=[]
            for key in ('goals','effects','preserves'):
                if not isinstance(a.get(key,[]),list):raise ScopeError('Review '+key+' must be an array')
                rows.extend(a.get(key,[]))
            for row in rows:
                if not isinstance(row,dict):raise ScopeError('Review entries must be objects')
                cid=row.get('control_id')
                if cid is not None and (not isinstance(cid,str) or cid not in self.control_ids or cid in self.blocked_controls):
                    raise ScopeError('Review references a privacy-excluded or unknown control')
        if tool=='locua_act_sequence':
            steps=a.get('steps')
            if not isinstance(steps,list):raise ScopeError('Sequence steps must be an array')
            for step in steps:
                aid=step.get('action_id') if isinstance(step,dict) else None
                if not isinstance(aid,str) or aid not in self.action_ids or aid in self.blocked_actions:
                    raise ScopeError('Sequence references a privacy-excluded or unknown action')
        return True

    def _text(self, value):
        for secret in sorted(self.private_titles|self.redacted_literals,key=len,reverse=True):
            value=value.replace(secret,'[private content omitted]')
        # Account names in the system menu are not needed for any scoped app task.
        value=re.sub(r'/Users/[^/\s\"\']+', '/Users/[account omitted]', value)
        if re.match(r'^Log Out\s+.+',value,re.I):return 'Log Out [account omitted]'
        return value

    def project(self, tool, result):
        """Pure copy projection; callers must register all newly captured evidence.

        Pagination and rows are retained. Placeholders are not empty values or
        evidence of absence. Source evidence/canonical guards remain private.
        """
        if not isinstance(result,dict):raise ScopeError('Expected structured tool result')
        result=deepcopy(result)
        if result.get('snapshot_id') and result['snapshot_id'] not in self.snapshots:
            raise ScopeError('Unregistered snapshot cannot be disclosed')
        coverage=result.get('coverage',{})
        if not isinstance(coverage,dict):raise ScopeError('Coverage must be a structured object')
        coverage_scope=coverage.get('scope')
        # Native observations use a scope label such as "native_window";
        # progressive detail pages use {kind, control_id}. A label is coverage
        # metadata, never an implied control reference or an action handle.
        if coverage_scope is not None and not isinstance(coverage_scope,(str,dict)):
            raise ScopeError('Coverage scope must be a label or structured scope')
        detail_id=result.get('control_id')
        if detail_id is None and isinstance(coverage_scope,dict):
            detail_id=coverage_scope.get('control_id')
        if detail_id is not None and (not isinstance(detail_id,str) or not detail_id):
            raise ScopeError('Control detail identity must be a nonempty string')
        if detail_id in self.blocked_controls:raise ScopeError('Privacy-excluded control detail cannot be disclosed')
        if tool=='locua_apps':
            self.register_apps(result.get('items',[]))
            original=result.get('items',[]);result['items']=[a for a in original if self._app_allowed(a)]
            result['privacy_omitted_items']=len(original)-len(result['items'])
        if 'windows' in result and isinstance(result['windows'],list):
            original=result['windows'];result['windows']=[w for w in original if w.get('window_id') in self.window_ids]
            result['privacy_omitted_windows']=len(original)-len(result['windows'])
        def walk(value,key=None):
            if isinstance(value,str):
                return value if key in ('id','control_id','action_id','snapshot_id','window_id','app_id','parent','region_id','cursor','continuation','original_request','expression') else self._text(value)
            if isinstance(value,list):return [walk(v) for v in value]
            if not isinstance(value,dict):return value
            if value.get('kind') in ('unbound_text','context_only_text') or value.get('source_kind') in ('unbound_text','context_only_text'):
                return {'kind':'privacy_unknown_unbound_text','text_disclosed':False,'not_absence_evidence':True}
            if _target(value.get('target')) is not None and _target(value['target']) not in self.targets:
                return {'privacy_omitted':True,'unknown':True,'reason':'unrelated_target'}
            out={k:walk(v,k) for k,v in value.items() if k not in _OMIT}
            if isinstance(value.get('editor'),dict):out['editor']=deepcopy(value['editor']) if value['editor'].get('deferred') else _editor_summary(value['editor'])
            if isinstance(value.get('value_evidence'),dict):out['value_evidence']=_value_summary(value['value_evidence'])
            cid=value.get('root_control_id',value.get('id',value.get('control_id')))
            if cid in self.control_ids and cid not in self.blocked_controls:
                # Scope is structural. Authorized values remain byte/type exact even
                # when their text coincides with an excluded document caption.
                for task_field in ('value','name','semantics','editor'):
                    if task_field in value:out[task_field]=deepcopy(value[task_field]) if task_field!='editor' or value[task_field].get('deferred') else _editor_summary(value[task_field])
            if isinstance(out.get('name'),str) and re.match(r'^Log Out\s+.+',out['name'],re.I):out['name']='Log Out [account omitted]'
            if cid in self.blocked_controls:out['privacy_action_allowed']=False
            if any(cid in o['redacted'] for o in self.observations.values()):
                structural={'id','control_id','root_control_id','role','parent','children','kind',
                    'actions','region_id','parent_region_id','child_region_ids','snapshot_id',
                    'target','membership','counts','bounds','privacy_action_allowed'}
                out={k:v for k,v in out.items() if k in structural}
                out.update(privacy_omitted=True,unknown=True,privacy_action_allowed=False)
                out['value_evidence']={'precision':'privacy_unknown','exact_value_proven':False}
            if value.get('version')=='progressive-ui-v1':
                cols=value.get('columns',[])
                if cols and isinstance(out.get('items'),list):
                    for original_row,row in zip(value['items'],out['items']):
                        if isinstance(row,list) and len(row)==len(cols):
                            c=original_row[cols.index('id')]
                            for identity_key in ('id','actions','parent'):
                                if identity_key in cols:row[cols.index(identity_key)]=deepcopy(original_row[cols.index(identity_key)])
                            if c in self.control_ids and c not in self.blocked_controls:
                                for task_field in ('value','name'):
                                    if task_field in cols:row[cols.index(task_field)]=deepcopy(original_row[cols.index(task_field)])
                                if 'name' in cols and isinstance(row[cols.index('name')],str) and re.match(r'^Log Out\s+.+',row[cols.index('name')],re.I):
                                    row[cols.index('name')]='Log Out [account omitted]'
                            if any(c in o['redacted'] for o in self.observations.values()):
                                for key in ('name','value','states'):
                                    if key in cols:row[cols.index(key)]={'privacy_omitted':True,'unknown':True}
                coverage=out.get('coverage',{})
                coverage.update(privacy_filtered=True,negative_evidence_proven=False,uniqueness_proven=False)
                out['coverage']=coverage
            return out
        detail_summary=None;exact_detail_items={}
        if detail_id is not None:
            detail_sid=result.get('snapshot_id')
            if detail_sid not in self.observations:raise ScopeError('Detail requires exact registered snapshot')
            known=[self.observations[detail_sid]['controls'][detail_id]] if detail_id in self.observations[detail_sid]['controls'] else []
            if not known:raise ScopeError('Unknown detail identity')
            c=known[-1]
            detail_summary={k:deepcopy(c.get(k)) for k in ('id','role','name','value','states','parent','bounds','semantics')}
            detail_summary['value_evidence']=_value_summary(c.get('value_evidence',{}))
            if isinstance(c.get('editor'),dict):detail_summary['editor']=_editor_summary(c['editor'])
            for index,item in enumerate(result.get('items',[])):
                if isinstance(item,dict) and item.get('field')=='value':exact_detail_items[index]=deepcopy(item)
                if isinstance(item,dict) and item.get('field') in ('editor','value_evidence','capabilities'):
                    item.clear();item.update(kind='privacy_projected_evidence',full_evidence_retained_private=True,semantic_evidence='control_semantics')
        result=walk(result)
        if detail_summary is not None:
            for key in ('value','editor','semantics'):
                if len(json.dumps(detail_summary.get(key),ensure_ascii=True).encode())>2048:
                    detail_summary[key]={'deferred':True,'exact_value_in_this_summary':False,'read_original_control_detail_pages':True}
            result['control_semantics']=walk(detail_summary)
            for index,item in exact_detail_items.items():result['items'][index]=item
            # Exact value/name fragments are explicitly scoped to this allowed
            # control. Restore them from the caller's result after global metadata scrubbing.
            # The complete value also appears in control_semantics when bounded.
        result['privacy_scope']={'version':VERSION,'application':self.app['name'],
            'authorized_document_titles':sorted(self.document_titles),'unknown_content_not_absent':True,
            'recent_content_and_foreign_documents_redacted':True,'raw_evidence_retained_private':True,
            'implementation_provenance_omitted':True,'scope_is_not_action_authority':True}
        if len(json.dumps(result,sort_keys=True,ensure_ascii=True).encode())>16384:
            raise ScopeError('Task-scoped response exceeds disclosure byte bound; choose a smaller page or bounded control detail. No task action may be retried automatically.')
        return result


def scoped_tools_for_request(desktop_toolset, request):
    """Prepare one local privacy scope, then wrap unchanged Amplifier tools.

    Performs only normal local discovery. No scope is inferred from a model's
    answer. It must run before mounting a hosted provider or sending requests.
    The same wrapper is used for every comparison provider, including local.
    """
    import asyncio
    from amplifier_core.models import ToolResult
    inventory=desktop_toolset.desktop.apps()
    if inventory.get('status')!='ok':raise ScopeError('Privacy setup could not read installed app identities')
    scope=TaskObservationScope.from_request(request,inventory.get('apps',[]))
    discovered=desktop_toolset.desktop.app_windows(scope.app)
    if discovered.get('status')!='ok':raise ScopeError('Privacy setup could not reconcile app/window identities')
    quoted={match[1] for match in re.findall(r"(['\"])(.*?)\1",request)}
    titles={w.get('title') for w in discovered.get('windows',[]) if w.get('title') in quoted}
    if len(titles)>1:raise ScopeError('More than one quoted document matches; explicit scope clarification required')
    if titles:scope=TaskObservationScope(request,scope.app,document_titles=titles)
    def register_rows(rows, app_id=None):
        scope.register_windows([{'target':{'pid':w['pid'],'window_id':w['window_id']},
            'title':w.get('title'),'identity_proven':bool(w.get('app_identity_evidence'))}
            for w in rows],app_id)
    register_rows(discovered.get('windows',[]))
    if discovered.get('windows') and not scope.targets:
        raise ScopeError('No identity-proven app window matches an explicit requested title')
    lock=asyncio.Lock();blocked=False;audit_sequence=0
    def audit_event(tool,status,**details):
        nonlocal audit_sequence
        audit_sequence+=1
        if hasattr(desktop_toolset,'out'):
            from .engine.prototype.cli import private_json
            private_json(Path(desktop_toolset.out)/f'privacy-{audit_sequence:03d}.json',
                {'tool':tool,'status':status,'disclosure_counts':deepcopy(scope.audit),**details})
    def refresh():
        scope.register_apps([{'app_id':aid,**a} for aid,a in desktop_toolset._app_records.items()])
        for wid,w in desktop_toolset._window_records.items():
            if w['app_id'] not in scope.app_ids:continue
            scope.register_windows([{'window_id':wid,'target':w['target'],'title':w['raw'].get('title'),
                'identity_proven':bool(w['raw'].get('app_identity_evidence'))}],w['app_id'])
        for sid,o in desktop_toolset._observations.items():
            scope.register_observation(o,actions=list(desktop_toolset._actions.get(sid,{}).values()))
    class ScopedTool:
        def __init__(self, inner):
            self.inner=inner;self.name=inner.name;self.description=inner.description;self.input_schema=deepcopy(inner.input_schema)
        async def execute(self, arguments):
            nonlocal blocked
            async with lock:
                if blocked:return ToolResult(success=False,output={'status':'refused','code':'privacy_scope_blocked','action_started':False,'no_retry':True,'task_complete':False})
                try:
                    refresh();scope.check_call(self.name,arguments)
                except ScopeError as error:
                    return ToolResult(success=False,output={'status':'refused','code':'outside_task_disclosure_scope','reason':str(error),'action_started':False,'task_complete':False})
                result=await self.inner.execute(arguments)
                projection_stage='register_observation'
                try:
                    refresh()
                    projection_stage='project_result'
                    output=scope.project(self.name,result.output)
                    audit_event(self.name,'projected',original_sha256=_digest(result.output),projected_sha256=_digest(output))
                    # Raw native/framework errors can contain private titles or paths.
                    error={'message':'Tool reported an error; task-scoped output contains the available diagnosis.',
                           'raw_error_retained_private':True} if result.error is not None else None
                    return ToolResult(success=result.success,output=output,error=error)
                except Exception as error:
                    # The underlying effect may already have occurred. Do not
                    # mislabel this as a harmless predispatch refusal or retry.
                    blocked=True
                    audit_event(self.name,'projection_failed',effect_may_have_started=True,
                        projection_stage=projection_stage,exception_type=type(error).__name__,
                        exception_message=str(error),exception_detail_private_only=True)
                    return ToolResult(success=False,output={'status':'privacy_blocked','code':'task_disclosure_projection_failed',
                        'reason':'Private result retained; disclosure failed. Stop this task and inspect local evidence.',
                        'action_started':result.output.get('action_started') if isinstance(result.output,dict) else None,
                        'no_retry':True,'task_complete':False})
    metadata={'version':VERSION,'description':'Only the explicitly requested app/document is disclosed; Recent/foreign-document content is unknown and cannot be acted on.',
        'application':scope.app['name'],'document_titles':sorted(scope.document_titles),'identical_for_all_comparison_providers':True,
        'preflight_desktop_reads':2,'canonical_observations_and_guards_unchanged':True}
    return [ScopedTool(t) for t in desktop_toolset.tools()],metadata
