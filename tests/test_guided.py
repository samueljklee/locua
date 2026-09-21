from copy import deepcopy
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from locua.guided import catalog, compose, review_text, _value


def observed():
    roots=[{'id':'left','role':'group','name':'Defaults','parent':None},
           {'id':'right','role':'group','name':'Project','parent':None}]
    controls=roots+[{'id':k,'role':'textbox','name':'Title','parent':parent,'value':value,
        'actions':['type'],'states':{},'value_evidence':{'exact_value_proven':True}}
        for k,parent,value in [('a','left','default'),('b','right','draft')]]
    return {'kind':'browser_semantic_v2','snapshot_id':'s1','controls':controls,
            'handles':{k:{'kind':'browser'} for k in ['a','b']}}


class GuidedTests(unittest.TestCase):
    def test_duplicate_labels_keep_context_and_competitor(self):
        rows=catalog(observed())['fields']
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['subject']['ancestor']['name'],'Defaults')
        self.assertEqual(rows[1]['subject']['ancestor']['name'],'Project')
        self.assertIn('Project',rows[1]['label'])

    def test_exact_choices_compile_and_preserve_other_fields(self):
        rows=catalog(observed())['fields'];value='  00028-Ω\n'
        p=compose({'kind':'browser','url':'http://localhost/'},rows,{2:value})
        self.assertEqual(p['outcomes'][0]['value'],value)
        self.assertEqual(p['constraints'][0]['value'],'default')
        self.assertEqual(p['outcomes'][0]['subject'],rows[1]['subject'])
        self.assertIn('guided choices',review_text(p,rows,{2:value},'comparator'))
        self.assertIn('00028-Ω',review_text(p,rows,{2:value},'comparator'))

    def test_unknown_exactness_and_ambiguous_controls_reported_not_guessed(self):
        o=observed();o['controls'][2]['value_evidence']={}
        c=catalog(o);self.assertEqual(len(c['fields']),1);self.assertEqual(len(c['unavailable']),1)
        o=observed();o['controls'][1]['name']='Defaults'
        c=catalog(o);self.assertFalse(c['fields']);self.assertEqual(len(c['unavailable']),2)

    def test_unexpected_selection_type_and_empty_changes_rejected(self):
        rows=catalog(observed())['fields']
        for bad in ({},{3:'bad'},{True:'bad'},{1:False}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                compose({'kind':'browser','url':'http://localhost/'},rows,bad)

    def test_checkbox_value_is_boolean_and_literal_at_is_escaped(self):
        self.assertFalse(_value('off',{'property':'checked'}))
        with self.assertRaises(ValueError):_value('perhaps',{'property':'checked'})
        self.assertEqual(_value('@@example',{'property':'value'}),'@example')
        self.assertEqual(_value('  x\n',{'property':'value'}),'  x\n')

    def test_utf8_file_input_preserves_crlf_unicode_and_trailing_whitespace(self):
        value=' 00028\r\nΩ café\r\nfinal \t '
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'exact.txt';path.write_bytes(value.encode('utf-8'))
            actual=_value('@'+str(path),{'property':'value'})
        self.assertEqual(actual,value);self.assertEqual(actual.encode('utf-8'),value.encode('utf-8'))

    def test_max_constraints_refuses_instead_of_dropping(self):
        rows=[{'number':n,'label':str(n),'subject':{'name':str(n),'role':'textbox'},'property':'value','value':'old'} for n in range(1,19)]
        with self.assertRaisesRegex(ValueError,'More than 16'):
            compose({'kind':'browser','url':'http://localhost/'},rows,{1:'new'})

class GuidedReviewTests(unittest.TestCase):
    def test_declined_review_never_executes_and_records_readable_plan(self):
        import tempfile, json
        from unittest.mock import patch
        from locua.guided import start
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'run';answers=iter(['2','new title','',''])
            prep={'scope':{'kind':'browser','url':'http://localhost/'},'target_label':'Local project',
                  'observation':observed()}
            with patch('locua.lib._engine',return_value={'result':prep}),patch('locua.lib.run') as execute,patch('locua.engine.prototype.observation_tools.overview',return_value={'items':[]}):
                result=start(url='http://localhost/',out=folder,ask=lambda _:next(answers),progress=lambda _:None)
            execute.assert_not_called();self.assertEqual(result['status'],'canceled')
            review=(folder/'review.txt').read_text();self.assertIn('Local project',review);self.assertIn('new title',review)
            self.assertFalse(json.loads((folder/'review.json').read_text())['accepted'])
            self.assertFalse(result['natural_language_autonomy_proven'])

    def test_approval_uses_composed_plan_not_driver_handles(self):
        import tempfile
        from unittest.mock import patch
        from locua.guided import start
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'run';answers=iter(['2','new title','','run'])
            prep={'scope':{'kind':'browser','url':'http://localhost/'},'target_label':'Local project','observation':observed()}
            with patch('locua.lib._engine',return_value={'result':prep}),patch('locua.lib.run',return_value={'result':{'status':'complete','artifacts':str(folder/'execution')}}) as execute,patch('locua.engine.prototype.observation_tools.overview',return_value={'items':[]}):
                result=start(url='http://localhost/',out=folder,model='comparator',ask=lambda _:next(answers),progress=lambda _:None)
            payload=execute.call_args.kwargs
            self.assertTrue(payload['execute']);self.assertEqual(payload['model'],'comparator')
            self.assertNotIn('control_id',str(payload['task']))
            self.assertEqual(payload['task']['outcomes'][0]['value'],'new title')
            self.assertEqual(result['status'],'complete');self.assertEqual(result['artifacts'],str(folder.resolve()))


def native_observed():
    """Synthetic raw contract, normalized by the shipped perception boundary."""
    from locua.engine.prototype.native_driver_contract import CONTRACT_ID, SOURCE_FINGERPRINT
    from locua.engine.prototype.perception import normalize_observation
    def editor(value):
        return {'contract':CONTRACT_ID,'plane':'editor_buffer',
                'raw_value':{'status':'ok','value':value},'raw_value_recheck':{'status':'ok','value':value},
                'value_settable':{'status':'ok','value':True},'focused':{'status':'ok','value':True},
                'coherence':{'value_stable':True,'focus_stable':True}}
    target={'pid':1234,'window_id':5678}
    raw={**target,'snapshot_id':'s00000001','elements_complete':False,
         'native_editor_contract':{'id':CONTRACT_ID,'source_fingerprint_sha256':SOURCE_FINGERPRINT,
                                  'platform':'macos','plane':'editor_buffer','handle_binding':'same_snapshot_element_token'},
         'elements':[{'element_index':0,'element_token':'s00000001:0','role':'AXWindow','label':'Notes.txt'},
                     {'element_index':1,'element_token':'s00000001:1','role':'AXTextArea','label':'Original note',
                      'editor':editor('  Original Ω\n')},
                     {'element_index':2,'element_token':'s00000001:2','role':'AXTextField','label':'Find',
                      'editor':editor('preserve this query')}],
         'tree_markdown':'- AXApplication "TextEdit"\n  - [0] AXWindow "Notes.txt"\n'
                         '    - [1] AXTextArea "Original note"\n    - [2] AXTextField "Find"'}
    schemas={'set_value':{'properties':{'pid':{},'value':{},'element_token':{}}},
             'get_window_state':{'properties':{'pid':{},'window_id':{}}}}
    return normalize_observation(raw,kind='native_window_state',expected_target=target,
                                 observed_at_ns=time.time_ns(),tool_schemas=schemas)


class GuidedNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.document=self.root/'Notes.txt'
        self.document.write_text('  Original Ω\n',encoding='utf-8')
        self.expected='  Exact café Ω\nSecond line.\n'

    def _guided(self,answers,*,execute_result=None,save_result=None,execute_error=None,prepare_update=None,
                save_route='textedit_shortcut'):
        from locua.guided import start
        folder=self.root/'run';messages=[];questions=[];choices=iter(answers)
        prep={'scope':{'kind':'native','pid':1234,'window_id':5678},'target_label':'TextEdit — '+str(self.document),
              'observation':native_observed(),'document_binding_id':'bound-document-test',
              'document_editor_id':'native:s00000001:1','document_opened_by_locua':True}
        prep.update(prepare_update or {})
        def ask(prompt):questions.append(prompt);return next(choices)
        def execute(**kwargs):
            if execute_error is not None:raise execute_error
            output=Path(kwargs['out']);output.mkdir(parents=True)
            result={'status':'complete','artifacts':str(output)}
            result.update(execute_result or {})
            if result['status']=='complete':
                (output/'task-state.json').write_text(json.dumps({'outcomes':{'change-1':'verified'}}))
            return {'result':result}
        saved={'status':'saved','saved_output_proven':True,'saved_file_proven':True,
               'explicit_save_proven':True,'save_command_dispatched':True,'save_shortcut_posted':True,
               'menu_dispatch_proven':False,'save_causality_proven':False,'post_buffer_proven':True}
        saved.update(save_result or {})
        with patch('locua.lib._engine',return_value={'result':prep}) as prepare, \
             patch('locua.lib.run',side_effect=execute) as run, \
             patch('locua.document_open.save_prepared',return_value=saved) as save, \
             patch('locua.document_open.release_prepared') as release, \
             patch('locua.engine.prototype.observation_tools.overview',return_value={'items':[{'label':'Document'}]}):
            result=start(document=self.document,model='comparator',native_save_route=save_route,
                         out=folder,ask=ask,progress=messages.append)
        return result,{'prepare':prepare,'run':run,'save':save,'release':release,
                       'folder':folder,'messages':messages,'questions':questions,'prep':prep}

    def test_native_catalog_keeps_document_and_other_writable_competitor(self):
        fields=catalog(native_observed())['fields']
        self.assertEqual([field['control_id'] for field in fields],['native:s00000001:1','native:s00000001:2'])
        self.assertEqual(fields[0]['subject'],{'role':'AXTextArea','ancestor':{'name':'Notes.txt','role':'AXWindow'}})
        self.assertEqual(fields[0]['value'],'  Original Ω\n')
        self.assertEqual(fields[1]['subject']['name'],'Find')

    def test_native_preflight_rejects_unbound_field_before_execution(self):
        result,state=self._guided(['2','different search',''])
        self.assertEqual(result['status'],'blocked');self.assertIn('only the bound Document text',result['reason'])
        state['run'].assert_not_called();state['save'].assert_not_called()
        self.assertFalse((state['folder']/'review.json').exists())
        state['release'].assert_called_once_with(state['prep'])

    def test_native_preflight_rejects_bound_plus_other_field(self):
        result,state=self._guided(['1',self.expected,'2','different search',''])
        self.assertEqual(result['status'],'blocked')
        state['run'].assert_not_called();state['save'].assert_not_called()
        self.assertFalse(result['saved_output_proven'])

    def test_missing_document_binding_editor_does_not_authorize_replacement(self):
        result,state=self._guided(['1',self.expected,''],prepare_update={'document_editor_id':'foreign-or-old-editor'})
        self.assertEqual(result['status'],'blocked');state['run'].assert_not_called();state['save'].assert_not_called()
        self.assertIn('only the bound Document text',result['reason'])

    def test_native_declined_review_never_executes_or_saves(self):
        result,state=self._guided(['1',self.expected,'',''])
        self.assertEqual(result['status'],'canceled');self.assertEqual(result['reason'],'review_not_approved')
        state['run'].assert_not_called();state['save'].assert_not_called()
        review=(state['folder']/'review.txt').read_text()
        self.assertIn('guarded TextEdit Command-S',review);self.assertIn('preserve this query',review)
        self.assertFalse(json.loads((state['folder']/'review.json').read_text())['accepted'])

    def test_saved_bytes_without_explicit_save_proof_block_completion(self):
        result,state=self._guided(['1',self.expected,'','run'],save_result={
            'status':'partial','explicit_save_proven':False,'save_command_dispatched':False,
            'save_shortcut_posted':False,'save_command_effect':'refused'})
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['reason'],'explicit_native_save_not_verified')
        self.assertTrue(result['saved_output_proven']);self.assertFalse(result['explicit_save_proven'])
        state['run'].assert_called_once();state['save'].assert_called_once()
        self.assertFalse(any(line.startswith('VERIFIED: 1 requested changes;') for line in state['messages']))
        self.assertFalse(any(line.startswith('Saved file verified:') for line in state['messages']))
        self.assertEqual(result['field_results'][0]['status'],'verified')
        self.assertTrue(any(line.startswith('STOPPED:') for line in state['messages']))
        persisted=json.loads((state['folder']/'summary.json').read_text())
        self.assertEqual(persisted['status'],'blocked');self.assertFalse(persisted['explicit_save_proven'])

    def test_acknowledged_save_without_saved_output_also_blocks(self):
        result,state=self._guided(['1',self.expected,'','run'],save_result={
            'status':'unknown_effect','saved_output_proven':False,'saved_file_proven':False,'explicit_save_proven':False})
        self.assertEqual(result['status'],'blocked');self.assertFalse(result['saved_output_proven'])
        self.assertFalse(any(line.startswith('VERIFIED: 1 requested changes;') for line in state['messages']))
        self.assertFalse(any(line.startswith('Saved file verified:') for line in state['messages']))

    def test_disappeared_target_before_execution_never_enters_save(self):
        result,state=self._guided(['1',self.expected,'','run'],execute_result={
            'status':'blocked','reason':'window_id_not_found','actions':0,'decisions':0})
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['reason'],'window_id_not_found')
        self.assertFalse(result['saved_output_proven']);state['save'].assert_not_called()
        self.assertEqual(result['field_results'][0]['status'],'unverified')
        self.assertFalse((state['folder']/'save').exists())
        self.assertFalse(any(line.startswith('VERIFIED:') for line in state['messages']))
        state['release'].assert_called_once_with(state['prep'])

    def test_disappeared_target_exception_never_enters_save(self):
        from locua.engine.prototype.cua import CuaRefusal
        result,state=self._guided(['1',self.expected,'','run'],execute_error=CuaRefusal({'code':'window_id_not_found'}))
        self.assertEqual(result['status'],'blocked');self.assertIn('window_id_not_found',result['reason'])
        self.assertFalse(result['saved_output_proven']);state['save'].assert_not_called()
        self.assertFalse((state['folder']/'save').exists())

    def test_verified_native_result_preserves_exact_choice_and_save_route(self):
        result,state=self._guided(['1',self.expected,'','run'])
        self.assertEqual(result['status'],'complete');self.assertTrue(result['saved_output_proven'])
        self.assertTrue(result['explicit_save_proven']);self.assertFalse(result['natural_language_autonomy_proven'])
        prepared=state['prepare'].call_args.args
        self.assertEqual(prepared[0],'prepare_guided');self.assertEqual(prepared[1]['document'],str(self.document))
        executed=state['run'].call_args.kwargs
        self.assertTrue(executed['execute']);self.assertEqual(executed['model'],'comparator')
        plan=executed['task'];self.assertEqual(plan['scope'],state['prep']['scope'])
        self.assertEqual(len(plan['outcomes']),1);self.assertEqual(plan['outcomes'][0]['value'],self.expected)
        self.assertEqual(plan['outcomes'][0]['subject']['role'],'AXTextArea')
        self.assertEqual(plan['constraints'][0]['value'],'preserve this query')
        self.assertNotIn('element_token',str(plan));self.assertNotIn('control_id',str(plan))
        saved_call=state['save'].call_args
        self.assertEqual(saved_call.args[:2],(state['prep'],self.expected))
        self.assertEqual(saved_call.kwargs,{'save_route':'textedit_shortcut'})
        self.assertEqual(result['field_results'],[{'label':'TextEdit > Notes.txt > Document text',
                                                  'requested':self.expected,'status':'verified'}])
        self.assertTrue(any(line.startswith('VERIFIED: 1 requested changes; 1 preserved fields.') for line in state['messages']))
        self.assertIn('Saved file verified: '+str(self.document),state['messages'])
        self.assertFalse(result['save']['save_causality_proven'])
        persisted=json.loads((state['folder']/'summary.json').read_text())
        self.assertEqual(persisted['field_results'],result['field_results']);self.assertTrue(persisted['explicit_save_proven'])
        state['release'].assert_called_once_with(state['prep'])


if __name__=='__main__':unittest.main()
