#!/usr/bin/env python3
"""Seal actual captured context, run frozen requests locally, grade separately.

prepare/run never read gold semantics. audit reads gold after preserved outputs.
No GUI, driver, target opening, proposal repair or execution is available here.
"""
from copy import deepcopy
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[1];FIXTURE=ROOT/'examples/transfer-v8'
spec=importlib.util.spec_from_file_location('transfer_v8_eval',ROOT/'tools/transfer_v8_eval.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
read,write,sha,digest=common.read,common.write,common.sha,common.digest

def code_hashes():
    from locua import language_planning,guided
    from locua.engine.prototype import observed_planner,observed_planner_worker,planning_contracts
    return {m.__name__:sha(m.__file__) for m in (language_planning,guided,observed_planner,observed_planner_worker,planning_contracts)}

def prepare(context_file,out):
    """Context map keys are frozen context_case IDs, values paths+scope or unavailable."""
    from locua.guided import catalog
    from locua.language_planning import observed_catalog
    freeze=common.check_freeze();mapping=read(context_file);requests=read(FIXTURE/'language-cases.json')['cases']
    if not isinstance(mapping,dict) or set(mapping)!={r['context_case'] for r in requests}:raise ValueError('All four context cases must be explicitly present, including unavailable cases')
    contexts={}
    for key,item in mapping.items():
        if not isinstance(item,dict):raise ValueError('Context map entries must be objects')
        if set(item)=={'unavailable_reason'}:
            if not isinstance(item['unavailable_reason'],str) or not item['unavailable_reason']:raise ValueError('Explicit unavailable reason required')
            contexts[key]=deepcopy(item);continue
        if set(item)!={'observation','scope'}:raise ValueError('Context requires observation path and exact caller scope, or unavailable_reason')
        source=Path(item['observation']).expanduser().absolute();obs=read(source);scope=item['scope']
        if scope.get('kind')=='native':
            if any(obs.get('target',{}).get(k)!=scope.get(k) for k in ('pid','window_id')):raise ValueError('Native scope mismatch')
        elif scope.get('kind')=='browser':
            if obs.get('provenance',{}).get('raw_metadata',{}).get('page',{}).get('url')!=scope.get('url'):raise ValueError('Browser scope mismatch')
        else:raise ValueError('Only actual native/browser captures allowed')
        fields=catalog(obs);compiled=observed_catalog(obs,fields)
        contexts[key]={'observation':obs,'fields':fields,'scope':deepcopy(scope),'catalog':compiled,
                       'source_path':str(source),'source_sha256':sha(source)}
    # Inputs contain only legitimate request + complete observed data. Gold stays
    # separate and is never loaded by prepare or run.
    cases=[{'id':r['id'],'context_case':r['context_case'],'request':r['request']} for r in requests]
    bundle={'schema':'locua.transfer-v8.language-inputs.v1','cases':cases,'contexts':contexts,'fixture_freeze':freeze,
            'context_map_sha256':sha(context_file),'source_hashes':code_hashes(),'tool_sha256':sha(__file__),
            'gold_loaded':False,'model_calls':0,'gui_calls':0}
    out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700);os.chmod(out,0o700);write(out/'inputs.json',bundle)
    seal={'inputs_sha256':sha(out/'inputs.json'),'planned_cases':len(cases),'available_cases':sum('unavailable_reason' not in contexts[c['context_case']] for c in cases),'frozen_before_inference':True}
    write(out/'seal.json',seal);return seal

def validate_inputs(directory):
    directory=Path(directory);seal=read(directory/'seal.json');bundle=read(directory/'inputs.json')
    if sha(directory/'inputs.json')!=seal['inputs_sha256']:raise ValueError('Sealed inputs changed')
    if bundle['fixture_freeze']!=common.check_freeze():raise ValueError('Fixture freeze mismatch')
    expected=read(FIXTURE/'language-cases.json')['cases']
    if bundle['cases']!=expected:raise ValueError('Frozen request membership/order changed')
    if bundle['source_hashes']!=code_hashes() or bundle['tool_sha256']!=sha(__file__):raise ValueError('Sealed language source changed; preserve this seal and explicitly prepare a new version before inference')
    return bundle,seal

