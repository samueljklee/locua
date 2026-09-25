"""Frozen local multistep projection comparison in the existing Amplifier loop.

This mounts synthetic observations/actions only. Independent review and state
oracles stay private; none is appended to a model request. Source and interface
inputs are frozen per phase, while one case-exposure ledger spans all models.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import patch

import continuity_eval as base
from locua.amplifier_session import (
    CONTEXT_CONFIG, VERIFIED_COMPLETION_POLICY, execute_session, verified_completion_checker,
)
from locua.amplifier_tools import DesktopToolset
from locua.engine.prototype.cli import private_json
from locua.instruction_policy import apply_tool_help, instruction_policy

ROOT=Path(__file__).resolve().parents[1]
VERSION='semantic-policy-eval-v1'
PROFILES=('continuity-v1','semantic-v1','semantic-v2')
MODELS=base.MODELS
INSTRUCTIONS='continuity-v1'
# Primary A/B preserves the production context budget and policy. Compaction is
# measured independently; a separate declared pressure trial must establish
# continuation if these small tasks do not naturally reach the trigger.
EVAL_CONTEXT=deepcopy(CONTEXT_CONFIG)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_json(path,value):
    """Mode600 replacement for mutable ledgers/handshakes; old entries retained."""
    path=Path(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        private_json(temporary,value);os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def read_cases(root):
    root=Path(root).resolve();manifest=json.loads((root/'cases.json').read_text());rows=[]
    for item in manifest['cases']:
        spec_path=root/(item['id']+'.json');oracle_path=root/(item['id']+'-oracle.json')
        if sha(spec_path)!=item['spec_sha256'] or sha(oracle_path)!=item['oracle_sha256']:
            raise ValueError('Frozen case/oracle bytes changed: '+item['id'])
        spec=json.loads(spec_path.read_text());oracle=json.loads(oracle_path.read_text())
        if spec['id']!=item['id'] or spec['split']!=item['split']:
            raise ValueError('Frozen case identity changed')
        rows.append((spec,oracle))
    return manifest,rows


class SemanticDesktop(base.SimulatedDesktop):
    """The same capability adapter with declarative observed controls/state."""
    def apps(self):
        result=super().apps();result['apps'][0]['name']='Layout Lab';return result

    def app_windows(self,app):
        result=super().app_windows(app);result['windows'][0]['title']='Layout Lab';return result

    def observe(self,target):
        if target.get('pid')!=41 or target.get('window_id')!=52:
            return {'status':'unavailable','reason':'Selected synthetic window is unavailable'}
        self.calls.append(('observe',deepcopy(target)))
        self.sequence+=1;sid='semantic-'+str(self.sequence);now=time.time_ns();controls=[]
        for index,spec in enumerate(self.spec['controls']):
            state_key=spec.get('state_key');role=spec['role'];value=deepcopy(spec.get('value'))
            states={'enabled':True} if role in ('AXTextField','AXButton','AXPopUpButton') else {}
            if state_key:
                if role=='AXButton':states['selected']=self.state[state_key]
                else:value=deepcopy(self.state[state_key])
            semantics={'identifier':spec['identifier']}
            if spec.get('help'):semantics['help']=spec['help']
            c={'id':sid+':'+spec['key'],'role':role,'name':spec.get('name'),
               'value':value,'states':states,'actions':[],
               'parent':sid+':'+spec['parent'] if spec.get('parent') else None,
               'semantics':semantics,
               'bounds':{'x':30,'y':20+index*28,'width':240,'height':24}}
            if role=='AXTextField':
                c['value_evidence']={'precision':'exact','exact_value_proven':True,'plane':'editor_buffer'}
            elif isinstance(value,str):
                c['value_evidence']={'precision':'display_only','exact_value_proven':False,'plane':'display'}
            controls.append(c)
        observation={'kind':'native_window_state','target':deepcopy(target),'snapshot_id':sid,
            'observed_at_ns':now,'provenance':{'observed_at_ns':now,'simulation':True},
            'controls':controls,'text':'','handles':{},'hierarchy':[],
            'coverage':{'complete':True,'simulation':True}}
        self.observations.append(deepcopy(observation))
        return {'status':'observed','observation':observation}

    def execute(self,action,observation):
        control=next(c for c in observation['controls'] if c['id']==action['control_id'])
        spec=next(c for c in self.spec['controls'] if c['identifier']==control['semantics']['identifier'])
        key=spec['key'];before=deepcopy(self.state);started_ns=time.time_ns()
        if key=='save':self.state['saved_bytes']=self.state['draft']
        elif spec.get('state_key'):
            self.state[spec['state_key']]=action['value'] if action['kind']=='set_text' else True
        else:raise ValueError('Synthetic control has no declared input effect')
        self.executions.append({'key':key,'action':deepcopy(action),'before':before,'after':deepcopy(self.state),
            'at_ns':started_ns,'completed_at_ns':time.time_ns(),'source_snapshot_id':observation['snapshot_id'],
            'source_target':deepcopy(observation['target']),'source_control_id':control['id'],
            'source_identifier':control['semantics']['identifier']})
        return {'status':'verified' if action['kind']=='set_text' else 'dispatched',
            'observation':self.observe(observation['target'])['observation'],
            'action_started':True,'driver_ack':{'effect':'unverifiable'}}


def audit_state(desktop,oracle,final,events):
    expected=oracle['expected_state'];required=oracle['required_effect_keys']
    exact=(set(desktop.state)==set(expected) and all(type(desktop.state[k]) is type(v) and desktop.state[k]==v for k,v in expected.items()))
    untouched=set(expected)-set(required);unintended=[];input_errors=[]
    goals={g['key']:g for g in oracle['goals']};specs={c['key']:c for c in desktop.spec['controls']}
    captures={o['snapshot_id']:o for o in desktop.observations};previous=desktop.spec['initial']
    for item in desktop.executions:
        if item['key'] not in required or any(base.digest(item['before'][k])!=base.digest(item['after'][k]) for k in untouched):
            unintended.append(item)
        goal=goals.get(item['key']);action=item.get('action',{});capture=captures.get(item.get('source_snapshot_id'),{})
        controls=[c for c in capture.get('controls',[]) if c.get('id')==item.get('source_control_id')]
        target_ok=(capture.get('target')==item.get('source_target')==action.get('target')=={'pid':41,'window_id':52})
        identity_ok=(goal is not None and len(controls)==1
            and action.get('control_id')==controls[0]['id']
            and controls[0].get('semantics',{}).get('identifier')==specs[item['key']]['identifier']==item.get('source_identifier')
            and action.get('snapshot_id')==capture.get('snapshot_id'))
        action_ok=(goal is not None and action.get('kind')==('set_text' if goal['kind']=='text' else 'press')
            and (goal['kind']!='text' or type(action.get('value')) is str and action['value']==goal['value']))
        chronology_ok=(type(item.get('at_ns')) is int and type(item.get('completed_at_ns')) is int
            and capture.get('observed_at_ns',2**64)<=item['at_ns']<=item['completed_at_ns'])
        transition_ok=(base.digest(item['before'])==base.digest(previous) and set(item['after'])==set(expected)
            and all(base.digest(item['before'][k])==base.digest(item['after'][k]) for k in expected if k!=item['key']))
        if not all((target_ok,identity_ok,action_ok,chronology_ok,transition_ok)):
            input_errors.append({'key':item['key'],'target_ok':target_ok,'identity_ok':identity_ok,
                'action_ok':action_ok,'chronology_ok':chronology_ok,'transition_ok':transition_ok})
        previous=item['after']
    keys=[e['key'] for e in desktop.executions]
    issued=(sorted(keys)==sorted(required) and oracle['minimum_dispatches']<=len(keys)<=oracle['maximum_dispatches']
            and not input_errors and base.digest(previous)==base.digest(desktop.state))
    checked=any(e['tool']=='locua_verify' and e['result'].get('status')=='verified' for e in events)
    final_verified=(final.get('status')=='verified_reviewed_scope' and final.get('all_reviewed_goals_verified') is True)
    latest=desktop.observations[-1] if desktop.observations else {}
    final_ui_ok=(bool(desktop.executions) and latest.get('observed_at_ns',0)>desktop.executions[-1].get('completed_at_ns',2**64)
                 and latest.get('target')=={'pid':41,'window_id':52})
    for predicate in oracle['goals']+oracle['preserves']:
        identifier=specs[predicate['key']]['identifier']
        rows=[c for c in latest.get('controls',[]) if c.get('semantics',{}).get('identifier')==identifier]
        prop=predicate.get('property','value')
        actual=(rows[0].get('value') if prop=='value' else rows[0].get('states',{}).get(prop)) if len(rows)==1 else None
        value_ok=(len(rows)==1 and type(actual) is type(predicate['value']) and actual==predicate['value'])
        if predicate.get('kind')=='text':
            evidence=rows[0].get('value_evidence',{}) if len(rows)==1 else {}
            value_ok=value_ok and evidence.get('plane')=='editor_buffer' and evidence.get('exact_value_proven') is True
        final_ui_ok=final_ui_ok and value_ok
    return {'passed':bool(exact and issued and not unintended and checked and final_verified and final_ui_ok),
        'independent_state_exact':exact,'requested_action_executed':issued,'unintended_changes':len(unintended),
        'model_requested_verification':checked,'fresh_reviewed_predicates_verified':final_verified,
        'dispatches':len(keys),'input_evidence_errors':input_errors,'independent_post_input_ui_verified':bool(final_ui_ok),
        'real_desktop_calls':0,'desktop_completion_credit':False}


def _interface_spec(profile,spec,out):
    desktop=SemanticDesktop(spec)
    owner=DesktopToolset({},out,spec['request'],lambda *_:'',desktop=desktop,tool_profile=profile)
    try:
        return [{'name':t.name,'description':t.description,'schema':deepcopy(t.input_schema)}
                for t in apply_tool_help(owner.tools(),INSTRUCTIONS)]
    finally:owner.close()


def prepare_phase(cases_root,out,*,cases=None):
    root=Path(out).resolve();root.mkdir(parents=True,mode=0o700,exist_ok=False)
    manifest,rows=read_cases(cases_root)
    ids=cases or [s['id'] for s,_ in rows]
    if not ids or len(set(ids))!=len(ids) or not set(ids)<={s['id'] for s,_ in rows}:
        raise ValueError('Known, distinct frozen cases required')
    interfaces={p:_interface_spec(p,rows[0][0],root/('interface-'+p)) for p in PROFILES}
    if any(interfaces[profile]!=interfaces[PROFILES[0]] for profile in PROFILES[1:]):
        raise ValueError('Projection A/B must preserve identical published tool schemas and descriptions')
    sources=base.source_hashes()
    for path in (Path(__file__),ROOT/'tools/continuity_cli_eval.py',ROOT/'tools/continuity_capture.py',
                 ROOT/'tools/comparison_audit.py',ROOT/'tools/loop_v9_eval.py'):
        sources[str(path.relative_to(ROOT))]=sha(path)
    phase={'version':VERSION,'created_ns':time.time_ns(),'cases_root':str(Path(cases_root).resolve()),
        'case_manifest_sha256':sha(Path(cases_root)/'cases.json'),'cases':ids,
        'models':list(MODELS),'profiles':list(PROFILES),'instruction_profile':INSTRUCTIONS,
        'system':instruction_policy(INSTRUCTIONS),'tool_specs':interfaces[PROFILES[0]],
        'same_system_schemas_and_descriptions':True,'source_hashes':sources,
        'context':EVAL_CONTEXT,'native_protocols':{'baseline':'qwen_json','comparator':'qwen_json','qwen38':'qwen_native_xml'},
        'compaction_policy':'Measure spontaneous compaction; frozen case force_compaction is deferred to a separate declared pressure phase',
        'compaction_required_for_primary_completion':False,
        'provider':'local','protocol_recovery':True,'rlcd':False,'runtime_model_decoder_cache_changed':False,
        'verified_completion_policy':VERIFIED_COMPLETION_POLICY,'max_calls':18,
        'timeout_s':{'baseline':120,'comparator':120,'qwen38':180},
        'heldout_rule':manifest['global_heldout_rule'],'scope':'Synthetic component behavior, not desktop completion'}
    private_json(root/'phase.json',phase)
    return phase


def load_phase(path,model,profile,case):
    path=Path(path).resolve();phase=json.loads((path/'phase.json').read_text())
    if model not in phase['models'] or profile not in phase['profiles'] or case not in phase['cases']:
        raise ValueError('Model/profile/case was not frozen in this phase')
    if sha(Path(phase['cases_root'])/'cases.json')!=phase['case_manifest_sha256']:
        raise ValueError('Frozen case manifest changed')
    for name,want in phase['source_hashes'].items():
        if sha(ROOT/name)!=want:raise ValueError('Frozen phase source changed: '+name)
    _,rows=read_cases(phase['cases_root']);spec,oracle=next(r for r in rows if r[0]['id']==case)
    return phase,spec,oracle


def exposure(cases_root,phase_path,model,profile,spec,*,report=None):
    """An append-only shared ledger: a fresh label cannot reset per model."""
    root=Path(cases_root);lock=root/'exposure-ledger.lock';lock.touch(mode=0o600,exist_ok=True)
    with lock.open('r+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        ledger_path=root/'exposure-ledger.json';ledger=json.loads(ledger_path.read_text())
        phase_hash=sha(Path(phase_path)/'phase.json')
        events=ledger['events'];same_case=[e for e in events if e['case']==spec['id']]
        if report is None:
            if spec['split']=='held-out' and any(e['phase_sha256']!=phase_hash for e in same_case):
                raise ValueError('Held-out case was exposed in another phase; it is no longer globally fresh')
            if any(e['phase_sha256']==phase_hash and e['model']==model and e['profile']==profile and e['event']=='started' for e in same_case):
                raise ValueError('This frozen lane/case was already attempted; no silent retries')
            lane=[e for e in events if e['event']=='finished' and e['model']==model and e['profile']==profile]
            if len(lane)>=2 and all(e.get('informed_failure') for e in lane[-2:]) and lane[-1]['failure_signature']==lane[-2]['failure_signature']:
                raise ValueError('Lane stopped after two equivalent informed failures')
        row={'event':'started' if report is None else 'finished','at_ns':time.time_ns(),
            'case':spec['id'],'split':spec['split'],'phase_sha256':phase_hash,'phase':str(Path(phase_path).resolve()),
            'model':model,'profile':profile,'globally_exposed_before_this_attempt':bool(same_case)}
        if report is not None:
            category=report['failure_category'];row.update(status=report['status'],
                informed_failure=category in ('argument_contract','reference_or_grounding','review_semantic_mismatch','repeated_nonprogress'),
                failure_signature=category)
        ledger['events'].append(row);replace_json(ledger_path,ledger)
        return row


def compaction_evidence(session,requests,reviews):
    compactions=[e for e in session.get('events',[]) if e['event']=='context:compaction']
    approvals=[r for r in reviews if r.get('accepted')]
    hits=[]
    for event in compactions:
        for request in requests:
            if request['at_ns']<event['at_ns']:continue
            prior=[r for r in approvals if r['at_ns']<event['at_ns']]
            if not prior:continue
            text=json.dumps(request['request'],ensure_ascii=False)
            if 'Retained task state' not in text:continue
            ids=[r['public_review'] for r in prior if r.get('public_review')]
            if ids and all(identifier in text for identifier in ids):
                hits.append({'compaction_at_ns':event['at_ns'],'provider_request_at_ns':request['at_ns'],
                             'retained_review_aliases':ids,'stale_authority_warning_present':'not fresh action authority' in text})
                break
    return {'real_compaction_events':len(compactions),'post_review_compaction_retained_bindings':bool(hits),
            'witnesses':hits,'canonical_transcript_preserved':True}


async def run_case(spec,oracle,out,provider,*,profile,context=None,max_calls=18,timeout_s=120,review_attestation=None,review_wait_s=180):
    from continuity_cli_eval import review_gate,digest as review_digest
    from locua.amplifier_contracts import _errors
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    desktop=SemanticDesktop(spec);reviews=[];requests=[];owner=None;review_wait_seconds=0.0
    decision_path=Path(review_attestation) if review_attestation else root/'review-decision.json'
    def ask(message,kind):
        nonlocal review_wait_seconds
        stamp=time.time_ns();review_path=owner.out/('review-'+str(owner._review_sequence)+'.json')
        review=json.loads(review_path.read_text()) if kind=='tool_scope_review' else {}
        observed=owner._observation(review['review_capture']['snapshot_id']) if review else None
        gate=review_gate(review,spec,oracle,live_observation=observed) if review else {'accepted':False,'reasons':['Unexpected clarification in a complete fixture']}
        wait_started=time.monotonic();attestation=None;pending=False
        if gate.get('decision')=='needs_independent_prose_review':
            pending_path=root/'review-pending.json'
            pending_record={'status':'pending_independent_prose_review','review_path':str(review_path.resolve()),
                'review_sha256':review_digest(review),'request_sha256':review_digest(spec['request']),
                'request':spec['request'],'decision_path':str(decision_path.resolve()),'gate':gate,
                'wait_limit_s':review_wait_s,'model_inference_paused':True}
            replace_json(pending_path,pending_record)
            print('Review ready for evaluator: '+str(pending_path.resolve()),flush=True)
            while True:
                try:attestation=json.loads(decision_path.read_text())
                except (OSError,ValueError):attestation=None
                bound=(isinstance(attestation,dict) and attestation.get('review_sha256')==review_digest(review)
                       and attestation.get('request_sha256')==review_digest(spec['request']))
                if bound and attestation.get('approved') is False:
                    gate={**gate,'decision':'reject','accepted':False,'reasons':['independent_prose_rejected'],
                          'independent_prose_attestation_used':True};break
                if bound:
                    gate=review_gate(review,spec,oracle,live_observation=observed,prose_attestation=attestation)
                    if gate.get('accepted'):break
                if time.monotonic()-wait_started>=review_wait_s:
                    pending=True;break
                time.sleep(.10)
            review_wait_seconds+=time.monotonic()-wait_started
            replace_json(pending_path,{**pending_record,'status':'pending' if pending else gate['decision']})
        row={'at_ns':stamp,'kind':kind,'accepted':gate.get('accepted') is True,'gate':gate,
             'review_path':str(review_path),'message':message,
             'public_review':'q'+str(owner._review_sequence),'pending_evaluator_response':pending,
             'attestation':attestation,'review_wait_s':time.monotonic()-wait_started if pending or attestation else 0.0}
        reviews.append(row);private_json(root/f'reviewer-{len(reviews):03d}.json',row)
        return 'run' if row['accepted'] else ''
    owner=DesktopToolset({},root/'tools',spec['request'],ask,desktop=desktop,tool_profile=profile)
    tools=apply_tool_help(owner.tools(),INSTRUCTIONS);schemas={t.name:t.input_schema for t in tools}
    bound=base.LoopBound(owner,max_calls=max_calls,repeat_limit=2)
    started=time.monotonic();result={};error=None;returned=False;original_complete=provider.complete
    async def complete(request,**kwargs):
        if bound.stop_reason:raise RuntimeError(bound.stop_reason)
        requests.append({'at_ns':time.time_ns(),'request':request.model_dump()})
        return await original_complete(request,**kwargs)
    provider.complete=complete
    try:
        with patch('locua.amplifier_session.CONTEXT_CONFIG',deepcopy(context or EVAL_CONTEXT)):
            task=asyncio.create_task(execute_session(spec['request'],provider,bound.wrap(tools),
                out=root/'session',system=instruction_policy(INSTRUCTIONS),max_iterations=max_calls,
                execution_facts=owner.model_interface.state_text,owner_cancellation=lambda:deepcopy(owner._cancellation),
                verified_completion=verified_completion_checker(owner),progress=lambda s:print(s,flush=True)))
            while not task.done():
                remaining=timeout_s-(time.monotonic()-started-review_wait_seconds)
                if remaining<=0:
                    task.cancel()
                    try:await task
                    except asyncio.CancelledError:pass
                    raise TimeoutError('Active component execution budget exhausted; evaluator review wait excluded')
                await asyncio.wait((task,),timeout=min(.25,remaining))
            result=await task
        returned=True
    except Exception as exc:error=type(exc).__name__+': '+str(exc)
    finally:
        provider.complete=original_complete
        if not returned and (root/'session/session.json').is_file():result=json.loads((root/'session/session.json').read_text())
        final=owner.finalize();events=owner.evidence['events'];audit=audit_state(desktop,oracle,final,events)
        visible=[json.loads(p.read_text()) for p in sorted(owner.out.glob('interface-*.json'))]
        continuation=compaction_evidence(result,requests,reviews)
        completion=base.completion_status(audit,error=error,session_returned=returned,
            session_cleanup=result.get('session_cleanup'),stopped=bool(bound.stop_reason or owner._cancellation))
        completion['outcome_achieved']=completion['outcome_achieved'] and audit['independent_post_input_ui_verified']
        category=base.classify(visible or events,audit,bound.stop_reason or error)
        if any(r.get('pending_evaluator_response') for r in reviews):
            category='review_audit_pending';completion['status']='pending_review'
        elif any(not r['accepted'] for r in reviews):category='review_semantic_mismatch'
        records=getattr(provider,'records',[]);generations=[r['generation'] for r in records if 'generation' in r]
        schema_errors=[]
        for e in visible:
            name=e.get('tool');arguments=e.get('arguments',e.get('input',{}))
            if name in schemas:
                errors=_errors(schemas[name],arguments)
                if errors:schema_errors.append({'sequence':e.get('sequence'),'tool':name,
                    'paths':[err['path'] for err in errors]})
        owner.close()
        report={'version':VERSION,'id':spec['id'],'split':spec['split'],'profile':profile,
            **completion,'audit':audit,'compaction':continuation,'failure_category':category,'error':error,
            'tool_calls':bound.calls,'model_calls':len(records),'provider_requests':len(requests),
            'known_usage':{k:sum(g.get('usage',{}).get(k,0) for g in generations) for k in ('input_tokens','output_tokens')},
            'elapsed_s':time.monotonic()-started-review_wait_seconds,'wall_s':time.monotonic()-started,
            'review_wait_s':review_wait_seconds,'known_generation_s':sum(g.get('timing',{}).get('generation_ms',0) for g in generations)/1000,
            'first_pass':{'native_protocol_valid':not any(r.get('status')=='failed' and 'NativeToolFormatError' in str(r.get('error')) for r in records),
                'schema_errors':schema_errors,'all_published_arguments_valid':not schema_errors,
                'first_refusal':next((e for e in visible if e.get('result',{}).get('status') in ('refused','unavailable','uncertain','canceled')),None),
                'review_semantics':reviews},
            'protocol_corrections':[r['protocol_recovery'] for r in records if 'protocol_recovery' in r],
            'memory':[g.get('model_info',{}).get('memory') for g in generations if g.get('model_info',{}).get('memory')],
            'actual_model_configurations':[{k:g.get('model_info',{}).get(k) for k in
                ('model_key','model_pin','decoder','template_kwargs','prompt_cache_policy','loaded_snapshot')}
                for g in generations[:1]],
            'reviews':reviews,'independent_state':deepcopy(desktop.state),'execution_ledger':desktop.executions,
            'human_assistance':0,'manager_prose_reviews':sum(r['gate'].get('independent_prose_attestation_used',False) for r in reviews),
            'automatic_structured_review':True,'ordinary_prose_independently_reviewed':True,
            'pending_review_uses_existing_cancel_mechanism':any(r.get('pending_evaluator_response') for r in reviews),
            'session_cleanup':result.get('session_cleanup'),'adapter_closed':desktop.closed,
            'real_desktop_calls':0,'desktop_completion_credit':False,'rlcd':False,
            'context':deepcopy(context or EVAL_CONTEXT),'instruction_profile':INSTRUCTIONS,
            'verified_completion_policy':VERIFIED_COMPLETION_POLICY}
        private_json(root/'requests.json',requests);private_json(root/'oracle.json',oracle)
        private_json(root/'summary.json',report)
    return report


def attest(pending,*,decision,reviewer,reason):
    from continuity_cli_eval import digest as review_digest
    record=json.loads(Path(pending).read_text());review=json.loads(Path(record['review_path']).read_text())
    if review_digest(review)!=record['review_sha256'] or review_digest(record['request'])!=record['request_sha256']:
        raise ValueError('Pending concrete review changed')
    if decision not in ('approve','reject') or not reviewer.strip() or not reason.strip():
        raise ValueError('Named reviewer, explicit decision and fidelity reason required')
    receipt={'review_sha256':record['review_sha256'],'request_sha256':record['request_sha256'],
             'approved':decision=='approve','contradictory_claims':decision!='approve',
             'reviewer':reviewer,'reason':reason,'at_ns':time.time_ns()}
    replace_json(record['decision_path'],receipt)
    return {'status':'attested','decision':decision,'path':record['decision_path']}


async def run(phase,out,*,model,profile,case,config=None,review_attestation=None):
    from provider_connection import make_provider
    frozen,spec,oracle=load_phase(phase,model,profile,case)
    exposure(frozen['cases_root'],phase,model,profile,spec)
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    backend=make_provider('local',model,root/'provider',config=config,max_calls=frozen['max_calls'])
    backend.protocol_recovery=frozen['protocol_recovery']
    report=None
    try:
        report=await run_case(spec,oracle,root/'case',backend,profile=profile,context=frozen['context'],
            max_calls=frozen['max_calls'],timeout_s=frozen['timeout_s'][model],review_attestation=review_attestation)
    finally:await backend.close()
    report.update(model=model,provider='local',native_protocol=frozen['native_protocols'][model],
                  phase_sha256=sha(Path(phase)/'phase.json'),provider_closed=backend._closed)
    exposure(frozen['cases_root'],phase,model,profile,spec,report=report)
    private_json(root/'summary.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare');p.add_argument('--cases-root',required=True);p.add_argument('--out',required=True)
    p.add_argument('--case',dest='cases',action='append')
    p=sub.add_parser('run');p.add_argument('--phase',required=True);p.add_argument('--out',required=True)
    p.add_argument('--model',choices=MODELS,required=True);p.add_argument('--profile',choices=PROFILES,required=True)
    p.add_argument('--case',required=True);p.add_argument('--config');p.add_argument('--review-attestation')
    p=sub.add_parser('attest');p.add_argument('--pending',required=True);p.add_argument('--decision',choices=('approve','reject'),required=True)
    p.add_argument('--reviewer',required=True);p.add_argument('--reason',required=True)
    args=vars(parser.parse_args());command=args.pop('command')
    result=prepare_phase(**args) if command=='prepare' else attest(**args) if command=='attest' else asyncio.run(run(**args))
    print(json.dumps({k:result[k] for k in ('version','id','model','profile','status','failure_category','elapsed_s','model_calls','tool_calls') if k in result}))
    return 0 if command in ('prepare','attest') or result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
