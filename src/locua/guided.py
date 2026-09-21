"""Guided, observed task composition. User choices are authority; no NL inference."""
from copy import deepcopy
import json
from pathlib import Path
import time

from .errors import LocuaError


def catalog(observation):
    """List all supported visible fields; never select one from an expected answer."""
    from .engine.prototype.core import matching, action_available
    from .engine.prototype.perception import native_set_value_eligible
    controls = observation['controls']
    by_id = {c['id']: c for c in controls}
    fields, unavailable = [], []
    for c in controls:
        role = c['role']
        text = role in ('textbox', 'searchbox', 'AXTextArea', 'AXTextField', 'AXSearchField', 'AXComboBox')
        check = role in ('checkbox', 'AXCheckBox')
        if not text and not check:
            continue
        ancestors, seen = [], {c['id']}
        parent = c.get('parent')
        while parent in by_id and parent not in seen:
            seen.add(parent); a = by_id[parent]; ancestors.append(a); parent = a.get('parent')
        named = [a for a in ancestors if isinstance(a.get('name'), str) and a['name']]
        base = {'role': role}
        # Native textarea names may be their mutable contents, not stable identities.
        if c.get('name') and role != 'AXTextArea':
            base['name'] = c['name']
        attempts = [{**base, 'ancestor': {'name': a['name'], 'role': a['role']}} for a in named]
        if 'name' in base:
            attempts.append(base)
        selector = next((s for s in attempts if len(matching(observation, s)) == 1), None)
        label_parts = [a['name'] for a in reversed(named) if a['role'] not in ('rootwebarea',)]
        label = ' > '.join([*label_parts, 'Document text' if role == 'AXTextArea' else c.get('name') or role])
        reason = None
        handle = observation.get('handles', {}).get(c['id'])
        states = c.get('states', {})
        if not selector:
            reason = 'no unique stable description'
        elif not handle or states.get('disabled') is True or states.get('enabled') is False:
            reason = 'not currently writable/addressable'
        elif text:
            if c.get('value_evidence', {}).get('exact_value_proven') is not True or not isinstance(c.get('value'), str):
                reason = 'exact editor value unavailable'
            elif handle['kind'] == 'native' and native_set_value_eligible(c) is not True:
                reason = 'native writability not proved'
            elif handle['kind'] == 'browser' and 'type' not in c.get('actions', []):
                reason = 'text input not supported'
        elif type(states.get('checked')) is not bool:
            reason = 'checked state unknown'
        if reason is None and handle['kind'] == 'browser' and not action_available(observation, c, 'set_text' if text else 'press'):
            reason = 'current visibility has no supported action route; reveal the control before starting'
        if reason:
            unavailable.append({'label': label, 'reason': reason}); continue
        fields.append({'number': len(fields)+1, 'label': label, 'subject': selector,
                       'property': 'value' if text else 'checked',
                       'value': c['value'] if text else states['checked'],
                       'access_note': ('Near viewport: the semantic reference route may reveal/scroll this control.'
                                       if c.get('source', {}).get('node', {}).get('visibility') == 'near_viewport' else None),
                       'control_id': c['id'], 'snapshot_id': observation['snapshot_id']})
    return {'fields': fields, 'unavailable': unavailable,
            'mode_limit': 'Observed text fields and checkboxes with supported action routes; hidden navigation and other actions are not authored by this guided mode.'}


def compose(scope, fields, changes, *, preserve=True):
    """Translate explicit field/value choices into the existing guarded contract."""
    from .engine.prototype.planning_contracts import validate_plan
    if not changes or len(changes) > 16:
        raise ValueError('Choose between 1 and 16 changes')
    lookup = {f['number']: f for f in fields}
    if any(type(n) is not int or n not in lookup for n in changes):
        raise ValueError('Unknown field choice')
    lines, outcomes, constraints = [], [], []
    for number, value in changes.items():
        f = lookup[number]
        if (f['property'] == 'checked' and type(value) is not bool) or (f['property'] == 'value' and not isinstance(value, str)):
            raise ValueError('Wrong value type for selected field')
        line = 'Set ' + f['label'] + ' to exact value: ' + (value if isinstance(value, str) else json.dumps(value)) + '\n'
        lines.append(line)
        outcomes.append({'id': 'change-'+str(number), 'subject': deepcopy(f['subject']), 'property': f['property'],
                         'value': value, 'requires': [], 'evidence_plane': 'editor_buffer' if f['property']=='value' else 'display',
                         'source_text': line})
    if preserve:
        for f in fields:
            if f['number'] in changes:
                continue
            line = 'Preserve ' + f['label'] + ' as exact value: ' + (f['value'] if isinstance(f['value'], str) else json.dumps(f['value'])) + '\n'
            lines.append(line)
            constraints.append({'subject': deepcopy(f['subject']), 'property': f['property'], 'value': f['value'], 'source_text': line})
    if len(constraints) > 16:
        raise ValueError('More than 16 preservation constraints; narrow the target instead of silently dropping fields')
    plan = {'version': 'locua-task-plan-v1', 'request': '\n'.join(lines), 'scope': deepcopy(scope),
            'outcomes': outcomes, 'constraints': constraints, 'unknowns': []}
    validate_plan(plan)
    return plan


