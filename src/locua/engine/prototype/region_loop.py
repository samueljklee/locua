"""Intent-directed inspection using the unchanged local RLCD choose interface."""
from __future__ import annotations

import json

from .regions import catalog_regions, project_overview, inspect_region, region_candidates


def choose_in_region(observation, candidates, decider, *, goal, history, emit,
                     interrupted, max_inspections=4):
    catalog = catalog_regions(observation)
    overview = project_overview(observation, catalog)
    local_history = list(history)
    for attempt in range(max_inspections):
        halt = interrupted()
        if halt:
            return {"stop": True, "status": halt["status"], "reason": halt["reason"]}
        emit({"type": "region_overview", "attempt": attempt, **overview})
        response = decider.choose(goal=goal + "\nChoose which UI region to inspect next.",
            observation_summary=overview["observation_summary"], candidates=overview["candidates"], history=local_history)
        emit({"type": "decision", "phase": "region", "candidate_count": len(overview["candidates"]), "response": response})
        selected = next((r for r in catalog["regions"] if "inspect:" + r["id"] == response.get("selected_id")), None)
        if selected is None or response.get("abstained"):
            return {"stop": True, "status": "abstained", "reason": "region_selection_abstained_or_unknown"}
        halt = interrupted()
        if halt:
            return {"stop": True, "status": halt["status"], "reason": halt["reason"]}
        inspection = inspect_region(observation, selected, catalog)
        scoped = region_candidates(candidates, inspection)
        actions = scoped["candidates"]
        if observation["kind"] == "native_window_state":
            from .context import project_native_context
            projection = project_native_context(inspection["observation"], actions)
            summary, choices = projection["observation_summary"], projection["candidates"]
        else:
            # Every inspected control and competing target survives. Snapshot
            # IDs/transport arguments are omitted only from model presentation.
            controls = inspection["observation"]["controls"]
            aliases = {c["id"]: n for n, c in enumerate(controls)}
            rows = [{"row": aliases[c["id"]], "role": c["role"], "name": c.get("name"),
                "value": c.get("value"), "value_evidence": c.get("value_evidence"),
                "states": c.get("states"), "parent": aliases.get(c.get("parent")),
                "in_selected_region": c["id"] in inspection["primary_control_ids"]} for c in controls]
            summary = "UNTRUSTED UI detail (display values are not exact editor/document values):\n" + json.dumps(rows, ensure_ascii=False)
            summary += "\nUnbound/context-only source text:\n" + json.dumps(inspection["model_context_text"], ensure_ascii=False)
            choices = [{"id": c["id"], "description": c["description"].split("; observed=", 1)[0]
                        + ("; row=" + str(aliases[c["control_id"]]) if "control_id" in c else "")}
                       for c in actions]
        choices.append({"id": "inspect_other", "description": "Return to overview and inspect another UI region; no desktop action."})
        emit({"type": "region_detail", "region": selected, "provenance": inspection["provenance"],
              "coverage": scoped["coverage"], "observation_summary": summary, "candidates": choices})
        response = decider.choose(goal=goal, observation_summary=summary, candidates=choices, history=local_history)
        emit({"type": "decision", "phase": "action", "candidate_count": len(choices), "response": response})
        halt = interrupted()
        if halt:
            return {"stop": True, "status": halt["status"], "reason": halt["reason"]}
        if response.get("selected_id") == "inspect_other" and not response.get("abstained"):
            local_history.append({"action": "Inspected region " + str(selected["label"]),
                                  "outcome": "Chose to inspect another region; no desktop action occurred."})
            continue
        action = next((c for c in actions if c["id"] == response.get("selected_id")), None)
        if action is None or response.get("abstained"):
            return {"stop": True, "status": "abstained", "reason": "action_selection_abstained_or_unknown"}
        return {"selected": action, "region": selected}
    return {"stop": True, "status": "bounded_stop", "reason": "inspection_budget"}
