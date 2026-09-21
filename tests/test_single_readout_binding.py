"""Generic single-readout structural identity; no desktop/model IO."""
from copy import deepcopy
import time
import unittest

from locua.engine.prototype.native_driver_contract import CONTRACT_ID, SOURCE_FINGERPRINT
from locua.engine.prototype.perception import normalize_observation
from locua.goal_verification import BindingError, bind, bind_for_review, matches_binding, verify


def tree(value="pending expression", *, snapshot="before", move=(0, 0),
         labels=("First operation", "Second operation"), identifiers=("anchor-first", "anchor-second"),
         extra_readouts=(), parent_name="Observed work area", order=(0, 1), scale=1):
    contract = {"id": CONTRACT_ID, "source_fingerprint_sha256": SOURCE_FINGERPRINT,
                "platform": "macos", "plane": "editor_buffer", "handle_binding": "same_snapshot_element_token"}
    x, y = 100+move[0], 200+move[1]
    elements = [{"element_index": 0, "element_token": snapshot+":0", "role": "AXWindow",
                 "label": parent_name, "actions": ["AXRaise"],
                 "frame": {"x": x, "y": y, "w": 300*scale, "h": 200*scale}}]
    lines = [f'- [0] AXWindow "{parent_name}" [id=work-area actions=[raise]]',
             f'  - AXStaticText = "{value}"']
    lines += [f'  - AXStaticText = "{v}"' for v in extra_readouts]
    for i in order:
        index = len(elements)
        elements.append({"element_index": index, "element_token": snapshot+":"+str(index),
                         "parent_index": 0, "role": "AXButton", "label": labels[i],
                         "enabled": True, "actions": ["AXPress"],
                         "frame": {"x": x+(20+100*i)*scale, "y": y+100*scale, "w": 60*scale, "h": 40*scale}})
        lines.append(f'  - [{index}] AXButton ({labels[i]}) [id={identifiers[i]} actions=[press]]')
    raw = {"pid": 41, "window_id": 52, "snapshot_id": snapshot, "elements_complete": False,
           "native_editor_contract": contract, "elements": elements, "tree_markdown": "\n".join(lines)}
    return normalize_observation(raw, kind="native", expected_target={"pid": 41, "window_id": 52},
                                 observed_at_ns=time.time_ns())


def goal():
    return {"id": "arithmetic", "kind": "calculation", "target": "Observed output",
            "expression": "3*5", "evidence_plane": "display"}


def readout(observation):
    return next(c for c in observation["controls"] if c["role"] == "AXStaticText")


