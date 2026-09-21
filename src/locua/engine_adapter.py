"""Public-library adapter for the bundled, guarded local engine.

No dependency is discovered from a developer checkout. The configured Python
loads only pinned local weights; the model never receives a dispatch capability.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .errors import LocuaError

_EXECUTION_LOCK = threading.RLock()


def require(config, fields):
    missing = [key for key in fields if not config.get(key)]
    if missing:
        raise LocuaError("runtime_configuration_missing", "Missing configuration: " + ", ".join(missing),
                         "Select an existing configuration with --config /path/to/locua.json or LOCUA_CONFIG. Otherwise run locua setup --help to configure the named local paths, then locua doctor.")


@contextmanager
def runtime_environment(config):
    require(config, ("runtime_python", "model_cache"))
    if sys.platform != "darwin":
        raise LocuaError("unsupported_inference_platform", "The pinned local MLX backend currently requires Apple silicon macOS.",
                         "Windows/Linux inference and driver backends have not been implemented; no online fallback is used.")
    names = {"LOCUA_RUNTIME_PYTHON": config["runtime_python"], "LOCUA_MODEL_CACHE": config["model_cache"]}
    old = {key: os.environ.get(key) for key in names}
    os.environ.update(names)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def artifact_directory(value, operation):
    from .config import default_path
    out = Path(value).expanduser().resolve() if value else default_path().parent / "runs" / (operation + "-" + uuid.uuid4().hex[:12])
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(out, 0o700)
    return out


def owner(config, out):
    from .engine.prototype.cua import CuaOwner, CuaTransport
    require(config, ("driver_binary", "driver_socket"))
    from .desktop_session_lock import acquire_desktop_session, current_desktop_session_token
    lease = acquire_desktop_session(purpose="Locua Cua owner", token=current_desktop_session_token())
    try:
        connection = CuaOwner(out / "cua", max_seconds=600, transport_factory=lambda **kw: CuaTransport(
            binary_path=config["driver_binary"], socket_path=config["driver_socket"], **kw))
        original_close = connection.close

        def close_with_lease():
            try:
                return original_close()
            finally:
                lease.close()

        # Retain the CuaOwner object/API, including its context-manager close.
        # Standalone acquisition does not create an implicit nesting context.
        connection.close = close_with_lease
        return connection
    except BaseException:
        lease.close()
        raise


@contextmanager
def observed_owner(config, out):
    """Read-only operations must also surface transport/session cleanup failures."""
    connection = owner(config, out)
    try:
        yield connection
    finally:
        errors = connection.close()
        if errors:
            raise LocuaError("cleanup_failed", "Driver cleanup was not verified: " + "; ".join(map(str, errors)),
                             "Inspect the local trace and reconcile owned sessions before another run.", exit_code=5)


def _acquire(payload, config, out):
    from .engine.prototype.cua import CuaAdapter
    target = payload["target"]
    if not isinstance(target, dict) or not target:
        raise ValueError("An exact target object is required")
    kind = {"native": "native_window_state", "browser": "browser_semantic_v2"}.get(payload["kind"])
    if kind is None:
        raise ValueError("Live observe requires kind native or browser")
    with observed_owner(config, out) as connection:
        return CuaAdapter(connection, kind=kind, target=target, regions=True).observe()


def _observe(payload, config, progress):
    from .engine.prototype import observation_tools as views
    from .engine.prototype.cli import private_json
    out = artifact_directory(payload.get("out"), "observe")
    observation = payload.get("snapshot")
    if observation is None:
        progress("Reading the explicitly scoped Cua target")
        observation = _acquire(payload, config, out)
    private_json(out / "observation.json", observation)
    kind = payload.get("view", "overview")
    page = {key: payload[key] for key in ("cursor", "limit") if payload.get(key) is not None}
    if kind == "full":
        if page:
            raise ValueError("full does not accept pagination options")
        view = observation
    elif kind == "overview":
        view = views.overview(observation, **page)
    elif kind == "inspect":
        if not payload.get("region_id"):
            raise ValueError("inspect requires region_id from the overview of this saved snapshot")
        view = views.inspect(observation, payload["region_id"], **page)
    elif kind == "search":
        if not isinstance(payload.get("query"), str) or not payload["query"]:
            raise ValueError("search requires a nonempty query")
        view = views.search(observation, payload["query"], region_id=payload.get("region_id"), **page)
    else:
        raise ValueError("Unsupported observation view")
    private_json(out / "view.json", view)
    return {"status": "observed", "artifacts": str(out), "view": view,
            "observation_file": str(out / "observation.json"), "desktop_mutated": False}


def _targets(payload, config, progress):
    from .engine.prototype.cli import private_json
    require(config, ("driver_binary", "driver_socket"))
    out = artifact_directory(payload.get("out"), "targets")
    args = {}
    if payload.get("pid") is not None:
        if type(payload["pid"]) is not int or payload["pid"] <= 0:
            raise ValueError("pid must be a positive integer")
        args["pid"] = payload["pid"]
    with observed_owner(config, out) as connection:
        _, value = connection.call("list_windows", args)
    private_json(out / "targets.json", value)
    return {"status": "observed", "artifacts": str(out), "targets": value,
            "desktop_mutated": False, "instructions": "Use an exact pid/window_id pair as native scope. Browser run creates its own isolated window at the supplied URL."}


def _prepare_guided(payload, config, progress):
    """Read-only discovery; owned browser cleanup precedes human review."""
    from .engine.prototype.cua import CuaAdapter
    from .engine.prototype.cli import prepare_owned_browser, private_json
    out = artifact_directory(payload.get("out"), "discovery")
    if payload.get("document"):
        from .document_open import open_document
        return open_document(payload["document"], config, out, progress)
    browser = {}; connection = owner(config, out)
    try:
        if payload.get("url"):
            url = _browser_url(payload["url"])
            scope = {"kind": "browser", "url": url}
            progress("Opening the requested URL for read-only field discovery: " + url)
            session = "locua-review-" + uuid.uuid4().hex[:12]
            target = prepare_owned_browser(connection, session, url, threading.Event(), browser)
            driver = CuaAdapter(connection, kind="browser_semantic_v2", target=target,
                                expected_url=url, regions=True,
                                browser_click_route=payload.get("browser_click_route", "trusted"))
            label = url
        else:
            target = payload["target"]
            if not isinstance(target, dict) or set(target) != {"pid", "window_id"} or any(type(x) is not int or x <= 0 for x in target.values()):
                raise ValueError("Choose an exact observed native window")
            scope = {"kind": "native", **target}
            driver = CuaAdapter(connection, kind="native_window_state", target=target, regions=True)
            label = "Native window " + str(target["window_id"]) + " in process " + str(target["pid"])
        observation = driver.observe()
        private_json(out / "observation.json", observation)
        return {"status": "observed", "scope": scope, "target_label": label,
                "observation": observation, "artifacts": str(out), "desktop_edited": False}
    finally:
        errors = []
        if browser.get("session_started"):
            try:
                connection.end_session(session)
                if browser.get("owned_browser_pid"):
                    _, windows = connection.call("list_windows", {"pid": browser["owned_browser_pid"]}, cleanup=True)
                    if windows["windows"]:
                        errors.append("Owned discovery browser windows remain")
            except Exception as error:
                errors.append(str(error))
        errors.extend(connection.close())
        private_json(out / "cleanup.json", {"errors": errors})
        if errors:
            raise ValueError("Discovery cleanup failed: " + "; ".join(errors))


def _browser_url(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("Scope needs a literal HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Browser scope needs a literal HTTP(S) URL without embedded credentials")
    _ = parsed.port
    return url


def _run(payload, config, progress, *, fixture=None, reusable_selector=None):
    from .engine.prototype.architecture import generate_plan, execute_plan, TimedDriver
    from .engine.prototype.cli import private_json, prepare_owned_browser, Cancelled
    from .engine.prototype.planning_contracts import validate_plan, ground_plan
    from .engine.prototype.core import assess
    from .engine.prototype.cua import CuaAdapter
    from .engine.prototype.decision import ModelService
    from .engine.prototype.simulation import SimulatedDriver

    started = time.monotonic()
    out = artifact_directory(payload.get("out"), "run")
    report = {"status": "starting", "artifacts": str(out), "inference_local_only": True,
              "model": payload.get("model", "baseline"), "selector_decoding": "original RLCD",
              "inspection_policy": payload.get("inspection_policy", "model_led"),
              "browser_click_route": payload.get("browser_click_route", "trusted"),
              "driver": "simulated" if fixture is not None else "cua", "cleanup_errors": [],
              "natural_language_autonomy_proven": False, "committed_document_proven": False, "saved_output_proven": False}
    cancel, connection, selector, browser = threading.Event(), None, reusable_selector, {}
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        with runtime_environment(config):
            if payload.get("request") is not None:
                report.update(driver=None, desktop_mutated=False, selector_decoding=None,
                              planner_decoding="compact_greedy", interpretation_requires_review=True)
                if payload.get("execute"):
                    raise ValueError("Natural-language interpretation currently requires plan review. Run without --execute, review plan.json, then run --task plan.json --execute.")
                plan, planning = generate_plan(payload["request"], payload["scope"], payload["model"], out, progress,
                                                supplied_data=payload.get("supplied_data"))
                report.update(status="needs_clarification" if plan["unknowns"] else "plan_ready_for_review",
                              planner=planning, plan=plan, plan_file=str(out / "plan.json"), driver=None,
                              desktop_mutated=False, interpretation_requires_review=True)
                return report
            plan = deepcopy(payload["task"])
            validate_plan(plan, supplied_data=payload.get("supplied_data"))
            private_json(out / "plan.json", plan)
            if plan["unknowns"]:
                raise ValueError("Resolve plan questions before execution: " + "; ".join(plan["unknowns"]))
            if any(item["evidence_plane"] in ("committed_document", "saved_output") for item in plan["outcomes"]):
                raise ValueError("This UI executor verifies display/editor_buffer only; committed/saved execution requires a dedicated document adapter")
            scope = plan["scope"]
            if fixture is not None:
                if scope != {"kind": "simulation", "case": fixture["id"]}:
                    raise ValueError("Simulation scope must match the supplied case identity")
                driver = SimulatedDriver(fixture)
            else:
                if scope["kind"] not in ("browser", "native"):
                    raise ValueError("A live run requires an explicit browser or native scope")
                if scope["kind"] == "browser":
                    _browser_url(scope["url"])
                connection = owner(config, out)
                if scope["kind"] == "browser":
                    browser["session"] = "locua-" + uuid.uuid4().hex[:12]
                    target = prepare_owned_browser(connection, browser["session"], scope["url"], cancel, browser)
                    driver = CuaAdapter(connection, kind="browser_semantic_v2", target=target,
                        expected_url=scope["url"], execute=payload["execute"], regions=True,
                        browser_click_route=payload.get("browser_click_route", "trusted"))
                else:
                    target = {key: scope[key] for key in ("pid", "window_id")}
                    driver = CuaAdapter(connection, kind="native_window_state", target=target,
                                        execute=payload["execute"], regions=True)
            driver = TimedDriver(driver)
            observation = driver.observe()
            private_json(out / "preflight-observation.json", observation)
            task, _ = ground_plan(plan, observation, supplied_data=payload.get("supplied_data"))
            assessment = assess(task, observation)
            report["preflight"] = assessment
            if assessment["status"] not in ("ready", "complete"):
                raise ValueError("preflight:" + assessment["reason"])
            if cancel.is_set():
                raise InterruptedError("Canceled before model load")
            load = time.monotonic()
            if selector is None and assessment["status"] != "complete":
                progress("Loading " + ("original 1.5B RLCD baseline" if payload["model"] == "baseline" else "experimental 7B RLCD comparator"))
                selector = ModelService(model=payload["model"])
            report["selector_load_wall_s"] = time.monotonic() - load
            if selector is not None:
                report["selector_info"] = selector.info()
            result = execute_plan(plan, driver, selector, out=out, cancel=cancel, progress=progress,
                                  supplied_data=payload.get("supplied_data"), execute=payload["execute"],
                                  inspection_policy=payload.get("inspection_policy", "model_led"))
            responses = [event["response"] for event in result["events"] if event["type"] == "decision"]
            report.update(status=result["status"], reason=result["reason"], loop_wall_s=result["wall_s"],
                          model_usage=result.get("model_usage"),
                          dispatches=driver.timing["dispatch_calls"], issued_actions=len(result["ledger"]),
                          decisions=len(responses), evidence_file=str(out / "task-state.json"),
                          timings={**driver.timing, "rlcd_inference_s": sum(r.get("timing", {}).get("inference_ms", 0) for r in responses)/1000})
    except (InterruptedError, KeyboardInterrupt, Cancelled):
        report.update(status="canceled", reason="user_interrupt")
    except Exception as error:
        report.update(status="blocked", reason=type(error).__name__ + ": " + str(error))
    finally:
        if selector is not None and selector is not reusable_selector:
            try:
                selector.close()
            except Exception as error:
                report["cleanup_errors"].append("model: " + str(error))
        if connection is not None:
            if browser.get("session_started"):
                try:
                    connection.end_session(browser["session"])
                    if browser.get("owned_browser_pid"):
                        _, windows = connection.call("list_windows", {"pid": browser["owned_browser_pid"]}, cleanup=True)
                        report["owned_browser_windows_remaining"] = len(windows["windows"])
                        if windows["windows"]:
                            report["cleanup_errors"].append("Owned browser windows remain")
                except Exception as error:
                    report["cleanup_errors"].append("browser: " + str(error))
            try:
                report["cleanup_errors"].extend(connection.close())
            except Exception as error:
                report["cleanup_errors"].append("driver: " + str(error))
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
        if report["cleanup_errors"]:
            report["status_before_cleanup_failure"] = report["status"]
            report["status"] = "cleanup_failed"
        report["end_to_end_wall_s"] = time.monotonic() - started
        private_json(out / "summary.json", report)
        progress("Finished: " + report["status"] + "; " + str(out))
    return report


def _decision_cases(bundle):
    """Preflight the entire bundle before any model load; gold stays separate."""
    from .engine.prototype.decision import validate_request
    if not isinstance(bundle, dict) or set(bundle) != {"kind", "cases"} or bundle["kind"] != "decisions":
        raise ValueError("Recorded decisions require exactly kind='decisions' and cases")
    cases = bundle["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Supply 1..100 recorded decision cases")
    identifiers, prepared = set(), []
    for case in cases:
        required = {"id", "goal", "observation_summary", "candidates"}
        if not isinstance(case, dict) or not required <= set(case) or set(case) - required - {"expected_id", "history"}:
            raise ValueError("Each recorded case requires id, goal, observation_summary, candidates and optional expected_id/history only")
        identity = case["id"]
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 256 or any(ord(c) < 32 for c in identity):
            raise ValueError("Recorded case IDs must be nonempty text up to 256 characters without control characters")
        if identity in identifiers:
            raise ValueError("Duplicate recorded case ID")
        identifiers.add(identity)
        # The unchanged service validates exact candidate shape, full order,
        # capacity and bytes without loading MLX or a tokenizer. Its token bound
        # remains an explicit worker refusal; no truncation is introduced here.
        model_input = {key: deepcopy(case[key]) for key in ("goal", "observation_summary", "candidates")}
        model_input["history"] = deepcopy(case.get("history", []))
        validate_request(**model_input)
        if "expected_id" in case:
            expected = case["expected_id"]
            if expected is not None and (not isinstance(expected, str) or expected not in [c["id"] for c in case["candidates"]]):
                raise ValueError("expected_id must be an existing candidate ID or null for abstention")
        prepared.append(model_input)
    # Library inputs, like CLI JSON, must be losslessly serializable finite data.
    json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return deepcopy(cases), prepared


def _decision_output_errors(response, candidates, request_id):
    """Validity and semantic scoring are deliberately separate checks."""
    if not isinstance(response, dict):
        return ["response_not_object"]
    errors, ids = [], [c["id"] for c in candidates]
    selected = response.get("selected_id")
    if "selected_id" not in response or (selected is not None and (not isinstance(selected, str) or selected not in ids)):
        errors.append("selected_id_outside_catalog")
    if response.get("request_id") != request_id:
        errors.append("request_id_mismatch")
    if response.get("abstained") is not (selected is None):
        errors.append("inconsistent_abstention")
    coverage = response.get("coverage")
    if (not isinstance(coverage, dict) or coverage.get("candidate_ids") != ids
            or type(coverage.get("omitted_candidates")) is not int or coverage["omitted_candidates"] != 0):
        errors.append("candidate_coverage_mismatch")
    if response.get("dispatched") is not False:
        errors.append("unexpected_dispatch_claim")
    try:
        json.dumps(response, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        errors.append("non_json_response")
    return errors


def _evaluate_decisions(payload, config, progress):
    from .engine.prototype.cli import private_json
    from .engine.prototype.decision import ModelService
    started = time.monotonic()
    cases, inputs = _decision_cases(payload["cases"])
    if payload["model"] not in ("baseline", "comparator"):
        raise ValueError("Choose baseline or the explicit comparator")
    out = artifact_directory(payload.get("out"), "eval-decisions")
    private_json(out / "cases.json", {"kind": "decisions", "cases": cases})
    private_json(out / "model-inputs.json", inputs)
    input_digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cancel, selector, old_handler = threading.Event(), None, None
    results, cleanup_errors = [], []
    report = {"status": "evaluated", "reason": "evaluation_finished", "kind": "decisions", "driver": None,
              "model": payload["model"], "selector_decoding": "original RLCD", "inference_local_only": True,
              "artifacts": str(out), "model_inputs_sha256": input_digest, "total": len(cases),
              "results": results, "cleanup_errors": cleanup_errors, "startup_wall_s": None,
              "desktop_mutated": False, "live_desktop_proven": False, "expected_answers_sent_to_model": False}
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        with runtime_environment(config):
            progress("Loading " + payload["model"] + " for " + str(len(cases)) + " recorded decisions")
            if cancel.is_set():
                raise InterruptedError("Canceled before model load")
            loading = time.monotonic()
            try:
                selector = ModelService(model=payload["model"])
                if cancel.is_set():
                    raise InterruptedError("Canceled during model load")
                report["selector_info"] = selector.info()
            finally:
                report["startup_wall_s"] = time.monotonic() - loading
            for index, (case, model_input) in enumerate(zip(cases, inputs)):
                if cancel.is_set():
                    raise InterruptedError("Canceled before next decision")
                progress("Recorded decision " + str(index+1) + "/" + str(len(cases)) + ": " + case["id"])
                if cancel.is_set():
                    raise InterruptedError("Canceled before next decision")
                row = {"id": case["id"], "status": "running", "expected_present": "expected_id" in case,
                       "output_valid": None, "selected_id": None, "score": None, "response_received": False}
                results.append(row)
                tick = time.monotonic()
                request_id = "recorded-decision-" + str(index+1)
                try:
                    response = selector.choose(**deepcopy(model_input), request_id=request_id)
                    row["response_received"] = True
                    errors = _decision_output_errors(response, model_input["candidates"], request_id)
                    row.update(output_valid=not errors, validity_errors=errors)
                    # Real ModelService responses are JSON. Retain malformed fake
                    # responses only when serializable so diagnostics cannot lose
                    # the rest of the finite evaluation report.
                    if "non_json_response" not in errors:
                        row["response"] = response
                    if errors:
                        row["status"] = "invalid_output"
                        report.update(status="blocked", reason="invalid_decision_output")
                        break
                    row["selected_id"] = response["selected_id"]
                    if cancel.is_set():
                        row.update(status="canceled", response_discarded_after_cancellation=True)
                        raise InterruptedError("Canceled during decision; result not scored")
                    row["status"] = "evaluated"
                    if "expected_id" in case:
                        row["score"] = {"expected_id": case["expected_id"], "correct": response["selected_id"] == case["expected_id"]}
                except (InterruptedError, KeyboardInterrupt):
                    row["status"] = "canceled"
                    raise
                except Exception as error:
                    row.update(status="error", error=type(error).__name__ + ": " + str(error))
                    report.update(status="blocked", reason="decision_error")
                    break
                finally:
                    row["wall_s"] = time.monotonic() - tick
                    private_json(out / f"case-{index+1:03d}.json", row)
    except (InterruptedError, KeyboardInterrupt):
        report.update(status="canceled", reason="evaluation_interrupted")
    except Exception as error:
        report.update(status="blocked", reason=type(error).__name__ + ": " + str(error))
    finally:
        if selector is not None:
            try:
                selector.close()
            except Exception as error:
                cleanup_errors.append("model: " + str(error))
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
        if cleanup_errors:
            report.update(status_before_cleanup_failure=report["status"], status="cleanup_failed")
        graded = [row for row in results if row["score"] is not None]
        expected_total = sum("expected_id" in case for case in cases)
        report.update(attempted=len(results), unrun=len(cases)-len(results),
                      unrun_case_ids=[case["id"] for case in cases[len(results):]],
                      evaluated=sum(row["status"] == "evaluated" for row in results),
                      valid_outputs=sum(row["output_valid"] is True for row in results),
                      invalid_outputs=sum(row["output_valid"] is False for row in results),
                      scoring={"expected_total": expected_total, "graded": len(graded),
                               "correct": sum(row["score"]["correct"] for row in graded),
                               "incorrect": sum(not row["score"]["correct"] for row in graded),
                               "ungraded_expected": expected_total-len(graded)},
                      end_to_end_wall_s=time.monotonic()-started)
        private_json(out / "summary.json", report)
        progress("Recorded evaluation finished: " + report["status"] + "; " + str(out))
    return report


def _evaluate(payload, config, progress):
    from .engine.prototype.cli import private_json
    from .engine.prototype.decision import ModelService
    cases = payload["cases"]
    if isinstance(cases, dict) and "kind" in cases:
        if cases["kind"] != "decisions":
            raise ValueError("Explicit eval bundle kind must be decisions; omit kind for existing simulations")
        return _evaluate_decisions(payload, config, progress)
    if isinstance(cases, dict):
        cases = cases.get("cases", [cases])
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Supply 1..100 declarative simulation fixtures")
    for case in cases:
        if not isinstance(case, dict) or not all(key in case for key in ("id", "controls", "reference_plan")):
            raise ValueError("Each simulation case needs id, controls and reference_plan; no live dispatch occurs in eval")
    out = artifact_directory(payload.get("out"), "eval")
    results, cleanup_errors, selector = [], [], None
    started = time.monotonic()
    status, reason = "evaluated", "evaluation_finished"
    try:
        with runtime_environment(config):
            selector = ModelService(model=payload["model"])
            for index, case in enumerate(cases):
                progress("Simulation " + str(index+1) + "/" + str(len(cases)) + ": " + case["id"])
                results.append(_run({"task": case["reference_plan"], "model": payload["model"], "execute": True,
                                     "supplied_data": case.get("supplied_data"),
                                     "out": str(out / f"case-{index+1:03d}")}, config, progress,
                                    fixture=case, reusable_selector=selector))
                if results[-1]["status"] == "canceled":
                    status, reason = "canceled", "evaluation_canceled_with_unrun_cases"
                    break
    except (InterruptedError, KeyboardInterrupt):
        status, reason = "canceled", "evaluation_interrupted"
    except Exception as error:
        status, reason = "blocked", type(error).__name__ + ": " + str(error)
    finally:
        if selector is not None:
            try:
                selector.close()
            except Exception as error:
                cleanup_errors.append("model: " + str(error))
    report = {"status": status, "reason": reason, "driver": "simulated", "model": payload["model"], "results": results,
              "completed": sum(x["status"] == "complete" for x in results), "total": len(cases),
              "attempted": len(results), "unrun": len(cases)-len(results), "cleanup_errors": cleanup_errors,
              "end_to_end_wall_s": time.monotonic()-started, "artifacts": str(out), "live_desktop_proven": False}
    if cleanup_errors:
        report.update(status_before_cleanup_failure=status, status="cleanup_failed")
    private_json(out / "summary.json", report)
    return report


def driver_readiness(value, *, activation=False):
    """Normalize only the documented daemon permission result, never a package label."""
    reply = value.get("permissions")
    structured = reply.get("result", {}).get("structuredContent") if isinstance(reply, dict) and reply.get("ok") is True else None
    verified = isinstance(structured, dict) and all(type(structured.get(k)) is bool for k in ("accessibility", "screen_recording"))
    permissions = {k: structured[k] for k in ("accessibility", "screen_recording")} if verified else None
    ready = value.get("runtime") == "running" and verified and all(permissions.values())
    status = ("ready" if ready else "awaiting_permissions" if value.get("runtime") in ("running", "launch_pending") else "blocked") if activation else "diagnostic"
    return {"status": status, "ok": ready if activation else True, "ready": ready, "runtime": value,
            "permissions_verified": verified, "permissions": permissions,
            "readiness_basis": "owned_daemon_reported_accessibility_and_screen_recording_grants",
            "direct_capture_proven": False, "model_load_proven": False,
            "remedy": None if ready else "Review the distinct Locua Driver's normal permission status, then run locua doctor --probe. Do not repeat an unresolved launch."}


def runtime_dependency_status(config):
    """Bounded package-metadata check in the configured interpreter; no ML imports."""
    lock = Path(__file__).with_name("engine") / "probes/requirements-rlcd.lock.txt"
    expected = dict(line.split("==", 1) for line in lock.read_text().splitlines() if line and not line.startswith("#"))
    base = {"requirements_file": str(lock), "expected": expected, "model_imported": False, "inference_started": False}
    interpreter = config.get("runtime_python")
    if not interpreter:
        return {**base, "ready": False, "reason": "runtime_python_not_configured", "remedy": "Set runtime_python to the local virtual environment's Python executable."}
    script = """import importlib.metadata as m,json,sys