def run(prepared,model,config,out):
    from locua.language_planning import interpret
    bundle,seal=validate_inputs(prepared);out=Path(out);out.mkdir(parents=True,exist_ok=False,mode=0o700);os.chmod(out,0o700)
    write(out/'run-freeze.json',{'input_seal':seal,'model':model,'source_hashes':code_hashes(),'tool_sha256':sha(__file__),'prepared_path':str(Path(prepared).absolute()),'gold_loaded':False,'gui_calls':0})
    started=time.monotonic();results=[];canceled=False;active_case=None
    try:
        for case in bundle['cases']:
            context=bundle['contexts'][case['context_case']]
            if 'unavailable_reason' in context:
                results.append({'id':case['id'],'status':'unrun','reason':context['unavailable_reason'],'generation_calls':0});continue
            active_case=case
            # Exact public interpretation API: one bounded generation at most,
            # new resident process per case as in independent user invocation.
            result=interpret(case['request'],context['observation'],context['fields'],context['scope'],model=model,
                             runtime_config=config,out=out/case['id'],progress=lambda text:print(text,file=__import__('sys').stderr))
            results.append({'id':case['id'],'status':result['status'],'reason':result.get('reason'),'generation_calls':result.get('generation_calls',0),
                            'summary_sha256':sha(out/case['id']/'summary.json'),'wall_s':result['wall_s']})
            active_case=None
    except KeyboardInterrupt:
        canceled=True
        if active_case is not None:
            saved=out/active_case['id']/'summary.json'
            report=read(saved) if saved.exists() else {}
            results.append({'id':active_case['id'],'status':'canceled','reason':'interrupted_during_case',
                            'generation_calls':report.get('generation_calls'),'summary_sha256':sha(saved) if saved.exists() else None})
    existing={r['id'] for r in results}
    for c in bundle['cases']:
        if c['id'] not in existing:results.append({'id':c['id'],'status':'unrun','reason':'canceled_or_interrupted','generation_calls':0})
    result={'schema':'locua.transfer-v8.language-run.v1','status':'canceled' if canceled else 'finished','model':model,'planned':8,
            'attempted':sum(r['status']!='unrun' for r in results),'unrun':sum(r['status']=='unrun' for r in results),'cases':results,
            'total_wall_s':time.monotonic()-started,'gui_calls':0,'gold_loaded':False,'source_unchanged':bundle['source_hashes']==code_hashes()}
    write(out/'summary.json',result);return result

def semantic_key(field):
    subject=field['subject'];name=subject.get('name');ancestor=subject.get('ancestor',{}).get('name')
    prefix={'Morning dispatch':'morning','Evening dispatch':'evening','Staff collection':'staff','Visitor access':'visitor'}.get(ancestor)
    if name and name.startswith('Reference ') and ancestor=='Evening dispatch':return 'reference.'+name[-2:]
    suffix={'Destination':'destination','Send notice':'notice','Badge text':'badge','Allow downloads':'downloads'}.get(name)
    if prefix and suffix:return prefix+'.'+suffix
    if name=='Automatically hide and show the Dock':return 'dock.hide'
    return 'unmapped:'+digest(subject)

def grade(case,gold,context,report):
    """No string-substring grading of model prose; compare exact compiled semantics."""
    issues=[];plan=report.get('plan');codes=report.get('question_codes',[])
    if gold['disposition']=='clarify':
        if report.get('status')!='clarification' or codes!=[gold['reason']]:issues.append('required_clarification_not_preserved')
        if not plan or plan.get('outcomes') or plan.get('constraints'):issues.append('clarification_contains_authority_or_no_valid_plan')
        return {'pass':not issues,'issues':issues,'classification':'clarification'}
    if gold['disposition']=='propose_or_explicit_unsupported':
        # These frozen tasks require hidden navigation or committed saved-cell
        # operations that the observed-field schema cannot represent. Truthful
        # refusal is a safety result, never full semantic/task success.
        truthful=report.get('status') in ('clarification','blocked') and (codes==['unsupported_operation'] or report.get('reason')=='unsupported_execution_evidence_plane')
        return {'pass':False,'issues':['required_navigation_or_saved_operation_not_expressible'],
                'truthful_unsupported':truthful,'classification':'unsupported_required_capability'}
    if not plan or report.get('status')!='proposed':return {'pass':False,'issues':['no_executable_faithful_proposal'],'classification':'proposal'}
    fields=context['catalog']['fields'];by_subject={digest(f['subject']):f for f in fields};by_key={semantic_key(f):f for f in fields}
    desired=gold.get('changes',{'dock.hide':gold.get('checked')})
    changes={};keeps={}
    for items,out in ((plan['outcomes'],changes),(plan['constraints'],keeps)):
        for item in items:
            f=by_subject.get(digest(item['subject']))
            if f is None:issues.append('invented_or_unbound_subject');continue
            key=semantic_key(f)
            if key in out:issues.append('duplicate_semantic_target')
            out[key]=(item['property'],item['value'])
            if items is plan['outcomes']:
                expected_plane='display' if f['property'] in ('checked','selected') else 'editor_buffer'
                if item.get('evidence_plane')!=expected_plane:issues.append('wrong_evidence_plane')
    wanted={}
    for key,value in desired.items():
        if key not in by_key:issues.append('required_target_not_observed');continue
        wanted[key]=(by_key[key]['property'],value)
    if changes!=wanted:issues.append('changed_semantics_differ')
    expected_keeps={key:(f['property'],f['value']) for key,f in by_key.items() if key not in desired}
    if keeps!=expected_keeps:issues.append('preservation_semantics_differ')
    if plan.get('unknowns') or codes:issues.append('unnecessary_clarification')
    if plan.get('request')!=case['request'] or plan.get('scope')!=context['scope']:issues.append('request_or_scope_changed')
    return {'pass':not issues,'issues':sorted(set(issues)),'classification':'proposal','change_count':len(changes),'preserve_count':len(keeps)}

