"""Native capability boundaries using a synthetic Cua peer; no apps or models."""
from copy import deepcopy
from pathlib import Path
import json
import plistlib
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from locua.desktop_tools import DesktopTools, _bundle_executable, _process_identity
from locua.engine.prototype.cua import CuaRefusal
from locua.engine.prototype.native_driver_contract import CONTRACT_ID,SOURCE_FINGERPRINT

APP={'pid':0,'name':'Fixture','bundle_id':'test.locua.fixture','running':False,
     'active':False,'launch_path':'/Applications/Fixture.app','kind':'desktop','windows':[]}
TARGET={'pid':41,'window_id':52}


class Peer:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir()
        self.server={'serverInfo':{'name':'cua-driver','version':'0.28.2'}}
        self.inventory={'tools':[{'name':'set_value','inputSchema':{'type':'object','required':['pid','value'],
            'properties':{'pid':{'type':'integer'},'value':{'type':'string'},'element_token':{'type':'string'},
                          'snapshot_id':{'type':'string'},'element_index':{'type':'integer'},'window_id':{'type':'integer'}}}}]}
        self.calls=[];self.sequence=0;self.value='Initial';self.enabled=True;self.writable=True
        self.exact=True;self.duplicate=False;self.title='Fixture window';self.window_present=True
        self.value_label=False;self.editor_identifier=None;self.editor_frame=None
        self.next_error=None;self.mutation_error=None;self.after_error=None;self.reuse_snapshot=False
        self.pid=41;self.apps=[deepcopy(APP)];self.closed=0;self.ack={'effect':'unverifiable','route':'accessibility'}
        self.close_errors=[]
        self.activation_reply={'code':'bring_to_front_exact_window_verified','status':'activated',
            'activated':True,'process_activated':True,'request_accepted':True,**TARGET,
            'exact_window_effect':{'verified':True,'focused':True,'frontmost_ordinary':True,'target_visible_ordinary':True},
            'observed':{'frontmost_pid':41,'focused_window_id':52,'frontmost_ordinary_window_id':52}}
    def check(self):pass
    def close(self):self.closed+=1;return self.close_errors
    def snapshot(self):
        self.sequence+=0 if self.reuse_snapshot else 1;s=f's{self.sequence:08x}'
        editor={'contract':CONTRACT_ID,'plane':'editor_buffer','raw_value':{'status':'ok','value':self.value},
                'value_settable':{'status':'ok','value':self.writable},'coherence':{'value_stable':self.exact,'focus_stable':True},
                'focused':{'status':'ok','value':False}}
        nodes=[{'role':'AXWindow','label':self.title,'actions':['AXRaise']},
               {'role':'AXButton','label':'One','actions':['AXPress'],'enabled':self.enabled,'parent_index':0},
               {'role':'AXButton','label':'Two','actions':['AXPress'],'enabled':True,'parent_index':0},
               {'role':'AXMenuItem','label':'New','actions':['AXPress'],'enabled':True,'parent_index':0},
               {'role':'AXTextField','label':'Entry','value':self.value,'editor':editor,'enabled':True,'parent_index':0}]
        if self.value_label:nodes[4]['label']=self.value
        if self.editor_identifier:nodes[4]['identifier']=self.editor_identifier
        if self.editor_frame:nodes[4]['frame']=self.editor_frame
        if self.duplicate:nodes.append(deepcopy(nodes[1]))
        for i,n in enumerate(nodes):n.update(element_index=i,element_token=f'{s}:{i}')
        return {**TARGET,'pid':self.pid,'snapshot_id':s,'elements':nodes,'elements_complete':False,'tree_markdown':'',
                'native_editor_contract':{'id':CONTRACT_ID,'source_fingerprint_sha256':SOURCE_FINGERPRINT,
                 'platform':'macos','plane':'editor_buffer','handle_binding':'same_snapshot_element_token'}}
    def call(self,name,args,**kw):
        self.calls.append((name,deepcopy(args)))
        if name=='list_apps':raw={'apps':deepcopy(self.apps)}
        elif name=='list_windows':raw={'windows':[{'pid':self.pid,'window_id':52,'title':self.title,'is_on_screen':True}]
                                      if self.window_present else []}
        elif name=='launch_app':
            if self.mutation_error:raise self.mutation_error
            self.pid=77
            raw={'pid':77,'bundle_id':APP['bundle_id'],'name':'Fixture','windows':[],
                 'launch_state':{'requested':True,'process_running':True,'window_ready':False}}
        elif name=='get_window_state':
            if self.next_error:
                error=self.next_error;self.next_error=None;raise error
            raw=self.snapshot()
        elif name in ('click','set_value'):
            if self.mutation_error:raise self.mutation_error
            if name=='set_value':self.value=args['value']
            if self.after_error:self.next_error=self.after_error
            raw=deepcopy(self.ack)
        elif name=='bring_to_front':
            if self.mutation_error:raise self.mutation_error
            raw=deepcopy(self.activation_reply)
        else:raise AssertionError('Unexpected tool '+name)
        return {'request':{'name':name,'arguments':args},'response':{'result':{'structuredContent':raw}}},raw


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.peer=Peer(Path(self.tmp.name)/'peer')
        self.patch=patch('locua.engine_adapter.owner',return_value=self.peer);self.patch.start();self.addCleanup(self.patch.stop)
        self.tools=DesktopTools({},Path(self.tmp.name)/'out');self.addCleanup(self.tools.close)
    def capture(self):
        result=self.tools.observe(TARGET);self.assertEqual(result['status'],'observed');return result['observation']
    def action(self,o,kind='press',name='One'):
        rows=self.tools.actions(o);self.assertEqual(rows['status'],'ok')
        return next(x for x in rows['actions'] if x['kind']==kind and x['name']==name)
    def mutations(self):return [x for x in self.peer.calls if x[0] in ('click','set_value','launch_app')]

    def test_app_inventory_full_and_launch_is_explicit_bundle_only_with_returned_pid(self):
        apps=self.tools.apps();self.assertEqual(apps['apps'],[APP]);self.assertFalse(self.mutations())
        r=self.tools.launch(apps['apps'][0]);self.assertEqual(r['status'],'launched');self.assertEqual(r['app']['pid'],77)
        self.assertEqual(self.mutations(),[('launch_app',{'bundle_id':APP['bundle_id']})])
        self.assertEqual(self.peer.calls[-1],('list_windows',{'pid':77}));self.assertEqual(r['windows'][0]['pid'],77)
    def test_running_app_launch_is_not_silently_replaced_by_reuse(self):
        self.peer.apps[0].update(pid=41,running=True)
        record=self.tools.apps()['apps'][0];self.assertEqual(self.tools.launch(record)['status'],'launched')
        self.assertEqual(self.mutations()[0][0],'launch_app')
    def test_model_cannot_invent_launch_args_or_change_inventory_identity(self):
        a=self.tools.apps()['apps'][0];a['additional_arguments']=['--bad']
        self.assertEqual(self.tools.launch(a)['status'],'refused');self.assertFalse(self.mutations())
        a=self.tools.apps()['apps'][0];self.peer.apps[0]['launch_path']='/Other.app'
        self.assertEqual(self.tools.launch(a)['status'],'refused');self.assertFalse(self.mutations())
    def test_unknown_launch_effect_cannot_be_replayed(self):
        a=self.tools.apps()['apps'][0];self.peer.mutation_error=TimeoutError('transport timeout')
        self.assertEqual(self.tools.launch(a)['status'],'uncertain')
        self.assertEqual(self.tools.launch(a)['status'],'refused');self.assertEqual(len(self.mutations()),1)
    def test_unaddressable_and_offscreen_windows_are_retained(self):
        self.peer.window_present=False;r=self.tools.windows(41);self.assertEqual(r['status'],'ok');self.assertEqual(r['windows'],[])
        r=self.tools.observe(TARGET);self.assertEqual(r['status'],'refused');self.assertFalse(self.mutations())
    def test_full_observation_retained_and_all_supported_competitors_catalogued(self):
        o=self.capture();a=self.tools.actions(o)
        self.assertEqual(len(o['controls']),5);self.assertFalse(o['coverage']['complete'])
        self.assertEqual({(x['kind'],x['name']) for x in a['actions']},
                         {('press','One'),('press','Two'),('press','New'),('set_text','Entry')})
        self.assertTrue(all('element_token' not in x for x in a['actions']))
    def test_disabled_and_unknown_text_capabilities_never_promoted(self):
        self.peer.enabled=False;self.peer.writable=False;o=self.capture();a=self.tools.actions(o)['actions']
        self.assertNotIn('One',[x['name'] for x in a]);self.assertFalse(any(x['kind']=='set_text' for x in a))
        self.peer.writable=True;self.peer.exact=False;o=self.capture();r=self.tools.actions(o)
        self.assertFalse(any(x['kind']=='set_text' for x in r['actions']));self.assertEqual(r['unavailable'][0]['reason'],'exact_editor_readback_unproved')
    def test_press_uses_fresh_token_and_returns_post_state_without_completion_claim(self):
        o=self.capture();r=self.tools.execute(self.action(o),o)
        self.assertEqual(r['status'],'dispatched');self.assertFalse(r['task_complete']);self.assertFalse(r['verification']['task_complete'])
        self.assertEqual(self.mutations(),[('click',{'pid':41,'window_id':52,'element_token':'s00000002:1'})])
        self.assertEqual(r['observation']['snapshot_id'],'s00000003');self.assertFalse(r['verification']['saved_output_proven'])
    def test_exact_text_preserves_whitespace_unicode_and_only_proves_editor_plane(self):
        o=self.capture();a=self.action(o,'set_text','Entry');a['value']=' 00028\r\nΩ  '
        r=self.tools.execute(a,o);self.assertEqual(r['status'],'verified');self.assertEqual(self.peer.value,a['value'])
        self.assertEqual(r['verification']['plane'],'editor_buffer');self.assertFalse(r['verification']['committed_document_proven'])
        self.assertEqual(self.mutations()[0][0],'set_value')
    def test_foreign_modified_and_superseded_observations_refuse_without_input(self):
        o=self.capture();a=self.action(o);forged=deepcopy(o);forged['controls'][1]['name']='Changed'
        self.assertEqual(self.tools.execute(a,forged)['status'],'refused')
        self.capture();self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_descriptor_and_model_supplied_raw_arguments_are_rejected(self):
        o=self.capture();a=self.action(o);a['x']=10
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
        a=self.action(o);a['target']['window_id']=123
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_stale_capture_refused(self):
        o=self.capture();a=self.action(o)
        with patch('locua.engine.prototype.perception.time.time_ns',return_value=o['observed_at_ns']+121_000_000_000):
            self.assertEqual(self.tools.execute(a,o)['status'],'refused')
        self.assertFalse(self.mutations())
    def test_fresh_changed_ancestor_or_ambiguous_control_refuses(self):
        for field,value in [('title','Different document'),('duplicate',True),('enabled',False),('value','Changed')]:
            with self.subTest(field=field):
                setattr(self.peer,field,False if field=='duplicate' else True if field=='enabled' else 'Initial' if field=='value' else 'Fixture window')
                o=self.capture();a=self.action(o,'set_text','Entry') if field=='value' else self.action(o)
                if field=='value':a['value']='New'
                setattr(self.peer,field,value);self.assertEqual(self.tools.execute(a,o)['status'],'refused')
                setattr(self.peer,field,False if field=='duplicate' else True if field=='enabled' else 'Initial' if field=='value' else 'Fixture window')
        self.assertFalse(self.mutations())
    def test_disappeared_target_prevents_input_and_invalidates_old_authority(self):
        o=self.capture();a=self.action(o);self.peer.window_present=False
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.peer.window_present=True
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_driver_refusal_is_not_success_and_old_action_is_consumed(self):
        o=self.capture();a=self.action(o);self.peer.mutation_error=CuaRefusal({'effect':'refused','refusal':{'code':'permission_required'}})
        r=self.tools.execute(a,o);self.assertEqual(r['status'],'refused');self.assertEqual(r['code'],'permission_required')
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertEqual(len(self.mutations()),1)
    def test_timeout_consumes_action_and_blocks_further_same_target_mutation(self):
        o=self.capture();a=self.action(o);self.peer.mutation_error=TimeoutError('unknown delivery')
        self.assertEqual(self.tools.execute(a,o)['status'],'uncertain');self.peer.mutation_error=None
        o=self.capture();self.assertEqual(self.tools.execute(self.action(o),o)['status'],'refused');self.assertEqual(len(self.mutations()),1)
    def test_refusal_with_possible_input_blocks_new_action_even_after_fresh_observation(self):
        o=self.capture();a=self.action(o)
        self.peer.mutation_error=CuaRefusal({'effect':'refused','action_started':True,'refusal':{'code':'delivery_uncertain'}})
        self.assertEqual(self.tools.execute(a,o)['status'],'uncertain');self.peer.mutation_error=None
        o=self.capture();self.assertEqual(self.tools.execute(self.action(o),o)['status'],'refused')
        self.assertEqual(len(self.mutations()),1)
    def test_explicit_no_input_refusal_does_not_invent_uncertain_effect(self):
        o=self.capture();a=self.action(o)
        self.peer.mutation_error=CuaRefusal({'effect':'refused','action_started':False,'refusal':{'code':'permission_required'}})
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.peer.mutation_error=None
        o=self.capture();self.assertEqual(self.tools.execute(self.action(o),o)['status'],'dispatched')
        self.assertEqual(len(self.mutations()),2)
    def test_value_derived_editor_label_can_change_with_stable_identifier(self):
        self.peer.value_label=True;self.peer.editor_identifier='fixture-entry'
        o=self.capture();a=self.action(o,'set_text','Initial');a['value']='Changed'
        r=self.tools.execute(a,o);self.assertEqual(r['status'],'verified')
        self.assertTrue(r['verification']['exact_value_proven']);self.assertEqual(len(self.mutations()),1)
    def test_value_derived_editor_label_without_stable_evidence_blocks_before_write(self):
        self.peer.value_label=True;o=self.capture();a=self.action(o,'set_text','Initial');a['value']='Changed'
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_value_derived_editor_label_can_change_with_named_ancestor_and_geometry(self):
        self.peer.value_label=True;self.peer.editor_frame={'x':10,'y':20,'width':200,'height':80}
        o=self.capture();a=self.action(o,'set_text','Initial');a['value']='Changed'
        self.assertEqual(self.tools.execute(a,o)['status'],'verified');self.assertEqual(len(self.mutations()),1)
    def test_failed_post_readback_is_uncertain_not_retried(self):
        o=self.capture();self.peer.after_error=RuntimeError('AX surface unavailable')
        r=self.tools.execute(self.action(o),o);self.assertEqual(r['status'],'uncertain');self.assertEqual(r['code'],'post_observation_unavailable')
        self.assertEqual(len(self.mutations()),1)
    def test_degraded_observation_returns_typed_unavailable_and_no_old_capture(self):
        o=self.capture();a=self.action(o)
        from locua.engine.prototype.perception import NativeObservationUnavailable
        self.peer.next_error=NativeObservationUnavailable({'degraded_reason':'ax_window_unresolved','background_input':{'exact_window':{'status':'ax_unresolved'}}},TARGET,'a'*64)
        r=self.tools.observe(TARGET);self.assertEqual(r['status'],'unavailable');self.assertEqual(r['code'],'native_accessibility_unavailable')
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_same_snapshot_refresh_cannot_authorize_input(self):
        o=self.capture();a=self.action(o);self.peer.reuse_snapshot=True
        self.assertEqual(self.tools.execute(a,o)['status'],'refused');self.assertFalse(self.mutations())
    def test_close_reports_cleanup_errors_and_leaves_apps_open_idempotently(self):
        self.tools.apps();self.peer.close_errors=['session reconciliation failed'];r=self.tools.close()
        self.assertEqual(r['status'],'cleanup_failed');self.assertTrue(r['applications_left_open']);self.assertEqual(self.tools.close(),r);self.assertEqual(self.peer.closed,1)
        self.assertFalse(any(n=='kill_app' for n,_ in self.peer.calls))
    def test_trace_is_private_and_raw_full_observation_is_retained(self):
        o=self.capture();path=self.tools.out/'desktop.jsonl'
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        rows=[json.loads(l) for l in path.read_text().splitlines()];self.assertEqual(rows[-1]['result']['observation'],o)

    def install_fixture_bundle(self):
        root=Path(self.tmp.name)/'Installed Fixture.app';binary=root/'Contents'/'MacOS'/'Fixture'
        binary.parent.mkdir(parents=True);binary.write_bytes(b'synthetic executable, never run')
        with (root/'Contents'/'Info.plist').open('wb') as f:
            plistlib.dump({'CFBundleIdentifier':APP['bundle_id'],'CFBundleExecutable':'Fixture'},f)
        self.peer.apps[0]['launch_path']=str(root)
        return self.tools.apps()['apps'][0],binary
    def process(self,pid,binary,start='Thu Sep 17 23:50:00 2026'):
        return {'pid':pid,'started_at_utc':start,'executable_path':str(binary.resolve())}
    def test_app_windows_proves_executable_when_cached_app_says_closed(self):
        app,binary=self.install_fixture_bundle();before=deepcopy(app)
        with patch('locua.desktop_tools._process_identity',side_effect=lambda pid:self.process(pid,binary)) as probe:
            result=self.tools.app_windows(app)
        self.assertEqual(result['status'],'ok');self.assertEqual(len(result['windows']),1)
        self.assertFalse(result['raw_app']['running']);self.assertEqual(result['raw_app']['pid'],0)
        self.assertEqual(app,before);self.assertEqual(result['windows'][0]['pid'],41)
        evidence=result['windows'][0]['app_identity_evidence'];self.assertFalse(evidence['name_matching_used'])
        self.assertFalse(evidence['dispatch_authority']);self.assertEqual(probe.call_count,2)
        self.assertEqual(result['availability'],'owned_windows_observed');self.assertFalse(self.mutations())
    def test_app_windows_does_not_join_spoofed_title_or_same_executable_basename_elsewhere(self):
        app,binary=self.install_fixture_bundle();other=Path(self.tmp.name)/'Other.app'/'Contents'/'MacOS'/'Fixture'
        other.parent.mkdir(parents=True);other.write_bytes(b'not run')
        self.peer.title=app['name']
        with patch('locua.desktop_tools._process_identity',return_value=self.process(41,other)):
            result=self.tools.app_windows(app)
        self.assertEqual(result['windows'],[]);self.assertEqual(result['nonmatching_process_pids'],[41])
        self.assertEqual(result['availability'],'no_owned_windows_observed');self.assertFalse(self.mutations())
    def test_app_windows_helper_inside_bundle_does_not_impersonate_main_executable(self):
        app,binary=self.install_fixture_bundle();helper=binary.parent/'Helper';helper.write_bytes(b'not run')
        with patch('locua.desktop_tools._process_identity',return_value=self.process(41,helper)):
            self.assertEqual(self.tools.app_windows(app)['windows'],[])
    def test_app_windows_recycled_pid_and_unavailable_process_stay_unknown(self):
        app,binary=self.install_fixture_bundle()
        with patch('locua.desktop_tools._process_identity',side_effect=[self.process(41,binary),self.process(41,binary,'later')]):
            result=self.tools.app_windows(app)
        self.assertEqual(result['windows'],[]);self.assertEqual(result['availability'],'unknown')
        with patch('locua.desktop_tools._process_identity',side_effect=PermissionError('denied')):
            result=self.tools.app_windows(app)
        self.assertEqual(result['windows'],[]);self.assertFalse(result['all_listed_pids_resolved'])
    def test_app_windows_window_disappearance_after_process_read_is_reconciled(self):
        app,binary=self.install_fixture_bundle()
        with patch('locua.desktop_tools._process_identity',side_effect=lambda pid:self.process(pid,binary)),\
             patch.object(self.tools,'_windows',side_effect=[{'windows':[{'pid':41,'window_id':52}]},{'windows':[]}]):
            result=self.tools.app_windows(app)
        self.assertEqual(result['windows'],[]);self.assertFalse(self.mutations())
    def test_app_windows_foreign_or_changed_installed_identity_refused_before_process_probe(self):
        app,binary=self.install_fixture_bundle();app['launch_path']='/Other.app'
        with patch('locua.desktop_tools._process_identity') as probe:
            self.assertEqual(self.tools.app_windows(app)['status'],'refused');probe.assert_not_called()
        app=self.tools.apps()['apps'][0];self.peer.apps[0]['bundle_id']='changed'
        with patch('locua.desktop_tools._process_identity') as probe:
            self.assertEqual(self.tools.app_windows(app)['status'],'refused');probe.assert_not_called()
    def test_bundle_metadata_must_match_and_executable_must_stay_inside_bundle(self):
        app,binary=self.install_fixture_bundle();plist=binary.parent.parent/'Info.plist'
        for metadata in ({'CFBundleIdentifier':'foreign','CFBundleExecutable':'Fixture'},
                         {'CFBundleIdentifier':APP['bundle_id'],'CFBundleExecutable':'../Fixture'}):
            with plist.open('wb') as f:plistlib.dump(metadata,f)
            with self.assertRaises(ValueError):_bundle_executable(app)
    def test_process_probe_is_exact_pid_no_arguments_and_c_locale_start_time(self):
        _,binary=self.install_fixture_bundle()
        reply=SimpleNamespace(returncode=0,stdout=' 41 Thu Sep 17 23:50:00 2026 '+str(binary)+'\n',stderr='')
        with patch('locua.desktop_tools.sys.platform','darwin'),patch('locua.desktop_tools.subprocess.run',return_value=reply) as run:
            result=_process_identity(41)
        self.assertEqual(result,self.process(41,binary))
        self.assertEqual(run.call_args.args[0],['/bin/ps','-ww','-p','41','-o','pid=,lstart=,comm='])
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'],'C');self.assertEqual(run.call_args.kwargs['env']['TZ'],'UTC')
        self.assertEqual(run.call_args.kwargs['timeout'],2)
        for output in ('41 Thu Sep 17 23:50:00 2026 Fixture\n',reply.stdout+reply.stdout):
            with patch('locua.desktop_tools.sys.platform','darwin'),patch('locua.desktop_tools.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=output)):
                with self.assertRaises(ValueError):_process_identity(41)

    def enable_activation(self):
        self.peer.inventory['tools'].append({'name':'bring_to_front','inputSchema':{'type':'object',
            'required':['pid'],'properties':{'pid':{'type':'integer'},'window_id':{'type':'integer'}}}})
        self.tools.windows()
    def test_exact_activation_attestation_and_no_unrelated_fallback(self):
        self.enable_activation();obs=self.capture();action=self.action(obs)
        result=self.tools.activate(TARGET)
        self.assertEqual(result['status'],'activated');self.assertTrue(result['exact_window_activation_proven'])
        self.assertTrue(result['foreground_persistent']);self.assertFalse(result['accessibility_readability_proven'])
        self.assertFalse(result['task_complete'])
        self.assertEqual([c for c in self.peer.calls if c[0]=='bring_to_front'],[('bring_to_front',TARGET)])
        self.assertEqual(self.tools.execute(action,obs)['status'],'refused')
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
    def test_activation_requires_known_fresh_unique_window_and_supported_schema(self):
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
        self.tools.windows();self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
        self.enable_activation();self.peer.window_present=False
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
        self.assertFalse(any(c[0]=='bring_to_front' for c in self.peer.calls))
    def test_activation_partial_or_foreign_attestation_is_uncertain_not_success(self):
        self.enable_activation();self.peer.activation_reply['observed']['focused_window_id']=999
        result=self.tools.activate(TARGET);self.assertEqual(result['status'],'uncertain')
        self.assertFalse(result['exact_window_activation_proven'])
        self.peer.activation_reply['observed']['focused_window_id']=52
        obs=self.capture();self.assertEqual(self.tools.execute(self.action(obs),obs)['status'],'refused')
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
    def test_activation_error_reply_preserves_partial_effect_and_exact_no_request_refusal(self):
        self.enable_activation()
        self.peer.mutation_error=CuaRefusal({'code':'bring_to_front_exact_window_unverified',
            'request_accepted':True,'activated':False,**TARGET})
        self.assertEqual(self.tools.activate(TARGET)['status'],'uncertain')
        self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
    def test_activation_known_driver_precondition_refusal_is_not_partial_success(self):
        self.enable_activation()
        self.peer.mutation_error=CuaRefusal({'code':'bring_to_front_window_not_found','request_accepted':False,
                                           'activated':False,**TARGET})
        result=self.tools.activate(TARGET);self.assertEqual(result['status'],'refused')
        self.assertFalse(result['action_started']);self.assertFalse(result['exact_window_activation_proven'])
    def test_activation_timeout_stops_without_fallback_or_replay(self):
        self.enable_activation();self.peer.mutation_error=TimeoutError('uncertain activation')
        self.assertEqual(self.tools.activate(TARGET)['status'],'uncertain')
        self.peer.mutation_error=None;self.assertEqual(self.tools.activate(TARGET)['status'],'refused')
        self.assertEqual(len([c for c in self.peer.calls if c[0]=='bring_to_front']),1)


if __name__=='__main__':unittest.main()