class SingleReadoutTests(unittest.TestCase):
    def test_bind_then_exact_result_readback_uses_independent_anchor_identity(self):
        before = tree(); b = bind(goal(), readout(before), before)
        self.assertEqual(b["identity_policy"], {"mode": "rendered_static_text_slot", "read_only": True})
        self.assertEqual(b["core_identity"]["anchor_basis"], "single_readout_identified_native_siblings_v1")
        self.assertEqual(len(b["core_identity"]["native_anchors"]), 2)
        self.assertNotIn("pending expression", str(b["core_identity"]))
        after = tree("15", snapshot="after")
        result = verify(b, goal(), after)
        self.assertTrue(result["matched"], result)
        self.assertEqual(result["evidence"]["actual"], "15")
        self.assertFalse(result["evidence"]["persistent_ax_object_proven"])
        self.assertFalse(result["evidence"]["saved_output_proven"])
        self.assertFalse(matches_binding(b, after, readout(after)["id"]))
        self.assertNotIn(readout(after)["id"], after["handles"])

    def test_whole_window_translation_and_anchor_labels_do_not_become_identity(self):
        before = tree(); b = bind(goal(), readout(before), before)
        after = tree("15", snapshot="after", move=(73, -25), labels=("Changed label", "Other state"))
        self.assertTrue(verify(b, goal(), after)["matched"])

    def test_output_layout_added_removed_reordered_or_reflowed_requires_new_binding(self):
        before = tree(); b = bind(goal(), readout(before), before)
        cases = [tree("not numeric", snapshot="after", extra_readouts=("15",)),
                 tree("15", snapshot="after", extra_readouts=("New heading",)),
                 tree("15", snapshot="after", order=(1, 0)),
                 tree("15", snapshot="after", scale=2)]
        for after in cases:
            with self.subTest(case=after["text"]):self.assertFalse(verify(b, goal(), after)["matched"])
        after = tree("15", snapshot="after")
        c = readout(after)
        after["controls"].remove(c)
        after["hierarchy"] = [r for r in after["hierarchy"] if r.get("control_id") != c["id"]]
        self.assertFalse(verify(b, goal(), after)["matched"])
        self.assertTrue(b["review_descriptor"]["layout_change_requires_new_binding"])

    def test_missing_duplicate_unlocated_or_coincident_anchors_refuse(self):
        for observation in (tree(order=(0,)), tree(identifiers=("duplicate", "duplicate"))):
            with self.assertRaises(BindingError):bind(goal(), readout(observation), observation)
        for mutation in ("no_frame", "same_location", "unproved_identifier", "line_changed", "foreign_source"):
            observation = tree(); buttons = [c for c in observation["controls"] if c["role"] == "AXButton"]
            c = buttons[1]
            if mutation == "no_frame":c["bounds"] = None
            elif mutation == "same_location":
                c["bounds"] = deepcopy(buttons[0]["bounds"])
                c["source"]["node"]["frame"] = deepcopy(buttons[0]["source"]["node"]["frame"])
            elif mutation == "unproved_identifier":c["semantics"]["identifier"] = "Invented"
            elif mutation == "line_changed":c["source"]["markdown_line"] += " changed"
            else:c["source"]["native_editor_contract"]["source_fingerprint_sha256"] = "0"*64
            with self.subTest(mutation=mutation), self.assertRaises(BindingError):
                bind(goal(), readout(observation), observation)

    def test_changed_anchor_identity_frame_parent_or_target_refuses(self):
        before = tree(); b = bind(goal(), readout(before), before)
        for after in (tree("15", snapshot="after", identifiers=("other", "anchor-second")),
                      tree("15", snapshot="after", parent_name="Different document")):
            self.assertFalse(verify(b, goal(), after)["matched"])
        after = tree("15", snapshot="after")
        after["target"]["window_id"] += 1
        self.assertFalse(verify(b, goal(), after)["matched"])

    def test_historical_binding_keeps_original_stamp_and_never_verifies_old_capture(self):
        before = tree(); stamp = before["observed_at_ns"]-120_000_000_000
        before["observed_at_ns"] = before["provenance"]["observed_at_ns"] = stamp
        original = deepcopy(before)
        with self.assertRaises(ValueError):bind(goal(), readout(before), before)
        b = bind_for_review(goal(), readout(before), before)
        self.assertEqual(before, original)
        self.assertEqual(b["bound_at_ns"], stamp)
        self.assertFalse(b["review_capture"]["current_state_proven"])
        self.assertFalse(verify(b, goal(), before)["matched"])
        self.assertTrue(verify(b, goal(), tree("15", snapshot="after"))["matched"])

    def test_no_promoted_editability_or_expected_value_control_selection(self):
        before = tree(); b = bind(goal(), readout(before), before)
        wrong = tree("14", snapshot="after")
        self.assertEqual(verify(b, goal(), wrong)["status"], "mismatch")
        requested = {"id": "edit", "kind": "text", "target": "Observed output", "value": "15", "evidence_plane": "editor_buffer"}
        with self.assertRaises(BindingError):bind(requested, readout(before), before)
        self.assertFalse(matches_binding(b, before, readout(before)["id"]))

    def test_ambiguous_parent_truncation_and_forged_handle_refuse(self):
        for mutation in ("parent_twin", "truncation", "handle"):
            o = tree()
            if mutation == "parent_twin":
                twin = deepcopy(o["controls"][0]); twin["id"] = "another-parent"; o["controls"].append(twin)
            elif mutation == "truncation":o["coverage"]["truncated"] = True
            else:
                key = next(c["id"] for c in o["controls"] if c["role"] == "AXButton")
                o["handles"][key]["element_token"] = "wrong"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):bind(goal(), readout(o), o)


if __name__ == "__main__":
    unittest.main()
