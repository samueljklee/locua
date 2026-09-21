"""Source-backed value-label identities; synthetic observations, no app calls."""
from copy import deepcopy
import time
import unittest

from locua.goal_verification import BindingError, bind, bind_for_review, matches_binding, verify
from locua.engine.prototype.native_driver_contract import CONTRACT_ID, SOURCE_FINGERPRINT
from locua.engine.prototype.perception import normalize_observation


def captured(value='Draft: generic local check.\n', snapshot='s100', *, duplicate=False):
    contract={'id':CONTRACT_ID,'source_fingerprint_sha256':SOURCE_FINGERPRINT,
              'platform':'macos','plane':'editor_buffer','handle_binding':'same_snapshot_element_token'}
    shown=value.strip()
    editor={'contract':CONTRACT_ID,'plane':'editor_buffer',
            'raw_value':{'status':'ok','value':value},
            'raw_value_recheck':{'status':'ok','value':value},
            'coherence':{'value_stable':True,'focus_stable':True},
            'value_settable':{'status':'ok','value':True},
            'focused':{'status':'ok','value':True}}
    rows=[{'role':'AXWindow','element_index':0,'element_token':snapshot+':0',
           'label':'Disposable document','actions':['AXRaise']}]
    lines=['- [0] AXWindow "Disposable document" [id=window-identity actions=[raise]]']
    for index in (1,2) if duplicate else (1,):
        rows.append({'role':'AXTextArea','element_index':index,'element_token':f'{snapshot}:{index}',
                     'parent_index':0,'label':shown,'value':shown,'editor':deepcopy(editor),
                     'actions':['AXShowMenu'],'frame':{'x':10,'y':20,'w':300,'h':200}})
        # Space-containing metadata reproduces the old semantic-parser warning;
        # the fallback proof must not turn this identifier into parsed identity.
        lines.append(f'  - [{index}] AXTextArea = "{shown}" [id=Text View actions=[showmenu]]')
    raw={'pid':41,'window_id':52,'snapshot_id':snapshot,'elements_complete':False,
         'native_editor_contract':contract,'elements':rows,'tree_markdown':'\n'.join(lines)}
    return normalize_observation(raw,kind='native',expected_target={'pid':41,'window_id':52},
                                 observed_at_ns=time.time_ns())


def goal(value='Ready: generic verified text.'):
    return {'id':'editor','kind':'text','target':'Reviewed disposable editor',
            'value':value,'evidence_plane':'editor_buffer'}


