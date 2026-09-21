"""Pure representation tests; no desktop, subprocess, model, or expected answers."""
from copy import deepcopy
import hashlib
import json
import time
import unittest

from locua import progressive_ui as ui
from locua.engine.prototype.regions import catalog_regions, inspect_region


def fixture(buttons=8):
    now = time.time_ns()
    controls = []

    def add(cid, role, name=None, value=None, parent=None, states=None, **extra):
        controls.append(dict(id=cid, role=role, name=name, value=value, parent=parent,
                             states=states or {}, actions=[], semantics={}, **extra))

    add("w", "AXWindow", "Work surface")
    add("a", "AXGroup", "First section", parent="w")
    add("b", "AXGroup", "Second section", parent="w")
    for n in range(buttons):
        add(f"a{n}", "AXButton", f"Item {n}", parent="a", states={"enabled": True})
    add("other", "AXButton", "Item 0", parent="b", states={"enabled": True})
    add("read", "AXStaticText", value="  00028\r\nΩ  ", parent="a")
    add("other_read", "AXStaticText", value="other", parent="b")
    add("hidden_read", "AXStaticText", value="hidden", parent="b", states={"hidden": True})
    add("toolbar", "AXToolbar", parent="w")
    add("navigation", "AXButton", "Inspect group", parent="toolbar", states={"enabled": True})
    o = {"kind": "native_window_state", "target": {"pid": 41, "window_id": 52},
         "snapshot_id": "snapshot1", "observed_at_ns": now, "provenance": {"observed_at_ns": now},
         "controls": controls, "handles": {}, "text": "Unbound status", "hierarchy": [],
         "coverage": {"complete": False}}
    actions = [{"id": "action:"+c["id"], "kind": "press", "requires_value": False,
                "control_id": c["id"], "snapshot_id": o["snapshot_id"], "target": deepcopy(o["target"]),
                "description": "Press "+str(c["name"])} for c in controls if c["role"] == "AXButton"]
    return o, actions


def pages(function, observation, **kwargs):
    cursor = None
    for _ in range(500):
        page = function(observation, cursor=cursor, **kwargs)
        yield page
        cursor = page["coverage"]["continuation"]
        if cursor is None:
            return
    raise AssertionError("Continuation did not terminate")


