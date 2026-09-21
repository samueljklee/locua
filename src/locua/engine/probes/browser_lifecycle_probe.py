#!/usr/bin/env python3
"""Bounded Cua 0.28.2 lifecycle experiment; run only with operator approval.

Creates one disposable driver-owned browser on about:blank. No desktop input,
screenshots, existing profiles, internal session IDs, or global TTL changes.
Each invocation owns a separate persistent MCP transport. Raw timestamped
responses and a machine-readable summary are kept in a new local directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import uuid

from cua_transport import CuaTransport


TOOLS = {"start_session", "get_session", "end_session", "browser_prepare",
         "list_windows", "get_browser_state"}


class ProbeFailure(RuntimeError):
    pass


def structured(response):
    return response.get("result", {}).get("structuredContent", {})


def refused(response):
    result = response.get("result", {})
    return bool(result.get("isError")) or structured(response).get("status") == "refused"


def refusal_text(response):
    return json.dumps(response.get("result", {}), ensure_ascii=False).lower()


def session_ended(response):
    text = refusal_text(response)
    return refused(response) and ("session_ended" in text or
                                  ("session" in text and "has ended" in text))


def lifecycle_lost(response):
    return refused(response) and "driver-owned browser lifecycle ended" in refusal_text(response)


def check_clock(response):
    data = structured(response)
    if refused(response) or data.get("state") != "active":
        raise ProbeFailure("Expected an active lifecycle snapshot")
    idle, remaining = data.get("idle_seconds"), data.get("expires_in_seconds")
    if type(idle) is not int or type(remaining) is not int:
        raise ProbeFailure("Lifecycle snapshot omitted numeric idle/expiry fields")
    # Both fields are rounded down; tolerate that, but do not silently adapt to
    # an unexpected host configuration or extend this approved experiment.
    awaiting_sweep = remaining == 0 and 298 <= idle <= 365
    if not awaiting_sweep and not 298 <= idle + remaining <= 300:
        raise ProbeFailure("Observed TTL is not the expected 300 seconds; no configuration was changed")
    return data


def validate_call(inventory, name, arguments):
    by_name = {tool["name"]: tool for tool in inventory["tools"]}
    schema = by_name[name]["inputSchema"]
    if set(arguments) - set(schema.get("properties", {})):
        raise ProbeFailure(f"Call contains fields absent from installed {name} schema")
    if set(schema.get("required", [])) - set(arguments):
        raise ProbeFailure(f"Call omits fields required by installed {name} schema")
    if any(key.startswith("_") for key in arguments):
        raise ProbeFailure("Internal session/authority arguments are forbidden")


def select_owned_blank_window(windows, pid):
    """Select the unique observed blank page; exact CDP binding still required.

    Edge also publishes untitled offscreen auxiliary windows. Title/geometry
    select a candidate only, never authorize a heuristic browser binding.
    """
    if not isinstance(windows, list) or any(window.get("pid") != pid for window in windows):
        raise ProbeFailure("Owned-PID window listing included an unexpected process")
    candidates = []
    for window in windows:
        bounds = window.get("bounds", {})
        if (window.get("app_name") == "Microsoft Edge"
                and window.get("title") == "about:blank"
                and window.get("layer", 0) == 0
                and bounds.get("width", 0) >= 100
                and bounds.get("height", 0) >= 100):
            candidates.append(window)
    if len(candidates) > 1:
        raise ProbeFailure("Multiple owned about:blank windows are ambiguous")
    return candidates[0] if candidates else None


class Probe:
    def __init__(self, mode, directory, socket=None, poll_seconds=15, factory=CuaTransport):
        self.mode, self.directory, self.socket = mode, directory, socket
        self.poll_seconds, self.factory = poll_seconds, factory
        self.named = f"locua-lifecycle-{mode}-{uuid.uuid4().hex[:10]}"
        self.driver = None
        self.pid = None
        self.target = None
        self.started = time.monotonic()
        self.directory.mkdir(parents=True, exist_ok=False)
        os.chmod(self.directory, 0o700)
        self.trace = os.fdopen(os.open(self.directory / "trace.jsonl",
                                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.summary = {"mode": mode, "session": self.named, "status": "running",
                        "expected_idle_ttl_seconds": 300, "poll_seconds": poll_seconds,
                        "source_revision": "9e60d90b8681d3ba7ccf2c7801dbaa21b0d6efbb"}

    def record(self, event, **fields):
        item = {"utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "event": event, **fields}
        self.trace.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.trace.flush()

    def call(self, name, arguments, *, allow_refusal=False, driver=None):
        client = driver or self.driver
        validate_call(client.inventory, name, arguments)
        self.record("request", transport="observer" if driver else "owner",
                    name=name, arguments=arguments)
        try:
            response = client.call(name, arguments)
        except BaseException as error:
            self.record("transport_error", name=name, error=repr(error))
            raise
        self.record("response", transport="observer" if driver else "owner",
                    name=name, arguments=arguments, response=response)
        if refused(response) and not allow_refusal:
            raise ProbeFailure(f"{name} refused; inspect the raw trace; no retry")
        return response

    def observe_owned_windows(self):
        """Separate observer prevents read-only list_windows from renewing owner."""
        with self.factory(self.socket, timeout=30) as observer:
            self.record("observer_connected", server=observer.server)
            result = self.call("list_windows", {"pid": self.pid}, driver=observer)
            windows = structured(result).get("windows")
            if not isinstance(windows, list):
                raise ProbeFailure("Owned-PID window check omitted windows array")
            self.call("end_session", {}, driver=observer)
            return windows

    def snapshot(self, allow_refusal=False):
        result = self.call("get_browser_state", {
            **self.target, "session": self.named, "snapshot_format": "semantic_v2",
            "include_screenshot": False}, allow_refusal=allow_refusal)
        if not refused(result) and structured(result).get("page", {}).get("url") != "about:blank":
            raise ProbeFailure("Owned tab is no longer about:blank; stopping scope-limited probe")
        return result

    def setup(self):
        self.driver = self.factory(self.socket, timeout=30)
        if not TOOLS <= self.driver.tool_names:
            raise ProbeFailure(f"Installed inventory lacks {sorted(TOOLS - self.driver.tool_names)}")
        inventory = json.dumps(self.driver.inventory, indent=2)
        (self.directory / "inventory.json").write_text(inventory + "\n")
        self.record("initialized", server=self.driver.server,
                    inventory_sha256=hashlib.sha256(inventory.encode()).hexdigest())
        self.call("start_session", {})
        self.call("start_session", {"session": self.named})
        check_clock(self.call("get_session", {}))
        check_clock(self.call("get_session", {"session": self.named}))
        prepared = structured(self.call("browser_prepare", {
            "session": self.named, "allow_launch": True,
            "profile": {"mode": "isolated_new"}}))
        if prepared.get("action") != "launched_isolated_browser" or not prepared.get("prepared"):
            raise ProbeFailure("Preparation did not create a disposable isolated browser")
        self.pid = prepared.get("prepared_pid")
        if type(self.pid) is not int or self.pid <= 0:
            raise ProbeFailure("Preparation omitted its owned PID")
        self.summary["owned_pid"] = self.pid
        # Only this setup poll uses the owner's implicit session. No action
        # targets another process/window, and ambiguous own windows fail closed.
        deadline = time.monotonic() + 30
        while True:
            windows = structured(self.call("list_windows", {"pid": self.pid})).get("windows", [])
            window = select_owned_blank_window(windows, self.pid)
            if window or time.monotonic() >= deadline:
                break
            time.sleep(1)
        if window is None:
            raise ProbeFailure("No unique owned Microsoft Edge about:blank window appeared")
        self.record("window_candidate_selected", window=window,
                    excluded_owned_window_ids=[row["window_id"] for row in windows
                                               if row["window_id"] != window["window_id"]])
        self.summary["owned_window_id"] = window["window_id"]
        binding = structured(self.call("get_browser_state", {
            "pid": self.pid, "window_id": window["window_id"], "session": self.named}))
        tabs = binding.get("tabs", [])
        if (binding.get("binding_quality") != "exact" or
                binding.get("endpoint_access_class") != "driver_owned" or
                len(tabs) != 1 or tabs[0].get("url") != "about:blank"):
            raise ProbeFailure("Expected exact driver-owned binding to one about:blank tab")
        self.target = {"target_id": binding["target_id"], "tab_id": tabs[0]["tab_id"]}
        self.snapshot()
        # Establish a clean implicit baseline without reading an internal ID.
        if structured(self.call("start_session", {})).get("revived") is not False:
            raise ProbeFailure("Implicit owner unexpectedly needed revival during setup")
        self.baseline = time.monotonic()
        self.record("monitor_baseline", target=self.target, session=self.named,
                    implicit=check_clock(self.call("get_session", {})),
                    named=check_clock(self.call("get_session", {"session": self.named})))

    def monitor(self):
        # 365 seconds spans one 300-second TTL plus 30-second sweep and margin.
        # 665 seconds spans two actual TTL periods plus sweep and margin.
        duration = 365 if self.mode == "expire" else 665
        deadline = self.baseline + duration
        renewal_at = self.baseline + 45
        print(json.dumps({"event": "monitoring", "mode": self.mode,
                          "maximum_seconds": duration, "session": self.named}), flush=True)
        while time.monotonic() < deadline:
            time.sleep(min(self.poll_seconds, max(0, deadline - time.monotonic())))
            implicit = self.call("get_session", {}, allow_refusal=True)
            named = self.call("get_session", {"session": self.named}, allow_refusal=True)
            elapsed = time.monotonic() - self.baseline
            state = self.snapshot(allow_refusal=True)
            if self.mode == "expire" and session_ended(implicit):
                named_active = not refused(named) and structured(named).get("state") == "active"
                gone = self.observe_owned_windows() == []
                reproduced = named_active and lifecycle_lost(state) and gone
                self.summary.update(status="failure_reproduced" if reproduced else "different_expiry_behavior",
                                    observed_at_seconds=round(elapsed, 3), named_active=named_active,
                                    lifecycle_refusal=lifecycle_lost(state), owned_windows_gone=gone)
                return
            if refused(implicit) or refused(named) or refused(state):
                raise ProbeFailure("Unexpected refusal before the intended expiry condition")
            check_clock(implicit)
            check_clock(named)
            if self.mode == "keepalive" and time.monotonic() >= renewal_at:
                renewal = structured(self.call("start_session", {}))
                if renewal.get("revived") is not False:
                    raise ProbeFailure("Keepalive revived an expired session; continuity failed")
                check_clock(self.call("get_session", {}))
                renewal_at = time.monotonic() + 45
            self.record("tick", monitor_elapsed_seconds=round(elapsed, 3))
        self.summary.update(status="survived_two_ttl_periods" if self.mode == "keepalive"
                            else "expiry_failure_not_reproduced",
                            observed_at_seconds=round(time.monotonic() - self.baseline, 3))

    def cleanup(self):
        errors = []
        if self.driver:
            if not self.driver.poisoned:
                try:
                    self.call("end_session", {"session": self.named})
                    # Verify before ending implicit or closing the proxy, so
                    # EOF cleanup cannot masquerade as named explicit cleanup.
                    if self.pid:
                        self.summary["explicit_cleanup_owned_windows_gone"] = self.observe_owned_windows() == []
                except BaseException as error:
                    errors.append(repr(error))
                if not self.driver.poisoned:
                    try:
                        self.call("end_session", {})
                    except BaseException as error:
                        errors.append(repr(error))
            else:
                errors.append("Owner transport poisoned: explicit end_session not dispatched")
            try:
                self.driver.close()
            except BaseException as error:
                errors.append(repr(error))
        # Verify only the returned disposable PID, never another user's browser.
        if self.pid:
            try:
                self.summary["cleanup_owned_windows_gone"] = self.observe_owned_windows() == []
            except BaseException as error:
                errors.append(repr(error))
                self.summary["cleanup_owned_windows_gone"] = None
        self.summary["cleanup_errors"] = errors
        self.summary["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.record("summary", **self.summary)
        (self.directory / "summary.json").write_text(json.dumps(self.summary, indent=2) + "\n")
        self.trace.close()

    def run(self):
        try:
            self.setup()
            self.monitor()
        except BaseException as error:
            self.summary.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "inconclusive",
                                error=repr(error))
        finally:
            self.cleanup()
        print(json.dumps(self.summary), flush=True)
        passed = self.summary["status"] in {"failure_reproduced", "survived_two_ttl_periods"}
        clean = (self.summary.get("explicit_cleanup_owned_windows_gone") is True
                 and self.summary.get("cleanup_owned_windows_gone") is True
                 and not self.summary["cleanup_errors"])
        return 0 if passed and clean else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("expire", "keepalive"))
    parser.add_argument("--output", required=True, type=Path, help="New Git-ignored artifact directory")
    parser.add_argument("--socket")
    parser.add_argument("--poll-seconds", type=int, default=15, choices=range(5, 31), metavar="5..30")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    return Probe(args.mode, args.output, args.socket, args.poll_seconds).run()


if __name__ == "__main__":
    raise SystemExit(main())
