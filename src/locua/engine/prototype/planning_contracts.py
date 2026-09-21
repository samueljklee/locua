"""Provisional outcome plans. Syntax/provenance checks are not semantic approval."""
from copy import deepcopy
from .core import digest, matching, validate_task, _validate_observation

VERSION = "locua-task-plan-v1"
PLANES = {"display", "editor_buffer", "committed_document", "saved_output"}


def subject(value, *, allow_ancestor=True):
    """Exact label, or a role inside an explicitly named observed ancestor.

    Omitted name is intentional: editor labels can be their mutable contents.
    It never means an empty/null name, inferred label, or ordinal target.
    """
    if not isinstance(value, dict) or set(value) - {"name", "role", "ancestor"}:
        raise ValueError("Subject is a description, never a driver handle or selector program")
    if "name" in value:
        if not isinstance(value["name"], str) or not value["name"]:
            raise ValueError("Subject name must be an exact nonempty observed name")
    elif (not allow_ancestor or not isinstance(value.get("role"), str) or not value["role"]
          or not isinstance(value.get("ancestor"), dict)
          or not isinstance(value["ancestor"].get("name"), str) or not value["ancestor"]["name"]):
        raise ValueError("Subject without name requires role and an exact named ancestor")
    if "role" in value and (not isinstance(value["role"], str) or not value["role"]):
        raise ValueError("Invalid subject role")
    if "ancestor" in value:
        if not allow_ancestor:
            raise ValueError("Nested ancestor descriptions unsupported")
        subject(value["ancestor"], allow_ancestor=False)
    return value


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def validate_plan(plan, *, request=None, scope=None, supplied_data=None):
    required = {"version", "request", "scope", "outcomes", "constraints", "unknowns"}
    if not isinstance(plan, dict) or set(plan) != required or plan["version"] != VERSION:
        raise ValueError("Invalid outcome-plan envelope")
    if not isinstance(plan["request"], str) or not plan["request"].strip():
        raise ValueError("Original request required")
    if request is not None and plan["request"] != request:
        raise ValueError("Planner altered original request")
    if not isinstance(plan["scope"], dict) or not plan["scope"]:
        raise ValueError("Explicit caller scope required")
    declared_scope = plan["scope"]
    scope_fields = {"browser": {"kind", "url"}, "native": {"kind", "pid", "window_id"},
                    "simulation": {"kind", "case"}}
    if declared_scope.get("kind") not in scope_fields or set(declared_scope) != scope_fields[declared_scope["kind"]]:
        raise ValueError("Unsupported scope fields; restrictions must never be silently ignored")
    if declared_scope["kind"] == "native":
        if any(type(declared_scope[k]) is not int or declared_scope[k] <= 0 for k in ("pid", "window_id")):
            raise ValueError("Native scope needs positive pid/window_id")
    elif any(not isinstance(v, str) or not v for v in declared_scope.values()):
        raise ValueError("Scope requires nonempty literal strings")
    if scope is not None and plan["scope"] != scope:
        raise ValueError("Planner changed authorized scope")
    for key in ("outcomes", "constraints", "unknowns"):
        maximum = 32 if key == "constraints" else 16
        if not isinstance(plan[key], list) or len(plan[key]) > maximum:
            raise ValueError(f"Plan {key} bounded to {maximum}; no truncation")
    if any(not isinstance(x, str) or not x for x in plan["unknowns"]):
        raise ValueError("Unknowns must be explicit nonempty questions")
    if not plan["outcomes"] and not plan["unknowns"]:
        raise ValueError("Empty plan without explanation")
    literals = list(_strings(supplied_data or {})) + [plan["request"]]
    def predicate(item, *, existence=False):
        subject(item["subject"])
        prop, value = item["property"], item["value"]
        if prop in ("checked", "selected"):
            if type(value) is not bool:
                raise ValueError("State values must be boolean")
        elif prop == "value":
            if not isinstance(value, str) or len(value) > 8192:
                raise ValueError("Exact text values required")
            if value and not any(value in text for text in literals):
                raise ValueError("Desired literal absent from request/supplied data")
        elif existence and prop == "exists" and value is True:
            pass
        else:
            raise ValueError("Unsupported predicate")
    seen = set()
    for item in plan["outcomes"]:
        keys = {"id", "subject", "property", "value", "requires", "evidence_plane", "source_text"}
        if not isinstance(item, dict) or set(item) - (keys | {"postcondition", "completion"}) or not keys <= set(item):
            raise ValueError("Invalid outcome fields")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in seen:
            raise ValueError("Outcome IDs must be unique")
        if not isinstance(item["requires"], list) or len(set(item["requires"])) != len(item["requires"]) or any(x not in seen for x in item["requires"]):
            raise ValueError("Dependencies require earlier outcomes")
        seen.add(item["id"])
        if item["evidence_plane"] not in PLANES:
            raise ValueError("Unknown evidence plane")
        if item["property"] in ("checked", "selected") and item["evidence_plane"] != "display":
            raise ValueError("State predicates support display evidence only")
        if item["property"] == "invoke":
            subject(item["subject"])
            if item.get("completion", "persistent") not in ("persistent", "milestone"):
                raise ValueError("Invoke completion must be persistent or explicit milestone")
            if item["value"] is not None or item["evidence_plane"] != "display":
                raise ValueError("Invoke has null payload and explicit display postcondition")
            pred = item.get("postcondition")
            if not isinstance(pred, dict) or set(pred) != {"subject", "property", "value"}:
                raise ValueError("Invoke needs an explicit observable postcondition")
            predicate(pred, existence=True)
        else:
            if "postcondition" in item or "completion" in item:
                raise ValueError("Postcondition/completion only permitted for invoke")
            predicate(item)
    for item in plan["constraints"]:
        if not isinstance(item, dict) or set(item) != {"subject", "property", "value", "source_text"}:
            raise ValueError("Invalid constraint")
        predicate(item)
    for item in plan["outcomes"] + plan["constraints"]:
        if not isinstance(item["source_text"], str) or not item["source_text"] or item["source_text"] not in plan["request"]:
            raise ValueError("Provenance must quote an exact source span")
    for item in plan["outcomes"]:
        for constraint in plan["constraints"]:
            if (item["subject"] == constraint["subject"] and item["property"] == constraint["property"]
                    and (type(item["value"]) is not type(constraint["value"]) or item["value"] != constraint["value"])):
                raise ValueError("Outcome contradicts an explicit preservation constraint")
    return plan