class ProgressiveUITests(unittest.TestCase):
    def setUp(self):
        self.o, self.actions = fixture()
        self.region = next(r["id"] for r in catalog_regions(self.o)["regions"] if r["root_control_id"] == "a")

    def test_overview_readouts_first_and_visibility_remains_unknown(self):
        result = ui.overview(self.o, actions=self.actions)
        self.assertEqual(result["items"][0]["id"], "read")
        self.assertEqual(result["items"][0]["value"], "  00028\r\nΩ  ")
        self.assertIsNone(result["items"][0]["visible"])
        readouts = [r for r in result["items"] if r["kind"] == "unnamed_readout"]
        self.assertEqual([r["id"] for r in readouts], ["read", "other_read"])
        navigation = [r for r in result["items"] if r["kind"] == "navigation_candidate"]
        self.assertEqual(navigation[0]["id"], "navigation")
        self.assertEqual(navigation[0]["actions"], [["action:navigation", "press", False]])
        regions = [r for r in result["items"] if r["kind"] == "region"]
        self.assertEqual(len(regions), len(catalog_regions(self.o)["regions"]))
        self.assertTrue(all("parent_region_id" in r and "child_region_ids" in r for r in regions))
        self.assertFalse(result["provenance"]["authorizes_actions"])

    def test_region_listing_keeps_competitors_parent_links_and_action_ids(self):
        result = ui.listing(self.o, region_id=self.region, actions=self.actions)
        rows = ui.exposed_controls(result)
        lookup = {r["id"]: r for r in rows}
        self.assertEqual(lookup["a0"]["membership"], "primary")
        self.assertEqual(lookup["other"]["membership"], "context")
        self.assertEqual(lookup["a0"]["parent"], "a")
        self.assertEqual(lookup["other"]["actions"][0]["id"], "action:other")
        self.assertFalse(result["coverage"]["uniqueness_proven"])
        self.assertIn("Unbound status", [r["text"] for r in result["items"] if isinstance(r, dict)])

    def test_named_readouts_keep_text_and_region_and_follow_region_overview(self):
        self.o["controls"].append({"id": "named", "role": "AXStaticText", "name": "Observed status",
                                    "value": None, "parent": "a", "states": {}, "actions": []})
        result = ui.overview(self.o, actions=self.actions)
        row = next(r for r in result["items"] if r["kind"] == "named_readout")
        self.assertEqual(row["name"], "Observed status")
        self.assertIsNone(row["value"])
        self.assertEqual(row["region_id"], self.region)
        self.assertLess(max(i for i, r in enumerate(result["items"]) if r["kind"] == "region"),
                        result["items"].index(row))
        self.assertIn("named", [r["id"] for r in ui.exposed_controls(result)])

    def test_region_role_and_query_filters_share_scope_preserve_twins(self):
        result = ui.listing(self.o, region_id=self.region, role="AXButton", query="Item 0", actions=self.actions)
        self.assertEqual([(r["id"], r["membership"]) for r in ui.exposed_controls(result)],
                         [("a0", "primary"), ("other", "context")])
        readouts = ui.listing(self.o, region_id=self.region, role="AXStaticText")
        self.assertIn("read", [r["id"] for r in ui.exposed_controls(readouts)])
        no_value_search = ui.listing(self.o, region_id=self.region, query="00028")
        self.assertEqual(no_value_search["items"], [])
        self.assertFalse(no_value_search["coverage"]["negative_evidence_proven"])
        self.assertFalse(no_value_search["query_searches_values"])

    def test_byte_paging_recovers_every_item_in_order_without_source_mutation(self):
        self.o, self.actions = fixture(40)
        self.region = next(r["id"] for r in catalog_regions(self.o)["regions"] if r["root_control_id"] == "a")
        original = deepcopy(self.o)
        all_pages = list(pages(ui.listing, self.o, region_id=self.region, actions=self.actions,
                               max_bytes=3500, limit=256))
        self.assertGreater(len(all_pages), 1)
        expected = inspect_region(self.o, self.region)["observation"]["controls"]
        actual = [r["id"] for p in all_pages for r in ui.exposed_controls(p)]
        self.assertEqual(actual, [r["id"] for r in expected])
        offset = 0
        for p in all_pages:
            self.assertLessEqual(ui._bytes(p), 3500)
            self.assertEqual(p["coverage"]["previous_page_count"], offset)
            offset += len(p["items"])
            self.assertFalse(p["representation"]["items_clipped"])
        self.assertEqual(offset, all_pages[0]["coverage"]["matched_total"])
        self.assertEqual(self.o, original)

    def test_cursor_rejects_other_snapshot_query_scope_and_actions(self):
        p = ui.listing(self.o, actions=self.actions, limit=1)
        cursor = p["coverage"]["continuation"]
        for changes in ({"role": "AXButton"}, {"region_id": self.region}, {"query": "Item"}):
            with self.assertRaises(ui.ProgressiveUIError):
                ui.listing(self.o, actions=self.actions, cursor=cursor, **changes)
        changed = deepcopy(self.o); changed["controls"][1]["name"] = "Changed"
        with self.assertRaises(ui.ProgressiveUIError):ui.listing(changed, actions=self.actions, cursor=cursor)
        with self.assertRaises(ui.ProgressiveUIError):ui.listing(self.o, cursor=cursor)
        second = ui.listing(self.o, actions=self.actions, cursor=cursor, limit=4)
        self.assertEqual(second["coverage"]["previous_page_count"], 1)

    def test_oversized_exact_value_is_deferred_then_losslessly_reassembled(self):
        c = next(c for c in self.o["controls"] if c["id"] == "read")
        literal = '  Ω\\quote"\r\n' * 900
        c["value"] = literal
        listing = ui.listing(self.o, role="AXStaticText")
        row = next(r for r in ui.exposed_controls(listing) if r["id"] == "read")
        self.assertTrue(row["value"]["deferred"])
        self.assertFalse(row["value"]["exact_value_in_this_view"])
        details = list(pages(ui.detail, self.o, control_id="read", max_bytes=3500))
        parts = [i for p in details for i in p["items"] if i["field"] == "value"]
        self.assertTrue(all(i["kind"] == "json_fragment" for i in parts))
        self.assertEqual([i["part"] for i in parts], list(range(parts[0]["parts"])))
        joined = ''.join(i["text"] for i in parts)
        self.assertEqual(json.loads(joined), literal)
        self.assertEqual(hashlib.sha256(joined.encode()).hexdigest(), row["value"]["sha256"])
        self.assertTrue(all(ui._bytes(p) <= 3500 for p in details))

    def test_oversized_unbound_text_remains_losslessly_paged(self):
        self.o["text"] = "A shared, unindexed status " * 500
        result = list(pages(ui.listing, self.o, region_id=self.region, max_bytes=3500))
        fragments = [i for p in result for i in p["items"] if isinstance(i, dict) and i.get("kind") == "json_fragment"]
        self.assertEqual(json.loads(''.join(i["text"] for i in fragments)), self.o["text"])
        self.assertTrue(all(i["source_kind"] == "unbound_text" for i in fragments))

    def test_detail_preserves_semantics_unknown_editor_evidence_and_children(self):
        control = next(c for c in self.o["controls"] if c["id"] == "a")
        control["editor"] = {"plane": "editor_buffer", "focused": {"status": "unknown", "value": None},
                             "value_settable": {"status": "ok", "value": False}}
        control["semantics"] = {"help": "Observed description", "identifier": "section"}
        result = ui.detail(self.o, "a", actions=self.actions)
        fields = {i["field"]: i["value"] for i in result["items"] if i["kind"] == "attribute"}
        self.assertEqual(fields["editor"], control["editor"])
        self.assertEqual(fields["semantics"], control["semantics"])
        self.assertIn("a0", fields["children"])
        self.assertFalse(result["provenance"]["creates_handles"])
        self.assertEqual(ui.exposed_controls(result)[0]["id"], "a")

    def test_retained_age_is_readable_and_foreign_action_binding_refused(self):
        self.o["observed_at_ns"] -= 10**12
        self.o["provenance"]["observed_at_ns"] = self.o["observed_at_ns"]
        self.assertTrue(ui.overview(self.o)["retained_observation_only"])
        actions = deepcopy(self.actions); actions[0]["target"]["window_id"] += 1
        with self.assertRaises(ui.ProgressiveUIError):ui.overview(self.o, actions=actions)
        actions = deepcopy(self.actions); actions[0]["snapshot_id"] = "another"
        with self.assertRaises(ui.ProgressiveUIError):ui.listing(self.o, actions=actions)
        result = ui.listing(self.o)
        self.assertTrue(all(not r["actions"] for r in ui.exposed_controls(result)))

    def test_dense_content_is_compact_and_unnamed_readout_never_needs_list_paging(self):
        o, actions = fixture(55)
        region = next(r["id"] for r in catalog_regions(o)["regions"] if r["root_control_id"] == "a")
        result = list(pages(ui.listing, o, region_id=region, actions=actions))
        self.assertLessEqual(len(result), 2)
        self.assertTrue(all(ui._bytes(p) <= 10000 for p in result))
        self.assertEqual(ui.overview(o, actions=actions)["items"][0]["id"], "read")

    def test_duplicate_ids_corrupt_continuation_and_bad_table_refused(self):
        self.o["controls"].append(deepcopy(self.o["controls"][0]))
        with self.assertRaises(ValueError):ui.overview(self.o)
        o, _ = fixture()
        with self.assertRaises(ui.ProgressiveUIError):ui.listing(o, cursor="garbage")
        with self.assertRaises(ui.ProgressiveUIError):ui.unpack_row({"operation": "list", "columns": ui.COLUMNS}, ["missing"])


if __name__ == "__main__":
    unittest.main()
