import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from locua.guided import catalog, compose, review_text, start


def near_field():
    return {'kind': 'browser_semantic_v2', 'snapshot_id': 's1',
            'controls': [
                {'id': 'group', 'role': 'group', 'name': 'Recording', 'parent': None},
                {'id': 'room', 'role': 'textbox', 'name': 'Room', 'parent': 'group',
                 'value': 'Willow', 'actions': ['type'], 'states': {},
                 'source': {'node': {'visibility': 'near_viewport'}},
                 'value_evidence': {'exact_value_proven': True}}],
            'handles': {'room': {'kind': 'browser'}}}


class GuidedRoutes(unittest.TestCase):
    def test_near_field_without_route_contract_is_reported_unavailable(self):
        result = catalog(near_field())
        self.assertEqual(result['fields'], [])
        self.assertEqual(result['unavailable'][0]['label'], 'Recording > Room')
        self.assertIn('no supported action route', result['unavailable'][0]['reason'])

    def test_supported_near_route_is_disclosed_in_review(self):
        # Capability proof is covered by the core/adapter tests. This checks its
        # presentation boundary, not a claim that this synthetic handle is valid.
        with patch('locua.engine.prototype.core.action_available', return_value=True):
            fields = catalog(near_field())['fields']
        plan = compose({'kind': 'browser', 'url': 'http://localhost/'}, fields, {1: 'Orchid'})
        review = review_text(plan, fields, {1: 'Orchid'}, 'comparator', browser_click_route='dom_event')
        self.assertIn('synthetic DOM clicks', review)
        self.assertIn('semantic reference typing', review)
        self.assertIn('Near viewport:', review)
        self.assertIn('may reveal/scroll', review)

    def test_discovery_receives_same_explicit_route_as_review(self):
        with tempfile.TemporaryDirectory() as td:
            answers = iter(['1', 'Orchid', '', 'cancel'])
            prep = {'scope': {'kind': 'browser', 'url': 'http://localhost/'},
                    'target_label': 'Local recording form', 'observation': near_field()}
            with patch('locua.lib._engine', return_value={'result': prep}) as prepare, \
                 patch('locua.lib.run') as execute, \
                 patch('locua.engine.prototype.core.action_available', return_value=True), \
                 patch('locua.engine.prototype.observation_tools.overview', return_value={'items': []}):
                result = start(url='http://localhost/', browser_click_route='dom_event',
                               out=Path(td)/'run', ask=lambda _: next(answers), progress=lambda _: None)
            self.assertEqual(prepare.call_args.args[1]['browser_click_route'], 'dom_event')
            self.assertEqual(result['status'], 'canceled')
            execute.assert_not_called()
