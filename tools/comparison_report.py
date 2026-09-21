#!/usr/bin/env python3
"""Read-only aggregate of retained comparison runs. No model, SDK or desktop calls.

Writes a NEW timestamped snapshot; earlier reports remain immutable. Identity,
cost and replay evidence come from run artifacts, never current remote lookups.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('locua_comparison_audit', ROOT/'tools/comparison_audit.py')
audit_module = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(audit_module)

class Reader:
    def __init__(self): self.sources = {}
    def read(self, path):
        path = Path(path); raw = path.read_bytes()
        self.sources[str(path.resolve())] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        return audit_module.strict_json(raw.decode())

def provider_key(summary):
    p = summary.get('provider', 'local')
    if isinstance(p, dict):
        return p['provider'] if p['provider'] != 'local' else 'local-thinking' if p.get('thinking') else 'local-nonthinking'
    return p if p != 'local' else 'local-thinking' if summary.get('thinking') else 'local-nonthinking'

def replay_harness(summary, protocols, uniform):
    digest=summary.get('manifest_sha256')
    matches=[v for v,p in protocols.items() if digest and p['protocol'].get('replay_manifest_sha256')==digest]
    if len(matches)==1:return matches[0]
    if not matches and digest and digest==uniform.get('frozen_manifest_sha256'):return 'tools-v6.2'
    return 'unknown_manifest'

def replay_counts(summary):
    return {'scheduled_decisions':summary.get('planned_calls',summary.get('calls')),
        'attempted_provider_calls':summary.get('calls'),
        'unrun_decisions':summary.get('unrun_calls',sum(x.get('status')=='unrun' for x in summary.get('results',[]))),
        'terminal_failure':summary.get('terminal_failure'),'runner_version':summary.get('runner_version')}

def identity(reader, run):
    path = run/'provider/provider-ready.json'
    if path.exists():
        d = reader.read(path)
        return {'source': str(path), 'provider': d.get('provider'), 'model_id': d.get('model'),
            'provider_revision': d.get('provider_revision'), 'dependencies': d.get('dependencies'),
            'config': d.get('config'), 'model_id_is_weight_snapshot': False,
            'max_input_tokens': d.get('max_input_tokens'), 'max_output_tokens': d.get('max_output_tokens'),
            'timeout_s': d.get('timeout_s'), 'package_source_sha256': d.get('package_source_sha256'),
            'official_module': d.get('official_module')}
    path = run/'provider/worker-ready.json'
    if not path.exists(): return {'status': 'readiness_identity_missing'}
    d = reader.read(path)
    return {'source': str(path), 'provider': 'local', 'model_pin': d.get('model_pin'),
        'actual_enable_thinking': d.get('enable_thinking'), 'template_kwargs': d.get('template_kwargs'),
        'decoder': d.get('decoder'), 'sampling': d.get('sampling'), 'dependencies': d.get('installed_dependencies'),
        'prompt_cache_policy': d.get('prompt_cache_policy'), 'memory_config': d.get('memory_config'),
        'model_load_ms': d.get('load_ms'), 'file_integrity_check_ms': d.get('file_integrity_check_ms'),
        'startup_memory': d.get('memory'),
        'verified_file_manifest_sha256': hashlib.sha256(audit_module.canonical(d.get('verified_loaded_files')).encode()).hexdigest(),
        **{k:d.get(k) for k in ('worker_sha256','provider_sha256','runtime_sha256','native_chat_template_sha256')}}

def record_metrics(reader, run, summary):
    records = [reader.read(p) for p in sorted((run/'provider').glob('call-*-summary.json'))]
    s = dict(summary)
    if not isinstance(s.get('provider'), str): s['provider'] = provider_key(s).split('-')[0]
    metrics = audit_module.count_metrics(records, s)
    poisoned = [r.get('call') for r in records if (r.get('error') or {}).get('type') == 'ProtocolError'
        and (r.get('error') or {}).get('message') == 'Decision service is closed/poisoned; create a new service']
    unknown = [c for c in metrics['unknown_calls'] if c not in poisoned]
    compact = {k:v for k,v in metrics.items() if k not in ('calls','identities','summary_reported')}
    compact.update(service_requests=len(records), poisoned_channel_refusals_before_worker_send=poisoned,
        unresolved_generation_calls=unknown, usage_accounting_source='provider records; full raw usage retained in those artifacts')
    if poisoned:
        compact.update(unknown_calls=unknown, closed_channel_is_not_model_decision=True,
            interrupted_generation_classification='First EOF is consistent with the bounded worker watchdog; exit code is not retained, so watchdog versus other runtime exit is not proved.')
    return compact

def costs(summary):
    c = summary.get('api_cost') or {}
    return {k:c.get(k) for k in ('per_run_known_charge_upper_usd','per_run_unknown_reserved_usd',
        'per_run_unknown_reservation_count','official_provider_reported_cost_estimate_usd','official_invoice_cost')}

def compact_audit(d):
    return {k:d.get(k) for k in ('status','functional_pass','comparison_eligibility',
        'conditional_on_application_following_observed_locale','integrity_issues','manual_audit_required',
        'first_consequential_failure','execution','latency','reviews','cleanup','outcomes','api_cost',
        'request_coverage','repeated_inspections','context_compactions')}

def interval_seconds(intervals):
    """Union wall intervals so simultaneous read-only tools are not double counted."""
    total=0; stop=None
    for start,end in sorted(intervals):
        total+=max(0,end-max(start,stop if stop is not None else start));stop=max(end,stop or end)
    return total/1e9

def timing_breakdown(reader, run, summary, model_identity, metrics, audit):
    path=run/'session/session.json';intervals=[];reviews=[];pending={};issues=[]
    if path.exists():
        for event in reader.read(path).get('events',[]):
            kind=event.get('event');data=event.get('data') or {};key=data.get('tool_call_id');at=event.get('at_ns')
            if kind not in ('tool:pre','tool:post'):continue
            if not key or type(at) is not int:issues.append('Missing tool ID or exact timestamp');continue
            if kind=='tool:pre':
                if key in pending:issues.append('Duplicate tool start: '+key)
                pending[key]=(at,data.get('tool_name'));continue
            before=pending.pop(key,None)
            if before is None or at<before[0]:issues.append('Unpaired or invalid tool end: '+key);continue
            (reviews if before[1]=='locua_review' else intervals).append((before[0],at))
    else:issues.append('Session event trace absent')
    if pending:issues.append('Unfinished tool calls: '+','.join(sorted(pending)))
    budget=summary.get('budget_measurements') or {};load=model_identity.get('model_load_ms')
    return {'workflow_wall_s':audit['latency'].get('workflow_wall_s'),
        'excluding_human_review_s':audit['latency'].get('excluding_review_s'),
        'human_review_s':audit['reviews'].get('human_wait_s'),
        'model_load_s':load/1000 if isinstance(load,(int,float)) else None,
        'file_integrity_check_s':model_identity.get('file_integrity_check_ms')/1000 if isinstance(model_identity.get('file_integrity_check_ms'),(int,float)) else None,
        'generation_known_wall_s':metrics.get('known_generation_s'),
        'generation_complete':metrics.get('usage_complete'),
        'token_meter_service_s':budget.get('service_wall_s'),
        'token_meter_calls':budget.get('requests'),
        'token_meter_includes_initial_model_load':budget.get('includes_initial_model_load'),
        'tool_nonreview_observed_union_s':interval_seconds(intervals) if path.exists() else None,
        'review_tool_observed_union_s':interval_seconds(reviews) if path.exists() else None,
        'tool_timing_complete':not issues,'tool_timing_issues':issues,
        'phase_caveat':'Known intervals only, not an additive decomposition: model load may be inside tokenizer preflight; review tool includes human wait; generation is SDK/worker wall, not isolated compute. Missing phases remain null.'}

def independent_notes(reader, base, run_name):
    names={'v63-openai-textedit-1':['audit-v63-001/textedit-1-diagnosis.json'],
        'v63-local-calculator-1':['audit-v63-001/local-calculator-1-diagnosis.json'],
        'v64-openai-fresh-1':['audit-v64-independent-001/fresh-1-diagnosis.json'],
        'v65-openai-fresh-1':['audit-v65-independent-001/fresh-1-diagnosis.json',
                            'audit-v65-independent-001/action-reference-note.json'],
        'v65-openai-calculator-1b':['audit-v65-independent-001/calculator-1b-independent-reconciliation.json'],
        'v65-openai-calculator-2':['audit-v65-independent-001/calculator-2-diagnosis.json'],
        'v65-local-calculator-1':['audit-v65-independent-001/local-calculator-1-diagnosis.json']}
    return [{'path':str(base/name),'record':reader.read(base/name)}
            for name in names.get(run_name,[]) if (base/name).exists()]

def operator_abort(reader, run):
    path=run/'operator-abort.json'
    if not path.exists():return None
    record=reader.read(path);events=list((run/'desktop').glob('event-*.json'))
    requests=list((run/'provider').glob('call-*-input.json'))
    dispatches=list((run/'provider').glob('dispatch-*-input.json'))
    return {'run':str(run),'status':'operator_aborted','source':str(path),'record':record,
        'qualification_credit':False,'model_failure_inferred':False,
        'public_tool_event_files':len(events),'provider_request_files':len(requests),
        'sdk_dispatch_input_files':len(dispatches),'generation_count':None if dispatches else 0,
        'generation_count_note':'A retained dispatch request without returned usage does not prove completed generation; unresolved reservation remains charged separately.',
        'integrity_issues':['Operator abort has public desktop events; inspect retained effects manually'] if events else [],
        'identity':identity(reader,run),'cost':{}}

def qualifications(protocols, rows):
    result=[]
    for version, info in protocols.items():
        p=info['protocol']; repeats=p.get('desktop_repeats_per_task',2)
        for provider in ('local-nonthinking','local-thinking','openai','anthropic'):
            relevant=[r for r in rows if r['harness']==version and r['provider']==provider]
            slots=[]
            for task in p['tasks']:
                for ordinal in range(1,repeats+1):
                    found=[r for r in relevant if r['task_id']==task['id'] and r['trial_ordinal']==ordinal and not r['development_suffix']]
                    slots.append({'task_id':task['id'],'repeat':ordinal,'attempts':[r['run'] for r in found],
                        'status':'unrun' if not found else 'verified' if len(found)==1 and found[0]['audit']['functional_pass'] and found[0]['audit']['comparison_eligibility']['eligible'] else 'not_verified',
                        'conditional_locale_proof':any(r['audit']['conditional_on_application_following_observed_locale'] for r in found)})
            result.append({'harness':version,'provider':provider,'planned_trials':len(slots),
                'completed_attempts_including_development':len(relevant),'completed_qualification_slots':sum(s['status']!='unrun' for s in slots),
                'verified_slots':sum(s['status']=='verified' for s in slots),'unrun_slots':sum(s['status']=='unrun' for s in slots),
                'six_outcome_gate_satisfied':len(slots)==6 and all(s['status']=='verified' for s in slots),
                'source_equivalence_across_runs_independently_proven':False,'slots':slots})
    return result

def build(base, output):
    reader=Reader(); started=time.time_ns(); output.mkdir(parents=True,exist_ok=False,mode=0o700)
    original=base/'protocol.json'
    protocols={'tools-v6.2':{'path':str(original),'protocol':reader.read(original)}}
    for p in sorted(base.parent.glob('model-comparison-v7-freeze-*/protocol-v*.json')):
        d=reader.read(p);version=d.get('tool_interface')
        if not version or version in protocols:raise ValueError('Missing or duplicate explicit protocol version: '+str(p))
        protocols[version]={'path':str(p),'protocol':d}
    uniform_path=base/'replay-uniform-v2.json'
    uniform=reader.read(uniform_path) if uniform_path.exists() else {}
    amended={Path(p['run']).name:p for p in uniform.get('providers',[])}
    connections=[]; replays=[]; desktops=[]; setups=[]; pending=[];aborts=[];components=[]
    for run in sorted(p for p in base.iterdir() if p.is_dir()):
        path=run/'summary.json'
        if not path.exists():
            aborted=operator_abort(reader,run)
            if aborted is not None:aborts.append(aborted);continue
            if re.match(r'^v6\d+-(local|openai|anthropic)-',run.name): pending.append(str(run))
            continue
        s=reader.read(path)
        if s.get('kind')=='operator_navigation_component_diagnostic':
            components.append({'run':str(run),'source':str(path),'record':s,
                'qualification_credit':False,'model_task_execution':False})
            continue
        if run.name.startswith(('setup-','fixture-')):
            setups.append({'run':str(run), **{k:s.get(k) for k in ('case','status','error','wall_s','wall_seconds','model_calls','model_decisions','desktop_calls','setup_assistance')}})
            continue
        if not run.name.startswith(('connection-','replay-')) and not (run/'desktop/evidence.json').exists(): continue
        row={'run':str(run),'summary':str(path),'provider':provider_key(s),'identity':identity(reader,run),
            'metrics':record_metrics(reader,run,s),'cost':costs(s)}
        if run.name.startswith('connection-'):
            response_files=sorted((run/'provider').glob('call-*-response.json'))
            finish_reasons=[reader.read(p).get('finish_reason') for p in response_files]
            row.update(status=s.get('status'),checks=s.get('checks'),returned_responses=s.get('returned_responses'),
                tool_calls=s.get('tool_calls'),wall_s=s.get('wall_s'),desktop_calls=s.get('desktop_calls'),
                finish_reasons=finish_reasons,service_refusal_observed='refusal' in finish_reasons,
                qualification_credit=False)
            connections.append(row);continue
        if run.name.startswith('replay-'):
            original=s.get('results',[]); revised=amended.get(run.name)
            use_revision=revised is not None and s.get('manifest_sha256')==uniform.get('frozen_manifest_sha256')
            scores=[x.get('evaluation_v2') or {} for x in revised['rows']] if use_revision else [x.get('evaluation') or {} for x in original]
            version=replay_harness(s,protocols,uniform)
            row.update(harness=version,manifest_sha256=s.get('manifest_sha256'),**replay_counts(s),
                returned_decisions=sum(x.get('status')=='returned' for x in original),
                admissible_next_actions=sum(x.get('semantic_progress') is True for x in scores),
                classifications=dict(Counter(c.get('classification') for x in scores for c in x.get('calls',[]))),
                evaluator_amendment_applied=use_revision,evaluator_amendment_path=str(uniform_path) if use_revision else None,
                wall_s=sum(x.get('wall_seconds',0) for x in original),desktop_calls=0,qualification_credit=False)
            replays.append(row);continue
        version=s.get('tool_interface'); info=protocols.get(version)
        matches=[t['id'] for t in (info or {}).get('protocol',{}).get('tasks',[]) if t['request']==s.get('request')]
        if len(matches)!=1:
            row.update(harness=version,status='protocol_task_unresolved');desktops.append(row);continue
        task=matches[0]; result=audit_module.audit(run,info['path'],task)
        audit_path=output/(run.name+'-audit.json');audit_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        reader.sources.update(result['evidence'])
        ordinal=re.search(r'-(\d+)([a-z]*)$',run.name)
        row.update(harness=version,task_id=task,trial_ordinal=int(ordinal[1]) if ordinal else None,
            development_suffix=ordinal[2] if ordinal else 'unknown',audit_path=str(audit_path),audit=compact_audit(result),
            assisted_by_review=bool(s.get('human_interactions')),review_interactions=[{'purpose':h.get('purpose'),'approved':h.get('answer')=='run',
                'answer_supplied_action_hint':False if h.get('purpose')=='tool_scope_review' and h.get('answer') in ('run','') else None}
                for h in s.get('human_interactions') or []])
        row['timing_breakdown']=timing_breakdown(reader,run,s,row['identity'],row['metrics'],result)
        row['interaction_counts']={'tool_calls':result['tool_calls'],
            'repeated_inspection_feedback_events':len(result['repeated_inspections']),
            'context_compactions':result['context_compactions'],
            'human_interactions':result['reviews']['human_interactions']}
        notes=independent_notes(reader,base,run.name)
        if notes:
            row['independent_review_diagnosis']=notes[0]
            row['independent_review_notes']=notes
        desktops.append(row)
    ledger_path=base/'api-budget.json';ledger=reader.read(ledger_path) if ledger_path.exists() else {}
    reservations=ledger.get('reservations',[])
    cost_by_run=defaultdict(lambda:{'charged_upper_micro_usd':0,'unknown_reserved_micro_usd':0,'dispatches':0})
    for r in reservations:
        run_id=r.get('metadata',{}).get('run_id','unknown')
        key=str((ROOT/Path(run_id)).resolve()) if run_id!='unknown' else run_id
        entry=cost_by_run[key];entry['dispatches']+=1
        if r.get('state')=='completed_conservative_charge':entry['charged_upper_micro_usd']+=r.get('charged_micro_usd',0)
        else:entry['unknown_reserved_micro_usd']+=r.get('reserved_micro_usd',0)
    setup_ledger=base/'fixture-setup-ledger.json'
    setups_declared=reader.read(setup_ledger) if setup_ledger.exists() else {}
    packages=[]
    for p in sorted(base.glob('package-identity*.json')):
        d=reader.read(p);packages.append({'path':str(p),'record':d})
    presentation_path=base/'cli-presentation-001/audit.json'
    presentation=({'path':str(presentation_path),'record':reader.read(presentation_path)}
        if presentation_path.exists() else None)
    historical=[]
    for p in sorted((base/'audit-validation-004').glob('*.json')):
        if p.name not in ('textedit-001.json','calculator-001.json','calculator-002.json'): continue
        d=reader.read(p)
        historical.append({'audit_path':str(p),'run':d.get('run'),'qualification_credit':False,
            'configuration':d.get('configuration'),'functional_pass':d.get('functional_pass'),
            'latency':d.get('latency'),'first_consequential_failure':d.get('first_consequential_failure'),
            'model_calls':d.get('metrics',{}).get('model_calls'),
            'task_input_requests':d.get('execution',{}).get('task_input_requests')})
    for row in connections+replays+desktops+aborts:
        ledger_cost=cost_by_run.get(str(Path(row['run']).resolve()/'provider'))
        if ledger_cost:
            row['cost']['ledger_known_conservative_upper_usd']=ledger_cost['charged_upper_micro_usd']/1e6
            row['cost']['ledger_unknown_reserved_usd']=ledger_cost['unknown_reserved_micro_usd']/1e6
    valid_desktops=[r for r in desktops if 'audit' in r]
    return {'schema_version':'locua-comparison-preliminary-v1','generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'snapshot_started_at_ns':started,'snapshot_finished_at_ns':time.time_ns(),'read_only':True,'provider_desktop_calls':0,
        'purpose':'Progress toward a usable installed CLI; connections and frozen decisions do not substitute for task execution.',
        'protocols':protocols,'connections':connections,'retained_state_replays':replays,'completed_cli_runs':desktops,
        'operator_aborted_cli_runs':aborts,
        'operator_component_diagnostics':components,
        'historical_context_not_qualification':historical,
        'pending_cli_directories':pending,'qualification':qualifications(protocols,valid_desktops),
        'selection':{'winner':None,'reason':'No automatic winner from preliminary, differently versioned, assisted trials; require six verified task trials within one comparable configuration plus independent source equivalence and practical latency/cost assessment.'},
        'setup':{'observed_fixture_runs':setups,'declared_ledger':setups_declared,'credited_as_model_task_execution':False,
            'note':'Disposable fixture resets and operator reopening of the existing TextEdit document are setup. Faithful typed-run approval is review assistance; declined Save scope is a failed task attempt, not a successful edit.'},
        'api_budget':{'source':str(ledger_path),'cap_usd':ledger.get('cap_micro_usd',0)/1e6,
            'known_conservative_upper_usd':sum(x['charged_upper_micro_usd'] for x in cost_by_run.values())/1e6,
            'unknown_reserved_usd':sum(x['unknown_reserved_micro_usd'] for x in cost_by_run.values())/1e6,
            'per_run':dict(cost_by_run),'official_invoice_cost':None,
            'note':'One global ledger, not a sum of cumulative run-summary charges. Upper estimates/reservations are not an invoice; hosted inference is explicitly nonlocal.'},
        'package_identity_records':packages,'source_artifacts':reader.sources,
        'presentation_only_equivalence_evidence':presentation,
        'audit_tool_sha256':hashlib.sha256((ROOT/'tools/comparison_audit.py').read_bytes()).hexdigest(),
        'limitations':['Completed summaries only; in-progress runs are excluded from task results.',
            'Normalized Usage.input_tokens is not comparable across providers; gross adds OpenAI cache-write, Anthropic cache-read plus cache-write.',
            'Hosted generation time is SDK dispatch wall time including network; local phase counters are not isolated GPU benchmarks.',
            'Local allocator/process peak memory are distinct, not system RAM or a hard memory cap. Hosted model memory is unavailable.',
            'Source weights/decoder differ between local and hosted candidates. This is an approach comparison, not a controlled model-size or RLCD benchmark.',
            'Original RLCD baseline remains unchanged and is not used in these ordinary native tool-calling trials.',
            'Locale-backed Calculator outcomes are conditional on app use of independently observed locale; private app formatter is not instrumented.']}

def md(report):
    lines=['# Preliminary installed-CLI comparison','',f"Snapshot: {report['generated_at_utc']}. No model selected as winner.",'',
        'The deliverable is a working CLI. A causal tool connection and an admissible retained-state decision are separate gates; neither completes a desktop task.','',
        '| Connection | Result | Model responses / tools | Wall s |','|---|---|---:|---:|']
    for r in report['connections']:lines.append(f"| {r['provider']} | {r['status']} | {r['returned_responses']} / {r['tool_calls']} | {r['wall_s']:.2f} |")
    lines += ['','| Candidate | Actual model / reasoning | Runtime or SDK |','|---|---|---|']
    for r in report['connections']:
        i=r['identity'];d=i.get('dependencies') or {};c=i.get('config') or {};pin=i.get('model_pin') or {}
        model=pin.get('model_id') or i.get('model_id')
        mode=('thinking' if i.get('actual_enable_thinking') else 'nonthinking greedy') if i.get('provider')=='local' else str(c.get('reasoning_effort'))+(' / '+c['thinking_type'] if c.get('thinking_type') else '')
        sdk=(f"MLX {d.get('mlx')}; mlx-lm {d.get('mlx-lm')}; weights {pin.get('revision','')[:12]}" if i.get('provider')=='local' else f"{i.get('provider')} {d.get(i.get('provider'))}; provider {str(i.get('provider_revision'))[:12]}")
        lines.append(f"| {r['provider']} | {model} / {mode} | {sdk} |")
    lines += ['', '| Replay / harness | Returned / scheduled | Provider attempts / unrun | Admissible next choices |','|---|---:|---:|---:|']
    for r in report['retained_state_replays']:lines.append(f"| {r['provider']} / {r['harness']} | {r['returned_decisions']} / {r['scheduled_decisions']} | {r['attempted_provider_calls']} / {r['unrun_decisions']} | {r['admissible_next_actions']} |")
    lines += ['','Original v6.2 thinking replay produced one unknown interrupted generation followed by seven closed-channel refusals, not eight wrong model decisions. The v6.4 terminal-stop runner attempted one call and explicitly left seven unrun. Original replay scores use the retained uniform evaluator correction.','',
        f"Completed CLI attempts: {len(report['completed_cli_runs'])}; pending directories: {len(report['pending_cli_directories'])}. Retries/development suffixes remain separate from planned repeat slots.",'',
        '| CLI attempt | Harness | Verified task | Inputs | Calls | Seconds excluding review |','|---|---|---|---:|---:|---:|']
    for r in report['completed_cli_runs']:
        if 'audit' not in r:continue
        a=r['audit'];n=r['metrics']['model_calls'];lat=a['latency']['excluding_review_s']
        note='yes (locale conditional)' if a['functional_pass'] and a['conditional_on_application_following_observed_locale'] else 'yes' if a['functional_pass'] else 'no'
        calls=str(n) if n is not None else str(r['metrics']['known_generation_dispatches'])+' known + unknown'
        lines.append(f"| {Path(r['run']).name} | {r['harness']} | {note} | {a['execution']['task_input_requests']} | {calls} | {lat:.2f} |")
    for aborted in report.get('operator_aborted_cli_runs',[]):
        reserved=aborted.get('cost',{}).get('ledger_unknown_reserved_usd')
        lines += ['',f"Operator abort: `{Path(aborted['run']).name}` — {aborted['public_tool_event_files']} public tool events, {aborted['sdk_dispatch_input_files']} SDK dispatch request(s), generation usage unknown; unresolved reservation ${reserved:.5f}. Excluded from pending runs and model-failure/qualification counts. Replacement/development suffixes receive no automatic slot credit." if reserved is not None else f"Operator abort: `{Path(aborted['run']).name}`; see retained abort and cost evidence; no qualification credit."]
    lines += ['','| CLI attempt / exact harness | Input normalized / gross | Output | Tools | Repeated-page feedback | Human interactions | Compactions |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in report['completed_cli_runs']:
        if 'audit' not in r:continue
        m=r['metrics'];c=r['interaction_counts'];partial=' known' if not m['usage_complete'] else ''
        lines.append(f"| {Path(r['run']).name} / {r['harness']} | {m['known_input_tokens']} / {m['known_gross_input_tokens']}{partial} | {m['known_output_tokens']}{partial} | {c['tool_calls']} | {c['repeated_inspection_feedback_events']} | {c['human_interactions']} | {c['context_compactions']} |")
    lines += ['','| CLI attempt / exact harness | Load s | Generation s | Token meter s (calls) | Non-review tools s | Review tool s | Human review s |',
        '|---|---:|---:|---:|---:|---:|---:|']
    def seconds(v):return 'unknown' if v is None else f'{v:.2f}'
    for r in report['completed_cli_runs']:
        if 'audit' not in r:continue
        t=r['timing_breakdown'];gen=seconds(t['generation_known_wall_s'])+(' known' if not t['generation_complete'] else '')
        meter=seconds(t['token_meter_service_s'])+f" ({t['token_meter_calls']})"+(' incl. load' if t['token_meter_includes_initial_model_load'] else '')
        tools=seconds(t['tool_nonreview_observed_union_s'])+(' partial' if not t['tool_timing_complete'] else '')
        lines.append(f"| {Path(r['run']).name} / {r['harness']} | {seconds(t['model_load_s'])} | {gen} | {meter} | {tools} | {seconds(t['review_tool_observed_union_s'])} | {seconds(t['human_review_s'])} |")
    lines += ['','Phase columns are not additive: load can overlap tokenizer preflight; review-tool wall includes human wait. Concurrent tool intervals are unioned. Repeated-page feedback also occurs after legitimate resets, so it is not itself a count of wrong model decisions. Unknown interrupted generation time/usage is not zero.']
    lines += ['','Each provider/harness needs three task cases × two trials = six verified trials. Versions are not pooled: v6.3 adds readout recovery/locale evidence; v6.4 clarifies buffer-only versus explicit persistence authority; v6.5 conservatively distinguishes generic Clear/C from All Clear/AC.','',
        '| Candidate / harness with attempts | Completed qualification slots / 6 | Verified | Unrun |','|---|---:|---:|---:|']
    for q in report['qualification']:
        if q['completed_attempts_including_development']:lines.append(f"| {q['provider']} / {q['harness']} | {q['completed_qualification_slots']} | {q['verified_slots']} | {q['unrun_slots']} |")
    budget=report['api_budget']
    total=budget['known_conservative_upper_usd']+budget['unknown_reserved_usd']
    lines += ['',f"Global hosted ledger: ${budget['known_conservative_upper_usd']:.6f} known conservative upper estimate + ${budget['unknown_reserved_usd']:.6f} unresolved reservation = ${total:.6f} committed/reserved against ${budget['cap_usd']:.2f}; remaining ${budget['cap_usd']-total:.6f}. Actual invoice unknown.",'',
        'The JSON records exact model/provider pins and SDKs, per-run normalized and gross tokens, generation/prefill/decode/cache/memory evidence, review latency, source hashes and setup records. Hosted dispatch timing includes network; unavailable memory/usage remains unknown.','',
        'Historical context: the earlier local nonthinking tools-v6.0 TextEdit run completed one exact buffer edit in 287.80 seconds excluding review. It is retained separately and contributes no new-comparison qualification credit. Anthropic returned an explicit service refusal at the causal connection gate; no desktop failure is inferred from that.','',
        'Fixture resets and operator reopening of the existing disposable TextEdit document are setup assistance. Faithful scope approval supplies no action hints. The declined v6.3 TextEdit Save proposal made no task edits and remains a failure; the generic persistence description was indirect, so it motivated the separately versioned v6.4 clarification.','',
        'One verified v6.3 Calculator task is not a winner or a six-trial qualification. Its 246.41-second latency also exceeds the 120-second practical target. No autonomous language, saved-file, or global unchanged-desktop claim.','',
        'The v6.3 local Calculator trial issued only full Clear and the first digit. It reused an old action ID, later invented a scope reference, and timed out: 14 completed generations plus one interrupted call with unknown usage. The correct scope was absent after compaction in the latter input; status recovery was available. Guards rejected both invalid actions. This mixes model reference/recovery failure with retained-context loss, not wrong-target desktop writes.','',
        'The v6.4 fresh arithmetic trial failed after 18 inputs. A wrong digit continuation was followed by a harness reset error: Clear retained `81−` in the UI while the witness claimed an empty known start. The final UI was `81−81−29÷4` / `−7.25`. Both causes are retained; the conservative v6.5 guard is a separate version, not retrospective task success.','',
        'Rerun: `.venv/bin/python tools/comparison_report.py` (CPU, file reads only; creates a new timestamped snapshot).','']
    if any(Path(r['run']).name=='v65-openai-fresh-1' and 'independent_review_diagnosis' in r for r in report['completed_cli_runs']):
        lines[-2:-2]=['The v6.5 fresh arithmetic trial used correct All Clear resets and accepted fresh references, but twice entered `81−29÷4` and obtained73.75. The original grouped request and current state were present at the first Divide. Compaction later lost the prior wrong result; action-ID refresh required extra inspection. These are distinct recovery/latency burdens, not a reset defect or proof that grouping capabilities were already visible. The task remained incomplete.','']
    if any(Path(r['run']).name=='v65-local-calculator-1' and 'independent_review_diagnosis' in r for r in report['completed_cli_runs']):
        lines[-2:-2]=['The v6.5 local Calculator run issued All Clear,1,9, then stalled on status/detail reads and timed out. Unlike v6.3, correct scope and the next digit2 action remained in its final inputs; no stale reference was refused. Seventeen completed generations plus one interrupted call consumed572.04seconds excluding review; known prefill438.05seconds dominated completed generation511.33seconds.','']
    if any(Path(r['run']).name=='v65-openai-calculator-2' and 'independent_review_diagnosis' in r for r in report['completed_cli_runs']):
        lines[-2:-2]=['The v6.5 hosted runs verified two exact TextEdit buffer edits and two Calculator executions (including replacement1b), the strongest observed partial result here. Calculator2 first invented a goal ID absent from its compacted context; the guard refused, status restored the real ID, and model-selected read-only rebinding plus fresh verification succeeded. The fresh arithmetic task still failed; replacement1b does not silently fill an aborted qualification slot. No full qualification or winner.','']
    if report.get('presentation_only_equivalence_evidence'):
        lines[-2:-2]=['Calculator2 used a separately recorded presentation-only build:92 packaged files unchanged; CLI/session rendering changes have retained AST-equivalence evidence. Tool descriptions, model inputs/results, policy and guards were reported unchanged. Exact wheel/install hashes and proof are in JSON; tool-version separation and qualification rules remain unchanged.','']
    if report.get('operator_component_diagnostics'):
        lines[-2:-2]=['The operator-directed Mode-button component diagnostic is recorded separately: zero model calls and no task/qualification credit. It is not a completed CLI model trial.','']
    return '\n'.join(lines)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',type=Path,default=ROOT/'artifacts/model-comparison-v7-001');p.add_argument('--out',type=Path)
    args=p.parse_args(); base=args.base.resolve();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    out=(args.out or base/'results-preliminary'/stamp).resolve();report=build(base,out)
    report['tool_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');(out/'report.md').write_text(md(report))
    print(json.dumps({'report':str(out/'report.json'),'markdown':str(out/'report.md'),'completed_cli_runs':len(report['completed_cli_runs']),'pending_cli_directories':len(report['pending_cli_directories'])}))

if __name__=='__main__':main()
