"""Locale evidence through the real goal verifier; synthetic captures only."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from locua.desktop_tools import DesktopTools, _digest
from locua.goal_verification import bind, verify
from locua.number_format import evidence_from_probe
from test_goal_verification import control, observation, outcome
from test_number_format import BUNDLE, probe


def locale_evidence(target, *, age_s=0, **format_options):
    now = time.time_ns()
    payload = probe(**format_options)
    payload["capture_finished_at_ns"] = now - int(age_s * 1_000_000_000)
    payload["capture_started_at_ns"] = payload["capture_finished_at_ns"] - 10_000_000
    return evidence_from_probe(payload, bundle_id=BUNDLE, target=target,
                               received_at_ns=now)


class LocaleGoalVerificationTests(unittest.TestCase):
    def bound(self, expression="192*231-100"):
        goal = outcome(expression=expression)
        initial = observation(control(value="0"))
        return goal, bind(goal, initial["controls"][1], initial), initial["target"]

    def test_grouped_result_matches_bound_display_with_conditional_locale_evidence(self):
        goal, binding, target = self.bound()
        result = verify(binding, goal,
            observation(control(value="\u200e44,252"), snapshot="fresh"),
            number_format=locale_evidence(target))
        self.assertEqual(result["status"], "matched", result)
        self.assertEqual(result["evidence"]["actual"], "\u200e44,252")
        interpretation = result["evidence"]["numeric_interpretation"]
        self.assertEqual(interpretation["canonical_ascii"], "44252")
        self.assertEqual(interpretation["target"], target)
        self.assertFalse(interpretation["application_formatter_proven"])
        self.assertEqual(result["evidence"]["plane"], "display")
        self.assertFalse(result["evidence"]["saved_output_proven"])
        self.assertFalse(result["evidence"]["committed_document_proven"])

    def test_invalid_locale_evidence_never_falls_back_to_matching_dot_decimal(self):
        goal, binding, target = self.bound("3*5")
        foreign_window = {**target, "window_id": target["window_id"] + 1}
        foreign_process = {**target, "pid": target["pid"] + 1}
        examples = {
            "stale": locale_evidence(target, age_s=61),
            "foreign_window": locale_evidence(foreign_window),
            "foreign_process": locale_evidence(foreign_process),
            "unknown": {"status": "unknown", "reason": "custom_numeric_overrides_unresolved"},
            "empty": {},
        }
        for label, evidence in examples.items():
            with self.subTest(label=label):
                result = verify(binding, goal, observation(control(value="15"), snapshot="fresh"),
                                number_format=evidence)
                self.assertEqual(result["status"], "unavailable", result)
                self.assertFalse(result["matched"])
                self.assertTrue(result["reason"].startswith("numeric_display_uninterpretable:"))
                self.assertIsNone(result["evidence"])

    def test_malformed_grouping_is_uninterpretable_not_a_numeric_mismatch(self):
        goal, binding, target = self.bound()
        for displayed in ("44,25", "4,4,252", "442,52", "44,,252", "4425,2"):
            with self.subTest(displayed=displayed):
                result = verify(binding, goal, observation(control(value=displayed), snapshot="fresh"),
                                number_format=locale_evidence(target))
                self.assertEqual(result["status"], "unavailable", result)
                self.assertFalse(result["matched"])

    def test_valid_grouping_with_wrong_number_is_mismatch(self):
        goal, binding, target = self.bound()
        result = verify(binding, goal, observation(control(value="44,253"), snapshot="fresh"),
                        number_format=locale_evidence(target))
        self.assertEqual(result["status"], "mismatch", result)
        self.assertEqual(result["evidence"]["numeric_interpretation"]["numerator"], 44253)

    def test_locale_interpretation_is_not_selected_from_the_expected_answer(self):
        goal, binding, target = self.bound("44252/1000")
        after = observation(control(value="44.252"), snapshot="fresh")
        german = locale_evidence(target, decimal=",", group=".", identifier="de_DE")
        result = verify(binding, goal, after, number_format=german)
        self.assertEqual(result["status"], "mismatch", result)
        self.assertEqual(result["evidence"]["numeric_interpretation"]["numerator"], 44252)
        self.assertEqual(verify(binding, goal, after)["status"], "matched")

    def test_correct_value_in_competing_control_does_not_replace_bound_readout(self):
        goal, binding, target = self.bound()
        after = observation(control(value="0"), control(name="History", value="44,252"), snapshot="fresh")
        result = verify(binding, goal, after, number_format=locale_evidence(target))
        self.assertEqual(result["status"], "mismatch", result)
        self.assertEqual(result["evidence"]["control_id"], "fresh:1")
        self.assertEqual(result["evidence"]["actual"], "0")

    def test_fresh_locale_does_not_refresh_stale_native_capture(self):
        goal, binding, target = self.bound()
        after = observation(control(value="44,252"), snapshot="stale")
        after["observed_at_ns"] = after["provenance"]["observed_at_ns"] = time.time_ns() - 40_000_000_000
        result = verify(binding, goal, after, number_format=locale_evidence(target))
        self.assertEqual(result["status"], "unavailable", result)
        self.assertFalse(result["matched"])

    def test_changed_locale_contract_fails_integrity_check(self):
        goal, binding, target = self.bound()
        evidence = deepcopy(locale_evidence(target))
        evidence["format"]["grouping_separator"] = " "
        result = verify(binding, goal, observation(control(value="44,252"), snapshot="fresh"),
                        number_format=evidence)
        self.assertEqual(result["status"], "unavailable", result)
        self.assertFalse(result["matched"])


class DesktopLocaleOwnershipTests(unittest.TestCase):
    """No driver, ps, preferences, compiler, filesystem bundle or app calls."""
    def setUp(self):
        self.app = {"bundle_id": BUNDLE, "name": "Synthetic", "launch_path": "/Applications/Synthetic.app"}
        self.target = {"pid": 41, "window_id": 52}
        self.process = {"pid": 41, "started_at_utc": "synthetic incarnation", "executable_path": "/mock/app/main"}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.desktop = DesktopTools.__new__(DesktopTools)
        self.desktop._lock = threading.RLock()
        self.desktop._apps = {_digest(self.app): deepcopy(self.app)}
        self.desktop.out = Path(self.temporary.name)
        self.events = []
        self.desktop.trace = self.events.append

    def test_read_only_probe_uses_issued_bundle_and_reconciles_process_incarnation(self):
        evidence = locale_evidence(self.target)
        with patch("locua.desktop_tools._bundle_executable", return_value={"executable_path": "/mock/app/main"}), \
             patch("locua.desktop_tools._process_identity", side_effect=[self.process, deepcopy(self.process)]) as process, \
             patch("locua.number_format.probe_app_number_format", return_value=evidence) as probe:
            result = self.desktop.number_format(deepcopy(self.app), self.target)
        self.assertEqual(result, evidence)
        self.assertEqual(process.call_count, 2)
        self.assertEqual([call.args for call in process.call_args_list], [(41,), (41,)])
        probe.assert_called_once_with(BUNDLE, self.target,
            cache_dir=self.desktop.out / "number-format-module-cache")
        self.assertEqual(len(self.events), 1)

    def test_foreign_executable_or_unissued_app_does_not_run_locale_probe(self):
        for app, executable in ((self.app, "/mock/another-app/main"),
                                ({**self.app, "bundle_id": "org.other.application"}, "/mock/app/main")):
            with self.subTest(app=app, executable=executable), \
                 patch("locua.desktop_tools._bundle_executable", return_value={"executable_path": "/mock/app/main"}), \
                 patch("locua.desktop_tools._process_identity", return_value={**self.process, "executable_path": executable}), \
                 patch("locua.number_format.probe_app_number_format") as probe:
                result = self.desktop.number_format(app, self.target)
                self.assertEqual(result["status"], "unknown", result)
                probe.assert_not_called()

    def test_changed_process_after_probe_discards_locale_evidence(self):
        with patch("locua.desktop_tools._bundle_executable", return_value={"executable_path": "/mock/app/main"}), \
             patch("locua.desktop_tools._process_identity", side_effect=[self.process, {**self.process, "started_at_utc": "new incarnation"}]), \
             patch("locua.number_format.probe_app_number_format", return_value=locale_evidence(self.target)):
            result = self.desktop.number_format(self.app, self.target)
        self.assertEqual(result["status"], "unknown", result)
        self.assertFalse(result["application_formatter_proven"])
        self.assertFalse(self.events)


if __name__ == "__main__":
    unittest.main()
