from copy import deepcopy
import asyncio
import json
import unittest
from unittest.mock import patch

from locua.task_observation_scope import ScopeError, TaskObservationScope, scoped_tools_for_request

APP={'name':'Example App','bundle_id':'test.example','pid':7,'app_id':'app:example'}
TARGET={'pid':7,'window_id':9}
REGIONS=[{'id':'region:main','root_control_id':'window'}, {'id':'region:recent','root_control_id':'recent'}]


def fixture():
    return {'kind':'native_window_state','target':TARGET,'snapshot_id':'s1','observed_at_ns':1,'controls':[
        {'id':'window','role':'AXWindow','name':'Example App','parent':None},
        {'id':'readout','role':'AXStaticText','name':None,'value':'pending expression','parent':'window'},
        {'id':'command','role':'AXButton','name':'Mode','parent':'window'},
        {'id':'menu','role':'AXMenuBarItem','name':'File','parent':None},
        {'id':'recent','role':'AXMenuItem','name':'Open Recent','parent':'menu'},
        {'id':'private','role':'AXMenuItem','name':'Private-client.txt','parent':'recent'},
        {'id':'foreign','role':'AXMenuItem','name':'Other-document.txt','parent':'menu'},
        {'id':'valid-menu','role':'AXMenuItem','name':'Scientific','parent':'menu'},
    ]}


def scope():
    s=TaskObservationScope('Use Example App to calculate.',APP)
    s.register_windows([{'window_id':'window:main','target':TARGET,'title':'Example App','identity_proven':True},
        {'window_id':'window:other','target':{'pid':7,'window_id':10},'title':'Other-document.txt','identity_proven':True}],APP['app_id'])
    actions=[{'id':f'a:{c["id"]}','control_id':c['id'],'snapshot_id':'s1','target':TARGET} for c in fixture()['controls']]
    s.register_observation(fixture(),actions=actions,regions=REGIONS)
    return s


