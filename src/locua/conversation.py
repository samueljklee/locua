"""Ordinary-language entry, observed interpretation, review and guarded execution.

This module orchestrates existing boundaries; it never manufactures task goals,
chooses fields for the model, or upgrades an evidence plane.
"""
from copy import deepcopy
import json
from pathlib import Path
import time

from .errors import LocuaError


def render_review(plan, observation, *, label, model, inspection_policy, click_route):
    from .engine.prototype.core import matching
    lines = ['Review the interpreted request', 'Request: ' + plan['request'], 'Target: ' + label,
             'Interpretation: local ordinary-generation planner; verify that this covers your whole request.',
             'Action model: ' + ('experimental local 7B comparator' if model == 'comparator' else 'original local 1.5B baseline') + '; original RLCD',
             'Inspection: ' + inspection_policy]
    if plan['scope']['kind'] == 'browser':
        lines.append('Browser clicks: ' + ('explicit synthetic DOM events' if click_route == 'dom_event' else 'trusted') + '; no fallback.')
    for item in plan['outcomes']:
        subject = item['subject']
        name = json.dumps(subject, ensure_ascii=False)
        controls = matching(observation, subject)
        before = None
        if len(controls) == 1:
            c = controls[0]
            before = c.get('value') if item['property'] == 'value' else c.get('states', {}).get(item['property'])
        lines.extend(['Change: ' + name,
                      '  From: ' + (json.dumps(before, ensure_ascii=False) if len(controls) == 1 else 'not uniquely observed'),
                      '  To:   ' + json.dumps(item.get('value', item.get('postcondition')), ensure_ascii=False),
                      '  Verify: ' + item['evidence_plane']])
    for item in plan['constraints']:
        lines.append('Preserve: ' + json.dumps(item['subject'], ensure_ascii=False) + ' = ' + json.dumps(item['value'], ensure_ascii=False))
    if not plan['constraints']:
        lines.append('Preservation: no explicit preservation predicates were interpreted. Only the listed changes are authorized.')
    lines.append('Fresh driver observations verify declared outcomes; displayed/editor values do not prove saved files.')
    return '\n'.join(lines)


def summarize_usage(report):
    """Separate ordinary generation from unchanged RLCD; count reported tokens only."""
    planning = report.get('planning_attempts', [])
    routing = report.get('target_attempts', [])
    execution = report.get('execution', {}).get('model_usage') or {}
    attempted = [p for p in planning if p.get('planning_calls_started', p.get('generation_calls'))]
    input_counts = [p.get('usage', {}).get('input_tokens') for p in attempted]
    output_counts = [p.get('usage', {}).get('output_tokens') for p in attempted]
    route_counts = []
    for row in routing:
        stages = row.get('decision', {}).get('stages', [])
        values = [s.get('full_input_tokens') for s in stages]
        if row.get('routing_calls_started', bool(row.get('decision'))):
            complete = row.get('routing_calls_started', 0) == row.get('routing_calls_completed', 0)
            route_counts.append(sum(values) if complete and values and all(type(v) is int for v in values) else None)
    def total(values):
        return sum(values) if all(type(v) is int for v in values) else None
    return {'planning_calls_started': sum(p.get('planning_calls_started', p.get('generation_calls') or 0) for p in planning),
            'planning_calls_completed': sum(p.get('planning_calls_completed', bool(p.get('usage'))) for p in planning),
            'planning_generation_calls': total([p.get('generation_calls', 0) for p in planning]),
            'planning_input_tokens': total(input_counts), 'planning_output_tokens': total(output_counts),
            'routing_rlcd_calls_started': sum(r.get('routing_calls_started', bool(r.get('decision'))) for r in routing),
            'routing_rlcd_calls_completed': sum(r.get('routing_calls_completed', bool(r.get('decision'))) for r in routing),
            'routing_full_input_tokens': total(route_counts),
            'execution_rlcd': execution,
            'token_note': 'Tokenizer-reported counts only; RLCD full_input_tokens counts each reported stage input. Missing telemetry is not estimated.'}


