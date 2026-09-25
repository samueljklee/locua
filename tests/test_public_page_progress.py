"""Public page progress through actual tools and Amplifier callbacks; CPU only."""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_tools import DesktopToolset
from locua.model_interface import ModelInterface
from tests.test_amplifier_tools import Desktop


class LargeDesktop(Desktop):
    def observe(self,target):
        result=super().observe(target)
        if result.get('status')!='observed':return result
        o=result['observation'];sid=o['snapshot_id']
        for n in range(65):
            o['controls'].append({'id':f'{sid}:meter{n}','parent':f'{sid}:0','role':'AXStaticText',
                'name':f'Meter {n}','value':f'Reading {n}','states':{},'actions':[],
                'semantics':{'help':'This captured status has contextual description. '*7},
                'bounds':{'x':10,'y':n*10,'width':100,'height':8}})
        return result

class PublicProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.owners=[]
    def owner(self,profile,desktop=None):
        desktop=desktop or LargeDesktop()
        owner=DesktopToolset({},Path(self.tmp.name)/str(len(self.owners)),'Read the displayed meters without changing anything.',lambda *_:'run',desktop=desktop,tool_profile=profile)
        self.owners.append(owner);self.addCleanup(owner.close)
        ui=owner.model_interface
        app=ui.call('locua_apps',{'query':'Tool surface'})['items'][0]['app_id']
        window=ui.call('locua_windows',{'app_id':app})['windows'][0]['window_id']
        view=ui.call('locua_observe',{'window_id':window})['view']
        return owner,ui,view
    def test_all_projected_interfaces_count_new_pages_as_progress_without_false_feedback(self):
        for profile in ['continuity-v1','semantic-v1','semantic-v2']:
            with self.subTest(profile=profile):
                owner,ui,view=self.owner(profile)
                args={'view':view,'role':'AXStaticText','query':'Meter','limit':32}
                targets=[];calls=0
                while args:
                    result=ui.call('locua_inspect',args);calls+=1
                    self.assertEqual(result['status'],'ok',result)
                    feedback=result['exploration_feedback']
                    self.assertEqual(feedback['basis'],'delivered_public_page')
                    self.assertEqual(feedback['equivalent_inspection_count'],1)
                    self.assertTrue(feedback['new_information'])
                    self.assertNotIn('code',feedback)
                    self.assertNotIn('nonprogress',result)
                    self.assertNotIn('execution_stopped',result)
                    self.assertIsNone(owner._cancellation)
                    targets.extend(r['target'] for r in result['items'])
                    args=result['coverage'].get('continue_with')
                    self.assertLess(calls,30)
                self.assertGreater(calls,2)
                self.assertEqual(len(targets),65);self.assertEqual(len(set(targets)),65)
                self.assertEqual(owner.desktop.executions,[])
    def test_exact_same_public_page_still_warns_then_stops(self):
        for profile in ['continuity-v1','semantic-v1','semantic-v2']:
            with self.subTest(profile=profile):
                owner,ui,view=self.owner(profile)
                first=ui.call('locua_inspect',{'view':view,'query':'Meter','limit':32})
                args=first['coverage']['continue_with']
                pages=[]
                for count in [1,2,3]:
                    page=ui.call('locua_inspect',args);pages.append(page)
                    f=page['exploration_feedback'];self.assertEqual(f['equivalent_inspection_count'],count)
                    self.assertEqual(f['new_information'],count==1)
                    self.assertEqual(f.get('code'),'repeated_inspection' if count>1 else None)
                self.assertEqual(pages[0]['items'],pages[1]['items']);self.assertEqual(pages[1]['items'],pages[2]['items'])
                self.assertEqual(owner._cancellation['reason'],'nonprogress_limit')
                self.assertTrue(pages[-1]['execution_stopped']);self.assertEqual(owner.desktop.executions,[])
    def test_unknown_cursor_remains_a_refusal_and_genuine_new_detail_is_progress(self):
        for profile in ['continuity-v1','semantic-v1','semantic-v2']:
            with self.subTest(profile=profile):
                owner,ui,view=self.owner(profile)
                listed=ui.call('locua_inspect',{'view':view,'query':'Meter 0','limit':32})
                target=listed['items'][0]['target']
                detail=ui.call('locua_inspect',{'view':view,'target':target})
                self.assertTrue(detail['exploration_feedback']['new_information'])
                refused=ui.call('locua_inspect',{'view':view,'cursor':'not_observed'})
                self.assertEqual(refused['status'],'refused')
                self.assertNotIn('exploration_feedback',refused)
                self.assertEqual(owner.desktop.executions,[])
    def test_source_and_published_contracts_are_not_modified(self):
        for profile in ['continuity-v1','semantic-v1','semantic-v2']:
            owner,ui,view=self.owner(profile)
            expected=ModelInterface(owner)
            self.assertEqual([(t.name,t.description,t.input_schema) for t in ui.tools()],[(t.name,t.description,t.input_schema) for t in expected.tools()])


