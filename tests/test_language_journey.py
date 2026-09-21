"""Reviewed language-to-engine boundaries; synthetic proposals are not model evidence."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.conversation import run
from locua.guided import catalog
from test_guided import observed


class JourneyTests(unittest.TestCase):
    request = "Set the Project Title to 'Autumn'; keep the Defaults Title unchanged."

    def plan(self, request=None, plane='editor_buffer'):
        f = catalog(observed())['fields']
        return {'version':'locua-task-plan-v1', 'request':request or self.request,
                'scope':{'kind':'browser','url':'http://localhost/'},
                'outcomes':[{'id':'change-title','subject':f[1]['subject'], 'property':'value','value':'Autumn',
                             'requires':[], 'evidence_plane':plane, 'source_text':'Autumn'}],
                'constraints':[{'subject':f[0]['subject'],'property':'value','value':'default',
                                'source_text':'keep the Defaults Title unchanged'}], 'unknowns':[]}

    def journey(self, proposals, answers=('run',), **kwargs):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            folder=Path(tmp)/'journey'; responses=iter(answers)
            prep={'observation':observed(), 'target_label':'Synthetic Project',
                  'scope':{'kind':'browser','url':'http://localhost/'}}
            stack.enter_context(patch('locua.lib._engine', return_value={'result':prep}))
            stack.enter_context(patch('locua.lib._config', return_value={}))
            interpret=stack.enter_context(patch('locua.language_planning.interpret', side_effect=proposals))
            def execute(**payload):
                d=Path(payload['out']);d.mkdir()
                (d/'task-state.json').write_text(json.dumps({'outcomes':{'change-title':'verified'}}))
                return {'result':{'status':'complete','artifacts':str(d),'issued_actions':1,'end_to_end_wall_s':2.0}}
            execution=stack.enter_context(patch('locua.lib.run', side_effect=execute))
            result=run(self.request,url='http://localhost/',out=folder,ask=lambda _:next(responses),progress=lambda _:None,**kwargs)
            saved=json.loads((folder/'summary.json').read_text())
            review=(folder/'review.txt').read_text() if (folder/'review.txt').exists() else None
            return result,saved,interpret,execution,review

    def proposal(self, **kwargs):
        return {'status':'proposed','plan':self.plan(**kwargs), 'validation_data':{'observed-default':'default'}}

    def test_faithful_reviewed_plan_reaches_engine_and_preservation_data_is_explicit(self):
        result,saved,interpret,execute,review=self.journey([self.proposal()])
        self.assertEqual(result['status'],'complete'); self.assertEqual(saved,result)
        self.assertIn(self.request,review); self.assertIn('Preserve:',review)
        self.assertIn('7B',review); self.assertIn('original RLCD',review)
        payload=execute.call_args.kwargs
        self.assertEqual(payload['task']['request'],self.request)
        self.assertEqual(payload['supplied_data'],{'observed-default':'default'})
        self.assertEqual(payload['inspection_policy'],'reviewed_target_first')
        self.assertTrue(payload['execute']);self.assertFalse(result['saved_output_proven'])
        self.assertLessEqual(result['wall_excluding_human_s'],result['full_workflow_wall_s'])
        self.assertEqual(result['human_assistance_count'],0)

    def test_declined_review_never_dispatches(self):
        result,_,_,execute,_=self.journey([self.proposal()],answers=('',))
        execute.assert_not_called();self.assertEqual(result['reason'],'review_not_approved')
        self.assertFalse(result['task_actions_started'])

    def test_unresolved_interpretation_does_not_fall_back_to_field_picker(self):
        result,_,_,execute,review=self.journey([{'status':'blocked','reason':'unsupported_navigation'}],answers=())
        execute.assert_not_called();self.assertIsNone(review)
        self.assertEqual(result['reason'],'unsupported_navigation')

    def test_required_save_never_downgraded_to_editor_execution(self):
        result,_,_,execute,_=self.journey([self.proposal(plane='saved_output')],answers=())
        execute.assert_not_called();self.assertEqual(result['reason'],'required_document_or_saved_evidence_not_supported')

    def test_model_cannot_change_scope_or_request_before_review(self):
        proposal=self.proposal();proposal['plan']['scope']['url']='http://other/'
        result,_,_,execute,review=self.journey([proposal],answers=())
        self.assertEqual(result['status'],'blocked');execute.assert_not_called();self.assertIsNone(review)

    def test_clarification_is_recorded_and_interpreted_not_used_as_direct_field_authority(self):
        clarified=self.request+'\nClarification: The evening project'
        result,_,interpret,execute,_=self.journey([
            {'status':'clarification','questions':['Which project?']},self.proposal(request=clarified)],
            answers=('The evening project','run'))
        self.assertEqual(result['status'],'complete');self.assertEqual(result['human_assistance_count'],1)
        self.assertEqual(interpret.call_args.args[0],clarified)
        self.assertEqual(execute.call_args.kwargs['task']['request'],clarified)

    def test_url_literal_is_data_not_navigation_authority(self):
        with tempfile.TemporaryDirectory() as tmp, patch('locua.lib._config',return_value={}), \
             patch('locua.lib.targets',return_value={'result':{'targets':{'windows':[]}}}), \
             patch('locua.lib._engine') as prepare:
            result=run('Type "https://example.test/" into the URL field in Code',out=Path(tmp)/'run',
                       ask=lambda _:self.fail('no permission inferred'),progress=lambda _:None)
            self.assertEqual(result['error']['code'],'no_open_target')
            prepare.assert_not_called()


if __name__=='__main__': unittest.main()
