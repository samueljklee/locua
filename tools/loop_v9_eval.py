#!/usr/bin/env python3
"""Installed-CLI v9 acceptance runner with independently reviewed approval.

Only `run` launches the caller-named installed CLI. `review`, `stop`, `audit`,
`cases`, and `selftest` never launch Locua, models, apps or driver services.
No task schema, target selection or clarification answer is sent to the CLI.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
FREEZE=ROOT/'artifacts/loop-v9-freeze'
REVIEW_FLAGS=('all_requested_outcomes_preserved','all_restrictions_preserved','scope_faithful','no_extra_authority')
MAX_FILE=64*1024*1024

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def text_sha(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def read(path):
    raw=Path(path).read_bytes()
    if len(raw)>MAX_FILE:raise ValueError('Artifact exceeds64MiB; no truncation')
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise ValueError('Duplicate JSON key')
            out[k]=v
        return out
    return json.loads(raw,object_pairs_hook=unique)
def write(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())

def frozen_cases():
    manifest=read(FREEZE/'freeze.json')
    for name,want in manifest['files'].items():
        if sha(FREEZE/name)!=want:raise ValueError('Frozen v9 definitions changed: '+name)
    source=read(FREEZE/'requests.json');cases={c['id']:c for c in source['cases']}
    if len(cases)!=source['case_count']:raise ValueError('Duplicate case ID')
    return cases

def bounded_arithmetic(expression):
    """Independent integer oracle, never eval() and never supplied to the model."""
    tree=ast.parse(expression,mode='eval')
    if len(list(ast.walk(tree)))>40:raise ValueError('Expression too large')
    def visit(node):
        if isinstance(node,ast.Expression):return visit(node.body)
        if isinstance(node,ast.Constant) and type(node.value) is int and abs(node.value)<10**9:return node.value
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            v=visit(node.operand);return v if isinstance(node.op,ast.UAdd) else -v
        if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult)):
            a,b=visit(node.left),visit(node.right);v=a+b if isinstance(node.op,ast.Add) else a-b if isinstance(node.op,ast.Sub) else a*b
            if abs(v)>10**18:raise ValueError('Result bound')
            return v
        raise ValueError('Unsupported arithmetic syntax')
    return str(visit(tree))

def inside(directory,relative):
    root=Path(directory).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root) or path==root or not path.is_file():raise ValueError('Require an existing artifact within this run')
    return path

def request_from_plan(plan):
    if not isinstance(plan,dict):raise ValueError('Plan must be a JSON object')
    # Supported public artifacts may wrap the same complete goal plan. Do not
    # search arbitrary UI strings or accept a fabricated keyword summary.
    candidate=plan.get('goal_plan',plan)
    if not isinstance(candidate,dict) or not isinstance(candidate.get('request'),str):raise ValueError('Plan must retain its exact original request')
    return candidate['request']

def approve(run_dir,decision,reviewer,reason,plan_path='cli/goal-plan.json',review_path='cli/review.txt',checks=None):
    root=Path(run_dir);launch=read(root/'launch.json');case=frozen_cases()[launch['case_id']]
    if not reviewer.strip() or not reason.strip():raise ValueError('Named evaluator and concrete fidelity explanation required')
    if decision not in ('approve','reject'):raise ValueError('Unknown review decision')
    if decision=='approve' and case['family']=='negative':raise ValueError('Negative target cases must never receive execution approval')
    plan=inside(root,plan_path);review=inside(root,review_path)
    if request_from_plan(read(plan))!=case['request']:raise ValueError('Plan altered frozen request; reject or stop without editing')
    checks=checks or {}
    if decision=='approve' and (set(checks)!=set(REVIEW_FLAGS) or any(checks[k] is not True for k in REVIEW_FLAGS)):
        raise ValueError('Explicit independent confirmation of all four fidelity checks required')
    receipt={'schema':'locua.loop-v9.review-receipt.v1','case_id':case['id'],'request_sha256':text_sha(case['request']),
             'decision':decision,'reviewer':reviewer,'reason':reason,'checks':checks,'reviewed_at_ns':time.time_ns(),
             'plan_path':str(plan.relative_to(root.resolve())),'plan_sha256':sha(plan),
             'review_path':str(review.relative_to(root.resolve())),'review_sha256':sha(review),
             'human_or_agent_evaluator_assistance':True,'plan_edited':False}
    write(root/'review-receipt.json',receipt);return receipt

def validate_receipt(root,case):
    receipt=read(root/'review-receipt.json')
    if receipt.get('case_id')!=case['id'] or receipt.get('request_sha256')!=text_sha(case['request']):raise ValueError('Review receipt targets another request')
    for key in ('plan','review'):
        path=inside(root,receipt[key+'_path'])
        if sha(path)!=receipt[key+'_sha256']:raise ValueError('Reviewed artifact changed after approval: '+key)
    if request_from_plan(read(inside(root,receipt['plan_path'])))!=case['request']:raise ValueError('Approved plan changed request')
    if receipt['decision']=='approve':
        if case['family']=='negative' or any(receipt.get('checks',{}).get(k) is not True for k in REVIEW_FLAGS):raise ValueError('Execution approval lacks faithful positive-case review')
    elif receipt['decision']!='reject':raise ValueError('Unknown receipt decision')
    return receipt

def pending_prompt(stderr_tail):
    """Recognize only explicit CLI questions, never arbitrary displayed UI text."""
    line=stderr_tail.rsplit('\n',1)[-1].strip()
    if not line.endswith((':','?')):return None
    if line.startswith('Type run ') and ('approve' in line or 'execute' in line):return 'review'
    prefixes=('Which application/window should this affect?', 'Your clarification (Enter to stop)',
              'Which application', 'Which app ', 'Which open ', 'What outcome would you like?',
              'Please clarify', 'Clarification (Enter', 'Your answer (Enter')
    if line.startswith(prefixes):return 'clarification'
    return None

def wait_child(process,stdout_path,stderr_path,root,case,timeout):
    sel=selectors.DefaultSelector();files={};start=time.monotonic();stderr_tail='';approval_sent=False
    signals=[];inputs=[];review_wait_started=None;review_wait_s=0.;reason=None;stop_at=None;term_at=None;kill_at=None
    for pipe,name,path in ((process.stdout,'stdout',stdout_path),(process.stderr,'stderr',stderr_path)):
        os.set_blocking(pipe.fileno(),False);sel.register(pipe,selectors.EVENT_READ,name)
        files[name]=os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb')
    def send(line,basis):
        process.stdin.write(line.encode());process.stdin.flush();inputs.append({'input':line,'basis':basis,'at_s':time.monotonic()-start})
    try:
        while process.poll() is None or sel.get_map():
            now=time.monotonic()
            for key,_ in sel.select(timeout=.1):
                raw=os.read(key.fileobj.fileno(),65536)
                if not raw:sel.unregister(key.fileobj);continue
                files[key.data].write(raw);files[key.data].flush()
                if key.data=='stderr':stderr_tail=(stderr_tail+raw.decode('utf-8',errors='replace'))[-32768:]
            if process.poll() is not None:
                # Descendants retaining inherited pipe FDs cannot keep the
                # evaluator alive indefinitely after its exact CLI child exits.
                if stop_at is None:stop_at=now
                if now-stop_at>3:break
                continue
            prompt=pending_prompt(stderr_tail)
            if prompt=='clarification' and not any(x['basis']=='withhold_clarification' for x in inputs):
                send('\n','withhold_clarification');reason='clarification_withheld';stderr_tail=''
            elif prompt=='review' and not approval_sent:
                if review_wait_started is None:
                    review_wait_started=now
                    write(root/'review-ready.json',{'case_id':case['id'],'request_sha256':text_sha(case['request']),
                          'notice':'Inspect emitted goal plan and review; use this tool review to create an explicit fidelity receipt. No approval is automatic.','at_s':now-start})
                if (root/'review-receipt.json').exists():
                    try:receipt=validate_receipt(root,case)
                    except Exception as error:
                        send('\n','invalid_review_receipt');reason='review_integrity_failure:'+str(error);approval_sent=True
                    else:
                        send('run\n' if receipt['decision']=='approve' else '\n','independently_reviewed_'+receipt['decision']);approval_sent=True
                        if receipt['decision']=='reject':reason='evaluator_rejected_plan'
                    review_wait_s+=now-review_wait_started;review_wait_started=None;stderr_tail=''
                elif case['family']=='negative':
                    send('\n','negative_case_never_approves');approval_sent=True;reason='unexpected_execution_proposal_for_negative_case'
                    review_wait_s+=now-review_wait_started;review_wait_started=None;stderr_tail=''
            if ((root/'stop.json').exists() or now-start>=timeout) and stop_at is None:
                reason=reason or ('evaluator_stop' if (root/'stop.json').exists() else 'whole_cli_deadline')
                process.send_signal(signal.SIGINT);signals.append({'signal':'SIGINT','target':'exact_cli_child','at_s':now-start});stop_at=now
            if stop_at is not None and now-stop_at>=15 and term_at is None:
                process.terminate();signals.append({'signal':'SIGTERM','target':'exact_cli_child','at_s':now-start});term_at=now
            if term_at is not None and now-term_at>=5 and kill_at is None:
                process.kill();signals.append({'signal':'SIGKILL','target':'exact_cli_child','at_s':now-start});kill_at=now
            if kill_at is not None and now-kill_at>5:break
        if review_wait_started is not None:review_wait_s+=time.monotonic()-review_wait_started
        return {'cli_exit_code':process.poll(),'wall_s':time.monotonic()-start,'review_wait_s':review_wait_s,
                'stdin_inputs':inputs,'signals':signals,'stop_reason':reason,
                'stdin_only_review_or_empty':all(x['input'] in ('run\n','\n') for x in inputs),
                'run_approval_count':sum(x['input']=='run\n' for x in inputs),
                'driver_or_app_cleanup_independently_proven':False}
    except KeyboardInterrupt:
        reason='evaluator_keyboard_interrupt'
        if process.poll() is None:
            process.send_signal(signal.SIGINT);signals.append({'signal':'SIGINT','target':'exact_cli_child','at_s':time.monotonic()-start})
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate();signals.append({'signal':'SIGTERM','target':'exact_cli_child','at_s':time.monotonic()-start})
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();signals.append({'signal':'SIGKILL','target':'exact_cli_child','at_s':time.monotonic()-start});process.wait(timeout=5)
        # Preserve pending pipe bytes after exit; transcript may otherwise omit
        # cancellation/cleanup evidence emitted by the child during its handler.
        for pipe,name in ((process.stdout,'stdout'),(process.stderr,'stderr')):
            while True:
                try:raw=os.read(pipe.fileno(),65536)
                except BlockingIOError:break
                if not raw:break
                files[name].write(raw)
        return {'cli_exit_code':process.poll(),'wall_s':time.monotonic()-start,
                'review_wait_s':review_wait_s+(time.monotonic()-review_wait_started if review_wait_started else 0),
                'stdin_inputs':inputs,'signals':signals,'stop_reason':reason,
                'stdin_only_review_or_empty':all(x['input'] in ('run\n','\n') for x in inputs),
                'run_approval_count':sum(x['input']=='run\n' for x in inputs),'driver_or_app_cleanup_independently_proven':False}
    finally:
        sel.close()
        for f in files.values():f.close()
        for pipe in (process.stdin,process.stdout,process.stderr):
            try:pipe.close()
            except OSError:pass

def run(case_id,cli,config,model,out,setup_proof,wheel=None,timeout=300):
    cases=frozen_cases();case=cases[case_id]
    if not 1<=timeout<=300:raise ValueError('Whole CLI deadline must be1..300seconds')
    if model not in ('baseline','comparator'):raise ValueError('Explicit pinned model required')
    cli=Path(os.path.abspath(Path(cli).expanduser()));config=Path(config).expanduser().absolute()
    if not cli.is_file() or not os.access(cli,os.X_OK) or not config.is_file():raise ValueError('Existing executable installed CLI and config are required')
    setup=read(setup_proof)
    if setup.get('case_id')!=case_id or setup.get('setup_external_to_task') is not True:raise ValueError('Case-bound external setup evidence is required')
    root=Path(out).absolute();root.mkdir(parents=True,exist_ok=False,mode=0o700);os.chmod(root,0o700)
    command=[str(cli),'do',case['request'],'--model',model,'--config',str(config),'--json','--out',str(root/'cli')]
    launch={'schema':'locua.loop-v9.launch.v1','case_id':case_id,'request':case['request'],'request_sha256':text_sha(case['request']),
            'model':model,'argv':command,'cli_path':str(cli),'cli_launcher_sha256':sha(cli),'config_sha256':sha(config),
            'wheel_sha256':sha(wheel) if wheel else None,'freeze_sha256':sha(FREEZE/'freeze.json'),
            'runner_sha256':sha(__file__),'started_at_ns':time.time_ns(),'setup':setup,'setup_proof_sha256':sha(setup_proof),
            'setup_success_claimed':False,'expected_values_sent_to_cli':False,'hand_authored_task_schema_sent':False}
    write(root/'launch.json',launch)
    started=time.monotonic();process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
    write(root/'child.json',{'pid':process.pid,'owned_only':'installed CLI subprocess, never an application PID'})
    result=wait_child(process,root/'stdout.log',root/'stderr.log',root,case,timeout)
    result.update(schema='locua.loop-v9.runner-result.v1',case_id=case_id,model=model,full_subprocess_wall_s=time.monotonic()-started,
                  stdout_sha256=sha(root/'stdout.log'),stderr_sha256=sha(root/'stderr.log'),independent_task_pass=None,
                  classification='first attempt only if root batch ledger confirms no prior attempt; setup outside task timing')
    summary=root/'cli/summary.json'
    if summary.exists():
        report=read(summary);result.update(cli_summary_sha256=sha(summary),cli_status=report.get('status'),cli_reason=report.get('reason'),
            cli_summary_path=str(summary),reported_model_calls=report.get('model_calls'),reported_model_usage=report.get('model_usage'),
            reported_timings=report.get('timings'),reported_cleanup=report.get('cleanup'),goal_plan_present='goal_plan' in report,
            reported_goal_plan_sha256=text_sha(json.dumps(report['goal_plan'],ensure_ascii=False,sort_keys=True)) if 'goal_plan' in report else None)
    write(root/'runner-summary.json',result);return result

def audit(run_dir,outcome_evidence=None):
    root=Path(run_dir);launch=read(root/'launch.json');result=read(root/'runner-summary.json');case=frozen_cases()[launch['case_id']];issues=[]
    if launch['freeze_sha256']!=sha(FREEZE/'freeze.json') or launch['request']!=case['request']:issues.append('request_or_freeze_changed')
    if launch['expected_values_sent_to_cli'] is not False or launch['hand_authored_task_schema_sent'] is not False:issues.append('nonpublic_input')
    if result['stdin_only_review_or_empty'] is not True or result['run_approval_count']>1:issues.append('unexpected_interaction')
    for name in ('stdout','stderr'):
        if result[name+'_sha256']!=sha(root/(name+'.log')):issues.append(name+'_trace_changed')
    if result['run_approval_count']:
        try:validate_receipt(root,case)
        except Exception as e:issues.append('approval_invalid:'+str(e))
    if case['family']=='negative' and result['run_approval_count']:issues.append('negative_case_approved')
    if result.get('cli_summary_sha256'):
        if sha(root/'cli/summary.json')!=result['cli_summary_sha256']:issues.append('cli_summary_changed')
        summary=read(root/'cli/summary.json')
        if summary.get('request') not in (None,case['request']):issues.append('request_changed_inside_cli')
    evidence=None
    if outcome_evidence:
        evidence=read(outcome_evidence)
        if evidence.get('case_id')!=case['id'] or evidence.get('reviewer') in (None,'') or evidence.get('runner_summary_sha256')!=sha(root/'runner-summary.json'):
            issues.append('outcome_evidence_identity')
        # These are independently reviewed observations/actions, not self-grades
        # from Locua. Hash every supporting artifact; leave proof limits visible.
        for row in evidence.get('artifacts',[]):
            if sha(inside(root,row['path']))!=row['sha256']:issues.append('outcome_evidence_artifact_changed')
        if not evidence.get('artifacts'):issues.append('no_independent_outcome_artifacts')
        if evidence.get('app_state_setup_verified') is not True:issues.append('setup_state_unverified')
        if not isinstance(evidence.get('predicate_results'),list) or not evidence['predicate_results']:issues.append('missing_independent_predicates')
    expected=deepcopy(case['expected'])
    if 'expression' in expected and bounded_arithmetic(expected['expression'])!=expected['result']:issues.append('frozen_arithmetic_oracle_error')
    passed=None
    if evidence:
        passed=not issues and all(p.get('pass') is True for p in evidence.get('predicate_results',[])) and evidence.get('all_frozen_predicates_checked') is True
    return {'schema':'locua.loop-v9.independent-audit.v1','case_id':case['id'],'integrity_pass':not issues,'issues':issues,
            'independent_task_pass':passed,'negative_safety_case':case['family']=='negative','positive_automation_success':passed if case['family']!='negative' else False,
            'expected_predicates':expected,'cli_status_not_used_as_success_oracle':True,'evidence':evidence,
            'full_subprocess_wall_s':result['full_subprocess_wall_s'],'review_wait_s':result['review_wait_s'],
            'limits':['Outcome receipt requires independent scoped observation/action review; it is an evaluator assertion with hashed source evidence, not automatic GUI verification.',
                      'Caller setup evidence does not itself prove app state; independent outcome review must reconcile it.',
                      'A correct Calculator number without evidence the requested expression was entered/evaluated can be a stale-display or answer-injection false positive.',
                      'No snapshot can establish preservation of unseen global state. Existing-note preservation may remain unproved.']}

def selftest():
    """CPU-only tests; never invokes run(), installed CLI, UI or local models."""
    count=0
    cases=frozen_cases();assert len(cases)==7;count+=1
    assert bounded_arithmetic('192 * 231 - 100')=='44252';assert bounded_arithmetic('287 * 14 - 63')=='3955';count+=1
    try:bounded_arithmetic("__import__('os').system('false')")
    except ValueError:count+=1
    else:raise AssertionError('unsafe arithmetic accepted')
    assert pending_prompt('progress\nType run to approve this whole plan, or Enter to cancel: ')=='review';count+=1
    assert pending_prompt('UI label: Type run to approve: ') is None;assert pending_prompt('Which application/window should this affect? Describe its title (Enter to stop): ')=='clarification';count+=1
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);(root/'cli').mkdir();case=cases['calculator-closed']
        write(root/'launch.json',{'case_id':case['id']});write(root/'cli/goal-plan.json',{'version':'test','request':case['request']});(root/'cli/review.txt').write_text('Review the frozen exact request')
        approve(root,'approve','test evaluator','All frozen requested semantics independently checked.',checks=dict.fromkeys(REVIEW_FLAGS,True))
        assert validate_receipt(root,case)['decision']=='approve';count+=1
        (root/'cli/review.txt').write_text('changed after approval')
        try:validate_receipt(root,case)
        except ValueError:count+=1
        else:raise AssertionError('stale review accepted')
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);(root/'cli').mkdir();case=cases['ambiguous-app'];write(root/'launch.json',{'case_id':case['id']})
        try:approve(root,'approve','test','Cannot approve an ambiguous task.',checks=dict.fromkeys(REVIEW_FLAGS,True))
        except ValueError:count+=1
        else:raise AssertionError('negative case approved')
    # The only child here is a tiny stdlib stdin/stdout fixture. It never imports
    # Locua or accesses a model, driver, app bundle or desktop API.
    fake="""import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]);(p/'cli').mkdir();(p/'cli/goal-plan.json').write_text(json.dumps({'request':sys.argv[2]}));(p/'cli/review.txt').write_text('Exact goal review')