async def run_amplifier_progress(out, *, repeat=False):
    from amplifier_core.message_models import ChatResponse, TextBlock, ToolCall, ToolCallBlock, Usage
    from locua.amplifier_provider import LocalAmplifierProvider, native_request
    from locua.amplifier_session import execute_session
    out=Path(out);progress=[];rendered=[]
    owner=DesktopToolset({},out/'desktop','Read the observed meters without input.',lambda *_:'run',
        desktop=LargeDesktop(),tool_profile='semantic-v1',progress=progress.append)
    ui=owner.model_interface
    app=ui.call('locua_apps',{'query':'Tool surface'})['items'][0]['app_id']
    window=ui.call('locua_windows',{'app_id':app})['windows'][0]['window_id']
    view=ui.call('locua_observe',{'window_id':window})['view']
    class Provider(LocalAmplifierProvider):
        request_budget=None
        repeat_args=None
        async def complete(self, request, **kwargs):
            native=native_request(request,model='qwen38',structured_tool_results=self._structured_tool_results)
            rendered.append(native);number=len(rendered)
            (out/f'provider-input-{number:03}.json').write_text(json.dumps(native,ensure_ascii=False,indent=2)+'\n')
            if number==1:
                args={'view':view,'role':'AXStaticText','query':'Meter','limit':32}
            else:
                tool=next(m for m in reversed(native['messages']) if m['role']=='tool')
                result=json.loads(tool['content'])
                while isinstance(result.get('output'),dict):result=result['output']
                args=result.get('coverage',{}).get('continue_with')
                if repeat:
                    if self.repeat_args is None:self.repeat_args=deepcopy(args)
                    args=self.repeat_args
            usage=Usage(input_tokens=1,output_tokens=1,total_tokens=2)
            if not args:
                return ChatResponse(content=[TextBlock(text='The captured pages were read; no input was issued.')],usage=usage)
            cid='scripted-'+str(number)
            return ChatResponse(content=[ToolCallBlock(id=cid,name='locua_inspect',input=args)],
                tool_calls=[ToolCall(id=cid,name='locua_inspect',arguments=args)],usage=usage)
    provider=Provider(model='qwen38')
    try:
        with patch('locua.engine_adapter.runtime_environment',return_value=nullcontext()):
            session=await execute_session('Read the observed meters without input.',provider,ui.tools(),out=out/'session',
                progress=progress.append,max_iterations=12,owner_cancellation=lambda:deepcopy(owner._cancellation))
        report={'progress':progress,'provider_decisions':len(rendered),'session_cleanup':session['session_cleanup'],
            'cancellation':deepcopy(owner._cancellation),'desktop_inputs':len(owner.desktop.executions),
            'model_generations':0,'gui_calls':0,'repeat_fixture':repeat,
            'warning_lines':[line for line in progress if line.startswith('Repeated inspection')],
            'inspections':[e['data']['result']['output'] for e in session['events'] if e['event']=='tool:post' and e['data'].get('tool_name')=='locua_inspect']}
        (out/'progress-proof.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report
    finally:
        owner.close();await provider.close()


@unittest.skipUnless(importlib.util.find_spec('amplifier_core'),'Optional Amplifier extra absent')
class ActualAmplifierProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_progress_callback_has_no_false_continuation_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=await run_amplifier_progress(Path(tmp)/'run')
        self.assertEqual(report['warning_lines'],[])
        self.assertEqual(report['session_cleanup'],'closed')
        self.assertEqual(report['desktop_inputs'],0)
        self.assertIsNone(report['cancellation'])
        self.assertEqual(len(report['inspections']),5)
        self.assertTrue(all(r['exploration_feedback']['new_information'] for r in report['inspections']))
        self.assertTrue(all(any(f'{len(r["items"])} entries returned' in line for line in report['progress']) for r in report['inspections']))
    async def test_real_progress_warns_and_stops_genuine_repeated_public_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=await run_amplifier_progress(Path(tmp)/'run',repeat=True)
        self.assertEqual(len(report['warning_lines']),2)
        self.assertIn('(2 times)',report['warning_lines'][0])
        self.assertIn('(3 times)',report['warning_lines'][1])
        self.assertEqual(report['cancellation']['reason'],'nonprogress_limit')
        self.assertEqual(report['provider_decisions'],4)
        self.assertEqual(report['desktop_inputs'],0)
        self.assertEqual(report['session_cleanup'],'closed')

if __name__=='__main__':unittest.main()
