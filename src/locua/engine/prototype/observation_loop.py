"""Bounded model-controlled overview/search/inspect/page tools on one snapshot."""
from copy import deepcopy
import hashlib
import json
import time
from . import observation_tools as views
from .decision import validate_request
from .regions import catalog_regions, revalidate_region

INSPECTION_POLICIES = ("model_led", "reviewed_target_first")


def _reviewed_route(observation, task, ledger, catalog):
    """Resolve current reviewed intent to an existing view, never to an action.

    Do not consume stored TaskState bindings or reuse prior-snapshot cursors.
    The source facade's same-snapshot cursor encoder permits a direct page jump;
    the offset is derived from control membership/order, never desired values.
    """
    from .core import assess, matching
    basis = {"inspection_policy": "reviewed_target_first", "route": "discovery",
             "snapshot_id": observation.get("snapshot_id"), "target": deepcopy(observation.get("target")),
             "source_observation_sha256": catalog["source_observation_sha256"],
             "authorizes_action": False, "cached_binding_used": False}
    if (catalog.get("snapshot_id") != observation.get("snapshot_id")
            or catalog.get("target") != observation.get("target")
            or catalog.get("source_observation_sha256") != views._digest(observation)):
        return None, {**basis, "reason": "stale_region_catalog"}
    if task is None:
        return None, {**basis, "reason": "reviewed_task_missing"}
    assessment = assess(task, observation, ledger)
    if assessment["status"] != "ready":
        return None, {**basis, "reason": "current_task_not_ready:" + assessment["reason"]}
    active = next((i for i in task["intents"] if assessment["intents"].get(i["id"]) == "pending"), None)
    if active is None:
        return None, {**basis, "reason": "no_pending_intent"}
    basis.update(intent_id=active["id"], selector=deepcopy(active["selector"]))
    matches = matching(observation, active["selector"])
    if len(matches) != 1:
        return None, {**basis, "reason": "ambiguous_or_unbound_current_selector"}
    control = matches[0]
    if control["id"] not in observation.get("handles", {}):
        return None, {**basis, "reason": "current_target_not_addressable"}
    rid = catalog["memberships"].get(control["id"])
    region = next((r for r in catalog["regions"] if r["id"] == rid), None)
    if (region is None or region["kind"] in ("flat_controls", "unassigned_controls")
            or catalog["coverage"]["hierarchy_basis"] != "normalized_bound_parent_links"):
        return None, {**basis, "reason": "no_bound_structural_region"}
    try:
        region = revalidate_region(observation, region)
    except ValueError:
        return None, {**basis, "reason": "region_not_uniquely_stable"}
    first = views.inspect(observation, region["id"], limit=16)
    provenance = first["metadata"]["inspection_provenance"]
    included = set(provenance["primary_control_ids"]) | set(provenance["context_control_ids"])
    ordered = [c["id"] for c in observation["controls"] if c["id"] in included]
    if control["id"] not in provenance["primary_control_ids"] or ordered.count(control["id"]) != 1:
        return None, {**basis, "reason": "target_not_unique_primary_region_member"}
    offset = ordered.index(control["id"]) // 16 * 16
    cursor = (views._cursor(first["provenance"]["source_observation_sha256"],
                            first["coverage"]["scope"], offset, 16) if offset else None)
    page = views.inspect(observation, region["id"], limit=16, cursor=cursor) if cursor else first
    if not any(x.get("membership") == "primary" and x.get("control", {}).get("id") == control["id"]
               for x in page["items"]):
        return None, {**basis, "reason": "target_page_binding_failed"}
    return {"region": region, "cursor": cursor, "page": page}, {
        **basis, "route": "direct_inspect", "reason": "fresh_unique_pending_selector_and_stable_region",
        "control_id": control["id"], "region_id": region["id"],
        "region_semantic_signature": region["semantic_signature"],
        "region_membership_signature": region["membership_signature"],
        "page_offset": offset, "page_limit": 16,
        "uniqueness_scope": "captured_controls", "source_complete": observation.get("coverage", {}).get("complete"),
        "expected_values_used_for_routing": False, "all_regions_accessible": True}