class TaskScopeTests(unittest.TestCase):
    def test_literal_app_resolution_refuses_ambiguity_and_unmentioned_names(self):
        self.assertEqual(TaskObservationScope.from_request('Use Example App.',[APP]).app['name'],'Example App')
        with self.assertRaises(ScopeError):TaskObservationScope.from_request('Do it.',[APP])
        with self.assertRaises(ScopeError):TaskObservationScope.from_request('Use Example App and Other.',[APP,{'name':'Other'}])
        with self.assertRaises(ScopeError):TaskObservationScope('Use Example App.',APP,document_titles=['secret.txt'])

    def test_recent_and_foreign_rows_redact_in_place_keep_roles_order_counts_navigation(self):
        s=scope();o=fixture();before=deepcopy(o)
        page={'version':'progressive-ui-v1','snapshot_id':'s1','target':TARGET,
              'columns':['id','role','name','value','states'],
              'items':[[c['id'],c['role'],c.get('name'),c.get('value'),{}] for c in o['controls']],
              'coverage':{'matched_total':8,'returned_count':8,'remaining_count':0,'continuation':None}}
        got=s.project('locua_inspect',page);wire=json.dumps(got)
        self.assertNotIn('Private-client.txt',wire);self.assertNotIn('Other-document.txt',wire)
        self.assertIn('Scientific',wire);self.assertIn('Open Recent',wire);self.assertIn('Mode',wire)
        self.assertEqual([x[:2] for x in got['items']],[x[:2] for x in page['items']])
        self.assertEqual(got['coverage']['matched_total'],8);self.assertFalse(got['coverage']['uniqueness_proven'])
        self.assertEqual(o,before);self.assertEqual(got['observed_at_ns'] if 'observed_at_ns' in got else None,None)

    def test_private_commands_cannot_be_acted_reviewed_or_inspected(self):
        s=scope()
        for tool,args in [('locua_inspect',{'snapshot_id':'s1','control_id':'private'}),
                          ('locua_inspect',{'snapshot_id':'s1','region_id':'region:recent'}),
                          ('locua_act',{'snapshot_id':'s1','action_id':'a:private'}),
                          ('locua_act',{'snapshot_id':'s1','action_id':'a:recent'}),
                          ('locua_review',{'snapshot_id':'s1','goals':[{'control_id':'foreign'}]})]:
            with self.subTest(tool=tool,args=args),self.assertRaises(ScopeError):s.check_call(tool,args)
        self.assertTrue(s.check_call('locua_act',{'snapshot_id':'s1','action_id':'a:valid-menu'}))

    def test_sequence_checks_every_nested_action_before_any_input(self):
        s=scope()
        for action in ('a:private','a:recent','a:foreign','invented',None):
            with self.assertRaises(ScopeError):
                s.check_call('locua_act_sequence',{'snapshot_id':'s1','steps':[
                    {'action_id':'a:valid-menu'},{'action_id':action}]})
        self.assertTrue(s.check_call('locua_act_sequence',{'snapshot_id':'s1',
            'steps':[{'action_id':'a:valid-menu'}]}))

    def test_sequence_private_evidence_paths_never_enter_disclosure(self):
        s=scope()
        raw={'status':'sequence_completed','snapshot_id':'s1','source_evidence_ref':'/private/project/source.json',
             'sequence_evidence_ref':'/private/project/event.json#sequence_evidence',
             'receipts':[{'step':1,'full_response_ref':'/private/project/step.json','action_started':True}]}
        original=deepcopy(raw)
        result=s.project('locua_act_sequence',raw)
        self.assertNotIn('/private/project',json.dumps(result))
        self.assertNotIn('source_evidence_ref',result)
        self.assertNotIn('sequence_evidence_ref',result)
        self.assertTrue(result['receipts'][0]['action_started'])
        self.assertEqual(raw,original)

    def test_foreign_unknown_refs_and_unregistered_capture_fail_closed(self):
        s=scope()
        for args in ({'app_id':'app:foreign'},{'window_id':'window:other'},{'snapshot_id':'unknown'},{'control_id':'invented'}):
            with self.assertRaises(ScopeError):s.check_call('locua_inspect',args)
        with self.assertRaises(ScopeError):s.project('locua_observe',{'snapshot_id':'new','overview':{}})
        with self.assertRaises(ScopeError):s.project('locua_inspect',{'snapshot_id':'s1','control_id':'private','items':[]})

    def test_private_source_unbound_text_and_accounts_not_disclosed(self):
        s=scope();got=s.project('locua_inspect',{'snapshot_id':'s1','source':{'secret':'source-private'},
            'raw':{'secret':'raw-private'},'tree_markdown':'global-private',
            'items':[{'kind':'unbound_text','text':'foreign hidden words'}, {'name':'Log Out Example Person…'}]})
        wire=json.dumps(got)
        for secret in ('source-private','raw-private','global-private','foreign hidden words','Example Person'):
            self.assertNotIn(secret,wire)
        self.assertIn('privacy_unknown_unbound_text',wire)

    def test_same_window_changed_document_fails_before_disclosure(self):
        s=scope();o=fixture();o['snapshot_id']='s2';o['controls'][0]['name']='New secret document'
        with self.assertRaises(ScopeError):s.register_observation(o,regions=REGIONS)
        self.assertNotIn('s2',s.snapshots)

    def test_inventory_projection_counts_unknown_not_absent(self):
        s=scope();got=s.project('locua_apps',{'items':[APP,{'name':'Private App','app_id':'secret'}],'total':2})
        self.assertEqual(len(got['items']),1);self.assertEqual(got['privacy_omitted_items'],1)
        self.assertTrue(got['privacy_scope']['unknown_content_not_absent'])
        got=s.project('locua_windows',{'windows':[{'window_id':'window:main','title':'Example App'},
            {'window_id':'window:other','title':'Other-document.txt'}]})
        self.assertEqual(len(got['windows']),1);self.assertNotIn('Other-document.txt',json.dumps(got))


    def test_fragmented_unbound_text_and_short_private_region_are_not_disclosed(self):
        s=scope();o=fixture();o['snapshot_id']='s2';o['controls'][5]['name']='a'
        s.register_observation(o,regions=REGIONS)
        got=s.project('locua_inspect',{'snapshot_id':'s2','items':[
            {'kind':'json_fragment','source_kind':'unbound_text','text':'SECRET_FRAGMENT'},
            {'id':'r1','root_control_id':'private','label':'a','role':'AXMenuItem','states':{'description':'SECRET_STATE'}},
            {'id':'private','name':'a','states':{'description':'SECRET_STATE'},'extra':'SECRET_EXTRA'}]})
        wire=json.dumps(got)
        for secret in ('SECRET_FRAGMENT','SECRET_STATE','SECRET_EXTRA'):self.assertNotIn(secret,wire)
        self.assertNotIn('label',got['items'][1]);self.assertTrue(got['items'][1]['unknown'])

    def test_authorized_editor_semantics_and_coverage_survive_source_projection(self):
        s=scope();o=fixture();o['snapshot_id']='s2';c=o['controls'][1]
        c['editor']={'plane':'editor_buffer','focused':{'status':'ok','value':False},
            'raw_value':{'status':'ok','value':'  Original Ω\n'},'value_settable':{'status':'ok','value':True},
            'selected_range':{'status':'ok','value':{'location':0,'length':0}},'contract':'implementation-private'}
        c['value_evidence']={'precision':'exact','exact_value_proven':True,'basis':'private-implementation'}
        s.register_observation(o,regions=REGIONS)
        got=s.project('locua_inspect',{'snapshot_id':'s2','control_id':'readout',
            'metadata':{'region':{'id':'region:main','label':'Main'},'primary_control_count':4},
            'items':[{'kind':'json_fragment','field':'editor','text':'implementation-private'}]})
        ed=got['control_semantics']['editor']
        self.assertEqual(ed['raw_value']['value'],'  Original Ω\n');self.assertIs(ed['focused']['value'],False)
        self.assertIs(ed['value_settable']['value'],True)
        self.assertEqual(got['metadata']['primary_control_count'],4)
        self.assertNotIn('implementation-private',json.dumps(got));self.assertNotIn('private-implementation',json.dumps(got))

    def test_authorized_values_coinciding_with_private_titles_stay_exact(self):
        s=scope();value='Other-document.txt'
        for v in (value,0,False,''):
            got=s.project('locua_inspect',{'snapshot_id':'s1','id':'readout','value':v,
                'value_evidence':{'precision':'exact','exact_value_proven':True}})
            self.assertEqual(got['value'],v);self.assertTrue(got['value_evidence']['exact_value_proven'])
        named=s.project('locua_inspect',{'snapshot_id':'s1','id':'readout','name':value,'semantics':{'description':value}})
        self.assertEqual(named['name'],value);self.assertEqual(named['semantics']['description'],value)
        page={'version':'progressive-ui-v1','snapshot_id':'s1','columns':['id','role','value'],
            'items':[['readout','AXStaticText',value]],'coverage':{}}
        self.assertEqual(s.project('locua_inspect',page)['items'][0][2],value)

    def test_native_observe_string_coverage_scope_preserves_overview_and_target(self):
        # Minimal public shape from the recorded native observe failure: the
        # outer native scope is a string, the nested progressive scope a dict.
        s=scope();native_coverage={'complete':False,'scope':'native_window',
            'structured_actionable_only':True,'static_markdown_retained':True,
            'raw_element_count':174,'normalized_control_count':195}
        result={'status':'observed','snapshot_id':'s1','target':TARGET,
            'window_id':'window:main','coverage':native_coverage,'task_complete':False,
            'overview':{'version':'progressive-ui-v1','operation':'overview','snapshot_id':'s1',
                'target':TARGET,'coverage':{'scope':{'kind':'overview'},'returned_count':2,'continuation':None},
                'items':[{'kind':'unnamed_readout','id':'readout','role':'AXStaticText','value':'pending expression'},
                         {'kind':'navigation_candidate','id':'valid-menu','role':'AXMenuItem','name':'Scientific'}]}}
        original=deepcopy(result);got=s.project('locua_observe',result)
        self.assertEqual(got['coverage'],native_coverage);self.assertEqual(got['target'],TARGET)
        self.assertEqual(got['overview']['items'],result['overview']['items'])
        self.assertNotIn('control_semantics',got);self.assertEqual(result,original)
        self.assertEqual(got['privacy_scope']['version'],'task-observation-scope-v1.1')

    def test_structured_detail_scope_still_resolves_only_registered_allowed_control(self):
        s=scope();page={'snapshot_id':'s1','coverage':{'scope':{'kind':'control','control_id':'readout'}},'items':[]}
        got=s.project('locua_inspect',page)
        self.assertEqual(got['control_semantics']['id'],'readout')
        page['coverage']['scope']['control_id']='private'
        with self.assertRaises(ScopeError):s.project('locua_inspect',page)
        for bad in ([],True,12):
            page['coverage']['scope']=bad
            with self.assertRaises(ScopeError):s.project('locua_inspect',page)
        page['coverage']['scope']='native_window';page['control_id']=[]
        with self.assertRaises(ScopeError):s.project('locua_inspect',page)

    def test_snapshot_reuse_with_changed_content_and_malformed_refs_refused(self):
        s=scope();o=fixture();o['controls'][1]['value']='changed'
        with self.assertRaises(ScopeError):s.register_observation(o,regions=REGIONS)
        for args in ({'control_id':[]},{'goals':'wrong'},{'effects':[1]}):
            with self.assertRaises(ScopeError):s.check_call('locua_review',args)