def review_text(plan, fields, changes, model, *, save_path=None, target_label=None, native_save_route="menu", browser_click_route="trusted"):
    lookup = {f['number']: f for f in fields}
    lines = ['Review this task', 'Target: '+(target_label or json.dumps(plan['scope'], ensure_ascii=False)),
             'Model: '+('original 1.5B RLCD baseline' if model=='baseline' else 'experimental 7B comparator; original RLCD'),
             'Interpretation: your guided choices (no automatic language planning)']
    if plan['scope']['kind'] == 'browser':
        lines.append('Browser input: '+('synthetic DOM clicks' if browser_click_route == 'dom_event' else 'trusted clicks')+' and semantic reference typing; no route fallback.')
    for number, value in changes.items():
        f=lookup[number]
        lines.extend(['Change: '+f['label'], '  From: '+json.dumps(f['value'], ensure_ascii=False),
                      '  To:   '+json.dumps(value, ensure_ascii=False)])
        if f.get('access_note'):
            lines.append('  Access: '+f['access_note'])
    for c in plan['constraints']:
        label = next(f['label'] for f in fields if f['subject']==c['subject'])
        lines.append('Preserve: '+label+' = '+json.dumps(c['value'], ensure_ascii=False))
    lines.append('Verify: fresh exact editor values and checked states after actions.')
    if save_path:
        lines.append('Save: existing TextEdit plain-text document '+str(save_path)+'; independently verify exact UTF-8 file bytes.')
        lines.append('Save method: '+('guarded TextEdit Command-S (temporary foreground input)' if native_save_route=='textedit_shortcut' else 'observed native menu')+'; no automatic route fallback.')
    else:
        lines.append('Persistence: editor state only; no saved-file claim.')
    lines.append('Only these changes are authorized. Other actions remain visible to the model and guarded.')
    return '\n'.join(lines)


def _value(answer, field):
    if field['property']=='checked':
        if answer.strip().lower() not in ('on','off','true','false','yes','no'):
            raise ValueError('Enter on or off')
        return answer.strip().lower() in ('on','true','yes')
    if answer.startswith('@@'):
        return answer[1:]
    if answer.startswith('@'):
        return Path(answer[1:]).expanduser().read_bytes().decode('utf-8')
    return answer


def _window_status(window):
    value = window.get('is_on_screen')
    status = 'on screen' if value is True else 'not on screen' if value is False else 'visibility unknown'
    if window.get('on_current_space') is False:
        status += '; another desktop'
    return status