def run(request=None, *, url=None, document=None, model='comparator', browser_click_route='trusted',
        native_save_route='menu', inspection_policy='reviewed_target_first', out=None,
        config=None, ask, progress):
    from . import lib
    from .config import selected_path
    from .engine_adapter import artifact_directory
    from .engine.prototype.cli import private_json
    from .engine.prototype.planning_contracts import validate_plan
    from .guided import catalog
    from .language_planning import choose_target, interpret

    started = time.monotonic()
    root = artifact_directory(out, 'do')
    report = {'status': 'starting', 'artifacts': str(root), 'request': request,
              'interpretation': 'local_observed_language_planner', 'model': model,
              'selector_decoding': 'original RLCD', 'inspection_policy': inspection_policy,
              'inference_local_only': True, 'saved_output_proven': False,
              'human_interactions': [], 'planning_attempts': [], 'target_attempts': [],
              'human_wait_s': 0.0, 'task_actions_started': False,
              'natural_language_autonomy_proven': False}
    prepared = None

    def question(prompt, kind):
        tick = time.monotonic()
        try:
            answer = ask(prompt)
            report['human_interactions'].append({'kind': kind, 'prompt': prompt, 'answer': answer})
            return answer
        finally:
            report['human_wait_s'] += time.monotonic() - tick

    try:
        if not isinstance(config, dict):
            report['config_path'] = str(selected_path(config))
        if request is None:
            request = question('What outcome would you like? ', 'request')
        if not isinstance(request, str) or not request.strip():
            report.update(status='canceled', reason='no_request')
            return report
        report['request'] = request
        runtime_config = lib._config(config)
        progress('Preview model: ' + ('local 7B comparator' if model == 'comparator' else 'local 1.5B baseline') + '. All inference stays local; action selection uses original RLCD.')
        if url or document:
            report.setdefault('target_source', 'explicit_caller_target')
            progress('Reading the requested target…')
            prepared = lib._engine('prepare_guided', {'url': url, 'document': str(document) if document else None,
                'target': None, 'browser_click_route': browser_click_route, 'out': str(root/'discovery')},
                config, progress)['result']
        else:
            listed = lib.targets(config=config, out=root/'target-inventory', progress=progress)['result']['targets']['windows']
            windows = listed
            if not windows:
                raise LocuaError('no_open_target', 'No application windows were observed.',
                                 'Open the application you want to control, or use --url URL. Application launching is not implemented.', exit_code=4)
            private_json(root/'windows.json', windows)
            for turn in range(3):
                progress('Matching your request to observed application windows…')
                routed = choose_target(request, windows, model=model, runtime_config=runtime_config,
                                       out=root/f'target-{turn+1:02d}', progress=progress)
                report['target_attempts'].append(routed)
                if routed.get('status') == 'selected':
                    break
                if routed.get('status') != 'clarification' or turn == 2:
                    report.update(status='blocked', reason=routed.get('reason', 'target_not_resolved'))
                    return report
                progress('Observed windows: ' + '; '.join((w.get('app_name') or w.get('owner_name') or 'App') + ': ' + (w.get('title') or '(untitled)') for w in windows))
                answer = question('Which application/window should this affect? Describe its name or title (Enter to stop): ', 'target_clarification')
                if not answer.strip():
                    report.update(status='canceled', reason='target_clarification_canceled')
                    return report
                request += '\nTarget clarification: ' + answer
            report['target_source'] = 'local_model_from_observed_windows'
            # Guard the routing boundary independently of model-wrapper validity.
            target = routed['target']
            if not any(w.get('pid') == target.get('pid') and w.get('window_id') == target.get('window_id') for w in windows):
                raise ValueError('Target router returned an unobserved window')
            progress('Selected: ' + routed['target_label'])
            prepared = lib._engine('prepare_guided', {'url': None, 'document': None, 'target': target,
                'browser_click_route': browser_click_route, 'out': str(root/'discovery')}, config, progress)['result']
            prepared['target_label'] = routed['target_label']
        observation = prepared['observation']
        fields = catalog(observation)
        private_json(root/'fields.json', fields)
        report['target_label'] = prepared['target_label']
        progress('Observed: ' + prepared['target_label'] + '. Interpreting your request against its current controls…')
        for turn in range(3):
            proposal = interpret(request, observation, fields, prepared['scope'], model=model,
                                 runtime_config=runtime_config, out=root/f'planning-{turn+1:02d}', progress=progress)
            report['planning_attempts'].append(proposal)
            if proposal.get('status') == 'proposed':
                break
            if proposal.get('status') != 'clarification' or turn == 2:
                report.update(status='blocked', reason=proposal.get('reason', 'language_interpretation_blocked'),
                              remedy='This request was not executed. Inspect the planner result; use the optional manual mode only if you choose to supply the interpretation yourself.')
                return report
            questions = proposal.get('questions') or ['What exact outcome or missing value should be used?']
            progress('Clarification needed: ' + '; '.join(str(q) for q in questions))
            answer = question('Your clarification (Enter to stop): ', 'intent_clarification')
            if not answer.strip():
                report.update(status='canceled', reason='intent_clarification_canceled')
                return report
            request += '\nClarification: ' + answer
        plan = deepcopy(proposal['plan'])
        data = deepcopy(proposal.get('validation_data', {}))
        validate_plan(plan, supplied_data=data)
        if plan['scope'] != prepared['scope'] or plan['request'] != request or plan['unknowns']:
            raise ValueError('Planner must retain the exact request, observed scope and resolve its unknowns')
        private_json(root/'plan.json', plan)
        if any(o['evidence_plane'] in ('committed_document', 'saved_output') for o in plan['outcomes']):
            report.update(status='blocked', reason='required_document_or_saved_evidence_not_supported',
                          remedy='The requested persistence requirement is retained. This language workflow cannot yet prove native saving; no task edits were made.')
            return report
        review = render_review(plan, observation, label=prepared['target_label'], model=model,
                               inspection_policy=inspection_policy, click_route=browser_click_route)
        review_file = root/'review.txt'
        review_file.write_text(review+'\n', encoding='utf-8'); review_file.chmod(0o600)
        progress('\n'+review)
        accepted = question('Type run to approve this whole plan, or Enter to cancel: ', 'plan_review').strip().lower() == 'run'
        private_json(root/'review.json', {'accepted': accepted, 'plan': plan, 'source': 'local_language_interpretation',
                                        'model': model, 'inspection_policy': inspection_policy})
        if not accepted:
            report.update(status='canceled', reason='review_not_approved')
            return report
        report['execution_started'] = True
        progress('Approved. Rechecking the target and executing guarded changes…')
        execution = lib.run(task=plan, supplied_data=data, model=model, execute=True,
                            browser_click_route=browser_click_route, inspection_policy=inspection_policy,
                            out=root/'execution', config=config, progress=progress)['result']
        report['execution'] = execution
        report['task_actions_started'] = execution.get('issued_actions', 0) > 0
        report.update(status=execution['status'], reason=execution.get('reason'),
                      execution_artifacts=execution['artifacts'], execution_wall_s=execution.get('end_to_end_wall_s'))
        state_path = Path(execution['artifacts'])/'task-state.json'
        report['outcome_results'] = json.loads(state_path.read_text()).get('outcomes', {}) if state_path.is_file() else {}
        for identity, status in report['outcome_results'].items():
            progress(str(status).upper()+': '+identity)
        if report['status'] == 'complete':
            progress('VERIFIED: all reviewed outcomes and preservation predicates matched fresh observations. Saving is not claimed.')
        return report
    except KeyboardInterrupt:
        report.update(status='canceled', reason='user_interrupt')
        return report
    except Exception as error:
        report.update(status='blocked', reason=str(error))
        if isinstance(error, LocuaError):
            report.update(error=error.as_dict(), remedy=error.remedy)
        return report
    finally:
        if prepared and prepared.get('document_binding_id'):
            from .document_open import release_prepared
            release_prepared(prepared)
            report['document_window_policy'] = 'left_open_for_user'
        report['full_workflow_wall_s'] = time.monotonic()-started
        report['wall_excluding_human_s'] = report['full_workflow_wall_s']-report['human_wait_s']
        report['human_assistance_count'] = sum(i['kind'] in ('target_clarification', 'intent_clarification') for i in report['human_interactions'])
        report['model_usage'] = summarize_usage(report)
        private_json(root/'summary.json', report)
        if report['status'] not in ('complete', 'starting'):
            progress('STOPPED: '+str(report.get('reason', report['status'])))
        progress(f"Elapsed excluding input/review: {report['wall_excluding_human_s']:.2f}s. Results: {root/'summary.json'}")
