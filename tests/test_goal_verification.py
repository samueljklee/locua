"""Pure synthetic native bindings: no apps, model inference or GUI actions."""
from copy import deepcopy
import time
import unittest

from locua.goal_verification import BindingError, bind, matches_binding, verify
from locua.engine.prototype.perception import normalize_observation
from locua.engine.prototype.native_driver_contract import CONTRACT_ID, SOURCE_FINGERPRINT


def outcome(kind="calculation", **changes):
    row = {"id": "goal", "kind": kind, "target": "Result", "evidence_plane": "display"}
    if kind == "calculation": row["expression"] = "3*5"
    elif kind == "text": row["value"] = "exact\r\nΩ  "
    else: row["value"] = True
    row.update(changes)
    return row


def control(role="AXStaticText", name="Result", value="0", **changes):
    row = {"id": "s1:1", "role": role, "name": name, "value": value,
           "states": {}, "parent": "s1:0", "semantics": {},
           "bounds": {"x": 20, "y": 10, "width": 100, "height": 20}}
    row.update(changes)
    return row


def observation(*rows, snapshot="s1", target=None):
    now = time.time_ns()
    window = {"id": snapshot+":0", "role": "AXWindow", "name": "Test document",
              "parent": None, "semantics": {"identifier": "document-window"}}
    items = [window]
    for n, original in enumerate(rows, 1):
        row = deepcopy(original); row["id"] = snapshot+":"+str(n)
        row["parent"] = window["id"]
        # Deliberately retain snapshot-qualified denormalized metadata. It must
        # not leak into cross-snapshot identity.
        row.setdefault("semantics", {})["ancestors"] = [{"id": window["id"], "name": window["name"]}]
        row["semantics"]["evidence_sources"] = {"line_number": n+len(snapshot)}
        items.append(row)
    return {"kind": "native_window_state", "target": target or {"pid": 41, "window_id": 52},
            "snapshot_id": snapshot, "observed_at_ns": now, "provenance": {"observed_at_ns": now},
            "controls": items, "handles": {}, "coverage": {"complete": False}}


def editor(value="initial", **changes):
    return control("AXTextArea", "Contents", value,
        value_evidence={"precision": "exact", "exact_value_proven": True, "plane": "editor_buffer"}, **changes)