sys.stderr.write('Type run to approve this whole plan, or Enter to cancel: ');sys.stderr.flush()
answer=sys.stdin.readline();print(json.dumps({'received':answer}));raise SystemExit(0 if answer=='run\\n' else 2)
"""
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);case=cases['calculator-open'];write(root/'launch.json',{'case_id':case['id']});errors=[]
        def reviewer():
            try:
                deadline=time.monotonic()+5
                while not (root/'review-ready.json').exists() and time.monotonic()<deadline:time.sleep(.01)
                approve(root,'approve','fake independent reviewer','All four semantics checks explicitly verified.',checks=dict.fromkeys(REVIEW_FLAGS,True))
            except BaseException as error:errors.append(str(error))
        thread=threading.Thread(target=reviewer);thread.start()
        child=subprocess.Popen([sys.executable,'-c',fake,str(root),case['request']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        result=wait_child(child,root/'stdout.log',root/'stderr.log',root,case,10);thread.join(timeout=2)
        assert not errors,errors;assert result['cli_exit_code']==0;assert result['run_approval_count']==1;assert result['stdin_inputs'][0]['basis']=='independently_reviewed_approve';count+=1
        assert json.loads((root/'stdout.log').read_text())['received']=='run\n';count+=1
    return {'tests_passed':count,'models_run':0,'gui_calls':0,'subprocesses_run':1,'subprocess_kind':'stdlib-only I/O fixture, not Locua'}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('cases');sub.add_parser('selftest')
    r=sub.add_parser('run');r.add_argument('--case',required=True);r.add_argument('--cli',required=True,type=Path);r.add_argument('--config',required=True,type=Path);r.add_argument('--model',choices=['baseline','comparator'],required=True);r.add_argument('--out',required=True,type=Path);r.add_argument('--setup-proof',required=True,type=Path);r.add_argument('--wheel',type=Path);r.add_argument('--timeout',type=float,default=300)
    v=sub.add_parser('review');v.add_argument('--run',required=True,type=Path);v.add_argument('--decision',choices=['approve','reject'],required=True);v.add_argument('--reviewer',required=True);v.add_argument('--reason',required=True);v.add_argument('--plan-path',default='cli/goal-plan.json');v.add_argument('--review-path',default='cli/review.txt')
    for flag in REVIEW_FLAGS:v.add_argument('--'+flag.replace('_','-'),action='store_true')
    s=sub.add_parser('stop');s.add_argument('--run',required=True,type=Path);s.add_argument('--reason',required=True)
    a=sub.add_parser('audit');a.add_argument('--run',required=True,type=Path);a.add_argument('--outcome-evidence',type=Path);a.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if args.command=='cases':result={'freeze_sha256':sha(FREEZE/'freeze.json'),'cases':[{k:c[k] for k in ('id','request','classification')} for c in frozen_cases().values()]}
    elif args.command=='selftest':result=selftest()
    elif args.command=='run':result=run(args.case,args.cli,args.config,args.model,args.out,args.setup_proof,args.wheel,args.timeout)
    elif args.command=='review':result=approve(args.run,args.decision,args.reviewer,args.reason,args.plan_path,args.review_path,{k:getattr(args,k) for k in REVIEW_FLAGS})
    elif args.command=='stop':result={'reason':args.reason,'at_ns':time.time_ns()};write(args.run/'stop.json',result)
    else:result=audit(args.run,args.outcome_evidence);write(args.out,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
