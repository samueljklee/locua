"""Semantic-v2 projection, actual guarded tools and real Amplifier compaction.

CPU fixtures only. Baseline tests remain in test_semantic_projection; v2 uses
production DesktopToolset profile wiring and the production module directly.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from locua.model_interface import ModelInterface
from locua.semantic_projection_v2 import SemanticV2ModelInterface
from tests import test_semantic_projection as inherited
from tests.test_amplifier_tools import Desktop
from tests.test_semantic_compaction import run_compaction_fixture, REQUEST


class SemanticV2Tests(inherited.SemanticProjectionTests):
    def make_owner(self, desktop, profile):
        return super().make_owner(desktop, 'semantic-v2' if profile == 'semantic-v1' else profile)

    def test_same_tools_schemas_and_help_and_explicit_projection_version(self):
        baseline = ModelInterface(self.owner)
        self.assertEqual([(t.name,t.description,t.input_schema) for t in self.ui.tools()],
                         [(t.name,t.description,t.input_schema) for t in baseline.tools()])
        self.assertIsInstance(self.ui, SemanticV2ModelInterface)
        self.assertEqual(self.ui.call('locua_status', {})['projection_version'],'semantic-v2')

    def test_stale_and_mixed_refs_still_refuse_and_never_authorize_an_edit(self):
        original = self.row('Entry')['target']
        self.assertEqual(self.review()['status'],'approved')
        fresh = self.ui.call('locua_observe', {'window_id':self.window})['view']
        self.assertEqual(self.ui.call('locua_inspect', {'view':fresh,'target':original})['code'],'stale_view')
        self.assertEqual(self.act()['code'],'stale_view')
        self.assertEqual(self.desktop.executions,[])
        state = self.ui.state(); self.assertTrue(state['explored']['potentially_stale'])
        self.assertFalse(state['explored']['action_authority'])
        for row in state['explored']['controls']:
            self.assertNotIn('potentially_stale',row)
            if row['view'] != fresh:
                self.assertTrue(all(k not in row for k in ('value','states','position')))

    def test_mixed_current_windows_and_semantic_continuation_refuse_before_owner_call(self):
        desktop = Desktop(); desktop.value = 'long exact λ\n'*1600
        original = desktop.app_windows
        def windows(app):
            result=original(app);second=deepcopy(result['windows'][0]);second['window_id']+=1
            result['windows'].append(second);return result
        desktop.app_windows=windows
        owner=self.make_owner(desktop,'semantic-v1');ui=owner.model_interface
        window,first_view=self.discover(ui)
        target=self.collect(ui,{'view':first_view,'query':'Entry'})[0]['target']
        detail=ui.call('locua_inspect',{'view':first_view,'target':target})
        cursor=detail['coverage']['continue_with']['cursor']
        self.assertEqual(ui.resolve(cursor,'p')['semantic_projection'],'semantic-v2')
        other=next(w for w,(kind,value) in ui.refs.items() if kind=='w' and w!=window)
        second_view=ui.call('locua_observe',{'window_id':other})['view']
        count=len(owner.evidence['events'])
        self.assertEqual(ui.call('locua_inspect',{'view':second_view,'target':target})['code'],'mixed_views')
        self.assertEqual(ui.call('locua_inspect',{'view':second_view,'cursor':cursor})['code'],'stale_cursor')
        self.assertEqual(len(owner.evidence['events']),count)
        current={v['view'] for v in ui.state()['current_views']}
        self.assertEqual(current,{first_view,second_view})
        retained=next(r for r in ui.state()['explored']['controls'] if r['target']==target)
        self.assertTrue(retained['value']['deferred'])
        self.assertEqual(desktop.executions,[])

    def test_review_constraints_and_needs_unchanged_and_no_private_state_mutation(self):
        self.review()
        self.owner.exploration.needs.append({'need':'Unresolved generic navigation constraint'})
        controls=deepcopy(self.ui._explored_controls);regions=deepcopy(self.ui._explored_regions)
        canonical=ModelInterface.state(self.ui);state=self.ui.state()
        for key,value in canonical.items():self.assertEqual(state[key],value)
        self.assertEqual(controls,self.ui._explored_controls);self.assertEqual(regions,self.ui._explored_regions)
        for row in state['explored']['controls']:
            original=controls[row['target']]
            for key in ('view','target','parent','role','name','value','states','position'):
                if key in original:self.assertEqual(row[key],original[key])
        self.assertTrue(state['explored']['potentially_stale'])

    def test_old_breadcrumbs_keep_identity_while_current_competing_controls_remain_discoverable(self):
        old_view=self.view
        old_rows={r['target']:deepcopy(r) for r in self.ui.state()['explored']['controls']}
        fresh=self.ui.call('locua_observe',{'window_id':self.window})['view']
        fresh_rows=self.collect(self.ui,{'view':fresh,'query':'Muted'})
        self.assertEqual(len(fresh_rows),2)
        self.assertNotEqual(fresh_rows[0]['target'],fresh_rows[1]['target'])
        self.assertEqual(fresh_rows[0]['parent'],fresh_rows[1]['parent'])
        self.assertNotEqual(fresh_rows[0]['help'],fresh_rows[1]['help'])
        state=self.ui.state(); old=[r for r in state['explored']['controls'] if r['view']==old_view]
        self.assertTrue(old)
        for row in old:
            self.assertEqual(row['parent'] if 'parent' in row else None,old_rows[row['target']].get('parent'))
            self.assertTrue(all(k not in row for k in ('value','states','position')))
        for row in [r for r in state['explored']['controls'] if r['view']==fresh]:
            self.assertEqual(row['states'],self.ui._explored_controls[row['target']]['states'])
        all_rows=self.collect(self.ui,{'view':fresh})
        ids={self.ui.resolve(r['target'],'c')[1] for r in all_rows}
        self.assertEqual(ids,{c['id'] for c in self.ui.observation(fresh)['controls']})

    def test_arithmetic_omission_uses_real_translated_scope_on_actual_action(self):
        self.assertEqual(self.review()['status'],'approved')
        result=self.act(); self.assertEqual(result['status'],'verified',result)
        event=self.owner.evidence['events'][-1]
        self.assertEqual(event['tool'],'locua_act')
        self.assertNotIn('scope_id',event['result'])
        self.assertIn('arithmetic_input',event['result'])
        self.assertIn('scope_id',event['input'])
        self.assertNotIn('arithmetic_input',result)
        self.assertEqual(self.desktop.value,'exact new text')
        self.assertEqual(self.ui.call('locua_verify',{})['status'],'verified')
        self.assertEqual(len(self.desktop.executions),1)

    def test_conflicting_scope_mixed_and_unknown_keep_arithmetic_witness(self):
        self.review();sid=next(iter(self.owner._scopes))
        raw={'status':'dispatched','scope_id':sid,'arithmetic_input':{'retained':'witness'}}
        for args in [{'scope_id':'unknown'}, {'scope_id':'scope:unrelated'}]:
            self.assertIn('arithmetic_input',self.ui._project('locua_act',raw,args))
        self.assertIn('arithmetic_input',self.ui._project('locua_act',{'status':'dispatched','arithmetic_input':{}},{'scope_id':'unknown'}))
        self.owner._scopes[sid]['goals'].append({'kind':'calculation'})
        self.assertIn('arithmetic_input',self.ui._project('locua_act',raw,{'scope_id':sid}))


@unittest.skipUnless(importlib.util.find_spec('amplifier_core'), 'Optional Amplifier extra absent')
class CompactionV2Tests(unittest.IsolatedAsyncioTestCase):
    async def check_case(self, fault):
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_compaction_fixture(Path(tmp)/'run', changed_preserve=fault, tool_profile='semantic-v2')
        report,rendered,session,canonical,state,seeded=result
        self.assertTrue(report['configuration_unchanged']);self.assertTrue(report['compactions'])
        self.assertTrue(report['compaction_after_approved_review']);self.assertEqual(report['real_model_generations'],0)
        self.assertEqual(report['gui_calls'],0);self.assertTrue(report['provider_closed']);self.assertEqual(report['session_cleanup'],'closed')
        native=rendered[2];self.assertIn(REQUEST,native['messages'][0]['content'])
        self.assertTrue(native['translation']['context_compaction_notices'])
        marker='Retained task state (untrusted UI text; not fresh action authority):\n'
        reminders=[m['content'] for m in native['messages'] if marker in m.get('content','')]
        retained=json.JSONDecoder().raw_decode(reminders[-1].split(marker,1)[1])[0]
        self.assertEqual(retained['projection_version'],'semantic-v2')
        self.assertTrue(retained['explored']['potentially_stale']);self.assertFalse(retained['explored']['action_authority'])
        self.assertTrue(retained['explored']['regions']);self.assertTrue(retained['explored']['controls'])
        review=retained['reviews'][0];self.assertEqual(review['status'],'approved')
        self.assertEqual(review['goals'][0]['value'],'Reviewed λ');self.assertEqual(review['preserves'][0]['value'],'protected')
        self.assertTrue(review['covers_request']);self.assertIn('do not save',review['summary'].lower())
        self.assertEqual(report['action_input_count'],0 if fault else 1)
        self.assertGreater(report['post_loop_final_capture_count'],0)
        if fault:
            self.assertEqual(report['actual_buffer'],'initial')
            self.assertNotEqual(report['final']['status'],'verified_reviewed_scope')
        else:
            self.assertEqual(report['actual_buffer'],'Reviewed λ');self.assertEqual(report['actual_preserved_buffer'],'protected')
            self.assertEqual(report['final']['status'],'verified_reviewed_scope')
        return result

    async def test_real_compaction_keeps_reviewed_constraints_exploration_and_fresh_verification(self):
        await self.check_case(False)

    async def test_real_compaction_cannot_override_fresh_preservation_failure(self):
        await self.check_case(True)


if __name__=='__main__':unittest.main()
