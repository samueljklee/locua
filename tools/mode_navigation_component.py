"""One operator-selected native navigation press, then read-only capture and stop.

This is a development component diagnostic, not a model trial or app workflow.
It never launches/activates apps, selects a resulting menu item, types, or saves.
Full private evidence is retained; summary stdout contains paths/identities only.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from locua.amplifier_tools import DesktopToolset
from locua.desktop_session_lock import acquire_desktop_session
from locua.engine.prototype.cli import private_json
from locua.lib import _config


def in_window_button(observation, name):
    """Choose one explicit label under this captured native window; no coordinates."""
    controls = {c["id"]: c for c in observation["controls"]}
    candidates = []
    for control in controls.values():
        if control.get("role") != "AXButton" or control.get("name") != name:
            continue
        node = control; visited = set()
        while node is not None and node["id"] not in visited:
            visited.add(node["id"])
            if node.get("role") == "AXWindow":
                handle = observation.get("handles", {}).get(node["id"], {})
                if (handle.get("kind") == "native"
                        and handle.get("pid") == observation["target"]["pid"]
                        and handle.get("window_id") == observation["target"]["window_id"]
                        and handle.get("snapshot_id") == observation["snapshot_id"]):
                    candidates.append(control)
                break
            node = controls.get(node.get("parent"))
    if len(candidates) != 1:
        raise ValueError("A unique observed named AXButton under the exact window is required")
    return candidates[0]


def run(out, *, app_name, control_name, config=None, target=None):
    root = Path(out).expanduser().absolute()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    started = time.monotonic()
    report = {"kind": "operator_navigation_component_diagnostic", "model_calls": 0,
              "app_name": app_name, "control_name": control_name,
              "maximum_press_attempts": 1, "press_attempts": 0,
              "application_left_open": True, "mode_selection_performed": False,
              "task_completion_claim": False, "reviews": []}
    request = ("Inspect the existing " + app_name + " window, then press its observed in-window "
               + control_name + " button once. Capture the result and stop without selecting anything else.")
    toolset = None

    def approve(prompt, purpose):
        if purpose != "tool_scope_review" or report["reviews"]:
            raise ValueError("Only the single preauthorized navigation review is permitted")
        report["reviews"].append({"purpose": purpose, "prompt": prompt, "answer": "run",
                                  "actor": "explicitly authorized component runner; no model"})
        return "run"

    try:
        with acquire_desktop_session(purpose="Locua one-press native navigation component"):
            report["desktop_lease_acquired"] = True
            try:
                toolset = DesktopToolset(_config(config), root / "desktop", request, approve)

                def call(name, **arguments):
                    result = toolset.call("locua_" + name, arguments)
                    if result.get("status") in ("refused", "unavailable", "uncertain", "canceled"):
                        raise ValueError(name + ": " + str(result.get("reason", result.get("code"))))
                    return result

                apps = call("apps", query=app_name)["items"]
                apps = [a for a in apps if a.get("name") == app_name]
                if len(apps) != 1:
                    raise ValueError("Exact installed application name is absent or ambiguous")
                windows = call("windows", app_id=apps[0]["app_id"])["windows"]
                choices = [w for w in windows if w.get("identity_proven") is True
                           and w.get("is_on_screen") is True
                           and (target is None or w.get("target") == target)]
                if len(choices) != 1:
                    raise ValueError("One existing visible, app-owned exact window is required; no launch/activation fallback")
                window = choices[0]; report["window"] = deepcopy(window)
                observed = call("observe", window_id=window["window_id"])
                snapshot = observed["snapshot_id"]
                raw = toolset._observations[snapshot]
                selected = in_window_button(raw, control_name)
                call("inspect", snapshot_id=snapshot, operation="control", control_id=selected["id"])
                actions = [a for a in toolset._actions[snapshot].values()
                           if a["control_id"] == selected["id"] and a["kind"] == "press"]
                if len(actions) != 1:
                    raise ValueError("Unique observed press capability is unavailable")
                report["before_snapshot_id"] = snapshot
                report["selected_control"] = {k: deepcopy(selected.get(k)) for k in
                                               ("id", "role", "name", "parent", "semantics")}
                purpose = "Open only the observed navigation control; inspect the resulting UI without selecting a mode"
                review = call("review", snapshot_id=snapshot, summary=purpose, goals=[],
                              effects=[{"kind": "press", "control_id": selected["id"], "purpose": purpose}],
                              covers_entire_request=False)
                report["scope_id"] = review["scope_id"]
                report["press_attempts"] = 1
                action = toolset.call("locua_act", {"scope_id": review["scope_id"],
                                     "snapshot_id": snapshot, "action_id": actions[0]["id"]})
                private_json(root / "press-result.json", action)
                report["press_status"] = action.get("status")
                report["action_started"] = action.get("action_started")
                report["driver_ack"] = deepcopy(action.get("driver_ack"))
                report["immediate_post_snapshot_id"] = action.get("snapshot_id")
                # Read-only reconciliation is permitted after any outcome. There
                # is no repeat press, foreground fallback, menu choice or typing.
                captured = toolset.call("locua_observe", {"window_id": window["window_id"]})
                private_json(root / "fresh-capture-result.json", captured)
                report["capture_status"] = captured.get("status")
                if captured.get("status") == "observed":
                    sid = captured["snapshot_id"]; report["after_snapshot_id"] = sid
                    state = toolset._observations[sid]
                    private_json(root / "after-observation.json", state)
                    private_json(root / "after-actions.json", list(toolset._actions[sid].values()))
                    # A bounded first compact page is a view, not a complete list.
                    # Full observation/actions above retain every captured control.
                    listing = toolset.call("locua_inspect", {"snapshot_id": sid, "operation": "list", "limit": 128})
                    private_json(root / "after-compact-list.json", listing)
                    report["compact_listing_continuation"] = listing.get("coverage", {}).get("continuation")
                report["status"] = ("captured_after_single_press" if action.get("status") in ("dispatched", "verified")
                                    and captured.get("status") == "observed" else "stopped_unproved")
                report["next"] = "Operator inspects private artifacts. No automatic next step or mode selection."
            finally:
                if toolset is not None:
                    report["cleanup"] = toolset.close()
        report["desktop_lease_released"] = True
    except BaseException as error:
        report.update(status="stopped", error_type=type(error).__name__, error=str(error))
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        report["wall_s"] = time.monotonic() - started
        report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        private_json(root / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config")
    parser.add_argument("--app", default="Calculator")
    parser.add_argument("--control", default="Mode")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--native-window-id", type=int)
    parser.add_argument("--execute-one-press", action="store_true",
                        help="Explicitly run this operator diagnostic; no model is involved.")
    args = parser.parse_args(argv)
    if not args.execute_one_press:
        parser.error("--execute-one-press is required; no desktop calls were made")
    if (args.pid is None) != (args.native_window_id is None):
        parser.error("Supply both --pid and --native-window-id, or neither")
    target = None if args.pid is None else {"pid": args.pid, "window_id": args.native_window_id}
    report = run(args.out, app_name=args.app, control_name=args.control, config=args.config, target=target)
    print(json.dumps({k: report.get(k) for k in ("status", "press_attempts", "press_status", "capture_status", "after_snapshot_id")} |
                     {"summary": str(Path(args.out).expanduser().absolute() / "summary.json")}, indent=2))
    return 0 if report["status"] == "captured_after_single_press" else 1


if __name__ == "__main__":
    raise SystemExit(main())
