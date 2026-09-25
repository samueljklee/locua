"""Standard Amplifier tools over Locua's guarded native desktop capabilities.

No phase machine or model policy lives here. Tools may be called in any order
that their observed identities permit. Exploration is read-only except explicit
launch/activation; task input requires a human-reviewed scope. Full evidence is
retained privately, while paged results expose semantics, not raw driver handles.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .arithmetic_input import InputWitness, symbol
from .desktop_tools import DesktopTools
from .engine.prototype.cli import private_json
from .engine.prototype.core import _identity, _signature
from . import progressive_ui
from .amplifier_contracts import SPECS, PRESERVE, ArgumentContractError, validate_tool_arguments, persistence_specs, TEXT_PERSISTENCE_CONTRACT, LEGACY_PERSISTENCE_CONTRACT
from .exploration import Exploration
from .engine.prototype.perception import validate_observation
from .goal_verification import (BindingError, _DISPLAY, _TEXT, bind_for_review,
                                check_review_predicate, matches_binding, matches_readback_identity, verify)


def _proven_installed_app(app):
    """PID-independent identity only after the declared bundle checks succeed.

    Exact declared paths stay distinct even if the filesystem resolves aliases.
    This is evidence for discovery deduplication, never dispatch authority.
    """
    from .desktop_tools import _bundle_executable
    from xml.parsers.expat import ExpatError
    if not isinstance(app,dict):return None
    try:
        installed=_bundle_executable(app)
    except (ValueError,OSError,TypeError,ExpatError):return None
    return {'bundle_id':app['bundle_id'],'launch_path':app['launch_path'],
            'installed_bundle':installed}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


MODEL_RESPONSE_BYTES=12*1024


def _control(c):
    semantics=c.get('semantics',{})
    result={k:deepcopy(c.get(k)) for k in ('id','role','name','value','states','value_evidence')} | {
        'semantics':{k:deepcopy(semantics.get(k)) for k in ('title','description','help','identifier','value_description','ancestors')}}
    if isinstance(c.get('editor'),dict):result['editor']=deepcopy(c['editor'])
    return result


def _model_control(control):
    """Semantic evidence for selection; the unprojected control remains private."""
    result=deepcopy(control)
    evidence=result.get('value_evidence')
    if isinstance(evidence,dict):
        keep=('kind','precision','exact_value_proven','plane','atomic_capture',
              'committed_document_proven','saved_output_proven','structured_value_trimmed',
              'possible_placeholder','markdown_is_exact_attribute_read','markdown_value')
        result['value_evidence']={k:deepcopy(evidence[k]) for k in keep if k in evidence}
    editor=result.get('editor')
    if isinstance(editor,dict):
        summary={k:deepcopy(editor[k]) for k in ('plane','atomic_capture','coherence',
            'committed_document_proven','saved_file_proven') if k in editor}
        for key in ('focused','value_settable','selected_range','selected_text'):
            if isinstance(editor.get(key),dict):
                summary[key]={k:deepcopy(editor[key][k]) for k in ('status','value') if k in editor[key]}
        result['editor']=summary
    return result


def _model_page(page):
    result=deepcopy(page);metadata=page.get('metadata',{})
    provenance=metadata.get('inspection_provenance',{})
    memberships=provenance.get('all_control_region_memberships',{})
    competitors=set(metadata.get('competing_control_ids',[]))
    for item in result.get('items',[]):
        if isinstance(item.get('control'),dict):
            cid=item['control']['id'];item['control']=_model_control(item['control'])
            if cid in memberships:item['region_id']=memberships[cid]
            if 'competing_control_ids' in metadata:item['competing_control']=cid in competitors
    if metadata:
        # Keep semantic region identity, page coverage, and counts. Global
        # mappings/ID lists duplicate controls on other discoverable pages.
        compact={k:deepcopy(metadata[k]) for k in ('primary_control_count','context_control_count',
            'context_controls_are_not_action_candidates','all_regions_discoverable',
            'searched_controls','nonmatching_control_count','search_fields','values_searched',
            'empty_result_proves_absence') if k in metadata}
        if isinstance(metadata.get('region'),dict):
            compact['region']={k:deepcopy(metadata['region'][k]) for k in (
                'id','kind','label','root_control_id','semantic_descriptor','counts',
                'snapshot_id','target','parent_region_id','child_region_ids') if k in metadata['region']}
        if 'competing_control_ids' in metadata:compact['competing_control_count']=len(metadata['competing_control_ids'])
        catalog=metadata.get('catalog_coverage')
        if isinstance(catalog,dict):
            compact['catalog_coverage']={k:deepcopy(catalog[k]) for k in ('input_controls',
                'assigned_controls','unassigned_controls','all_regions_discoverable',
                'flat_fallback','hierarchy_basis','warnings') if k in catalog}
        if provenance.get('warnings'):compact['warnings']=deepcopy(provenance['warnings'])
        result['metadata']=compact
    return result


def model_projection(result, *, full_response_ref):
    """Project representation only: no task/goal inputs, filters, or new handles."""
    if 'sequence_evidence' in result:
        result=deepcopy(result)
        private=result.pop('sequence_evidence')
        result['sequence_evidence_ref']=full_response_ref+'#sequence_evidence'
        result['sequence_evidence_sha256']=_hash(private)
    if result.get('uncertain_action') is True:
        # Preserve the original failure and full captures in the private event.
        # Never turn a potentially executed operation into a harmless refusal.
        projected={k:deepcopy(result[k]) for k in ('status','code','scope_id','target',
            'snapshot_id','action_started','no_retry','task_complete','window_id',
            'reference_note','reconciliation') if k in result}
        raw=result.get('raw_action_result',{})
        verification=raw.get('verification')
        checked=verification.get('readback') if isinstance(verification,dict) else None
        checked=checked if isinstance(checked,dict) else {}
        reason=checked.get('reason') or raw.get('reason') or result.get('reason') or 'Action outcome is uncertain'
        if not isinstance(reason,str):reason=json.dumps(reason,ensure_ascii=True)
        if len(reason.encode('utf-8'))>1024:
            projected['reason']='Blocking detail exceeds the response budget; exact detail is retained privately.'
            projected['reason_detail']={'sha256':_hash(reason),'utf8_bytes':len(reason.encode('utf-8')),
                                        'full_response_ref':full_response_ref}
        else:projected['reason']=reason
        code=raw.get('code')
        if isinstance(code,str) and len(code.encode('utf-8'))<=128:projected['blocking_code']=code
        projected.update(operation_status=raw.get('status','uncertain'),
            retained_readback_available=bool(result.get('snapshot_id')),
            full_response_ref=full_response_ref,full_response_sha256=_hash(result),
            current_state_proven=False,input_authority_restored=False,
            next='Do not repeat the input. Use the documented read-only reconciliation if available; otherwise report the blocking evidence.')
        return projected
    projected=deepcopy(result);changed=False
    if isinstance(result.get('overview'),dict) and result['overview'].get('version') != progressive_ui.VERSION:
        projected['overview']=_model_page(result['overview']);changed=True
    if result.get('version') != progressive_ui.VERSION and result.get('snapshot_id') and ('items' in result or 'control' in result):
        projected=_model_page(result);changed=True
        if isinstance(result.get('control'),dict):projected['control']=_model_control(result['control'])
    if changed:
        projected['projection']={'version':'semantic-tool-response-v1','full_response_ref':full_response_ref,
            'full_response_sha256':_hash(result),'page_items_omitted':0,'actions_omitted':0,
            'omitted_implementation_metadata':['global control-to-region maps and duplicated ID lists',
                'source-code locations, contract revisions and duplicated raw attribute reads'],
            'semantic_ancestry_retained':True,'full_observation_used_for_guards':True}
    return projected


class _Tool:
    def __init__(self,toolset,name):
        self.toolset=toolset;self.name=name;self.description=toolset._specs[name][0];self.input_schema=deepcopy(toolset._specs[name][1])
    async def execute(self,input):
        from amplifier_core.models import ToolResult
        async with self.toolset._async_lock:
            output=self.toolset.call(self.name,input)
        # Success indicates the tool completed its operation, not task success.
        return ToolResult(success=output.get('status') not in ('refused','unavailable','uncertain','canceled','blocked'),output=output)


class DesktopToolset:
    def __init__(self,config,out,request,ask,progress=None,desktop=None,tool_profile='baseline',persistence_contract=None):
        if not isinstance(request,str) or not request.strip():raise ValueError('Original request required')
        if tool_profile not in ('baseline','fresh-region-v1','execution-state-v1','continuity-v1','semantic-v1','semantic-v2','step-v1','step-v2'):raise ValueError('Unknown tool profile')
        self.tool_profile=tool_profile
        self.persistence_contract=persistence_contract
        self._specs=persistence_specs(SPECS,contract=persistence_contract)
        self.request=request;self.ask=ask;self.progress=progress or (lambda _:None)
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=False);self.out.chmod(0o700)
        self.desktop=desktop if desktop is not None else DesktopTools(config,self.out/'desktop')
        self._async_lock=asyncio.Lock();self._lock=threading.RLock()
        self._app_records={};self._inventories={};self._window_records={};self._observations={};self._latest={};self._actions={}
        self._snapshot_refs={};self._driver_actions={}
        self._scopes={};self._closed=False;self._review_sequence=0;self._attention_sequence=0;self._cancellation=None
        self.exploration=Exploration()
        self.evidence={'persistence_contract':persistence_contract or LEGACY_PERSISTENCE_CONTRACT,'request':request,'request_sha256':_hash(request),'events':[],
            'scopes':{},'verification':{},'task_complete':False,'completion_authority':'fresh reviewed goals only; original request coverage requires caller assessment'}
        self.model_interface=None
        if tool_profile=='continuity-v1':
            from .model_interface import ModelInterface
            self.model_interface=ModelInterface(self)
        elif tool_profile=='semantic-v1':
            from .semantic_projection import SemanticModelInterface
            self.model_interface=SemanticModelInterface(self)
        elif tool_profile=='semantic-v2':
            from .semantic_projection_v2 import SemanticV2ModelInterface
            self.model_interface=SemanticV2ModelInterface(self)
        elif tool_profile=='step-v1':
            from .step_interface import StepModelInterface
            self.model_interface=StepModelInterface(self)
        elif tool_profile=='step-v2':
            from .step_interface import StepNamedModelInterface
            self.model_interface=StepNamedModelInterface(self)
    def tools(self):
        return self.model_interface.tools() if self.model_interface else [_Tool(self,name) for name in self._specs]
    def _write_evidence(self):
        temporary=self.out/('evidence-'+uuid.uuid4().hex+'.tmp')
        private_json(temporary,self.evidence)
        os.replace(temporary,self.out/'evidence.json')
    def _record(self,name,args,result):
        # Each retained result must carry the public reference needed to
        # recapture its window after earlier inventory messages are compacted.
        # Native pid/window numbers are evidence, not public tool arguments.
        result=deepcopy(result)
        reference_args=args if isinstance(args,dict) else {}
        def retained(mapping,key):return mapping.get(key) if isinstance(key,str) else None
        target=result.get('target')
        observation=retained(self._observations,result.get('snapshot_id',reference_args.get('snapshot_id')))
        scope=retained(self._scopes,result.get('scope_id',reference_args.get('scope_id')))
        window=retained(self._window_records,reference_args.get('window_id'))
        if target is None:
            target=(observation['target'] if observation else scope['target'] if scope else
                    window['target'] if window else None)
        if target is not None:
            references=[wid for wid,w in self._window_records.items() if w['target']==target]
            if references:
                # A fresh app inventory can give the same exact native target
                # another public alias. Use the latest registered alias; keep
                # all aliases available and revalidate the target before input.
                result['window_id']=references[-1]
                result['reference_note']='Use this public window_id for observe/activate; target contains native metadata. Retained references do not prove current state.'
            scopes=[{'scope_id':sid,'status':s['status'],
                     'covers_entire_request':s['covers_entire_request'],
                     'goal_ids':[g['id'] for g in s['goals']]}
                    for sid,s in self._scopes.items() if s['target']==target]
            if scopes:
                result['retained_scope_refs']={'items':scopes[-4:],'total':len(scopes),
                    'retained_evidence_only':True,'current_state_proven':False,
                    'action_authority_granted':False,
                    'next':'Use these exact scope_id references; locua_status pages full goals/effects/preserves. Every input still requires fresh guards.'}
        event={'sequence':len(self.evidence['events'])+1,'tool':name,'input':deepcopy(args),'result':deepcopy(result)}
        path=self.out/f'event-{event["sequence"]:03d}.json'
        visible=model_projection(result,full_response_ref=str(path)+'#result')
        # Index-paged inventories/status/all-control views can be reduced
        # without another application read. The original result stays private.
        if all(k in result for k in ('items','start','total','next_start')):
            count=len(result['items'])
            while self._response_bytes(visible)>MODEL_RESPONSE_BYTES and count>1:
                count=max(1,count//2);page=deepcopy(result);page['items']=page['items'][:count]
                end=page['start']+count
                page.update(next_start=end if end<page['total'] else None,omitted=page['total']-count,
                    representation_page={'requested_limit':args.get('limit'),
                        'returned_count':count,'max_response_bytes':MODEL_RESPONSE_BYTES,
                        'items_clipped':False,'limit_is_upper_bound':True})
                visible=model_projection(page,full_response_ref=str(path)+'#result')
                event['requested_page_result']=deepcopy(result)
                event['result']=deepcopy(page)
        ui=visible.get('overview',visible)
        reserved=(isinstance(ui,dict) and ui.get('version')==progressive_ui.VERSION or
            name in ('locua_observe','locua_inspect','locua_windows','locua_apps') and
            visible.get('status') in ('refused','unavailable','uncertain'))
        response_limit=MODEL_RESPONSE_BYTES-1200 if reserved else MODEL_RESPONSE_BYTES
        if self._response_bytes(visible)>response_limit and isinstance(visible.get('overview'),dict):
            # Fit discovery around the complete input receipt, including public
            # references and verification. Re-page retained evidence rather than
            # replacing a successful operation with a representation failure.
            page=visible['overview'];captured=self._observations.get(page.get('snapshot_id'))
            allowance=self._response_bytes(page)-(self._response_bytes(visible)-response_limit)-128
            if captured is not None and page.get('version')==progressive_ui.VERSION and allowance>=2048:
                try:
                    smaller=progressive_ui.overview(captured,
                        actions=list(self._actions[captured['snapshot_id']].values()),
                        max_bytes=allowance)
                    visible={**visible,'overview':smaller}
                except ValueError:
                    pass  # An unpageable receipt still uses the no-replay error.
        if self._response_bytes(visible)>response_limit and isinstance(visible.get('fresh_region'),dict):
            # Verification receipts vary in size. Budget continuation against
            # the complete enriched envelope, never fixed view allowances alone.
            # Re-page the same unfiltered region without another app capture.
            page=visible['fresh_region'];captured=self._observations.get(page.get('snapshot_id'))
            region=page.get('coverage',{}).get('scope',{}).get('region_id')
            allowance=self._response_bytes(page)-(self._response_bytes(visible)-response_limit)-128
            if captured is not None and region is not None and allowance>=2048:
                try:
                    smaller=progressive_ui.listing(captured,region_id=region,
                        actions=list(self._actions[captured['snapshot_id']].values()),
                        max_bytes=min(6000,allowance))
                    visible={**visible,'fresh_region':smaller}
                except ValueError:
                    pass  # The ordinary explicit overflow path remains honest.
        if self._response_bytes(visible)>response_limit:
            visible=self._overflow(event['result'],path,self._response_bytes(visible))
        # Remember only the page actually delivered, not a private page that
        # overflowed. Feedback has a reserved bounded envelope above.
        feedback=self.exploration.record(name,args,visible)
        # Index only the continuation page actually delivered. It is retained
        # evidence, not a second inspection call or revived action authority.
        if isinstance(visible.get('fresh_region'),dict):
            continuation={**visible['fresh_region'],'window_id':visible.get('window_id')}
            self.exploration.record(name,args,continuation)
        if feedback is not None:visible={**visible,'exploration_feedback':feedback}
        if visible!=event['result']:event['model_result']=deepcopy(visible)
        self.evidence['events'].append(event)
        private_json(path,event)
        self._write_evidence()
        return visible
    @staticmethod
    def _response_bytes(result):
        # Bound the complete model-visible JSON, including continuation and
        # projection metadata. ASCII escaping also covers non-ASCII literals.
        return len(json.dumps(result,sort_keys=True,allow_nan=False).encode('utf-8'))
    def _preview_bytes(self,result):
        path=self.out/f'event-{len(self.evidence["events"])+1:03d}.json'
        return self._response_bytes(model_projection(result,full_response_ref=str(path)+'#result'))
    def _overflow(self,result,path,size):
        # This describes a representation failure, not an action refusal. An
        # operation may already have completed; never invite its replay.
        snapshot=result.get('snapshot_id');observation=self._observations.get(snapshot)
        item=result.get('control') or next(iter(result.get('items',[])),{})
        control=item.get('control',item) if isinstance(item,dict) else {}
        return {'status':'unavailable','code':'model_response_too_large',
            'reason':'One complete item or the response envelope exceeds the model response byte budget; exact evidence was not clipped.',
            'operation_status':result.get('status'),'action_started':result.get('action_started'),
            'operation_may_have_completed':True,'do_not_repeat_operation':True,
            'snapshot_id':snapshot,'target':deepcopy(result.get('target',observation.get('target') if observation else None)),
            'window_id':result.get('window_id'),
            'scope_id':result.get('scope_id',item.get('scope_id') if isinstance(item,dict) else None),
            'control_id':control.get('id'),
            'retained_readback_available':observation is not None,
            'full_response_ref':str(path)+'#result','full_response_sha256':_hash(result),
            'response_bytes':size,'max_response_bytes':MODEL_RESPONSE_BYTES,
            'task_complete':False,'negative_evidence_proven':False,'uniqueness_proven':False,
            'next':'Use locua_status to recover current retained IDs; inspect a smaller observed region/control or a paged status section. A single oversized exact item is unsupported, not absent.'}
    def call(self,name,args):
        with self._lock:
            if self._cancellation is not None and name!='locua_status':
                return self._record(name,args,deepcopy(self._cancellation))
            try:
                if self._closed:raise ValueError('Toolset is closed')
                if name not in self._specs or not isinstance(args,dict):raise ValueError('Unknown tool or nonobject arguments')
                validate_tool_arguments(name,args,specs=self._specs)
                persistence=(self._text_persistence(args['goals'],stage='before_snapshot_binding')
                    if name=='locua_review' else None)
                if persistence is not None and not persistence['all_requirements_satisfied']:
                    result=self._persistence_refusal(persistence)
                else:
                    result=getattr(self,'_'+name[len('locua_'):])(**args)
            except ArgumentContractError as error:
                result=error.as_result()
            except (ValueError,KeyError,TypeError) as error:
                result={'status':'refused','reason':str(error),'task_complete':False}
            except Exception as error:
                result={'status':'unavailable','reason':str(error),'task_complete':False}
            return self._record(name,args,result)
    def _page(self,rows,start=0,limit=32):
        if type(start) is not int or start<0 or type(limit) is not int or not 1<=limit<=64:
            raise ValueError('Page start >=0 and limit 1..64 required')
        return {'items':deepcopy(rows[start:start+limit]),'total':len(rows),'start':start,
                'next_start':start+limit if start+limit<len(rows) else None,'omitted':len(rows)-len(rows[start:start+limit])}
    def _status_window(self,target):
        record=next(((wid,w) for wid,w in self._window_records.items() if w['target']==target),None)
        snapshot=self._latest.get(_hash(target));observation=self._observations.get(snapshot)
        result={'target':deepcopy(target),'window_id':record[0] if record else None,
                'snapshot_id':snapshot,'retained_capture_available':observation is not None}
        if record:
            row=record[1];app=self._app_records.get(row['app_id'],{})
            result.update(app_id=row['app_id'],application=app.get('name'),window_title=row['raw'].get('title'))
        if observation:
            result.update(capture_age_seconds=max(0,(time.time_ns()-observation['observed_at_ns'])/1e9),
                control_count=len(observation['controls']),source_complete=observation.get('coverage',{}).get('complete'),
                region_discovery={'tool':'locua_inspect','operation':'overview','snapshot_id':snapshot})
        return result
    def _status_scope(self,scope_id,scope):
        receipt=self.evidence['verification'].get(scope_id)
        active=scope['status'] in ('approved','reconciled_verified') and self._cancellation is None
        latest=self._latest.get(_hash(scope['target']))
        retained=active and receipt is not None and latest is not None
        all_goals=retained and {r['goal_id'] for r in receipt['goals']}=={g['id'] for g in scope['goals']}
        return {'scope_id':scope_id,'status':scope['status'],'summary':scope['summary'],
            'target':deepcopy(scope['target']),'latest_snapshot_id':latest,
            'goals':deepcopy(scope['goals']),
            'preserves':[{'target':p['goal']['target'],'property':p['binding']['property'],
                          'value':deepcopy(p['goal']['value'])} for p in scope['preserves']],
            'limitations':deepcopy(scope['limitations']),
            'unresolved_requirements':deepcopy(scope['unresolved_requirements']),
            'covers_entire_request_declared_by_user':scope['covers_entire_request'],
            'effects_count':len(scope['effects']),'issued_press_effects':deepcopy(scope['issued_press_effects']),
            'input_authority_available':scope['status']=='approved' and self._cancellation is None
                and not any(s['target']==scope['target'] and s.get('uncertain_action') for s in self._scopes.values()),
            'reconciliation':deepcopy(scope.get('reconciliation')),
            'arithmetic_issuance':scope['witness'].view(),
            'verification':{'validity':'retained_for_latest_capture' if retained else 'invalidated_or_unverified',
                'snapshot_id':latest if retained else None,
                'goals':[{'goal_id':r['goal_id'],'matched':r['matched'],'reason':r.get('reason')}
                         for r in receipt['goals']] if retained else [],
                'all_reviewed_goals_matched':receipt['all_reviewed_goals_matched'] if all_goals else None,
                'all_preservation_predicates_matched':receipt['all_preservation_predicates_matched'] if retained else None,
                'fresh_readback_performed_now':False,'current_state_proven':False}}
    def _status(self,operation='summary',scope_id=None,start=0,limit=4,needs=None):
        """Recover retained semantics without capturing, dispatching or reviving authority."""
        sections=('summary','windows','scopes','goals','effects','preserves','witness','clarifications','exploration','controls','needs')
        if operation=='needs':self.exploration.set_needs(needs)
        if operation not in sections:raise ValueError('Unknown status operation')
        scoped=operation in ('goals','effects','preserves','witness')
        if scoped and scope_id not in self._scopes:raise ValueError('Choose a retained scope_id from status')
        if not scoped and scope_id is not None:raise ValueError('scope_id is only supported for goals/effects/preserves/witness')
        # Validate pagination even for an empty section. No status call changes
        # the retained snapshot, approval, issuance ledger or receipt validity.
        self._page([],start,limit)
        latest_target=None
        for event in reversed(self.evidence['events']):
            if event['tool']=='locua_status':continue
            args=event['input']
            if not isinstance(args,dict):continue
            window=self._window_records.get(args.get('window_id')) if isinstance(args.get('window_id'),str) else None
            observation=self._observations.get(args.get('snapshot_id')) if isinstance(args.get('snapshot_id'),str) else None
            scope=self._scopes.get(args.get('scope_id')) if isinstance(args.get('scope_id'),str) else None
            latest_target=(window['target'] if window else observation['target'] if observation else
                           scope['target'] if scope else None)
            if latest_target is not None:break
        if latest_target is None and self._latest:
            latest_target=self._observations[next(reversed(self._latest.values()))]['target']
        result={'status':'ok','operation':operation,'original_request':self.evidence['request'],
            'request_sha256':self.evidence['request_sha256'],
            'task_state':'canceled' if self._cancellation else 'blocked' if any(s['status'] not in ('approved','reconciled_verified') for s in self._scopes.values()) else 'open',
            'cancellation':deepcopy(self._cancellation),
            'latest_target':self._status_window(latest_target) if latest_target else None,
            'scope_count':len(self._scopes),'known_window_count':len(self._window_records),
            'sections':list(sections),'all_scopes_discoverable':True,'all_regions_discoverable':True,
            'retained_evidence_only':True,'action_authority_granted':False,'task_complete':False,
            'saved_output_proven':False,'current_state_proven':False,
            'exploration':self.exploration.summary(),
            'limits':['Status performs no fresh app read; inspect retained evidence or observe the exact window.',
                      'Only fresh verification can establish the reviewed predicates; input still requires every existing guard.']}
        if operation in ('summary','scopes'):
            rows=[self._status_scope(sid,s) for sid,s in self._scopes.items()]
        elif operation=='windows':
            rows=[self._status_window(w['target']) for w in self._window_records.values()]
        elif operation=='clarifications':rows=deepcopy(self.evidence.get('clarifications',[]))
        elif operation in ('exploration','controls'):rows=self.exploration.rows(operation)
        elif operation=='needs':rows=[]
        else:
            scope=self._scopes[scope_id];details=self._status_scope(scope_id,scope)
            # The chosen section contains its complete predicates. Repeating
            # every other section here would make paging a large scope futile.
            details.pop('goals');details.pop('preserves');details['verification'].pop('goals')
            details['sections']={'goals':len(scope['goals']),'effects':len(scope['effects']),
                'preserves':len(scope['preserves']),'witness':len(scope['witness'].events)}
            details['other_sections_available_through_status']=True
            result['scope']=details
            if operation=='goals':
                rows=[{'goal':deepcopy(g),'observed_binding':deepcopy(scope['bindings'][g['id']]['review_descriptor'])}
                      for g in scope['goals']]
            elif operation=='effects':
                rows=[{**{k:deepcopy(v) for k,v in e.items() if k not in ('identity','bounds')},
                       'effect_index':i,'issued':i in scope['issued_press_effects'],
                       **({'target_name':e['identity'].get('name'),'target_role':e['identity'].get('role')}
                          if e['kind']=='press' else {})} for i,e in enumerate(scope['effects'])]
            elif operation=='preserves':
                rows=[{'predicate':deepcopy(p['goal']),'property':p['binding']['property'],
                       'observed_binding':deepcopy(p['binding']['review_descriptor'])} for p in scope['preserves']]
            else:rows=deepcopy(scope['witness'].events)
        return {**result,**self._page(rows,start,limit)}
    def _apps(self,query='',start=0,limit=32,inventory_id=None):
        return _apps_handler(self,query,start,limit,inventory_id)
    def _windows(self,app_id):
        return _windows_handler(self,app_id)
    def _app(self,app_id):
        if app_id not in self._app_records:raise ValueError('Choose an app_id returned by locua_apps')
        return self._app_records[app_id]
    def _window(self,window_id):
        if window_id not in self._window_records:
            raise ValueError('Use the public window_id (window:...) returned by locua_windows, observe, inspect or review; native target.window_id is metadata. Recover references with locua_status operation=windows.')
        return self._window_records[window_id]['target']
    def _number_format_app(self,target):
        app_ids={w['app_id'] for w in self._window_records.values() if w['target']==target}
        if len(app_ids)==1:return self._app_records.get(next(iter(app_ids)))
        if not app_ids or self.tool_profile not in ('continuity-v1','semantic-v1','semantic-v2','step-v1','step-v2'):return None
        apps=[self._app_records.get(identifier) for identifier in sorted(app_ids)]
        identities=[_proven_installed_app(app) for app in apps]
        if identities[0] is None or any(identity!=identities[0] for identity in identities):return None
        # The probe independently rechecks this exact target's process ownership.
        # These aliases have one proven installed identity, not competing apps.
        return apps[0]
    def _windows_result(self,app_id,result):
        rows=[]
        installed=(_proven_installed_app(self._app_records.get(app_id))
                   if self.tool_profile in ('continuity-v1','semantic-v1','semantic-v2','step-v1','step-v2') else None)
        for w in result.get('windows',[]):
            target={k:w[k] for k in ('pid','window_id')}
            identity={'app_id':app_id,'target':target}
            proof=w.get('app_identity_evidence') or {};process=proof.get('process') or {}
            if (installed and proof.get('installed_bundle')==installed['installed_bundle']
                and proof.get('process_rechecked') is True and process.get('pid')==target['pid']
                and process.get('executable_path')==installed['installed_bundle']['executable_path']
                and isinstance(process.get('started_at_utc'),str) and process['started_at_utc']):
                identity={'installed_app':installed,'target':target,
                          'process_started_at_utc':process['started_at_utc']}
            identifier='window:'+_hash(identity)[:24]
            self._window_records[identifier]={'target':target,'app_id':app_id,'raw':deepcopy(w)}
            rows.append({'window_id':identifier,'target':target,'title':w.get('title'),'is_on_screen':w.get('is_on_screen'),
                         'on_current_space':w.get('on_current_space'),'identity_proven':bool(w.get('app_identity_evidence'))})
        return {'status':result['status'],'windows':rows,'availability':result.get('availability'),
                'unresolved_pids':result.get('unresolved_pids',[]),'reason':result.get('reason')}
    def _launch(self,app_id):
        result=self.desktop.launch(self._app(app_id))
        return {'status':result['status'],'app':result.get('app'),'reason':result.get('reason'),
                'next':'Call locua_windows to inspect current app windows; launch is not task completion.',
                'action_started':result.get('action_started',False),'task_complete':False}
    def _activate(self,window_id):
        target=self._window(window_id);result=self.desktop.activate(target)
        self._invalidate_verification(target)
        self._latest.pop(_hash(target),None)
        return result
    def _retain(self,observation):
        validate_observation(observation,expected_target=observation['target'],max_age_s=30)
        sid=observation['snapshot_id'];key=_hash(observation['target'])
        if sid in self._observations and self._observations[sid]!=observation:
            raise ValueError('Snapshot identifier collision')
        self._observations[sid]=deepcopy(observation);self._latest[key]=sid
        self.exploration.retain(observation)
        self._invalidate_verification(observation['target'])
        catalog=self.desktop.actions(observation)
        if catalog.get('status')!='ok':raise ValueError('Fresh action catalog unavailable: '+str(catalog.get('reason')))
        from .action_references import public_catalog
        if sid not in self._snapshot_refs:self._snapshot_refs[sid]=len(self._snapshot_refs)+1
        public,private=public_catalog(catalog['actions'],self._snapshot_refs[sid])
        if sid in self._driver_actions and self._driver_actions[sid]!=private:
            raise ValueError('Retained snapshot action catalog changed')
        self._actions[sid]={a['id']:a for a in public};self._driver_actions[sid]=private
        private_json(self.out/('action-catalog-'+str(self._snapshot_refs[sid])+'.json'),
            {'snapshot_id':sid,'public_actions':public,'private_descriptors':private})
        private_json(self.out/('observation-'+_hash({'target':observation['target'],'snapshot':sid})[:24]+'.json'),observation)
        return sid
    def _observation(self,snapshot_id):
        if snapshot_id not in self._observations:raise ValueError('Observe the exact window first')
        o=self._observations[snapshot_id]
        if self._latest.get(_hash(o['target']))!=snapshot_id:raise ValueError('Snapshot superseded; inspect the latest capture')
        # Read-only inspection may use retained evidence. Mutation code obtains
        # a fresh capture and compares its full control signature before input.
        validate_observation(o,expected_target=o['target'],now_ns=time.time_ns())
        return o
    def _overview(self,o):
        try:
            return progressive_ui.overview(o,actions=list(self._actions[o['snapshot_id']].values()))
        except ValueError as error:
            # Representation failure must not erase a successful input receipt
            # or invite replay. Full retained state remains inspectable.
            return {'status':'unavailable','code':'overview_representation_unavailable',
                'reason':str(error),'snapshot_id':o['snapshot_id'],
                'next':'Use locua_inspect operation=list on this retained snapshot; control details remain available.',
                'retained_observation_only':True,'action_authority':False}
    def _observe(self,window_id):
        target=self._window(window_id);self._latest.pop(_hash(target),None)
        self._invalidate_verification(target)
        result=self.desktop.observe(target)
        if result.get('status')!='observed':return result
        o=result['observation'];sid=self._retain(o)
        self._show_attention(o, label='window overview')
        return {'status':'observed','snapshot_id':sid,'target':o['target'],
            'overview':self._overview(o),
            'control_count':len(o['controls']),'coverage':o.get('coverage'),'task_complete':False}
    def _decorate(self,o,c):
        actions=[{k:deepcopy(a.get(k)) for k in ('id','kind','description','requires_value')}
                 for a in self._actions[o['snapshot_id']].values() if a['control_id']==c['id']]
        return {'control':_control(c),'actions':actions,'arithmetic_symbol':symbol(c)}
    def _inspect(self,snapshot_id,operation,region_id=None,control_id=None,query=None,role=None,cursor=None,limit=128):
        o=self._observation(snapshot_id);actions=list(self._actions[snapshot_id].values())
        if operation=='overview':
            result = progressive_ui.overview(o,actions=actions,cursor=cursor,limit=limit)
        elif operation=='list':
            result = progressive_ui.listing(o,region_id=region_id,role=role,query=query,
                actions=actions,cursor=cursor,limit=limit,
                include_values=self.tool_profile in ('continuity-v1','semantic-v1','semantic-v2','step-v1','step-v2'))
        elif operation=='control':
            result = progressive_ui.detail(o,control_id,actions=actions,cursor=cursor)
        else:
            raise ValueError('Use overview, list, or control')
        ids = ([control_id] if operation=='control' else
               [row[0] for row in result.get('items', []) if isinstance(row,list)] if operation=='list' else None)
        label = ('control '+str(self._find(o,control_id).get('name') or self._find(o,control_id)['role'])
                 if operation=='control' else 'listed UI region' if operation=='list' else 'window overview')
        self._show_attention(o, control_ids=ids, label=label, retained=True)
        return result
    def _show_attention(self,o,**kwargs):
        show=getattr(self.desktop,'attention',None)
        if show is None:return
        try:
            result=show(o,**kwargs)
        except Exception as exc:
            result={'status':'unavailable','reason':str(exc)}
        self._attention_sequence+=1
        private_json(self.out/f'attention-{self._attention_sequence:03d}.json',result)
        if result.get('status')=='shown':
            suffix=' (retained snapshot; no input)' if kwargs.get('retained') else ' (no input)'
            self.progress('Cursor: reading '+kwargs.get('label','UI')+suffix+'.')
        else:
            self.progress('Cursor unavailable: '+str(result.get('reason','unsupported driver'))+'.')
    def _find(self,o,cid):
        rows=[c for c in o['controls'] if c['id']==cid]
        if len(rows)!=1:raise ValueError('Control absent/ambiguous in current observation')
        return rows[0]
    def _text_persistence(self,goals,*,stage):
        """Requirements are model declarations; only trusted file evidence can prove them.

        This generic owner has neither a trusted document/file binding nor a
        supported way to prevent an application's autosave during buffer input.
        Accepting an arbitrary path or observing a hash cannot grant that input
        authority. Required persistence is deliberately unsupported here.
        """
        if self.persistence_contract is None:
            return {'contract_version':LEGACY_PERSISTENCE_CONTRACT,'stage':stage,
                    'requirements':[],'unmet':[],'all_requirements_satisfied':True,
                    'backing_file_status':'unknown','saved_output_proven':False,
                    'explicit_save_dispatched':None,
                    'proof_limit':'Historical buffer-only comparison contract; no backing-file preservation guarantee.'}
        rows=[]
        for goal in goals:
            if goal.get('kind')!='text':continue
            requirement=goal.get('persistence_requirement')
            missing=requirement not in ('not_requested','backing_file_unchanged','saved_output_required')
            supported=requirement=='not_requested'
            rows.append({'goal_id':goal.get('id'),'target':goal.get('target'),
                'requirement':requirement,'status':'missing' if missing else 'not_requested' if supported else 'unsupported',
                'satisfied':supported,'backing_file_status':'unknown','saved_output_proven':False,
                'explicit_save_dispatched':None,
                'reason':('Text goal requires an explicit persistence_requirement; omission is not not_requested.' if missing else
                    'No backing-file requirement was declared. Buffer verification does not prove unchanged bytes; the application may autosave.' if supported else
                    'The generic native editor has no trusted backing-file binding and no supported persistence isolation or saved-output verification. No edit is authorized for this requirement.')})
        pending=self.evidence.setdefault('unmet_persistence_requirements',[])
        for row in rows:
            if not row['satisfied'] and not any(_hash(p)==_hash(row) for p in pending):pending.append(deepcopy(row))
        return {'contract_version':TEXT_PERSISTENCE_CONTRACT,'stage':stage,'requirements':rows,'unmet':deepcopy(pending),
                'all_requirements_satisfied':not pending and all(r['satisfied'] for r in rows),
                'backing_file_status':'unknown','saved_output_proven':False,
                'explicit_save_dispatched':None,
                'proof_limit':'No Save dispatch is not proof that backing bytes remained unchanged.'}
    def _persistence_refusal(self,persistence):
        requirements={row.get('requirement') for row in persistence['unmet']}
        reason=('Locua cannot ensure the saved file stays unchanged: this app may save buffer edits automatically. No edit is authorized.'
            if 'backing_file_unchanged' in requirements else
            'Locua cannot verify saved output through this editor interface. No edit is authorized.'
            if 'saved_output_required' in requirements else
            'The plan must state whether saved-file changes are allowed, forbidden or required. No edit is authorized.')
        result={'status':'refused','code':'text_persistence_unmet',
                'reason':reason,
                'persistence':deepcopy(persistence),'unmet_persistence_requirements':deepcopy(persistence['unmet']),
                'action_started':False,'task_complete':False,'saved_output_proven':False,
                'next':'Inspect the explicit persistence requirement. A title, exact buffer or absence of Save cannot establish backing-file identity or prevent autosave. This capability is currently unsupported; do not downgrade the declared requirement.'}
        if self._cancellation is None and any(row.get('status')=='unsupported' and row.get('requirement') in
               ('backing_file_unchanged','saved_output_required') for row in persistence['unmet']):
            # This is a trusted capability boundary, not a repairable argument
            # error. Use the existing standard-loop stop path before another
            # generation can reinterpret the unmet requirement.
            result.update(status='blocked',stop_reason='unsupported_text_persistence',
                execution_stopped=True,authority_revoked=True,action_started_scope='current_refused_operation')
            self._cancellation={**deepcopy(result),'status':'blocked',
                'all_reviewed_goals_verified':False,'backing_file_status':'unknown',
                'explicit_save_dispatched':None}
            self.evidence['cancellation']=deepcopy(self._cancellation)
        return result
    def _review(self,snapshot_id,summary,goals,effects,preserves=None,limitations=None,covers_entire_request=False,unresolved_requirements=None):
        o=self._observation(snapshot_id);preserves=preserves or [];limitations=limitations or []
        unresolved_requirements=unresolved_requirements or []
        if not isinstance(summary,str) or not summary.strip() or not isinstance(goals,list) or not isinstance(effects,list):
            raise ValueError('Concrete summary, goals and effects required')
        if not effects or len(goals)>16 or len(effects)>32 or len(preserves)>64:
            raise ValueError('Review requires 1..32 effects, at most16 goals/64 explicit preserves')
        if type(covers_entire_request) is not bool or not isinstance(limitations,list) or any(not isinstance(x,str) for x in limitations):
            raise ValueError('Explicit coverage boolean and textual limitations required')
        if covers_entire_request and (not goals or unresolved_requirements):
            raise ValueError('A navigation-only scope or unmet user requirement cannot cover the entire request')
        persistence=self._text_persistence(goals,stage='before_review')
        if not persistence['all_requirements_satisfied']:return self._persistence_refusal(persistence)
        bound={};clean=[]
        for g in goals:
            c=self._find(o,g['control_id']);goal={k:deepcopy(v) for k,v in g.items() if k!='control_id'}
            if goal['id'] in bound:raise ValueError('Duplicate goal id')
            try:
                bound[goal['id']]=bind_for_review(goal,c,o)
            except BindingError as error:
                if str(error)!='calculation_requires_readable_display':raise
                return {'status':'refused','code':str(error),'snapshot_id':snapshot_id,
                    'target':deepcopy(o['target']),'goal_id':goal['id'],
                    'selected_control':{'id':c['id'],'role':c.get('role')},
                    'reason':'The selected '+str(c.get('role'))+' is not a supported readable calculation result. '
                        'Bind the observed result control with string readback, not a window/container or input button. '
                        'Its current value need not equal the requested result; a role alone does not prove a valid binding.',
                    'inspect_options':[
                        {'tool':'locua_inspect','arguments':{'snapshot_id':snapshot_id,'operation':'overview'}},
                        {'tool':'locua_inspect','arguments':{'snapshot_id':snapshot_id,'operation':'list'},
                         'optional_role_filters':sorted(_DISPLAY)}],
                    'next':'Inspect this retained snapshot, then explicitly choose the result control for review. '
                        'Do not recapture merely because review is next; act independently checks fresh state. '
                        'Use a goal effect for arithmetic inputs, not a list of navigation press effects.',
                    'arguments_rewritten':False,'action_started':False,'task_complete':False,
                    'current_state_proven':False,'action_authority':False}
            clean.append(goal)
        approved=[]
        for e in effects:
            if e.get('kind')=='goal':
                if e.get('goal_id') not in bound:raise ValueError('Effect references an unbound goal')
                if set(e)!={'kind','goal_id'}:raise ValueError('Goal effect permits no extra control/arguments')
                approved.append(deepcopy(e))
            elif e.get('kind')=='press':
                if set(e)!={'kind','control_id','purpose'} or not isinstance(e['purpose'],str) or not e['purpose'].strip():
                    raise ValueError('Navigation press requires observed control and concrete purpose')
                c=self._find(o,e['control_id'])
                if not (c.get('name') or c.get('semantics',{}).get('identifier') or c.get('semantics',{}).get('description')):
                    raise ValueError('A reviewable press requires an observed semantic label or identifier')
                if not any(a['kind']=='press' and a['control_id']==c['id'] for a in self._actions[snapshot_id].values()):
                    raise ValueError('Observed control has no supported press route')
                approved.append({**deepcopy(e),'identity':_identity(c,o),'bounds':deepcopy(c.get('bounds'))})
            else:raise ValueError('Unknown effect kind')
        keep=[]
        for n,p in enumerate(preserves):
            if not isinstance(p,dict) or set(p)!=set(PRESERVE['required']):raise ValueError('Explicit preservation predicate required')
            c=self._find(o,p['control_id'])
            kind=('text' if c.get('role') in _TEXT else 'display_value') if p['property']=='value' else 'state'
            g={'id':'preserve:'+str(n),'kind':kind,'target':str(c.get('name')),'value':p['value'],
               'evidence_plane':'editor_buffer' if kind=='text' else 'display'}
            if kind=='state':g['property']=p['property']
            b=bind_for_review(g,c,o)
            if b['property']!=p['property'] or not check_review_predicate(b,g,o)['matched_at_capture']:
                raise ValueError('Preserved property was unknown or did not match at the retained capture')
            keep.append({'goal':g,'binding':b})
        scope={'target':deepcopy(o['target']),'summary':summary,'goals':clean,'bindings':bound,
               'effects':approved,'preserves':keep,'limitations':deepcopy(limitations),'status':'proposed',
               'unresolved_requirements':deepcopy(unresolved_requirements),
               'covers_entire_request':covers_entire_request,'issued_press_effects':[]}
        self._review_sequence+=1
        # Session-local names are references, not credentials. The retained
        # contract and fresh guards supply authority; all reviews remain unique.
        scope_id='scope:'+str(self._review_sequence)
        capture={'snapshot_id':o['snapshot_id'],'observed_at_ns':o['observed_at_ns'],
                 'age_seconds':max(0,(time.time_ns()-o['observed_at_ns'])/1e9),
                 'retained_evidence_only':True,'current_state_proven':False,
                 'fresh_capture_required_before_input':True}
        review={'original_request':self.request,'scope_id':scope_id,'target':scope['target'],
                'review_capture':capture,
                'summary':summary,'goals':clean,'effects':approved,
                'unresolved_requirements':deepcopy(unresolved_requirements),
                'coverage_declaration':'This scope covers the entire original request; please reject if anything is missing.' if covers_entire_request else 'Partial scope only; other requested outcomes remain unproved.',
                'observed_bindings':{k:b['review_descriptor'] for k,b in bound.items()},
                'preserves':[{**p['goal'],'observed':p['binding']['review_descriptor']} for p in keep],
                'limits':['Native app coverage can be incomplete; only listed predicates are checked.',
                    'Display/editor evidence is not committed-document or saved-file evidence.']+limitations}
        window=next((w for w in self._window_records.values() if w['target']==scope['target']),None)
        app=self._app_records.get(window['app_id'],{}) if window else {}
        lines=['Review this scope before task input', 'Original request: '+self.request,
            'Application: '+str(app.get('name','Unknown'))+'; window: '+str(window['raw'].get('title') if window else scope['target']),
            'Plan: '+summary, review['coverage_declaration'],
            'Observed capture: '+str(capture['snapshot_id'])+'; '+format(capture['age_seconds'],'.1f')+
                ' seconds old. These values describe that capture; current state will be checked before input.']
        for goal in clean:
            descriptor=bound[goal['id']]['review_descriptor']
            label=descriptor.get('name') or descriptor.get('semantics',{}).get('identifier') or descriptor.get('role')
            if 'sibling_index' in descriptor:label+=' (read-only structural text slot '+str(descriptor['sibling_index'])+')'
            if goal['kind']=='calculation':lines.append('Calculate '+goal['expression']+'; read the result from '+str(label)+'.')
            else:lines.append('Set '+str(label)+' to '+repr(goal.get('value'))+'.')
            if goal['kind']=='state':
                state=repr(descriptor.get('state_at_binding')) if descriptor.get('state_observed_at_binding') else 'unknown'
                lines.append('  Verify: '+descriptor['property']+' = '+repr(goal['value'])+
                    '; state at capture: '+state+'. Fresh explicit readback is required; a press alone is not success.')
            else:
                lines.append('  Verify: '+bound[goal['id']]['evidence_plane']+'; value observed at capture '+repr(descriptor.get('value_at_binding'))+'.')
            if goal['kind']=='text' and self.persistence_contract is not None:
                lines.append('  Persistence requirement: '+goal['persistence_requirement']+'. Backing-file bytes are not verified; the application may autosave. Reject this scope if unchanged bytes or saved output are required.')
        for effect in approved:
            if effect['kind']=='press':lines.append('Press once: '+str(effect['identity'].get('name'))+' — '+effect['purpose']+'.')
            else:
                g=next(g for g in clean if g['id']==effect['goal_id'])
                lines.append('Allowed input: '+('observed arithmetic controls in this exact window' if g['kind']=='calculation' else 'only the bound '+g['target']+' control')+'.')
        for p in keep:lines.append('Preserve '+p['goal']['target']+': '+repr(p['goal']['value'])+'.')
        lines+=['Informational caveat: '+limit for limit in review['limits']]
        lines+=['Unmet request: '+need for need in unresolved_requirements]
        message='\n'.join(lines)
        private_json(self.out/('review-'+scope_id.split(':')[1]+'.json'),{**review,'displayed_text':message})
        answer=self.ask(message+'\nType run to approve, or Enter to cancel: ','tool_scope_review')
        if not isinstance(answer,str) or answer.strip().lower()!='run':
            self._cancellation={'status':'canceled','reason':'user_declined_review',
                'scope_id':scope_id,'action_started':False,'task_complete':False,
                'authority_revoked':True,'next':'This task was canceled. A new task requires a new user request.'}
            self.evidence['cancellation']=deepcopy(self._cancellation)
            self.evidence['verification']={}
            return deepcopy(self._cancellation)
        scope['status']='approved';self._scopes[scope_id]=scope
        scope['witness']=InputWitness()
        self.evidence['scopes'][scope_id]={k:deepcopy(v) for k,v in scope.items() if k!='witness'}
        return {'status':'approved','scope_id':scope_id,'goals':clean,'effects':approved,
                'target':scope['target'],'review_capture':capture,'task_complete':False,
                **({'arithmetic_input':scope['witness'].view()} if any(g['kind']=='calculation' for g in clean) else {})}
    def _scope(self,scope_id):
        if scope_id not in self._scopes or self._scopes[scope_id]['status']!='approved':
            raise ValueError('Approved, nonblocked scope required')
        return self._scopes[scope_id]
    def _act_sequence(self,scope_id,snapshot_id,steps):
        from .action_sequence import run_sequence
        return run_sequence(self,scope_id,snapshot_id,steps)
    def _invalidate_verification(self,target):
        for sid,s in self._scopes.items():
            if s['target']==target:self.evidence['verification'].pop(sid,None)
        self.evidence['task_complete']=False
    def _preserved(self,scope,o):
        return all(verify(p['binding'],p['goal'],o)['matched'] for p in scope['preserves'])
    def _permit(self,scope,o,action,value):
        c=self._find(o,action['control_id'])
        for effect_index,e in enumerate(scope['effects']):
            if e['kind']=='press':
                if effect_index in scope['issued_press_effects']:continue
                if (action['kind']=='press' and value is None and _identity(c,o)==e['identity']
                        and c.get('bounds')==e['bounds']):return {'kind':'press','effect_index':effect_index}
                continue
            goal=next(g for g in scope['goals'] if g['id']==e['goal_id']);binding=scope['bindings'][goal['id']]
            if goal['kind']=='text' and action['kind']=='set_text' and value==goal['value'] and matches_binding(binding,o,c['id']):
                return {'kind':'text','goal_id':goal['id']}
            if goal['kind']=='state' and action['kind']=='press' and value is None and matches_binding(binding,o,c['id']):
                checked=verify(binding,goal,o)
                if checked['matched']:raise ValueError('State already satisfied; unnecessary toggle refused')
                if checked.get('status')!='mismatch':raise ValueError('Fresh state is unknown; toggle refused')
                return {'kind':'state','goal_id':goal['id']}
            if goal['kind']=='calculation':
                token=symbol(c)
                if action['kind']=='press' and value is None and token is not None:
                    return {'kind':'arithmetic','goal_id':goal['id'],'symbol':token}
                if action['kind']=='set_text' and value==goal['expression'] and matches_binding(binding,o,c['id']):
                    return {'kind':'arithmetic_text','goal_id':goal['id']}
        raise ValueError('Action is outside every explicitly reviewed effect')
    @staticmethod
    def _reviewed_contract(scope):
        return {k:deepcopy(scope[k]) for k in ('target','goals','bindings','effects','preserves',
            'covers_entire_request','unresolved_requirements')}
    def _block_action(self,scope_id,scope,action,permit,pre,result,*,preservation_failure=False):
        """Retain one attempted operation without replay or renewed authority."""
        self._latest.pop(_hash(scope['target']),None);self._invalidate_verification(scope['target'])
        post=result.get('observation');sid=None;capture_error=None
        if isinstance(post,dict):
            try:
                validate_observation(post,expected_target=scope['target'],max_age_s=30)
                if post['snapshot_id']==pre['snapshot_id'] or post['observed_at_ns']<=pre['observed_at_ns']:
                    raise ValueError('Failed action readback is not independent of its pre-action capture')
                sid=self._retain(post)
                # A later matching state cannot erase a recorded preservation
                # failure, including an incomplete preservation readback.
                preservation_failure=preservation_failure or not self._preserved(scope,post)
            except Exception as error:
                capture_error=str(error)
                preservation_failure=preservation_failure or bool(scope['preserves'])
        elif scope['preserves']:preservation_failure=True
        scope['status']='blocked_preservation_unknown' if preservation_failure else 'blocked_uncertain'
        eligible=(scope['status']=='blocked_uncertain' and result.get('action_started') is not False
                  and permit.get('kind') in ('text','state')
                  and bool(scope['goals']) and all(g['kind'] in ('text','state') for g in scope['goals'])
                  and all(e['kind']=='goal' for e in scope['effects']))
        pending={'version':'uncertain-action-v1','attempted_action':deepcopy(action),
            'permit':deepcopy(permit),'target':deepcopy(scope['target']),
            'attempted_action_sha256':_hash(action),'permit_sha256':_hash(permit),
            'reviewed_contract_sha256':_hash(self._reviewed_contract(scope)),
            'pre_snapshot_id':pre['snapshot_id'],'returned_snapshot_id':post.get('snapshot_id') if isinstance(post,dict) else None,
            'completed_at_ns':time.time_ns(),'preservation_failure':preservation_failure,
            'eligible_for_read_only_reconciliation':eligible,'readback_retention_error':capture_error,
            'delivery_proven':False,'input_authority_restored':False}
        scope['uncertain_action']=pending
        self.evidence['scopes'][scope_id]['status']=scope['status']
        self.evidence['scopes'][scope_id]['uncertain_action']=deepcopy(pending)
        return {'status':'uncertain','code':'action_outcome_unverified','uncertain_action':True,
            'scope_id':scope_id,'target':deepcopy(scope['target']),'snapshot_id':sid,
            'action_started':result.get('action_started'),'no_retry':True,'task_complete':False,
            'raw_action_result':deepcopy(result),
            'reconciliation':{'available':eligible,'read_only':True,'input_authority_restored':False,
                'arguments':{'scope_id':scope_id,'reconcile':True} if eligible else None,
                'tool':'locua_verify' if eligible else None,
                'limits':'Checks the original complete reviewed predicates only. No rebinding, action replay, delivery proof or other-side-effect proof.'}}
    def _act(self,scope_id,snapshot_id,action_id,value=None,*,_pre_dispatch=None):
        scope=self._scope(scope_id);o=self._observation(snapshot_id)
        persistence=self._text_persistence(scope['goals'],stage='before_input')
        if not persistence['all_requirements_satisfied']:return self._persistence_refusal(persistence)
        if any(s['target']==scope['target'] and s.get('uncertain_action') for s in self._scopes.values()):
            raise ValueError('This target has an uncertain attempted input; read-only reconciliation never restores input authority, including through a new review')
        if o['target']!=scope['target']:raise ValueError('Action target differs from reviewed window')
        action=self._actions[snapshot_id].get(action_id)
        if action is None:raise ValueError('Choose an action_id returned for this snapshot')
        # A retained semantic choice may outlive a human review. It grants no
        # input authority here: full scope permission is evaluated below only
        # after a fresh capture and unchanged exact control signature.
        # Check all preservation predicates on a new capture before input. The
        # underlying DesktopTools performs a further exact control refresh.
        fresh_result=self.desktop.observe(scope['target'])
        self._latest.pop(_hash(scope['target']),None);self._invalidate_verification(scope['target'])
        if fresh_result.get('status')!='observed':return fresh_result
        fresh=fresh_result['observation'];self._retain(fresh)
        if fresh['snapshot_id']==snapshot_id:raise ValueError('Predispatch capture is not fresh')
        if not self._preserved(scope,fresh):raise ValueError('Pre-action preservation evidence unavailable or changed')
        if _pre_dispatch is not None:
            # Internal sequence guard; never supplied by model arguments. The
            # single-action path remains unchanged. This checks the actual new
            # capture before input, not just the prior post-action snapshot.
            transition=_pre_dispatch(fresh)
            if not isinstance(transition,dict) or transition.get('matched') is not True:
                return {'status':'refused','code':'sequence_fresh_state_changed',
                    'reason':transition.get('reason','Fresh sequence state is not established') if isinstance(transition,dict) else 'Invalid fresh sequence guard result',
                    'scope_id':scope_id,'snapshot_id':fresh['snapshot_id'],'target':fresh['target'],
                    'action_started':False,'task_complete':False,'transition':deepcopy(transition),
                    'next':'Inspect the newly retained state before choosing any further input.'}
        before_control=self._find(o,action['control_id']);identity=_identity(before_control,o)
        controls=[c for c in fresh['controls'] if _identity(c,fresh)==identity
                  and _signature(c,fresh)==_signature(before_control,o)]
        if len(controls)!=1:raise ValueError('Chosen action identity changed or became ambiguous')
        fresh_actions=[a for a in self._actions[fresh['snapshot_id']].values()
                       if a['control_id']==controls[0]['id'] and a['kind']==action['kind']]
        if len(fresh_actions)!=1:raise ValueError('Fresh action capability unavailable')
        fresh_action=deepcopy(fresh_actions[0]);permit=self._permit(scope,fresh,fresh_action,value)
        if (permit['kind']=='arithmetic' and not scope['witness'].known_start
                and permit['symbol'] not in ('clear','clear_entry')):
            return {'status':'refused','code':'arithmetic_start_unproved',
                    'reason':'Establish a known input start with an observed full reset or whole-expression replacement before entering arithmetic. A displayed zero or Clear Entry alone is insufficient.',
                    'snapshot_id':fresh['snapshot_id'],'target':fresh['target'],
                    'arithmetic_input':scope['witness'].view(),'action_started':False,
                    'task_complete':False,'arguments_rewritten':False,
                    'next':'Inspect supported controls in this fresh snapshot; choose the reset or replacement capability yourself.'}
        if value is not None:fresh_action['value']=value
        driver_action=deepcopy(self._driver_actions[fresh['snapshot_id']][fresh_action['id']])
        if value is not None:driver_action['value']=value
        if {**driver_action,'id':fresh_action['id']}!=fresh_action:
            raise ValueError('Public action reference differs from its private issued descriptor')
        scope['status']='issued_unverified'
        # A reviewed navigation press is one issuance, including a refused
        # attempt. Only a new human review can authorize another issuance.
        # Arithmetic goal effects intentionally remain repeatable capabilities.
        if permit['kind']=='press':scope['issued_press_effects'].append(permit['effect_index'])
        try:
            result=self.desktop.execute(driver_action,fresh)
        except Exception as error:
            return self._block_action(scope_id,scope,driver_action,permit,fresh,
                {'status':'uncertain','reason':str(error),'action_started':True,'no_retry':True})
        self._latest.pop(_hash(scope['target']),None);self._invalidate_verification(scope['target'])
        if result.get('status') not in ('verified','dispatched'):
            if result.get('status')=='refused' and result.get('action_started') is False:
                scope['status']='approved';return result
            return self._block_action(scope_id,scope,driver_action,permit,fresh,result)
        post=result.get('observation')
        if not isinstance(post,dict):
            return self._block_action(scope_id,scope,driver_action,permit,fresh,
                {**result,'status':'uncertain','reason':'Post-action observation absent','action_started':True})
        try:sid=self._retain(post)
        except Exception as error:
            return self._block_action(scope_id,scope,driver_action,permit,fresh,
                {**result,'status':'uncertain','reason':'Post-action evidence could not be retained: '+str(error),'action_started':True})
        if not self._preserved(scope,post):
            return self._block_action(scope_id,scope,driver_action,permit,fresh,
                {**result,'status':'uncertain','reason':'Post-action preservation could not be established; no retry','action_started':True},
                preservation_failure=True)
        if permit['kind']=='arithmetic':scope['witness'].record(permit['symbol'],snapshot_id=sid,descriptor=action['description'])
        elif permit['kind']=='arithmetic_text':scope['witness'].record_replacement(value,snapshot_id=sid,descriptor=action['description'])
        scope['status']='approved'
        self.evidence['scopes'][scope_id]['witness']=scope['witness'].view()
        self.evidence['scopes'][scope_id]['status']=scope['status']
        views={'overview':self._overview(post)}
        if self.tool_profile=='fresh-region-v1':
            from .interaction_context import post_action_views
            views=post_action_views(o,action['control_id'],post,list(self._actions[sid].values()))
        return {'status':result['status'],'snapshot_id':sid,'target':post['target'],'action_started':True,
                'driver_ack':result.get('driver_ack'),'verification':result.get('verification'),
                'arithmetic_input':scope['witness'].view(),'task_complete':False,
                **views,
                'next':'This is newly captured state after input. Inspect it or verify the reviewed goal.'}
    def _rebind_calculation_result(self,scope_id,goal_id,snapshot_id,control_id):
        """Explicit model-selected readback recovery, never a write rebinding.

        Retain the reviewed goal and issuance witness. Select by identity before
        comparing any value. Refuse re-selection of an existing mismatching
        result; this must not become an expected-answer search mechanism.
        """
        scope=self._scope(scope_id)
        goals=[g for g in scope['goals'] if g['id']==goal_id and g['kind']=='calculation']
        if len(goals)!=1:raise ValueError('Read-only result recovery requires one reviewed calculation goal')
        goal=goals[0];o=self._observation(snapshot_id)
        if o['target']!=scope['target']:raise ValueError('Result recovery target differs from reviewed window')
        if not scope['witness'].matches(goal['expression']):
            return {'status':'refused','code':'arithmetic_issuance_unproved','action_started':False,
                    'reason':'Execute and evaluate the complete reviewed expression from a known start before result rebinding.',
                    'arithmetic_input':scope['witness'].view(),'task_complete':False}
        selected=self._find(o,control_id)
        if selected.get('role') not in ('AXStaticText','AXHeading') or selected.get('actions'):
            raise ValueError('Result recovery supports read-only static text/headings, never an editor or input control')
        candidate=bind_for_review(goal,selected,o)
        self._latest.pop(_hash(scope['target']),None);self._invalidate_verification(scope['target'])
        captured=self.desktop.observe(scope['target'])
        if captured.get('status')!='observed':return captured
        fresh=captured['observation'];self._retain(fresh)
        if fresh['snapshot_id']==snapshot_id:
            raise ValueError('Result recovery requires a new capture, not the retained selected snapshot')
        old=scope['bindings'][goal_id]
        old_state=verify(old,goal,fresh)
        if old_state.get('reason')!='bound_target_absent':
            return {'status':'refused','code':'existing_result_binding_present','action_started':False,
                    'reason':'The original result binding is still present. Verify it; a value mismatch does not authorize selecting another result.',
                    'snapshot_id':fresh['snapshot_id'],'task_complete':False}
        if not any(matches_readback_identity(candidate,fresh,c['id']) for c in fresh['controls']):
            raise ValueError('Selected replacement readout changed or became ambiguous during fresh capture')
        if not self._preserved(scope,fresh):raise ValueError('Preservation evidence unavailable during result recovery')
        revision={'kind':'read_only_calculation_result','goal_id':goal_id,
                  'selected_snapshot_id':snapshot_id,'selected_control_id':control_id,
                  'checked_snapshot_id':fresh['snapshot_id'],'previous_binding':deepcopy(old),
                  'replacement_binding':deepcopy(candidate),'input_authority_changed':False,
                  'reviewed_goal_sha256':_hash(goal),'witness':scope['witness'].view(),
                  'selection_before_value_comparison':True}
        # Commit the explicit selection even when its value will mismatch. Never
        # iterate candidates or let an expected number select the binding.
        scope['bindings'][goal_id]=candidate
        scope.setdefault('binding_revisions',[]).append(revision)
        self.evidence['scopes'][scope_id]['bindings'][goal_id]=deepcopy(candidate)
        self.evidence['scopes'][scope_id]['binding_revisions']=deepcopy(scope['binding_revisions'])
        result=self._verify_scopes([(scope_id,scope,[goal])])
        result['binding_recovery']={k:deepcopy(v) for k,v in revision.items()
                                    if k not in ('previous_binding','replacement_binding')}
        return result
    def _reconcile(self,scope_id):
        """Check current original predicates; never authorize input or infer delivery."""
        scope=self._scopes.get(scope_id);pending=scope.get('uncertain_action') if scope else None
        if (not scope or scope['status']!='blocked_uncertain' or not isinstance(pending,dict)
                or pending.get('eligible_for_read_only_reconciliation') is not True
                or pending.get('preservation_failure') is not False):
            raise ValueError('Original blocked text/state action with complete preservation evidence required; navigation/arithmetic uncertainty cannot be reconciled')
        if (_hash(self._reviewed_contract(scope))!=pending['reviewed_contract_sha256']
                or _hash(pending['attempted_action'])!=pending['attempted_action_sha256']
                or _hash(pending['permit'])!=pending['permit_sha256']
                or pending['target']!=scope['target']
                or pending['attempted_action'].get('target')!=scope['target']
                or pending['permit'].get('kind') not in ('text','state')):
            raise ValueError('Original attempted action or reviewed predicates changed; reconciliation refused')
        self._latest.pop(_hash(scope['target']),None);self._invalidate_verification(scope['target'])
        captured=self.desktop.observe(scope['target'])
        if captured.get('status')!='observed':
            return {'status':'unverified','code':'reconciliation_capture_unavailable','scope_id':scope_id,
                'action_started':False,'no_retry':True,'task_complete':False,'input_authority_restored':False}
        fresh=captured['observation']
        validate_observation(fresh,expected_target=scope['target'],max_age_s=30)
        if (fresh['snapshot_id'] in (pending['pre_snapshot_id'],pending['returned_snapshot_id'])
                or fresh['observed_at_ns']<=pending['completed_at_ns']):
            raise ValueError('Reconciliation requires an independent new capture after the uncertain operation')
        self._retain(fresh)
        preserves=[{'predicate_id':p['goal']['id'],**verify(p['binding'],p['goal'],fresh)} for p in scope['preserves']]
        goals=[{'goal_id':g['id'],**verify(scope['bindings'][g['id']],g,fresh)} for g in scope['goals']]
        keep=all_bool(p.get('matched') is True for p in preserves)
        matched=bool(goals) and keep and all_bool(g.get('matched') is True for g in goals)
        proof={'goals':goals,'preserves':preserves,'all_reviewed_goals_matched':all_bool(g.get('matched') is True for g in goals),
            'all_preservation_predicates_matched':keep,'all_reviewed_predicates_matched':matched}
        reconciliation={'status':'current_predicates_verified' if matched else 'unverified',
            'snapshot_id':fresh['snapshot_id'],'observed_at_ns':fresh['observed_at_ns'],
            'original_contract_sha256':pending['reviewed_contract_sha256'],
            'original_uncertain_receipt_preserved':True,'delivery_proven':False,
            'other_side_effects_proven_absent':False,'input_authority_restored':False}
        scope.setdefault('reconciliation_attempts',[]).append(deepcopy(reconciliation))
        if not keep:
            scope['status']='blocked_preservation_unknown'
            pending['preservation_failure']=True;pending['eligible_for_read_only_reconciliation']=False
        if matched:
            scope['status']='reconciled_verified';scope['reconciliation']=deepcopy(reconciliation)
            self.evidence['verification'][scope_id]=deepcopy(proof)
        self.evidence['scopes'][scope_id].update(status=scope['status'],
            reconciliation=deepcopy(scope.get('reconciliation')),
            reconciliation_attempts=deepcopy(scope['reconciliation_attempts']),uncertain_action=deepcopy(pending))
        return {'status':'verified' if matched else 'unverified','scope_id':scope_id,'scopes':{scope_id:proof},
            'reconciliation':reconciliation,'scope_status':scope['status'],'snapshot_id':fresh['snapshot_id'],
            'target':deepcopy(scope['target']),'fresh_refresh':True,'action_started':False,'no_retry':True,
            'input_authority_restored':False,'task_complete':False,'saved_output_proven':False}
    def _verify(self,scope_id=None,goal_id=None,all=False,snapshot_id=None,control_id=None,reconcile=False):
        if reconcile:
            if all or goal_id is not None or snapshot_id is not None or control_id is not None:
                raise ValueError('Read-only reconciliation takes only scope_id and reconcile=true')
            return self._reconcile(scope_id)
        if snapshot_id is not None or control_id is not None:
            if all or not all_bool(x is not None for x in (scope_id,goal_id,snapshot_id,control_id)):
                raise ValueError('Result recovery requires scope_id, goal_id, snapshot_id and control_id only')
            return self._rebind_calculation_result(scope_id,goal_id,snapshot_id,control_id)
        if all:
            if scope_id is not None or goal_id is not None:raise ValueError('all verification takes no individual ids')
            return self.final_refresh()
        scope=self._scopes.get(scope_id)
        if not scope or scope['status'] not in ('approved','reconciled_verified'):
            raise ValueError('Approved or read-only reconciled scope required; use explicit reconcile=true for eligible uncertain actions')
        goals=[g for g in scope['goals'] if goal_id is None or g['id']==goal_id]
        if not goals and (goal_id is not None or not scope['preserves']):raise ValueError('No reviewed predicate to verify')
        return self._verify_scopes([(scope_id,scope,goals)])
    def _clarify(self,question,reason):
        if any(not isinstance(v,str) or not v.strip() or len(v)>1024 for v in (question,reason)):
            raise ValueError('A bounded question and reason for missing information are required')
        answer=self.ask('Original request: '+self.request+'\nMissing information: '+reason+'\n'+question+' ','tool_clarification')
        if not isinstance(answer,str):raise ValueError('User clarification must be text')
        row={'question':question,'reason':reason,'answer':answer,'action_authority_granted':False}
        self.evidence.setdefault('clarifications',[]).append(deepcopy(row))
        return {'status':'answered',**row}
    def _verify_scopes(self,entries):
        persistence=self._text_persistence([g for _,_,goals in entries for g in goals],stage='verification')
        if not persistence['all_requirements_satisfied']:
            return {**self._persistence_refusal(persistence),'status':'unverified','fresh_refresh':False,'scopes':{}}
        results={};captures={};number_formats={}
        for sid,s,goals in entries:
            key=_hash(s['target'])
            if key not in number_formats and any(g['kind']=='calculation' for g in goals):
                probe=getattr(self.desktop,'number_format',None)
                if callable(probe):
                    app=self._number_format_app(s['target'])
                    number_formats[key]=probe(app,s['target']) if app else {
                        'status':'unknown','reason':'independent_application_identity_unavailable'}
            if key not in captures:
                self._latest.pop(key,None);self._invalidate_verification(s['target'])
                captured=self.desktop.observe(s['target'])
                captures[key]=captured
                if captured.get('status')=='observed':self._retain(captured['observation'])
            captured=captures[key];rows=[];preserved=[]
            for predicate in s['preserves']:
                if captured.get('status')!='observed':proof={'matched':False,'reason':'Exact target readback unavailable','evidence':None}
                else:proof=verify(predicate['binding'],predicate['goal'],captured['observation'])
                preserved.append({'predicate_id':predicate['goal']['id'],**proof})
            preservation_ok=all_bool(p['matched'] for p in preserved)
            for goal in goals:
                if captured.get('status')!='observed':result={'matched':False,'reason':'Exact target readback unavailable','evidence':None}
                else:
                    o=captured['observation'];result=verify(s['bindings'][goal['id']],goal,o,
                        number_format=number_formats.get(key))
                    if goal['kind']=='calculation' and not s['witness'].matches(goal['expression']):
                        result={'matched':False,'reason':'Requested arithmetic input/evaluation issuance is unproved','display_readback':result,'evidence':None,
                                'arithmetic_input':s['witness'].view()}
                    elif goal['kind']=='calculation' and result.get('reason')=='bound_target_absent':
                        result['recovery']={'tool':'locua_verify','required_arguments':{'scope_id':sid,'goal_id':goal['id'],'snapshot_id':o['snapshot_id']},
                            'missing_argument':'control_id: inspect and explicitly select the new read-only result control',
                            'reason':'Layout changed; the previous readout identity is unavailable. This recovery preserves the reviewed expression and input witness and grants no input authority.'}
                    revisions=[r for r in s.get('binding_revisions',[]) if r['goal_id']==goal['id']]
                    if revisions:
                        revision=revisions[-1]
                        if o['snapshot_id'] in (revision['selected_snapshot_id'],revision['checked_snapshot_id']):
                            result={'matched':False,'reason':'Result recovery needs an independent new capture','evidence':None}
                        elif result.get('evidence'):
                            result['evidence'].update(binding_origin='model_selected_read_only_recovery',
                                binding_review_required=False,original_goal_review_preserved=True,
                                replacement_target_selected_by_human=False,input_authority_changed=False)
                    if not preservation_ok:result={'matched':False,'reason':'Preservation evidence unavailable or changed','evidence':None}
                rows.append({'goal_id':goal['id'],**result})
            results[sid]={'goals':rows,'preserves':preserved,
                'all_reviewed_goals_matched':bool(rows) and all_bool(r['matched'] for r in rows),
                'all_preservation_predicates_matched':preservation_ok,
                'all_reviewed_predicates_matched':bool(rows or preserved) and preservation_ok and all_bool(r['matched'] for r in rows)}
            self.evidence['verification'][sid]=deepcopy(results[sid])
        has_goals=any(r['goals'] for r in results.values())
        return {'status':'verified' if has_goals and all_bool(r['all_reviewed_predicates_matched'] for r in results.values()) else 'unverified',
                'scopes':results,'fresh_refresh':True,'saved_output_proven':False,'task_complete':False,
                'request_coverage_proven':False,'persistence':persistence}
    def final_refresh(self):
        """Caller must also assess original-request coverage; receipts are not Done."""
        if self._cancellation is not None:return deepcopy(self._cancellation)
        entries=[(sid,s,s['goals']) for sid,s in self._scopes.items() if s['status'] in ('approved','reconciled_verified') and (s['goals'] or s['preserves'])]
        if not entries:return {'status':'unverified','reason':'No approved goal bindings','task_complete':False}
        return self._verify_scopes(entries)
    def finalize(self):
        """Fresh result for the caller; never trust the model's closing text."""
        with self._lock:
            if self._cancellation is not None:
                self.evidence['final']=deepcopy(self._cancellation)
                self._write_evidence()
                return deepcopy(self._cancellation)
            result=self.final_refresh()
            persistence=self._text_persistence([g for s in self._scopes.values() for g in s['goals']],stage='completion')
            statuses={sid:s['status'] for sid,s in self._scopes.items()}
            blocked=any(status not in ('approved','reconciled_verified') for status in statuses.values())
            complete_declarations=[sid for sid,s in self._scopes.items()
                                   if s['covers_entire_request'] and s['status'] in ('approved','reconciled_verified')]
            verified=result.get('status')=='verified' and not blocked and persistence['all_requirements_satisfied']
            finalized={'status':'verified_reviewed_scope' if verified and complete_declarations else 'partial' if verified else 'blocked',
                'verification':result,'scope_statuses':statuses,
                'reconciliations':{sid:deepcopy(s['reconciliation']) for sid,s in self._scopes.items() if s.get('reconciliation')},
                'request_coverage':{'user_reviewed_complete_declaration':bool(complete_declarations),
                    'scope_ids':complete_declarations,'independent_language_coverage_proven':False},
                'all_reviewed_goals_verified':verified,'task_complete':False,
                'saved_output_proven':False,'committed_document_proven':False,
                'persistence':persistence,'backing_file_status':'unknown','explicit_save_dispatched':None,
                'limits':['Only captured reviewed predicates have positive proof.',
                          'A complete scope declaration was reviewed by the user; language coverage is not independently inferred.']}
            if persistence['requirements'] or persistence['unmet']:
                finalized['limits'].append('Exact editor-buffer readback proves neither unchanged backing-file bytes nor saved output. Automatic persistence is not controlled by this generic adapter.')
            if not persistence['all_requirements_satisfied']:
                finalized['reason']=self._persistence_refusal(persistence)['reason']
            self.evidence['final']=deepcopy(finalized)
            self.evidence['scopes']={sid:{k:deepcopy(v) for k,v in s.items() if k!='witness'}|{'witness':s['witness'].view()}
                                     for sid,s in self._scopes.items()}
            self._write_evidence()
            return finalized
    def close(self):
        with self._lock:
            if self._closed:return deepcopy(self.evidence.get('cleanup'))
            self._closed=True;self.evidence['cleanup']=self.desktop.close()
            self._write_evidence()
            return deepcopy(self.evidence['cleanup'])


