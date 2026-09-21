"""Thin native CLI over Amplifier's unchanged standard tool loop.

No local state machine chooses the next tool. Cua capabilities enforce their own
freshness/review contracts; the final report comes from observed verification.
"""
import asyncio
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import time

from .errors import LocuaError

from .instruction_policy import BASELINE as SYSTEM

# Standard Amplifier ephemeral compaction keeps the canonical transcript. This
# conservative cap leaves room for native tools/template overhead; the local
# provider also supplies exact upcoming-request counts through Amplifier's
# standard measured-view protocol. Counting and compaction precede generation.
CONTEXT_CONFIG = {
    'max_tokens':16000, 'compact_threshold':0.75, 'target_usage':0.50,
    'protected_recent':0.15, 'protected_tool_results':2, 'truncate_chars':500,
    'compaction_notice_enabled':True, 'compaction_notice_verbosity':'minimal',
    'compaction_notice_token_reserve':256, 'token_meter':'actual',
    'max_tool_result_bytes':1048576,
}


def _dependencies():
    try:
        from amplifier_core import AmplifierSession, HookResult
        import amplifier_module_loop_streaming
        import amplifier_module_context_simple
    except ImportError as error:
        raise LocuaError('amplifier_dependencies_missing', str(error),
            'Install the pinned Amplifier extra with python -m pip install "locua[amplifier]" in this CLI environment.') from error
    return AmplifierSession, HookResult


