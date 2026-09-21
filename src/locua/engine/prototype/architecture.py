"""Developer preview: local planning, durable state, progressive inspection and RLCD."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import signal
import threading
import time
import sys

from .cli import private_json, prepare_owned_browser, loopback_url
from .core import run_loop
from .decision import LAB, ModelService
from .planning_contracts import validate_plan, ground_plan
from .task_state import TaskState
from .simulation import SimulatedDriver


class TimedDriver:
    def __init__(self, driver):
        self.driver = driver
        self.timing = {"observation_calls": 0, "observation_wall_s": 0, "dispatch_calls": 0, "dispatch_wall_s": 0}

    def observe(self):
        started = time.monotonic()
        self.timing["observation_calls"] += 1
        try:
            return self.driver.observe()
        finally:
            self.timing["observation_wall_s"] += time.monotonic()-started

    def dispatch(self, action):
        started = time.monotonic()
        self.timing["dispatch_calls"] += 1
        try:
            return self.driver.dispatch(action)
        finally:
            self.timing["dispatch_wall_s"] += time.monotonic()-started


def generate_plan(request, scope, model, out, progress, *, supplied_data=None):
    from .planner import PlannerService
    started = time.monotonic()
    progress("Loading local planner: " + model + " (ordinary generation; separate from RLCD)")
    with PlannerService(model=model) as planner:
        result = planner.plan(request, scope, supplied_data=supplied_data)
    result["planning_with_load_wall_s"] = time.monotonic() - started
    private_json(out / "planner-result.json", result)
    if result.get("plan") is None:
        raise ValueError("planner_output_invalid:" + str(result.get("parse_error")))
    plan = result["plan"]
    validate_plan(plan, request=request, scope=scope, supplied_data=supplied_data)
    private_json(out / "plan.json", plan)
    progress("Plan recorded; " + str(len(plan["outcomes"])) + " outcomes, " + str(len(plan["unknowns"])) + " questions")
    return plan, result


def execute_plan(plan, driver, selector, *, out, cancel, progress, supplied_data=None, execute=True,
                 inspection_policy="model_led"):
    from .cua import PrivateTrace
    if inspection_policy not in ("model_led", "reviewed_target_first"):
        raise ValueError("Unsupported inspection policy")
    initial = driver.observe()
    private_json(out / "initial-observation.json", initial)
    task, bindings = ground_plan(plan, initial, supplied_data=supplied_data)
    private_json(out / "grounded-task.json", {"task": task, "bindings": bindings})
    state = TaskState(plan, out / "task-state.json")
    with_trace = PrivateTrace(out / "events.jsonl")
    def trace(event):
        with_trace(event)
        if event["type"] == "region_detail":
            progress("Inspecting UI region: " + str(event["region"]["label"]))
        elif event["type"] == "inspection_route":
            progress("Inspection policy " + event["inspection_policy"] + ": " + event["reason"])
        elif event["type"] == "decision":
            progress("Local RLCD selected the next " + ("inspection" if event["phase"] == "overview" else "step"))
        elif event["type"] == "dispatch":
            outcome = next((x for x in plan["outcomes"] if x["id"] == event["intent_id"]), None)
            subject = outcome["subject"] if outcome else {}
            name = subject.get("name", subject.get("role", event["intent_id"]))
            ancestor = subject.get("ancestor", {}).get("name")
            progress("Applied change to " + (ancestor + " > " if ancestor else "") + name + "; verifying fresh readback")
        elif event["type"] == "assessment":
            progress(event["status"] + ": " + event["reason"])
    try:
        result = run_loop(task, driver, selector, cancel=cancel, max_steps=12, max_seconds=240,
                          trace=trace, execute=execute, regions=True, observation_tools=True,
                          max_inspections=4, task_state=state, inspection_policy=inspection_policy)
        private_json(out / "result.json", result)
        return result
    finally:
        with_trace.close()

