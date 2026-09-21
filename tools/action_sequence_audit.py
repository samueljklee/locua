#!/usr/bin/env python3
"""Offline sequence receipts, native writes and final-outcome audit. No execution.

Frozen oracles belong only to this evaluator. Existing snapshots keep their
timestamps; historical guard replay grants no present action authority.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tools'))
import comparison_audit as common

VERSION='locua-action-sequence-audit-v1'
READS=common.READS;SETUP=common.SETUP


def digest(value):return common.sha(common.canonical(value).encode())
def file_record(path):
    p=Path(path);raw=p.read_bytes();return {'path':str(p.resolve()),'sha256':common.sha(raw),'bytes':len(raw)}
def read(path):return common.strict_json(Path(path).read_text())
def write(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x') as f:json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')


def sealed(path):
    value=read(path);body=deepcopy(value);claim=body.pop('content_sha256',None)
    if claim!=digest(body):raise ValueError('Frozen suite/addendum digest mismatch')
    return value


def task_from_suite(path,task_id,amendment=None):
    suite=sealed(path);tasks=deepcopy(suite['tasks'])
    if amendment:
        change=sealed(amendment)
        if change['original_suite']['sha256']!=file_record(path)['sha256']:raise ValueError('Amendment names another suite')
        tasks=[change['task'] if t['id']==change['replace_task_id'] else t for t in tasks]
    rows=[t for t in tasks if t['id']==task_id]
    if len(rows)!=1:raise ValueError('Exactly one frozen task required')
    return suite,rows[0]


def private_ref(run,reference,fragment=None):
    if not isinstance(reference,str):raise ValueError('Private evidence reference required')
    name,sep,part=reference.partition('#');path=Path(name)
    if not path.is_absolute():path=Path(run)/path
    path=path.resolve(strict=True)
    if not path.is_relative_to(Path(run).resolve()):raise ValueError('Evidence reference leaves recorded run')
    if fragment is not None and (not sep or part!=fragment):raise ValueError('Unexpected private reference fragment')
    return path,read(path)


def historical_catalog(observation):
    """Use the pure catalog method, never initialize a desktop connection."""
    from threading import RLock
    from locua.desktop_tools import DesktopTools
    facade=SimpleNamespace(_lock=RLock(),_issued_observation=lambda o:None,_catalogs={},
        _result=lambda operation,**values:values,
        _failure=lambda operation,error:{'status':'refused','reason':type(error).__name__})
    result=DesktopTools.actions(facade,observation)
    if result.get('status')!='ok':raise ValueError('Historical action catalog could not be reconstructed')
    return {x['id']:x for x in result.get('actions',[])}


def load_action_catalogs(run,snapshots):
    """Validate saved short aliases against the unchanged private descriptors."""
    catalogs={};issues=[];sources=[]
    for path in sorted((Path(run)/'desktop').glob('action-catalog-*.json')):
        sources.append(file_record(path));row=read(path);sid=row.get('snapshot_id')
        try:
            if sid in catalogs:raise ValueError('Repeated snapshot catalog')
            if sid not in snapshots:raise ValueError('Catalog observation missing')
            actual=historical_catalog(snapshots[sid][0]);public=row['public_actions'];private=row['private_descriptors']
            if not isinstance(public,list) or not isinstance(private,dict):raise ValueError('Catalog types')
            by_id={x['id']:x for x in public}
            if len(by_id)!=len(public) or set(by_id)!=set(private):raise ValueError('Alias membership mismatch')
            if {x['id'] for x in private.values()}!=set(actual) or len(private)!=len(actual):raise ValueError('Catalog omitted competitors')
            for alias,pub in by_id.items():
                original=private[alias]
                if ({**pub,'id':original['id']}!=original or actual.get(original['id'])!=original
                        or pub.get('snapshot_id')!=sid):raise ValueError('Alias changes issued descriptor')
            catalogs[sid]={'actions':by_id,'private':private,'source':file_record(path),'valid':True}
        except (ValueError,KeyError,TypeError):
            issues.append('public_private_action_catalog_mismatch:'+str(sid))
            catalogs[sid]={'actions':{},'private':{},'source':file_record(path),'valid':False}
    return catalogs,issues,sources


def proposal_mapping(event,snapshots,scopes,catalogs=None):
    """Describe model source IDs from saved aliases or historical exact hashes."""
    from locua.arithmetic_input import symbol
    args=event.get('input') or {};pair=snapshots.get(args.get('snapshot_id'));scope=scopes.get(args.get('scope_id'))
    rows=[];catalog={};controls={};catalogs=catalogs or {};saved=catalogs.get(args.get('snapshot_id'))
    if pair:
        obs=pair[0];controls={c['id']:c for c in obs['controls']}
        catalog=saved['actions'] if saved is not None else historical_catalog(obs)
    for i,step in enumerate(args.get('steps') or [],1):
        action=catalog.get(step.get('action_id')) if isinstance(step,dict) else None
        control=controls.get(action['control_id'],{}) if action else {}
        rows.append({'step':i,'source_action_id':step.get('action_id') if isinstance(step,dict) else None,
            'found_in_reconstructed_original_catalog':action is not None,
            'private_issued_action_id':saved['private'].get(step.get('action_id'),{}).get('id') if saved and isinstance(step,dict) else action.get('id') if action else None,
            'kind':action.get('kind') if action else None,'control_id':control.get('id'),
            'observed_role':control.get('role'),'observed_name':control.get('name'),
            'arithmetic_symbol':symbol(control) if control else None,
            'value_present':isinstance(step,dict) and 'value' in step,
            'value_sha256':digest(step['value']) if isinstance(step,dict) and 'value' in step else None})
    return {'sequence_event':event.get('sequence'),'scope_exists':scope is not None,
        'source_snapshot_exists':pair is not None,
        'scope_target_matches_snapshot':bool(scope and pair and scope.get('target')==pair[0].get('target')),
        'steps':rows,'catalog_source':saved.get('source') if saved else None,
        'historical_catalog_reconstruction_only':True,'action_authority':False}


def reviewed_contract_check(event,source,evidence,snapshots):
    """Rebuild approval from its own capture; permit only proved read-only revisions.

    Goals/effects/preserves/coverage never come from the final mutable scope as
    authority. A later display binding revision cannot rewrite the sequence's
    original hash, and a revision before the sequence must be explicitly joined
    to its earlier tool event and captured evidence.
    """
    import locua.goal_verification as verifier
    from locua.amplifier_tools import _identity
    issues=[];scope_id=source.get('scope_id');scope=evidence.get('scopes',{}).get(scope_id) or {}
    events=evidence.get('events',[]);original=None;at_start=None;revisions=[]
    try:
        approvals=[r for r in events if r.get('tool')=='locua_review'
            and r.get('result',{}).get('status')=='approved' and r['result'].get('scope_id')==scope_id
            and type(r.get('sequence'))is int and r['sequence']<event['sequence']]
        if len(approvals)!=1:raise ValueError('One original earlier approval required')
        approval=approvals[0];args=approval['input'];obs=snapshots[args['snapshot_id']][0]
        controls={c['id']:c for c in obs['controls']};goals=[];bindings={};effects=[];preserves=[]
        for row in args['goals']:
            goal={k:deepcopy(v) for k,v in row.items() if k!='control_id'}
            if goal['id'] in bindings:raise ValueError('Repeated goal ID')
            bindings[goal['id']]=verifier.bind_for_review(goal,controls[row['control_id']],obs);goals.append(goal)
        for row in args['effects']:
            effect=deepcopy(row)
            if row['kind']=='press':
                c=controls[row['control_id']];effect.update(identity=_identity(c,obs),bounds=deepcopy(c.get('bounds')))
            elif row['kind']!='goal' or row.get('goal_id') not in bindings:raise ValueError('Unbound effect')
            effects.append(effect)
        for index,row in enumerate(args.get('preserves') or []):
            c=controls[row['control_id']];kind='text' if row['property']=='value' else 'state'
            goal={'id':'preserve:'+str(index),'kind':kind,'target':str(c.get('name')),'value':deepcopy(row['value']),
                'evidence_plane':'editor_buffer' if kind=='text' else 'display'}
            binding=verifier.bind_for_review(goal,c,obs)
            if binding['property']!=row['property']:raise ValueError('Preserve property changed')
            preserves.append({'goal':goal,'binding':binding})
        original={'target':deepcopy(obs['target']),'goals':goals,'bindings':bindings,'effects':effects,'preserves':preserves,
            'covers_entire_request':args.get('covers_entire_request',False),'unresolved_requirements':deepcopy(args.get('unresolved_requirements') or [])}
        for key in original:
            if key!='bindings' and scope.get(key)!=original[key]:issues.append('reviewed_contract_changed:'+key)
        at_start=deepcopy(original);expected_final=deepcopy(bindings)
        source_time=source['source_observation']['observed_at_ns']
        for goal in goals:
            audited=common.binding_revision_audit(scope,goal,snapshots,verifier)
            if not audited['valid']:issues.append('invalid_read_only_binding_revision:'+goal['id'])
            chain=[r for r in scope.get('binding_revisions',[]) if r.get('goal_id')==goal['id']]
            if chain and chain[0].get('previous_binding')!=bindings[goal['id']]:issues.append('revision_does_not_start_at_original_review:'+goal['id'])
            previous_event=approval['sequence'];previous_capture=obs['observed_at_ns']
            for rev in chain:
                matches=[r for r in events if r.get('tool')=='locua_verify'
                    and r.get('input',{}).get('scope_id')==scope_id and r['input'].get('goal_id')==goal['id']
                    and r['input'].get('snapshot_id')==rev.get('selected_snapshot_id')
                    and r['input'].get('control_id')==rev.get('selected_control_id')
                    and (r.get('result',{}).get('binding_recovery') or {}).get('checked_snapshot_id')==rev.get('checked_snapshot_id')]
                if len(matches)!=1 or type(matches[0].get('sequence'))is not int:
                    issues.append('binding_revision_event_not_unique:'+goal['id']);continue
                seq=matches[0]['sequence'];checked=snapshots[rev['checked_snapshot_id']][0]
                selected=snapshots[rev['selected_snapshot_id']][0]
                if seq==event['sequence'] or seq<=previous_event:issues.append('binding_revision_event_order_invalid')
                if selected['observed_at_ns']<previous_capture:issues.append('binding_revision_capture_order_invalid')
                previous_event=seq;previous_capture=checked['observed_at_ns']
                before=seq<event['sequence']
                if (before and checked['observed_at_ns']>source_time) or (not before and checked['observed_at_ns']<=source_time):
                    issues.append('binding_revision_capture_order_invalid')
                if before:at_start['bindings'][goal['id']]=deepcopy(rev['replacement_binding'])
                expected_final[goal['id']]=deepcopy(rev['replacement_binding'])
                revisions.append({'goal_id':goal['id'],'tool_event':seq,'applied_before_sequence':before,
                    'checked_snapshot_id':rev['checked_snapshot_id'],'human_selected_replacement':False})
        if scope.get('bindings')!=expected_final:issues.append('final_bindings_not_original_or_proved_revisions')
        if any(r.get('goal_id') not in bindings for r in scope.get('binding_revisions',[])):issues.append('revision_for_unreviewed_goal')
        if digest(at_start)!=source.get('reviewed_contract_sha256'):issues.append('sequence_reviewed_contract_hash_mismatch')
    except (ValueError,KeyError,TypeError) as error:
        issues.append('original_reviewed_contract_unreconstructable:'+type(error).__name__)
    return {'matched':not issues,'issues':sorted(set(issues)),
        'source_contract_sha256':source.get('reviewed_contract_sha256'),
        'original_approved_contract_sha256':digest(original) if original is not None else None,
        'sequence_start_contract_sha256':digest(at_start) if at_start is not None else None,
        'read_only_binding_revisions':revisions,'final_scope_used_as_original_authority':False}


def sequence_receipts(event,run,snapshots,*,zero_write_prevalidation=False,catalogs=None):
    """Reconcile the public tail with EVERY private attempt, including stops."""
    args=event.get('input') or {};result=event.get('result') or {};issues=[];acts=[]
    proposed=args.get('steps');public=result.get('receipts') or []
    if not isinstance(proposed,list) or not 1<=len(proposed)<=32:return [],{'issues':['invalid_model_step_list'],'partial':True}
    # A refused/canceled call may end before freezing any executable sequence.
    if (not result.get('source_evidence_ref') and (result.get('action_started')is False or zero_write_prevalidation)
            and result.get('status') in ('refused','canceled','unavailable') and not public
            and result.get('steps_attempted',0)==0):
        return [],{'status':result['status'],'steps_planned':len(proposed),'steps_attempted':0,
            'steps_completed':0,'partial':True,'preflight_only':True,'issues':[],
            'task_completion_inferred':False,'unattempted_steps':list(range(1,len(proposed)+1)),
            'zero_writes_and_no_private_sequence_files_independently_recorded':zero_write_prevalidation}
    try:
        source_path,source=private_ref(run,result.get('source_evidence_ref'))
        source_obs=source['source_observation'];source_steps=source['steps']
        valid=(source.get('scope_id')==args.get('scope_id')==result.get('scope_id')
            and source.get('sequence_id')==result.get('sequence_id')
            and source.get('source_snapshot_id')==args.get('snapshot_id')==result.get('source_snapshot_id')
            and source_obs.get('snapshot_id')==args.get('snapshot_id')
            and source.get('target')==source_obs.get('target') and len(source_steps)==len(proposed)
            and snapshots.get(source_obs['snapshot_id'],(None,None))[0]==source_obs)
        if not valid:issues.append('frozen_source_does_not_match_original_observation_or_proposal')
        source_catalog=None
        if catalogs is not None:
            saved=catalogs.get(source_obs['snapshot_id'])
            source_catalog=saved['actions'] if saved is not None else historical_catalog(source_obs)
        for index,(frozen,proposal) in enumerate(zip(source_steps,proposed),1):
            if (frozen.get('index')!=index or frozen.get('source_action_id')!=proposal.get('action_id')
                    or frozen.get('value_present')!=('value' in proposal)
                    or ('value' in proposal and frozen.get('value')!=proposal['value'])
                    or frozen.get('action',{}).get('id')!=proposal.get('action_id')
                    or frozen.get('action',{}).get('snapshot_id')!=args.get('snapshot_id')
                    or frozen.get('action',{}).get('target')!=source_obs.get('target')):
                issues.append('step_differs_from_model_original_proposal')
            if source_catalog is not None and source_catalog.get(proposal.get('action_id'))!=frozen.get('action'):
                issues.append('frozen_step_differs_from_issued_source_descriptor')
        if not source_path.name.endswith('-source.json'):raise ValueError('Source filename not canonical')
        paths=sorted(source_path.parent.glob(source_path.name.removesuffix('-source.json')+'-step-*.json'))
    except (OSError,ValueError,KeyError,TypeError) as error:
        return [],{'issues':['missing_or_invalid_source_evidence'],'detail':type(error).__name__,'partial':True}
    stopped=False;completed=0;started=0;unknown=False;refs=[];derived=[];terminal=None
    for index,path in enumerate(paths,1):
        try:
            path,full=private_ref(run,str(path));refs.append(file_record(path))
            if index>len(proposed):raise ValueError('Extra private step')
            proposal=proposed[index-1];frozen=source_steps[index-1];call=full['arguments'];raw=full['result']
            transition=full.get('transition') or {};pre=full.get('pre_dispatch_observation');action=full.get('pre_dispatch_action')
            if (path.name!=source_path.name.removesuffix('-source.json')+'-step-'+str(index).zfill(3)+'.json'
                    or full.get('step')!=index or full.get('sequence_id')!=source['sequence_id']
                    or full.get('source_action_id')!=proposal.get('action_id')
                    or call.get('scope_id')!=args.get('scope_id')
                    or ('value' in proposal)!=('value' in call)
                    or ('value' in proposal and call.get('value')!=proposal['value'])):
                issues.append('private_step_differs_from_model_proposal')
            if stopped:issues.append('step_attempted_after_terminal_sequence_receipt')
            derived.append({'step':index,'source_action_id':full.get('source_action_id'),
                'action_id':call.get('action_id'),'input_snapshot_id':call.get('snapshot_id'),
                'snapshot_id':raw.get('snapshot_id'),'status':raw.get('status'),
                'action_started':raw.get('action_started'),'transition_matched':transition.get('matched'),
                'full_response_ref':str(path)+'#result'})
            if raw.get('action_started')is not False:
                unknown=unknown or raw.get('action_started')is None
                if raw.get('action_started')is True:started+=1
                acts.append({'sequence':event.get('sequence'),'tool':'locua_act_sequence.step','input':call,'result':raw,
                    'source_action':frozen.get('action'),'source_observation':source_obs,'sequence_step':index,
                    'pre_dispatch_action':action,'pre_dispatch_observation':pre,'private_evidence':file_record(path)})
            if raw.get('action_started')is True:
                if catalogs is not None and isinstance(pre,dict):
                    saved=catalogs.get(pre.get('snapshot_id'))
                    current_catalog=saved['actions'] if saved is not None else historical_catalog(pre)
                    if current_catalog.get((action or {}).get('id'))!=action:issues.append('fresh_step_differs_from_issued_descriptor')
                known=snapshots.get((pre or {}).get('snapshot_id'),(None,None))[0]
                if (not isinstance(pre,dict) or pre!=known or not isinstance(action,dict)
                        or pre.get('snapshot_id')!=full.get('pre_dispatch_snapshot_id')
                        or pre.get('observed_at_ns')!=full.get('pre_dispatch_observed_at_ns')
                        or pre.get('target')!=source_obs.get('target')
                        or action.get('target')!=pre.get('target') or action.get('snapshot_id')!=pre.get('snapshot_id')
                        or action.get('kind')!=frozen.get('action',{}).get('kind')
                        or full.get('pre_dispatch_transition',{}).get('matched')is not True):
                    issues.append('missing_or_conflicting_fresh_dispatch_proof')
            succeeded=raw.get('status') in ('verified','dispatched')
            if succeeded:completed+=1
            if not succeeded or transition.get('matched')is not True:
                stopped=True;terminal={'step':index,'status':raw.get('status'),'code':raw.get('code'),
                    'post_transition':transition or None,'action_started':raw.get('action_started')}
        except (OSError,ValueError,KeyError,TypeError):issues.append('missing_or_invalid_private_step');stopped=True
    # Public receipts intentionally retain only the final eight; no lost prefix.
    tail=derived[-8:]
    if not isinstance(public,list) or len(public)!=len(tail):issues.append('public_private_step_receipt_mismatch')
    else:
        for actual,expected in zip(public,tail):
            try:
                resolved,_=private_ref(run,actual.get('full_response_ref'),'result')
                actual={**actual,'full_response_ref':str(resolved)+'#result'}
                if actual!=expected:issues.append('public_private_step_receipt_mismatch')
            except (OSError,ValueError,KeyError,TypeError):issues.append('missing_or_invalid_public_step_reference')
    counts={'steps_planned':len(proposed),'steps_attempted':len(paths),'steps_completed':completed,'steps_unattempted':len(proposed)-len(paths)}
    for key,n in {**counts,'receipts_omitted':max(0,len(paths)-8)}.items():
        if type(result.get(key))is not int or result[key]!=n:issues.append('receipt_counter_mismatch:'+key)
    expected_started=True if started else None if unknown else False
    if result.get('action_started')is not expected_started:issues.append('aggregate_action_started_mismatch')
    if result.get('status')=='sequence_completed' and (stopped or completed!=len(proposed)):issues.append('false_sequence_completion')
    if result.get('task_complete')is not False or result.get('goal_verified')is not False:issues.append('sequence_receipt_claims_task_verification')
    if result.get('do_not_repeat_sequence')is not bool(paths):issues.append('sequence_repeat_policy_missing')
    return acts,{'sequence_id':result.get('sequence_id'),'status':result.get('status'),**counts,
        'recorded_action_started_steps':started,'unknown_action_started_steps':sum(a['result'].get('action_started')is None for a in acts),
        'source_evidence':file_record(source_path),'step_evidence':refs,'partial':result.get('status')!='sequence_completed',
        'unattempted_steps':list(range(len(paths)+1,len(proposed)+1)),'terminal_step':terminal,
        'issues':sorted(set(issues)),'task_completion_inferred':False}


def load_snapshots(run,transport):
    from locua.engine.prototype.perception import normalize_observation
    snapshots={};issues=[]
    for p in sorted((run/'desktop').glob('observation-*.json')):
        o=read(p)
        if o['snapshot_id'] in snapshots:issues.append('duplicate_saved_snapshot_id')
        snapshots[o['snapshot_id']]=(o,str(p))
    for index,row in enumerate(transport,1):
        call=row.get('request') or {};raw=(row.get('response',{}).get('result') or {}).get('structuredContent') or {};sid=raw.get('snapshot_id')
        if call.get('name')!='get_window_state' or not sid or sid in snapshots:continue
        args=call.get('arguments') or {}
        try:
            obs=normalize_observation(row,kind='native_window_state',expected_target={k:args[k] for k in ('pid','window_id')},observed_at_ns=row['started_at_ns'])
            snapshots[sid]=(obs,str(run/'desktop/desktop/cua/transport.jsonl')+f'#record={index}')
        except (ValueError,KeyError,TypeError):issues.append('unparsed_native_capture:'+str(sid))
    return snapshots,issues


def match_at_capture(binding,goal,control,obs):
    from locua.goal_verification import bind_for_review
    candidate=bind_for_review(goal,control,obs)
    return (candidate['target']==binding.get('target') and candidate['core_identity']==binding.get('core_identity')
        and candidate['identity_policy']==binding.get('identity_policy'))


def reconcile_writes(writes,acts,scopes,snapshots):
    import locua.goal_verification as verifier
    from locua.amplifier_tools import _identity
    from locua.arithmetic_input import InputWitness,symbol
    witnesses={sid:InputWitness() for sid in scopes};rows=[];used_navigation=set()
    for index,driver in enumerate(writes):
        act=acts[index] if index<len(acts) else {};scope_id=act.get('input',{}).get('scope_id');scope=scopes.get(scope_id) or {}
        item={'write':index+1,'operation':driver['request']['name'],'scope_id':scope_id,'sequence_step':act.get('sequence_step'),
            'authorized':False,'classification':'unresolved_input','started_at_ns':driver.get('started_at_ns')}
        match=common.resolve_native_input(driver,snapshots)
        if not match or not scope:rows.append(item);continue
        control,obs,path=match;kind=driver['request']['name'];args=driver['request'].get('arguments') or {}
        item.update(control_id=control['id'],snapshot_id=obs['snapshot_id'],source=path)
        age=(driver['started_at_ns']-obs['observed_at_ns'])/1e9
        if obs['target']!=scope.get('target') or not 0<=age<=30:item['classification']='wrong_target_or_stale_dispatch';rows.append(item);continue
        original=act.get('source_action');original_obs=act.get('source_observation')
        if act.get('result',{}).get('action_started')is not True:
            item['classification']='input_start_uncertain';rows.append(item);continue
        if original:
            originals=[c for c in original_obs['controls'] if c['id']==original.get('control_id')]
            if (len(originals)!=1 or _identity(originals[0],original_obs)!=_identity(control,obs)
                    or originals[0].get('bounds')!=control.get('bounds')):
                item['classification']='sequence_target_differs_from_model_frozen_control';rows.append(item);continue
            pre=act.get('pre_dispatch_observation') or {};action=act.get('pre_dispatch_action') or {}
            peers=[c for c in pre.get('controls',[]) if c['id']==action.get('control_id')]
            if (len(peers)!=1 or pre.get('target')!=obs['target'] or pre.get('observed_at_ns',0)>obs['observed_at_ns']
                    or _identity(peers[0],pre)!=_identity(control,obs) or peers[0].get('bounds')!=control.get('bounds')
                    or (kind=='click' and action.get('kind')!='press') or (kind=='set_value' and action.get('kind')!='set_text')):
                item['classification']='driver_input_differs_from_sequence_fresh_action';rows.append(item);continue
        for effect_index,effect in enumerate(scope.get('effects',[])):
            if effect.get('kind')=='press':
                key=(scope_id,effect_index)
                if kind=='click' and key not in used_navigation and _identity(control,obs)==effect.get('identity') and control.get('bounds')==effect.get('bounds'):
                    used_navigation.add(key);item.update(authorized=True,classification='reviewed_navigation');break
                continue
            goal=next((g for g in scope.get('goals',[]) if g.get('id')==effect.get('goal_id')),None)
            if effect.get('kind')!='goal' or not goal:continue
            binding=scope.get('bindings',{}).get(goal['id']);token=symbol(control)
            if goal['kind']=='calculation' and kind=='click' and token is not None:
                legal=witnesses[scope_id].known_start or token in ('clear','clear_entry')
                witnesses[scope_id].record(token,snapshot_id=obs['snapshot_id'],descriptor={})
                item.update(authorized=legal,classification='arithmetic_input',symbol=token,goal_id=goal['id']);break
            if not binding:continue
            try:bound=match_at_capture(binding,goal,control,obs)
            except (ValueError,KeyError,TypeError):bound=False
            if bound and goal['kind']=='text' and kind=='set_value' and args.get('value')==goal['value']:
                item.update(authorized=True,classification='exact_editor_write',goal_id=goal['id']);break
            if bound and goal['kind']=='state' and kind=='click':
                prop=binding['property'];current=control.get('states',{}).get(prop)
                if type(current)is bool and current!=goal['value']:item.update(authorized=True,classification='reviewed_state_change',goal_id=goal['id']);break
            if bound and goal['kind']=='calculation' and kind=='set_value' and args.get('value')==goal['expression']:
                witnesses[scope_id].record_replacement(args['value'],snapshot_id=obs['snapshot_id'],descriptor={})
                item.update(authorized=True,classification='reviewed_expression_replacement',goal_id=goal['id']);break
        rows.append(item)
    return rows,{key:w.view() for key,w in witnesses.items()}


def final_outcomes(task,summary,evidence,snapshots,transport,witnesses,locale_evidence=None):
    import locua.goal_verification as verifier
    import locua.number_format as number_format_module
    oracle=task['oracle'];kind=oracle['kind'];reports=[];locale_evidence=locale_evidence or {}
    if kind not in ('calculation','text','states'):return {'supported':False,'pass':False,'reason':'Missing public execution/outcome primitive; family retained as capability gap.'}
    final=summary.get('verification') or {};receipts=(final.get('verification') or {}).get('scopes') or {}
    writes=[r for r in transport if r.get('request',{}).get('name') not in READS|SETUP and r.get('request',{}).get('name')]
    last_end=max((r.get('started_at_ns',0)+int(r.get('response',{}).get('wall_ms',0)*1e6) for r in writes),default=0)
    for scope_id,scope in evidence.get('scopes',{}).items():
        app=common.application_proof(task,scope,evidence.get('events',[]));last_read=None
        for row in sorted(transport,key=lambda r:r.get('started_at_ns',0)):
            call=row.get('request') or {};args=call.get('arguments') or {}
            if call.get('name')=='get_window_state' and {k:args.get(k) for k in ('pid','window_id')}==scope.get('target'):
                last_read=((row.get('response') or {}).get('result') or {}).get('structuredContent',{}).get('snapshot_id')
        for goal in scope.get('goals',[]):
            binding=scope.get('bindings',{}).get(goal['id']) or {};role=binding.get('review_descriptor',{}).get('role');name=binding.get('review_descriptor',{}).get('name')
            desired_match=(goal.get('kind')==kind or kind=='states' and goal.get('kind')=='state')
            if kind=='calculation':desired_match=desired_match and re.sub(r'\s+','',goal.get('expression',''))==re.sub(r'\s+','',oracle['expression'])
            elif kind=='text':desired_match=desired_match and goal.get('value')==oracle['value'] and any(a.get('name')==oracle['document'] for a in binding.get('core_identity',{}).get('ancestors',[]))
            else:desired_match=desired_match and any(x['name']==name and x['value'] is goal.get('value') for x in oracle['desired'])
            found=[r for r in receipts.get(scope_id,{}).get('goals',[]) if r.get('goal_id')==goal['id']];receipt=found[0] if len(found)==1 else {};proof=receipt.get('evidence') or {};pair=snapshots.get(proof.get('snapshot_id'))
            checked=None;exact=False;preserves=[];parsed=None;format_path=None;revision=common.binding_revision_audit(scope,goal,snapshots,verifier)
            if pair:
                obs,path=pair
                structural=(proof.get('snapshot_id')==last_read and proof.get('observed_at_ns')==obs.get('observed_at_ns')
                    and proof.get('target')==obs.get('target')==scope.get('target') and proof.get('binding_id')==binding.get('id')
                    and obs.get('observed_at_ns',0)>last_end and proof.get('snapshot_id') not in revision['forbidden_verification_snapshots'])
                clock=SimpleNamespace(time_ns=lambda:obs['observed_at_ns'])
                try:
                    number_format=None;interpretation=proof.get('numeric_interpretation')
                    if interpretation is not None:
                        rows=locale_evidence.get(interpretation.get('locale_evidence_sha256'),[])
                        bundles={r['bundle_id'] for r in app['matches'] if r.get('bundle_id')}
                        if len(rows)!=1 or len(bundles)!=1:raise ValueError('Unique recorded locale/app evidence required')
                        number_format,format_path=rows[0]
                        parsed=number_format_module.parse_display_number(proof.get('actual'),number_format,
                            expected_target=obs['target'],expected_bundle_id=next(iter(bundles)),now_ns=obs['observed_at_ns'])
                        if parsed!=interpretation or parsed.get('status')!='parsed_under_observed_locale' or parsed.get('application_formatter_proven')is not False:
                            raise ValueError('Locale interpretation differs from independently recorded proof')
                    with patch.object(verifier,'time',clock),patch.object(number_format_module,'time',clock):
                        checked=verifier.verify(binding,goal,obs,number_format=number_format)
                        preserves=[verifier.verify(p['binding'],p['goal'],obs) for p in scope.get('preserves',[])]
                    actual=(checked.get('evidence') or {}).get('actual')
                    exact=structural and checked.get('matched')is True and receipt.get('matched')is True and all(proof.get(k)==v for k,v in (checked.get('evidence') or {}).items() if k!='binding_review_required')
                    if kind=='calculation':exact=exact and (Fraction(parsed['numerator'],parsed['denominator']) if parsed else common.number(actual))==Fraction(oracle['numerator'],oracle['denominator'])
                    elif kind=='text':
                        controls=[c for c in obs['controls'] if c['id']==proof.get('control_id')]
                        exact=exact and len(controls)==1 and common._editor_exact(controls[0]) and actual==oracle['value']
                except (ValueError,KeyError,TypeError):exact=False
            witness=witnesses.get(scope_id) or {};issuance=(kind!='calculation' or witness.get('known_start')is True and witness.get('issued_evaluation')==re.sub(r'\s+','',oracle['expression']))
            preservation=all(p.get('matched')is True for p in preserves)
            reports.append({'scope_id':scope_id,'goal_id':goal['id'],'name':name,'app_verified':app['verified'],'desired_goal_matches':bool(desired_match),
                'latest_independent_predicate_matches':bool(exact),'arithmetic_issuance_matches':bool(issuance),'recorded_preservation_predicates':len(preserves),
                'recorded_preservation_matches':preservation,'revision':revision,'snapshot_id':proof.get('snapshot_id'),'numeric_interpretation':parsed,'locale_evidence_path':format_path,
                'conditional_on_application_following_observed_locale':parsed is not None,'application_formatter_proven':False,
                'pass':bool(desired_match and app['verified'] and exact and issuance and preservation and revision['valid'] and scope.get('status') in ('approved','reconciled_verified'))})
    expected=2 if kind=='states' else 1
    positive=sum(r['pass'] for r in reports)==expected and len(reports)==expected
    if kind=='states':positive=positive and {r['name'] for r in reports}=={x['name'] for x in oracle['desired']}
    return {'supported':True,'pass':bool(positive and summary.get('status')=='verified_reviewed_scope' and final.get('all_reviewed_goals_verified')is True
        and final.get('request_coverage',{}).get('user_reviewed_complete_declaration')is True),'goals':reports,
        'current_freshness_claim':False,'historical_clock_replay_only':True,'all_other_observed_settings_preservation_independently_proven':False if kind=='states' else None,
        'saved_file_preservation_proven':False if kind=='text' else None}


def classify_failure(task,summary,issues,outcomes,sequences,witnesses):
    if issues:return {'classification':'receipt_scope_or_evidence_integrity_failure','issues':sorted(set(issues))}
    if outcomes['pass']:return None
    if task.get('oracle',{}).get('kind')=='calculation':
        from locua.arithmetic_input import normalized
        expected=normalized(task['oracle']['expression'])
        divergent=[{'scope_id':sid,'issued_evaluation':w['issued_evaluation'],'requested_expression':expected}
            for sid,w in witnesses.items() if isinstance(w.get('issued_evaluation'),str)
            and normalized(w['issued_evaluation'])!=expected]
        if divergent:return {'classification':'issued_evaluation_differs_from_requested_expression','evaluations':divergent,
            'sequence_terminal_receipts':[s.get('terminal_step') for s in sequences if s.get('terminal_step')],
            'later_transition_stop_is_not_the_input_error':True,'model_input_availability_requires_separate_causal_audit':True}
    terminal=next((s for s in sequences if s['partial']),None)
    return {'classification':'partial_sequence_stopped' if terminal else 'task_outcome_incomplete_at_run_end',
        'sequence':terminal,'summary_status':summary.get('status'),'reason':summary.get('reason'),'model_nonprogress_proven':False}


def audit(run,suite_path,task_id,amendment=None,*,disk_before=None,disk_after=None):
    run=Path(run).resolve();suite,task=task_from_suite(suite_path,task_id,amendment)
    summary=read(run/'summary.json');evidence=read(run/'desktop/evidence.json')
    transport=[common.strict_json(x) for x in (run/'desktop/desktop/cua/transport.jsonl').read_text().splitlines() if x.strip()]
    snapshots,issues=load_snapshots(run,transport);events=evidence.get('events',[]);acts=[];sequences=[]
    catalogs,catalog_issues,catalog_sources=load_action_catalogs(run,snapshots);issues+=catalog_issues
    if summary.get('request')!=task['request'] or evidence.get('request')!=task['request']:issues.append('frozen_request_differs')
    locale_evidence={};locale_path=run/'desktop/desktop/desktop.jsonl'
    if locale_path.exists():
        for index,line in enumerate(locale_path.read_text().splitlines(),1):
            row=common.strict_json(line);value=row.get('evidence')
            if row.get('operation')=='number_format' and isinstance(value,dict) and value.get('evidence_sha256'):
                locale_evidence.setdefault(value['evidence_sha256'],[]).append((value,str(locale_path)+'#record='+str(index)))
    if summary.get('request')!=task['request'] or evidence.get('request')!=task['request']:issues.append('request_differs_from_frozen_case')
    writes=[r for r in transport if r.get('request',{}).get('name') not in READS|SETUP and r.get('request',{}).get('name')]
    no_private=not list((run/'desktop').glob('sequence-*.json'))
    for e in events:
        if e.get('tool')=='locua_act_sequence':
            step_acts,report=sequence_receipts(e,run,snapshots,zero_write_prevalidation=not writes and no_private,catalogs=catalogs)
            if report.get('source_evidence'):
                source=read(report['source_evidence']['path']);contract=reviewed_contract_check(e,source,evidence,snapshots)
                report['reviewed_contract']=contract;report['issues']+=contract['issues']
            report['model_proposal']=proposal_mapping(e,snapshots,evidence.get('scopes',{}),catalogs)
            acts+=step_acts;sequences.append(report);issues+=report['issues']
        elif e.get('tool')=='locua_act' and e.get('result',{}).get('action_started')is not False:acts.append(e)
    referenced={s['source_evidence']['path'] for s in sequences if s.get('source_evidence')}
    orphan_sources=[file_record(p) for p in (run/'desktop').glob('sequence-*-source.json') if str(p.resolve()) not in referenced]
    if orphan_sources:issues.append('sequence_private_evidence_without_completed_public_event')
    # Do not permit a later input to erase an earlier uncertain delivery.
    uncertain_targets=set()
    for act in acts:
        sid=act.get('input',{}).get('scope_id');scope=evidence.get('scopes',{}).get(sid,{})
        target=digest(scope.get('target'))
        if target in uncertain_targets and act.get('result',{}).get('action_started')is not False:issues.append('input_after_uncertain_delivery')
        if act.get('result',{}).get('action_started')is None or act.get('result',{}).get('status')=='uncertain':uncertain_targets.add(target)
    writes=[r for r in transport if r.get('request',{}).get('name') not in READS|SETUP and r.get('request',{}).get('name')]
    if len(writes)!=len(acts):issues.append('actual_driver_write_and_receipt_count_mismatch')
    inputs,witnesses=reconcile_writes(writes,acts,evidence.get('scopes',{}),snapshots)
    if any(not r['authorized'] for r in inputs):issues.append('unreconciled_or_outside_scope_driver_write')
    outcomes=final_outcomes(task,summary,evidence,snapshots,transport,witnesses,locale_evidence)
    records=[read(p) for p in sorted((run/'provider').glob('call-*-summary.json'))];metrics=common.count_metrics(records,summary)
    session_path=run/'session/session.json';session=read(session_path) if session_path.exists() else {};starts={};tool_times=[]
    for event in session.get('events',[]):
        d=event.get('data') or {};key=d.get('tool_call_id')
        if event.get('event')=='tool:pre':starts[key]=event
        elif event.get('event')=='tool:post' and key in starts:
            tool_times.append({'tool':d.get('tool_name'),'seconds':(event['at_ns']-starts[key]['at_ns'])/1e9})
    feedback=[{'event':e['sequence'],'tool':e['tool'],'feedback':(e.get('model_result') or e.get('result') or {}).get('exploration_feedback')}
        for e in events if ((e.get('model_result') or e.get('result') or {}).get('exploration_feedback') or {}).get('new_information')is False]
    first=classify_failure(task,summary,issues,outcomes,sequences,witnesses)
    complete=not issues and outcomes['pass'];limits=[]
    if task['family']=='settings':limits.append('Other-setting preservation and post-run restoration require independent full-initial-state audit; no full family pass from desired toggles alone.')
    editor_audit=None
    if task['family']=='editor':
        if disk_before is not None and disk_after is not None and task.get('oracle_source'):
            import contract_repair_eval as editor
            oracle_ref=task['oracle_source']
            if file_record(oracle_ref['path'])['sha256']!=oracle_ref['sha256']:raise ValueError('Frozen editor oracle changed')
            oracle=read(oracle_ref['path'])
            if oracle['expected_buffer']!=task['oracle']['value']:raise ValueError('Editor oracle disagreement')
            editor_audit=editor.audit_saved(run,request=task['request'],expected=task['oracle']['value'],document=task['oracle']['document'],
                disk_before=read(disk_before),disk_after=read(disk_after),oracle=oracle)
            complete=complete and editor_audit['functional_pass']
            if not editor_audit['functional_pass']:limits.append('Independent exact-editor/file-preservation checks did not pass.')
        else:limits.append('Disk preservation requires before/after fixture attestations; buffer proof does not prove no autosave.')
    return {'version':VERSION,'run':str(run),'case_id':task_id,'suite':file_record(suite_path),'amendment':file_record(amendment) if amendment else None,
        'task_exposure':task['exposure'],'sequence_receipts':sequences,'orphan_sequence_sources':orphan_sources,'action_catalog_sources':catalog_sources,'independent_editor_audit':editor_audit,'actual_per_step_inputs':inputs,'actual_task_writes':len(writes),'arithmetic_replay':witnesses,
        'outcome':outcomes,'supported_predicate_pass':complete,'whole_case_pass':complete and not limits,
        'within_120s':type(summary.get('wall_excluding_human_s'))in(int,float) and summary['wall_excluding_human_s']<=120,
        'first_consequential_failure':first,'issues':sorted(set(issues)),'limits':limits,'metrics':metrics,
        'timing':{'wall_excluding_human_s':summary.get('wall_excluding_human_s'),'full_workflow_wall_s':summary.get('full_workflow_wall_s'),
            'human_wait_s':summary.get('human_wait_s'),'tool_pre_post':tool_times,'budget_measurements':summary.get('budget_measurements'),
            'nested_phases_not_additive':True},'api_cost':summary.get('api_cost'),'tool_calls':dict(Counter(e['tool'] for e in events)),
        'recorded_repetition_feedback':feedback,'feedback_is_not_causal_nonprogress_proof':True,
        'baseline_comparison':{'evidence':suite['baseline'],'same_granularity':False,'no_model_loop_inferred_from_v10_refreshes':True},
        'configuration':{k:summary.get(k) for k in ('model','provider','decoding','rlcd_used','thinking','instructions','tool_profile','tool_interface','task_scoped_observations','prefix_cache_policy')},
        'cleanup':{k:summary.get(k) for k in ('provider_closed','session_cleanup','desktop_cleanup','desktop_control_lease')},
        'sources':[file_record(p) for p in (run/'summary.json',run/'desktop/evidence.json',run/'desktop/desktop/cua/transport.jsonl',Path(__file__),ROOT/'src/locua/arithmetic_input.py',ROOT/'src/locua/goal_verification.py',ROOT/'src/locua/desktop_tools.py',ROOT/'src/locua/action_sequence.py',ROOT/'src/locua/action_sequence_guard.py')],
        'auditor_model_calls':0,'auditor_desktop_calls':0,'unobserved_global_effects_proven_absent':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True)
    p.add_argument('--suite',type=Path,default=ROOT/'artifacts/action-sequence-v11-001/frozen-suite.json')
    p.add_argument('--case',required=True);p.add_argument('--amendment',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--disk-before',type=Path);p.add_argument('--disk-after',type=Path)
    args=p.parse_args();result=audit(args.run,args.suite,args.case,args.amendment,disk_before=args.disk_before,disk_after=args.disk_after);write(args.out,result)
    print(json.dumps({'artifact':str(args.out),'supported_predicate_pass':result['supported_predicate_pass'],'whole_case_pass':result['whole_case_pass'],'actual_task_writes':result['actual_task_writes'],'issues':result['issues']}))

if __name__=='__main__':main()
