"""CPU-only persistence contract tests; no application or model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_tools import DesktopToolset
from locua.amplifier_contracts import validate_tool_arguments, ArgumentContractError, SPECS, LEGACY_PERSISTENCE_CONTRACT
from tests.test_amplifier_tools import Desktop


class AutosavingDesktop(Desktop):
    def __init__(self):
        super().__init__(); self.backing_bytes = b'initial'
    def execute(self, action, observation):
        result = super().execute(action, observation)
        if action['kind'] == 'set_text':
            self.backing_bytes = self.value.encode()
        return result


class PersistenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.reviews = []
        self.desktop = AutosavingDesktop()
        def ask(message, purpose):
            self.reviews.append(message); return 'run'
        self.owner = DesktopToolset({}, Path(self.tmp.name)/'out',
            'Replace Entry with exact λ. Preserve any explicitly reviewed backing-file requirement.',
            ask, desktop=self.desktop, tool_profile='semantic-v1', persistence_contract='text-persistence-v1')
        self.ui = self.owner.model_interface
        app = self.ui.call('locua_apps', {'query': 'Tool surface'})['items'][0]['app_id']
        window = self.ui.call('locua_windows', {'app_id': app})['windows'][0]['window_id']
        self.view = self.ui.call('locua_observe', {'window_id': window})['view']
        rows = self.ui.call('locua_inspect', {'view': self.view})['items']
        self.entry = next(r['target'] for r in rows if r.get('name') == 'Entry')

    def tearDown(self):
        self.owner.close(); self.tmp.cleanup()

    def review(self, requirement='not_requested', **extra):
        goal = {'kind': 'text', 'target': self.entry, 'value': 'exact λ'}
        if requirement is not None: goal['persistence_requirement'] = requirement
        return self.ui.call('locua_review', {'summary': 'Replace the exact buffer; explicitly review persistence.',
            'goals': [goal], 'covers_request': True, **extra})

    def act(self):
        return self.ui.call('locua_act', {'target': self.entry, 'operation': 'set_text', 'value': 'exact λ'})

    def test_missing_typed_requirement_has_no_silent_default(self):
        result = self.review(None)
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('persistence_requirement', json.dumps(result))
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)
        self.assertEqual(self.owner.finalize()['status'], 'blocked')

    def test_raw_and_compact_envelopes_report_blocking_reason_as_failure(self):
        import asyncio
        from locua.amplifier_tools import _Tool
        from locua.amplifier_session import _tool_progress
        args = {'snapshot_id': 'missing', 'summary': 'Preserve backing bytes.',
            'goals': [{'id': 'g1', 'kind': 'text', 'target': 'Entry', 'control_id': 'missing:1',
                      'value': 'exact λ', 'evidence_plane': 'editor_buffer',
                      'persistence_requirement': 'backing_file_unchanged'}],
            'effects': [{'kind': 'goal', 'goal_id': 'g1'}]}
        raw = asyncio.run(_Tool(self.owner, 'locua_review').execute(args))
        compact_tool = next(t for t in self.ui.tools() if t.name == 'locua_review')
        compact = asyncio.run(compact_tool.execute({'summary': 'Preserve backing bytes.',
            'goals': [{'kind': 'text', 'target': self.entry, 'value': 'exact λ',
                       'persistence_requirement': 'backing_file_unchanged'}], 'covers_request': True}))
        for receipt in (raw, compact):
            self.assertFalse(receipt.success)
            self.assertEqual(receipt.output['status'], 'blocked')
            progress = _tool_progress({'name': 'locua_review', 'result': receipt})
            self.assertIn('cannot ensure the saved file stays unchanged', ' '.join(progress))
        self.assertFalse(self.desktop.executions); self.assertFalse(self.reviews)

    def test_raw_amplifier_schema_also_requires_same_field(self):
        observation, control = self.ui.control(self.entry)
        args = {'snapshot_id': observation['snapshot_id'], 'summary': 'Replace buffer',
            'goals': [{'id': 'g1', 'kind': 'text', 'target': 'Entry', 'control_id': control['id'],
                       'value': 'exact λ', 'evidence_plane': 'editor_buffer'}],
            'effects': [{'kind': 'goal', 'goal_id': 'g1'}]}
        with self.assertRaises(ArgumentContractError): validate_tool_arguments('locua_review', args, specs=self.owner._specs)
        args['goals'][0]['persistence_requirement'] = 'backing_file_unchanged'
        validate_tool_arguments('locua_review', args, specs=self.owner._specs)
        args['goals'][0]['persistence_requirement'] = 'pretend_autosave_is_off'
        with self.assertRaises(ArgumentContractError): validate_tool_arguments('locua_review', args, specs=self.owner._specs)

    def test_unchanged_backing_requirement_stops_before_approval_and_input(self):
        result = self.review('backing_file_unchanged')
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertFalse(result['action_started']); self.assertFalse(self.reviews)
        self.assertEqual(self.desktop.backing_bytes, b'initial')
        self.assertFalse(self.desktop.executions); self.assertFalse(self.owner._scopes)
        self.assertIn('backing_file_unchanged', json.dumps(self.ui.state()))

    def test_saved_output_requirement_is_explicitly_unsupported(self):
        result = self.review('saved_output_required')
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)
        final = self.owner.finalize()
        self.assertEqual(final['status'], 'blocked')
        self.assertFalse(final['saved_output_proven'])

    def test_declared_requirement_cannot_be_erased_by_later_not_requested(self):
        self.review('backing_file_unchanged')
        result = self.review('not_requested')
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertIn('backing_file_unchanged', json.dumps(result))
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)

    def test_public_restriction_survives_unknown_target_before_binding(self):
        result = self.ui.call('locua_review', {'summary': 'Preserve backing bytes.',
            'goals': [{'kind': 'text', 'target': 'invented', 'value': 'exact λ',
                       'persistence_requirement': 'backing_file_unchanged'}], 'covers_request': True})
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertEqual(result['persistence']['stage'], 'before_reference_binding')
        self.assertTrue(self.owner.evidence['unmet_persistence_requirements'])
        self.assertEqual(self.review()['code'], 'text_persistence_unmet')
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)

    def test_public_restriction_survives_expired_control_observation(self):
        observation, _ = self.ui.control(self.entry)
        self.owner._observations.pop(observation['snapshot_id'])
        self.owner._latest = {key: sid for key, sid in self.owner._latest.items()
                              if sid != observation['snapshot_id']}
        result = self.review('saved_output_required')
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertEqual(self.review()['code'], 'text_persistence_unmet')
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)

    def test_raw_restriction_survives_unknown_snapshot_before_binding(self):
        args = {'snapshot_id': 'missing', 'summary': 'Preserve backing bytes.',
            'goals': [{'id': 'g1', 'kind': 'text', 'target': 'Entry', 'control_id': 'missing:1',
                      'value': 'exact λ', 'evidence_plane': 'editor_buffer',
                      'persistence_requirement': 'backing_file_unchanged'}],
            'effects': [{'kind': 'goal', 'goal_id': 'g1'}]}
        result = self.owner.call('locua_review', args)
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertEqual(result['persistence']['stage'], 'before_snapshot_binding')
        self.assertEqual(self.review()['code'], 'text_persistence_unmet')
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)

    def test_malformed_review_is_rejected_before_latching(self):
        result = self.ui.call('locua_review', {'summary': 'Invalid declaration.',
            'goals': [{'kind': 'text', 'target': 'invented', 'value': 'exact λ',
                       'persistence_requirement': 'backing_file_unchanged', 'invented': True}],
            'covers_request': True})
        self.assertEqual(result['code'], 'argument_contract_invalid', result)
        self.assertFalse(self.owner.evidence.get('unmet_persistence_requirements'))
        self.assertFalse(self.reviews); self.assertFalse(self.desktop.executions)

    def test_tool_help_profiles_preserve_active_persistence_contract(self):
        from locua.instruction_policy import apply_tool_help
        from locua.amplifier_contracts import TEXT_PERSISTENCE_GUIDANCE
        for profile in ('baseline', 'continuity-v1', 'continuity-arguments-v1', 'principles-help-v1', 'concise-help-v1'):
            with self.subTest(profile=profile):
                tool = next(t for t in apply_tool_help(self.ui.tools(), profile) if t.name == 'locua_review')
                self.assertEqual(tool.description.count(TEXT_PERSISTENCE_GUIDANCE), 1)

    def test_exact_buffer_and_no_save_do_not_prove_unchanged_autosaved_bytes(self):
        self.review('backing_file_unchanged')
        # External fixture change simulates the demonstrated backing-store
        # contradiction. No Locua Save or task input is issued by this test.
        self.desktop.value = 'exact λ'; self.desktop.backing_bytes = 'exact λ'.encode()
        final = self.owner.finalize()
        self.assertEqual(final['status'], 'blocked')
        self.assertFalse(final['all_reviewed_goals_verified'])
        self.assertEqual(final['backing_file_status'], 'unknown')
        self.assertIsNone(final['explicit_save_dispatched'])
        self.assertFalse(self.desktop.executions)

    def test_no_declared_persistence_constraint_has_honest_buffer_only_result(self):
        result = self.review()
        self.assertEqual(result['status'], 'approved', result)
        self.assertIn('Persistence requirement: not_requested', self.reviews[0])
        self.assertIn('may autosave', self.reviews[0])
        self.assertIn('not_requested', json.dumps(self.ui.state()))
        self.assertIn(self.act()['status'], ('verified', 'dispatched'))
        final = self.owner.finalize()
        self.assertEqual(final['status'], 'verified_reviewed_scope', final)
        self.assertEqual(self.desktop.backing_bytes, 'exact λ'.encode())
        self.assertEqual(final['backing_file_status'], 'unknown')
        self.assertFalse(final['saved_output_proven'])
        self.assertIsNone(final['explicit_save_dispatched'])
        self.assertIn('neither unchanged', ' '.join(final['limits']))

    def test_preinput_rechecks_requirement_instead_of_trusting_old_approval(self):
        self.assertEqual(self.review()['status'], 'approved')
        scope = next(iter(self.owner._scopes.values()))
        scope['goals'][0]['persistence_requirement'] = 'backing_file_unchanged'
        result = self.act()
        self.assertEqual(result['code'], 'text_persistence_unmet', result)
        self.assertFalse(self.desktop.executions)

    def test_final_verification_cannot_accept_forged_saved_requirement_from_buffer(self):
        self.assertEqual(self.review()['status'], 'approved')
        self.desktop.value = 'exact λ'
        scope = next(iter(self.owner._scopes.values()))
        scope['goals'][0]['persistence_requirement'] = 'saved_output_required'
        result = self.ui.call('locua_verify', {})
        self.assertEqual(result['status'], 'unverified', result)
        final = self.owner.finalize()
        self.assertEqual(final['status'], 'blocked')
        self.assertFalse(final['all_reviewed_goals_verified'])

    def test_published_and_validated_strict_schema_share_the_same_source(self):
        tool = next(t for t in self.ui.tools() if t.name == 'locua_review')
        self.assertEqual(tool.input_schema, self.ui.specs['locua_review'][1])
        branch = tool.input_schema['properties']['goals']['items']['oneOf'][0]
        self.assertIn('persistence_requirement', branch['required'])
        self.assertEqual(self.owner.evidence['persistence_contract'], 'text-persistence-v1')

    def test_legacy_owner_schema_is_retained_and_marked_comparison_only(self):
        legacy = DesktopToolset({}, Path(self.tmp.name)/'legacy', 'Legacy comparison only.',
            lambda *args: 'run', desktop=Desktop())
        try:
            tool = next(t for t in legacy.tools() if t.name == 'locua_review')
            self.assertEqual(tool.input_schema, SPECS['locua_review'][1])
            self.assertNotIn('persistence_requirement', tool.input_schema['properties']['goals']['items']['properties'])
            self.assertEqual(legacy.evidence['persistence_contract'], LEGACY_PERSISTENCE_CONTRACT)
        finally: legacy.close()

    def test_ordinary_cli_session_enables_contract_before_any_driver_or_model(self):
        from locua import amplifier_session
        captured = []
        class Lease:
            closed = False
            def __enter__(self): return self
            def __exit__(self, *args): self.closed = True
        def constructor(*args, **kwargs):
            captured.append(kwargs)
            raise RuntimeError('CPU constructor interception; no desktop service')
        with patch('locua.amplifier_tools.DesktopToolset', side_effect=constructor), \
             patch('locua.engine_adapter.require', return_value=None), \
             patch('locua.lib._config', return_value={}), \
             patch('locua.desktop_session_lock.acquire_desktop_session', return_value=Lease()):
            result = amplifier_session.run('A complete ordinary-language outcome.', model='qwen38',
                out=Path(self.tmp.name)/'ordinary-cli', config={}, ask=lambda *args: 'run', progress=lambda text: None)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['persistence_contract'], 'text-persistence-v1')
        self.assertEqual(result['persistence_contract'], 'text-persistence-v1')
        self.assertEqual(result['metrics']['model_calls'], 0)


if __name__ == '__main__': unittest.main(verbosity=2)