def _tool_progress(data):
    """Bounded user-facing receipts; never dump UI trees or derive outcomes."""
    result = data.get('result')
    if hasattr(result, 'model_dump'):
        result = result.model_dump()
    if not isinstance(result, dict):
        return ['Tool returned an unrecognized result envelope; see the private trace.']
    output = result.get('output')
    if not isinstance(output, dict):
        return ['Tool returned.' if result.get('success') is True else 'Tool failed; see the private trace.']
    name = data.get('tool_name', data.get('name', 'tool'))
    status = output.get('status', 'returned')
    def brief(value, limit=180):
        if not isinstance(value, str):
            return ''
        # UI-sourced exception text stays bounded and cannot inject terminal lines.
        value = ' '.join(''.join(c if c.isprintable() else ' ' for c in value).split())
        return value if len(value) <= limit else value[:limit] + '…'
    label = brief(str(name), 64) + ': ' + brief(str(status), 48)
    if name == 'locua_act_sequence':
        done, planned = output.get('steps_completed'), output.get('steps_planned')
        if type(done) is int and type(planned) is int:
            label += f'; {done}/{planned} steps completed (outcome verification separate)'
    if status in ('refused', 'unavailable', 'uncertain', 'canceled') or result.get('success') is False:
        reason = brief(output.get('reason') or result.get('error'))
        code = brief(output.get('code'), 80)
        lines = [label + ((' [' + code + ']') if code else '') + (': ' + reason if reason else '; see the private trace.')]
        failed_step=output.get('failed_step');selected=output.get('selected_control')
        if name=='locua_act_sequence' and type(failed_step) is int and isinstance(selected,dict):
            control=brief(selected.get('name') or selected.get('role') or 'unnamed control',80)
            kind=brief(selected.get('action_kind') or 'action',32)
            lines.append(f'Rejected step {failed_step}: {control} ({kind}).'+
                (' No sequence input was attempted.' if output.get('steps_attempted')==0 else ''))
        if output.get('exploration_feedback', {}).get('code') == 'repeated_failed_read':
            lines.append('Repeated read failure: no fresh UI state. Use supported recovery or report the concrete blocker; repeating the same read is not progress.')
        if output.get('no_retry') is True:
            lines.append('Input may already have occurred; repeating it is disabled.')
            recovery = output.get('reconciliation')
            if isinstance(recovery, dict) and recovery.get('available') is True and recovery.get('read_only') is True:
                lines.append('Fresh read-only verification of the original reviewed outcomes is available.')
        return lines
    page = output.get('overview', output)
    if isinstance(page, dict) and page.get('version') == 'progressive-ui-v1':
        coverage = page.get('coverage', {})
        if not isinstance(coverage, dict):
            coverage = {}
        operation = page.get('operation')
        kinds = {'overview': 'overview entries', 'list': 'list entries', 'control': 'detail entries'}
        kind = kinds.get(operation, 'entries')
        parts = []
        for key, description in (('returned_count', kind + ' returned'), ('remaining_count', 'remaining')):
            value = coverage.get(key)
            if type(value) is int and value >= 0:
                parts.append(str(value) + ' ' + description)
        counts = page.get('counts')
        if operation == 'overview' and isinstance(counts, dict):
            for key in ('controls', 'regions'):
                value = counts.get(key)
                if type(value) is int and value >= 0:
                    parts.append(str(value) + ' total ' + key)
        if coverage.get('continuation') is not None:
            parts.append('continuation available')
        label += '; ' + ', '.join(parts) if parts else ''
    elif isinstance(output.get('windows'), list):
        label += '; ' + str(len(output['windows'])) + ' windows returned'
    elif isinstance(output.get('items'), list):
        label += '; ' + str(len(output['items'])) + ' entries returned'
        if type(output.get('total')) is int:
            label += ' of ' + str(output['total'])
        if output.get('next_start') is not None:
            label += '; continuation available'
    feedback = output.get('exploration_feedback')
    lines = [label + '.']
    reconciliation = output.get('reconciliation')
    if (name == 'locua_verify' and status == 'verified'
            and output.get('scope_status') == 'reconciled_verified'
            and isinstance(reconciliation, dict)
            and reconciliation.get('status') == 'current_predicates_verified'
            and reconciliation.get('input_authority_restored') is False):
        lines.append('Original reviewed outcomes match fresh read-only evidence; input remains disabled. Delivery of the earlier action is still unproved.')
    if name in ('locua_act', 'locua_act_sequence') and status in ('dispatched', 'verified', 'sequence_completed') and output.get('action_started') is True:
        witness = output.get('arithmetic_input')
        if isinstance(witness, dict):
            entered = witness.get('issued_expression_since_clear')
            evaluated = witness.get('issued_evaluation')
            if isinstance(entered, str) and (entered or witness.get('known_start') is True):
                literal = json.dumps(entered, ensure_ascii=True)
                lines.append('Issued input: ' + brief(literal, 160) + ' (issuance only; result not yet verified).')
            if isinstance(evaluated, str):
                lines.append('Evaluation issued for: ' + brief(json.dumps(evaluated, ensure_ascii=True), 160) + ' (issuance only).')
        verification = output.get('verification')
        if (status == 'verified' and isinstance(verification, dict)
                and verification.get('plane') == 'editor_buffer'
                and verification.get('exact_value_proven') is True):
            lines.append('Exact editor-buffer readback confirmed after input; committed content and saving are not proved by this receipt.')
    if isinstance(feedback, dict) and feedback.get('code') == 'repeated_inspection':
        count = feedback.get('equivalent_inspection_count')
        repeats = ' (' + str(count) + ' times)' if type(count) is int and count > 1 else ''
        lines.append('Repeated inspection' + repeats + ': no new UI evidence; use a continuation, another scope or control detail, or report the missing evidence.')
    return lines


