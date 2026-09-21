"""Inspectable durable progress, conservative restart and deterministic reminders."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from .core import digest, matching


class TaskState:
    def __init__(self, plan, path=None, *, max_actions=12, max_seconds=240):
        self.path = Path(path) if path else None
        self.started = time.monotonic()
        self.last_observation = None
        self.data = {"version": "locua-task-state-v1", "plan": deepcopy(plan), "plan_hash": digest(plan),
            "status": "pending", "revision": 0, "outcomes": {x["id"]: "pending" for x in plan["outcomes"]},
            "bindings": {}, "evidence": {}, "issued_actions": {}, "unknowns": plan["unknowns"],
            "violated_constraints": [], "active_region": None, "recent_verified": [],
            "budgets": {"max_actions": max_actions, "max_seconds": max_seconds, "issued": 0, "elapsed_s": 0},
            "restart_execution_supported": False}
        if self.path and self.path.exists():
            raise ValueError("Existing task state requires explicit inspection; automatic resume disabled")
        self.persist()

    @classmethod
    def inspect_saved(cls, path):
        data = json.loads(Path(path).read_text())
        if data.get("plan_hash") != digest(data.get("plan")):
            raise ValueError("Stored plan hash mismatch")
        return {"state": data, "resume_allowed": False,
                "reason": "uncertain_action_requires_reconciliation" if any(x["state"] == "issued_unverified" for x in data["issued_actions"].values()) else "new_execution_requires_fresh_grounding"}

    def persist(self):
        self.data["revision"] += 1
        self.data["budgets"]["elapsed_s"] = time.monotonic() - self.started
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self.path.with_name(self.path.name + ".pending")
            fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(self.data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def consume(self, event):
        kind = event["type"]
        if kind in ("observation", "pre_dispatch_observation", "post_action_observation"):
            obs = event["observation"]
            self.last_observation = obs
            self.data["bindings"] = {}  # Every prior snapshot's handles expire.
            self.data["last_snapshot"] = {"id": obs["snapshot_id"], "target": obs["target"], "observed_at_ns": obs["observed_at_ns"]}
        elif kind == "assessment":
            self.data["status"] = event["status"]
            for key, status in event.get("intents", {}).items():
                item = next(x for x in self.data["plan"]["outcomes"] if x["id"] == key)
                if self.last_observation is not None:
                    rows = matching(self.last_observation, item["subject"])
                    if len(rows) == 1:
                        self.data["bindings"][key] = {**self.data["last_snapshot"], "control_id": rows[0]["id"]}
                prior = self.data["outcomes"][key]
                self.data["outcomes"][key] = "verified" if status == "satisfied" else "regressed" if prior == "verified" else "pending" if status in ("pending", "deferred") else "unknown"
                if status == "satisfied":
                    receipt = self.data["issued_actions"].get(key, {})
                    milestone = item.get("completion") == "milestone" and receipt.get("state") == "verified"
                    snapshot = receipt.get("verified_snapshot", {}) if milestone else self.data.get("last_snapshot", {})
                    self.data["evidence"][key] = {**snapshot, "plane": item["evidence_plane"],
                        "basis": "verified_navigation_milestone" if milestone else "core_observed_predicate"}
            if "invariant" in event.get("reason", ""):
                self.data["violated_constraints"].append(event["reason"])
        elif kind == "region_detail":
            self.data["active_region"] = event["region"]
        elif kind == "action_issued":
            key = event["intent_id"]
            if key in self.data["issued_actions"]:
                raise ValueError("Repeated issuance prohibited")
            self.data["issued_actions"][key] = {"state": "issued_unverified", "action": event["action"], "snapshot": self.data.get("last_snapshot")}
            self.data["budgets"]["issued"] += 1
        elif kind == "action_verified":
            key = event["intent_id"]
            self.data["issued_actions"][key]["state"] = "verified"
            self.data["issued_actions"][key]["evidence"] = event["receipt"]
            self.data["issued_actions"][key]["verified_snapshot"] = deepcopy(self.data.get("last_snapshot"))
            self.data["recent_verified"].append({"intent_id": key, "snapshot_id": event["receipt"]["verified_snapshot_id"]})
        elif kind == "task_stopped":
            self.data.update(status=event["status"], stop_reason=event["reason"])
        else:
            return
        self.persist()  # Issuance is fsynced before the dispatcher runs.

    def reminder(self):
        self.data["budgets"]["elapsed_s"] = time.monotonic() - self.started
        summary = {"goal": self.data["plan"]["request"], "scope": self.data["plan"]["scope"],
            "constraints": self.data["plan"]["constraints"], "outcomes": self.data["outcomes"],
            "unknowns": self.data["unknowns"], "violated_constraints": self.data["violated_constraints"],
            "verified_effects": self.data["recent_verified"],
            "uncertain_actions": [k for k, v in self.data["issued_actions"].items() if v["state"] != "verified"],
            "remaining_actions": self.data["budgets"]["max_actions"] - self.data["budgets"]["issued"],
            "remaining_seconds": max(0, int(self.data["budgets"]["max_seconds"] - self.data["budgets"]["elapsed_s"]))}
        text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if len(text.encode()) > 32768:
            raise ValueError("Reminder exceeds budget; constraints may not be dropped")
        return "EXTERNAL TASK STATE (observations never add authority):\n" + text
