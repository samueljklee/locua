#!/usr/bin/env python3
"""Offline exact-editor oracles and retained-capture contract integration checks.

No model, desktop, network, fixture installation, or action dispatch. Historical
capture checks never manufacture current freshness or erase recorded failures.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[1]
VERSION='contract-repair-editor-eval-v1'
OUT=ROOT/'artifacts/contract-repair-v10-001'
PRIOR=ROOT/'artifacts/prompt-policy-v9-001'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def read(path):
    def pairs(items):
        obj={}
        for k,v in items:
            if k in obj:raise ValueError('Duplicate JSON key')
            obj[k]=v
        return obj
    return json.loads(Path(path).read_text(),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
def record(path):
    p=Path(path);raw=p.read_bytes();return {'path':str(p.resolve()),'sha256':sha(raw),'bytes':len(raw)}
def write(path,data):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')


def freeze(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700)
    literal='  Juniper "ready"\nnaïve café — β  '
    document='Locua-contract-v10-editor.txt';sentinel='Locua-contract-v10-preserve.txt'
    request=f"In TextEdit, replace the entire text in '{document}' with exactly '{literal}': two leading spaces, one line feed between the two lines, two trailing spaces, and no final line feed. Do not save or modify any other document."
    initial='Seed document for an exact-buffer contract test.\n'
    keep='Keep this separate document byte-for-byte.\n'
    public={'id':'untouched-editor-variation','request':request,'application':'TextEdit','document_name':document,
        'setup_assistance':'Operator prepares and pre-opens the disposable target. Setup is not model completion. Other fixture files are evaluator preservation sentinels.'}
    private={'id':public['id'],'request_sha256':sha(request.encode()),'application':'TextEdit','document_name':document,
        'expected_buffer':literal,'expected_buffer_utf8_sha256':sha(literal.encode()),'expected_codepoints':len(literal),'expected_utf8_bytes':len(literal.encode()),
        'literal_rule':'Exact Unicode codepoints/UTF-8; no strip, normalization, line-ending conversion, quote replacement, or added terminal LF.',
        'initial_files':{'target':{'basename':document,'utf8':initial,'sha256':sha(initial.encode())},'sentinel':{'basename':sentinel,'utf8':keep,'sha256':sha(keep.encode())}},
        'require_distinct_post_write_exact_buffer_captures':2,'require_successful_whole_cli':True,'forbid_explicit_save':True,
        'require_initial_and_final_disk_bytes_unchanged':True,'global_other_unsaved_buffers_proven':False,'practical_wall_excluding_review_s':120,
        'no_answer_or_expected_hash_in_model_observations':True,'qualification':'A new task plus retained gates; one pass alone is not transfer qualification.'}
    write(out/'request.json',public);write(out/'oracle-private.json',private)
    old_protocol=ROOT/'artifacts/model-comparison-v7-freeze-006/protocol-v65.json'
    prior=read(old_protocol)
    gate={'retained_protocol':record(old_protocol),'tasks':deepcopy(prior['tasks']),'repeats_per_task':prior['desktop_repeats_per_task'],
        'practical_wall_excluding_review_s':120,'new_editor_variation_is_additional':True,
        'prior_fresh_multiline_is_now_exposed_regression':record(PRIOR/'replay-freeze/heldout-private.json'),
        'no_new_paid_authorization':'Hosted deadline expired; both old ledgers/caps remain untouched. Root must obtain explicit renewal before paid work.',
        'tasks_and_oracles_never_added_to_runtime_prompt_except_the_selected_user_request':True}
    write(out/'retained-gate-private.json',gate)
    write(out/'freeze.json',{'version':VERSION,'created_at_ns':time.time_ns(),'files':{p.name:record(p) for p in out.iterdir() if p.is_file()},
        'source':record(__file__),'policy_tuning_to_new_outcomes':False,'model_calls':0,'desktop_calls':0,'file_setup_performed':False})
    return read(out/'freeze.json')


def load(frozen):
    root=Path(frozen);seal=read(root/'freeze.json')
    if seal['version']!=VERSION:raise ValueError('Unknown oracle version')
    for name,row in seal['files'].items():
        if sha((root/name).read_bytes())!=row['sha256']:raise ValueError('Frozen oracle/request changed')
    return read(root/'request.json'),read(root/'oracle-private.json')


def disk_attestation(frozen,target,sentinel):
    _,oracle=load(frozen);rows={}
    for key,source in [('target',target),('sentinel',sentinel)]:
        p=Path(source).expanduser().resolve(strict=True)
        if p.name!=oracle['initial_files'][key]['basename']:raise ValueError('File does not match declared fixture basename')
        before=p.stat();raw=p.read_bytes();after=p.stat()
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('File changed during read')
        rows[key]={'path':str(p),'sha256':sha(raw),'bytes':len(raw),'device':after.st_dev,'inode':after.st_ino,'mtime_ns':after.st_mtime_ns,'captured_at_ns':time.time_ns()}
    return {'version':VERSION,'read_only':True,'files':rows,'source_oracle':record(Path(frozen)/'oracle-private.json')}


def exposed_task(frozen):
    load(frozen)
    retained=read(Path(frozen)/'retained-gate-private.json')['prior_fresh_multiline_is_now_exposed_regression']
    if record(retained['path'])['sha256']!=retained['sha256']:raise ValueError('Exposed source oracle changed')
    rows=[r for r in read(retained['path'])['tasks'] if r['id']=='heldout-text']
    if len(rows)!=1:raise ValueError('Exposed source task missing or ambiguous')
    task=rows[0]
    return {'request':task['request'],'expected':task['criterion']['exact_buffer'],
        'document':task['request'].split("'")[1],'source_oracle':retained}


def exposed_disk_attestation(frozen,target):
    task=exposed_task(frozen);p=Path(target).expanduser().resolve(strict=True)
    if p.name!=task['document']:raise ValueError('File does not match exposed task document')
    before=p.stat();raw=p.read_bytes();after=p.stat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('File changed during read')
    return {'version':VERSION,'read_only':True,'mode':'exposed_regression_current_disk_baseline',
        'source_oracle':task['source_oracle'],'files':{'target':{'path':str(p),'sha256':sha(raw),'bytes':len(raw),
        'device':after.st_dev,'inode':after.st_ino,'mtime_ns':after.st_mtime_ns,'captured_at_ns':time.time_ns()}},
        'historical_disk_state_or_cause_inferred':False,'other_document_preservation_proven':False}


def compare_disks(before,after,oracle,*,first_write_start_ns,last_write_end_ns):
    checks={};details={}
    for key,definition in oracle['initial_files'].items():
        b=before.get('files',{}).get(key,{});a=after.get('files',{}).get(key,{})
        expected_bytes=len(definition['utf8'].encode()) if 'utf8' in definition else definition.get('bytes')
        valid=(b.get('path')==a.get('path') and isinstance(a.get('path'),str)
            and isinstance(definition.get('sha256'),str) and len(definition['sha256'])==64
            and all(c in '0123456789abcdef' for c in definition['sha256'])
            and Path(a['path']).name==definition['basename'] and b.get('sha256')==a.get('sha256')==definition['sha256']
            and type(expected_bytes)is int and expected_bytes>=0
            and type(a.get('bytes')) is int and a['bytes']==b.get('bytes')==expected_bytes
            and type(a.get('captured_at_ns'))is int and a['captured_at_ns']>last_write_end_ns
            and type(b.get('captured_at_ns'))is int and b['captured_at_ns']<first_write_start_ns)
        checks[key]=bool(valid);details[key]={'exact_initial_and_final_bytes':bool(valid),'metadata_equal':all(a.get(k)==b.get(k) for k in ('device','inode','mtime_ns')) if a and b else None}
    return {'all_fixture_files_unchanged':all(checks.values()),'files':details,'unobserved_other_buffers_or_global_desktop_preservation_proven':False}


def exact_raw(control):
    editor=control.get('editor') or {};proof=control.get('value_evidence') or {}
    a=editor.get('raw_value') or {};b=editor.get('raw_value_recheck') or {};value=control.get('value')
    return (type(value)is str and proof.get('precision')=='exact' and proof.get('exact_value_proven')is True
        and proof.get('plane')=='editor_buffer' and editor.get('plane')=='editor_buffer'
        and editor.get('coherence',{}).get('value_stable')is True
        and a.get('status')==b.get('status')=='ok' and type(a.get('value'))is str and type(b.get('value'))is str
        and a['value']==b['value']==value)


def retained_identity_check(before,control_id,goal,after):
    """Exercise real identity/predicate code without advancing/replacing clocks.

    Candidate identity and uniqueness are resolved before comparing expected
    text. A second independent binding validates the exact captured surface;
    matching identity policy is required. This grants no current write authority.
    """
    from locua.goal_verification import bind_for_review,check_review_predicate
    originals=(sha(encoded(before)),sha(encoded(after)))
    rows=[c for c in before['controls'] if c['id']==control_id]
    if len(rows)!=1:raise ValueError('Original reviewed control absent/ambiguous')
    original=bind_for_review(goal,rows[0],before);matches=[];errors=[]
    for control in after['controls']:
        if control.get('role') not in ('AXTextArea','AXTextField'):continue
        try:candidate=bind_for_review(goal,control,after)
        except (ValueError,KeyError,TypeError) as error:errors.append(str(error));continue
        if (candidate['target']==original['target'] and candidate['core_identity']==original['core_identity']
                and candidate['identity_policy']==original['identity_policy']):matches.append((control,candidate))
    result={'matching_identity_count':len(matches),'identity_policy':original['identity_policy'],
        'matched_at_capture':False,'exact_raw_buffer':False,'current_freshness_proven':False,'action_authority':False,'candidate_errors':errors}
    if len(matches)==1:
        c,b=matches[0];result.update(check_review_predicate(b,goal,after));result['exact_raw_buffer']=exact_raw(c)
        result.update(control_id=c['id'],actual_utf8_sha256=sha(c['value'].encode()) if type(c.get('value'))is str else None,
            exact_raw_matches_expected=exact_raw(c) and c['value']==goal['value'])
    if originals!=(sha(encoded(before)),sha(encoded(after))):raise AssertionError('Replay mutated saved observation')
    return result


def reconciliation_check(event,scopes,observations):
    """Reconcile the saved read-only receipt with the original private contract."""
    result=event['result'];proof=result.get('reconciliation') or {};scope_id=event['input'].get('scope_id')
    scope=scopes.get(scope_id) or {};pending=scope.get('uncertain_action') or {}
    keys=('target','goals','bindings','effects','preserves','covers_entire_request','unresolved_requirements')
    contract_sha=sha(encoded({k:scope[k] for k in keys})) if all(k in scope for k in keys) else None
    captured=observations.get(proof.get('snapshot_id'));obs=captured[0] if captured else {}
    receipt_ok=(result.get('status')=='verified' and result.get('scope_status')=='reconciled_verified'
        and result.get('scope_id')==scope_id and result.get('action_started')is False and result.get('no_retry')is True
        and proof.get('status')=='current_predicates_verified' and proof.get('input_authority_restored')is False
        and proof.get('original_uncertain_receipt_preserved')is True and proof.get('delivery_proven')is False
        and proof.get('other_side_effects_proven_absent')is False)
    identity_ok=(contract_sha is not None and proof.get('original_contract_sha256')==contract_sha==pending.get('reviewed_contract_sha256')
        and obs.get('target')==scope.get('target') and type(obs.get('observed_at_ns'))is int
        and obs['observed_at_ns']==proof.get('observed_at_ns') and type(pending.get('completed_at_ns'))is int
        and obs['observed_at_ns']>pending['completed_at_ns']
        and obs.get('snapshot_id') not in (pending.get('pre_snapshot_id'),pending.get('returned_snapshot_id')))
    return {'event':event['sequence'],'status':result.get('status'),'scope_status':result.get('scope_status'),
        'action_started':result.get('action_started'),'no_retry':result.get('no_retry'),'reconciliation':deepcopy(proof),
        'saved_contract_and_receipt_consistent':bool(receipt_ok and identity_ok),
        'delivery_proven':False,'unobserved_side_effects_proven_absent':False,'input_authority_restored':False}


def final_original_scope_check(final,scope_id,goal,target,captures,private_evidence,last_read_snapshot):
    """Earlier matching history cannot substitute for the last independent read."""
    scope=(private_evidence.get('scopes') or {}).get(scope_id) or {}
    proof=(final.get('verification') or {}).get('scopes',{}).get(scope_id) or {}
    rows=[g for g in proof.get('goals',[]) if g.get('goal_id')==goal['id']]
    latest=captures[-1] if captures else {};capture_check=latest.get('contract_check') or {}
    binding=(scope.get('bindings') or {}).get(goal['id']) or {}
    source_goals=[g for g in scope.get('goals',[]) if g.get('id')==goal['id']]
    evidence=rows[0].get('evidence',{}) if len(rows)==1 else {}
    ok=(len(rows)==1 and rows[0].get('matched')is True and source_goals==[goal]
        and scope_id in final.get('request_coverage',{}).get('scope_ids',[])
        and scope.get('target')==target and evidence.get('target')==target
        and isinstance(binding.get('id'),str) and evidence.get('binding_id')==binding['id']
        and evidence.get('plane')=='editor_buffer' and evidence.get('property')=='value' and evidence.get('actual')==goal['value']
        and latest.get('snapshot_id')==evidence.get('snapshot_id')==last_read_snapshot
        and type(latest.get('observed_at_ns'))is int and latest['observed_at_ns']==evidence.get('observed_at_ns')
        and capture_check.get('control_id')==evidence.get('control_id')
        and capture_check.get('matched_at_capture')is True and capture_check.get('exact_raw_matches_expected')is True)
    return {'matched_latest_original_scope_predicate':bool(ok),'scope_id':scope_id,'goal_id':goal['id'],
        'final_referenced_snapshot_id':evidence.get('snapshot_id'),'latest_captured_snapshot_id':latest.get('snapshot_id'),
        'last_recorded_target_read_snapshot_id':last_read_snapshot,'current_freshness_proven':False}


def audit_saved(run,*,request,expected,document,disk_before=None,disk_after=None,oracle=None):
    run=Path(run);summary=read(run/'summary.json');events=[read(p) for p in sorted((run/'desktop').glob('event-*.json'))]
    observations={read(p)['snapshot_id']:(read(p),p) for p in (run/'desktop').glob('observation-*.json')}
    issues=[];notes=[]
    if summary.get('request')!=request:issues.append('original_request_differs')
    reviews=[e for e in events if e['tool']=='locua_review' and e['result'].get('status')=='approved']
    eligible=[]
    for review in reviews:
        for proposed in review['input'].get('goals',[]):
            if proposed.get('kind')=='text' and proposed.get('evidence_plane')=='editor_buffer':eligible.append((review,proposed))
    if not eligible:raise ValueError('No approved text goal in saved run')
    review,proposed=eligible[0];goal={k:v for k,v in proposed.items() if k!='control_id'}
    if goal.get('value')!=expected:issues.append('review_literal_differs')
    before,pbefore=observations[review['input']['snapshot_id']]
    cid=proposed['control_id'];target=before['target']
    byid={c['id']:c for c in before['controls']};chain=[];cursor=byid[cid]
    while cursor.get('parent') is not None:
        cursor=byid[cursor['parent']];chain.append(cursor.get('name'))
        if len(chain)>len(byid):raise ValueError('Cyclic source ancestry')
    if document not in chain:issues.append('requested_document_not_in_reviewed_ancestry')
    transport=run/'desktop/desktop/cua/transport.jsonl';driver=[json.loads(line) for line in transport.read_text().splitlines() if line.strip()]
    # Older runs keep fresh predispatch/postdispatch captures only in transport.
    # Normalize every missing read, never select by expected value. Recorded raw
    # timestamps are the explicit basis; no saved capture is rewritten as fresh.
    from locua.engine.prototype.perception import normalize_observation
    transport_source=record(transport);reconstructed=[]
    for index,row in enumerate(driver,1):
        call=row.get('request') or {};raw=(row.get('response',{}).get('result') or {}).get('structuredContent') or {};sid=raw.get('snapshot_id')
        if call.get('name')!='get_window_state' or not sid or sid in observations:continue
        args=call.get('arguments') or {}
        try:
            obs=normalize_observation(row,kind='native_window_state',expected_target={k:args[k] for k in ('pid','window_id')},observed_at_ns=row['started_at_ns'])
            source={**transport_source,'jsonl_record':index,'derived_by_current_normalizer':True,'timestamp_basis':'Recorded tool read start, not current time or overwritten saved observation.'}
            observations[sid]=(obs,source);reconstructed.append({'snapshot_id':sid,'source':source})
        except (ValueError,KeyError,TypeError) as error:notes.append('A raw saved capture could not be normalized: '+type(error).__name__)
    from comparison_audit import READS,SETUP,resolve_native_input,application_proof
    app=application_proof({'app_name':'TextEdit'},{'target':target},events)
    if not app['verified']:issues.append('requested_application_identity_unproven')
    snapshots={sid:(o,str(p)) for sid,(o,p) in observations.items()}
    writes=[];setup=[];last_end=0
    for row in driver:
        call=row.get('request',{});name=call.get('name')
        if name in SETUP:setup.append({'operation':name,'started_at_ns':row.get('started_at_ns')})
        if not name or name in READS or name in SETUP:continue
        args=call.get('arguments') or {};last_end=max(last_end,row.get('started_at_ns',0)+int(row.get('response',{}).get('wall_ms',0)*1e6))
        resolved=resolve_native_input(row,snapshots);valid=False
        if name=='set_value' and resolved:
            control,obs,_=resolved
            try:match=retained_identity_check(before,cid,goal,obs)
            except (ValueError,KeyError,TypeError):match={}
            valid=(obs['target']==target and match.get('matching_identity_count')==1 and match.get('control_id')==control['id'] and args.get('value')==expected)
        if not valid:issues.append('unreconciled_write_or_unsupported_route_manual_audit')
        writes.append({'operation':name,'started_at_ns':row.get('started_at_ns'),'exact_reviewed_target_and_literal':valid,'request_sha256':sha(encoded(call))})
    if not writes:issues.append('no_task_input_recorded')
    captures=[];last_read_snapshot=None
    for row in sorted(driver,key=lambda r:r.get('started_at_ns',0)):
        call=row.get('request') or {};args=call.get('arguments') or {}
        if call.get('name')=='get_window_state' and {k:args.get(k) for k in ('pid','window_id')}==target:
            raw=(row.get('response',{}).get('result') or {}).get('structuredContent') or {}
            last_read_snapshot=raw.get('snapshot_id')
    for sid,(obs,path) in sorted(observations.items(),key=lambda x:x[1][0]['observed_at_ns']):
        if obs['target']!=target or obs['observed_at_ns']<=last_end:continue
        try:result=retained_identity_check(before,cid,goal,obs)
        except (ValueError,KeyError,TypeError) as error:result={'matched_at_capture':False,'exact_raw_matches_expected':False,'reason':str(error)}
        captures.append({'source':path if isinstance(path,dict) else record(path),'snapshot_id':sid,'observed_at_ns':obs['observed_at_ns'],'contract_check':result})
    matching=[c for c in captures if c['contract_check'].get('matched_at_capture') and c['contract_check'].get('exact_raw_matches_expected')]
    independent=(len(matching)>=2 and len({c['snapshot_id'] for c in matching})>=2 and len({c['observed_at_ns'] for c in matching})>=2)
    uncertain=[e['sequence'] for e in events if e['tool']=='locua_act' and e['result'].get('status')=='uncertain' and e['result'].get('action_started')is True]
    later_attempts=[e['sequence'] for e in events if e['tool']=='locua_act' and uncertain and e['sequence']>min(uncertain)]
    later_writes=[e['sequence'] for e in events if e['sequence'] in later_attempts and e['result'].get('action_started')is True]
    if later_writes:issues.append('task_input_after_uncertainty_requires_reconciliation_audit')
    first_start=min((w['started_at_ns'] for w in writes),default=0)
    disk=compare_disks(disk_before or {},disk_after or {},oracle,first_write_start_ns=first_start,last_write_end_ns=last_end) if oracle else {'all_fixture_files_unchanged':None,'unobserved_other_buffers_or_global_desktop_preservation_proven':False}
    elapsed=summary.get('wall_excluding_human_s');wall_ok=type(elapsed)in(int,float) and elapsed<=120
    final=summary.get('verification') or {}
    completed=(summary.get('status')=='verified_reviewed_scope' and final.get('all_reviewed_goals_verified')is True
        and final.get('request_coverage',{}).get('user_reviewed_complete_declaration')is True
        and bool(final.get('scope_statuses')) and all(v in ('approved','reconciled_verified') for v in final['scope_statuses'].values()))
    reconciliations=[];evidence_path=run/'desktop/evidence.json'
    private_evidence=read(evidence_path) if evidence_path.exists() else {}
    final_check=final_original_scope_check(final,review['result']['scope_id'],goal,target,captures,private_evidence,last_read_snapshot)
    if completed and not final_check['matched_latest_original_scope_predicate']:issues.append('final_original_scope_predicate_unproven_or_later_drift')
    for e in events:
        if e['tool']=='locua_verify' and e['input'].get('reconcile')is True:
            check=reconciliation_check(e,private_evidence.get('scopes',{}),observations);reconciliations.append(check)
            if e['result'].get('status')=='verified' and not check['saved_contract_and_receipt_consistent']:
                issues.append('reconciliation_authority_or_evidence_contract_missing')
    if not completed:notes.append('Historical or actual CLI failure retained; posthoc predicate success does not erase blocked/uncertain scopes.')
    if not oracle:notes.append('Historical preservation attestations are not promoted to prospective before/after file proof.')
    return {'version':VERSION,'source_summary':record(run/'summary.json'),'source_transport':record(transport),'review_observation':record(pbefore),
        'source_evaluator':record(__file__),'source_goal_verification':record(ROOT/'src/locua/goal_verification.py'),
        'recorded_cli_status':summary.get('status'),'request_faithful':summary.get('request')==request and goal.get('value')==expected,
        'requested_application_proof':app,'recorded_setup_effects':setup,
        'reviewed_scope_id':review['result']['scope_id'],'writes':writes,'recorded_task_input_count':len(writes),'uncertain_event_sequences':uncertain,
        'subsequent_task_input_sequences':later_writes,'subsequent_act_attempt_sequences':later_attempts,'reconciliation_receipts':reconciliations,'reconstructed_capture_sources':reconstructed,'capture_checks':captures,'two_distinct_later_exact_buffer_captures':independent,
        'file_preservation':disk,'recorded_cli_accepted_reviewed_scope':completed,'final_original_scope_check':final_check,'independent_language_coverage_proven':False,'wall_excluding_human_s':elapsed,'within_120s':wall_ok,
        'issues':sorted(set(issues)),'functional_pass':not issues and independent and completed and disk['all_fixture_files_unchanged']is True,
        'practical_pass':not issues and independent and completed and disk['all_fixture_files_unchanged']is True and wall_ok,
        'unintended_changes_claim':'Only recorded task writes and attested fixture file bytes; unobserved/global desktop and unrelated unsaved buffers remain unknown.',
        'notes':notes,'model_calls_made_by_auditor':0,'desktop_calls_made_by_auditor':0}


def regressions(out):
    rows=[]
    for name in ('live-text-regression-baseline-1','live-text-regression-concise-v1-1','live-heldout-text-baseline-1','live-heldout-text-concise-v1-1'):
        run=PRIOR/name;summary=read(run/'summary.json');fresh='heldout' in name
        rows.append({'run':name,**audit_saved(run,request=summary['request'],expected='  Birch\ncomplete  ' if fresh else 'Status: reviewed locally.',document='Locua-policy-holdout-text.txt' if fresh else 'Locua-v10-transfer-draft.txt')})
    result={'version':VERSION,'mode':'historical_saved_capture_contract_integration_only','rows':rows,'new_live_completion_credit':False,'model_calls':0,'desktop_calls':0}
    write(out,result);return result


def check_exposed(frozen,run,before,after):
    task=exposed_task(frozen)
    for attestation in (before,after):
        if (attestation.get('mode')!='exposed_regression_current_disk_baseline'
                or attestation.get('source_oracle',{}).get('sha256')!=task['source_oracle']['sha256']):
            raise ValueError('Exposed current disk baseline/source missing')
    initial=before.get('files',{}).get('target',{})
    oracle={'initial_files':{'target':{'basename':task['document'],'sha256':initial.get('sha256'),'bytes':initial.get('bytes')}}}
    report=audit_saved(run,request=task['request'],expected=task['expected'],document=task['document'],
        disk_before=before,disk_after=after,oracle=oracle)
    report.update(mode='exposed_regression_current_disk_baseline',source_oracle=task['source_oracle'],fresh_acceptance_credit=False,
        historical_disk_state_or_cause_inferred=False,other_document_preservation_proven=False)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('freeze');s.add_argument('--out',type=Path,default=OUT/'editor-variation-private')
    s=sub.add_parser('files');s.add_argument('--freeze',type=Path,required=True);s.add_argument('--target',type=Path,required=True);s.add_argument('--sentinel',type=Path,required=True);s.add_argument('--out',type=Path,required=True)
    s=sub.add_parser('files-exposed');s.add_argument('--freeze',type=Path,required=True);s.add_argument('--target',type=Path,required=True);s.add_argument('--out',type=Path,required=True)
    s=sub.add_parser('check');s.add_argument('--freeze',type=Path,required=True);s.add_argument('--run',type=Path,required=True);s.add_argument('--disk-before',type=Path,required=True);s.add_argument('--disk-after',type=Path,required=True);s.add_argument('--out',type=Path,required=True)
    s=sub.add_parser('check-exposed');s.add_argument('--freeze',type=Path,required=True);s.add_argument('--run',type=Path,required=True);s.add_argument('--disk-before',type=Path,required=True);s.add_argument('--disk-after',type=Path,required=True);s.add_argument('--out',type=Path,required=True)
    s=sub.add_parser('regressions');s.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.command=='freeze':r=freeze(a.out)
    elif a.command=='files':r=disk_attestation(a.freeze,a.target,a.sentinel);write(a.out,r)
    elif a.command=='files-exposed':r=exposed_disk_attestation(a.freeze,a.target);write(a.out,r)
    elif a.command=='check-exposed':r=check_exposed(a.freeze,a.run,read(a.disk_before),read(a.disk_after));write(a.out,r)
    elif a.command=='regressions':r=regressions(a.out)
    else:
        public,oracle=load(a.freeze);r=audit_saved(a.run,request=public['request'],expected=oracle['expected_buffer'],document=oracle['document_name'],disk_before=read(a.disk_before),disk_after=read(a.disk_after),oracle=oracle);write(a.out,r)
    print(json.dumps({'status':'saved','output':str(a.out),'model_calls':0,'desktop_calls':0}))

if __name__=='__main__':main()