def ground_plan(plan, observation, *, supplied_data=None):
    """Bind descriptions only by unique fresh observed identity; no fuzzy repair.

    Future dependent subjects may be deferred, but core authorization must uniquely
    rebind them when they become actionable. Unsupported planes stop before action.
    """
    validate_plan(plan, supplied_data=supplied_data)
    if plan["unknowns"]:
        raise ValueError("unresolved_plan_questions:" + "; ".join(plan["unknowns"]))
    if not plan["outcomes"]:
        raise ValueError("no_outcomes")
    scope = plan["scope"]
    if scope.get("kind") == "browser":
        url = observation.get("provenance", {}).get("raw_metadata", {}).get("page", {}).get("url")
        if observation.get("kind") != "browser_semantic_v2" or url != scope.get("url"):
            raise ValueError("observation_outside_browser_scope")
    elif scope.get("kind") == "native":
        if observation.get("kind") != "native_window_state" or any(observation.get("target", {}).get(k) != scope.get(k) for k in ("pid", "window_id")):
            raise ValueError("observation_outside_native_scope")
    elif scope.get("kind") == "simulation":
        if observation.get("provenance", {}).get("simulated") is not True:
            raise ValueError("simulation_scope_cannot_authorize_real_driver")
    else:
        raise ValueError("unsupported_scope_kind")
    task = {"id": "plan-" + digest(plan)[:16], "goal": plan["request"],
            "target": deepcopy(observation["target"]), "intents": [], "invariants": []}
    _validate_observation(task, observation)
    bindings = {}
    def bind(desc, key, deferred=False):
        rows = matching(observation, desc)
        if len(rows) > 1 or (not rows and not deferred):
            raise ValueError("subject_not_uniquely_grounded:" + key)
        bindings[key] = {"snapshot_id": observation["snapshot_id"], "control_id": rows[0]["id"] if rows else None,
                         "status": "observed_unique" if rows else "deferred_until_prerequisites"}
        return deepcopy(desc)
    for item in plan["outcomes"]:
        if item["evidence_plane"] in ("committed_document", "saved_output"):
            raise ValueError("unsupported_execution_evidence_plane:" + item["evidence_plane"])
        selector = bind(item["subject"], item["id"], deferred=bool(item["requires"]))
        common = {"id": item["id"], "selector": selector, "requires": item["requires"]}
        if item["property"] == "invoke":
            pred = item["postcondition"]
            # Absent postconditions are expected before invoking; identities are
            # checked against actual observations by the core after dispatch.
            intent = {**common, "kind": "invoke", "completion": item.get("completion", "persistent"),
                      "postcondition": {"selector": deepcopy(pred["subject"]),
                                        "property": "display_value" if pred["property"] == "value" else pred["property"], "value": pred["value"]}}
        elif item["property"] == "value":
            intent = {**common, "kind": "set_text", "value": item["value"],
                      "verification": "display" if item["evidence_plane"] == "display" else "exact"}
        else:
            intent = {**common, "kind": "set_state", "property": item["property"], "value": item["value"]}
        task["intents"].append(intent)
    for n, item in enumerate(plan["constraints"]):
        task["invariants"].append({"selector": bind(item["subject"], "constraint-" + str(n)),
                                   "property": item["property"], "value": item["value"]})
    validate_task(task)
    return task, bindings