def _discover_native(config, root, ask, progress, report, browser_click_route):
    """Explicit recovery before review; never focus, rebind, or retry an action."""
    from . import lib
    from .errors import LocuaError
    target = None
    label = None
    attempts = report.setdefault('discovery_attempts', [])
    for attempt in range(1, 4):
        if target is None:
            listed = lib.targets(config=config, progress=progress)['result']['targets']['windows']
            windows = [w for w in listed if w.get('title')]
            if not windows:
                raise ValueError('No titled native windows observed; use --url or --document')
            progress('Choose a native window (read access is checked after selection):')
            for n, w in enumerate(windows, 1):
                progress(f"{n}. {w.get('app_name', w.get('owner_name', 'App'))}: {w['title']} [{_window_status(w)}] (process {w.get('pid')}, window {w.get('window_id')})")
            answer = ask('Window number (Enter to cancel): ').strip()
            if not answer:
                return None
            if not answer.isdecimal() or not 1 <= int(answer) <= len(windows):
                raise ValueError('Choose one listed window number')
            w = windows[int(answer) - 1]
            target = {'pid': w['pid'], 'window_id': w['window_id']}
            label = w.get('app_name', w.get('owner_name', 'App')) + ': ' + w['title']
        directory = root / ('discovery' if attempt == 1 else f'discovery-{attempt:02d}')
        entry = {'target': dict(target), 'label': label, 'artifacts': str(directory)}
        attempts.append(entry)
        progress('Reading ' + label + '…')
        try:
            prepared = lib._engine('prepare_guided', {'url': None, 'document': None, 'target': target,
                                  'browser_click_route': browser_click_route, 'out': str(directory)},
                                  config, progress)['result']
            entry['status'] = 'observed'
            prepared['target_label'] = label
            return prepared
        except LocuaError as error:
            entry.update(status='unavailable', error=error.as_dict())
            if error.code not in ('native_accessibility_unavailable', 'native_observation_degraded'):
                raise
            progress('Unavailable: ' + label + '. ' + error.message)
            progress('Next step: ' + error.remedy)
            if attempt == 3:
                progress('Stopped after three discovery attempts; no task actions were taken.')
                raise
            while True:
                answer = ask('Type retry to read this window again, choose for another window, or Enter to stop: ').strip().lower()
                if answer in ('retry', 'choose', ''):
                    break
                progress('Enter retry, choose, or leave the answer empty.')
            if not answer:
                raise
            if answer == 'choose':
                target = None


