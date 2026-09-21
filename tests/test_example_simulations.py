"""Shipped-plan wiring regressions; deterministic oracle, no model or desktop."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from locua.engine.prototype.architecture import execute_plan
from locua.engine.prototype.simulation import SimulatedDriver


class NavigationOracle:
    """Select the declared test sequence from real loop catalogs, never infer."""

    def __init__(self):
        self.names = ["Actions", "Edit details", "Memo"]
        self.index = 0

    def choose(self, **request):
        if self.index >= len(self.names):
            raise AssertionError("The loop requested an unexpected fourth action")
        name = self.names[self.index]
        choices = request["candidates"]
        summary = request["observation_summary"]
        if summary.startswith("UNTRUSTED UI overview"):
            view = json.loads(summary.split("\n", 1)[1])
            regions = [row for row in view["items"]
                       if any(node["name"] == name for node in row["landmarks"])]
            if len(regions) != 1:
                raise AssertionError("The test target must have one observed region")
            selected = "inspect:" + regions[0]["region_id"]
        else:
            actions = [c for c in choices if json.dumps(name) in c["description"]
                       and c["description"].startswith(("Replace text", "Press"))]
            if len(actions) != 1:
                raise AssertionError("The test target must have one offered action")
            selected = actions[0]["id"]
            self.index += 1
        return {"selected_id": selected, "abstained": False}


class ShippedSimulationTests(unittest.TestCase):
    def test_transient_menu_receipt_survives_dialog_transition_only_when_declared(self):
        examples = Path(__file__).resolve().parents[1] / "examples/regression-simulations.json"
        fixture = next(c for c in json.loads(examples.read_text()) if c["id"] == "menu-dialog")
        original = deepcopy(fixture)
        self.assertEqual(fixture["reference_plan"]["outcomes"][0]["completion"], "milestone")
        for malformed in (False, True):
            with self.subTest(persistent_menu=malformed), tempfile.TemporaryDirectory() as temp:
                case = deepcopy(fixture)
                if malformed:
                    del case["reference_plan"]["outcomes"][0]["completion"]
                driver = SimulatedDriver(case)
                result = execute_plan(case["reference_plan"], driver, NavigationOracle(),
                                      out=Path(temp), cancel=threading.Event(), progress=lambda _: None)
                controls = {c["key"]: c for c in driver.controls}
                self.assertFalse(controls["edit"]["visible"])
                self.assertTrue(controls["dialog"]["visible"])
                if malformed:
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(result["reason"], "completion_receipt_precedes_required_state")
                    self.assertEqual(len(driver.actions), 2)
                    self.assertEqual(controls["memo"]["value"], "")
                else:
                    self.assertEqual(result["status"], "complete", result["reason"])
                    self.assertEqual(len(driver.actions), 3)
                    self.assertEqual(controls["memo"]["value"], "Survey complete")
                    self.assertEqual(set(result["ledger"]), {"open-menu", "open-dialog", "memo"})
                    self.assertTrue(all(r["state"] == "verified" for r in result["ledger"].values()))
                    self.assertFalse(result["saved_output_proven"])
        self.assertEqual(fixture, original)


if __name__ == "__main__":
    unittest.main()