result={}
for name in json.loads(sys.argv[1]):
 try: result[name]=m.version(name)
 except m.PackageNotFoundError: result[name]=None
print(json.dumps({'versions':result,'python':sys.version,'prefix':sys.prefix,'executable':sys.executable}))
"""
    try:
        completed = subprocess.run([interpreter, "-I", "-c", script, json.dumps(list(expected))],
                                   text=True, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
        if completed.returncode:
            raise RuntimeError("Configured interpreter exited " + str(completed.returncode) + ": " + completed.stderr[-1000:])
        if len(completed.stdout) > 65536:
            raise ValueError("Dependency report exceeded its bound")
        identity = json.loads(completed.stdout)
        versions = identity["versions"]
        if not isinstance(versions, dict) or set(versions) != set(expected):
            raise ValueError("Dependency report did not cover the lock")
        mismatches = [{"package": name, "expected": version, "installed": versions[name]}
                      for name, version in expected.items() if versions[name] != version]
        return {**base, "ready": not mismatches, "interpreter": identity, "mismatches": mismatches,
                "remedy": "Install the listed requirements_file into the configured interpreter; retain the virtual-environment executable path." if mismatches else None}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {**base, "ready": False, "reason": str(error),
                "remedy": "Check runtime_python points to an executable local virtual environment, then install its pinned requirements_file."}


def execute(operation, payload, config, progress):
    with _EXECUTION_LOCK:
        if operation in ("doctor", "setup"):
            from .engine.driver_runtime import status, start
            activation = operation == "setup" and payload.get("activate_driver")
            if activation:
                return driver_readiness(start(config), activation=True)
            dependencies = runtime_dependency_status(config)
            try:
                result = driver_readiness(status(config))
            except Exception as error:
                result = {"status": "diagnostic", "ok": True, "ready": False, "permissions_verified": False,
                          "permissions": None, "runtime": {"status": "unavailable", "reason": str(error)},
                          "remedy": "Configure the distinct driver app/binary/socket and inspect its local status."}
            result["runtime_dependencies"] = dependencies
            result["driver_ready"] = result["ready"]
            result["ready"] = result["driver_ready"] and dependencies["ready"]
            return result
        if operation == "targets":
            return _targets(payload, config, progress)
        if operation == "prepare_guided":
            return _prepare_guided(payload, config, progress)
        if operation == "observe":
            return _observe(payload, config, progress)
        if operation == "run":
            return _run(payload, config, progress)
        if operation == "eval":
            return _evaluate(payload, config, progress)
        raise ValueError("Unsupported engine operation: " + operation)
