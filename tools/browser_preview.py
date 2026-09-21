#!/usr/bin/env python3
"""Developer acceptance runner for two shipped browser fixtures.

This utility is not an action recipe or generic Locua capability. It owns one
loopback fixture server, calls the public guarded library, then checks exact
persisted fixture fields outside the model. No online model or automatic retry.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from urllib.parse import urlsplit

from locua import lib
from locua.config import load as load_config, read_json, strict_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/browser-lab"
# Independent fixture oracle. These values are never passed to lib.run/model.
EXPECTED = {
    "contact": {"shipping.email": "shipping@example.test", "shipping.cost": "701",
                "billing.cost": "0046", "billing.email": "accounts+071@example.test", "billing.news": False},
    "badge": {"default.caption": "Visitor", "visitor.caption": "Research guest 09", "visitor.colour": "Blue"},
}


def private_json(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def read_server_ready(process, receipts, *, timeout_s=10):
    """Bounded read of this exact child's single startup line; no port guessing."""
    deadline, data = time.monotonic() + timeout_s, b""
    fd = process.stdout.fileno()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError("Fixture server did not report readiness within its startup bound")
        piece = os.read(fd, 4096)
        if not piece:
            raise RuntimeError("Fixture server exited before readiness; inspect server.stderr.log")
        data += piece
        if len(data) > 16384:
            raise ValueError("Fixture server readiness exceeded its output bound")
    ready = strict_json(data.split(b"\n", 1)[0].decode("utf-8"))
    if not isinstance(ready, dict) or set(ready) != {"url", "receipts"}:
        raise ValueError("Unexpected fixture server startup schema")
    parsed = urlsplit(ready["url"])
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
            or Path(ready["receipts"]).resolve() != receipts.resolve()):
        raise ValueError("Fixture server did not bind the owned loopback URL and receipt directory")
    if process.poll() is not None:
        raise RuntimeError("Fixture server exited immediately after readiness")
    return ready


def stop_server(process):
    """Signal only the Popen child this invocation created, never a discovered PID."""
    escalated, interrupted = False, False
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                escalated = True
                process.kill()
                process.wait(timeout=5)
            except KeyboardInterrupt:
                escalated, interrupted = True, True
                process.kill()
                process.wait(timeout=5)
        code = process.poll()
        return {"verified_stopped": code is not None, "return_code": code,
                "kill_required": escalated, "interrupted": interrupted}
    finally:
        if process.stdout is not None:
            process.stdout.close()


def verify_receipt(path, expected, *, wait_s=2):
    # Wait only for existence, never for a mismatch to become the expected answer.
    deadline = time.monotonic() + wait_s
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0, deadline-time.monotonic())))
    if not path.is_file() or path.is_symlink():
        return {"status": "fail", "reason": "saved_receipt_absent", "path": str(path)}
    try:
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError("Receipt exceeded fixture bound")
        actual = strict_json(raw.decode("utf-8"))
        if not isinstance(actual, dict):
            raise ValueError("Receipt must be an object")
        missing, extra = sorted(set(expected)-set(actual)), sorted(set(actual)-set(expected))
        mismatches = [key for key in expected if key in actual
                      and (type(actual[key]) is not type(expected[key]) or actual[key] != expected[key])]
        return {"status": "pass" if not (missing or extra or mismatches) else "fail",
                "reason": "exact_full_fields" if not (missing or extra or mismatches) else "receipt_fields_differ",
                "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                "missing_fields": missing, "extra_fields": extra, "mismatched_fields": mismatches,
                "field_count": len(actual), "evidence_plane": "fixture_server_saved_json"}
    except (OSError, UnicodeError, ValueError) as error:
        return {"status": "fail", "reason": "invalid_saved_receipt", "error": str(error), "path": str(path)}