class VerificationTests(unittest.TestCase):
    def binding(self, goal, row):
        obs = observation(row)
        return bind(goal, obs["controls"][1], obs), obs

    def test_wrong_control_with_same_numeric_value_cannot_satisfy(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        after = observation(control(value="0"), control(name="History", value="15"), snapshot="s2")
        self.assertFalse(verify(binding, goal, after)["matched"])
        self.assertFalse(matches_binding(binding, after, "s2:2"))
        self.assertTrue(matches_binding(binding, after, "s2:1"))

    def test_correct_bound_display_and_snapshot_ancestor_change(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        result = verify(binding, goal, observation(control(value="15"), snapshot="different-snapshot"))
        self.assertTrue(result["matched"])
        self.assertEqual(result["evidence"]["actual"], "15")
        self.assertFalse(result["evidence"]["saved_output_proven"])

    def test_value_label_changes_only_with_stable_geometry_and_ancestor(self):
        goal = outcome(); binding, _ = self.binding(goal, control(name="0", value="0"))
        after = observation(control(name="15", value="15"), snapshot="s2")
        self.assertTrue(verify(binding, goal, after)["matched"])
        after["controls"][1]["bounds"]["x"] += 1
        self.assertFalse(verify(binding, goal, after)["matched"])

    def test_value_derived_semantic_title_is_removed_but_ancestor_name_is_not(self):
        goal = outcome(); binding, _ = self.binding(goal, control(name="0", value="0", semantics={"title": "0"}))
        after = observation(control(name="15", value="15", semantics={"title": "15"}), snapshot="s2")
        self.assertTrue(verify(binding, goal, after)["matched"])
        after["controls"][0]["name"] = "Different document"
        self.assertFalse(verify(binding, goal, after)["matched"])

    def test_value_derived_weak_identity_is_refused(self):
        for bounds in (None, {"x": 0, "y": 0, "width": 0, "height": 0}):
            with self.subTest(bounds=bounds), self.assertRaises(BindingError):
                self.binding(outcome(), control(name="0", bounds=bounds))

    def test_stable_identifier_can_bind_value_label_without_geometry(self):
        goal = outcome(); binding, _ = self.binding(goal, control(name="0", bounds=None, semantics={"identifier": "result"}))
        after = observation(control(name="15", value="15", bounds=None, semantics={"identifier": "result"}), snapshot="s2")
        self.assertTrue(verify(binding, goal, after)["matched"])

    def test_ambiguous_identity_refuses_binding_and_later_verification(self):
        goal = outcome(); row = control(); obs = observation(row, row)
        with self.assertRaises(BindingError): bind(goal, obs["controls"][1], obs)
        binding, _ = self.binding(goal, row)
        after = observation(control(value="15"), control(value="15"), snapshot="s2")
        self.assertEqual(verify(binding, goal, after)["reason"], "bound_target_ambiguous")

    def test_checkbox_checked_does_not_use_selected_false(self):
        goal = outcome("state", value=False)
        binding, obs = self.binding(goal, control("AXCheckBox", "Enabled", None,
                                    states={"checked": True, "selected": False}))
        self.assertEqual(binding["property"], "checked")
        self.assertFalse(verify(binding, goal, obs)["matched"])
        after = observation(control("AXCheckBox", "Enabled", None, states={"checked": False, "selected": True}), snapshot="s2")
        self.assertTrue(verify(binding, goal, after)["matched"])

    def test_role_specific_state_unknown_never_inferred(self):
        for role, states in (("AXCheckBox", {"selected": True}),
                             ("AXRadioButton", {"checked": True}), ("AXButton", {"selected": True}),
                             ("AXCheckBox", {"checked": 1})):
            with self.subTest(role=role, states=states), self.assertRaises(BindingError):
                self.binding(outcome("state"), control(role, "Choice", None, states=states))

    def test_radio_selected_and_checked_are_not_interchangeable(self):
        goal = outcome("state")
        binding, obs = self.binding(goal, control("AXRadioButton", "Choice", None,
                                    states={"selected": True, "checked": False}))
        self.assertEqual(binding["property"], "selected")
        self.assertTrue(verify(binding, goal, obs)["matched"])

    def test_exact_text_and_mutable_value_label_remain_buffer_only(self):
        goal = outcome("text"); row = editor(); row["name"] = row["value"]
        binding, _ = self.binding(goal, row)
        after_row = editor(goal["value"]); after_row["name"] = after_row["value"]
        result = verify(binding, goal, observation(after_row, snapshot="s2"))
        self.assertTrue(result["matched"])
        self.assertEqual(result["evidence"]["plane"], "editor_buffer")
        self.assertFalse(result["evidence"]["committed_document_proven"])

    def test_display_only_text_or_lost_exact_proof_cannot_complete(self):
        goal = outcome("text"); row = editor(goal["value"])
        binding, _ = self.binding(goal, row)
        row["value_evidence"]["exact_value_proven"] = False
        self.assertFalse(verify(binding, goal, observation(row, snapshot="s2"))["matched"])
        with self.assertRaises(BindingError): self.binding(goal, row)

    def test_empty_exact_value_is_real_and_whitespace_preserved(self):
        goal = outcome("text", value="")
        binding, obs = self.binding(goal, editor(""))
        self.assertTrue(verify(binding, goal, obs)["matched"])
        self.assertFalse(verify(binding, goal, observation(editor(" "), snapshot="s2"))["matched"])

    def test_final_refresh_losing_previously_true_evidence_is_not_cached(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        self.assertTrue(verify(binding, goal, observation(control(value="15"), snapshot="s2"))["matched"])
        self.assertFalse(verify(binding, goal, observation(control(value="0"), snapshot="s3"))["matched"])
        self.assertEqual(verify(binding, goal, observation(snapshot="s4"))["status"], "unavailable")

    def test_new_window_or_pid_cannot_reuse_reviewed_binding(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        for target in ({"pid": 41, "window_id": 53}, {"pid": 42, "window_id": 52}):
            after = observation(control(value="15"), snapshot="s2", target=target)
            self.assertFalse(verify(binding, goal, after)["matched"])
            self.assertFalse(matches_binding(binding, after, "s2:1"))

    def test_stale_future_predating_and_mismatched_timestamp_refuse(self):
        goal = outcome(); binding, obs = self.binding(goal, control())
        for stamp in (time.time_ns()-40_000_000_000, time.time_ns()+40_000_000_000, binding["bound_at_ns"]-1):
            after = observation(control(value="15"), snapshot="s2")
            after["observed_at_ns"] = after["provenance"]["observed_at_ns"] = stamp
            self.assertFalse(verify(binding, goal, after)["matched"])
        obs["provenance"]["observed_at_ns"] -= 1
        with self.assertRaises(ValueError): bind(goal, obs["controls"][1], obs)

    def test_outcome_target_or_literal_substitution_and_binding_mutation_refuse(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        after = observation(control(value="15"), snapshot="s2")
        changed = deepcopy(goal); changed["target"] = "Other control"
        self.assertFalse(verify(binding, changed, after)["matched"])
        binding["target_phrase"] = "Other control"
        self.assertFalse(verify(binding, goal, after)["matched"])

    def test_numeric_complete_display_no_answer_prose_or_locale_guess(self):
        goal = outcome(); binding, _ = self.binding(goal, control())
        for value in ("1,5", "Expected 15", "NaN", "Infinity", "15 apples"):
            with self.subTest(value=value):
                self.assertFalse(verify(binding, goal, observation(control(value=value), snapshot="s2"))["matched"])
        self.assertTrue(verify(binding, goal, observation(control(value="1.5e1"), snapshot="s2"))["matched"])

    def test_button_label_is_not_result_and_saved_plane_is_not_ui(self):
        with self.assertRaises(BindingError): self.binding(outcome(), control("AXButton", "15", "15"))
        with self.assertRaises(BindingError): self.binding(outcome("text", evidence_plane="saved_output"), editor())

    def test_unobserved_or_modified_control_cannot_bind(self):
        obs = observation(control()); row = deepcopy(obs["controls"][1]); row["name"] = "Forged"
        with self.assertRaises(BindingError): bind(outcome(), row, obs)
        obs["controls"][1]["parent"] = "missing"
        with self.assertRaises(BindingError): bind(outcome(), obs["controls"][1], obs)


def static_tree(values=("Unit", "2*3"), snapshot="s10", parent_name="Reading surface"):
    contract = {"id": CONTRACT_ID, "source_fingerprint_sha256": SOURCE_FINGERPRINT,
                "platform": "macos", "plane": "editor_buffer", "handle_binding": "same_snapshot_element_token"}
    raw = {"pid": 41, "window_id": 52, "snapshot_id": snapshot, "elements_complete": False,
        "native_editor_contract": contract,
        "elements": [{"element_index": 0, "element_token": snapshot+":0", "role": "AXWindow",
                      "label": parent_name, "actions": ["AXRaise"]}],
        "tree_markdown": '- [0] AXWindow "'+parent_name+'" [id=reading-surface actions=[raise]]\n'
                         + "".join('  - AXStaticText = "'+v+'"\n' for v in values)}
    return normalize_observation(raw, kind="native", expected_target={"pid": 41, "window_id": 52},
                                 observed_at_ns=time.time_ns())


class StructuralSlotTests(unittest.TestCase):
    def bind_slot(self):
        goal = outcome(); obs = static_tree()
        return goal, bind(goal, obs["controls"][2], obs)

    def test_dynamic_expression_to_result_uses_reviewed_slot_not_expected_number_search(self):
        goal, binding = self.bind_slot()
        self.assertEqual(binding["identity_policy"]["mode"], "rendered_static_text_slot")
        after = static_tree(("Unit", "15"), snapshot="s11")
        result = verify(binding, goal, after)
        self.assertTrue(result["matched"])
        self.assertEqual(result["evidence"]["identity_scope"], "rendered_static_text_slot")
        self.assertFalse(result["evidence"]["persistent_ax_object_proven"])
        self.assertFalse(matches_binding(binding, after, after["controls"][2]["id"]))
        self.assertNotIn(after["controls"][2]["id"], after["handles"])

    def test_added_missing_reordered_or_changed_landmark_refuses(self):
        goal, binding = self.bind_slot()
        for values in (("Unit", "15", "Other"), ("15",), ("15", "Unit"), ("Changed", "15"), ("Unit", "Unit")):
            with self.subTest(values=values):
                self.assertFalse(verify(binding, goal, static_tree(values, snapshot="s11"))["matched"])

    def test_same_number_elsewhere_does_not_replace_bound_slot(self):
        goal, binding = self.bind_slot()
        self.assertFalse(verify(binding, goal, static_tree(("15", "0"), snapshot="s11"))["matched"])

    def test_parent_identity_drift_unpinned_source_and_no_landmark_refuse(self):
        goal, binding = self.bind_slot()
        self.assertFalse(verify(binding, goal, static_tree(("Unit", "15"), snapshot="s11", parent_name="Other"))["matched"])
        obs = static_tree(("2*3",))
        with self.assertRaises(BindingError): bind(goal, obs["controls"][1], obs)
        obs = static_tree(); obs["provenance"]["raw_metadata"]["native_editor_contract"]["source_fingerprint_sha256"] = "0"*64
        with self.assertRaises(BindingError): bind(goal, obs["controls"][2], obs)

    def test_local_hierarchy_corruption_or_known_truncation_refuses(self):
        goal, binding = self.bind_slot()
        for mutation in (lambda o: o["hierarchy"].pop(),
                         lambda o: o["hierarchy"].append(deepcopy(o["hierarchy"][-1])),
                         lambda o: o["coverage"].update(truncated=True),
                         lambda o: o["coverage"].update(parse_warnings=["Repeated identity"]),
                         lambda o: o["hierarchy"][-1].update(raw="different line")):
            obs = static_tree(("Unit", "15"), snapshot="s11"); mutation(obs)
            self.assertFalse(verify(binding, goal, obs)["matched"])

    def test_structural_read_slot_cannot_be_promoted_to_exact_text_editing(self):
        obs = static_tree()
        with self.assertRaises(BindingError): bind(outcome("text"), obs["controls"][2], obs)
        goal, binding = self.bind_slot()
        self.assertFalse(verify(binding, goal, static_tree(("Unit", "0"), snapshot="s11"))["matched"])

    def test_direction_marks_preserved_in_evidence_but_no_grouping_guess(self):
        goal, binding = self.bind_slot()
        result = verify(binding, goal, static_tree(("Unit", "\u200e15\u200f"), snapshot="s11"))
        self.assertTrue(result["matched"]); self.assertEqual(result["evidence"]["actual"], "\u200e15\u200f")
        self.assertFalse(verify(binding, goal, static_tree(("Unit", "1,5"), snapshot="s12"))["matched"])


if __name__ == "__main__": unittest.main()