class WrappedTests(unittest.IsolatedAsyncioTestCase):
    def fake(self):
        from types import SimpleNamespace
        from amplifier_core.models import ToolResult
        class Desktop:
            def apps(self):return {'status':'ok','apps':[APP]}
            def app_windows(self,app):return {'status':'ok','windows':[{'pid':7,'window_id':9,'title':'Example App','app_identity_evidence':{'ok':True}}]}
        class Tool:
            name='locua_apps';description='actual';input_schema={'type':'object'}
            def __init__(self):self.calls=0;self.output={'status':'ok','items':[APP]}
            async def execute(self,args):self.calls+=1;return ToolResult(success=True,output=self.output)
        inner=Tool();owner=SimpleNamespace(desktop=Desktop(),_app_records={},_window_records={},_observations={},_actions={},tools=lambda:[inner])
        return owner,inner

    async def test_wrapper_refuses_foreign_before_underlying_call_and_preserves_schema(self):
        owner,inner=self.fake();tools,meta=scoped_tools_for_request(owner,'Use Example App.')
        self.assertEqual(tools[0].input_schema,inner.input_schema)
        bad=await tools[0].execute({'query':'Private App'});self.assertFalse(bad.success);self.assertEqual(inner.calls,0)
        good=await tools[0].execute({'query':'Example App'});self.assertTrue(good.success);self.assertEqual(inner.calls,1)
        self.assertEqual(meta['preflight_desktop_reads'],2)

    async def test_post_call_disclosure_failure_preserves_unknown_effect_and_blocks_retry(self):
        owner,inner=self.fake();tools,_=scoped_tools_for_request(owner,'Use Example App.')
        inner.output={'status':'verified','snapshot_id':'not-registered','action_started':True}
        first=await tools[0].execute({});self.assertEqual(first.output['status'],'privacy_blocked')
        self.assertTrue(first.output['action_started']);self.assertTrue(first.output['no_retry'])
        await tools[0].execute({});self.assertEqual(inner.calls,1)

    async def test_preflight_failure_happens_before_any_model_or_tool_execution(self):
        owner,inner=self.fake()
        with self.assertRaises(ScopeError):scoped_tools_for_request(owner,'Use a different application.')
        self.assertEqual(inner.calls,0)


