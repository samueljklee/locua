"""Offline route/capture/scope regressions; no browser, network, or model calls."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from locua.engine.prototype.core import action_available, authorize, build_candidates
from locua.engine.prototype.cua import CuaAdapter, browser_execution_capabilities
from locua.engine.prototype.driver_contract import CONTRACT_ID, SOURCE_FINGERPRINT, UPSTREAM
from locua.engine.prototype.perception import normalize_observation

SERVER = {"name": "cua-driver", "version": "0.28.2"}
TARGET = {"target_id": "owned-target", "tab_id": "owned-tab", "session": "owner"}
CONTRACT = {"id": CONTRACT_ID, "upstream_revision": UPSTREAM,
            "source_fingerprint_sha256": SOURCE_FINGERPRINT, "driver_version": "0.28.2",
            "experimental_patch": True, "atomic_capture": False,
            "ancestry": "frame_scoped_ax_parent", "exact_value_source": "DOMSnapshot"}
SCHEMAS = {
    "browser_type": {"required": ["target_id", "tab_id", "ref", "text"], "properties": {
        **{k: {"type": "string"} for k in ("target_id", "tab_id", "ref", "text")},
        "replace": {"type": "boolean"}, "mode": {"enum": ["insert_text", "keystrokes"]}}},
    "browser_click": {"required": ["target_id", "tab_id"], "properties": {
        **{k: {"type": "string"} for k in ("target_id", "tab_id", "ref")},
        "input_route": {"enum": ["trusted", "dom_event"]}}}}


def payload(sid="p1"):
    rows = []
    for i, (role, name, value, parent, visibility, actions) in enumerate([
        ("group", "Preserved", None, None, "in_viewport", []),
        ("textbox", "Label", "old", 0, "in_viewport", ["type"]),
        ("group", "Requested", None, None, "near_viewport", []),
        ("textbox", "Label", "old", 2, "near_viewport", ["type"]),
        ("checkbox", "Notify", "on", 2, "near_viewport", ["click"]),
        ("textbox", "Unrelated", "untouched", None, "in_viewport", ["type"])]):
        rows.append({"ref": f"{sid}:{i}", "role": role, "name": name, "value": value,
            "states": {"checked": False} if role == "checkbox" else {}, "actions": actions,
            "visibility": visibility, "parent_ref": f"{sid}:{parent}" if parent is not None else None,
            "parent_relation": "resolved" if parent is not None else "root",
            "frame_identity": {"frame_id": "frame", "loader_id": "loader", "oopif_target_id": None},
            "context_only": not bool(actions), "executable": bool(actions),
            "exact_value": {"precision": "exact", "value": value, "frame_document_proven": True,
                "plane": "dom_control_value", "source": "DOMSnapshot.inputValue"} if value is not None else {}})
    return {"status": "ok", **TARGET, "snapshot": {"format": "semantic_v2", "id": sid,
        "complete": True, "ancestry_complete": True, "driver_contract": deepcopy(CONTRACT)},
        "refs": rows, "content_refs": [], "outline": ""}


def observed(sid="p1", route="dom_event"):
    obs = normalize_observation(payload(sid), kind="browser_semantic_v2", expected_target=TARGET,
                                observed_at_ns=time.time_ns())
    obs["execution_capabilities"] = browser_execution_capabilities(obs, server=SERVER,
        schemas=SCHEMAS, click_route=route)
    return obs


def task():
    return {"id": "route-scope", "goal": "Change the requested label and enable notifications; preserve competitors.",
        "target": deepcopy(TARGET), "intents": [
            {"id": "text", "kind": "set_text", "selector": {"role": "textbox", "name": "Label",
                "ancestor": {"role": "group", "name": "Requested"}}, "value": "new"},
            {"id": "notify", "kind": "set_state", "selector": {"role": "checkbox", "name": "Notify"},
             "property": "checked", "value": True}],
        "invariants": [{"selector": {"role": "textbox", "name": "Label",
            "ancestor": {"role": "group", "name": "Preserved"}}, "property": "value", "value": "old"},
            {"selector": {"role": "textbox", "name": "Unrelated"}, "property": "value", "value": "untouched"}]}


def row(obs, index):
    return obs["controls"][index]


def candidate(obs, index, kind):
    return next(c for c in build_candidates(obs, task())
                if c.get("control_id") == row(obs, index)["id"] and c["kind"] == kind)


class FakeOwner:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.server = {"serverInfo": deepcopy(SERVER)}
        self.inventory = {"tools": [{"name": k, "inputSchema": deepcopy(v)} for k, v in SCHEMAS.items()]}
        self.calls = []
    def check(self):
        pass
    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        result = payload() if name == "get_browser_state" else {"effect": "unverifiable"}
        return result, result


class BrowserExecutionTests(unittest.TestCase):
    def test_adapter_binds_actual_configuration_and_preserves_every_control(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = FakeOwner(directory)
            adapter = CuaAdapter(owner, kind="browser_semantic_v2", target=TARGET, browser_click_route="dom_event")
            obs = adapter.observe()
            self.assertEqual(len(obs["controls"]), 6)
            self.assertEqual(obs["execution_capabilities"]["snapshot_id"], obs["snapshot_id"])
            self.assertTrue(action_available(obs, row(obs, 3), "set_text"))
            self.assertTrue(action_available(obs, row(obs, 4), "press"))
            self.assertEqual([name for name, _ in owner.calls], ["get_browser_state"])

    def test_catalog_retains_competitors_and_only_task_bound_actions_authorize(self):
        old, fresh = observed(), observed("p2")
        edits = [c for c in build_candidates(old, task()) if c["kind"] == "set_text"]
        self.assertEqual([c["control_id"] for c in edits], [row(old, i)["id"] for i in (1, 3, 5)])
        for i in (1, 5):
            self.assertEqual(authorize(task(), old, fresh, candidate(old, i, "set_text"))["reason"],
                             "outside_pending_task_scope_or_dependency")
        for i, kind in ((3, "set_text"), (4, "press")):
            self.assertTrue(authorize(task(), old, fresh, candidate(old, i, kind))["allowed"])

    def test_trusted_route_does_not_add_near_viewport_click_but_type_is_supported(self):
        obs = observed(route="trusted")
        self.assertTrue(action_available(obs, row(obs, 3), "set_text"))
        self.assertFalse(action_available(obs, row(obs, 4), "press"))
        self.assertFalse(any(c.get("control_id") == row(obs, 4)["id"] for c in build_candidates(obs, task())))

    def test_all_other_visibility_categories_remain_unavailable(self):
        for visibility in ("css_hidden", "hidden", "unknown", "page_occluded", "offscreen", "no_layout", None):
            for i, kind in ((3, "set_text"), (4, "press")):
                with self.subTest(visibility=visibility, kind=kind):
                    obs = observed(); control = row(obs, i)
                    control["source"]["node"]["visibility"] = visibility
                    control["states"]["visibility"] = visibility
                    self.assertFalse(action_available(obs, control, kind))

    def test_disabled_or_contradictory_visibility_remains_unavailable(self):
        for patch in ({"disabled": True}, {"enabled": False}, {"visibility": "in_viewport"}):
            obs = observed(); row(obs, 3)["states"].update(patch)
            self.assertFalse(action_available(obs, row(obs, 3), "set_text"))

    def test_version_alone_missing_wrong_fingerprint_or_schema_cannot_grant(self):
        for field, value in (("id", "other"), ("source_fingerprint_sha256", "wrong"),
                             ("driver_version", "0.28.3")):
            obs = observed(); obs["provenance"]["raw_metadata"]["snapshot"]["driver_contract"][field] = value
            self.assertIsNone(browser_execution_capabilities(obs, server=SERVER, schemas=SCHEMAS, click_route="dom_event"))
            self.assertFalse(action_available(obs, row(obs, 3), "set_text"))
        obs = observed(); del obs["execution_capabilities"]
        self.assertFalse(action_available(obs, row(obs, 3), "set_text"))
        for change in ("ref", "mode", "replace"):
            schemas = deepcopy(SCHEMAS); del schemas["browser_type"]["properties"][change]
            obs["execution_capabilities"] = browser_execution_capabilities(obs, server=SERVER, schemas=schemas, click_route="dom_event")
            self.assertFalse(action_available(obs, row(obs, 3), "set_text"))
        self.assertIsNone(browser_execution_capabilities(obs, server={**SERVER, "version": "0.28.3"}, schemas=SCHEMAS, click_route="dom_event"))

    def test_foreign_snapshot_target_timestamp_ref_frame_and_nonexecutables_fail(self):
        changes = [lambda o: o["execution_capabilities"].update(snapshot_id="p0"),
            lambda o: o["execution_capabilities"]["target"].update(tab_id="other"),
            lambda o: o["execution_capabilities"].update(observed_at_ns=0),
            lambda o: row(o, 3)["source"]["node"].update(ref="p1:1"),
            lambda o: row(o, 3)["source"]["node"]["frame_identity"].pop("loader_id"),
            lambda o: row(o, 3)["source"]["node"].update(context_only=True),
            lambda o: row(o, 3)["source"]["node"].update(executable=False),
            lambda o: row(o, 3)["source"]["node"].update(actions=[]),
            lambda o: o["handles"][row(o, 3)["id"]].update(session="other")]
        for change in changes:
            obs = observed(); change(obs)
            self.assertFalse(action_available(obs, row(obs, 3), "set_text"))

    def test_native_and_legacy_defaults_are_not_expanded(self):
        obs = observed(); obs["kind"] = "native_window_state"
        self.assertFalse(action_available(obs, row(obs, 3), "set_text"))
        legacy = {"controls": [{"id": "plain", "role": "textbox", "actions": ["type"], "states": {}}],
                  "handles": {"plain": {"kind": "browser"}}}
        self.assertTrue(action_available(legacy, legacy["controls"][0], "set_text"))
        self.assertFalse(action_available(legacy, legacy["controls"][0], "press"))

    def test_fresh_rebind_refuses_route_loss_or_changed_frame(self):
        old = observed()
        for mutate in (lambda o: o.pop("execution_capabilities"),
                       lambda o: o["execution_capabilities"]["actions"]["press"].update(input_route="trusted", near_viewport=False),
                       lambda o: row(o, 4)["source"]["node"]["frame_identity"].update(loader_id="navigated")):
            fresh = observed("p2"); mutate(fresh)
            self.assertFalse(authorize(task(), old, fresh, candidate(old, 4, "press"))["allowed"])

    def test_dispatch_binds_live_route_inventory_and_capture_once(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = FakeOwner(directory)
            adapter = CuaAdapter(owner, kind="browser_semantic_v2", target=TARGET,
                                 execute=True, browser_click_route="dom_event")
            obs = adapter.observe(); action = candidate(obs, 4, "press")
            result = adapter.dispatch(action)
            self.assertEqual(result["effect"], "unverifiable")
            self.assertEqual(owner.calls[-1], ("browser_click", {**TARGET, "ref": "p1:4", "input_route": "dom_event"}))
            adapter.browser_click_route = "trusted"
            with self.assertRaisesRegex(ValueError, "route or inventory changed"):
                adapter.dispatch(action)
            self.assertEqual(sum(n == "browser_click" for n, _ in owner.calls), 1)

    def test_dispatch_refuses_stale_capture_or_swapped_evidence_without_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = FakeOwner(directory)
            adapter = CuaAdapter(owner, kind="browser_semantic_v2", target=TARGET, execute=True, browser_click_route="dom_event")
            obs = adapter.observe(); action = candidate(obs, 3, "set_text")
            for mutation in (lambda o: o["execution"].update(mode="keystrokes"), lambda o: o.pop("execution")):
                changed = deepcopy(action); mutation(changed)
                with self.assertRaises(ValueError):
                    adapter.dispatch(changed)
            obs["observed_at_ns"] -= 31_000_000_000
            obs["execution_capabilities"]["observed_at_ns"] = obs["observed_at_ns"]
            with self.assertRaisesRegex(ValueError, "stale"):
                adapter.dispatch(action)
            self.assertEqual([n for n, _ in owner.calls], ["get_browser_state"])

    def test_type_dispatch_uses_ref_insert_text_and_no_coordinate_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = FakeOwner(directory)
            adapter = CuaAdapter(owner, kind="browser_semantic_v2", target=TARGET, execute=True)
            obs = adapter.observe(); adapter.dispatch(candidate(obs, 3, "set_text"))
            self.assertEqual(owner.calls[-1], ("browser_type", {**TARGET, "ref": "p1:3", "text": "new", "replace": True, "mode": "insert_text"}))

    def test_recorded_failed_capture_adds_supported_actions_without_removing_competitors(self):
        repo = Path(__file__).resolve().parents[1]
        root = repo / "artifacts/guided-context-check-001/run/execution"
        if not root.exists():
            root = repo.parent / "locua/artifacts/guided-context-check-001/run/execution"
        if not root.exists():
            self.skipTest("Optional private development recording is not shipped in the package")
        old = json.loads((root / "preflight-observation.json").read_text())
        fresh = json.loads((root / "initial-observation.json").read_text())
        explicit_task = json.loads((root / "grounded-task.json").read_text())["task"]
        server = json.loads((root / "cua/server.json").read_text())["serverInfo"]
        schemas = {t["name"]: t["inputSchema"] for t in json.loads((root / "cua/inventory.json").read_text())["tools"]}
        old_count = len(build_candidates(old, explicit_task))
        pristine_controls = deepcopy(old["controls"])
        for obs in (old, fresh):
            obs["execution_capabilities"] = browser_execution_capabilities(obs,
                server=server, schemas=schemas, click_route="dom_event")
        catalog = build_candidates(old, explicit_task)
        self.assertEqual(old["controls"], pristine_controls)
        self.assertGreater(len(catalog), old_count)
        # The clock is fixed to the recorded capture solely for an offline guard
        # replay; no capture is relabelled as current/live and no oracle is read.
        with patch("locua.engine.prototype.core.time.time_ns", return_value=fresh["observed_at_ns"] + 1):
            allowed, rejected = [], []
            for item in catalog:
                if item["kind"] == "done":
                    continue
                decision = authorize(explicit_task, old, fresh, item)
                (allowed if decision["allowed"] else rejected).append(decision)
        self.assertEqual({d["intent_id"] for d in allowed}, {i["id"] for i in explicit_task["intents"]})
        self.assertEqual(len(allowed), 2)
        self.assertGreaterEqual(sum(d["reason"] == "outside_pending_task_scope_or_dependency" for d in rejected), 3)


if __name__ == "__main__":
    unittest.main()
