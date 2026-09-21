"""Offline tool-protocol and scope tests; no GUI, subprocess or model calls."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from locua.amplifier_tools import DesktopToolset, MODEL_RESPONSE_BYTES, model_projection
from locua.engine.prototype.observation_tools import inspect as source_inspect
from locua.progressive_ui import exposed_controls, unpack_row


class Desktop:
    def __init__(self):
        self.sequence=0;self.value='initial';self.other='protected';self.checked=True
        self.display='0';self.calls=[];self.executions=[];self.uncertain=False;self.unavailable=False
    def apps(self):
        return {'status':'ok','apps':[{'name':'Tool surface','bundle_id':'test.surface','launch_path':'/Fixture.app','pid':41,'running':True}]
            +[{'name':f'Other {n}','bundle_id':f'test.other{n}','launch_path':f'/Other{n}.app','pid':0,'running':False} for n in range(70)]}
    def app_windows(self,app):
        return {'status':'ok','windows':[{'pid':41,'window_id':52,'title':'Tool surface','is_on_screen':True,
                                        'app_identity_evidence':{'fixture':True}}],'unresolved_pids':[]}
    def launch(self,app):self.calls.append(('launch',app));return {'status':'launched','app':app,'action_started':True}
    def activate(self,target):self.calls.append(('activate',target));return {'status':'activated','action_started':True}
    def observe(self,target):
        self.calls.append(('observe',deepcopy(target)))
        if self.unavailable:return {'status':'unavailable','reason':'Synthetic target missing'}
        self.sequence+=1;s=f's{self.sequence}';now=time.time_ns()
        controls=[{'id':s+':0','role':'AXWindow','name':'Tool surface','value':None,'parent':None,
                   'semantics':{'identifier':'main'},'states':{},'actions':[]}]
        entries=[('AXTextField','Entry',self.value),('AXTextField','Competitor',self.other),
                 ('AXCheckBox','Keep',None),('AXStaticText','Result',self.display)]
        entries += [('AXButton',name,None) for name in ('All Clear','2','+','3','=','Hide panel')]
        for n,(role,name,value) in enumerate(entries,1):
            c={'id':s+':'+str(n),'role':role,'name':name,'value':value,'parent':s+':0',
               'semantics':{'identifier':name},'states':{'enabled':True},'actions':[],
               'bounds':{'x':n*10,'y':20,'width':8,'height':20}}
            if role=='AXTextField':c['value_evidence']={'precision':'exact','exact_value_proven':True,'plane':'editor_buffer'}
            if role=='AXCheckBox':c['states']['checked']=self.checked
            controls.append(c)
        return {'status':'observed','observation':{'kind':'native_window_state','target':deepcopy(target),
            'snapshot_id':s,'observed_at_ns':now,'provenance':{'observed_at_ns':now},'controls':controls,
            'text':'','handles':{},'hierarchy':[],'coverage':{'complete':False}}}
    def actions(self,o):
        rows=[]
        for c in o['controls']:
            if c['role'] in ('AXTextField','AXButton','AXCheckBox'):
                rows.append({'id':'action:'+c['id'],'kind':'set_text' if c['role']=='AXTextField' else 'press',
                    'control_id':c['id'],'snapshot_id':o['snapshot_id'],'target':o['target'],
                    'description':c['name'],'requires_value':c['role']=='AXTextField'})
        return {'status':'ok','actions':rows}
    def execute(self,action,o):
        self.executions.append(deepcopy(action))
        if self.uncertain:return {'status':'uncertain','reason':'Synthetic timeout','action_started':True}
        name=next(c['name'] for c in o['controls'] if c['id']==action['control_id'])
        if name=='Entry':self.value=action['value']
        elif name=='Competitor':self.other=action['value']
        elif name=='Keep':self.checked=not self.checked
        elif name=='=':self.display='5'
        elif name=='All Clear':self.display='0'
        post=self.observe(o['target'])['observation']
        return {'status':'verified' if action['kind']=='set_text' else 'dispatched','observation':post,
                'driver_ack':{'effect':'unverifiable'},'action_started':True}
    def close(self):return {'status':'closed','applications_left_open':True}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.desktop=Desktop();self.reviews=[];self.answer='run'
        def ask(message,kind):self.reviews.append(message);return self.answer
        self.tools=DesktopToolset({},Path(self.tmp.name)/'tools','Set Entry to exact value, preserve Keep.',ask,desktop=self.desktop)
        self.addCleanup(self.tools.close)
        self.app=self.call('apps')['items'][0]['app_id']
        self.window=self.call('windows',app_id=self.app)['windows'][0]['window_id']
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
    def call(self,name,**args):return self.tools.call('locua_'+name,args)
    def items(self,sid=None):
        rows=[];cursor=None
        while True:
            args={'snapshot_id':sid or self.sid,'operation':'list','limit':128}
            if cursor is not None:args['cursor']=cursor
            page=self.call('inspect',**args)
            self.assertEqual(page['status'],'ok',page)
            rows += [{'control':{k:v for k,v in row.items() if k!='actions'},'actions':row['actions']}
                     for row in exposed_controls(page)]
            cursor=page['coverage']['continuation']
            if cursor is None:return rows
    def details(self,cid,sid=None):
        fields={};fragments={};cursor=None
        while True:
            args={'snapshot_id':sid or self.sid,'operation':'control','control_id':cid}
            if cursor is not None:args['cursor']=cursor
            page=self.call('inspect',**args);self.assertEqual(page['status'],'ok',page)
            self.assert_byte_bound(page)
            for row in page['items']:
                if row['kind']=='attribute':fields[row['field']]=row['value']
                else:fragments.setdefault(row['field'],[]).append(row)
            cursor=page['coverage']['continuation']
            if cursor is None:break
        for field,parts in fragments.items():
            self.assertEqual([p['part'] for p in parts],list(range(parts[0]['parts'])))
            encoded=''.join(p['text'] for p in parts)
            import hashlib
            self.assertEqual(hashlib.sha256(encoded.encode()).hexdigest(),parts[0]['sha256'])
            fields[field]=json.loads(encoded)
        return fields
    def item(self,name,sid=None):return next(r for r in self.items(sid) if r['control']['name']==name)
    def review_text(self,value='exact',complete=False,preserve=False):
        entry=self.item('Entry')['control'];kwargs={}
        if preserve:kwargs['preserves']=[{'control_id':self.item('Keep')['control']['id'],'property':'checked','value':True}]
        r=self.call('review',snapshot_id=self.sid,summary='Replace Entry only',
             goals=[{'id':'entry','kind':'text','target':'Entry','control_id':entry['id'],'value':value,'evidence_plane':'editor_buffer'}],
             effects=[{'kind':'goal','goal_id':'entry'}],covers_entire_request=complete,**kwargs)
        self.assertEqual(r['status'],'approved',r);return r['scope_id']
    def act(self,scope,name,value=None,sid=None):
        sid=sid or self.sid;action=self.item(name,sid)['actions'][0];args={}
        if value is not None:args['value']=value
        return self.call('act',scope_id=scope,snapshot_id=sid,action_id=action['id'],**args)

    def test_apps_full_inventory_paging_search_and_unknown_keys(self):
        first=self.call('apps',limit=32);self.assertEqual(first['total'],71)
        second=self.call('apps',inventory_id=first['inventory_id'],start=first['next_start'],limit=32)
        self.assertEqual(second['start'],32);self.assertTrue(second['other_apps_discoverable'])
        search=self.call('apps',inventory_id=first['inventory_id'],query='surface')
        self.assertEqual(search['total'],1);self.assertEqual(search['full_inventory_count'],71)
        self.assertEqual(self.call('apps',command='anything')['status'],'refused')
    def test_retained_results_keep_public_reference_without_inventory_history(self):
        # Any one of these results can outlive the original windows message.
        region=self.call('inspect',snapshot_id=self.sid,operation='overview')
        listing=self.call('inspect',snapshot_id=self.sid,operation='list')
        detail=self.call('inspect',snapshot_id=self.sid,operation='control',control_id=self.sid+':1')
        scope=self.review_text(complete=True)
        review=self.tools.evidence['events'][-1]['result']
        for retained in (region,listing,detail,review):
            self.assertEqual(retained['window_id'],self.window)
            self.assertIn('native metadata',retained['reference_note'])
        for operation in ('exploration','controls'):
            retained=self.call('status',operation=operation)
            self.assertTrue(retained['items'])
            self.assertTrue(all(item['window_id']==self.window for item in retained['items']))
        before=len(self.desktop.executions)
        refreshed=self.call('observe',window_id=review['window_id'])
        self.assertEqual(refreshed['status'],'observed')
        self.assertEqual(refreshed['window_id'],self.window)
        self.assertNotEqual(refreshed['snapshot_id'],self.sid)
        self.assertEqual(len(self.desktop.executions),before)
        self.assertEqual(self.tools._scopes[scope]['status'],'approved')
        stale=self.call('act',scope_id=scope,snapshot_id=self.sid,action_id='action:'+self.sid+':1',value='exact')
        self.assertEqual(stale['status'],'refused')
        self.assertIn('superseded',stale['reason'])
        self.assertEqual(len(self.desktop.executions),before)

    def test_native_window_number_is_refused_with_public_reference_recovery(self):
        before=len(self.desktop.calls)
        result=self.call('observe',window_id='52')
        self.assertEqual(result['status'],'refused')
        self.assertIn('locua_status operation=windows',result['reason'])
        self.assertEqual(len(self.desktop.calls),before)
        recovered=self.call('status',operation='windows')['items'][0]
        self.assertEqual(recovered['window_id'],self.window)
        self.assertEqual(recovered['target']['window_id'],52)

    def test_multiple_inventory_aliases_keep_one_usable_exact_target_reference(self):
        newer=self.tools._windows_result('new-app-inventory',self.desktop.app_windows({}))['windows'][0]['window_id']
        self.assertNotEqual(newer,self.window)
        retained=self.call('inspect',snapshot_id=self.sid,operation='overview')
        self.assertEqual(retained['window_id'],newer)
        self.assertEqual(self.tools._window(newer),self.tools._window(self.window))

    def test_reference_enrichment_cannot_turn_bad_argument_types_into_exception(self):
        for value in ([],{},12):
            result=self.call('inspect',snapshot_id=value,operation='list')
            self.assertEqual(result['status'],'refused')
            self.assertNotIn('window_id',result)
    def test_schema_error_names_unknown_missing_and_accepted_arguments(self):
        before=len(self.desktop.calls)
        error=self.call('inspect',snapshot_id=self.sid,window_id=self.window,arbitrary='ignored?')
        self.assertEqual(error['status'],'refused')
        self.assertCountEqual(error['unknown_arguments'],['arbitrary','window_id'])
        self.assertEqual(error['missing_required_arguments'],['operation'])
        self.assertIn('region_id',error['accepted_arguments']);self.assertNotIn('window_id',error['accepted_arguments'])
        self.assertIn('window_id',error['reason']);self.assertIn('operation',error['reason'])
        self.assertFalse(error['arguments_rewritten']);self.assertEqual(len(self.desktop.calls),before)
        repaired=self.call('inspect',snapshot_id=self.sid,operation='control',control_id=self.item('Entry')['control']['id'])
        self.assertEqual(repaired['status'],'ok')
    def test_projected_region_pagination_preserves_every_control_and_competitor(self):
        overview=self.call('inspect',snapshot_id=self.sid,operation='overview',limit=3)
        region=overview['items'][0]['region_id'];cursor=None;seen=[]
        while True:
            args={'snapshot_id':self.sid,'operation':'list','region_id':region,'limit':3}
            if cursor:args['cursor']=cursor
            result=self.call('inspect',**args)
            self.assertEqual(result['version'],'progressive-ui-v1')
            self.assertFalse(result['representation']['items_clipped'])
            self.assertFalse(result['provenance']['authorizes_actions'])
            for row in exposed_controls(result):
                seen.append(row['id'])
                self.assertIn('membership',row);self.assertIn('region_id',row)
                original=next(c for c in self.tools._observations[self.sid]['controls'] if c['id']==row['id'])
                actions=[a for a in self.tools._actions[self.sid].values() if a['control_id']==row['id']]
                self.assertEqual([a['id'] for a in row['actions']],[a['id'] for a in actions])
                self.assertEqual(self.details(row['id'])['semantics'],original['semantics'])
            cursor=result['coverage']['continuation']
            if not cursor:break
        expected=[c['id'] for c in self.tools._observations[self.sid]['controls']]
        self.assertCountEqual(seen,expected)
        self.assertIn(self.item('Competitor')['control']['id'],seen)
    def test_projection_keeps_exact_editor_limits_and_private_original_evidence(self):
        control=deepcopy(self.item('Entry')['control'])
        control.update(value='  00019\r\nΩ  ',states={'enabled':True,'focused':False,'value_settable':None})
        control['value_evidence']={'kind':'native_raw_editor_value','precision':'exact','exact_value_proven':True,
            'plane':'editor_buffer','atomic_capture':False,'saved_output_proven':False,
            'contract':{'source_revision':'source-version','proof':'full private proof'}}
        control['editor']={'plane':'editor_buffer','coherence':{'value_stable':True,'focus_stable':True},
            'raw_value':{'status':'ok','value':control['value']},'raw_value_recheck':{'status':'ok','value':control['value']},
            'focused':{'status':'ok','value':False},'value_settable':{'status':'unavailable','value':None},
            'selected_range':{'status':'ok','value':{'location':0,'length':0,'unit':'utf16'}},
            'selected_text':{'status':'unavailable','value':None},'saved_file_proven':False}
        raw={'status':'ok','snapshot_id':self.sid,'control':control,'actions':[{'id':'unchanged','kind':'set_text'}]}
        original=deepcopy(raw);visible=self.tools._record('locua_inspect',{},raw)
        self.assertEqual(raw,original);self.assertEqual(visible['control']['value'],control['value'])
        self.assertEqual(visible['control']['states'],control['states'])
        self.assertEqual(visible['control']['value_evidence']['precision'],'exact')
        self.assertFalse(visible['control']['value_evidence']['saved_output_proven'])
        self.assertEqual(visible['control']['editor']['value_settable'],{'status':'unavailable','value':None})
        self.assertEqual(visible['control']['editor']['focused'],{'status':'ok','value':False})
        self.assertNotIn('contract',visible['control']['value_evidence'])
        reference=Path(visible['projection']['full_response_ref'].split('#')[0])
        retained=json.loads(reference.read_text())
        self.assertEqual(retained['result'],{**original,'window_id':self.window,
            'reference_note':visible['reference_note']})
        self.assertEqual(retained['model_result'],visible)
        self.assertEqual(reference.stat().st_mode & 0o777,0o600)
    def test_projection_reduces_global_metadata_without_changing_page_or_cursor(self):
        control=deepcopy(self.item('Competitor')['control']);cid=control['id']
        raw={'snapshot_id':self.sid,'items':[{'kind':'control','membership':'context','control':control,
             'actions':[{'id':'action:'+cid,'kind':'set_text'}]}],
             'coverage':{'matched_total':400,'returned_count':1,'remaining_count':399,'continuation':'exact-cursor'},
             'metadata':{'competing_control_ids':[cid], 'inspection_provenance':{
                 'all_control_region_memberships':{f'other-control-{i}':'region:other' for i in range(400)}|{cid:'region:competitor'},
                 'outside_control_ids':[f'other-control-{i}' for i in range(400)]}}}
        projected=model_projection(raw,full_response_ref='private.json#result')
        self.assertLess(len(json.dumps(projected)),len(json.dumps(raw))*.25)
        self.assertEqual(projected['coverage'],raw['coverage'])
        self.assertEqual(projected['items'][0]['control'],raw['items'][0]['control'])
        self.assertEqual(projected['items'][0]['actions'],raw['items'][0]['actions'])
        self.assertEqual(projected['items'][0]['region_id'],'region:competitor')
        self.assertTrue(projected['items'][0]['competing_control'])
    def test_compact_refs_keep_exact_private_dispatch_and_expire_with_snapshot(self):
        scope=self.review_text('exact',complete=True,preserve=True)
        self.assertEqual(scope,'scope:1')
        item=self.item('Entry');public=item['actions'][0]['id']
        self.assertRegex(public,r'^action:1:[0-9]+$')
        result=self.call('act',scope_id=scope,snapshot_id=self.sid,action_id=public,value='exact')
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(self.desktop.executions[-1]['id'], 'action:'+self.desktop.executions[-1]['control_id'])
        self.assertNotEqual(self.desktop.executions[-1]['id'],public)
        stale=self.call('act',scope_id=scope,snapshot_id=result['snapshot_id'],action_id=public,value='exact')
        self.assertEqual(stale['status'],'refused')
        self.assertEqual(len(self.desktop.executions),1)

    def test_observation_and_inspection_retain_scoped_refs_without_authority(self):
        scope=self.review_text('exact',complete=True,preserve=True)
        r=self.call('observe',window_id=self.window)
        refs=r['retained_scope_refs']
        self.assertEqual(refs['items'][0]['scope_id'],scope)
        self.assertEqual(refs['items'][0]['status'],'approved')
        self.assertTrue(refs['retained_evidence_only'])
        self.assertFalse(refs['current_state_proven'])
        self.assertFalse(refs['action_authority_granted'])
        page=self.call('inspect',snapshot_id=r['snapshot_id'],operation='list',query='Entry')
        self.assertEqual(page['retained_scope_refs'],refs)
        self.assertEqual(self.desktop.executions,[])

    def test_public_reference_cannot_change_issued_private_control(self):
        scope=self.review_text('exact',complete=True,preserve=True)
        capture=self.tools._retain
        def corrupt(o):
            sid=capture(o)
            for a in self.tools._driver_actions[sid].values():
                if a.get('description')=='Entry':a['control_id']='wrong'
            return sid
        action=self.item('Entry')['actions'][0]['id']
        with patch.object(self.tools,'_retain',side_effect=corrupt):
            result=self.call('act',scope_id=scope,snapshot_id=self.sid,action_id=action,value='exact')
        self.assertEqual(result['status'],'refused')
        self.assertEqual(self.desktop.executions,[])

    def test_internal_sequence_guard_checks_new_capture_before_dispatch(self):
        scope=self.review_text('exact',complete=True,preserve=True)
        c=next(c for c in self.tools._observations[self.sid]['controls'] if c['name']=='Entry')
        a=next(a for a in self.tools._actions[self.sid].values() if a['control_id']==c['id'])
        checked=[]
        def stop(fresh):
            checked.append(fresh['snapshot_id'])
            return {'matched':False,'reason':'Observed dialog appeared'}
        result=self.tools._act(scope,self.sid,a['id'],'exact',_pre_dispatch=stop)
        self.assertEqual(result['status'],'refused')
        self.assertEqual(result['code'],'sequence_fresh_state_changed')
        self.assertFalse(result['action_started'])
        self.assertNotEqual(checked,[self.sid])
        self.assertEqual(checked,[result['snapshot_id']])
        self.assertEqual(self.desktop.executions,[])
        self.assertEqual(self.tools._scopes[scope]['status'],'approved')

    def test_standard_tools_have_schema_and_no_fixed_discovery_sequence(self):
        self.assertEqual(len(self.tools.tools()),12)
        self.assertTrue(all(t.input_schema['additionalProperties'] is False for t in self.tools.tools()))
        self.assertEqual(self.call('inspect',snapshot_id=self.sid,operation='list',query='Entry')['status'],'ok')
        self.assertEqual(self.call('launch',app_id=self.app)['status'],'launched')
        self.assertEqual(self.call('activate',window_id=self.window)['status'],'activated')
        self.assertEqual(self.call('inspect',snapshot_id=self.sid,operation='list')['status'],'refused')
    def test_status_recovers_exact_request_scope_and_latest_capture_without_read_or_authority(self):
        literal=' 0017\r\nΩ  ';scope=self.review_text(literal,complete=True,preserve=True)
        before=deepcopy(self.desktop.calls);review_count=len(self.reviews)
        result=self.call('status')
        self.assertEqual(result['original_request'],self.tools.request)
        self.assertEqual(result['latest_target']['window_id'],self.window)
        self.assertEqual(result['latest_target']['snapshot_id'],self.sid)
        self.assertEqual(result['latest_target']['region_discovery']['operation'],'overview')
        row=result['items'][0];self.assertEqual(row['scope_id'],scope)
        self.assertEqual(row['goals'][0]['value'],literal)
        self.assertEqual(row['preserves'],[{'target':'Keep','property':'checked','value':True}])
        self.assertEqual(row['verification']['validity'],'invalidated_or_unverified')
        self.assertFalse(result['action_authority_granted']);self.assertFalse(result['task_complete'])
        self.assertFalse(result['current_state_proven']);self.assertEqual(self.desktop.calls,before)
        self.assertEqual(len(self.reviews),review_count)
        result['items'][0]['goals'][0]['value']='mutated response'
        self.assertEqual(self.call('status')['items'][0]['goals'][0]['value'],literal)
    def test_status_after_action_verify_and_invalidation_never_uses_old_success(self):
        scope=self.review_text();action=self.act(scope,'Entry','exact')
        result=self.call('status');self.assertEqual(result['latest_target']['snapshot_id'],action['snapshot_id'])
        self.assertEqual(result['items'][0]['verification']['validity'],'invalidated_or_unverified')
        self.assertEqual(self.call('verify',scope_id=scope)['status'],'verified')
        before=len(self.desktop.calls);result=self.call('status');proof=result['items'][0]['verification']
        self.assertEqual(proof['validity'],'retained_for_latest_capture')
        self.assertTrue(proof['all_reviewed_goals_matched'])
        self.assertFalse(proof['current_state_proven']);self.assertFalse(proof['fresh_readback_performed_now'])
        self.assertEqual(len(self.desktop.calls),before)
        self.call('observe',window_id=self.window)
        self.assertEqual(self.call('status')['items'][0]['verification']['validity'],'invalidated_or_unverified')
        self.desktop.unavailable=True;self.call('observe',window_id=self.window)
        self.assertIsNone(self.call('status')['latest_target']['snapshot_id'])
    def test_status_pages_all_scopes_and_details_without_hiding_competitors(self):
        ids=[self.review_text(str(n)) for n in range(3)]
        first=self.call('status',limit=2);second=self.call('status',start=first['next_start'],limit=2)
        self.assertEqual([x['scope_id'] for x in first['items']+second['items']],ids)
        self.assertEqual(first['total'],3);self.assertEqual(first['omitted'],1)
        goals=self.call('status',operation='goals',scope_id=ids[1])
        self.assertEqual(goals['items'][0]['goal']['value'],'1')
        self.assertEqual(goals['items'][0]['observed_binding']['name'],'Entry')
        self.assertEqual(self.call('status',operation='effects',scope_id=ids[1])['items'][0]['goal_id'],'entry')
        self.assertEqual(self.call('status',operation='goals',scope_id='invented')['status'],'refused')
        self.assertEqual(self.call('status',operation='summary',scope_id=ids[0])['status'],'refused')
        self.assertEqual(self.call('status',limit=65)['status'],'refused')
        self.assertTrue(self.call('status')['all_regions_discoverable'])
        self.assertIsNotNone(self.item('Competitor'))
    def test_status_after_cancel_is_read_only_and_does_not_revive_tools(self):
        scope=self.review_text();self.answer=''
        self.call('review',snapshot_id=self.sid,summary='Another scope',goals=[],effects=[{
            'kind':'press','control_id':self.item('Hide panel')['control']['id'],'purpose':'Navigate'}])
        before=len(self.desktop.calls);result=self.call('status')
        self.assertEqual(result['status'],'ok');self.assertEqual(result['task_state'],'canceled')
        self.assertEqual(result['cancellation']['reason'],'user_declined_review')
        self.assertEqual(result['items'][0]['scope_id'],scope)
        self.assertEqual(result['items'][0]['verification']['validity'],'invalidated_or_unverified')
        self.assertFalse(result['action_authority_granted']);self.assertFalse(result['task_complete'])
        self.assertEqual(self.call('act',scope_id=scope,snapshot_id=self.sid,action_id='any')['status'],'canceled')
        self.assertEqual(self.call('observe',window_id=self.window)['status'],'canceled')
        self.assertEqual(len(self.desktop.calls),before)
    def test_status_reports_uncertain_scope_and_unissued_action_cannot_be_replayed(self):
        scope=self.review_text();self.desktop.uncertain=True
        self.assertEqual(self.act(scope,'Entry','exact')['status'],'uncertain')
        before=len(self.desktop.executions);result=self.call('status')
        self.assertEqual(result['task_state'],'blocked')
        self.assertEqual(result['items'][0]['status'],'blocked_uncertain')
        self.assertIsNone(result['latest_target']['snapshot_id'])
        self.assertEqual(self.call('act',scope_id=scope,snapshot_id=self.sid,action_id='any',value='exact')['status'],'refused')
        self.assertEqual(len(self.desktop.executions),before)
    def test_status_recovers_after_malformed_calls_and_retains_clarification(self):
        self.call('observe',window_id={'not':'an id'})
        self.tools.call('locua_inspect',[])
        self.answer='Use the second displayed record.'
        self.call('clarify',question='Which record?',reason='Two observed records have the same label.')
        result=self.call('status',operation='clarifications')
        self.assertEqual(result['items'][0]['answer'],self.answer)
        self.assertEqual(result['original_request'],self.tools.request)
        self.assertFalse(result['items'][0]['action_authority_granted'])
        self.assertEqual(result['latest_target']['snapshot_id'],self.sid)
    def test_region_scoped_search_is_honest_and_cursor_cannot_change_scope(self):
        # Generic groups with identical labels in different regions. No model,
        # application name, expected value or desired action selects the group.
        o=deepcopy(self.tools._observations[self.sid]);o['snapshot_id']='scoped'
        groups=[]
        for group in ('First','Second'):
            gid='group:'+group
            groups.append({'id':gid,'role':'AXGroup','name':group,'value':None,'parent':o['controls'][0]['id'],
                           'semantics':{},'states':{},'actions':[]})
            for n in range(2):
                c=deepcopy(o['controls'][1]);c.update(id=gid+str(n),name='Repeated',parent=gid)
                groups.append(c)
        o['controls']+=groups;sid=self.tools._retain(o)
        regions=self.call('inspect',snapshot_id=sid,operation='overview')['items']
        first=next(r['region_id'] for r in regions if r.get('label')=='First')
        second=next(r['region_id'] for r in regions if r.get('label')=='Second')
        whole=self.call('inspect',snapshot_id=sid,operation='list',query='Repeated')
        self.assertEqual(whole['coverage']['matched_total'],4)
        page=self.call('inspect',snapshot_id=sid,operation='list',region_id=first,query='Repeated',limit=1)
        self.assertEqual(page['coverage']['scope']['region_id'],first)
        # Region context intentionally retains same-name competitors elsewhere.
        self.assertEqual(page['coverage']['matched_total'],4)
        self.assertEqual(exposed_controls(page)[0]['region_id'],first)
        cursor=page['coverage']['continuation'];self.assertIsNotNone(cursor)
        self.assertEqual(self.call('inspect',snapshot_id=sid,operation='list',region_id=second,
                                  query='Repeated',limit=1,cursor=cursor)['status'],'refused')
        rest=self.call('inspect',snapshot_id=sid,operation='list',region_id=first,
                       query='Repeated',limit=1,cursor=cursor)
        self.assertEqual(exposed_controls(rest)[0]['region_id'],first)
        self.assertFalse(rest['coverage']['uniqueness_proven'])
        full=self.call('inspect',snapshot_id=sid,operation='list',region_id=first,query='Repeated',limit=64)
        self.assertEqual([r['membership'] for r in exposed_controls(full)],['primary','primary','context','context'])
        self.assertEqual([r['region_id'] for r in exposed_controls(full)[-2:]],[second,second])
        self.assertEqual(self.call('inspect',snapshot_id=sid,operation='list',region_id='made-up',
                                  query='Repeated')['status'],'refused')
    def large_observation(self,count=90,groups=False):
        o=deepcopy(self.tools._observations[self.sid]);o['snapshot_id']='large'
        root=deepcopy(o['controls'][0]);template=deepcopy(o['controls'][1]);o['controls']=[root]
        for n in range(count):
            parent=root['id']
            if groups:
                parent='group:'+str(n)
                o['controls'].append({'id':parent,'role':'AXGroup','name':str(n)+' Panel '+('label '*60),
                    'value':None,'parent':root['id'],'semantics':{},'states':{},'actions':[]})
            c=deepcopy(template);c.update(id='large:'+str(n),name='Repeated '+str(n),parent=parent,
                value=(' Ω\r\n  '+str(n)+'  ')*(300 if n==44 else 65))
            c['semantics']={'identifier':'field-'+str(n)};o['controls'].append(c)
        return o
    def assert_byte_bound(self,result):
        self.assertLessEqual(self.tools._response_bytes(result),MODEL_RESPONSE_BYTES)
    def test_byte_bounded_region_pages_keep_every_exact_item_across_chunk_and_limit_changes(self):
        o=self.large_observation();sid=self.tools._retain(o)
        region=self.call('inspect',snapshot_id=sid,operation='overview')['items'][0]['region_id']
        expected=source_inspect(o,region,limit=256)['items'];seen=[];cursor=None;limits=[]
        calls=len(self.desktop.calls)
        while True:
            limit=64 if len(seen)%2==0 else 17
            args={'snapshot_id':sid,'operation':'list','region_id':region,'limit':limit}
            if cursor is not None:args['cursor']=cursor
            page=self.call('inspect',**args)
            self.assertEqual(page['status'],'ok',page);self.assert_byte_bound(page)
            self.assertEqual(page['coverage']['previous_page_count'],len(seen))
            self.assertEqual(page['coverage']['returned_count'],len(page['items']))
            self.assertLessEqual(len(page['items']),limit);limits.append(len(page['items']))
            seen+=exposed_controls(page);cursor=page['coverage']['continuation']
            self.assertEqual(page['coverage']['remaining_count'],len(expected)-len(seen))
            if not cursor:break
        self.assertLess(min(limits),64);self.assertGreater(len(limits),1)
        self.assertEqual([r['id'] for r in seen],[r['control']['id'] for r in expected])
        self.assertTrue(any(isinstance(r['value'],dict) and r['value'].get('deferred') for r in seen))
        exact=[self.details(r['id'],sid)['value'] if isinstance(r['value'],dict) and r['value'].get('deferred') else r['value'] for r in seen]
        self.assertEqual(exact,[r['control']['value'] for r in expected])
        self.assertFalse(page['coverage']['uniqueness_proven']);self.assertFalse(page['coverage']['negative_evidence_proven'])
        self.assertEqual(len(self.desktop.calls),calls)
    def test_byte_bounded_search_pages_preserve_competing_regions_and_stale_binding(self):
        o=self.large_observation(80);root=o['controls'][0]
        for group in ('A','B'):
            o['controls'].append({'id':'group:'+group,'role':'AXGroup','name':'Group '+group,'value':None,
                'parent':root['id'],'semantics':{},'states':{},'actions':[]})
        for n,c in enumerate(o['controls'][1:81]):
            c['name']='Repeated';c['parent']='group:'+('A' if n<40 else 'B')
        sid=self.tools._retain(o)
        region=next(r['region_id'] for r in self.call('inspect',snapshot_id=sid,operation='overview')['items'] if r.get('label')=='Group A')
        seen=[];cursor=None;first_cursor=None
        while True:
            args={'snapshot_id':sid,'operation':'list','region_id':region,'query':'Repeated','limit':64}
            if cursor is not None:args['cursor']=cursor
            page=self.call('inspect',**args)
            self.assertEqual(page['status'],'ok',page);self.assert_byte_bound(page)
            seen+=exposed_controls(page);cursor=page['coverage']['continuation'];first_cursor=first_cursor or cursor
            if not cursor:break
        self.assertEqual(len(seen),80)
        self.assertEqual(sum(r['membership']=='primary' for r in seen),40)
        self.assertEqual(sum(r['membership']=='context' for r in seen),40)
        self.assertEqual(self.call('inspect',snapshot_id=sid,operation='list',region_id=region,
            query='different query',cursor=first_cursor,limit=64)['status'],'refused')
        newer=deepcopy(o);newer['snapshot_id']='newer';self.tools._retain(newer)
        self.assertEqual(self.call('inspect',snapshot_id='newer',operation='list',region_id=region,
            query='Repeated',cursor=first_cursor,limit=64)['status'],'refused')
    def test_byte_bounded_all_controls_updates_actual_continuation(self):
        o=self.large_observation();sid=self.tools._retain(o);seen=[];cursor=None
        while True:
            args={'snapshot_id':sid,'operation':'list','limit':64}
            if cursor is not None:args['cursor']=cursor
            result=self.call('inspect',**args)
            self.assertEqual(result['status'],'ok');self.assert_byte_bound(result)
            self.assertEqual(result['coverage']['previous_page_count'],len(seen));seen+=exposed_controls(result)
            self.assertEqual(self.tools.evidence['events'][-1]['result'],
                             {k:v for k,v in result.items() if k!='exploration_feedback'})
            cursor=result['coverage']['continuation']
            if cursor is None:break
        self.assertEqual([x['id'] for x in seen],[c['id'] for c in o['controls']])
    def test_byte_bounded_observe_overview_continues_without_another_capture(self):
        o=self.large_observation(80,groups=True)
        with patch.object(self.desktop,'observe',return_value={'status':'observed','observation':o}) as observe:
            first=self.call('observe',window_id=self.window)
        self.assertEqual(observe.call_count,1);self.assertEqual(first['status'],'observed');self.assert_byte_bound(first)
        pages=[first['overview']];cursor=pages[0]['coverage']['continuation']
        self.assertIsNotNone(cursor)
        before=len(self.desktop.calls)
        while cursor:
            result=self.call('inspect',snapshot_id=o['snapshot_id'],operation='overview',cursor=cursor,limit=64)
            self.assertEqual(result['status'],'ok');self.assert_byte_bound(result)
            pages.append(result);cursor=result['coverage']['continuation']
        ids=[r['region_id'] for page in pages for r in page['items'] if r['kind']=='region']
        self.assertEqual(len(ids),81);self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(len(self.desktop.calls),before)
    def test_oversized_exact_item_uses_deferred_value_and_lossless_detail_paging(self):
        o=self.large_observation(1);o['controls'][1]['value']='  Ω\r\n'*10000
        sid=self.tools._retain(o)
        result=self.call('inspect',snapshot_id=sid,operation='list')
        row=next(r for r in exposed_controls(result) if r['id']==o['controls'][1]['id'])
        self.assertEqual(result['status'],'ok');self.assertTrue(row['value']['deferred'])
        self.assert_byte_bound(result);self.assertEqual(result['snapshot_id'],sid)
        self.assertFalse(row['value']['exact_value_in_this_view'])
        self.assertFalse(result['coverage']['negative_evidence_proven'])
        self.assertEqual(self.details(o['controls'][1]['id'],sid)['value'],o['controls'][1]['value'])
        self.assertEqual(self.tools._observations[sid]['controls'][1]['value'],o['controls'][1]['value'])
        self.assertFalse(self.desktop.executions)
    def test_byte_bounded_status_pages_preserve_exact_goals_and_no_authority(self):
        literal='quoted \\" value '+(' Ω\r\n '*35)
        scopes=[self.review_text(literal+str(n)) for n in range(12)]
        before=len(self.desktop.calls);rows=[];start=0
        while True:
            result=self.call('status',start=start,limit=64);self.assert_byte_bound(result)
            self.assertEqual(result['status'],'ok');self.assertFalse(result['action_authority_granted'])
            rows+=result['items'];start=result['next_start']
            if start is None:break
        self.assertEqual([r['scope_id'] for r in rows],scopes)
        self.assertEqual([r['goals'][0]['value'] for r in rows],[literal+str(n) for n in range(12)])
        self.assertEqual(len(self.desktop.calls),before)
    def test_unpageable_post_operation_overflow_preserves_ack_and_never_replays(self):
        scope=self.review_text();original=self.desktop.execute
        def large_ack(*args):
            result=original(*args);result['driver_ack']['unbounded_detail']='x'*20000;return result
        with patch.object(self.desktop,'execute',side_effect=large_ack):result=self.act(scope,'Entry','exact')
        self.assertEqual(result['status'],'unavailable');self.assertEqual(result['operation_status'],'verified')
        self.assertTrue(result['action_started']);self.assertTrue(result['do_not_repeat_operation'])
        self.assertTrue(result['retained_readback_available']);self.assertEqual(len(self.desktop.executions),1)
        self.assert_byte_bound(result);self.assertEqual(self.tools.evidence['events'][-1]['result']['driver_ack']['unbounded_detail'],'x'*20000)
    def test_human_review_original_request_and_exact_literal_precede_input(self):
        scope=self.review_text(' 0017\r\nΩ  ',complete=True)
        self.assertIn(self.tools.request,self.reviews[-1]);self.assertIn('entire original request',self.reviews[-1])
        self.assertFalse(self.desktop.executions)
        result=self.act(scope,'Entry',' 0017\r\nΩ  ')
        self.assertEqual(result['status'],'verified',result);self.assertEqual(self.desktop.value,' 0017\r\nΩ  ')
        self.assertFalse(result['task_complete'])
    def test_out_of_scope_competitor_and_changed_literal_never_dispatch(self):
        scope=self.review_text()
        self.assertEqual(self.act(scope,'Competitor','exact')['status'],'refused')
        # Even a refused selection may obtain a read-only fresh capture, which
        # supersedes the previous action catalog without sending input.
        self.sid=self.call('observe',window_id=self.window)['snapshot_id']
        self.assertEqual(self.act(scope,'Entry','wrong')['status'],'refused');self.assertFalse(self.desktop.executions)
    def test_declined_scope_cannot_act(self):
        self.answer='';entry=self.item('Entry')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Entry',goals=[{'id':'e','kind':'text','target':'Entry',
                    'control_id':entry['id'],'value':'x','evidence_plane':'editor_buffer'}],effects=[{'kind':'goal','goal_id':'e'}])
        self.assertEqual(r['status'],'canceled')
        self.assertEqual(self.call('act',scope_id=r['scope_id'],snapshot_id=self.sid,action_id='any',value='x')['status'],'canceled')
        self.assertFalse(self.desktop.executions)
    def test_declined_review_latches_task_cancellation_without_reprompt_or_dispatch(self):
        self.answer='';entry=self.item('Entry')['control']
        args={'snapshot_id':self.sid,'summary':'Entry','goals':[{'id':'e','kind':'text','target':'Entry',
              'control_id':entry['id'],'value':'x','evidence_plane':'editor_buffer'}],
              'effects':[{'kind':'goal','goal_id':'e'}]}
        first=self.call('review',**args);self.answer='run';second=self.call('review',**args)
        self.assertEqual((first['status'],second['status']),('canceled','canceled'))
        self.assertEqual(first['scope_id'],second['scope_id']);self.assertEqual(len(self.reviews),1)
        self.assertEqual(len(list(self.tools.out.glob('review-*.json'))),1)
        before=len(self.desktop.calls)
        self.assertEqual(self.call('observe',window_id=self.window)['status'],'canceled')
        self.assertEqual(self.call('launch',app_id=self.app)['status'],'canceled')
        self.assertEqual(self.call('activate',window_id=self.window)['status'],'canceled')
        self.assertEqual(self.tools.finalize()['status'],'canceled')
        self.assertEqual(len(self.desktop.calls),before)
        self.assertFalse(self.desktop.executions)
    def test_repeated_evidence_replaces_symlink_entry_without_following_it(self):
        victim=Path(self.tmp.name)/'unrelated';victim.write_text('keep')
        evidence=self.tools.out/'evidence.json';evidence.unlink();evidence.symlink_to(victim)
        self.assertEqual(self.call('apps',query='surface')['status'],'ok')
        self.assertFalse(evidence.is_symlink());self.assertEqual(victim.read_text(),'keep')
        self.assertEqual(evidence.stat().st_mode & 0o777,0o600)
    def test_partial_navigation_can_be_reviewed_before_any_final_binding(self):
        c=self.item('Hide panel')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Navigate',goals=[],effects=[{'kind':'press','control_id':c['id'],'purpose':'Open requested area'}])
        self.assertEqual(r['status'],'approved')
        result=self.act(r['scope_id'],'Hide panel');self.assertEqual(result['status'],'dispatched')
        self.assertEqual(self.tools.finalize()['status'],'blocked')
    def test_reviewed_navigation_press_is_one_issuance(self):
        c=self.item('Hide panel')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Navigate',goals=[],effects=[{
            'kind':'press','control_id':c['id'],'purpose':'Open requested area'}])
        self.assertIn('Press once:',self.reviews[-1])
        first=self.act(r['scope_id'],'Hide panel');self.assertEqual(first['status'],'dispatched')
        again=self.act(r['scope_id'],'Hide panel',sid=first['snapshot_id'])
        self.assertEqual(again['status'],'refused');self.assertEqual(len(self.desktop.executions),1)
    def test_refused_navigation_issuance_requires_a_new_review(self):
        c=self.item('Hide panel')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Navigate',goals=[],effects=[{
            'kind':'press','control_id':c['id'],'purpose':'Open requested area'}])
        with patch.object(self.desktop,'execute',return_value={'status':'refused','action_started':False}) as execute:
            self.assertEqual(self.act(r['scope_id'],'Hide panel')['status'],'refused')
            self.sid=self.call('observe',window_id=self.window)['snapshot_id']
            self.assertEqual(self.act(r['scope_id'],'Hide panel')['status'],'refused')
            self.assertEqual(execute.call_count,1)
    def test_final_refresh_includes_preservation_only_navigation_scope(self):
        c=self.item('Hide panel')['control'];keep=self.item('Keep')['control']
        navigation=self.call('review',snapshot_id=self.sid,summary='Navigate',goals=[],effects=[{
            'kind':'press','control_id':c['id'],'purpose':'Open requested area'}],
            preserves=[{'control_id':keep['id'],'property':'checked','value':True}])
        first=self.act(navigation['scope_id'],'Hide panel');self.sid=first['snapshot_id']
        scope=self.review_text(complete=True);self.act(scope,'Entry','exact')
        self.assertEqual(self.tools.finalize()['status'],'verified_reviewed_scope')
        self.desktop.checked=False
        final=self.tools.finalize();self.assertEqual(final['status'],'blocked')
        proof=final['verification']['scopes'][navigation['scope_id']]
        self.assertFalse(proof['all_preservation_predicates_matched'])
        self.assertFalse(proof['preserves'][0]['matched'])
        self.assertFalse(proof['all_reviewed_goals_matched'])
    def test_preservation_only_scope_cannot_supply_completion(self):
        c=self.item('Hide panel')['control'];keep=self.item('Keep')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Navigate',goals=[],effects=[{
            'kind':'press','control_id':c['id'],'purpose':'Open requested area'}],
            preserves=[{'control_id':keep['id'],'property':'checked','value':True}])
        final=self.tools.finalize();self.assertEqual(final['status'],'blocked')
        proof=final['verification']['scopes'][r['scope_id']]
        self.assertTrue(proof['all_preservation_predicates_matched']);self.assertFalse(proof['all_reviewed_goals_matched'])
    def test_preservation_checked_before_dispatch_and_in_final_readback(self):
        scope=self.review_text(preserve=True);self.desktop.checked=False
        self.assertEqual(self.act(scope,'Entry','exact')['status'],'refused');self.assertFalse(self.desktop.executions)
    def test_final_refresh_invalidates_old_success_and_missing_window(self):
        scope=self.review_text(complete=True);self.assertEqual(self.act(scope,'Entry','exact')['status'],'verified')
        self.assertEqual(self.tools.finalize()['status'],'verified_reviewed_scope')
        self.desktop.value='changed externally'
        self.assertEqual(self.tools.finalize()['status'],'blocked')
        self.desktop.value='exact';self.desktop.unavailable=True
        self.assertEqual(self.tools.finalize()['status'],'blocked')
    def test_matching_goal_scope_without_full_request_declaration_is_partial(self):
        scope=self.review_text();self.act(scope,'Entry','exact')
        result=self.tools.finalize();self.assertEqual(result['status'],'partial')
        self.assertFalse(result['request_coverage']['user_reviewed_complete_declaration'])
        self.assertFalse(result['saved_output_proven']);self.assertFalse(result['task_complete'])
    def test_uncertain_input_blocks_scope_and_final_success_without_replay(self):
        scope=self.review_text();self.desktop.uncertain=True
        action_id=self.item('Entry')['actions'][0]['id']
        self.assertEqual(self.act(scope,'Entry','exact')['status'],'uncertain')
        self.desktop.uncertain=False
        self.assertEqual(self.call('act',scope_id=scope,snapshot_id=self.sid,action_id=action_id,value='exact')['status'],'refused')
        self.assertEqual(len(self.desktop.executions),1);self.assertEqual(self.tools.finalize()['status'],'blocked')
    def test_foreign_snapshot_and_raw_argument_injection_refuse(self):
        scope=self.review_text();self.call('observe',window_id=self.window)
        self.assertEqual(self.call('act',scope_id=scope,snapshot_id=self.sid,action_id='invented',value='exact')['status'],'refused')
        self.assertEqual(self.call('act',scope_id=scope,snapshot_id=self.sid,action_id='invented',element_token='forged')['status'],'refused')
        self.assertFalse(self.desktop.executions)
    def test_arithmetic_capabilities_need_issuance_witness_and_bound_display(self):
        result=self.item('Result')['control']
        r=self.call('review',snapshot_id=self.sid,summary='Perform requested arithmetic',goals=[{
            'id':'calc','kind':'calculation','target':'Result','control_id':result['id'],'expression':'2+3','evidence_plane':'display'}],
            effects=[{'kind':'goal','goal_id':'calc'}])
        scope=r['scope_id'];self.assertEqual(self.act(scope,'Hide panel')['status'],'refused')
        self.desktop.display='5'
        self.assertEqual(self.call('verify',scope_id=scope,goal_id='calc')['status'],'unverified')
        sid=self.call('observe',window_id=self.window)['snapshot_id']
        for key in ('All Clear','2','+','3','='):
            action=self.act(scope,key,sid=sid);self.assertEqual(action['status'],'dispatched',action);sid=action['snapshot_id']
        final=self.call('verify',scope_id=scope,goal_id='calc')
        self.assertEqual(final['status'],'verified',final);self.assertFalse(final['task_complete'])
        before=len(self.desktop.calls);status=self.call('status')
        self.assertEqual(status['items'][0]['arithmetic_issuance']['issued_evaluation'],'2+3')
        self.assertTrue(status['items'][0]['verification']['all_reviewed_goals_matched'])
        first=self.call('status',operation='witness',scope_id=scope,limit=3)
        last=self.call('status',operation='witness',scope_id=scope,start=first['next_start'],limit=3)
        self.assertEqual([r['input'] for r in first['items']+last['items']],['clear','2','+','3','='])
        self.assertEqual(len(self.desktop.calls),before);self.assertFalse(status['task_complete'])
    def test_standard_execute_returns_errors_to_model_and_serializes_parallel_calls(self):
        tools={t.name:t for t in self.tools.tools()}
        class Result:
            def __init__(self,**kwargs):self.__dict__.update(kwargs)
        fake=SimpleNamespace(ToolResult=Result)
        async def run():
            return await asyncio.gather(tools['locua_inspect'].execute({'snapshot_id':'wrong','operation':'list'}),
                                        tools['locua_apps'].execute({'query':'surface'}))
        with patch.dict('sys.modules',{'amplifier_core.models':fake}):out=asyncio.run(run())
        self.assertFalse(out[0].success);self.assertEqual(out[0].output['status'],'refused')
        self.assertTrue(out[1].success);self.assertEqual(out[1].output['total'],1)
    def test_private_evidence_contains_full_observation_without_raw_handles_in_model_view(self):
        self.assertEqual((self.tools.out/'evidence.json').stat().st_mode&0o777,0o600)
        self.assertTrue(list(self.tools.out.glob('observation-*.json')))
        viewed=self.items();self.assertFalse(any('handle' in r for r in viewed))
    def test_retained_old_snapshot_is_inspectable_but_act_rebinds_full_signature(self):
        scope=self.review_text();o=self.tools._observations[self.sid]
        o['observed_at_ns']-=180_000_000_000;o['provenance']['observed_at_ns']=o['observed_at_ns']
        result=self.call('inspect',snapshot_id=self.sid,operation='list')
        self.assertEqual(result['status'],'ok');self.assertGreater(result['capture_age_seconds'],170)
        self.assertTrue(result['retained_observation_only']);self.assertTrue(result['fresh_capture_required_for_action'])
        action=next(x for x in exposed_controls(result) if x['name']=='Entry')['actions'][0]
        self.desktop.value='Changed while awaiting review'
        result=self.call('act',scope_id=scope,snapshot_id=self.sid,action_id=action['id'],value='exact')
        self.assertEqual(result['status'],'refused');self.assertFalse(self.desktop.executions)
    def test_old_semantic_choice_with_unchanged_fresh_state_can_execute(self):
        scope=self.review_text();o=self.tools._observations[self.sid]
        o['observed_at_ns']-=180_000_000_000;o['provenance']['observed_at_ns']=o['observed_at_ns']
        self.assertEqual(self.act(scope,'Entry','exact')['status'],'verified')
    def test_unexpected_exception_after_input_boundary_locks_scope(self):
        scope=self.review_text();action_id=self.item('Entry')['actions'][0]['id']
        with patch.object(self.desktop,'execute',side_effect=RuntimeError('lost response')):
            result=self.call('act',scope_id=scope,snapshot_id=self.sid,action_id=action_id,value='exact')
        self.assertEqual(result['status'],'uncertain');self.assertTrue(result['action_started'])
        self.assertEqual(self.tools.finalize()['status'],'blocked')
    def test_clarification_records_answer_without_approving_any_scope(self):
        self.answer='The left document'
        result=self.call('clarify',question='Which of the two observed documents?',reason='Both match the requested title')
        self.assertEqual(result['answer'],'The left document');self.assertFalse(result['action_authority_granted'])
        self.assertFalse(self.tools._scopes);self.assertFalse(self.desktop.executions)


if __name__=='__main__':unittest.main()