async def execute_session(request, provider, tools, *, out, progress=None,
                          system=SYSTEM, max_iterations=48, execution_facts=None,
                          owner_cancellation=None):
    """Reusable actual Amplifier session; also used by the model-only smoke.

The caller owns provider/tools and closes them. No custom orchestrator or module
resolver is mounted, and only the supplied local provider is available.
"""
    AmplifierSession, HookResult = _dependencies()
    from .engine.prototype.cli import private_json
    progress = progress or (lambda _: None)
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    config={'session':{
        'orchestrator':{'module':'loop-streaming','config':{'max_iterations':max_iterations}},
        'context':{'module':'context-simple','config':deepcopy(CONTEXT_CONFIG)}}}
    if execution_facts is not None:
        config['session']['orchestrator']['config']['ephemeral_injection_mode']='persist'
    session=AmplifierSession(config)
    trace=[];turns=0
    async def event(event,data):
        nonlocal turns
        row={'event':event,'data':deepcopy(data),'at_ns':time.time_ns()}
        # Event payloads may include SDK objects; preserve their public form.
        row=json.loads(json.dumps(row,default=lambda v:v.model_dump() if hasattr(v,'model_dump') else str(v)))
        trace.append(row)
        if event=='tool:post' and hasattr(provider,'register_structured_tool_result'):
            # Lossless representation registration only. The provider verifies
            # the exact framework serialization before using the typed object.
            provider.register_structured_tool_result(data.get('tool_call_id'),data.get('result'))
        if event=='tool:post' and owner_cancellation is not None:
            # Only the canonical owner's human-review latch can end this run.
            # UI text, model arguments and projected tool results are not signals.
            cancellation=owner_cancellation()
            if (isinstance(cancellation,dict) and cancellation.get('status')=='canceled'
                    and cancellation.get('reason')=='user_declined_review'
                    and cancellation.get('authority_revoked') is True):
                result['cancellation']=deepcopy(cancellation)
                coordinator.cancellation.request_graceful()
        if event=='provider:request':
            turns+=1;progress(f'[{turns}] '+('Local' if getattr(provider,'local_only',True) else 'Hosted')+' model choosing a tool or response…')
        elif event=='tool:pre':
            name=str(data.get('name',data.get('tool_name','unknown')))
            labels={'locua_apps':'Finding applications','locua_windows':'Finding application windows',
                'locua_launch':'Opening or reopening the selected application','locua_activate':'Activating the selected window',
                'locua_observe':'Reading the selected window','locua_inspect':'Inspecting UI controls',
                'locua_review':'Preparing the plan for review','locua_act':'Applying a reviewed action',
                'locua_act_sequence':'Applying a model-selected reviewed sequence',
                'locua_verify':'Checking the outcome','locua_clarify':'Clarifying missing information',
                'locua_status':'Reading retained task progress'}
            progress(labels.get(name,'Tool: '+name)+'…')
        elif event=='tool:post':
            for line in _tool_progress(data):progress(line)
        elif event=='context:compaction':
            progress('Older context compacted; original request and retained task state remain available.')
        if event=='provider:request' and execution_facts is not None:
            return HookResult(action='inject_context',ephemeral=True,context_injection_role='user',
                              context_injection=execution_facts())
        return HookResult(action='continue')
    result={'framework':'AmplifierSession / loop-streaming / context-simple',
            'config':config,'request':request,'events':trace,
            'decoding':'ordinary tool calling; RLCD not invoked','provider':provider.name,'inference_local_only':getattr(provider,'local_only',True),
            'dependencies':{name:importlib.metadata.version(name) for name in
                ('amplifier-core','amplifier-module-loop-streaming','amplifier-module-context-simple')}}
    try:
        await session.initialize()
        coordinator=session.coordinator
        if hasattr(provider,'mount'):
            await provider.mount(coordinator)
        else:
            await coordinator.mount('providers',provider,name=provider.name)
        for tool in tools:await coordinator.mount('tools',tool,name=tool.name)
        for name in ('provider:request','llm:response','tool:pre','tool:post','tool:error','orchestrator:complete','context:compaction','cancel:requested','cancel:completed'):
            coordinator.hooks.register(name,event,name='locua-trace-'+name,priority=100)
        context=coordinator.get('context')
        await context.add_message({'role':'system','content':system+'\nORIGINAL USER REQUEST (retain throughout):\n'+request})
        result['response']=await asyncio.wait_for(session.execute(request),timeout=600)
        result['transcript']=await context.get_messages()
        return result
    finally:
        try:
            try:
                if session.coordinator.get('context') is not None:
                    result['transcript']=await session.coordinator.get('context').get_messages()
            except Exception as error:result['transcript_error']=type(error).__name__+': '+str(error)
            finally:
                try:
                    await session.cleanup();result['session_cleanup']='closed'
                except Exception as error:
                    result['session_cleanup']='failed: '+str(error)
                    raise
        finally:private_json(root/'session.json',result)


