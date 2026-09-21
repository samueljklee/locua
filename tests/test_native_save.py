"""Native-save adapter tests. UI effects are local test doubles, never real apps."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest

from locua import native_save as module
from locua.engine.prototype.cua import CuaRefusal
from locua.engine.prototype.native_driver_contract import CONTRACT_ID,SOURCE_FINGERPRINT
from locua.engine.prototype.perception import normalize_observation

TARGET={'pid':101,'window_id':202}


def snapshot(value, *, menu_enabled=True, static_menu=False, stamp=None, target=None, window_name='Document'):
    target=TARGET if target is None else target
    editor={'contract':CONTRACT_ID,'plane':'editor_buffer',
            'raw_value':{'status':'ok','value':value},'raw_value_recheck':{'status':'ok','value':value},
            'coherence':{'value_stable':True,'focus_stable':True},
            'focused':{'status':'ok','value':True},'value_settable':{'status':'ok','value':True}}
    elements=[{'element_index':0,'element_token':'s00000001:0','role':'AXWindow','label':window_name},
              {'element_index':1,'element_token':'s00000001:1','role':'AXTextArea','label':'','editor':editor}]
    if not static_menu:elements.append({'element_index':2,'element_token':'s00000001:2','role':'AXMenuItem','label':'Save','enabled':menu_enabled})
    raw={**target,'snapshot_id':'s00000001','elements_complete':False,'elements':elements,
         'native_editor_contract':{'id':CONTRACT_ID,'source_fingerprint_sha256':SOURCE_FINGERPRINT,
                                  'platform':'macos','plane':'editor_buffer','handle_binding':'same_snapshot_element_token'},
         'tree_markdown':f'- AXApplication "Fixture"\n  - [0] AXWindow "{window_name}"\n    - [1] AXTextArea ""\n'
           '  - AXMenuBar\n    - AXMenuBarItem "File"\n      - AXMenu\n        - '
           +('' if static_menu else '[2] ')+'AXMenuItem "Save"'}
    return normalize_observation(raw,kind='native_window_state',expected_target=target,
                                 observed_at_ns=time.time_ns() if stamp is None else stamp)


class Owner:
    def __init__(self,effect=None,*,focus=None,ack=None):
        self.effect=effect;self.calls=[];self.events=[];self.focus=focus
        self.ack=ack or {'effect':'unverifiable','route':'accessibility','delivery':{'mode':'foreground'}}
    def trace(self,event):self.events.append(deepcopy(event))
    def call(self,name,args):
        self.calls.append((name,deepcopy(args)))
        if name=='bring_to_front':
            if callable(self.focus):return {},self.focus()
            return {},self.focus if self.focus is not None else {
                'status':'activated','code':'bring_to_front_exact_window_verified',**TARGET,
                'activated':True,'process_activated':True,
                'exact_window_effect':{'verified':True,'focused':True,'frontmost_ordinary':True,'target_visible_ordinary':True},
                'observed':{'frontmost_pid':TARGET['pid'],'focused_window_id':TARGET['window_id'],
                            'frontmost_ordinary_window_id':TARGET['window_id']}}
        if self.effect:self.effect()
        return {},deepcopy(self.ack)


class NativeSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name).resolve()/'fixture.txt'
        self.initial='  Original Ω\n';self.expected='  New café Ω\nsecond line\n'
        self.path.write_bytes(self.initial.encode('utf-8'))
    def tearDown(self):self.temp.cleanup()
    def binding(self,observation=None,evidence=None):
        observation=observation or snapshot(self.initial)
        evidence=evidence or {'kind':'controlled_open','path':str(self.path),'target':TARGET,
                             'prior_windows':[],'opened_at_ns':observation['observed_at_ns']-1}
        return module.prepare_binding(self.path,target=TARGET,observation=observation,
                                      editor_id='native:s00000001:1',open_evidence=evidence)
    def save(self,owner,binding=None,observation=None,**kw):
        count=0
        def observe():
            nonlocal count
            count+=1
            value=deepcopy(observation) if observation is not None else snapshot(self.expected)
            # A static mock fixture represents another fresh capture after
            # focus; an explicitly supplied callable controls adverse timing.
            if count>1 and observation is not None:
                value['observed_at_ns']=time.time_ns()
                value['provenance']['observed_at_ns']=value['observed_at_ns']
            return value
        return module.save(owner,target=TARGET,binding=binding or self.binding(),expected=self.expected,
                           observe=kw.pop('observe',observe),timeout_s=.1,poll_interval_s=.02,**kw)

    def textedit_binding(self):
        binding=self.binding()
        binding.open_evidence.update(application_contract=deepcopy(module.TEXTEDIT_APPLICATION_CONTRACT),
            command=['/usr/bin/open','-g','-n','-a','/System/Applications/TextEdit.app',str(self.path)])
        return binding

    def test_one_native_menu_save_and_exact_atomic_file_readback(self):
        binding=self.binding()
        def ui_save():
            replacement=self.path.with_suffix('.tmp');replacement.write_bytes(self.expected.encode());replacement.replace(self.path)
        owner=Owner(ui_save);result=self.save(owner,binding)
        self.assertEqual(owner.calls,[('bring_to_front',TARGET),('invoke_menu',{**TARGET,'path':['File','Save']})])
        self.assertEqual(result['status'],'saved');self.assertTrue(result['saved_file_proven'])
        self.assertTrue(result['explicit_save_proven']);self.assertTrue(result['save_command_dispatched'])
        self.assertTrue(result['atomic_path_replacement_observed']);self.assertTrue(binding.consumed)
        self.assertFalse(result['committed_document_proven']);self.assertFalse(result['save_causality_proven'])
        self.assertFalse(result['model_selected_save']);self.assertFalse(result['retry_allowed'])
        self.assertEqual(result['file_after']['sha256'],module._hash(self.expected.encode()))
        self.assertEqual([e['type'] for e in owner.events],['native_save_focus_issued','native_save_issued','native_save_result'])
        self.assertNotIn(self.expected,str(result))

    def test_autosaved_bytes_use_enabled_save_but_do_not_claim_causality(self):
        binding=self.binding();self.path.write_bytes(self.expected.encode());owner=Owner()
        result=self.save(owner,binding)
        self.assertEqual(len(owner.calls),2);self.assertTrue(result['persistence_preceded_command'])
        self.assertTrue(result['saved_file_proven']);self.assertFalse(result['save_causality_proven'])
        self.assertTrue(result['explicit_save_proven'])

    def test_already_persisted_disabled_or_unknown_save_issues_nothing(self):
        for observation in (snapshot(self.expected,menu_enabled=False),snapshot(self.expected,static_menu=True)):
            self.path.write_bytes(self.initial.encode());binding=self.binding();self.path.write_bytes(self.expected.encode())
            owner=Owner();result=self.save(owner,binding,observation)
            self.assertEqual(result['status'],'already_persisted');self.assertEqual(owner.calls,[])
            self.assertFalse(result['explicit_save_proven']);self.assertFalse(result['save_command_dispatched'])

    def test_closed_menu_markdown_is_evidence_not_an_invented_handle(self):
        owner=Owner(lambda:self.path.write_bytes(self.expected.encode()))
        result=self.save(owner,observation=snapshot(self.expected,static_menu=True))
        self.assertEqual(result['status'],'saved')
        self.assertIsNone(result['menu_evidence']['enabled_observed'])
        self.assertTrue(result['menu_evidence']['control_id'].startswith('native:markdown:'))
        self.assertNotIn('element_token',owner.calls[1][1])

    def test_initial_buffer_or_prior_window_mismatch_refuses_binding(self):
        with self.assertRaises(module.NativeSaveError):self.binding(snapshot('different'))
        obs=snapshot(self.initial)
        evidence={'kind':'controlled_open','path':str(self.path),'target':TARGET,'prior_windows':[TARGET],
                  'opened_at_ns':obs['observed_at_ns']-1}
        with self.assertRaises(module.NativeSaveError):self.binding(obs,evidence)

    def test_fresh_buffer_must_match_complete_expected_including_whitespace(self):
        for text in (self.expected.strip(),self.expected[:-1],self.initial):
            owner=Owner()
            with self.assertRaises(module.NativeSaveError):self.save(owner,observation=snapshot(text))
            self.assertEqual(owner.calls,[])

    def test_disabled_ambiguous_and_save_as_menu_never_dispatch(self):
        disabled=snapshot(self.expected,menu_enabled=False)
        ambiguous=snapshot(self.expected)
        duplicate=deepcopy(next(c for c in ambiguous['controls'] if c['role']=='AXMenuItem'))
        duplicate['id']='extra-static-menu';duplicate['source']={'kind':'native_markdown','line':'- AXMenuItem "Save"'}
        ambiguous['controls'].append(duplicate)
        for observation in (disabled,ambiguous):
            owner=Owner()
            with self.assertRaises(module.NativeSaveError):self.save(owner,observation=observation)
            self.assertEqual(owner.calls,[])
        owner=Owner()
        with self.assertRaises(module.NativeSaveError):self.save(owner,menu_path=['File','Save As…'])
        self.assertEqual(owner.calls,[])

    def test_stale_or_foreign_observation_never_dispatch(self):
        binding=self.binding()
        for observation in (snapshot(self.expected,stamp=time.time_ns()-31_000_000_000),
                            snapshot(self.expected,target={**TARGET,'window_id':203})):
            owner=Owner()
            with self.assertRaises(ValueError):self.save(owner,binding,observation)
            self.assertFalse(owner.calls)

    def test_changed_document_title_in_same_window_never_receives_save(self):
        binding=self.binding();owner=Owner()
        changed=snapshot(self.expected,window_name='Another document')
        with self.assertRaisesRegex(module.NativeSaveError,'editor changed'):
            self.save(owner,binding,changed)
        self.assertEqual(owner.calls,[])
        self.assertFalse(binding.consumed)
        self.assertEqual(self.path.read_bytes(),self.initial.encode())

    def test_unexpected_preexisting_file_change_refuses(self):
        binding=self.binding();self.path.write_text('external modification');owner=Owner()
        with self.assertRaises(module.NativeSaveError):self.save(owner,binding)
        self.assertEqual(owner.calls,[]);self.assertEqual(self.path.read_text(),'external modification')

    def test_timeout_after_effect_can_prove_bytes_without_retry(self):
        def timeout():self.path.write_bytes(self.expected.encode());raise TimeoutError('uncertain acknowledgment')
        binding=self.binding();owner=Owner(timeout);result=self.save(owner,binding)
        self.assertEqual(result['status'],'partial');self.assertEqual(result['save_command_effect'],'unknown')
        self.assertTrue(result['saved_file_proven'])
        self.assertFalse(result['explicit_save_proven']);self.assertFalse(result['save_command_dispatched'])
        with self.assertRaises(module.NativeSaveError):self.save(owner,binding)
        self.assertEqual(len(owner.calls),2)

    def test_menu_refusal_never_becomes_explicit_save_from_autosaved_bytes(self):
        binding=self.binding();self.path.write_bytes(self.expected.encode())
        payload={'status':'refused','refusal':{'code':'menu_path_unavailable',
                 'message':'target window did not become stably key and frontmost'}}
        def refuse():raise CuaRefusal(payload)
        owner=Owner(refuse);result=self.save(owner,binding)
        self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
        self.assertEqual(result['save_command_effect'],'refused')
        self.assertEqual(result['save_command_refusal'],payload)
        self.assertFalse(result['explicit_save_proven']);self.assertFalse(result['save_command_dispatched'])
        self.assertFalse(result['save_causality_proven']);self.assertEqual(result['dispatch_count'],1)
        with self.assertRaises(module.NativeSaveError):self.save(owner,binding)
        self.assertEqual([name for name,_ in owner.calls],['bring_to_front','invoke_menu'])

    def test_menu_refusal_without_saved_bytes_is_refused(self):
        def refuse():raise CuaRefusal({'status':'refused','refusal':{'code':'menu_path_unavailable'}})
        result=self.save(Owner(refuse))
        self.assertEqual(result['status'],'refused');self.assertFalse(result['saved_file_proven'])
        self.assertFalse(result['explicit_save_proven'])

    def test_save_ack_requires_correct_route_delivery_and_no_refusal(self):
        payloads=[{'effect':'unverifiable'},
                  {'effect':'unverifiable','route':'global_input','delivery':{'mode':'foreground'}},
                  {'effect':'unverifiable','route':'accessibility','delivery':{'mode':'background'}},
                  {'effect':'unverifiable','route':'accessibility','delivery':{'mode':'foreground'},
                   'status':'refused','refusal':{'code':'menu_path_unavailable'}}]
        for ack in payloads:
            with self.subTest(ack=ack):
                self.path.write_bytes(self.initial.encode());binding=self.binding()
                self.path.write_bytes(self.expected.encode())
                result=self.save(Owner(ack=ack),binding)
                self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
                self.assertFalse(result['save_command_dispatched']);self.assertFalse(result['explicit_save_proven'])

    def test_partial_or_wrong_window_focus_never_issues_save(self):
        _,valid=Owner().call('bring_to_front',TARGET)
        payloads=[]
        for key,value in (('pid',102),('window_id',203),('status','partial'),('activated',False),
                          ('process_activated',False),('code','bring_to_front_process_verified')):
            item=deepcopy(valid);item[key]=value;payloads.append(item)
        for key in ('verified','focused','frontmost_ordinary','target_visible_ordinary'):
            item=deepcopy(valid);item['exact_window_effect'][key]=False;payloads.append(item)
        for key in ('frontmost_pid','focused_window_id','frontmost_ordinary_window_id'):
            item=deepcopy(valid);item['observed'][key]=999;payloads.append(item)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.path.write_bytes(self.initial.encode());binding=self.binding()
                self.path.write_bytes(self.expected.encode());owner=Owner(focus=payload)
                result=self.save(owner,binding)
                self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
                self.assertFalse(result['explicit_save_proven']);self.assertEqual(result['dispatch_count'],0)
                self.assertEqual(owner.calls,[('bring_to_front',TARGET)]);self.assertTrue(binding.consumed)

    def test_focus_refusal_or_timeout_never_issues_or_retries_save(self):
        for error in (CuaRefusal({'status':'refused','refusal':{'code':'focus_failed'}}),TimeoutError('unknown')):
            with self.subTest(error=type(error).__name__):
                def fail():raise error
                owner=Owner(focus=fail);binding=self.binding();result=self.save(owner,binding)
                self.assertFalse(result['saved_file_proven']);self.assertFalse(result['save_command_dispatched'])
                self.assertEqual(owner.calls,[('bring_to_front',TARGET)])
                with self.assertRaises(module.NativeSaveError):self.save(owner,binding)
                self.assertEqual(len(owner.calls),1)

    def test_focus_requires_new_capture_then_same_document_and_buffer(self):
        for violation in ('reused','title','buffer','target'):
            with self.subTest(violation=violation):
                first=snapshot(self.expected);reads=0;owner=Owner();binding=self.binding()
                def observe():
                    nonlocal reads
                    reads+=1
                    if reads==1 or violation=='reused':return first
                    if violation=='title':return snapshot(self.expected,window_name='Another document')
                    if violation=='buffer':return snapshot('Unexpected editor change')
                    return snapshot(self.expected,target={**TARGET,'window_id':999})
                with self.assertRaises(ValueError):self.save(owner,binding,observe=observe)
                self.assertEqual(reads,2);self.assertEqual(owner.calls,[('bring_to_front',TARGET)])
                self.assertTrue(binding.consumed)

    def test_disabled_fresh_menu_after_focus_does_not_invoke_save(self):
        reads=0;binding=self.binding();owner=Owner()
        def observe():
            nonlocal reads
            reads+=1
            if reads==2:self.path.write_bytes(self.expected.encode())
            return snapshot(self.expected,menu_enabled=reads==1)
        result=self.save(owner,binding,observe=observe)
        self.assertEqual(result['status'],'already_persisted');self.assertTrue(result['saved_file_proven'])
        self.assertFalse(result['explicit_save_proven']);self.assertEqual(owner.calls,[('bring_to_front',TARGET)])

    def test_autosave_during_focus_still_issues_known_enabled_save_once(self):
        def focus():
            self.path.write_bytes(self.expected.encode())
            return Owner().call('bring_to_front',TARGET)[1]
        owner=Owner(focus=focus);result=self.save(owner)
        self.assertTrue(result['persistence_preceded_command']);self.assertTrue(result['explicit_save_proven'])
        self.assertFalse(result['save_causality_proven'])
        self.assertEqual([name for name,_ in owner.calls],['bring_to_front','invoke_menu'])

    def test_explicit_textedit_shortcut_posts_exact_editor_key_without_persistent_focus(self):
        binding=self.textedit_binding()
        owner=Owner(lambda:self.path.write_bytes(self.expected.encode()),
                    ack={'effect':'unverifiable','route':'global_input','delivery':{'mode':'foreground'}})
        result=self.save(owner,binding,save_route='textedit_shortcut')
        self.assertEqual(owner.calls,[('press_key',{**TARGET,'snapshot_id':'s00000001',
            'element_token':'s00000001:1','key':'s','modifiers':['cmd'],'delivery_mode':'foreground'})])
        self.assertEqual(result['status'],'saved');self.assertTrue(result['saved_file_proven'])
        self.assertTrue(result['save_shortcut_posted']);self.assertTrue(result['save_command_dispatched'])
        self.assertTrue(result['explicit_save_proven']);self.assertTrue(result['post_buffer_proven'])
        self.assertFalse(result['menu_dispatch_proven']);self.assertFalse(result['save_causality_proven'])
        self.assertFalse(result['foreground_restoration_proven']);self.assertEqual(result['focus_dispatch_count'],0)
        self.assertFalse(result['shortcut_contract']['mapping_observed_from_menu'])
        self.assertEqual(result['shortcut_contract']['mapping_source'],'https://support.apple.com/en-us/102650')

    def test_textedit_shortcut_requires_exact_background_application_contract(self):
        for violation in ('missing','bundle','app','foreground_open','named_app','not_selected','other_flag','app_args'):
            with self.subTest(violation=violation):
                binding=self.textedit_binding();owner=Owner()
                if violation=='missing':binding.open_evidence.pop('application_contract')
                if violation=='bundle':binding.open_evidence['application_contract']['bundle_id']='other.app'
                if violation=='app':binding.open_evidence['application_contract']['application_path']='/tmp/TextEdit.app'
                if violation=='foreground_open':binding.open_evidence['command'].remove('-g')
                if violation=='named_app':binding.open_evidence['command'][-2]='TextEdit'
                if violation=='other_flag':binding.open_evidence['command'].insert(1,'-F')
                if violation=='app_args':binding.open_evidence['command']+=['--args','-ApplePersistenceIgnoreState','YES']
                route='automatic' if violation=='not_selected' else 'textedit_shortcut'
                with self.assertRaises(module.NativeSaveError):self.save(owner,binding,save_route=route)
                self.assertEqual(owner.calls,[]);self.assertFalse(binding.consumed)

    def test_shortcut_accepts_both_exact_background_open_forms_without_rebinding(self):
        for separate in (False,True):
            with self.subTest(separate_instance=separate):
                self.path.write_bytes(self.initial.encode());binding=self.textedit_binding()
                if not separate:binding.open_evidence['command'].remove('-n')
                owner=Owner(lambda:self.path.write_bytes(self.expected.encode()),
                            ack={'effect':'unverifiable','route':'global_input','delivery':{'mode':'foreground'}})
                result=self.save(owner,binding,save_route='textedit_shortcut')
                self.assertTrue(result['explicit_save_proven']);self.assertEqual(result['target'],TARGET)
                self.assertEqual(len(owner.calls),1);self.assertEqual(owner.calls[0][0],'press_key')

    def test_textedit_shortcut_refusal_never_falls_back_or_retries(self):
        binding=self.textedit_binding();self.path.write_bytes(self.expected.encode())
        def refuse():raise CuaRefusal({'effect':'refused','refusal':{'code':'native_target_mismatch'}})
        owner=Owner(refuse);result=self.save(owner,binding,save_route='textedit_shortcut')
        self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
        self.assertFalse(result['save_shortcut_posted']);self.assertFalse(result['explicit_save_proven'])
        self.assertEqual([name for name,_ in owner.calls],['press_key'])
        with self.assertRaises(module.NativeSaveError):self.save(owner,binding,save_route='menu')
        self.assertEqual(len(owner.calls),1)

    def test_textedit_shortcut_ack_cannot_claim_menu_or_background_route(self):
        for ack in ({'effect':'unverifiable','route':'accessibility','delivery':{'mode':'foreground'}},
                    {'effect':'unverifiable','route':'global_input','delivery':{'mode':'background'}},
                    {'effect':'unverifiable','route':'global_input'}):
            with self.subTest(ack=ack):
                self.path.write_bytes(self.initial.encode());binding=self.textedit_binding()
                self.path.write_bytes(self.expected.encode());owner=Owner(ack=ack)
                result=self.save(owner,binding,save_route='textedit_shortcut')
                self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
                self.assertFalse(result['save_shortcut_posted']);self.assertFalse(result['explicit_save_proven'])
                self.assertEqual(len(owner.calls),1)

    def test_post_operation_buffer_change_prevents_explicit_completion_even_if_saved_bytes_match(self):
        for route in ('menu','textedit_shortcut'):
            with self.subTest(route=route):
                self.path.write_bytes(self.initial.encode())
                binding=self.textedit_binding() if route=='textedit_shortcut' else self.binding()
                reads=0
                def observe():
                    nonlocal reads
                    reads+=1
                    return snapshot(self.expected if reads<3 else self.expected+'Tio')
                owner=Owner(lambda:self.path.write_bytes(self.expected.encode()),
                            ack={'effect':'unverifiable','route':'global_input' if route=='textedit_shortcut' else 'accessibility',
                                 'delivery':{'mode':'foreground'}})
                result=self.save(owner,binding,observe=observe,save_route=route)
                self.assertEqual(result['status'],'partial');self.assertTrue(result['saved_file_proven'])
                self.assertTrue(result['save_command_dispatched']);self.assertFalse(result['explicit_save_proven'])
                self.assertFalse(result['post_buffer_proven']);self.assertEqual(result['reason'],'post_save_editor_not_verified')

    def test_shortcut_post_unexpected_extra_saved_bytes_remain_failure(self):
        binding=self.textedit_binding();owner=Owner(lambda:self.path.write_bytes((self.expected+'Tio').encode()),
            ack={'effect':'unverifiable','route':'global_input','delivery':{'mode':'foreground'}})
        result=self.save(owner,binding,save_route='textedit_shortcut')
        self.assertTrue(result['save_shortcut_posted']);self.assertFalse(result['explicit_save_proven'])
        self.assertFalse(result['saved_file_proven']);self.assertEqual(result['status'],'unknown_effect')
        self.assertEqual(result['reason'],'unexpected_saved_bytes');self.assertEqual(len(owner.calls),1)

    def test_shortcut_does_not_press_disabled_save_even_when_selected(self):
        binding=self.textedit_binding();owner=Owner()
        with self.assertRaises(module.NativeSaveError):
            self.save(owner,binding,observation=snapshot(self.expected,menu_enabled=False),save_route='textedit_shortcut')
        self.assertEqual(owner.calls,[])

    def test_shortcut_already_persisted_enabled_save_still_posts_once(self):
        binding=self.textedit_binding();self.path.write_bytes(self.expected.encode())
        owner=Owner(ack={'effect':'unverifiable','route':'global_input','delivery':{'mode':'foreground'}})
        result=self.save(owner,binding,save_route='textedit_shortcut')
        self.assertEqual(len(owner.calls),1);self.assertTrue(result['save_shortcut_posted'])
        self.assertTrue(result['persistence_preceded_command']);self.assertTrue(result['explicit_save_proven'])
        self.assertFalse(result['save_causality_proven'])

    def test_no_effect_is_unknown_not_retried_or_proved(self):
        binding=self.binding();owner=Owner();result=self.save(owner,binding)
        self.assertEqual(result['status'],'unknown_effect');self.assertFalse(result['saved_file_proven'])
        self.assertTrue(result['save_command_dispatched']);self.assertFalse(result['explicit_save_proven'])
        self.assertEqual(result['reason'],'expected_bytes_not_observed')
        with self.assertRaises(module.NativeSaveError):self.save(owner,binding)
        self.assertEqual(len(owner.calls),2);self.assertEqual(self.path.read_bytes(),self.initial.encode())

    def test_symlink_substitution_after_command_is_not_persistence(self):
        external=self.path.with_name('other.txt');external.write_bytes(self.expected.encode())
        def substitute():self.path.unlink();self.path.symlink_to(external)
        owner=Owner(substitute);result=self.save(owner)
        self.assertFalse(result['saved_file_proven']);self.assertEqual(result['status'],'unknown_effect')
        self.assertEqual(external.read_bytes(),self.expected.encode());self.assertEqual(len(owner.calls),2)

    def test_file_reader_rejects_binary_invalid_utf8_and_hardlink_alias(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):module._read_file(self.path)
        self.path.write_bytes(self.initial.encode());self.path.with_suffix('.other').hardlink_to(self.path)
        with self.assertRaises(module.NativeSaveError):module._read_file(self.path)


if __name__=='__main__':unittest.main()
