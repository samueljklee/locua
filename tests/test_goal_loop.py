"""Deterministic loop boundaries; these tests are not live/model evidence."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
import time

from locua.goal_loop import GoalLoop, review_text
from locua.goal_verification import bind


def goal():
    return {'request':'Open Counter and calculate 12*3','outcomes':[
        {'id':'open','kind':'open_app','target':'Counter','value':None,'evidence_plane':'display'},
        {'id':'calc','kind':'calculation','target':'','expression':'12*3','value':None,'evidence_plane':'display'}],
        'restrictions':[]}


class Desktop:
    def __init__(self,opened=False):
        self.opened=opened;self.launches=0;self.captures=0
        self.app={'name':'Counter','bundle_id':'test.counter','pid':10 if opened else 0,'running':opened}
    def apps(self):return {'status':'ok','apps':[deepcopy(self.app)]}
    def windows(self):return {'status':'ok','windows':[{'pid':10,'window_id':20,'title':'Counter','is_on_screen':True}] if self.opened else []}
    def app_windows(self,app):return self.windows()
    def launch(self,app):
        self.launches+=1;self.opened=True;self.app['pid']=10
        return {'status':'launched','action_started':True,'app':deepcopy(self.app)}
    def observe(self,target):
        self.captures+=1
        now=time.time_ns()
        return {'status':'observed','observation':{'kind':'native_window_state','snapshot_id':str(self.captures),'target':target,
             'observed_at_ns':now,'provenance':{'observed_at_ns':now},'handles':{},'coverage':{'complete':False},
             'controls':[{'id':'window','role':'AXWindow','name':'Counter','parent':None,'semantics':{}},
                {'id':'display','role':'AXStaticText','name':'Result','value':'36','semantics':{},'parent':'window'}]}}


class Selector:
    def __init__(self,answers):self.answers=iter(answers);self.calls=[]
    def choose(self,**payload):self.calls.append(payload);return {'selected_id':next(self.answers),'abstained':False}


class LoopTests(unittest.TestCase):
    def loop(self,tmp,answers,opened=False):
        report={'model':'comparator','task_actions_started':False};d=Desktop(opened);s=Selector(answers)
        return GoalLoop(goal()['request'],goal(),d,s,Path(tmp),report,lambda *_:'run',lambda _:None),d,s,report

    def test_closed_app_opens_and_observes_without_claiming_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['app:0','launch','window:0'])
            self.assertTrue(loop.resolve_app());self.assertTrue(loop.review());self.assertTrue(loop.acquire())
            self.assertEqual(d.launches,1);self.assertEqual(set(loop.verified),{'open'})
            self.assertNotIn('calc',loop.verified)
            self.assertTrue(all(goal()['request'] in c['goal'] for c in s.calls))
            self.assertEqual(r['goal_plan']['request'],goal()['request'])

    def test_existing_window_reused_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['app:0','window:0'],True)
            loop.resolve_app();loop.review();self.assertTrue(loop.acquire());self.assertEqual(d.launches,0)

    def test_application_ambiguity_does_not_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['clarify'])
            self.assertFalse(loop.resolve_app());self.assertEqual(r['status'],'clarification');self.assertEqual(d.launches,0)

    def test_unavailable_app_does_not_substitute(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['unavailable'])
            self.assertFalse(loop.resolve_app());self.assertEqual(r['reason'],'requested_application_unavailable');self.assertEqual(d.launches,0)

    def test_verification_requires_fresh_actual_display_not_button_or_answer(self):
        o=goal()['outcomes'][1]
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['app:0','window:0'],True)
            loop.resolve_app();loop.review();loop.acquire()
            loop.bindings['calc']=bind(o,loop.observation['controls'][1],loop.observation)
            loop.verify(o,'display');self.assertNotIn('calc',loop.verified)
            for token in ['clear','1','2','*','3','=']:
                loop.arithmetic_input.record(token,snapshot_id='test',descriptor='synthetic input')
            loop.verify(o,'display')
            self.assertIn('calc',loop.verified);self.assertNotEqual(loop.verified['calc']['first_snapshot'],loop.verified['calc']['second_snapshot'])

    def test_text_needs_exact_readback_and_review_retains_full_goal(self):
        text=review_text(goal()['request'],goal(),Desktop().app,'comparator')
        self.assertIn('12*3',text);self.assertIn('original RLCD',text);self.assertIn('Opening a window is not proof',text)

class InteractionDesktop:
    """Synthetic driver boundary; no process, model or GUI is started."""
    def __init__(self, rows):
        self.rows=deepcopy(rows); self.captures=0; self.executed=[]
        self.after_execute=None; self.before_observe=None

    def observe(self,target):
        self.captures+=1
        if self.before_observe:self.before_observe(self)
        now=time.time_ns()
        return {'status':'observed','observation':{
            'kind':'native_window_state','snapshot_id':'synthetic-'+str(self.captures),
            'target':deepcopy(target),'observed_at_ns':now,'provenance':{'observed_at_ns':now},
            'handles':{},'coverage':{'complete':False},'controls':[
                {'id':'window','role':'AXWindow','name':'Synthetic document','parent':None,'semantics':{}},
                *deepcopy(self.rows)]}}

    def actions(self,observation):
        actions=[]
        for row in observation['controls']:
            if row['role']=='AXTextField':kind='set_text'
            elif row['role'] in ('AXCheckBox','AXButton'):kind='press'
            else:continue
            actions.append({'id':'issued:'+row['id'],'kind':kind,'control_id':row['id'],
                            'description':('Replace ' if kind=='set_text' else 'Press ')+row['name']})
        return {'status':'ok','actions':actions,'unavailable':[]}

    def execute(self,action,observation):
        self.executed.append(deepcopy(action))
        row=next(r for r in self.rows if r['id']==action['control_id'])
        if action['kind']=='set_text':row['value']=action['value']
        elif row['role']=='AXCheckBox':row['states']['checked']=not row['states']['checked']
        if self.after_execute:self.after_execute(self,action)
        return {'status':'dispatched','action_started':True,
                'observation':self.observe(observation['target'])['observation']}


def text_row(cid,name,value):
    return {'id':cid,'role':'AXTextField','name':name,'value':value,'parent':'window',
        'states':{},'semantics':{},'value_evidence':{'precision':'exact',
            'exact_value_proven':True,'plane':'editor_buffer'}}


def text_goal(cid,name,value):
    return {'id':cid,'kind':'text','target':name,'value':value,'evidence_plane':'editor_buffer'}


class MatchingSelector:
    def __init__(self,*steps):self.steps=iter(steps);self.calls=[]
    def choose(self,**payload):
        self.calls.append(deepcopy(payload));step=next(self.steps)
        matches=[c for c in payload['candidates'] if step(c)]
        if len(matches)!=1:raise AssertionError('Synthetic choice must match exactly one offered candidate')
        return {'selected_id':matches[0]['id'],'abstained':False}


def by_id(cid):return lambda c:c['id']==cid

def by_description(text):return lambda c:c['description']==text


class GoalLoopGuardTests(unittest.TestCase):
    def setup_loop(self,tmp,outcomes,rows,*answers):
        desktop=InteractionDesktop(rows);selector=MatchingSelector(*answers)
        plan={'request':'Set the reviewed fields and retain every requested outcome.',
              'outcomes':deepcopy(outcomes),'restrictions':[]}
        report={'model':'comparator','task_actions_started':False}
        loop=GoalLoop(plan['request'],plan,desktop,selector,Path(tmp),report,lambda *_:'run',lambda _:None)
        loop.app={'name':'Synthetic editor','bundle_id':'test.editor'}
        loop.target={'pid':41,'window_id':52}
        loop.observation=desktop.observe(loop.target)['observation'];loop.reviewed=True
        for outcome in outcomes:
            if outcome['kind']=='open_app':continue
            row=next(c for c in loop.observation['controls'] if c.get('name')==outcome['target'])
            loop.bindings[outcome['id']]=bind(outcome,row,loop.observation)
        return loop,desktop,selector,report

    def test_wrong_target_text_is_offered_but_refused_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,[text_goal('alpha','Alpha','A exact')],
                [text_row('a','Alpha','old A'),text_row('b','Beta','old B')],
                by_description('Replace Beta to exact requested input "A exact"'))
            with self.assertRaisesRegex(RuntimeError,'selected_text_action_outside_reviewed_binding'):loop.interact()
            self.assertEqual(d.executed,[]);self.assertFalse(r['task_actions_started'])
            self.assertTrue(any(c['description'].startswith('Replace Alpha') for c in s.calls[0]['candidates']))
            self.assertTrue(any(c['description'].startswith('Replace Beta') for c in s.calls[0]['candidates']))

    def test_other_outcomes_value_cannot_be_written_to_correct_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,[text_goal('alpha','Alpha','A exact'),text_goal('beta','Beta','B exact')],
                [text_row('a','Alpha','old A'),text_row('b','Beta','old B')],
                by_description('Replace Alpha to exact requested input "B exact"'))
            with self.assertRaisesRegex(RuntimeError,'selected_text_action_outside_reviewed_binding'):loop.interact()
            self.assertEqual(d.executed,[])

    def test_wrong_checkbox_is_offered_but_refused_before_dispatch(self):
        rows=[{'id':name.lower(),'role':'AXCheckBox','name':name,'value':None,'parent':'window',
               'semantics':{},'states':{'checked':False}} for name in ('Alpha','Beta')]
        outcome={'id':'alpha','kind':'state','target':'Alpha','value':True,'evidence_plane':'display'}
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,[outcome],rows,by_description('Press Beta'))
            with self.assertRaisesRegex(RuntimeError,'selected_press_outside_reviewed_state_change'):loop.interact()
            self.assertEqual(d.executed,[])

    def test_already_satisfied_checkbox_cannot_be_toggled_away(self):
        row={'id':'a','role':'AXCheckBox','name':'Alpha','value':None,'parent':'window',
             'semantics':{},'states':{'checked':True}}
        outcome={'id':'alpha','kind':'state','target':'Alpha','value':True,'evidence_plane':'display'}
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,[outcome],[row],by_description('Press Alpha'),by_id('stop'))
            # A state guard may refuse the unnecessary press; regardless of the
            # refusal code it must never issue an input that undoes the goal.
            with self.assertRaises(RuntimeError):loop.interact()
            self.assertEqual(d.executed,[])

    def test_complete_two_field_loop_uses_exact_values_and_fresh_final_readback(self):
        outcomes=[text_goal('alpha','Alpha',' A\nΩ '),text_goal('beta','Beta','B exact')]
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,outcomes,[text_row('a','Alpha','old A'),text_row('b','Beta','old B')],
                by_description('Replace Alpha to exact requested input " A\\nΩ "'),by_id('verify:alpha'),
                by_description('Replace Beta to exact requested input "B exact"'),by_id('verify:beta'))
            self.assertTrue(loop.interact())
            self.assertEqual([(a['control_id'],a['value']) for a in d.executed],[('a',' A\nΩ '),('b','B exact')])
            self.assertEqual(set(loop.verified),{'alpha','beta'})
            self.assertTrue(all('final_readback' in p for p in loop.verified.values()))
            for call in s.calls:
                self.assertIn(loop.request,call['goal']);self.assertIn('"id":"alpha"',call['goal']);self.assertIn('"id":"beta"',call['goal'])

    def test_later_edit_invalidates_an_earlier_verified_outcome(self):
        outcomes=[text_goal('alpha','Alpha','A exact'),text_goal('beta','Beta','B exact')]
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,outcomes,[text_row('a','Alpha','A exact'),text_row('b','Beta','old B')],
                by_description('Replace Beta to exact requested input "B exact"'),by_id('stop'))
            loop.verified['alpha']={'plane':'editor_buffer'}
            d.after_execute=lambda desktop,action:desktop.rows[0].update(value='externally changed')
            with self.assertRaisesRegex(RuntimeError,'required_interaction_or_verification_unavailable'):loop.interact()
            self.assertNotIn('alpha',loop.verified)
            self.assertTrue(any(e['kind']=='verification_invalidated' and e['outcome_id']=='alpha' for e in r['events']))

    def test_cached_whole_goal_receipts_cannot_replace_final_predicates(self):
        outcomes=[text_goal('alpha','Alpha','A exact'),text_goal('beta','Beta','B exact')]
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,outcomes,[text_row('a','Alpha','A exact'),text_row('b','Beta','B exact')])
            loop.verified.update({'alpha':{'plane':'editor_buffer'},'beta':{'plane':'editor_buffer'}})
            d.before_observe=lambda desktop:desktop.rows[0].update(value='changed after verification')
            with self.assertRaisesRegex(RuntimeError,'final_goal_predicate_no_longer_true: alpha'):loop.interact()
            self.assertEqual(s.calls,[]);self.assertEqual(d.executed,[])

    def test_uncertain_effect_is_observed_before_any_later_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.setup_loop(tmp,[text_goal('alpha','Alpha','A exact')],[text_row('a','Alpha','old A')],
                by_description('Replace Alpha to exact requested input "A exact"'),by_id('stop'))
            def uncertain(action,observation):
                d.executed.append(deepcopy(action));return {'status':'uncertain','action_started':True}
            d.execute=uncertain
            with self.assertRaisesRegex(RuntimeError,'required_interaction_or_verification_unavailable'):loop.interact()
            self.assertEqual(len(d.executed),1)
            self.assertTrue(any(e['kind']=='uncertain_action_readback' for e in r['events']))
            self.assertEqual(loop.verified,{})


class GoalLoopActivationTests(unittest.TestCase):
    loop=LoopTests.loop

    def test_explicit_activation_uses_exact_window_then_fresh_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['app:0','activate:0'],True)
            activated=[]
            d.activate=lambda target:(activated.append(deepcopy(target)) or {'status':'activated','action_started':True})
            loop.resolve_app();self.assertTrue(loop.acquire())
            self.assertEqual(activated,[{'pid':10,'window_id':20}]);self.assertEqual(d.launches,0)
            self.assertEqual(d.captures,1);self.assertEqual(set(loop.verified),{'open'})
            self.assertTrue(r['task_actions_started'])
            kinds=[e['kind'] for e in r['events']]
            self.assertLess(kinds.index('activation_result'),kinds.index('observation_result'))

    def test_refused_activation_does_not_fall_back_to_launch_or_claim_observation(self):
        for status in ('refused','uncertain'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as tmp:
                loop,d,s,r=self.loop(tmp,['app:0','activate:0'],True)
                d.activate=lambda target:{'status':status,'action_started':status=='uncertain'}
                loop.resolve_app()
                with self.assertRaisesRegex(RuntimeError,'exact_window_activation_'+status):loop.acquire()
                self.assertEqual(d.launches,0);self.assertEqual(d.captures,0);self.assertEqual(loop.verified,{})

    def test_existing_window_inspection_never_activates_implicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop,d,s,r=self.loop(tmp,['app:0','window:0'],True)
            def forbidden_activation(target):raise AssertionError('No implicit activation allowed')
            d.activate=forbidden_activation
            loop.resolve_app();self.assertTrue(loop.acquire())
            self.assertEqual(d.launches,0);self.assertFalse(r['task_actions_started'])

class ClarificationWorkflowTests(unittest.TestCase):
    """Real orchestration/GoalLoop with inert desktop and categorical stubs."""
    request='Open Counter and keep the complete original request.'

    def proposed(self,clarifications=()):
        return {'status':'proposed','goal_plan':{'request':self.request,
            'clarifications':list(clarifications),'app_hint':{'text':'Counter'},
            'outcomes':[{'id':'open','kind':'open_app','target':'Counter','value':None,'evidence_plane':'display'}],
            'restrictions':[]},'planning_calls_started':1,'planning_calls_completed':1,
            'usage':{'input_tokens':100,'output_tokens':20},'timing':{'generation_ms':10.0}}

    def clarify(self):
        return {'status':'clarification','questions':['Which Counter document?'],
            'planning_calls_started':1,'planning_calls_completed':1,
            'usage':{'input_tokens':80,'output_tokens':10},'timing':{'generation_ms':8.0}}

    def journey(self,plans,selector_answers,answers,*,first_capture_unavailable=False):
        from contextlib import ExitStack, nullcontext
        from unittest.mock import patch
        import json
        from locua.goal_loop import run
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            folder=Path(tmp)/'run';answers=iter(answers);proposals=iter(plans)
            lifecycle=[];planner_inputs=[];selectors=[];questions=[]
            desktop=Desktop(opened=True);desktop.activation_calls=[]
            original_observe=desktop.observe
            def observe(target):
                result=original_observe(target)
                if first_capture_unavailable and desktop.captures==1:return {'status':'unavailable'}
                return result
            desktop.observe=observe
            desktop.activate=lambda target:(desktop.activation_calls.append(deepcopy(target)) or {'status':'activated','action_started':True})
            desktop.close=lambda:(lifecycle.append('desktop-close') or {'status':'closed'})
            def plan(request,**kw):
                self.assertNotIn('selector-open',lifecycle[-1:] if lifecycle else [])
                self.assertTrue(all(getattr(s,'closed',False) for s in selectors))
                planner_inputs.append((request,deepcopy(kw['clarifications']),Path(kw['out'])))
                lifecycle.append('planner')
                value=next(proposals)
                return value(kw['clarifications']) if callable(value) else deepcopy(value)
            sequences=iter(selector_answers)
            class Service(Selector):
                def __init__(self,**kw):super().__init__(next(sequences));self.closed=False;selectors.append(self)
                def __enter__(self):lifecycle.append('selector-open');return self
                def __exit__(self,*args):self.closed=True;lifecycle.append('selector-close')
                def info(self):return {'synthetic':True}
                def choose(self,**kw):
                    result=super().choose(**kw)
                    return {**result,'stages':[{'full_input_tokens':50}],'timing':{'inference_ms':2.5}}
            def ask(prompt):questions.append(prompt);return next(answers)
            stack.enter_context(patch('locua.lib._config',return_value={k:'/synthetic/'+k for k in
                ('runtime_python','model_cache','driver_binary','driver_socket')}))
            planner=stack.enter_context(patch('locua.goal_planner.interpret_goal',side_effect=plan))
            constructor=stack.enter_context(patch('locua.desktop_tools.DesktopTools',return_value=desktop))
            stack.enter_context(patch('locua.engine_adapter.runtime_environment',side_effect=lambda cfg:nullcontext()))
            stack.enter_context(patch('locua.engine.prototype.decision.ModelService',Service))
            result=run(self.request,out=folder,ask=ask,progress=lambda _:None)
            saved=json.loads((folder/'summary.json').read_text())
            artifacts={str(p.relative_to(folder)):json.loads(p.read_text()) for p in folder.glob('attempt-*/summary.json')}
            return result,saved,planner_inputs,selectors,desktop,questions,artifacts,constructor.call_count

    def test_selector_question_replans_original_with_separate_answer_and_new_inventory(self):
        r,s,inputs,selectors,d,questions,artifacts,owners=self.journey(
            [self.proposed,self.proposed],[['clarify'],['app:0','window:0']],['The Counter app','run'])
        self.assertEqual(r['status'],'complete');self.assertEqual(s,r)
        self.assertEqual([(a,b) for a,b,_ in inputs],[(self.request,[]),(self.request,['The Counter app'])])
        self.assertEqual(owners,1);self.assertTrue(all(s.closed for s in selectors))
        self.assertEqual(len(r['workflow_attempts']),2);self.assertEqual(len(artifacts),2)
        self.assertNotEqual(inputs[0][2],inputs[1][2])
        self.assertEqual([h['kind'] for h in r['human_interactions']],['selector_clarification','plan_review'])
        self.assertEqual(r['model_usage']['planning_calls'],2);self.assertEqual(r['model_usage']['planning_input_tokens'],200)
        self.assertEqual(r['model_usage']['rlcd_calls'],3);self.assertEqual(r['model_usage']['rlcd_input_tokens'],150)
        self.assertEqual(r['model_usage']['planning_generation_ms'],20.0);self.assertEqual(r['model_usage']['rlcd_inference_ms'],7.5)
        self.assertEqual(len([e for e in r['events'] if e['kind']=='inventory']),2)
        self.assertEqual(r['workflow_attempts'][0]['status'],'clarification')
        self.assertEqual(r['workflow_attempts'][1]['status'],'complete')

    def test_planner_and_window_questions_share_answer_sources_and_budget(self):
        r,_,inputs,_,_,_,_,_=self.journey([self.clarify(),self.proposed,self.proposed],
            [['app:0','clarify'],['app:0','window:0']],['First answer','Second answer','run'])
        self.assertEqual(r['status'],'complete');self.assertEqual(len(inputs),3)
        self.assertEqual(inputs[-1][1],['First answer','Second answer'])
        self.assertEqual([h['kind'] for h in r['human_interactions']],['intent_clarification','selector_clarification','plan_review'])
        self.assertEqual(r['model_usage']['planning_calls'],3)

    def test_three_answers_global_limit_no_fourth_prompt_or_fifth_model_plan(self):
        r,_,inputs,selectors,_,questions,_,_=self.journey([self.clarify()]*4,[],['one','two','three'])
        self.assertEqual(r['status'],'clarification');self.assertEqual(r['reason'],'clarification_budget_exhausted')
        self.assertEqual(len(inputs),4);self.assertEqual(len(questions),3);self.assertEqual(selectors,[])
        self.assertEqual(r['clarifications'],['one','two','three'])

    def test_unanswered_selector_question_stops_without_replanning(self):
        r,_,inputs,_,d,questions,_,_=self.journey([self.proposed],[['clarify']],[''])
        self.assertEqual(r['reason'],'clarification_unanswered');self.assertEqual(len(inputs),1)
        self.assertEqual(d.launches,0);self.assertFalse(r['task_actions_started'])
        self.assertEqual(len(questions),1)

    def test_prior_activation_and_failed_capture_survive_clarification_and_cancel(self):
        r,_,inputs,_,d,_,artifacts,owners=self.journey([self.proposed,self.proposed],
            [['app:0','activate:0','clarify'],['app:0','window:0']],['This existing window',''],
            first_capture_unavailable=True)
        self.assertEqual(r['status'],'canceled');self.assertEqual(r['reason'],'review_not_approved')
        self.assertTrue(r['task_actions_started']);self.assertEqual(len(d.activation_calls),1);self.assertEqual(owners,1)
        self.assertTrue(r['workflow_attempts'][0]['task_actions_started'])
        self.assertFalse(r['workflow_attempts'][1]['task_actions_started'])
        self.assertTrue(any(e['kind']=='activation_result' and e['workflow_attempt']==1 for e in r['events']))
        self.assertEqual(r['model_usage']['rlcd_calls'],5);self.assertEqual(len(artifacts),2)

    def test_selector_runtime_failure_never_becomes_clarification_retry(self):
        r,_,inputs,_,_,questions,_,_=self.journey([self.proposed],[[]],[])
        self.assertEqual(r['status'],'blocked');self.assertIn('StopIteration',r['reason'])
        self.assertEqual(len(inputs),1);self.assertEqual(questions,[])
        self.assertEqual(r['model_usage']['rlcd_calls'],1);self.assertEqual(r['model_usage']['rlcd_calls_completed'],0)
        self.assertIsNone(r['model_usage']['rlcd_input_tokens']);self.assertIsNone(r['model_usage']['rlcd_inference_ms'])

    def test_capability_gap_is_not_asked_as_user_ambiguity(self):
        proposal=self.proposed();proposal['goal_plan']['outcomes'][0]['evidence_plane']='saved_output'
        r,_,inputs,selectors,_,questions,_,owners=self.journey([proposal],[],[])
        self.assertEqual(r['reason'],'required_verification_capability_unavailable')
        self.assertEqual(len(inputs),1);self.assertEqual(questions,[]);self.assertEqual(selectors,[]);self.assertEqual(owners,0)


if __name__=='__main__':unittest.main()
