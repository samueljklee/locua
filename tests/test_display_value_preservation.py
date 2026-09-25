"""Generic read-only display preservation; CPU observations, no desktop/model."""
from copy import deepcopy
import unittest

from locua.goal_verification import (
    BindingError, bind_for_review, check_review_predicate, matches_binding,
    matches_retained_identity, verify,
)
from test_goal_verification import control, editor, observation, outcome
import test_amplifier_tools as fixtures


def display_goal(value='Medium'):
    return outcome('display_value',value=value,evidence_plane='display',target='Size')


def popup(value='Medium',**changes):
    row=control('AXPopUpButton',value,value,semantics={'identifier':'size-selector'},
        value_evidence={'kind':'native_display_value','precision':'display_only',
                        'exact_value_proven':False,'structured_value_trimmed':True,
                        'possible_placeholder':True})
    row.update(changes)
    return row


class DisplayBindingTests(unittest.TestCase):
    def bind(self,row=None,goal=None):
        goal=goal or display_goal();captured=observation(row or popup())
        binding=bind_for_review(goal,captured['controls'][1],captured)
        return goal,binding,captured

    def test_display_value_is_preserved_without_promoting_to_editor_or_input_authority(self):
        goal,binding,captured=self.bind()
        self.assertEqual(binding['property'],'value')
        self.assertEqual(binding['evidence_plane'],'display')
        self.assertTrue(check_review_predicate(binding,goal,captured)['matched_at_capture'])
        fresh=observation(popup(),snapshot='fresh')
        proof=verify(binding,goal,fresh)
        self.assertTrue(proof['matched'],proof)
        self.assertEqual(proof['evidence']['plane'],'display')
        self.assertEqual(proof['evidence']['value_precision'],'display_only')
        for key in ('exact_raw_axvalue_proven','editor_buffer_proven','committed_document_proven','saved_output_proven'):
            self.assertFalse(proof['evidence'][key])
        self.assertTrue(binding['review_descriptor']['no_write_authority'])
        self.assertFalse(matches_binding(binding,fresh,fresh['controls'][1]['id']))
        self.assertFalse(matches_retained_identity(binding,fresh,fresh['controls'][1]['id']))

    def test_changed_value_and_value_derived_label_produce_mismatch_on_same_identity(self):
        goal,binding,_=self.bind()
        proof=verify(binding,goal,observation(popup('Large'),snapshot='fresh'))
        self.assertFalse(proof['matched']);self.assertEqual(proof['status'],'mismatch')
        self.assertEqual(proof['evidence']['actual'],'Large')

    def test_unknown_value_never_uses_label_or_coerces_nonstring(self):
        goal,binding,_=self.bind()
        for value in (None,False,1,{'text':'Medium'}):
            row=popup(value,name='Medium')
            with self.subTest(value=value),self.assertRaisesRegex(BindingError,'display_value_unknown'):
                self.bind(row)
            self.assertFalse(verify(binding,goal,observation(row,snapshot='fresh'))['matched'])

    def test_mutable_label_without_stable_identity_and_duplicate_identity_refuse(self):
        with self.assertRaisesRegex(BindingError,'stable_identity'):
            self.bind(popup(semantics={},bounds=None))
        obs=observation(popup(),popup())
        with self.assertRaisesRegex(BindingError,'ambiguous'):
            bind_for_review(display_goal(),obs['controls'][1],obs)
        goal,binding,_=self.bind()
        self.assertFalse(verify(binding,goal,observation(popup(),popup(),snapshot='fresh'))['matched'])

    def test_native_text_still_requires_exact_editor_proof_and_plane(self):
        with self.assertRaisesRegex(BindingError,'text_requires_native_editor'):
            self.bind(popup(),outcome('text',value='Medium'))
        with self.assertRaisesRegex(BindingError,'editor_preservation_requires_exact_buffer'):
            self.bind(editor('Medium'),display_goal())
        exact=editor('Medium');goal=outcome('text',value='Medium',evidence_plane='editor_buffer')
        goal,binding,_=self.bind(exact,goal)
        self.assertTrue(verify(binding,goal,observation(exact,snapshot='fresh'))['matched'])
        exact['value_evidence']['exact_value_proven']=False
        self.assertFalse(verify(binding,goal,observation(exact,snapshot='lost-proof'))['matched'])
        with self.assertRaisesRegex(BindingError,'exact_editor_value_unknown'):
            self.bind(exact,goal)


class ToolPreservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ToolTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.popup_value='Medium';original=self.fixture.desktop.observe
        def observe(target):
            result=original(target)
            if result.get('status')=='observed':
                o=result['observation'];sid=o['snapshot_id']
                o['controls'].append(popup(self.popup_value,id=sid+':99',parent=sid+':0',actions=[]))
            return result
        self.fixture.desktop.observe=observe
        self.fixture.sid=self.fixture.call('observe',window_id=self.fixture.window)['snapshot_id']

    def review(self,preserve_editor=False):
        f=self.fixture;entry=f.item('Entry')['control'];keep=f.item('Competitor' if preserve_editor else 'Medium')['control']
        return f.call('review',snapshot_id=f.sid,summary='Replace Entry and preserve the other observed value',
            goals=[{'id':'entry','kind':'text','target':'Entry','control_id':entry['id'],
                    'value':'exact','evidence_plane':'editor_buffer'}],
            effects=[{'kind':'goal','goal_id':'entry'}],covers_entire_request=True,
            preserves=[{'control_id':keep['id'],'property':'value','value':keep['value']}])

    def test_review_compiles_non_editor_value_to_read_only_display_and_final_refresh_checks_it(self):
        reviewed=self.review();self.assertEqual(reviewed['status'],'approved',reviewed)
        scope=self.fixture.tools._scopes[reviewed['scope_id']]
        self.assertEqual(scope['preserves'][0]['goal']['kind'],'display_value')
        self.assertEqual(scope['preserves'][0]['binding']['evidence_plane'],'display')
        result=self.fixture.act(reviewed['scope_id'],'Entry','exact')
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(self.fixture.tools.finalize()['status'],'verified_reviewed_scope')
        self.popup_value='Large'
        final=self.fixture.tools.finalize();self.assertEqual(final['status'],'blocked')
        proof=final['verification']['scopes'][reviewed['scope_id']]
        self.assertFalse(proof['all_preservation_predicates_matched'])
        self.assertFalse(proof['all_reviewed_goals_matched'])

    def test_changed_popup_blocks_before_input(self):
        reviewed=self.review();self.assertEqual(reviewed['status'],'approved',reviewed)
        self.popup_value='Large'
        result=self.fixture.act(reviewed['scope_id'],'Entry','exact')
        self.assertEqual(result['status'],'refused',result)
        self.assertIn('Pre-action preservation',result['reason'])
        self.assertEqual(self.fixture.desktop.executions,[])
        self.assertEqual(self.fixture.desktop.value,'initial')

    def test_unknown_popup_blocks_before_input(self):
        reviewed=self.review();self.assertEqual(reviewed['status'],'approved',reviewed)
        self.popup_value=None
        result=self.fixture.act(reviewed['scope_id'],'Entry','exact')
        self.assertEqual(result['status'],'refused',result)
        self.assertEqual(self.fixture.desktop.executions,[])

    def test_editor_preservation_keeps_existing_exact_buffer_contract(self):
        reviewed=self.review(preserve_editor=True)
        self.assertEqual(reviewed['status'],'approved',reviewed)
        kept=self.fixture.tools._scopes[reviewed['scope_id']]['preserves'][0]
        self.assertEqual(kept['goal']['kind'],'text')
        self.assertEqual(kept['binding']['evidence_plane'],'editor_buffer')
        self.fixture.desktop.other='changed'
        self.assertEqual(self.fixture.act(reviewed['scope_id'],'Entry','exact')['status'],'refused')
        self.assertEqual(self.fixture.desktop.executions,[])


if __name__=='__main__':unittest.main()