def _region_context(region):
    """Preserve observed semantic identity even when its rows are on later pages."""
    return deepcopy({key: region[key] for key in
                     ("id", "kind", "label", "semantic_descriptor") if key in region})


def choose_with_tools(observation, candidates, decider, *, goal, history, emit,
                      interrupted, max_inspections=4, search_labels=(),
                      inspection_policy="model_led", reviewed_task=None, ledger=None):
    if inspection_policy not in INSPECTION_POLICIES:
        raise ValueError("Unsupported inspection policy")
    catalog = catalog_regions(observation)
    regions = {r["id"]: r for r in catalog["regions"]}
    mode, region, cursor, query = "overview", None, None, None
    local_history = list(history)
    prepared_page = None
    if inspection_policy == "reviewed_target_first":
        routed, basis = _reviewed_route(observation, reviewed_task, ledger or {}, catalog)
        emit({"type": "inspection_route", **basis})
        if routed:
            mode, region, cursor = "inspect", routed["region"], routed["cursor"]
            prepared_page = routed["page"]
    for attempt in range(max_inspections):
        view_started = time.monotonic()
        halt = interrupted()
        if halt:
            return {"stop": True, "status": halt["status"], "reason": halt["reason"]}
        if mode == "overview":
            page = views.overview(observation, limit=16, cursor=cursor)
            rows = page["items"]
            choices = [{"id": "inspect:" + x["region_id"], "description": "Inspect region " + x["label"]}
                       for x in rows]
            choices += [{"id": "search:" + str(n), "description": "Search all observed labels for " + json.dumps(label)}
                        for n, label in enumerate(search_labels)]
        elif mode == "search":
            page = views.search(observation, query, limit=16, cursor=cursor)
            rows = [{"role": x["control"]["role"], "name": x["control"].get("name"), "region_id": x["region_id"]} for x in page["items"]]
            ids = list(dict.fromkeys(x["region_id"] for x in rows))
            choices = [{"id": "inspect:" + rid, "description": "Inspect matching region " + regions[rid]["label"]} for rid in ids]
        else:
            page = prepared_page or views.inspect(observation, region["id"], limit=16, cursor=cursor)
            prepared_page = None
            rows, primary = [], set()
            for item in page["items"]:
                if item["kind"] != "control":
                    rows.append(item)
                    continue
                c = item["control"]
                rows.append({"id": c["id"], "role": c["role"], "name": c.get("name"), "value": c.get("value"),
                             "states": c.get("states"), "membership": item["membership"],
                             "parent": c.get("parent"), "actions": c.get("actions", []),
                             "semantics": {k: c.get("semantics", {}).get(k) for k in
                                           ("title", "description", "help", "identifier", "ancestors")},
                             "value_precision": c.get("value_evidence", {}).get("precision", "unknown")})
                if item["membership"] == "primary":
                    primary.add(c["id"])
            actions = [c for c in candidates if c.get("control_id") in primary or c["kind"] == "done"]
            choices = [{"id": c["id"], "description": c["description"].split("; observed=", 1)[0]
                        + ("; control=" + c["control_id"] if c.get("control_id") else "")} for c in actions]
            emit({"type": "region_detail", "region": region, "coverage": page["coverage"], "provenance": page["provenance"]})
        if page["coverage"]["continuation"]:
            choices.append({"id": "next_page", "description": "Inspect next page; remaining items: " + str(page["coverage"]["remaining_count"])})
        if inspection_policy == "reviewed_target_first" and mode == "inspect" and cursor is not None:
            choices.append({"id": "first_page", "description": "Inspect this region from its first page; earlier controls and context remain available"})
        if mode != "overview" or cursor is not None:
            choices.append({"id": "overview", "description": "Return to the first overview page; every region remains available"})
        coverage = {k: v for k, v in page["coverage"].items() if k != "continuation"}
        model_view = {"items": rows, "coverage": coverage,
                      "competitors_in_full_region": page.get("metadata", {}).get("competing_control_ids", []),
                      "page_is_not_global_uniqueness_or_absence_proof": True}
        if mode == "inspect":
            model_view["inspected_region"] = _region_context(page["metadata"]["region"])
        if inspection_policy == "reviewed_target_first":
            from .context import compact_reviewed_view
            model_view = compact_reviewed_view(model_view)
            summary = "UNTRUSTED UI " + mode + ":\n" + json.dumps(model_view, ensure_ascii=False, separators=(",", ":"))
        else:
            summary = "UNTRUSTED UI " + mode + ":\n" + json.dumps(model_view, ensure_ascii=False)
        emit({"type": "inspection_tool", "operation": mode, "attempt": attempt, "page": page,
              "view_wall_s": time.monotonic()-view_started})
        phase_goal = goal
        if mode in ("overview", "search"):
            phase_goal = ("IMMEDIATE TASK: Choose a READ-ONLY observation tool to locate the current outcome's control. "
                          "Inspecting a relevant region is progress. Desktop actions become available after inspection; "
                          "they are not expected in this tool list. Use the observed labels and region membership.\n\n"
                          + goal + "\nFor this decision, choose which observation tool to use next.")
        else:
            phase_goal += ("\nThis is an inspected UI region. Choose the current authorized outcome's desktop action, "
                           "or another observation tool if more context is needed.")
        request = deepcopy({"goal": phase_goal, "observation_summary": summary,
                            "candidates": choices, "history": local_history})
        context_limit = getattr(decider, "max_context_tokens", 8192)
        validate_request(**request, max_context_tokens=context_limit)
        request_sha256 = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        # Emit before invocation so failed/timed-out calls remain replayable too.
        # A separate copy prevents later history appends or trace consumers from
        # mutating the actual input. No expected selection enters this record.
        emit({"type": "decision_request", "schema": "locua.decision_request.v1", "phase": mode,
              "attempt": attempt, "request_sha256": request_sha256,
              "max_context_tokens": context_limit, "request": deepcopy(request),
              "inspection_policy": inspection_policy,
              "input_characters": {key: len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))
                                   for key, value in request.items()}})
        response = decider.choose(**request)
        emit({"type": "decision", "phase": mode, "candidate_count": len(choices),
              "request_sha256": request_sha256, "response": response,
              "inspection_policy": inspection_policy})
        halt = interrupted()
        if halt:
            return {"stop": True, "status": halt["status"], "reason": halt["reason"]}
        selected = response.get("selected_id")
        if response.get("abstained") or selected not in {c["id"] for c in choices}:
            return {"stop": True, "status": "abstained", "reason": "inspection_or_action_abstained"}
        if selected == "next_page":
            cursor = page["coverage"]["continuation"]
        elif selected == "first_page":
            cursor = None
        elif selected == "overview":
            mode, cursor = "overview", None
        elif selected.startswith("inspect:"):
            region = regions[selected[len("inspect:"):]]
            mode, cursor = "inspect", None
        elif selected.startswith("search:"):
            query = search_labels[int(selected[len("search:"):])]
            mode, cursor = "search", None
        else:
            action = next(c for c in actions if c["id"] == selected)
            return {"selected": action, "region": region}
        observed_region = ("; UNTRUSTED observed region " + json.dumps(_region_context(region), ensure_ascii=False)
                           if selected.startswith("inspect:") else "")
        local_history.append({"action": "Observation tool " + selected + observed_region,
                              "outcome": "Read-only inspection; no desktop effect or new task authority."})
    return {"stop": True, "status": "bounded_stop", "reason": "inspection_budget"}
