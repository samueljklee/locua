"""Pure sequence continuity tests; no desktop or inference operations."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from locua.action_sequence_guard import check_transition, remap_control, SequenceGuardError
from test_native_value_label_binding import captured


def observation(snapshot='s1', *, advance=0):
    stamp=time.time_ns()-2_000_000+advance
    target={'pid':41,'window_id':52}
    rows=[('AXWindow','Document','window',None),('AXButton','Run','run',0),
          ('AXButton','Available action','action',0),('AXStaticText',None,None,0),
          ('AXTextField','Other field','other',0)]
    controls=[];handles={}
    for i,(role,name,identifier,parent) in enumerate(rows):
        cid=f'native:{snapshot}:{i}';frame={'x':i*10,'y':0,'w':8,'h':8}
        node={'role':role,'element_index':i,'element_token':f'{snapshot}:{i}', 'frame':frame}
        controls.append({'id':cid,'role':role,'name':name,'value':'0' if i==3 else 'protected' if i==4 else None,
            'parent':f'native:{snapshot}:{parent}' if parent is not None else None,
            'semantics':{'identifier':identifier},'states':{'enabled':True,'focused':False},
            'actions':['AXPress'] if role=='AXButton' else [],
            'bounds':{'x':i*10,'y':0,'width':8,'height':8},'source':{'kind':'native','node':node}})
        handles[cid]={'kind':'native','snapshot_id':snapshot,**target,'element_index':i,'element_token':f'{snapshot}:{i}'}
    return {'kind':'native_window_state','target':target,'snapshot_id':snapshot,'observed_at_ns':stamp,
            'provenance':{'observed_at_ns':stamp},'controls':controls,'handles':handles,'text':'','coverage':{'complete':False}}


def next_capture(before,snapshot='s2'):
    after=deepcopy(before);old=before['snapshot_id'];after['snapshot_id']=snapshot
    after['observed_at_ns']=before['observed_at_ns']+1000
    after['provenance']['observed_at_ns']=after['observed_at_ns'];after['handles']={}
    for i,c in enumerate(after['controls']):
        prior=c['id'];c['id']=prior.replace(old,snapshot)
        if c.get('parent'):c['parent']=c['parent'].replace(old,snapshot)
        c['source']['node']['element_token']=f'{snapshot}:{i}'
        h=deepcopy(before['handles'][prior]);h.update(snapshot_id=snapshot,element_token=f'{snapshot}:{i}')
        after['handles'][c['id']]=h
    return after


class SequenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.before=observation();self.after=next_capture(self.before)
        self.acted=self.before['controls'][1]['id'];self.future=self.before['controls'][2]['id']

    def check(self,**kwargs):
        return check_transition(self.before,self.after,acted_control_id=self.acted,**kwargs)

    def test_same_topology_readout_focus_and_unplanned_leaf_replacement(self):
        self.after['controls'][3]['value']='1'
        self.after['controls'][1]['states']['focused']=True
        self.after['controls'][2]['name']='Replacement action'
        self.after['controls'][2]['semantics']['identifier']='replacement'
        result=self.check();self.assertTrue(result['matched'],result)
        self.assertIn('unplanned_leaf_semantic_replacement',[c['kind'] for c in result['evidence']['changes']])
        self.assertFalse(result['evidence']['same_layout_navigation_excluded'])
        self.assertFalse(result['evidence']['creates_authority'])

    def test_future_identity_signature_or_competitor_change_stops(self):
        variants=[lambda c:c.update(name='other'),lambda c:c['states'].update(enabled=False),
                  lambda c:c.update(value='changed'),lambda c:c['bounds'].update(x=999)]
        for mutate in variants:
            with self.subTest(mutate=mutate):
                self.after=next_capture(self.before);mutate(self.after['controls'][2])
                self.assertFalse(self.check(planned_control_ids=[self.future])['matched'])
                with self.assertRaises(SequenceGuardError):remap_control(self.before,self.future,self.after)

    def test_identity_matching_precedes_value_signature_and_is_unique(self):
        twin=deepcopy(self.after['controls'][2]);twin['id']='native:s2:5';twin['value']='different'
        twin['source']['node'].update(element_index=5,element_token='s2:5')
        self.after['controls'].append(twin)
        self.after['handles'][twin['id']]={**self.after['handles']['native:s2:2'],'element_index':5,'element_token':'s2:5'}
        with self.assertRaisesRegex(SequenceGuardError,'ambiguous'):remap_control(self.before,self.future,self.after)

    def test_dialog_menu_parent_reorder_and_window_changes_stop(self):
        mutations=[lambda o:o['controls'][3].update(role='AXDialog'),
                   lambda o:o['controls'][2].update(parent=o['controls'][1]['id']),
                   lambda o:o['controls'].reverse(),
                   lambda o:o['controls'][0].update(name='Different document'),
                   lambda o:o['controls'].append(deepcopy(o['controls'][3])),
                   lambda o:o['controls'][2].update(role='AXMenu')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.after=next_capture(self.before);mutate(self.after)
                self.assertFalse(self.check()['matched'])

    def test_selected_navigation_changes_refused_even_when_acted(self):
        for role in ('AXTab','AXRadioButton'):
            with self.subTest(role=role):
                self.before['controls'][1]['role']=role;self.before['controls'][1]['source']['node']['role']=role
                self.before['controls'][1]['states']['selected']=False
                self.after=next_capture(self.before);self.after['controls'][1]['states']['selected']=True
                self.assertFalse(self.check()['matched'])

    def test_acted_button_expansion_or_visibility_change_stops_without_new_rows(self):
        for state in ('expanded','visible','hidden','selected'):
            with self.subTest(state=state):
                self.before['controls'][1]['states'][state]=False
                self.after=next_capture(self.before);self.after['controls'][1]['states'][state]=True
                self.assertEqual(self.check()['reason'],'sequence_acted_state_changed')

    def test_unrelated_editor_and_checkbox_state_never_treated_as_readout(self):
        self.after['controls'][4]['value']='changed'
        self.assertEqual(self.check()['reason'],'sequence_unacted_value_changed')
        self.after=next_capture(self.before);self.after['controls'][2]['states']['checked']=True
        self.assertEqual(self.check()['reason'],'sequence_unacted_state_changed')

    def test_acted_state_is_allowed_but_repeated_future_target_is_not(self):
        self.before['controls'][1]['role']='AXCheckBox';self.before['controls'][1]['source']['node']['role']='AXCheckBox'
        self.after=next_capture(self.before)
        self.after['controls'][1]['states']['checked']=True
        self.assertTrue(self.check()['matched'])
        self.assertFalse(self.check(planned_control_ids=[self.acted])['matched'])

    def test_predispatch_accepts_focus_only_not_any_unauthorized_changes(self):
        self.after['controls'][1]['states']['focused']=True
        self.assertTrue(check_transition(self.before,self.after,acted_control_id=None)['matched'])
        for index,key,value in ((3,'value','1'),(2,'name','new'),(1,'value','edited')):
            with self.subTest(index=index,key=key):
                after=deepcopy(self.after);after['controls'][index][key]=value
                self.assertFalse(check_transition(self.before,after,acted_control_id=None)['matched'])

    def test_fresh_post_required_retained_remap_keeps_review_delay_supported(self):
        for o in (self.before,self.after):
            o['observed_at_ns']-=120_000_000_000;o['provenance']['observed_at_ns']=o['observed_at_ns']
        self.assertEqual(remap_control(self.before,self.future,self.after),'native:s2:2')
        self.assertFalse(self.check()['matched'])
        self.assertTrue(check_transition(self.before,self.after,acted_control_id=None)['matched'])

    def test_timestamp_target_snapshot_handles_and_missing_hierarchy_refuse(self):
        mutations=[lambda o:o['target'].update(window_id=99),
                   lambda o:o.update(snapshot_id='s1'),
                   lambda o:o['handles']['native:s2:2'].update(element_token='s1:2'),
                   lambda o:o['controls'][2].update(parent='missing'),
                   lambda o:o.update(observed_at_ns=self.before['observed_at_ns']-1),
                   lambda o:o.update(observed_at_ns=time.time_ns()+10**12)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.after=next_capture(self.before);mutate(self.after)
                self.assertFalse(self.check(planned_control_ids=[self.future])['matched'])
                with self.assertRaises(SequenceGuardError):remap_control(self.before,self.future,self.after)

    def test_same_snapshot_changed_contents_refuse(self):
        self.after=deepcopy(self.before);self.after['controls'][2]['value']='changed'
        with self.assertRaisesRegex(SequenceGuardError,'snapshot_content'):remap_control(self.before,self.future,self.after)

    def test_acted_multiline_editor_label_requires_pinned_proof(self):
        before=captured('  First\r\nline "Ω"  ',snapshot='s10')
        after=captured('  Second\nline 新  ',snapshot='s11')
        cid=before['controls'][1]['id']
        r=check_transition(before,after,acted_control_id=cid);self.assertTrue(r['matched'],r)
        after['controls'][1]['source']['node']['description']='real field label'
        self.assertFalse(check_transition(before,after,acted_control_id=cid)['matched'])

    def test_unknown_planned_control_or_browser_is_refused(self):
        self.assertFalse(self.check(planned_control_ids=['guessed'])['matched'])
        self.after['kind']='browser_semantic_v2'
        self.assertFalse(self.check()['matched'])

    def test_capture_coverage_loss_and_unbound_acted_handle_stop(self):
        self.after['coverage']['complete']=True
        self.assertFalse(self.check()['matched'])
        self.after=next_capture(self.before);self.after['coverage']['parse_warnings']=['ambiguous']
        self.assertFalse(self.check()['matched'])
        self.after=next_capture(self.before);self.before['handles'].pop(self.acted)
        self.assertEqual(self.check()['reason'],'sequence_acted_handle_unbound')

    def test_inputs_never_mutated(self):
        before,after=deepcopy(self.before),deepcopy(self.after)
        self.check(planned_control_ids=[self.future]);remap_control(self.before,self.future,self.after)
        self.assertEqual((self.before,self.after),(before,after))

    def test_recorded_calculator_transition_and_original_future_targets(self):
        root=Path(__file__).resolve().parents[1]/'artifacts/contract-repair-v10-001/live-calculator-local-1/desktop'
        if not root.exists():self.skipTest('Private retained runtime artifacts unavailable')
        files=list(root.glob('observation-*.json'));hashes={f:hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        captures={o['snapshot_id']:o for f in files if (o:=json.loads(f.read_text()))}
        before,after=captures['s00000335'],captures['s00000338']
        with patch('locua.action_sequence_guard.time.time_ns',return_value=after['observed_at_ns']):
            r=check_transition(before,after,acted_control_id='native:s00000335:13',
                               planned_control_ids=['native:s00000335:7'])
            self.assertTrue(r['matched'],r)
            self.assertEqual(remap_control(before,'native:s00000335:7',after),'native:s00000338:7')
            self.assertFalse(check_transition(before,after,acted_control_id='native:s00000335:13',
                planned_control_ids=['native:s00000335:2'])['matched'])
        self.assertEqual(hashes,{f:hashlib.sha256(f.read_bytes()).hexdigest() for f in files})

    def test_recorded_multiline_editor_change_without_capture_rewrites(self):
        root=Path(__file__).resolve().parents[1]/'artifacts/prompt-policy-v9-001/live-heldout-text-baseline-1/desktop'
        if not root.exists():self.skipTest('Private retained runtime artifacts unavailable')
        files=list(root.glob('observation-*.json'));hashes={f:hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        captures={o['snapshot_id']:o for f in files if (o:=json.loads(f.read_text()))}
        before,after=captures['s000002f6'],captures['s000002f9']
        with patch('locua.action_sequence_guard.time.time_ns',return_value=after['observed_at_ns']):
            result=check_transition(before,after,acted_control_id='native:s000002f6:1')
            self.assertTrue(result['matched'],result)
            self.assertIn('acted_editor_value_label',[r['kind'] for r in result['evidence']['changes']])
            # A changed captured menu inventory before the input is a stop,
            # even though the editor itself remains semantically recognizable.
            self.assertFalse(check_transition(captures['s000002f4'],before,acted_control_id=None)['matched'])
        self.assertEqual(hashes,{f:hashlib.sha256(f.read_bytes()).hexdigest() for f in files})


if __name__=='__main__':unittest.main()