class PrivateFailureAuditTests(unittest.IsolatedAsyncioTestCase):
    fake=WrappedTests.fake
    async def test_projection_exception_details_are_private_and_no_repeat_authority(self):
        import tempfile
        from pathlib import Path
        owner,inner=self.fake()
        with tempfile.TemporaryDirectory() as folder:
            owner.out=Path(folder);tools,_=scoped_tools_for_request(owner,'Use Example App.')
            with patch.object(TaskObservationScope,'project',side_effect=ValueError('PRIVATE_ERROR_DETAIL /Users/private/document')):
                result=await tools[0].execute({})
            wire=json.dumps(result.model_dump())
            self.assertNotIn('PRIVATE_ERROR_DETAIL',wire);self.assertNotIn('/Users/private',wire)
            self.assertTrue(result.output['no_retry']);self.assertEqual(result.output['status'],'privacy_blocked')
            log=json.loads((owner.out/'privacy-001.json').read_text())
            self.assertEqual(log['exception_type'],'ValueError')
            self.assertIn('PRIVATE_ERROR_DETAIL',log['exception_message'])
            self.assertEqual(log['projection_stage'],'project_result')
            self.assertEqual((owner.out/'privacy-001.json').stat().st_mode & 0o777,0o600)
            await tools[0].execute({});self.assertEqual(inner.calls,1)
