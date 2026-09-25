"""Experimental structured-intent lab CLI; preview by default, no free-text parser."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .core import run_loop, validate_task
from .cua import CuaOwner, CuaAdapter, CuaRefusal, PrivateTrace
from .decision import LAB, ModelService

TASK_KEYS = {"id", "goal", "target", "intents", "invariants"}


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Canceled before another operation")


def loopback_url(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("Browser URL must be a literal loopback HTTP(S) URL")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or parts.hostname not in ("localhost", "127.0.0.1")
            or parts.username is not None or parts.password is not None):
        raise ValueError("Browser mode permits only http(s)://localhost or 127.0.0.1 without credentials")
    _ = parts.port  # Reject malformed/out-of-range ports before creating anything.
    return url


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON task key: " + key)
        value[key] = item
    return value


def load_task(path, url=None):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Task JSON exceeds 1 MiB")
    content = path.read_bytes()
    if len(content) > 1024 * 1024:
        raise ValueError("Task JSON exceeds 1 MiB")
    def reject_constant(value):
        raise ValueError("Non-finite JSON value: " + value)
    task = json.loads(content, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    if not isinstance(task, dict) or set(task) - TASK_KEYS:
        raise ValueError("Task accepts only id, goal, target, intents and invariants")
    if url is not None:
        loopback_url(url)
        if task.get("target") not in (None, {}):
            raise ValueError("Browser task target must be empty/absent; foreign handles cannot be adopted")
        validated = {**deepcopy(task), "target": {"target_id": "new-owned-browser", "tab_id": "new-owned-tab"}}
        validate_task(validated)  # Syntax only, before any model/GUI setup.
    else:
        target = task.get("target")
        if not isinstance(target, dict) or set(target) - {"pid", "window_id", "session"}:
            raise ValueError("Native target requires exact pid/window_id and optional session")
        if any(type(target.get(k)) is not int or target[k] <= 0 for k in ("pid", "window_id")):
            raise ValueError("Native pid/window_id must be positive integers")
        if "session" in target and (not isinstance(target["session"], str) or not target["session"].strip()):
            raise ValueError("Native session, when supplied, must be a nonempty name")
        validate_task(task)
    return task, hashlib.sha256(content).hexdigest()


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def owned_visible_window_candidates(windows, pid):
    """Discover visible windows of a freshly launched PID, not browser bindings.

    Titles, app names and positions are not authority. The declared discovery
    scope excludes off-screen windows, nonzero layers and tiny surfaces. Every
    exclusion is reported; unknown visibility on a plausible surface refuses.
    Exact driver/CDP ownership must still be established separately.
    """
    if not isinstance(windows, list):
        raise RuntimeError("Owned-PID window list is malformed")
    candidates, exclusions, seen = [], [], set()
    for window in windows:
        if not isinstance(window, dict) or type(window.get("pid")) is not int or window["pid"] != pid:
            raise RuntimeError("Owned-PID window listing included an unexpected process")
        wid = window.get("window_id")
        if type(wid) is not int or wid <= 0 or wid in seen:
            raise RuntimeError("Owned-PID window identity is invalid or duplicated")
        seen.add(wid)
        layer, bounds = window.get("layer"), window.get("bounds")
        if type(layer) is not int or not isinstance(bounds, dict):
            raise RuntimeError("Owned window layer or bounds are unknown")
        size = [bounds.get("width"), bounds.get("height")]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in size):
            raise RuntimeError("Owned window dimensions are invalid or unknown")
        if layer != 0:
            reason = "outside_layer_zero_discovery_scope"
        elif any(v < 100 for v in size):
            reason = "below_100_pixel_discovery_size"
        elif type(window.get("is_on_screen")) is not bool:
            raise RuntimeError("Plausible owned window visibility is unknown; no fallback")
        elif not window["is_on_screen"]:
            reason = "explicitly_off_screen_outside_visible_discovery_scope"
        else:
            candidates.append(window)
            continue
        exclusions.append({"window_id": wid, "reason": reason,
                           "is_on_screen": window.get("is_on_screen"),
                           "layer": layer, "width": size[0], "height": size[1]})
    return candidates, {"scope": "prepared_pid_on_screen_layer_zero_at_least_100_by_100",
                        "pid": pid, "observed_window_ids": [w["window_id"] for w in windows],
                        "candidate_window_ids": [w["window_id"] for w in candidates],
                        "excluded_windows": exclusions, "exclusions_prove_auxiliary": False,
                        "title_or_app_name_used": False}


def prepare_owned_browser(owner, session, url, cancel, state):
    """Create a new isolated owner, retaining PID for cleanup on partial failure."""
    check_cancel(cancel)
    owner.start_session(session)
    state["session_started"] = True
    check_cancel(cancel)
    _, prepared = owner.call("browser_prepare", {"session": session, "profile": {"mode": "isolated_new"}, "allow_launch": True})
    if not prepared.get("prepared") or prepared.get("action") != "launched_isolated_browser":
        raise RuntimeError("A new isolated browser was not established")
    pid = prepared.get("prepared_pid")
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("Owned browser PID is unavailable for verified cleanup")
    state["owned_browser_pid"] = pid
    window = None
    state["browser_startup_window_samples"] = []
    for _ in range(20):
        check_cancel(cancel)
        _, windows = owner.call("list_windows", {"pid": pid})
        candidates, evidence = owned_visible_window_candidates(windows.get("windows"), pid)
        state["browser_startup_window_samples"].append(evidence)
        trace = getattr(owner, "trace", None)
        if callable(trace):
            trace({"type": "browser_window_discovery", **evidence})
        # Chromium may briefly expose a startup surface beside its page window.
        # Wait within the same bounded read-only discovery budget; never choose
        # among competitors using titles, size ordering, or the intended URL.
        if len(candidates) == 1:
            window = candidates[0]
            break
        cancel.wait(.3)
    if window is None:
        if len(candidates) > 1:
            raise RuntimeError("Multiple plausible visible owned windows remain ambiguous after startup; no first-match binding")
        raise RuntimeError("Unique driver-owned blank window unavailable")
    # Only the initial read-only bind can retry explicit endpoint-not-ready.
    # Every attempt re-runs driver attestation for the identical prepared owner;
    # there is no new launch, window selection, cached endpoint or mutation retry.
    bind_args = {"pid": pid, "window_id": window["window_id"], "session": session}
    state["browser_startup_binding_attempts"] = []
    retry_delays = (.1, .25)
    for attempt in range(1, 4):
        check_cancel(cancel)
        record = {"attempt": attempt, "request": deepcopy(bind_args), "status": "started"}
        state["browser_startup_binding_attempts"].append(record)
        try:
            _, bound = owner.call("get_browser_state", deepcopy(bind_args))
        except CuaRefusal as error:
            record.update(status="refused", refusal_code=error.code)
            if error.code != "browser_requires_setup" or attempt == 3:
                raise
            delay = retry_delays[attempt - 1]
            record["retry_wait_s"] = delay
            trace = getattr(owner, "trace", None)
            if callable(trace):
                trace({"type": "browser_binding_retry", "request": deepcopy(bind_args),
                       "failed_attempt": attempt, "next_attempt": attempt + 1,
                       "max_attempts": 3, "refusal_code": error.code, "wait_s": delay,
                       "scope": "initial_read_only_bind_before_navigation"})
            check_cancel(cancel)
            cancel.wait(delay)
            check_cancel(cancel)
        else:
            record["status"] = "returned"
            break
    tabs = bound.get("tabs")
    if not (bound.get("binding_quality") == "exact" and bound.get("endpoint_access_class") == "driver_owned"
            and bound.get("mutation_allowed") is True and isinstance(tabs, list) and len(tabs) == 1
            and isinstance(tabs[0], dict) and tabs[0].get("url") == "about:blank"):
        raise RuntimeError("Browser target lacks exact owned binding to one about:blank tab")
    target = {"target_id": bound.get("target_id"), "tab_id": tabs[0].get("tab_id"), "session": session}
    if any(not isinstance(v, str) or not v for v in target.values()):
        raise RuntimeError("Owned browser target identity is incomplete")
    state["owned_browser_window_id"] = window["window_id"]
    state["browser_binding_basis"] = "exact_driver_owned_CDP_binding_and_single_about_blank_tab"
    check_cancel(cancel)
    owner.call("browser_navigate", {**target, "url": url})
    return target


def run(args, cancel=None):
    wall_started = time.monotonic()
    cancel = cancel or threading.Event()
    if args.ocr and args.url is not None:
        raise ValueError("--ocr is currently native-only")
    if not 1 <= args.max_steps <= 32 or not 0 < args.max_seconds <= 600:
        raise ValueError("Loop bounds require 1..32 steps and 0..600 seconds")
    task, task_sha = load_task(args.task, args.url)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(out, 0o700)
    report = {"version": "locua-lab-cli-v1", "mode": "execute" if args.execute else "preview",
        "model_key": args.model, "task_id": task["id"], "task_input_sha256": task_sha,
        "artifacts": str(out), "status": "starting", "cleanup": {"errors": []},
        "limits": {"max_steps": args.max_steps, "max_seconds": args.max_seconds},
        "structured_intents_supplied_by_caller": True,
        "natural_language_interpretation_implemented": False,
        "regions": getattr(args, "regions", False)}
    source_hashes = {str(p.relative_to(LAB)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted((LAB / "prototype").glob("*.py"))}
    private_json(out / "source-hashes.json", source_hashes)
    events = PrivateTrace(out / "loop.jsonl")
    worker, owner, browser_state = None, None, {}
    def progress(message):
        if getattr(args, "progress", False):
            print(f"[{time.monotonic() - wall_started:7.2f}s] {message}", file=sys.stderr, flush=True)
    def event_trace(event):
        events(event)
        kind = event.get("type")
        if kind == "region_detail":
            progress("Inspecting region: " + str(event["region"]["label"]))
        elif kind == "decision":
            progress("Local RLCD " + event.get("phase", "action") + " decision received")
        elif kind == "dispatch":
            progress("Dispatched intent: " + event["intent_id"])
        elif kind == "assessment":
            progress("Task " + event["status"] + ": " + event["reason"])
    try:
        check_cancel(cancel)
        progress("Loading local " + ("RLCD baseline 1.5B" if args.model == "baseline" else "experimental 7B RLCD comparator"))
        worker = ModelService(model=args.model, timeout_s=60)
        report["model_info"] = worker.info()
        report["model_load_wall_s"] = time.monotonic() - wall_started
        check_cancel(cancel)
        owner = CuaOwner(out / "cua", max_seconds=min(600, args.max_seconds + 90))
        check_cancel(cancel)
        if args.url is not None:
            session = "locua-cli-" + uuid.uuid4().hex[:16]
            browser_state = {"session": session, "session_started": False}
            target = prepare_owned_browser(owner, session, args.url, cancel, browser_state)
            task = {**deepcopy(task), "target": target}
            kind = "browser_semantic_v2"
        else:
            target = deepcopy(task["target"])
            if "session" in target:
                owner.start_session(target["session"])
            kind = "native_window_state"
        validate_task(task)
        private_json(out / "bound-task.json", task)
        adapter = CuaAdapter(owner, kind=kind, target=target, expected_url=args.url,
                             execute=args.execute, browser_click_route=args.browser_click_route, ocr=args.ocr,
                             **({"regions": True} if getattr(args, "regions", False) else {}))
        check_cancel(cancel)
        report["setup_wall_s"] = time.monotonic() - wall_started
        progress("Starting bounded observation/decision/action loop")
        result = run_loop(task, adapter, worker, cancel=cancel, max_steps=args.max_steps,
                          max_seconds=args.max_seconds, trace=event_trace, execute=args.execute,
                          regions=getattr(args, "regions", False))
        private_json(out / "result.json", result)
        report.update(status=result["status"], reason=result["reason"],
            decisions=sum(e["type"] == "decision" for e in result["events"]),
            dispatches=sum(e["type"] == "dispatch" for e in result["events"]))
        report["loop_wall_s"] = result["wall_s"]
        report["verification"] = {k: result[k] for k in ("completion_basis", "text_verification", "committed_document_proven", "saved_output_proven")}
        report["model_final_info"] = worker.info()
    except (Cancelled, KeyboardInterrupt):
        cancel.set()
        report.update(status="canceled", reason="interrupt_before_further_dispatch")
    except Exception as exc:
        report.update(status="failed", reason=type(exc).__name__ + ": " + str(exc))
    finally:
        errors = report["cleanup"]["errors"]
        if owner is not None:
            if browser_state.get("session_started"):
                try:
                    owner.end_session(browser_state["session"])
                    report["cleanup"]["browser_session_ended"] = True
                except Exception as exc:
                    errors.append("browser session end: " + repr(exc))
            if browser_state.get("owned_browser_pid"):
                try:
                    _, windows = owner.call("list_windows", {"pid": browser_state["owned_browser_pid"]}, cleanup=True)
                    report["cleanup"]["owned_browser_pid"] = browser_state["owned_browser_pid"]
                    report["cleanup"]["owned_browser_windows_remaining"] = len(windows["windows"])
                    if windows["windows"]:
                        errors.append("Owned browser still has windows after session cleanup")
                except Exception as exc:
                    errors.append("owned browser closure verification: " + repr(exc))
            try:
                errors.extend(owner.close())
                report["cleanup"]["owner_closed"] = True
            except Exception as exc:
                errors.append("owner close: " + repr(exc))
                report["cleanup"]["owner_closed"] = False
        if worker is not None:
            try:
                worker.close()
                report["cleanup"]["worker_exited"] = worker.process.poll() is not None
                if not report["cleanup"]["worker_exited"]:
                    errors.append("Model worker remains running")
            except Exception as exc:
                errors.append("worker close: " + repr(exc))
        if errors:
            report["result_status_before_cleanup_failure"] = report["status"]
            report["status"] = "cleanup_failed"
        events({"type": "cli_cleanup", **report["cleanup"]})
        events.close()
        report["end_to_end_wall_s"] = time.monotonic() - wall_started
        progress("Finished: " + report["status"] + "; artifacts " + str(out))
        private_json(out / "summary.json", report)
    code = 130 if report["status"] == "canceled" else 0 if report["status"] in ("complete", "proposed") else 2
    return code, report


def parser():
    p = argparse.ArgumentParser(prog="python -m prototype", description=__doc__)
    p.add_argument("--task", type=Path, required=True, help="JSON with explicit intents/invariants, not free-text parsing")
    p.add_argument("--out", type=Path, required=True, help="NEW private artifact directory; never overwritten")
    p.add_argument("--model", choices=("baseline", "comparator"), default="baseline")
    p.add_argument("--execute", action="store_true", help="Dispatch only fresh, scope-checked proposals; default is preview")
    p.add_argument("--url", help="Browser mode: localhost/127.0.0.1 only, new isolated browser, task target empty/absent")
    p.add_argument("--ocr", action="store_true", help="Native-only local OCR evidence; never creates pixel actions")
    p.add_argument("--regions", action="store_true", help="Model chooses a structural region, then an action; all other regions remain discoverable")
    p.add_argument("--progress", action="store_true", help="Print live local progress and elapsed time to stderr")
    p.add_argument("--browser-click-route", choices=("trusted", "dom_event"), default="trusted",
                   help="DOM events require explicit opt-in; no automatic fallback")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--max-seconds", type=float, default=180)
    return p