class NativeValueLabelTests(unittest.TestCase):
    def bind_initial(self):
        before=captured();g=goal()
        return g,bind(g,before['controls'][1],before),before

    def test_trimmed_native_name_rebinds_and_preserves_exact_buffer(self):
        g,binding,before=self.bind_initial()
        self.assertEqual(binding['identity_policy']['name_derivation'],'pinned_native_value_fallback_trim')
        self.assertIsNone(binding['core_identity']['name'])
        self.assertIsNone(before['controls'][1]['semantics']['identifier'])
        self.assertIn('native_markdown_syntax_ambiguous_or_unparsed',before['controls'][1]['semantics']['warnings'])
        after=captured(g['value'],snapshot='s101');unchanged=deepcopy(after)
        result=verify(binding,g,after)
        self.assertTrue(result['matched'],result)
        self.assertEqual(after,unchanged)
        self.assertEqual(result['evidence']['actual'],g['value'])
        self.assertEqual(result['evidence']['plane'],'editor_buffer')
        self.assertFalse(result['evidence']['saved_output_proven'])
        self.assertFalse(result['evidence']['persistent_ax_object_proven'])
        self.assertTrue(matches_binding(binding,after,after['controls'][1]['id']))

    def test_expected_text_never_selects_binding_peer_or_relaxes_exactness(self):
        g,binding,_=self.bind_initial()
        after=captured(g['value']+'\n',snapshot='s101')
        self.assertTrue(matches_binding(binding,after,after['controls'][1]['id']))
        result=verify(binding,g,after)
        self.assertFalse(result['matched']);self.assertEqual(result['status'],'mismatch')
        self.assertEqual(result['evidence']['actual'],g['value']+'\n')

    def test_even_initial_untrimmed_value_uses_proof_again_on_fresh_read(self):
        before=captured('Original');g=goal();binding=bind(g,before['controls'][1],before)
        after=captured(g['value'],snapshot='s101')
        after['controls'][1]['source']['node']['editor']['coherence']['value_stable']=False
        self.assertFalse(verify(binding,g,after)['matched'])

    def test_missing_pin_raw_recheck_or_conflicting_facts_cannot_refresh(self):
        g,binding,_=self.bind_initial()
        for mutate in (
            lambda o:o['provenance']['raw_metadata'].pop('native_editor_contract'),
            lambda o:o['controls'][1]['source'].pop('native_editor_contract'),
            lambda o:o['controls'][1]['source']['node']['editor'].pop('raw_value_recheck'),
            lambda o:o['controls'][1]['source']['node']['editor']['raw_value_recheck'].update(value='different'),
            lambda o:o['controls'][1]['source']['node']['editor']['raw_value'].update(status='unsupported'),
            lambda o:o['controls'][1]['value_evidence'].update(exact_value_proven=False),
            lambda o:o['controls'][1].update(display_value='different'),
            lambda o:o['controls'][1]['source']['node'].update(label='different'),
        ):
            after=captured(g['value'],snapshot='s101');mutate(after)
            self.assertFalse(verify(binding,g,after)['matched'])
            self.assertFalse(matches_binding(binding,after,after['controls'][1]['id']))

    def test_missing_provenance_does_not_guess_initial_trim_derivation(self):
        before=captured();before['controls'][1]['source'].pop('native_editor_contract')
        binding=bind(goal(),before['controls'][1],before)
        self.assertNotIn('name_derivation',binding['identity_policy'])
        self.assertEqual(binding['core_identity']['name'],before['controls'][1]['name'])
        self.assertFalse(verify(binding,goal(),captured(goal()['value'],snapshot='s101'))['matched'])

    def test_title_or_description_fallback_cannot_masquerade_as_value(self):
        g,binding,_=self.bind_initial()
        for addition in ('title','description'):
            after=captured(g['value'],snapshot='s101');c=after['controls'][1]
            c['source']['node'][addition]=g['value']
            self.assertFalse(verify(binding,g,after)['matched'])
        for line in ('  - [1] AXTextArea "Ready: generic verified text." = "Ready: generic verified text."',
                     '  - [1] AXTextArea = "Ready: generic verified text." (Description)'):
            after=captured(g['value'],snapshot='s101');self.replace_line(after,line)
            self.assertFalse(verify(binding,g,after)['matched'])

    @staticmethod
    def replace_line(obs,line):
        obs['controls'][1]['source']['markdown_line']=line
        obs['text']=obs['text'].splitlines()[0]+'\n'+line

    def test_source_row_absence_repetition_or_stale_token_refuses(self):
        g,binding,_=self.bind_initial()
        for mutate in (
            lambda o:o.update(text=''),
            lambda o:o.update(text=o['text']+'\n'+o['controls'][1]['source']['markdown_line']),
            lambda o:o['controls'][1]['source'].update(markdown_line_number=1),
            lambda o:o['handles'][o['controls'][1]['id']].update(element_token='s100:1'),
        ):
            after=captured(g['value'],snapshot='s101');mutate(after)
            self.assertFalse(verify(binding,g,after)['matched'])

    def test_geometry_and_named_ancestor_changes_still_refuse(self):
        g,binding,_=self.bind_initial()
        for mutate in (
            lambda o:o['controls'][1]['bounds'].update(x=11),
            lambda o:o['controls'][0].update(name='Different document'),
            lambda o:o['controls'][0]['semantics'].update(identifier='different'),
        ):
            after=captured(g['value'],snapshot='s101');mutate(after)
            self.assertFalse(verify(binding,g,after)['matched'])

    def test_missing_geometry_remains_weak_and_duplicate_editors_ambiguous(self):
        before=captured();before['controls'][1]['bounds']=None
        with self.assertRaises(BindingError):bind(goal(),before['controls'][1],before)
        before=captured(duplicate=True)
        with self.assertRaisesRegex(BindingError,'ambiguous'):bind(goal(),before['controls'][1],before)
        g,binding,_=self.bind_initial()
        after=captured(g['value'],snapshot='s101',duplicate=True)
        self.assertEqual(verify(binding,g,after)['reason'],'bound_target_ambiguous')

    def test_empty_placeholder_and_unsupported_separator_remain_unsupported(self):
        g,binding,_=self.bind_initial()
        for value in ('', '  ', '\x1cready\x1c', 'line1\u2028line2', 'line1\rline2'):
            after=captured(value,snapshot='s101')
            self.assertFalse(matches_binding(binding,after,after['controls'][1]['id']))

    def test_retained_review_does_not_bypass_live_freshness_or_target(self):
        old=captured();old['observed_at_ns']-=120_000_000_000
        old['provenance']['observed_at_ns']=old['observed_at_ns']
        g=goal();binding=bind_for_review(g,old['controls'][1],old)
        self.assertEqual(binding['bound_at_ns'],old['observed_at_ns'])
        self.assertFalse(verify(binding,g,old)['matched'])
        after=captured(g['value'],snapshot='s101')
        self.assertTrue(verify(binding,g,after)['matched'])
        after['target']['window_id']=53
        self.assertFalse(verify(binding,g,after)['matched'])


if __name__ == '__main__':unittest.main()
