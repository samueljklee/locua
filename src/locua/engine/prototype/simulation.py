"""Declarative UI simulator for wiring tests, explicitly never desktop evidence."""
from copy import deepcopy
import time


class SimulatedDriver:
    def __init__(self, fixture, *, uncertain_effect=False):
        self.fixture = deepcopy(fixture)
        self.controls = self.fixture["controls"]
        self.sequence = 0
        self.actions = []
        self.uncertain_effect = uncertain_effect

    def observe(self):
        self.sequence += 1
        sid = "p" + str(self.sequence)
        target = {"target_id": "simulated-browser", "tab_id": self.fixture["id"]}
        ids = {c["key"]: f"browser:{sid}:{n}" for n, c in enumerate(self.controls)}
        controls, handles = [], {}
        for n, c in enumerate(self.controls):
            if c.get("visible") is False:
                continue
            ref = f"{sid}:{n}"
            node = {"ref": ref, "parent_ref": ids[c["parent"]].split("browser:", 1)[1] if c.get("parent") else None}
            control = {"id": ids[c["key"]], "role": c["role"], "name": c["name"], "value": c.get("value"),
                "states": c.get("states", {}), "actions": c.get("actions", []), "parent": ids.get(c.get("parent")),
                "bounds": None, "source": {"kind": "browser", "node": node},
                "value_evidence": {"precision": "exact", "exact_value_proven": True, "plane": "editor_buffer", "basis": "simulated_explicit_value"}}
            controls.append(control)
            if c.get("actions"):
                handles[control["id"]] = {"kind": "browser", **target, "snapshot_id": sid, "ref": ref}
        return {"kind": "browser_semantic_v2", "target": target, "snapshot_id": sid,
            "observed_at_ns": time.time_ns(), "controls": controls, "handles": handles,
            "coverage": {"complete": True, "structured_parent_links_available": True},
            "hierarchy": [], "text": "SIMULATED UI — no desktop observation", "provenance": {"simulated": True}}

    def dispatch(self, action):
        if action["snapshot_id"] != "p" + str(self.sequence):
            raise ValueError("stale_simulated_action")
        self.actions.append(deepcopy(action))
        if self.uncertain_effect:
            return {"effect": "unknown", "simulated": True}
        index = int(action["handle"]["ref"].split(":")[-1])
        control = self.controls[index]
        if action["kind"] == "set_text":
            control["value"] = action["value"]
        elif action["kind"] == "press":
            effect = self.fixture.get("effects", {}).get(control["key"], {})
            for c in self.controls:
                if c["key"] in effect.get("show", []):
                    c["visible"] = True
                if c["key"] in effect.get("hide", []):
                    c["visible"] = False
            for key in ("checked", "selected"):
                if type(control.get("states", {}).get(key)) is bool:
                    control["states"][key] = not control["states"][key]
        return {"effect": "unverified", "simulated": True}
