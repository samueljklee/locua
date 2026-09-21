"""Supplemental native editor evidence, separate from committed/saved content.

The helper only reads macOS AX. Its own Accessibility permission is required;
it never borrows Cua's identity, requests grants, or creates an action handle.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .perception import ObservationError, validate_observation

LAB = Path(__file__).resolve().parents[1]
DEFAULT_AX_BINARY = Path.home() / "Library/Caches/locua/native_ax_observer"
AX_SOURCE = Path(__file__).with_name("native_ax_observer.swift")
RAW_PROTOCOL = "locua.native_ax_raw.v2"
EDITOR_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
_FACTS = {"raw_value": str, "value_settable": bool, "selected_text_settable": bool,
          "selected_text": str, "selected_range": dict, "focused_attribute": bool,
          "focused_by_application": bool, "enabled": bool}


class EditorObservationError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _request_bytes(request):
    return json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _stamp(value, name):
    if type(value) is not int or value < 0:
        raise EditorObservationError(f"Invalid {name}")
    return value


def _age(captured, now, max_age_s):
    _stamp(captured, "timestamp"); _stamp(now, "now_ns")
    if type(max_age_s) not in (int, float) or not math.isfinite(max_age_s) or max_age_s < 0:
        raise EditorObservationError("Invalid age bound")
    if now < captured or now - captured > max_age_s * 1_000_000_000:
        raise EditorObservationError("Editor observation is stale or future-dated")


def _descriptor(control):
    semantics = control.get("semantics", {})
    return {"role": control["role"], "label": control.get("name"),
            **{key: semantics.get(key) for key in ("title", "description", "identifier")}}


def prepare_editor_request(observation, control_id, *, now_ns=None, max_age_s=10,
                           max_nodes=1200, max_depth=32, timeout_ms=8000):
    """Prepare an exact-target search; no UI or subprocess work occurs here."""
    now = time.time_ns() if now_ns is None else now_ns
    try:
        validate_observation(observation, expected_target=observation["target"], now_ns=now, max_age_s=max_age_s)
    except (ObservationError, KeyError) as error:
        raise EditorObservationError("Invalid source observation: " + str(error)) from error
    if observation["kind"] != "native_window_state":
        raise EditorObservationError("Supplemental AX supports native observations only")
    controls = {c["id"]: c for c in observation["controls"]}
    control = controls.get(control_id)
    handle = observation.get("handles", {}).get(control_id)
    if control is None or handle is None or control["role"] not in EDITOR_ROLES:
        raise EditorObservationError("An actual addressed native text control is required")
    bounds = control.get("bounds")
    if not isinstance(bounds, dict) or bounds.get("coordinate_space") != "screen_points":
        raise EditorObservationError("Observed native screen-point bounds are required")
    frame = {key: bounds.get(key) for key in ("x", "y", "width", "height")}
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in frame.values()) or frame["width"] <= 0 or frame["height"] <= 0:
        raise EditorObservationError("Editor frame must be finite and positive")
    selector = {**_descriptor(control), "frame": frame, "ancestors": []}
    if not any(isinstance(selector[k], str) and selector[k] for k in ("label", "title", "description", "identifier")):
        raise EditorObservationError("Role and geometry alone do not establish editor identity")
    parent, seen = control.get("parent"), {control_id}
    while parent is not None:
        if parent in seen or parent not in controls:
            raise EditorObservationError("Editor ancestry is cyclic or unresolved")
        seen.add(parent)
        row = controls[parent]
        descriptor = _descriptor(row)
        # Match the actual captured chain as an ordered subsequence. Cua can
        # collapse empty layout containers that the supplemental walk retains.
        selector["ancestors"].append(descriptor)
        parent = row.get("parent")
    for key, value, upper in (("max_nodes", max_nodes, 4000), ("max_depth", max_depth, 64), ("timeout_ms", timeout_ms, 10000)):
        if type(value) is not int or not 1 <= value <= upper:
            raise EditorObservationError(f"Invalid {key}")
    return {"schema": "locua.native_ax_request.v1", "target": deepcopy(observation["target"]),
            "snapshot_id": observation["snapshot_id"], "control_id": control_id,
            "cua_handle": deepcopy(handle), "observed_at_ns": observation["observed_at_ns"],
            "selector": selector, "selector_sha256": _digest(selector),
            "limits": {"max_nodes": max_nodes, "max_depth": max_depth, "timeout_ms": timeout_ms}}


def _descriptor_matches(actual, wanted):
    if not isinstance(actual, dict):
        return False
    return all(not isinstance(value, str) or actual.get(key) == value for key, value in wanted.items()
               if key in ("role", "label", "title", "description", "identifier"))


def _identity_matches(actual, selector):
    if not isinstance(actual, dict) or not _descriptor_matches(actual, selector):
        return False
    frame = actual.get("frame", {})
    if not isinstance(frame, dict):
        return False
    for key, expected in selector["frame"].items():
        value = frame.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value - expected) > .5:
            return False
    ancestors = actual.get("ancestors")
    if not isinstance(ancestors, list):
        return False
    cursor = 0
    for wanted in selector["ancestors"]:
        while cursor < len(ancestors) and not _descriptor_matches(ancestors[cursor], wanted):
            cursor += 1
        if cursor == len(ancestors):
            return False
        cursor += 1
    return True


def _validate_request(request):
    if not isinstance(request, dict) or request.get("schema") != "locua.native_ax_request.v1":
        raise EditorObservationError("Invalid editor request")
    target, handle = request.get("target", {}), request.get("cua_handle", {})
    if not isinstance(target, dict) or not isinstance(handle, dict):
        raise EditorObservationError("Invalid editor request address")
    for key in ("pid", "window_id"):
        if type(target.get(key)) is not int or target[key] <= 0 or type(handle.get(key)) is not int or handle[key] != target[key]:
            raise EditorObservationError("Request handle targets another surface")
    snapshot, index = request.get("snapshot_id"), handle.get("element_index")
    if (not isinstance(snapshot, str) or not snapshot or type(index) is not int or index < 0
            or handle.get("kind") != "native" or handle.get("snapshot_id") != snapshot
            or handle.get("element_token") != f"{snapshot}:{index}"
            or request.get("control_id") != f"native:{snapshot}:{index}"
            or "session" in target and handle.get("session") != target["session"]):
        raise EditorObservationError("Request handle identity disagrees")
    selector = request.get("selector", {})
    if not isinstance(selector, dict) or selector.get("role") not in EDITOR_ROLES or request.get("selector_sha256") != _digest(selector):
        raise EditorObservationError("Invalid native editor selector")
    frame = selector.get("frame")
    if not isinstance(frame, dict) or set(frame) != {"x", "y", "width", "height"} or any(type(v) not in (int, float) or not math.isfinite(v) for v in frame.values()) or frame["width"] <= 0 or frame["height"] <= 0:
        raise EditorObservationError("Invalid request geometry")
    if not any(isinstance(selector.get(k), str) and selector[k] for k in ("label", "title", "description", "identifier")) or not isinstance(selector.get("ancestors"), list):
        raise EditorObservationError("Missing editor semantic identity")
    if any(not isinstance(a, dict) or not isinstance(a.get("role"), str) for a in selector["ancestors"]):
        raise EditorObservationError("Invalid ancestor selector")
    limits = request.get("limits", {})
    if not isinstance(limits, dict) or set(limits) != {"max_nodes", "max_depth", "timeout_ms"}:
        raise EditorObservationError("Invalid request limits")
    for key, maximum in (("max_nodes", 4000), ("max_depth", 64), ("timeout_ms", 10000)):
        if type(limits[key]) is not int or not 1 <= limits[key] <= maximum:
            raise EditorObservationError("Unbounded supplemental request")
    _stamp(request.get("observed_at_ns"), "source capture timestamp")


def _fact(raw, name):
    if not isinstance(raw, dict):
        raise EditorObservationError("AX facts must be an object")
    item = raw.get(name)
    if not isinstance(item, dict) or item.get("status") not in ("ok", "unsupported", "error"):
        raise EditorObservationError(f"Invalid AX fact {name}")
    if type(item.get("ax_error")) is not int:
        raise EditorObservationError(f"Missing AX result code for {name}")
    if item["status"] != "ok":
        if item.get("value") is not None:
            raise EditorObservationError(f"Unknown AX fact carries a value: {name}")
        return None
    if item["ax_error"] != 0 or type(item.get("value")) is not _FACTS[name]:
        raise EditorObservationError(f"AX fact type/result mismatch: {name}")
    return item["value"]


def _selection_coherence(values):
    """Compare literal UTF-16 ranges without normalizing Unicode or whitespace."""
    selected_range, text, selected = (values[k] for k in ("selected_range", "raw_value", "selected_text"))
    if selected_range is None:
        return None
    if (set(selected_range) != {"location", "length", "unit"}
            or selected_range.get("unit") != "utf16_code_units"
            or any(type(selected_range.get(k)) is not int or selected_range[k] < 0
                   for k in ("location", "length"))):
        raise EditorObservationError("Invalid native selection range")
    if text is None:
        return None
    try:
        encoded = text.encode("utf-16-le")
        start, end = selected_range["location"] * 2, (selected_range["location"] + selected_range["length"]) * 2
        if end > len(encoded):
            raise EditorObservationError("Selection range exceeds observed raw value")
        # Empty ranges must also fall on valid scalar boundaries.
        encoded[:start].decode("utf-16-le")
        encoded[end:].decode("utf-16-le")
        literal = encoded[start:end].decode("utf-16-le")
    except UnicodeError as error:
        raise EditorObservationError("Selection splits a UTF-16 scalar or raw value is invalid Unicode") from error
    if selected is None:
        return None
    if literal != selected:
        raise EditorObservationError("Selected text disagrees with the raw-value UTF-16 range")
    return True


def _stable_reads(raw, match, request, start, end):
    if raw["schema"] != RAW_PROTOCOL:
        return None
    stable = raw.get("stability")
    if not isinstance(stable, dict) or stable.get("method") != "bracketed_attribute_reads":
        raise EditorObservationError("Raw v2 requires before/after editor evidence")
    samples = [stable.get("before"), stable.get("after")]
    stamps = []
    for sample in samples:
        if not isinstance(sample, dict) or not _identity_matches(sample.get("identity"), request["selector"]):
            raise EditorObservationError("Editor identity changed during bracketed reads")
        facts = sample.get("facts", {})
        values = {key: _fact(facts, key) for key in _FACTS}
        _selection_coherence(values)
        stamps.append(_stamp(sample.get("observed_at_ns"), "attribute sample"))
    if not start <= stamps[0] <= stamps[1] <= end:
        raise EditorObservationError("Attribute sample clock ordering failed")
    if samples[0]["identity"] != samples[1]["identity"] or samples[0]["identity"] != match["identity"]:
        raise EditorObservationError("Editor identity changed during bracketed reads")
    if samples[0]["facts"] != samples[1]["facts"] or samples[0]["facts"] != match["facts"]:
        raise EditorObservationError("Editor attributes changed during bracketed reads")
    return True


def bind_editor_result(raw, request, *, received_at_ns, max_age_s=10):
    """Bind successful raw attributes to the exact request; refusals never bind."""
    _validate_request(request)
    if raw.get("schema") not in ("locua.native_ax_raw.v1", RAW_PROTOCOL) or raw.get("status") != "observed":
        raise EditorObservationError("Supplemental observation refused: " + str(raw.get("reason", "unavailable")))
    if raw.get("read_only") is not True or raw.get("permission_prompted") is not False:
        raise EditorObservationError("Unexpected observer behavior")
    if raw.get("provider") != "local_macos_ax_supplement":
        raise EditorObservationError("Unexpected supplemental provider")
    for key in ("target", "snapshot_id", "control_id", "selector_sha256"):
        if _digest(raw.get(key)) != _digest(request.get(key)):
            raise EditorObservationError(f"Supplemental identity mismatch: {key}")
    if raw.get("request_sha256") != hashlib.sha256(_request_bytes(request)).hexdigest():
        raise EditorObservationError("Supplemental request digest mismatch")
    start, end = _stamp(raw.get("started_at_ns"), "start"), _stamp(raw.get("finished_at_ns"), "end")
    _age(request["observed_at_ns"], received_at_ns, max_age_s)
    if not request["observed_at_ns"] <= start <= end <= received_at_ns:
        raise EditorObservationError("Supplemental observation clock ordering failed")
    traversal = raw.get("traversal", {})
    if traversal.get("complete") is not True:
        raise EditorObservationError("Incomplete supplemental search cannot establish uniqueness")
    visited = traversal.get("visited")
    if type(visited) is not int or not 1 <= visited <= request["limits"]["max_nodes"]:
        raise EditorObservationError("Invalid supplemental traversal count")
    matches = raw.get("matches")
    if not isinstance(matches, list) or len(matches) != 1:
        raise EditorObservationError("Editor match is missing or ambiguous")
    match = matches[0]
    if not _identity_matches(match.get("identity"), request["selector"]):
        raise EditorObservationError("Matched editor does not satisfy request identity")
    facts = match.get("facts", {})
    values = {key: _fact(facts, key) for key in _FACTS}
    selected_range = values["selected_range"]
    selection_coherent = _selection_coherence(values)
    stable = _stable_reads(raw, match, request, start, end)
    focus = values["focused_by_application"]
    conflict = focus is not None and values["focused_attribute"] is not None and focus != values["focused_attribute"]
    if conflict:
        focus = None
    value_fact = {"plane": "editor_buffer", "precision": "exact" if values["raw_value"] is not None else "unknown",
                  "value": values["raw_value"], "source_attribute": "AXValue",
                  "placeholder_substitution": False, "trimmed": False,
                  "committed_document_proven": False, "saved_file_proven": False}
    return {"schema": "locua.bound_editor_observation.v1", "status": "observed",
            "target": deepcopy(request["target"]), "snapshot_id": request["snapshot_id"],
            "control_id": request["control_id"], "cua_handle": deepcopy(request["cua_handle"]),
            "selector_sha256": request["selector_sha256"], "observed_at_ns": end,
            "cua_observed_at_ns": request["observed_at_ns"], "received_at_ns": received_at_ns,
            "identity": deepcopy(match["identity"]), "value": value_fact,
            "writable": {"AXValue": values["value_settable"], "AXSelectedText": values["selected_text_settable"]},
            "focus": {"focused": focus, "conflicting_sources": conflict},
            "selection": {"text": values["selected_text"], "range": deepcopy(selected_range)},
            "coherence": {"before_after_stable": stable, "selection_matches_buffer": selection_coherent,
                          "atomic_snapshot": False,
                          "legacy_protocol": raw["schema"] != RAW_PROTOCOL},
            "enabled": values["enabled"],
            "planes": {"display": "caller_cua_observation", "editor_buffer": value_fact["precision"],
                       "committed_document": "unknown", "saved_file": "unknown"},
            "creates_action_handle": False, "authorizes_mutation": False,
            "provenance": {"provider": "local_macos_ax_supplement", "clock": "unix_time_ns",
                           "match_basis": "unique_role_semantics_ancestry_frame_in_AXChildren",
                           "cua_token_consumed_by_helper": False, "same_AX_object_as_Cua_proven": False,
                           "raw": deepcopy(raw), "request": deepcopy(request)}}


def validate_editor_observation(bound, observation, control_id, *, now_ns=None, max_age_s=10):
    now = time.time_ns() if now_ns is None else now_ns
    request = prepare_editor_request(observation, control_id, now_ns=now, max_age_s=max_age_s,
                                     **bound.get("provenance", {}).get("request", {}).get("limits", {}))
    if bound.get("provenance", {}).get("request") != request:
        raise EditorObservationError("Editor evidence belongs to a different or changed observation")
    _age(bound.get("observed_at_ns"), now, max_age_s)
    rebuilt = bind_editor_result(bound["provenance"]["raw"], request,
                                 received_at_ns=bound["received_at_ns"], max_age_s=max_age_s)
    if bound != rebuilt:
        raise EditorObservationError("Supplemental editor evidence was altered")


def compile_ax_observer(binary_path=DEFAULT_AX_BINARY, *, swiftc="/usr/bin/swiftc", timeout_s=60):
    if sys.platform != "darwin":
        raise EditorObservationError("Supplemental native AX is Mac-only")
    path = Path(binary_path); path.parent.mkdir(parents=True, exist_ok=True)
    cache = path.parent / "ax-swift-cache"; cache.mkdir(exist_ok=True)
    run = subprocess.run([swiftc, "-O", "-module-cache-path", str(cache), str(AX_SOURCE), "-o", str(path)],
                         capture_output=True, text=True, timeout=timeout_s)
    if run.returncode:
        raise EditorObservationError("AX observer compilation failed: " + run.stderr[:2000])
    manifest = {"schema": "locua.native_ax_build.v1", "observation_protocol": RAW_PROTOCOL,
                "source_sha256": hashlib.sha256(AX_SOURCE.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "binary_path": str(path.resolve()), "permission_identity_stability_proven": False}
    path.with_suffix(path.suffix + ".build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def editor_doctor(*, binary_path=DEFAULT_AX_BINARY, timeout_s=5):
    result = {"available": False, "platform_supported": sys.platform == "darwin",
              "binary_exists": Path(binary_path).is_file(), "permission_prompted": False,
              "actions_supported": False, "ax_permission": "unknown"}
    if not result["platform_supported"] or not result["binary_exists"]:
        result["reason"] = "platform_unsupported" if not result["platform_supported"] else "helper_not_compiled"
        return result
    try:
        binary = Path(binary_path)
        binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
        source_sha = hashlib.sha256(AX_SOURCE.read_bytes()).hexdigest()
        manifest_file = binary.with_suffix(binary.suffix + ".build.json")
        manifest = json.loads(manifest_file.read_text()) if manifest_file.is_file() else None
        manifest_matches = bool(isinstance(manifest, dict) and manifest.get("schema") == "locua.native_ax_build.v1"
            and manifest.get("source_sha256") == source_sha and manifest.get("binary_sha256") == binary_sha
            and manifest.get("observation_protocol") == RAW_PROTOCOL
            and manifest.get("binary_path") == str(binary.resolve()))
        result["helper_identity"] = {"binary_path": str(binary.resolve()), "binary_sha256": binary_sha,
            "source_sha256": source_sha, "build_manifest_matches": manifest_matches,
            "permission_identity_stability_proven": False}
        run = subprocess.run([str(binary_path), "doctor"], capture_output=True, text=True, timeout=timeout_s)
        raw = json.loads(run.stdout)
        if (run.returncode or raw.get("schema") != "locua.native_ax_doctor.v1"
                or raw.get("permission_prompted") is not False or raw.get("read_only") is not True
                or raw.get("actions_supported") is not False or raw.get("network_used") is not False):
            raise EditorObservationError("Invalid doctor response")
        if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_sha:
            raise EditorObservationError("Observer binary changed during doctor")
        trusted = raw.get("accessibility_trusted")
        protocol_matches = raw.get("observation_protocol") == RAW_PROTOCOL
        available = trusted is True and raw.get("window_identity_spi_available") is True and protocol_matches and manifest_matches
        reason = ("accessibility_permission_missing" if trusted is False else
                  "accessibility_permission_unknown" if trusted is not True else
                  "observer_protocol_or_build_unverified" if not protocol_matches or not manifest_matches else
                  "window_identity_spi_unavailable" if raw.get("window_identity_spi_available") is not True else "ready")
        result.update(ax_permission="granted" if trusted is True else "missing" if trusted is False else "unknown",
                      available=available, raw=raw, reason=reason)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        result["reason"] = "doctor_failed:" + type(error).__name__
    return result


def observe_native_editor(observation, control_id, *, binary_path=DEFAULT_AX_BINARY,
                          timeout_s=15, max_age_s=10, output_path=None):
    request = prepare_editor_request(observation, control_id, max_age_s=max_age_s)
    if sys.platform != "darwin" or not Path(binary_path).is_file():
        raise EditorObservationError("Compiled macOS supplemental helper unavailable")
    binary_sha = hashlib.sha256(Path(binary_path).read_bytes()).hexdigest()
    # A private request file carries only the targeted control/ancestor evidence.
    with tempfile.TemporaryDirectory(prefix="locua-ax-") as directory:
        request_path = Path(directory) / "request.json"
        payload = _request_bytes(request)
        request_path.write_bytes(payload); request_path.chmod(0o600)
        try:
            run = subprocess.run([str(binary_path), "observe", str(request_path)], capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            raise EditorObservationError("Supplemental observer timed out; no mutations were requested") from error
        received = time.time_ns()
        raw = json.loads(run.stdout)
        if run.returncode or raw.get("request_sha256") != hashlib.sha256(payload).hexdigest():
            raise EditorObservationError("Supplemental helper failed or request bytes disagree")
        if raw.get("schema") != RAW_PROTOCOL:
            raise EditorObservationError("Runtime editor observation requires the bracketed-read v2 helper")
        if hashlib.sha256(Path(binary_path).read_bytes()).hexdigest() != binary_sha:
            raise EditorObservationError("Supplemental binary changed while observing")
        bound = bind_editor_result(raw, request, received_at_ns=received, max_age_s=max_age_s)
        # Runtime evidence fields are stored separately from the pure binding.
        if output_path is not None:
            path = Path(output_path)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                json.dump({"binding": bound, "binary_sha256": binary_sha}, file, ensure_ascii=False, indent=2)
        return bound
