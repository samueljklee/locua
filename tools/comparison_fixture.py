"""Disposable test setup only. Never imported by the generic Locua decision loop.

Uses the same guarded desktop tools, records setup separately, and has no model.
It restores the declared test buffer or observed calculator mode/clear state.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
from locua.amplifier_tools import DesktopToolset
from locua.arithmetic_input import symbol
from locua.desktop_session_lock import acquire_desktop_session
from locua.engine.prototype.cli import private_json
from locua.lib import _config

TITLE='Locua-v10-transfer-draft.txt'
INITIAL='Using Draft: local automation trial.\n'

def setup(out,case,config=None):
    root=Path(out);root.mkdir(parents=True,mode=0o700,exist_ok=False)
    started=time.monotonic();report={'case':case,'kind':'authorized disposable fixture setup; no model decision','model_calls':0,'review_assistance':[]}
    def approve(prompt,purpose):
        report['review_assistance'].append({'purpose':purpose,'answer':'run','actor':'authorized setup runner','prompt':prompt})
        return 'run'
    with acquire_desktop_session(purpose='Locua disposable comparison fixture setup'):
        tools=DesktopToolset(_config(config),root/'desktop','Reset only the authorized disposable comparison fixture.',approve)
        def call(name,**args):
            result=tools.call('locua_'+name,args)
            if result.get('status') in ('refused','unavailable','uncertain','canceled'):
                raise RuntimeError(name+': '+str(result.get('reason',result.get('code'))))
            return result
        try:
            app='TextEdit' if case=='textedit' else 'Calculator'
            apps=call('apps',query=app)['items']
            if len(apps)!=1:raise RuntimeError('Fixture app identity ambiguous')
            aid=apps[0]['app_id'];windows=call('windows',app_id=aid)['windows']
            title=TITLE if case=='textedit' else 'Calculator'
            matches=[w for w in windows if w.get('title')==title and w.get('identity_proven')]
            if len(matches)!=1:raise RuntimeError('Exact disposable fixture window unavailable/ambiguous')
            wid=matches[0]['window_id'];report['target']=matches[0]
            call('activate',window_id=wid);seen=call('observe',window_id=wid)
            def state():return tools._observations[seen['snapshot_id']]
            def actions(cid):return [a for a in tools._actions[seen['snapshot_id']].values() if a['control_id']==cid]
            def press(control,purpose):
                nonlocal seen
                candidates=[a for a in actions(control['id']) if a['kind']=='press']
                if len(candidates)!=1:raise RuntimeError('Fixture press route ambiguous/unavailable')
                scope=call('review',snapshot_id=seen['snapshot_id'],summary=purpose,goals=[],effects=[{'kind':'press','control_id':control['id'],'purpose':purpose}],covers_entire_request=False)
                seen=call('act',scope_id=scope['scope_id'],snapshot_id=seen['snapshot_id'],action_id=candidates[0]['id'])
            if case=='textedit':
                editors=[c for c in state()['controls'] if c.get('role')=='AXTextArea' and any(a['kind']=='set_text' for a in actions(c['id']))]
                if len(editors)!=1:raise RuntimeError('Unique editable fixture buffer unavailable')
                c=editors[0];report['before_buffer']=c.get('value')
                if c.get('value')!=INITIAL:
                    action=next(a for a in actions(c['id']) if a['kind']=='set_text')
                    scope=call('review',snapshot_id=seen['snapshot_id'],summary='Restore the declared disposable buffer before comparison; do not save.',
                        goals=[{'id':'fixture','kind':'text','target':'disposable editor buffer','control_id':c['id'],'value':INITIAL,'evidence_plane':'editor_buffer'}],
                        effects=[{'kind':'goal','goal_id':'fixture'}],covers_entire_request=True)
                    seen=call('act',scope_id=scope['scope_id'],snapshot_id=seen['snapshot_id'],action_id=action['id'],value=INITIAL)
                    proof=call('verify',scope_id=scope['scope_id'])
                    if proof['status']!='verified':raise RuntimeError('Fixture buffer reset unverified')
                # Always recapture independently of an action acknowledgement.
                seen=call('observe',window_id=wid)
                rows=[c for c in state()['controls'] if c.get('role')=='AXTextArea']
                if len(rows)!=1 or rows[0].get('value')!=INITIAL or rows[0].get('value_evidence',{}).get('exact_value_proven')is not True:
                    raise RuntimeError('Fixture exact buffer precondition not established')
                report['verified_initial_buffer']=INITIAL
            else:
                # The frozen original state has24 total buttons (22 named). Do not issue
                # unnecessary app-menu commands whose window ownership is unknown.
                # A changed layout is a setup failure, never silently a new fixture.
                if sum(c.get('role')=='AXButton' for c in state()['controls'])!=24:
                    raise RuntimeError('Fixture layout differs from the original24-button state; reset requires separate setup diagnosis')
                # A failed trial can leave an unfinished entry. Entry-clear is
                # setup only and never substitutes for the full reset below.
                full=[c for c in state()['controls'] if symbol(c)=='clear' and any(a['kind']=='press' for a in actions(c['id']))]
                if not full:
                    entry=[c for c in state()['controls'] if symbol(c)=='clear_entry' and any(a['kind']=='press' for a in actions(c['id']))]
                    if len(entry)!=1:raise RuntimeError('Unique disposable entry-clear capability unavailable')
                    report['entry_clear_before_full_reset']=True
                    press(entry[0],'Clear the unfinished disposable entry; a full calculation reset is still required')
                for _ in range(2):
                    clear=[c for c in state()['controls'] if symbol(c)=='clear' and any(a['kind']=='press' for a in actions(c['id']))]
                    if len(clear)!=1:raise RuntimeError('Unique whole-calculation clear capability unavailable')
                    press(clear[0],'Clear only the disposable arithmetic test state')
                seen=call('observe',window_id=wid)
                values=[c.get('value') for c in state()['controls'] if c.get('role')=='AXStaticText']
                clean=[v.replace('\u200e','').replace('\u200f','').strip() for v in values if isinstance(v,str)]
                if clean!=['0']:raise RuntimeError('Fixture zero display precondition not established')
                report['verified_initial_readouts']=values
                report['button_count']=sum(c.get('role')=='AXButton' for c in state()['controls'])
            report.update(status='ready',snapshot_id=seen['snapshot_id'],saved_output_claim=False)
        except BaseException as error:
            report.update(status='failed',error=type(error).__name__+': '+str(error))
            if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
        finally:
            report['cleanup']=tools.close();report['wall_s']=time.monotonic()-started
            private_json(root/'summary.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('review_assistance',)},indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',choices=('calculator','textedit','fresh'),required=True);p.add_argument('--out',required=True);p.add_argument('--config');a=p.parse_args()
    r=setup(a.out,a.case,a.config);raise SystemExit(0 if r['status']=='ready' else 1)