def run_preview(*, case, config, out, model="baseline", browser_click_route="trusted", execute=False):
    started = time.monotonic()
    if case not in EXPECTED or model not in ("baseline", "comparator") or browser_click_route not in ("trusted", "dom_event"):
        raise ValueError("Choose a shipped case, explicit local model, and supported browser click route")
    if execute is not True:
        raise ValueError("This acceptance runner requires explicit --execute for the reviewed fixture plan")
    config = Path(config).expanduser().resolve()
    if not config.is_file():
        raise ValueError("An existing explicit Locua configuration file is required")
    load_config(config)  # Validate before server startup; no model or desktop call.
    plan_path, server_path = FIXTURES / (case + "-plan.json"), FIXTURES / "server.py"
    template = read_json(plan_path)
    if template.get("scope") != {"kind": "browser", "url": "http://127.0.0.1:8765/" + case}:
        raise ValueError("Shipped plan scope differs from the reviewed fixture contract")
    out = Path(out).expanduser().resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(out, 0o700)
    receipts = out / "receipts"; receipts.mkdir(mode=0o700)
    private_json(out / "expected-receipt.json", EXPECTED[case])
    report = {"schema": "locua.browser-fixture-acceptance.v1", "case": case, "model": model,
              "browser_click_route": browser_click_route, "execute": True, "status": "fail",
              "artifacts": str(out), "fixture_case_count": 1, "general_reliability_proven": False,
              "oracle_supplied_to_model": False, "fixture_setup": "runner_owned_loopback_server_and_reviewed_plan",
              "source_sha256": {"plan": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                                "server": hashlib.sha256(server_path.read_bytes()).hexdigest()},
              "engine_complete": False, "receipt_verification": {"status": "unrun"},
              "server_cleanup": {"verified_stopped": True, "reason": "not_started"}, "timing": {}}
    process = None
    progress_fd = os.open(out / "progress.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(progress_fd, "w", encoding="utf-8") as progress_file, os.fdopen(
            os.open(out / "server.stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as server_log:
        def progress(message):
            print(message, file=sys.stderr, flush=True)
            progress_file.write(json.dumps({"elapsed_s": time.monotonic()-started, "message": str(message)}, ensure_ascii=False)+"\n")
            progress_file.flush()
        try:
            progress("Starting the owned loopback fixture server on an OS-assigned port")
            tick = time.monotonic()
            process = subprocess.Popen([sys.executable, str(server_path), "--port", "0", "--out", str(receipts)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=server_log,
                                       cwd=out, start_new_session=True)
            report["server_pid"] = process.pid
            ready = read_server_ready(process, receipts)
            report["timing"]["server_startup_wall_s"] = time.monotonic()-tick
            report["server"] = ready
            private_json(out / "server.json", ready)
            task = deepcopy(template)
            task["scope"]["url"] = ready["url"] + "/" + case
            private_json(out / "task.json", task)
            progress("Running the unchanged reviewed " + case + " plan with " + model)
            tick = time.monotonic()
            try:
                engine = lib.run(task=task, config=config, model=model, browser_click_route=browser_click_route,
                                 execute=True, out=out / "run", progress=progress)
            finally:
                report["timing"]["locua_run_including_model_and_cleanup_wall_s"] = time.monotonic()-tick
            private_json(out / "engine-result.json", engine)
            report["engine_result"] = str(out / "engine-result.json")
            report["engine_status"] = engine.get("result", {}).get("status")
            report["engine_complete"] = engine.get("ok") is True and report["engine_status"] == "complete"
            if report["engine_status"] == "canceled":
                report["status"] = "canceled"
        except KeyboardInterrupt:
            report.update(status="canceled", reason="caller_interrupt")
        except Exception as error:
            report.update(status="fail", reason=type(error).__name__ + ": " + str(error))
        finally:
            # Also inspect partial effects after an exception; a receipt cannot
            # override blocked/canceled execution or repair a failed run.
            if "server" in report:
                tick = time.monotonic()
                try:
                    report["receipt_verification"] = verify_receipt(receipts / (case + ".json"), EXPECTED[case])
                    private_json(out / "receipt-verification.json", report["receipt_verification"])
                except KeyboardInterrupt:
                    report.update(status="canceled", reason="caller_interrupt_during_receipt_verification")
                    report["receipt_verification"] = {"status": "unrun", "reason": "verification_interrupted"}
                except Exception as error:
                    report["receipt_verification"] = {"status": "fail", "reason": "oracle_error", "error": str(error)}
                report["timing"]["receipt_verification_wall_s"] = time.monotonic()-tick
                if (report["status"] != "canceled" and "reason" not in report and report["engine_complete"]
                        and report["receipt_verification"]["status"] == "pass"):
                    report["status"] = "pass"
            tick = time.monotonic()
            if process is not None:
                try:
                    report["server_cleanup"] = stop_server(process)
                    if report["server_cleanup"].get("interrupted"):
                        report.update(status="canceled", reason="caller_interrupt_during_server_cleanup")
                except Exception as error:
                    report["server_cleanup"] = {"verified_stopped": False, "error": str(error)}
            report["timing"]["server_cleanup_wall_s"] = time.monotonic()-tick
            if not report["server_cleanup"]["verified_stopped"]:
                report.update(status_before_cleanup_failure=report["status"], status="cleanup_failed")
            report["timing"]["full_end_to_end_wall_s"] = time.monotonic()-started
            private_json(out / "summary.json", report)
            progress("Acceptance result: " + report["status"] + "; " + str(out / "summary.json"))
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--case", choices=tuple(EXPECTED), required=True)
    value.add_argument("--config", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True, help="New private artifact directory; never reuse a prior run.")
    value.add_argument("--model", choices=("baseline", "comparator"), default="baseline")
    value.add_argument("--browser-click-route", choices=("trusted", "dom_event"), default="trusted")
    value.add_argument("--execute", action="store_true", required=True, help="Explicitly authorize this reviewed fixture task.")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = run_preview(**vars(args))
    except Exception as error:
        print(json.dumps({"status": "invalid_input_or_setup", "error": str(error)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "pass" else 130 if report["status"] == "canceled" else 6


if __name__ == "__main__":
    raise SystemExit(main())