def start(*, url=None, document=None, model='baseline', browser_click_route='trusted', native_save_route='menu', out=None,
          inspection_policy='reviewed_target_first', config=None, ask, progress):
    """Public workflow. ask(prompt)->string is supplied by CLI or another client."""
    from . import lib
    from .config import selected_path
    from .errors import LocuaError
    from .engine_adapter import artifact_directory
    from .engine.prototype.cli import private_json
    from .engine.prototype.observation_tools import overview
    if model not in ('baseline','comparator') or browser_click_route not in ('trusted','dom_event'):
        raise ValueError('Unsupported explicit model or browser click route')
    if native_save_route not in ('menu','textedit_shortcut') or (native_save_route!='menu' and not document):
        raise ValueError('Explicit TextEdit save route requires --document')
    started=time.monotonic(); root=artifact_directory(out,'start')
    report={'status':'starting','artifacts':str(root),'interpretation':'guided_user_choices',
            'natural_language_autonomy_proven':False,'model':model,'native_save_route':native_save_route if document else None,'saved_output_proven':False}
    prep=None
    try:
        if not isinstance(config, dict):
            report['config_path']=str(selected_path(config))
        if not url and not document:
            prep=_discover_native(config, root, ask, progress, report, browser_click_route)
            if prep is None:
                report.update(status='canceled', reason='window_selection_canceled')
                return report
        else:
            prep=lib._engine('prepare_guided',{'url':url,'document':str(document) if document else None,'target':None,
                           'browser_click_route': browser_click_route,
                           'out':str(root/'discovery')},config,progress)['result']
        observation=prep['observation']; fields=catalog(observation)
        private_json(root/'fields.json',fields)
        progress('Observed target: '+prep['target_label'])
        regions=overview(observation)
        progress('Available UI regions: '+', '.join(r.get('label',r.get('name',r.get('id','region'))) for r in regions.get('items',[])))
        progress(fields['mode_limit'])
        for f in fields['fields']:
            progress(f"{f['number']}. {f['label']} = {json.dumps(f['value'],ensure_ascii=False)}")
            if f.get('access_note'):
                progress('   '+f['access_note'])
        for f in fields['unavailable']:
            progress('Unavailable: '+f['label']+' — '+f['reason'])
        if not fields['fields']:
            raise ValueError('No exact writable fields available in this target')
        changes={}
        while True:
            choice=ask('Field number to change (Enter when finished): ').strip()
            if not choice:
                break
            if not choice.isdecimal() or not 1<=int(choice)<=len(fields['fields']):
                progress('Choose a listed field number.');continue
            f=fields['fields'][int(choice)-1]
            answer=ask('New value (on/off): ' if f['property']=='checked' else 'Exact new text (or @/path/to/UTF-8-file; @@ for literal @): ')
            changes[f['number']]=_value(answer,f)
        if not changes:
            report.update(status='canceled',reason='no_changes_selected');return report
        if document:
            bound = [f for f in fields['fields'] if f['control_id']==prep.get('document_editor_id')]
            if len(bound)!=1 or set(changes)!={bound[0]['number']}:
                raise ValueError('Document saving requires changing only the bound Document text field; no edits were executed')
        plan=compose(prep['scope'],fields['fields'],changes,preserve=True)
        review=review_text(plan,fields['fields'],changes,model,save_path=document,target_label=prep['target_label'],native_save_route=native_save_route,browser_click_route=browser_click_route)
        private_json(root/'plan.json',plan)
        (root/'review.txt').write_text(review+'\n',encoding='utf-8');(root/'review.txt').chmod(0o600)
        progress('\n'+review)
        accepted=ask('Type run to approve and execute, or Enter to cancel: ').strip().lower()=='run'
        private_json(root/'review.json',{'accepted':accepted,'plan':plan,'source':'guided_user_choices','model':model,
                                        'browser_click_route':browser_click_route,'native_save_route':native_save_route if document else None})
        if not accepted:
            report.update(status='canceled',reason='review_not_approved');return report
        progress('Approved. Reading the target again and starting local execution.')
        result=lib.run(task=plan,model=model,execute=True,browser_click_route=browser_click_route,
                       inspection_policy=inspection_policy,
                       out=root/'execution',config=config,progress=progress)
        report.update(result['result']);report['execution_artifacts']=report['artifacts'];report['artifacts']=str(root)
        state_path=Path(report['execution_artifacts'])/'task-state.json'
        states=json.loads(state_path.read_text()).get('outcomes',{}) if state_path.is_file() else {}
        report['field_results']=[{'label':fields['fields'][n-1]['label'],'requested':v,
                                 'status':states.get('change-'+str(n),'unverified')}
                                for n,v in changes.items()]
        if document and report['status']=='complete':
            from .document_open import save_prepared
            expected=[value for number,value in changes.items() if fields['fields'][number-1]['property']=='value']
            if len(expected)!=1:
                raise ValueError('Plain-text saving requires exactly one reviewed document text change')
            saved=save_prepared(prep,expected[0],config,root/'save',progress,save_route=native_save_route)
            report['save']=saved;report['saved_output_proven']=saved.get('saved_output_proven') is True
            report['explicit_save_proven']=saved.get('explicit_save_proven') is True
            if not report['saved_output_proven'] or not report['explicit_save_proven']:
                report.update(status='blocked',reason='explicit_native_save_not_verified')
        if report['status']=='complete':
            progress('VERIFIED: '+str(len(changes))+' requested changes; '+str(len(plan['constraints']))+' preserved fields.')
            progress('Saved file verified: '+str(document) if report['saved_output_proven'] else 'Evidence: editor state verified; saving not claimed.')
        else:
            reason=report.get('reason',report['status'])
            readable={'false_model_completion':'The model tried to finish while requested changes were still pending.',
                      'inspection_or_action_abstained':'The model could not choose a supported next step.'}.get(reason,str(reason))
            progress('STOPPED: '+readable)
            if reason == 'false_model_completion':
                report['remedy'] = ('The model claimed completion without matching verified task state. Review the verified and pending results before starting a new task. '
                                    'The optional --model comparator selects the experimental local 7B model explicitly; it is not a guaranteed fix.')
        for item in report['field_results']:
            progress(item['status'].upper()+': '+item['label'])
        return report
    except KeyboardInterrupt:
        report.update(status='canceled',reason='user_interrupt');return report
    except Exception as error:
        report.update(status='blocked',reason=str(error))
        if isinstance(error, LocuaError):
            report['error']=error.as_dict()
            report['remedy']=error.remedy
        progress('STOPPED: '+str(error))
        return report
    finally:
        if prep and prep.get('document_binding_id'):
            from .document_open import release_prepared
            release_prepared(prep)
            report['document_window_policy']='left_open_for_user'
        report['full_workflow_wall_s']=time.monotonic()-started
        private_json(root/'summary.json',report)
        progress(f"Total elapsed: {report['full_workflow_wall_s']:.2f}s (includes review time). Results: {root/'summary.json'}")