# Builtin alias avoids collision with the public `all` argument in verification.
all_bool=all


def _apps_handler(self,query='',start=0,limit=32,inventory_id=None):
    if not isinstance(query,str):raise ValueError('Search query must be text')
    if inventory_id is None:
        if start:raise ValueError('Continue with the prior inventory_id')
        result=self.desktop.apps()
        if result.get('status')!='ok':return result
        rows=result['apps'];inventory_id='apps:'+_hash(rows)[:24];self._inventories[inventory_id]=deepcopy(rows)
    elif inventory_id not in self._inventories:raise ValueError('Unknown inventory; request a fresh first page')
    rows=self._inventories[inventory_id];items=[]
    for row in rows:
        identifier='app:'+_hash(row)[:24];self._app_records[identifier]=deepcopy(row)
        if query.casefold() not in (str(row.get('name',''))+' '+str(row.get('bundle_id',''))).casefold():continue
        items.append({'app_id':identifier,**{k:row.get(k) for k in ('name','bundle_id','pid','running','launch_path')}})
    return {'status':'ok','inventory_id':inventory_id,'query':query,'full_inventory_count':len(rows),
            'other_apps_discoverable':True,**self._page(items,start,limit)}


def _windows_handler(self,app_id):
    return self._windows_result(app_id,self.desktop.app_windows(self._app(app_id)))
