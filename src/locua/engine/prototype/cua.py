"""Exact-target Cua adapter for the lab loop. No implicit launch or pixel input."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "probes"))
from cua_transport import CuaTransport
from browser_lifecycle_probe import validate_call
from .perception import normalize_observation, CUA_MACOS_0_28_2_CONTRACT


class PrivateTrace:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.lock = threading.Lock()

    def __call__(self, event):
        with self.lock:
            self.file.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
            self.file.flush()

    def close(self):
        self.file.close()


class CuaRefusal(RuntimeError):
    """Structured driver refusal, compatible with existing RuntimeError callers."""
    def __init__(self, payload, detail=""):
        self.payload = deepcopy(payload)
        refusal = self.payload.get("refusal") if isinstance(self.payload, dict) else None
        code = refusal.get("code") if isinstance(refusal, dict) else None
        self.code = code if isinstance(code, str) and code else None
        self.detail = detail
        super().__init__("Cua refusal: " + json.dumps(self.payload, ensure_ascii=False) + "; " + detail)


class CuaOwner:
    """One private transport, serialized session renewal, bounded lifetime."""
    def __init__(self, directory, *, max_seconds=600, transport_factory=CuaTransport):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        os.chmod(self.directory, 0o700)
        self.trace = PrivateTrace(self.directory / "transport.jsonl")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.sessions = []
        self.failure = None
        self.deadline = time.monotonic() + max_seconds
        self.transport = None
        self.renew_thread = None
        try:
            self.transport = transport_factory(timeout=35)
            self.inventory = self.transport.inventory
            self.server = getattr(self.transport, "server", {})
            (self.directory / "server.json").write_text(json.dumps(self.server, indent=2) + "\n")
            (self.directory / "inventory.json").write_text(json.dumps(self.inventory, indent=2) + "\n")
            self.start_session(None)
            self.renew_thread = threading.Thread(target=self._renew, daemon=True)
            self.renew_thread.start()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.failure:
            raise RuntimeError("Cua owner continuity lost: " + self.failure)
        if self.stop_event.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError("Cua owner stopped or lifetime expired")

    def call(self, name, arguments, *, cleanup=False):
        with self.lock:
            if not cleanup:
                self.check()
                if name not in ("start_session", "end_session") and arguments.get("session") not in self.sessions:
                    raise RuntimeError("Inactive or unknown lifecycle session")
            validate_call(self.inventory, name, arguments)
            started = time.time_ns()
            request = {"name": name, "arguments": arguments}
            try:
                reply = self.transport.call(name, arguments)
                self.trace({"type": "tool", "started_at_ns": started, "request": request, "response": reply})
                result = reply["result"]
                payload = result.get("structuredContent", {})
                if (result.get("isError") or payload.get("status") == "refused"
                        or payload.get("effect") == "refused" or payload.get("refusal")):
                    detail = " ".join(item.get("text", "") for item in result.get("content", [])
                                      if item.get("type") == "text")
                    raise CuaRefusal(payload, detail)
                return {"request": request, "response": reply}, payload
            except Exception as error:
                self.trace({"type": "tool_error", "request": request, "error": repr(error)})
                raise

    def start_session(self, name):
        with self.lock:
            _, state = self.call("start_session", {} if name is None else {"session": name})
            if state.get("revived") or state.get("active") is not True or state.get("session") != (name or "implicit"):
                self.failure = "refusing revived, inactive or mismatched lifecycle owner"
                raise RuntimeError(self.failure)
            if name not in self.sessions:
                self.sessions.append(name)

    def end_session(self, name):
        with self.lock:
            if name not in self.sessions:
                return
            self.sessions.remove(name)
            self.call("end_session", {} if name is None else {"session": name}, cleanup=True)

    def _renew(self):
        while not self.stop_event.wait(35):
            try:
                with self.lock:
                    self.check()
                    for name in list(self.sessions):
                        self.start_session(name)
            except Exception as error:
                self.failure = repr(error)
                self.trace({"type": "renewal_failure", "error": self.failure})
                return

    def close(self):
        self.stop_event.set()
        if self.renew_thread:
            self.renew_thread.join(timeout=40)
        errors = []
        with self.lock:
            for name in reversed(list(self.sessions)):
                try:
                    self.end_session(name)
                except Exception as error:
                    errors.append(repr(error))
            if self.transport:
                self.transport.close()
        self.trace({"type": "owner_cleanup", "errors": errors})
        self.trace.close()
        return errors

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def browser_execution_capabilities(observation, *, server, schemas, click_route):
    """Bind inspected semantic routes to this exact capture and live inventory.

    Only the explicit adapter route grants synthetic DOM clicks. The producer
    fingerprint pins the reviewed source; this is not a binary attestation.
    """
    from .driver_contract import check_contract
    snapshot = observation.get("provenance", {}).get("raw_metadata", {}).get("snapshot", {})
    contract = snapshot.get("driver_contract", {})
    if (observation.get("kind") != "browser_semantic_v2"
            or server != {"name": "cua-driver", "version": "0.28.2"}
            or not check_contract(contract) or contract.get("driver_version") != "0.28.2"
            or snapshot.get("id") != observation.get("snapshot_id")
            or click_route not in ("trusted", "dom_event")):
        return None
    actions = {}
    schema = schemas.get("browser_type", {})
    props = schema.get("properties", {})
    if (set(schema.get("required", [])) >= {"target_id", "tab_id", "ref", "text"}
            and all(props.get(k, {}).get("type") == "string" for k in ("target_id", "tab_id", "ref", "text"))
            and props.get("replace", {}).get("type") == "boolean"
            and "insert_text" in props.get("mode", {}).get("enum", [])):
        actions["set_text"] = {"tool": "browser_type", "mode": "insert_text", "near_viewport": True}
    schema = schemas.get("browser_click", {})
    props = schema.get("properties", {})
    if (set(schema.get("required", [])) >= {"target_id", "tab_id"}
            and all(props.get(k, {}).get("type") == "string" for k in ("target_id", "tab_id", "ref"))
            and click_route in props.get("input_route", {}).get("enum", [])):
        actions["press"] = {"tool": "browser_click", "input_route": click_route,
                            "near_viewport": click_route == "dom_event"}
    return {"id": "locua.cua.browser_execution.v1", "server": deepcopy(server),
            "target": deepcopy(observation["target"]), "snapshot_id": observation["snapshot_id"],
            "observed_at_ns": observation["observed_at_ns"], "driver_contract": deepcopy(contract),
            "actions": actions}


class CuaAdapter:
    def __init__(self, owner, *, kind, target, expected_url=None, execute=False,
                 browser_click_route="trusted", ocr=False, regions=False):
        if kind not in ("browser_semantic_v2", "native_window_state"):
            raise ValueError("Unsupported Cua observation kind")
        if browser_click_route not in ("trusted", "dom_event"):
            raise ValueError("Unknown browser click route")
        self.owner, self.kind, self.target = owner, kind, dict(target)
        self.expected_url, self.execute = expected_url, execute
        self.browser_click_route, self.ocr = browser_click_route, ocr
        self.regions = regions
        self.sequence, self.latest = 0, None

    def observe(self):
        # A failed refresh must not leave the previous capture dispatchable.
        self.latest = None
        self.sequence += 1
        captured = time.time_ns()
        image_path = self.owner.directory / f"observation-{self.sequence:03d}.png"
        if self.kind == "browser_semantic_v2":
            record, payload = self.owner.call("get_browser_state", {**self.target, "snapshot_format": "semantic_v2"})
            if self.expected_url and payload.get("page", {}).get("url") != self.expected_url:
                raise RuntimeError("Browser navigation left the bound task URL")
        else:
            record, payload = self.owner.call("get_window_state", {**self.target, "include_screenshot": self.ocr,
                **({"screenshot_out_file": str(image_path)} if self.ocr else {})})
        schemas = {t["name"]: t["inputSchema"] for t in self.owner.inventory["tools"]}
        server = getattr(self.owner, "server", {}).get("serverInfo", {})
        # This is an explicit, version-bound inference from inspected source,
        # not proof the installed binary is reproducibly identical to that source.
        native_contract = (CUA_MACOS_0_28_2_CONTRACT if self.kind == "native_window_state"
                           and sys.platform == "darwin" and server == {"name": "cua-driver", "version": "0.28.2"}
                           else None)
        result = normalize_observation(record, kind=self.kind, expected_target=self.target,
                                       observed_at_ns=captured, tool_schemas=schemas, native_contract=native_contract)
        if self.kind == "browser_semantic_v2":
            execution = browser_execution_capabilities(result, server=server, schemas=schemas,
                                                       click_route=self.browser_click_route)
            if execution is not None:
                result["execution_capabilities"] = execution
        if self.regions and self.kind == "browser_semantic_v2":
            from .regions import bind_browser_hierarchy, CUA_BROWSER_0_28_2_CONTRACT
            result = bind_browser_hierarchy(result, source_contract=(CUA_BROWSER_0_28_2_CONTRACT
                if server == {"name": "cua-driver", "version": "0.28.2"} else None))
        if self.ocr and self.kind == "native_window_state":
            from .ocr import run_local_ocr, ground_ocr
            image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
            output = run_local_ocr(image_path, target=self.target, snapshot_id=result["snapshot_id"],
                                   captured_at_ns=captured, max_age_s=30)
            result["ocr"] = output
            transform = None
            if payload.get("screenshot_frame_valid") is True and payload.get("window_bounds"):
                if output["dimensions"] != {"width": payload.get("screenshot_width"), "height": payload.get("screenshot_height")}:
                    raise ValueError("OCR image dimensions differ from exact Cua capture")
                transform = {"target": self.target, "snapshot_id": result["snapshot_id"],
                    "image_sha256": image_hash, "dimensions": output["dimensions"],
                    "window_frame_screen_points": payload["window_bounds"]}
            result["ocr_grounding"] = ground_ocr(result, output, image_sha256=image_hash,
                                                  max_age_s=30, screenshot_transform=transform)
            # OCR remains evidence; it never creates executable controls by itself.
            result["text"] += "\nLOCAL OCR TEXT (untrusted; not an editable-control guarantee):\n" + json.dumps(
                [{"text": b["text"], "bounds": b["bounds"]} for b in output["boxes"]], ensure_ascii=False)
        self.latest = result
        return result

    def dispatch(self, action):
        self.owner.check()
        if not self.execute:
            raise RuntimeError("shadow_only_dispatch_disabled")
        if self.latest is None or action.get("snapshot_id") != self.latest["snapshot_id"]:
            raise ValueError("Action is not bound to the latest observation")
        handle = self.latest.get("handles", {}).get(action.get("control_id"))
        if handle != action.get("handle"):
            raise ValueError("Action handle differs from observation")
        if handle["kind"] == "browser":
            from .core import action_available, action_execution
            matches = [c for c in self.latest.get("controls", []) if c.get("id") == action.get("control_id")]
            if len(matches) != 1:
                raise ValueError("Browser action does not identify one observed control")
            control = matches[0]
            visibility = control.get("source", {}).get("node", {}).get("visibility",
                           control.get("states", {}).get("visibility", "in_viewport"))
            # Existing in-viewport legacy captures retain their old routes. Any
            # new capability and all non-visible controls require the full pin.
            if "execution_capabilities" in self.latest or visibility != "in_viewport":
                if (self.latest.get("target") != self.target
                        or not action_available(self.latest, control, action["kind"])):
                    raise ValueError("Browser action capability is unavailable")
                stamp = self.latest.get("observed_at_ns")
                if type(stamp) is not int or not 0 <= (time.time_ns() - stamp) / 1e9 <= 30:
                    raise ValueError("Browser execution capability is stale")
                execution = action_execution(self.latest, control, action["kind"])
                if execution is None or execution != action.get("execution"):
                    raise ValueError("Browser execution capability differs from the approved action")
                current = browser_execution_capabilities(self.latest,
                    server=getattr(self.owner, "server", {}).get("serverInfo", {}),
                    schemas={t["name"]: t["inputSchema"] for t in self.owner.inventory["tools"]},
                    click_route=self.browser_click_route)
                if current != self.latest.get("execution_capabilities"):
                    raise ValueError("Browser execution route or inventory changed after observation")
            args = {**self.target, "ref": handle["ref"]}
            if action["kind"] == "press":
                name, args = "browser_click", {**args, "input_route": self.browser_click_route}
            elif action["kind"] == "set_text":
                spin = action["signature"]["role"] == "spinbutton"
                if spin and action["signature"]["value"] not in (None, ""):
                    raise ValueError("Nonempty numeric replacement requires an unproved input route")
                name, args = "browser_type", {**args, "text": action["value"], "replace": not spin, "mode": "insert_text"}
            else:
                raise ValueError("Unsupported browser action")
        elif handle["kind"] == "native":
            args = {**self.target, "element_token": handle["element_token"]}
            if action["kind"] == "press":
                name = "click"
            elif action["kind"] == "set_text":
                name, args = "set_value", {**args, "value": action["value"]}
            else:
                raise ValueError("Unsupported native action")
        else:
            raise ValueError("OCR/static text is not an executable handle")
        _, result = self.owner.call(name, args)
        return result