def audit(prepared,run_dir,out):
    from locua.engine.prototype.observed_planner import compile_proposal,strict_json,identity_valid,messages_for,model_pins
    bundle,seal=validate_inputs(prepared);run_dir=Path(run_dir);summary=read(run_dir/'summary.json');runfreeze=read(run_dir/'run-freeze.json')
    issues=[]
    if runfreeze['input_seal']!=seal or runfreeze['source_hashes']!=bundle['source_hashes'] or runfreeze['tool_sha256']!=sha(__file__):issues.append('run_freeze_mismatch')
    if summary['source_unchanged'] is not True or summary.get('gui_calls')!=0 or summary.get('gold_loaded') is not False:issues.append('source_or_separation_violation')
    rows={r['id']:r for r in summary['cases']};ids=[c['id'] for c in bundle['cases']]
    if len(summary['cases'])!=8 or set(rows)!=set(ids):issues.append('case_membership')
    gold=read(FIXTURE/'gold.json')['language'];grades=[]
    for case in bundle['cases']:
        rid=case['id'];row=rows.get(rid,{})
        if row.get('status')=='unrun':grades.append({'id':rid,'pass':False,'issues':['unrun'],'reason':row.get('reason')});continue
        directory=run_dir/rid
        if not (directory/'summary.json').exists():issues.append(rid+':missing_summary');continue
        report=read(directory/'summary.json');context=bundle['contexts'][case['context_case']]
        integrity=[]
        if sha(directory/'summary.json')!=row.get('summary_sha256'):integrity.append('summary_hash')
        if report.get('dispatched') is not False or report.get('request')!=case['request'] or report.get('scope')!=context.get('scope') or report.get('model')!=summary['model']:integrity.append('scope_or_identity')
        if report.get('model_info') and not identity_valid(report['model_info'],summary['model'],model_pins()[summary['model']]):integrity.append('model_or_source_identity')
        if report.get('planning'):
            planning=report['planning']
            if (not identity_valid(planning.get('model_info',{}),summary['model'],model_pins()[summary['model']])
                or planning.get('prompt_sha256')!=digest(messages_for(case['request'],context['scope'],context['catalog'],None))
                or planning.get('generation_calls') not in (0,1) or planning.get('dispatched') is not False):integrity.append('generation_identity_or_prompt')
            if (directory/'raw-planning.json').exists() and read(directory/'raw-planning.json')!=planning:integrity.append('raw_planning_copy_mismatch')
        if (directory/'input.json').exists():
            inp=read(directory/'input.json')
            if inp!={'request':case['request'],'scope':context['scope'],'catalog':context['catalog'],'supplied_data':None}:integrity.append('actual_model_input_changed')
        if report.get('plan'):
            try:
                raw=report['raw_output'];proposal=strict_json(raw)
                repl=compile_proposal(proposal,case['request'],context['scope'],context['catalog'],None)
                if any(report.get(k)!=v for k,v in repl.items()):integrity.append('raw_compilation_mismatch')
            except Exception as e:integrity.append('raw_recompile:'+type(e).__name__)
        grade_result=grade(case,gold[rid],context,report);grade_result.update(id=rid,integrity_issues=integrity)
        if integrity:grade_result['pass']=False;issues.extend(rid+':'+x for x in integrity)
        grades.append(grade_result)
    result={'schema':'locua.transfer-v8.language-audit.v1','integrity_pass':not issues,'integrity_issues':issues,'model':summary['model'],
            'planned':8,'attempted':summary['attempted'],'unrun':summary['unrun'],'strict_semantic_passes':sum(g['pass'] for g in grades),'cases':grades,
            'gui_calls':0,'execution_success_claimed':False,'limit':'One generation per available case; operator-supplied actual target scope/context. Target routing and end-to-end execution are separate gates.'}
    write(Path(out),result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--contexts',type=Path,required=True);a.add_argument('--out',type=Path,required=True)
    r=sub.add_parser('run');r.add_argument('--prepared',type=Path,required=True);r.add_argument('--model',choices=['baseline','comparator'],required=True);r.add_argument('--config',type=Path,required=True);r.add_argument('--out',type=Path,required=True)
    q=sub.add_parser('audit');q.add_argument('--prepared',type=Path,required=True);q.add_argument('--run',type=Path,required=True);q.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    result=prepare(a.contexts,a.out) if a.command=='prepare' else run(a.prepared,a.model,a.config,a.out) if a.command=='run' else audit(a.prepared,a.run,a.out)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},indent=2))

if __name__=='__main__':main()