def run(request=None, *, model='comparator', provider='local', thinking=False, budget_ledger=None, budget_cap_usd=15, task_observations=False, tool_profile='baseline', instruction_profile='baseline', out=None, config=None, ask, progress, **options):
    """Public synchronous CLI/library entry; all decisions belong to Amplifier."""
    from . import lib
    from .config import selected_path
    from .engine_adapter import artifact_directory, require
    from .engine.prototype.cli import private_json
    from .instruction_policy import instruction_policy, metadata, apply_tool_help
    selected_system = instruction_policy(instruction_profile)
    root=artifact_directory(out,'do');started=time.monotonic();provider_name=provider
    hosted=provider_name!='local'
    if tool_profile=='execution-state-v1' and (hosted or task_observations):
        raise LocuaError('invalid_run_option','Retained-state profile currently requires unscoped local observations.',
                         'Use --provider local without --task-observations, or select --tool-profile baseline.')
    scoped=hosted or task_observations
    report={'status':'starting','request':request,'artifacts':str(root),
        'harness':'amplifier','policy':'amplifier-standard-tool-loop-v6.5','tool_interface':'tools-v6.9','provider_integration':'providers-v7.0','model':model,'provider':provider_name,'thinking':thinking,'task_scoped_observations':scoped,
        'inference_local_only':not hosted,'decoding':'ordinary '+('hosted' if hosted else 'local')+' tool calling',
        'rlcd_used':False,'human_interactions':[],'human_wait_s':0.0,'tool_profile':tool_profile,
        'instructions':metadata(instruction_profile),
        'prefix_cache_policy':'exact_chunk_aligned_prefix_before_compaction_notice_v2' if model=='qwen38' and not thinking and not hosted else 'off',
        'natural_language_autonomy_proven':False,'saved_output_proven':False}
    def question(prompt,purpose='user_input'):
        tick=time.monotonic()
        try:
            try:answer=ask(prompt)
            except LocuaError as error:
                if error.code=='guided_input_ended':
                    report['input_ended']=True
                    raise KeyboardInterrupt from error
                raise
            report['human_interactions'].append({'prompt':prompt,'answer':answer,'purpose':purpose})
            return answer
        finally:report['human_wait_s']+=time.monotonic()-tick
    async def execute(cfg):
        from .amplifier_provider import LocalAmplifierProvider
        from .amplifier_tools import DesktopToolset
        if hosted:
            from .provider_selection import HostedAmplifierProvider
            provider=HostedAmplifierProvider(provider_name,model,out=root/'provider',budget_path=budget_ledger,spend_cap_usd=budget_cap_usd)
        else:
            local_options=({'qwen38_thinking':True} if thinking else {})
            provider=LocalAmplifierProvider(model=model,runtime_config=cfg,out=root/'provider',
                qwen38_prompt_cache=(model=='qwen38' and not thinking),
                qwen38_stable_prefix_cache=(model=='qwen38' and not thinking),**local_options)
        desktop=None
        try:
            desktop=DesktopToolset(cfg,root/'desktop',request,question,progress=progress,tool_profile=tool_profile)
            if tool_profile!='baseline':progress('Explicit experimental tool profile: '+tool_profile+'. Model and decoding unchanged.')
            tools=desktop.tools()
            if scoped:
                from .task_observation_scope import scoped_tools_for_request
                tools,scope_info=scoped_tools_for_request(desktop,request)
                report['observation_scope']=scope_info
                progress('Task-scoped observations: '+scope_info['description']+'. Other desktop content stays local.')
            tools=apply_tool_help(tools,instruction_profile)
            if instruction_profile!='baseline':
                progress('Explicit instruction experiment: '+instruction_profile+'. Action guards, observations, model and decoding unchanged.')
            execution_facts=None
            if tool_profile=='execution-state-v1':
                from .execution_state import render_execution_facts
                def execution_facts():
                    with desktop._lock:return render_execution_facts(desktop)
            def owner_cancellation():
                with desktop._lock:return deepcopy(desktop._cancellation)
            result=await execute_session(request,provider,tools,out=root/'session',progress=progress,
                                         system=selected_system,execution_facts=execution_facts,owner_cancellation=owner_cancellation)
            report['assistant_response']=result['response']
            report['session_cleanup']=result['session_cleanup']
            proof=desktop.finalize()
            if hasattr(proof,'__await__'):proof=await proof
            report['verification']=proof
            report['status']=proof.get('status','incomplete')
            report['reason']=proof.get('reason', 'Reviewed outcomes match fresh application evidence; full request coverage was human-reviewed.'
                if report['status']=='verified_reviewed_scope' else 'No verified coverage of the complete request')
            if report['status']=='verified_reviewed_scope':
                progress('VERIFIED: reviewed outcomes match fresh application evidence. Full request coverage was checked at review.')
                for scope in proof.get('verification',{}).get('scopes',{}).values():
                    for row in scope.get('goals',[]):
                        evidence=row.get('evidence') or {}
                        if row.get('matched') and 'actual' in evidence:
                            progress(str(row['goal_id'])+' = '+json.dumps(evidence['actual'],ensure_ascii=False)+' ('+str(evidence['plane'])+')')
            elif result.get('response'):
                progress('Model report (not verified): '+str(result['response']))
        finally:
            try:
                if desktop is not None:
                    closed=desktop.close()
                    if hasattr(closed,'__await__'):closed=await closed
                    report['desktop_cleanup']=closed
                    report['tool_evidence']=deepcopy(desktop.evidence)
                    if isinstance(closed,dict) and closed.get('status') not in ('closed','ok'):
                        report.update(status='blocked',reason='Desktop cleanup was not verified')
            finally:
                await provider.close()
                report['provider_closed']=provider._closed and (getattr(provider,'_service',None) is None or provider._service.closed)
                if hosted:report['provider_configuration']=deepcopy(provider.metadata)
                unknown=any(r.get('inference_started') and 'generation' not in r and not r.get('pre_generation_refusal') for r in provider.records)
                known=[r['generation'] for r in provider.records if 'generation' in r]
                budgets=provider.budget_records
                report['metrics']={'provider_requests':len(provider.records),
                    'provider_responses':sum(r['status']=='completed' for r in provider.records),
                    'model_calls':None if unknown else sum(r.get('generation_calls',0) for r in known),
                    'unknown_generation_usage':unknown,
                    'pre_generation_refusals':[deepcopy(r['pre_generation_refusal']) for r in provider.records if r.get('pre_generation_refusal')],
                    'input_tokens':None if unknown else sum(r['usage']['input_tokens'] for r in known),
                    'output_tokens':None if unknown else sum(r['usage']['output_tokens'] for r in known),
                    'generation_s':None if unknown else sum(r['timing']['generation_ms'] for r in known)/1000,
                    'reported_usage':{'input_tokens':sum(r['usage']['input_tokens'] for r in known),
                        'output_tokens':sum(r['usage']['output_tokens'] for r in known)}}
                report['budget_measurements']={
                    'requests':len(budgets),
                    'completed':sum(r['status']=='completed' for r in budgets),
                    'failed':sum(r['status']=='failed' for r in budgets),
                    'service_wall_s':sum(r.get('service_wall_ms',0) for r in budgets)/1000,
                    'includes_initial_model_load':not hosted,
                    'generation_calls':0,'output_tokens':0,
                    'measurements':[{'sequence':r['measurement'],'status':r['status'],
                        'input_tokens':r.get('worker_measurement',{}).get('input_tokens'),
                        'service_wall_ms':r.get('service_wall_ms'),
                        'error':r.get('error')} for r in budgets]}
                if hosted:
                    report['api_cost']=deepcopy(provider.cost_report())
                if not report['provider_closed']:report.update(status='blocked',reason='Provider cleanup was not verified')
    try:
        if not isinstance(config,dict):report['config_path']=str(selected_path(config))
        if request is None:request=question('What outcome would you like? ')
        if not isinstance(request,str) or not request.strip():
            report.update(status='canceled',reason='No request supplied');return report
        report['request']=request
        cfg=lib._config(config);require(cfg,('driver_binary','driver_socket') if hosted else ('runtime_python','model_cache','driver_binary','driver_socket'))
        _dependencies()
        label=(provider_name+' '+model if hosted else {'comparator':'7B comparator','baseline':'1.5B baseline','qwen38':'Qwen3.8-27B experimental comparator'}[model])
        if thinking:label+=' · bounded thinking (2048 total output tokens)'
        if hosted:
            from .provider_selection import validate_selection
            validate_selection(provider_name,model)
        from .desktop_session_lock import acquire_desktop_session
        lease = None
        try:
            with acquire_desktop_session(purpose='Locua Amplifier desktop session') as lease:
                report['desktop_control_lease']={'status':'acquired','scope':'per-user desktop','released':False}
                progress(('HOSTED preview · ' if hosted else 'Local ')+label+' · Amplifier standard tool loop · tools-v6.9 / providers-v7.0 · ordinary tool calling. RLCD is not used in this run.')
                asyncio.run(execute(cfg))
        finally:
            if lease is not None:
                report['desktop_control_lease']['released']=lease.closed
    except KeyboardInterrupt:report.update(status='canceled',reason='review_input_ended' if report.get('input_ended') else 'user_interrupt')
    except Exception as error:
        report.update(status='blocked',reason=type(error).__name__+': '+str(error))
        if isinstance(error,LocuaError):report.update(error=error.as_dict(),remedy=error.remedy)
    finally:
        report['full_workflow_wall_s']=time.monotonic()-started
        report['wall_excluding_human_s']=report['full_workflow_wall_s']-report['human_wait_s']
        private_json(root/'summary.json',report)
        if report['status'] not in ('complete','verified_reviewed_scope'):progress('INCOMPLETE: '+str(report.get('reason',report['status'])))
        if report.get('metrics'):
            m=report['metrics']
            if m['unknown_generation_usage']:
                u=m['reported_usage'];progress(f"Model: {m['provider_responses']} returned responses; interrupted generation usage unknown. Reported tokens: {u['input_tokens']} input / {u['output_tokens']} output.")
            else:progress(f"Model: {m['model_calls']} calls, {m['input_tokens']} input / {m['output_tokens']} output tokens.")
        if report.get('api_cost'):
            cost=report['api_cost']
            progress(f"API estimate for this run: ${cost['per_run_known_charge_upper_usd']:.4f} conservative upper bound; "
                f"shared budget ${cost['charged_upper_bound_usd']:.4f} / ${cost['cap_usd']:.2f}. "
                f"Unknown charge reservations: {cost['per_run_unknown_reservation_count']} this run; "
                f"{cost['unknown_reservations']} shared. Details are in the private summary.")
        progress(f"Elapsed excluding input/review: {report['wall_excluding_human_s']:.2f}s. Results: {root/'summary.json'}")
    return report
