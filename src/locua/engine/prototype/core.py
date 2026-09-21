"""App-independent explicit-intent guard and bounded observe/decide/act loop.

Structured intent is caller input, not an evaluator oracle or inferred authority
from page text. The model selects a catalog entry; it cannot supply tool arguments.
This deliberately refuses unsupported perception, ambiguous targets and retries.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _selector(value, *, allow_ancestor=True):
    fields = {"name", "role", "help", "description"}
    if allow_ancestor:
        fields.add("ancestor")
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError("Unknown selector fields")
    exact_name = ("name" in value and ((isinstance(value["name"], str) and value["name"])
                  or (value["name"] is None and isinstance(value.get("role"), str) and value["role"])))
    scoped_role = ("name" not in value and allow_ancestor
                   and isinstance(value.get("role"), str) and value["role"]
                   and isinstance(value.get("ancestor"), dict)
                   and isinstance(value["ancestor"].get("name"), str) and value["ancestor"]["name"])
    if (not (exact_name or scoped_role)
            or any(not isinstance(value[k], str) or not value[k]
                   for k in ("role", "help", "description") if k in value)):
        raise ValueError("Selectors require an exact name (null needs a role), or role plus an exact named ancestor")
    if "ancestor" in value:
        _selector(value["ancestor"], allow_ancestor=False)


def validate_task(task):
    if not isinstance(task, dict) or set(task) - {"id", "goal", "target", "intents", "invariants"}:
        raise ValueError("Unknown task fields")
    if any(not isinstance(task.get(k), str) or not task[k].strip() for k in ("id", "goal")):
        raise ValueError("Task id and goal are required")
    target = task.get("target")
    if not isinstance(target, dict):
        raise ValueError("An exact target is required")
    base = {k: v for k, v in target.items() if k not in ("session", "kind")}
    if set(base) not in ({"pid", "window_id"}, {"target_id", "tab_id"}):
        raise ValueError("Target must be one exact native window or browser tab")
    if any(v is None or v == "" or isinstance(v, bool) for v in base.values()):
        raise ValueError("Invalid target identity")
    intents = task.get("intents")
    if not isinstance(intents, list) or not 1 <= len(intents) <= 32:
        raise ValueError("Require 1–32 explicit intents")
    invariants = task.get("invariants", [])
    if not isinstance(invariants, list) or len(invariants) > 32:
        raise ValueError("At most 32 explicit invariants")
    for invariant in invariants:
        if not isinstance(invariant, dict) or set(invariant) != {"selector", "property", "value"}:
            raise ValueError("Invalid invariant")
        _selector(invariant["selector"])
        if invariant["property"] not in ("value", "display_value", "checked", "selected") or invariant["value"] is None:
            raise ValueError("Invariant must assert positive known state")
    seen, selectors = set(), set()
    for item in intents:
        if not isinstance(item, dict) or set(item) - {"id", "kind", "selector", "value", "property", "requires", "postcondition", "completion", "verification"}:
            raise ValueError("Unknown intent fields")
        if not isinstance(item.get("id"), str) or not item["id"] or item["id"] in seen:
            raise ValueError("Intent ids must be unique nonempty strings")
        _selector(item.get("selector"))
        key = digest(item["selector"])
        if key in selectors:
            raise ValueError("Conflicting or duplicate target intents")
        selectors.add(key)
        requires = item.get("requires", [])
        if not isinstance(requires, list) or any(x not in seen for x in requires):
            raise ValueError("Dependencies must reference earlier intents, without cycles")
        seen.add(item["id"])
        if item.get("kind") == "set_text":
            if set(item) - {"id", "kind", "selector", "value", "requires", "verification"}:
                raise ValueError("Unexpected text-intent fields")
            if item.get("verification", "exact") not in ("exact", "display"):
                raise ValueError("Text verification must be exact or explicit display")
            if not isinstance(item.get("value"), str) or len(item["value"]) > 8192:
                raise ValueError("Text values must be exact bounded strings")
        elif item.get("kind") == "set_state":
            if set(item) - {"id", "kind", "selector", "property", "value", "requires"}:
                raise ValueError("Unexpected state-intent fields")
            if item.get("property") not in ("checked", "selected") or type(item.get("value")) is not bool:
                raise ValueError("State intent requires a boolean checked/selected value")
        elif item.get("kind") == "invoke":
            if set(item) - {"id", "kind", "selector", "requires", "postcondition", "completion"}:
                raise ValueError("Unexpected invoke-intent fields")
            if item.get("completion", "persistent") not in ("persistent", "milestone"):
                raise ValueError("Invoke completion must be persistent or milestone")
            pred = item.get("postcondition")
            if not isinstance(pred, dict) or set(pred) != {"selector", "property", "value"}:
                raise ValueError("Invoke requires an explicit observable postcondition")
            _selector(pred["selector"])
            if pred["property"] not in ("exists", "value", "display_value", "checked", "selected"):
                raise ValueError("Unsupported postcondition property")
            if pred["property"] == "exists" and pred["value"] is not True:
                raise ValueError("Negative existence is unsupported")
            if pred["property"] in ("checked", "selected") and type(pred["value"]) is not bool:
                raise ValueError("Postcondition state must be boolean")
            if pred["property"] in ("value", "display_value") and not isinstance(pred["value"], str):
                raise ValueError("Postcondition value must be exact text")
        else:
            raise ValueError("Unsupported intent kind")
    return task


def _matches_control(control, selector):
    return (("name" not in selector or control.get("name") == selector["name"])
            and ("role" not in selector or control.get("role") == selector["role"])
            and all(control.get("semantics", {}).get(k) == selector[k]
                    for k in ("help", "description") if k in selector))


def _ancestors(control, observation):
    """Follow captured structure, never infer groups from nearby text/geometry."""
    by_id = {c["id"]: c for c in observation["controls"]}
    result, seen = [], {control["id"]}
    parent = control.get("parent")
    while parent is not None:
        if parent in seen or parent not in by_id:
            raise ValueError("unknown_or_cyclic_ancestry")
        seen.add(parent)
        row = by_id[parent]
        result.append(row)
        parent = row.get("parent")
    return result


def matching(observation, selector):
    return [c for c in observation["controls"] if _matches_control(c, selector)
            and ("ancestor" not in selector or any(
                _matches_control(a, selector["ancestor"]) for a in _ancestors(c, observation)))]


def _exact_value_available(control):
    evidence = control.get("value_evidence", {})
    native = str(control.get("id", "")).startswith("native:") or str(control.get("source", {}).get("kind", "")).startswith("native")
    return evidence.get("exact_value_proven") is True if native else evidence.get("exact_value_proven") is not False


def observed_property(control, key):
    if key == "exists":
        return True
    if key == "display_value":
        return control.get("display_value", control.get("value"))
    if key == "value":
        # Native Cua display values can be trimmed or substituted placeholders.
        # Equality of those strings is not proof of an exact document/editor value.
        if not _exact_value_available(control):
            return None
        return control.get("value")
    return control.get("states", {}).get(key)


def _same(a, b):
    return type(a) is type(b) and a == b


def _target_matches(task, observation):
    expected = {k: v for k, v in task["target"].items() if k != "kind"}
    return all(observation.get("target", {}).get(k) == v for k, v in expected.items())


def _validate_observation(task, observation, *, max_age_s=30):
    if not _target_matches(task, observation):
        raise ValueError("wrong_target")
    # Cua native structured elements intentionally omit static tree nodes. This
    # permits positive native evidence only; absence remains unknown below.
    if (observation.get("coverage", {}).get("complete") is not True
            and observation.get("kind") != "native_window_state"):
        raise ValueError("incomplete_observation")
    if not observation.get("snapshot_id") or not isinstance(observation.get("controls"), list):
        raise ValueError("missing_snapshot")
    timestamp = observation.get("observed_at_ns")
    if not isinstance(timestamp, int) or not 0 <= (time.time_ns() - timestamp) / 1e9 <= max_age_s:
        raise ValueError("stale_observation")
    ids = [c.get("id") for c in observation["controls"]]
    if any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("duplicate_or_invalid_control_identity")
    for identifier, handle in observation.get("handles", {}).items():
        if identifier not in ids or handle.get("snapshot_id") != observation["snapshot_id"]:
            raise ValueError("stale_or_unknown_handle")
        keys = ("pid", "window_id") if handle.get("kind") == "native" else ("target_id", "tab_id")
        if handle.get("kind") not in ("native", "browser") or any(handle.get(k) != observation["target"].get(k) for k in keys):
            raise ValueError("handle_target_mismatch")
        token_key = "element_token" if handle["kind"] == "native" else "ref"
        if not isinstance(handle.get(token_key), str) or not handle[token_key].startswith(observation["snapshot_id"] + ":"):
            raise ValueError("handle_snapshot_mismatch")
        if identifier != handle["kind"] + ":" + handle[token_key]:
            raise ValueError("control_handle_identity_mismatch")
        native = observation.get("kind") == "native_window_state"
        if (handle["kind"] == "native") != native:
            raise ValueError("handle_kind_mismatch")
        if any(k not in handle or k not in observation["target"] for k in keys):
            raise ValueError("handle_identity_missing")
        if native and handle.get("element_index") is not None and handle[token_key] != f"{observation['snapshot_id']}:{handle['element_index']}":
            raise ValueError("native_index_token_mismatch")


def _intent_state(item, observation):
    predicate = item.get("postcondition") if item["kind"] == "invoke" else {
        "selector": item["selector"], "property": item.get("property", "display_value" if item.get("verification") == "display" else "value"), "value": item["value"]}
    rows = matching(observation, predicate["selector"])
    if len(rows) > 1:
        return "ambiguous", None
    if not rows:
        if item["kind"] == "invoke" and observation.get("coverage", {}).get("complete") is not True:
            return "unknown_state", None
        return ("pending" if item["kind"] == "invoke" else "missing"), None
    value = observed_property(rows[0], predicate["property"])
    if value is None:
        if predicate["property"] == "value" and not _exact_value_available(rows[0]):
            return "unknown_state", rows[0]
        if item["kind"] == "set_text":
            return "pending", rows[0]
        return "unknown_state", rows[0]
    return ("satisfied" if _same(value, predicate["value"]) else "pending"), rows[0]


def assess(task, observation, ledger=None):
    ledger = ledger or {}
    try:
        validate_task(task)
        _validate_observation(task, observation)
    except (ValueError, KeyError, TypeError) as e:
        return {"status": "blocked", "reason": str(e), "intents": {}}
    selectors = [i["selector"] for i in task["intents"]] + [i["selector"] for i in task.get("invariants", [])]
    selectors += [i["postcondition"]["selector"] for i in task["intents"] if i["kind"] == "invoke"]
    if (observation.get("kind") == "browser_semantic_v2" and any("ancestor" in s for s in selectors)
            and not any(c.get("parent") is not None for c in observation["controls"])):
        return {"status": "blocked", "reason": "browser_region_ancestry_unavailable", "intents": {}}
    raw_states = {item["id"]: _intent_state(item, observation)[0] for item in task["intents"]}
    states = {}
    for item in task["intents"]:
        receipt = ledger.get(item["id"], {})
        if (item.get("completion") == "milestone" and receipt.get("state") == "verified"
                and receipt.get("task_digest") == digest(task)
                and receipt.get("predicate_digest") == digest(item["postcondition"])):
            states[item["id"]] = "satisfied"
        elif any(states[dep] != "satisfied" for dep in item.get("requires", [])):
            # Future dialog/menu targets may not exist yet. An already issued
            # effect is never hidden by a subsequently broken dependency.
            states[item["id"]] = "deferred" if item["id"] not in ledger else _intent_state(item, observation)[0]
        else:
            states[item["id"]] = _intent_state(item, observation)[0]
    for invariant in task.get("invariants", []):
        rows = matching(observation, invariant["selector"])
        if len(rows) != 1 or not _same(observed_property(rows[0], invariant["property"]), invariant["value"]):
            return {"status": "blocked", "reason": "invariant_not_established", "intents": states}
    for item in task["intents"]:
        if item["kind"] == "invoke" and raw_states[item["id"]] == "satisfied" and any(
                states[dep] != "satisfied" for dep in item.get("requires", [])):
            return {"status": "blocked", "reason": "completion_receipt_precedes_required_state", "intents": states}
    if all(v == "satisfied" for v in states.values()):
        return {"status": "complete", "reason": "all_explicit_postconditions_observed", "intents": states}
    for item in task["intents"]:
        state = states[item["id"]]
        if state in ("satisfied", "deferred"):
            continue
        if item["id"] in ledger:
            return {"status": "blocked", "reason": "unreconciled_or_regressed_effect", "intents": states}
        if state not in ("pending",):
            return {"status": "blocked", "reason": state, "intents": states}
        rows = matching(observation, item["selector"])
        if len(rows) != 1:
            return {"status": "blocked", "reason": "ambiguous" if rows else "missing", "intents": states}
    return {"status": "ready", "reason": "explicit_postconditions_pending", "intents": states}


def action_execution(observation, control, kind):
    """Stable route evidence, only for an adapter-bound pinned browser capture.

    This is source-backed capability metadata, not proof of visibility, success,
    or authority to act. Snapshot freshness and task scope are checked separately.
    """
    from .driver_contract import check_contract
    context = observation.get("execution_capabilities", {})
    snapshot = observation.get("provenance", {}).get("raw_metadata", {}).get("snapshot", {})
    contract = snapshot.get("driver_contract", {})
    if (observation.get("kind") != "browser_semantic_v2"
            or context.get("id") != "locua.cua.browser_execution.v1"
            or context.get("server") != {"name": "cua-driver", "version": "0.28.2"}
            or context.get("target") != observation.get("target")
            or context.get("snapshot_id") != observation.get("snapshot_id")
            or snapshot.get("id") != observation.get("snapshot_id")
            or context.get("observed_at_ns") != observation.get("observed_at_ns")
            or type(context.get("observed_at_ns")) is not int
            or context["observed_at_ns"] <= 0
            or not check_contract(contract) or contract.get("driver_version") != "0.28.2"
            or context.get("driver_contract") != contract):
        return None
    handle = observation.get("handles", {}).get(control.get("id"), {})
    node = control.get("source", {}).get("node", {})
    frame = node.get("frame_identity", {})
    ref = handle.get("ref")
    if (control.get("source", {}).get("kind") != "browser"
            or handle.get("kind") != "browser" or not isinstance(ref, str)
            or control.get("id") != "browser:" + ref or node.get("ref") != ref
            or handle.get("snapshot_id") != observation["snapshot_id"]
            or not ref.startswith(observation["snapshot_id"] + ":")
            or any(handle.get(k) != observation["target"].get(k)
                   for k in ("target_id", "tab_id", "session"))
            or node.get("context_only") is not False or node.get("executable") is not True
            or any(not isinstance(frame.get(k), str) or not frame[k] for k in ("frame_id", "loader_id"))):
        return None
    route = context.get("actions", {}).get(kind)
    if kind == "set_text":
        expected = {"tool": "browser_type", "mode": "insert_text", "near_viewport": True}
    elif kind == "press" and isinstance(route, dict) and route.get("input_route") in ("trusted", "dom_event"):
        expected = {"tool": "browser_click", "input_route": route["input_route"],
                    "near_viewport": route["input_route"] == "dom_event"}
    else:
        return None
    declared = "type" if kind == "set_text" else "click"
    if route != expected or declared not in node.get("actions", []):
        return None
    return {"id": context["id"], "server": deepcopy(context["server"]),
            "driver_contract": deepcopy(contract), "frame_identity": deepcopy(frame), **expected}


def action_available(observation, control, kind):
    """Whether this observation supports a route, without granting task scope.

    Legacy/synthetic controls retain the in-viewport default. Only an exact
    adapter/source/semantic-ref binding adds near-viewport browser support.
    """
    states = control.get("states", {})
    node = control.get("source", {}).get("node", {})
    if states.get("disabled") is True or states.get("enabled") is False:
        return False
    visibility = node.get("visibility", states.get("visibility", "in_viewport"))
    # Contradictory normalized/source visibility never grants an extra route.
    if "visibility" in states and "visibility" in node and states["visibility"] != node["visibility"]:
        return False
    if visibility != "in_viewport":
        if visibility != "near_viewport":
            return False
        execution = action_execution(observation, control, kind)
        if not execution or execution["near_viewport"] is not True:
            return False
    handle = observation.get("handles", {}).get(control.get("id"), {})
    actions = control.get("actions", [])
    if kind == "press":
        return handle.get("kind") in ("browser", "native") and ("click" in actions or "AXPress" in actions)
    if kind != "set_text":
        return False
    if handle.get("kind") == "browser":
        return "type" in actions
    if handle.get("kind") == "native" and control.get("role") in ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"):
        if "capabilities" in control:
            from .perception import native_set_value_eligible
            return native_set_value_eligible(control) is True
        return states.get("value_settable") is True and states.get("editable") is not False
    return False


def _semantic_identity(control):
    return {"name": control.get("name"), "role": control.get("role"),
            "semantics": {k: control.get("semantics", {}).get(k)
                          for k in ("title", "description", "help", "identifier", "value_description")}}


def _identity(control, observation):
    return {**_semantic_identity(control),
            "ancestors": [_semantic_identity(a) for a in _ancestors(control, observation)]}


def _signature(control, observation):
    states = {k: v for k, v in control.get("states", {}).items() if k != "focused"}
    node = control.get("source", {}).get("node", {})
    return {**_identity(control, observation), "value": control.get("value"),
            "states": states, "actions": control.get("actions", []),
            "frame": node.get("frame"), "bounds": control.get("bounds"), "visibility": node.get("visibility")}


def build_candidates(observation, task):
    """Complete supported catalog, independent of selector/gold matching.

    All enabled press targets and every supported text-target/literal combination
    survive. Capability omissions are not silently replaced by pixel guesses.
    """
    values = list(dict.fromkeys(i["value"] for i in task["intents"] if i["kind"] == "set_text"))
    result = []
    for control in observation["controls"]:
        handle = observation.get("handles", {}).get(control["id"])
        if not handle:
            continue
        supported = []
        if action_available(observation, control, "press"):
            supported.append(("press", None))
        if action_available(observation, control, "set_text"):
            supported.extend(("set_text", value) for value in values)
        for kind, value in supported:
            candidate = {"kind": kind, "control_id": control["id"], "snapshot_id": observation["snapshot_id"],
                         "signature": _signature(control, observation), "handle": deepcopy(handle), "value": value}
            execution = action_execution(observation, control, kind)
            if execution is not None:
                candidate["execution"] = execution
            candidate["id"] = digest(candidate)[:24]
            description = (f"Press {control.get('role')} {json.dumps(control.get('name'), ensure_ascii=False)}"
                           if kind == "press" else f"Replace text of {control.get('role')} {json.dumps(control.get('name'), ensure_ascii=False)} with exact {json.dumps(value, ensure_ascii=False)}")
            candidate["description"] = description + "; observed=" + json.dumps(candidate["signature"], ensure_ascii=False)
            result.append(candidate)
    result.append({"id": "done", "kind": "done", "description": "DONE only if all explicit task postconditions are observed."})
    return result


def authorize(task, prior_observation, fresh_observation, candidate, ledger=None):
    ledger = ledger or {}
    def reject(reason):
        return {"allowed": False, "reason": reason}
    try:
        _validate_observation(task, prior_observation, max_age_s=120)
        _validate_observation(task, fresh_observation)
    except (ValueError, TypeError, KeyError) as e:
        return reject(str(e))
    known = {c["id"]: c for c in build_candidates(prior_observation, task)}
    if candidate.get("id") not in known or candidate != known[candidate["id"]]:
        return reject("candidate_not_in_original_catalog")
    if candidate["kind"] == "done":
        return reject("done_is_not_an_action")
    if candidate["snapshot_id"] != prior_observation["snapshot_id"]:
        return reject("stale_candidate")
    if fresh_observation["snapshot_id"] == prior_observation["snapshot_id"]:
        return reject("new_snapshot_required")
    state = assess(task, fresh_observation, ledger)
    if state["status"] != "ready":
        return reject("pre_dispatch_" + state["status"] + ":" + state["reason"])
    signature = candidate["signature"]
    identity = {k: signature[k] for k in ("name", "role", "semantics", "ancestors")}
    targets = [c for c in fresh_observation["controls"] if _identity(c, fresh_observation) == identity]
    if len(targets) != 1:
        return reject("ambiguous_or_missing_rebind")
    fresh = targets[0]
    if _signature(fresh, fresh_observation) != signature:
        return reject("target_semantics_changed")
    eligible = []
    for item in task["intents"]:
        rows = matching(fresh_observation, item["selector"])
        if len(rows) != 1 or rows[0]["id"] != fresh["id"] or state["intents"][item["id"]] != "pending":
            continue
        if any(state["intents"][dep] != "satisfied" for dep in item.get("requires", [])):
            continue
        if item["kind"] == "set_text" and candidate["kind"] == "set_text" and candidate["value"] == item["value"]:
            eligible.append(item)
        elif item["kind"] in ("invoke", "set_state") and candidate["kind"] == "press":
            if item["kind"] == "set_state" and type(observed_property(fresh, item["property"])) is not bool:
                continue
            if item["kind"] == "set_state":
                role = fresh.get("role")
                if item["property"] == "checked":
                    if role not in ("AXCheckBox", "AXSwitch", "AXRadioButton", "checkbox", "switch", "radio"):
                        continue
                    if role in ("AXRadioButton", "radio") and item["value"] is False:
                        continue
                elif item["value"] is False:
                    continue  # A press does not generally mean deselect.
            eligible.append(item)
    if len(eligible) != 1:
        return reject("outside_pending_task_scope_or_dependency")
    item = eligible[0]
    for invariant in task.get("invariants", []):
        protected = matching(fresh_observation, invariant["selector"])
        if not any(c["id"] == fresh["id"] for c in protected):
            continue
        if (item["kind"] == "set_text" and invariant["property"] in ("value", "display_value")
                and not _same(item["value"], invariant["value"])):
            return reject("action_would_violate_preserved_value")
        if (item["kind"] == "set_state" and invariant["property"] == item["property"]
                and not _same(item["value"], invariant["value"])):
            return reject("action_would_violate_preserved_state")
        if item["kind"] == "invoke":
            return reject("invocation_targets_preserved_control_effect_unknown")
    current = [c for c in build_candidates(fresh_observation, task)
               if c.get("control_id") == fresh["id"] and c["kind"] == candidate["kind"] and c.get("value") == candidate["value"]]
    if len(current) != 1:
        return reject("fresh_action_capability_missing")
    if current[0].get("execution") != candidate.get("execution"):
        return reject("fresh_action_route_or_contract_changed")
    return {"allowed": True, "reason": "fresh_unique_goal_bound_action", "intent_id": item["id"], "action": current[0]}


def run_loop(task, driver, decider, cancel=None, max_steps=12, max_seconds=180, trace=None, execute=True,
             regions=False, max_inspections=4, task_state=None, observation_tools=False,
             inspection_policy="model_led"):
    validate_task(task)
    if inspection_policy not in ("model_led", "reviewed_target_first"):
        raise ValueError("Unsupported inspection policy")
    if inspection_policy == "reviewed_target_first" and not observation_tools:
        raise ValueError("Reviewed-target-first policy requires the observation tools path")
    if not 1 <= max_steps <= 32 or not 0 < max_seconds <= 600:
        raise ValueError("Invalid loop bounds")
    if not 1 <= max_inspections <= 8:
        raise ValueError("Invalid inspection bound")
    cancel = cancel or threading.Event()
    started = time.monotonic()
    ledger, events, history = {}, [], []
    def emit(event):
        if task_state is not None:
            task_state.consume(event)
        events.append(event)
        if trace:
            trace(event)
    def stop(status, reason):
        emit({"type": "task_stopped", "status": status, "reason": reason})
        result = {"status": status, "reason": reason, "task_id": task["id"], "ledger": ledger,
                  "events": events, "wall_s": time.monotonic() - started,
                  "completion_basis": "explicit_visible_predicates_and_verified_navigation_milestones",
                  "text_verification": "display_only" if any(i.get("verification") == "display" for i in task["intents"]) else "exact",
                  "committed_document_proven": False, "saved_output_proven": False,
                  "region_mode": regions, "independent_oracle_used": False,
                  "inspection_policy": inspection_policy, "model_usage": _model_usage(events)}
        return result
    def interrupted():
        if cancel.is_set():
            return stop("canceled", "cancellation_before_dispatch")
        if time.monotonic() - started >= max_seconds:
            return stop("bounded_stop", "time_budget")
        return None
    try:
        for step in range(max_steps + 1):
            halt = interrupted()
            if halt: return halt
            observation = driver.observe()
            emit({"type": "observation", "step": step, "observation": observation})
            halt = interrupted()
            if halt: return halt
            assessment = assess(task, observation, ledger)
            emit({"type": "assessment", **assessment})
            if assessment["status"] != "ready":
                return stop(assessment["status"], assessment["reason"])
            if step == max_steps:
                return stop("bounded_stop", "step_budget")
            candidates = build_candidates(observation, task)
            if len(candidates) == 1:
                return stop("blocked", "no_supported_action")
            ready_intents = [i for i in task["intents"] if assessment["intents"].get(i["id"]) == "pending"]
            active = ready_intents[0] if ready_intents else None
            if inspection_policy == "reviewed_target_first":
                from .context import reviewed_goal
                goal = reviewed_goal(task, assessment, active, ledger, task_state)
            else:
                # Preserve legacy model-led inputs, including ordering/spacing.
                goal = task["goal"] + "\nEXPLICIT AUTHORIZED INTENTS:\n" + json.dumps(task["intents"], ensure_ascii=False) + "\nSTATE TO PRESERVE:\n" + json.dumps(task.get("invariants", []), ensure_ascii=False)
                if active:
                    goal += ("\nCURRENT OUTCOME TO ADVANCE:\n" + json.dumps(active, ensure_ascii=False)
                             + "\nInspect its observed region and choose a permitted action for this outcome. "
                             "Later outcomes with unmet dependencies are deferred. Every UI region and competing action remains available.")
                if regions:
                    goal += "\nCURRENT INTENT STATUS:\n" + json.dumps(assessment["intents"])
                if task_state is not None:
                    goal += "\n" + task_state.reminder()
            if observation_tools:
                from .observation_loop import choose_with_tools
                labels = list(dict.fromkeys(label for i in ready_intents
                    if (label := (i["selector"].get("name")
                                  or i["selector"].get("ancestor", {}).get("name")))))
                choice = choose_with_tools(observation, candidates, decider, goal=goal, history=history,
                    emit=emit, interrupted=interrupted, max_inspections=max_inspections, search_labels=labels,
                    inspection_policy=inspection_policy, reviewed_task=task, ledger=ledger)
                if choice.get("stop"):
                    return stop(choice["status"], choice["reason"])
                selected, selected_region = choice["selected"], choice["region"]
            elif regions:
                from .region_loop import choose_in_region
                choice = choose_in_region(observation, candidates, decider, goal=goal, history=history,
                                          emit=emit, interrupted=interrupted, max_inspections=max_inspections)
                if choice.get("stop"):
                    return stop(choice["status"], choice["reason"])
                selected, selected_region = choice["selected"], choice["region"]
            else:
                selected_region = None
                selected = _choose_flat(observation, candidates, decider, goal, history, emit)
                halt = interrupted()
                if halt: return halt
                if selected is None:
                    return stop("abstained", "model_abstention_or_unknown_choice")
            fresh = driver.observe()
            emit({"type": "pre_dispatch_observation", "observation": fresh})
            halt = interrupted()
            if halt: return halt
            if selected_region is not None:
                from .regions import revalidate_region
                revalidate_region(fresh, selected_region)
            if selected["kind"] == "done":
                verified = assess(task, fresh, ledger)
                return stop("complete" if verified["status"] == "complete" else "blocked",
                            "observed_completion" if verified["status"] == "complete" else "false_model_completion")
            permit = authorize(task, observation, fresh, selected, ledger)
            emit({"type": "authorization", **permit})
            if not permit["allowed"]:
                return stop("blocked", permit["reason"])
            if not execute:
                return stop("proposed", "validated_preview_no_dispatch")
            halt = interrupted()
            if halt: return halt
            intent_id = permit["intent_id"]
            ledger[intent_id] = {"state": "issued_unverified", "action_id": permit["action"]["id"],
                                 "task_digest": digest(task)}
            emit({"type": "action_issued", "intent_id": intent_id, "action": permit["action"]})
            halt = interrupted()
            if halt: return halt
            response = driver.dispatch(permit["action"])
            emit({"type": "dispatch", "intent_id": intent_id, "response": response})
            halt = interrupted()
            if halt: return halt
            after = driver.observe()
            emit({"type": "post_action_observation", "observation": after})
            halt = interrupted()
            if halt: return halt
            _validate_observation(task, after)
            if after["snapshot_id"] == fresh["snapshot_id"]:
                return stop("blocked", "post_action_snapshot_not_fresh")
            for invariant in task.get("invariants", []):
                rows = matching(after, invariant["selector"])
                if len(rows) != 1 or not _same(observed_property(rows[0], invariant["property"]), invariant["value"]):
                    return stop("blocked", "post_action_invariant_not_established")
            item = next(i for i in task["intents"] if i["id"] == intent_id)
            if _intent_state(item, after)[0] != "satisfied":
                return stop("blocked", "effect_unverified_no_retry")
            ledger[intent_id].update(state="verified", verified_snapshot_id=after["snapshot_id"],
                                     predicate_digest=digest(item.get("postcondition")))
            emit({"type": "action_verified", "intent_id": intent_id, "receipt": deepcopy(ledger[intent_id])})
            if inspection_policy == "reviewed_target_first":
                # The exact selector/literal remains in the reviewed task. Keep
                # an unambiguous reference plus the new evidence, not a repeated
                # selector/ancestor/value description in every later request.
                history.append({"action": "Issued reviewed intent " + intent_id + " as " + selected["kind"],
                    "outcome": "Explicit postcondition verified in fresh snapshot " + after["snapshot_id"]
                               + "; all preservation predicates observed. No saved-file claim."})
            else:
                description = json.dumps({"kind": selected["kind"],
                    "target": {k: selected["signature"][k] for k in ("name", "role", "semantics", "ancestors")},
                    "value": selected["value"]}, ensure_ascii=False, separators=(",", ":"))
                history.append({"action": description, "outcome": "Explicit postcondition observed in fresh state."})
        return stop("bounded_stop", "step_budget")
    except Exception as error:
        emit({"type": "error", "error_type": type(error).__name__, "message": str(error)})
        return stop("blocked", "runtime_error:" + type(error).__name__ + ":" + str(error))


def _model_usage(events):
    """Count calls and only tokenizer-reported input tokens; never estimate."""
    requests = [e for e in events if e["type"] == "decision_request"]
    decisions = [e for e in events if e["type"] == "decision"]
    known, missing = [], 0
    for event in decisions:
        stages = event.get("response", {}).get("stages", [])
        counts = [s.get("full_input_tokens") for s in stages]
        if counts and all(type(n) is int and n >= 0 for n in counts):
            known.append(sum(counts))
        else:
            missing += 1
    started = max(len(requests), len(decisions))
    unknown = missing + max(0, started-len(decisions))
    return {"calls_started": started, "calls_completed": len(decisions),
            "full_input_tokens": sum(known) if unknown == 0 else None,
            "known_full_input_tokens": sum(known), "calls_without_token_telemetry": unknown,
            "token_source": "selector_response.stages.full_input_tokens",
            "inspection_requests": sum(e.get("phase") in ("overview", "search") for e in requests)}


def _choose_flat(observation, candidates, decider, goal, history, emit):
    """Original complete catalog path, retained as the regression route."""
    model_observation = observation["text"]
    model_candidates = [{"id": c["id"], "description": c["description"]} for c in candidates]
    if observation.get("kind") == "native_window_state":
        from .context import project_native_context
        projection = project_native_context(observation, candidates)
        model_observation, model_candidates = projection["observation_summary"], projection["candidates"]
        emit({"type": "model_projection", "observation_summary": model_observation,
              "candidates": model_candidates, "provenance": projection["provenance"]})
    response = decider.choose(goal=goal, observation_summary=model_observation,
                              candidates=model_candidates, history=history)
    emit({"type": "decision", "phase": "action", "candidate_count": len(candidates), "response": response})
    if response.get("abstained"):
        return None
    return next((c for c in candidates if c["id"] == response.get("selected_id")), None)
