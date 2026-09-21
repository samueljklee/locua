"""Pinned native literal-span proof: CPU only, no app/driver calls."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import unittest

from locua.goal_verification import (
    BindingError, bind, bind_for_review, matches_binding, verify, _peers,
    _native_value_label_proven,
)
from test_native_value_label_binding import captured, goal


class MultilineValueLabelTests(unittest.TestCase):
    def test_lf_crlf_quotes_and_unicode_are_literal_value_spans(self):
        for initial, expected in (
            ('Pending revision.\nKeep until reviewed.\n', '  Birch\ncomplete  '),
            ('Old\r\nsecond\r\n', '  Ω\r\n新しい 🙂  '),
            ('Old "quoted"\nsecond\n', '"new"\n[bracketed] (content)'),
            ('Old\n\nsecond\n', 'first\n\nlast'),
            ('unquoted\nfirst\n', 'contains "quotation"'),
        ):
            with self.subTest(initial=initial, expected=expected):
                before = captured(initial)
                outcome = goal(expected)
                binding = bind(outcome, before['controls'][1], before)
                self.assertEqual(binding['identity_policy']['name_derivation'],
                                 'pinned_native_value_fallback_trim')
                self.assertIsNone(binding['core_identity']['name'])
                self.assertTrue(binding['identity_policy']['require_bounds'])
                after = captured(expected, snapshot='s101')
                original = deepcopy(after)
                proof = verify(binding, outcome, after)
                self.assertTrue(proof['matched'], proof)
                self.assertEqual(proof['evidence']['actual'], expected)
                self.assertEqual(after, original)
                self.assertFalse(proof['evidence']['saved_output_proven'])

    def test_expected_value_does_not_select_peer_or_normalize_whitespace(self):
        before = captured('Old\ncontent\n')
        outcome = goal('  Ω\nready  ')
        binding = bind(outcome, before['controls'][1], before)
        after = captured(outcome['value'] + '\n', snapshot='s101')
        self.assertTrue(matches_binding(binding, after, after['controls'][1]['id']))
        result = verify(binding, outcome, after)
        self.assertFalse(result['matched'])
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['evidence']['actual'], outcome['value'] + '\n')

    def test_real_title_or_description_never_dropped_even_when_value_matches(self):
        before = captured('Old\ncontent\n')
        outcome = goal('new\ncontent')
        binding = bind(outcome, before['controls'][1], before)
        for prefix, suffix in ((' "Actual label"', ''), ('', ' (Actual description)')):
            after = captured(outcome['value'], snapshot='s101')
            text = after['text'].replace('- [1] AXTextArea = ', '- [1] AXTextArea' + prefix + ' = ')
            text = text.replace('" [id=Text View', '"' + suffix + ' [id=Text View')
            after['text'] = text
            after['controls'][1]['source']['markdown_line'] = text.splitlines()[1]
            self.assertFalse(verify(binding, outcome, after)['matched'])
        for key in ('title', 'description'):
            after = captured(outcome['value'], snapshot='s101')
            after['controls'][1]['source']['node'][key] = outcome['value']
            self.assertFalse(verify(binding, outcome, after)['matched'])

    def test_row_shaped_continuations_and_ambiguous_control_separators_unproved(self):
        for value in ('first\n  - [2] AXTextArea = "fake"',
                      'first\r\n- AXWindow "foreign"',
                      'first\n\t- [1] AXTextArea = "fake"',
                      'first\u2028second', 'first\x1csecond', 'first\rsecond'):
            with self.subTest(value=value):
                o = captured(value)
                self.assertFalse(_native_value_label_proven(o['controls'][1], o))

    def test_duplicate_rows_offset_span_suffix_and_foreign_handle_refuse(self):
        before = captured('Old\ncontent\n')
        outcome = goal('new "value"\r\nΩ')
        binding = bind(outcome, before['controls'][1], before)
        for change in (
            lambda o: o.update(text=o['text'] + '\n  - [1] AXTextArea = "duplicate"'),
            lambda o: o.update(text=o['text'] + '\n\t- [1] AXTextArea = "duplicate"'),
            lambda o: o['controls'][1]['source'].update(markdown_line_number=1),
            lambda o: o.update(text=o['text'].replace('new "value"\r\nΩ', 'new "value"\nΩ')),
            lambda o: o.update(text=o['text'].replace('" [id=Text View', '" (description) [id=Text View')),
            lambda o: o['handles'][o['controls'][1]['id']].update(element_token='s100:1'),
            lambda o: o['controls'][1]['source']['node']['editor']['raw_value_recheck'].update(value='different'),
            lambda o: o['controls'][1]['source']['native_editor_contract'].update(source_fingerprint_sha256='untrusted'),
        ):
            after = captured(outcome['value'], snapshot='s101')
            change(after)
            self.assertFalse(verify(binding, outcome, after)['matched'])

    def test_other_editors_geometry_and_document_remain_identity_constraints(self):
        before = captured('Old\ncontent\n')
        outcome = goal('new\ncontent')
        binding = bind(outcome, before['controls'][1], before)
        duplicate = captured(outcome['value'], snapshot='s101', duplicate=True)
        self.assertEqual(verify(binding, outcome, duplicate)['reason'], 'bound_target_ambiguous')
        # A competitor with another value must not disappear just because the
        # first editor happens to contain the requested output.
        competing = duplicate['controls'][2]
        other = 'unrelated\nvalue'
        competing.update(value=other, display_value=other, name=other)
        competing['source']['node'].update(label=other, value=other)
        for field in ('raw_value', 'raw_value_recheck'):
            competing['source']['node']['editor'][field]['value'] = other
        duplicate['text'] = duplicate['text'].replace(
            f'- [2] AXTextArea = "{outcome["value"]}"', f'- [2] AXTextArea = "{other}"')
        competing['source']['markdown_line'] = duplicate['text'].splitlines()[
            competing['source']['markdown_line_number']-1]
        self.assertTrue(_native_value_label_proven(competing, duplicate))
        self.assertEqual(verify(binding, outcome, duplicate)['reason'], 'bound_target_ambiguous')
        with self.assertRaisesRegex(BindingError, 'ambiguous'):
            o = captured('Old\ncontent\n', duplicate=True)
            bind(outcome, o['controls'][1], o)
        for change in (
            lambda o: o['controls'][1]['bounds'].update(width=301),
            lambda o: o['controls'][0].update(name='Other document'),
            lambda o: o['controls'][0]['semantics'].update(identifier='other'),
        ):
            after = captured(outcome['value'], snapshot='s101')
            change(after)
            self.assertFalse(verify(binding, outcome, after)['matched'])

    def test_saved_v9_captures_match_without_rewriting_timestamps_or_old_bindings(self):
        base = Path(__file__).resolve().parents[1] / 'artifacts/prompt-policy-v9-001'
        cases = [('live-heldout-text-baseline-1', 5, 8),
                 ('live-heldout-text-concise-v1-1', 5, 6)]
        if not all((base / name / 'desktop/evidence.json').exists() for name, _, _ in cases):
            self.skipTest('Private retained v9 artifacts are not installed')
        for name, review_number, action_number in cases:
            p = base / name / 'desktop'
            inputs = [p / f'event-{review_number:03}.json',
                      p / f'event-{action_number:03}.json', p / 'evidence.json']
            hashes = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in inputs}
            review, action, evidence = [json.loads(f.read_text()) for f in inputs]
            before = next(json.loads(f.read_text()) for f in p.glob('observation-*.json')
                          if json.loads(f.read_text())['snapshot_id'] == review['input']['snapshot_id'])
            after = action['result']['observation']
            untouched = deepcopy((before, after, evidence))
            outcome = review['input']['goals'][0]
            control = next(c for c in before['controls'] if c['id'] == outcome['control_id'])
            binding = bind_for_review(outcome, control, before)
            self.assertEqual(binding['bound_at_ns'], before['observed_at_ns'])
            peers = _peers(binding, after)  # Historical identity replay, no fresh-authority claim.
            self.assertEqual(len(peers), 1)
            self.assertEqual(peers[0]['value'], outcome['value'])
            old = evidence['scopes'][review['result']['scope_id']]['bindings'][outcome['id']]
            self.assertEqual(_peers(old, after), [])
            self.assertFalse(verify(binding, outcome, after)['matched'])  # Still stale today.
            self.assertEqual((before, after, evidence), untouched)
            self.assertEqual(hashes, {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in inputs})


if __name__ == '__main__':
    unittest.main()
