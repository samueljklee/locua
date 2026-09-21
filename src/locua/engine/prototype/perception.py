"""Loss-conscious adapters for Cua 0.28.2 observations, without task filtering.

Observation timestamps use time.time_ns() (Unix wall-clock nanoseconds).
Callers retain the complete raw response in their trace; this module retains
the original nodes and hashes that response but is not a replacement trace.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time
from typing import Any, Mapping
from .driver_contract import check_contract, CONTRACT_ID
from .native_driver_contract import check_native_editor_contract, CONTRACT_ID as NATIVE_EDITOR_CONTRACT


class ObservationError(ValueError):
    """An observation cannot safely establish the requested identity."""


class NativeObservationUnavailable(ObservationError):
    """The driver deliberately withheld an executable exact-window AX capture."""
    def __init__(self, payload, target, raw_hash):
        background = payload.get("background_input")
        exact = background.get("exact_window", {}) if isinstance(background, dict) else {}
        unresolved = isinstance(exact, dict) and exact.get("status") == "ax_unresolved"
        self.code = "native_accessibility_unavailable" if unresolved else "native_observation_degraded"
        message = ("The driver can see this window, but cannot read its accessibility controls."
                   if unresolved else "The driver returned an incomplete native capture that cannot be used for control.")
        self.remedy = ("Bring the selected window onto the current desktop and let it finish loading, then retry. "
                       "If it remains unavailable, choose another window; this driver has no verified control route for the selected window.")
        self.details = {"target": deepcopy(target), "degraded_reason": payload.get("degraded_reason"),
                        "background_input": deepcopy(background), "raw_sha256": raw_hash,
                        "executable_snapshot_accepted": False, "recovery_guaranteed": False}
        super().__init__(message)


_KINDS = {"native": "native", "native_window_state": "native",
          "browser": "browser", "browser_semantic_v2": "browser", "semantic_v2": "browser"}
_NATIVE_LINE = re.compile(r"^(\s*)-\s+(?:\[(\d+)\]\s+)?(AX\w+)\b(.*)$")
_OUTLINE_LINE = re.compile(r"^(\s*)-\s+(\S+)(.*)$")
_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
_VALUE_ADDRESSED_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}
CUA_MACOS_0_28_2_CONTRACT = {
    "driver": "cua-driver", "version": "0.28.2", "platform": "macos",
    "source_revision": "9e60d90b8681d3ba7ccf2c7801dbaa21b0d6efbb",
}
_SEMANTIC_ATTRIBUTES = {"title": "AXTitle", "description": "AXDescription",
                        "help": "AXHelp", "identifier": "AXIdentifier",
                        "value_description": "AXValueDescription"}


def _native_contract(contract: Mapping | None) -> dict | None:
    """Require an explicit caller attestation; tool inventory is not a version pin."""
    if contract is None:
        return None
    if not isinstance(contract, Mapping) or dict(contract) != CUA_MACOS_0_28_2_CONTRACT:
        raise ObservationError("Unsupported native source contract")
    return deepcopy(dict(contract))


def _markdown_semantics(line: str) -> tuple[dict, list[str]]:
    """Parse only unambiguous rows from Cua's unescaped presentation grammar.

    The producer does not JSON-escape AX strings. Embedded quotes or ambiguous
    metadata boundaries therefore remain unparsed, rather than acquiring a
    plausible but invented semantic selector. Raw source lines are retained.
    """
    match = _NATIVE_LINE.fullmatch(line)
    if not match:
        return {}, ["native_markdown_syntax_unparsed"]
    tail = match.group(4)
    fields = {}
    for key, prefix in (("title", ' "'), ("value", ' = "')):
        if tail.startswith(prefix):
            end = tail.find('"', len(prefix))
            if end < 0:
                return {}, ["native_markdown_unescaped_quote_ambiguity"]
            fields[key] = tail[len(prefix):end]
            tail = tail[end + 1:]
    # Consider every possible metadata boundary: the same text can occur in an
    # AXDescription, so choosing the first/last one alone would be unsound.
    boundaries = [len(tail)] + [m.start() for m in re.finditer(r" \[", tail)]
    candidates = []
    metadata_pattern = re.compile(
        r'\[(?:id=(?P<identifier>[^\s\[\]"]+)(?: |(?=\])))?'
        r'(?:help="(?P<help>[^"\r\n]*)"(?: |(?=\])))?'
        r'(?:actions=\[(?P<actions>[^\[\]\r\n]*)\])?\]')
    for boundary in boundaries:
        description, metadata = tail[:boundary], tail[boundary:]
        parsed = dict(fields)
        if description:
            if not description.startswith(" (") or not description.endswith(")"):
                continue
            inner = description[2:-1]
            depth = 0
            balanced = True
            for char in inner:
                depth += (char == "(") - (char == ")")
                if depth < 0:
                    balanced = False
            if not balanced or depth or any(c in inner for c in '\r\n"'):
                continue
            parsed["description"] = inner
        if metadata:
            attrs = metadata_pattern.fullmatch(metadata[1:])
            if attrs is None:
                continue
            parsed.update({key: value for key, value in attrs.groupdict().items()
                           if key != "actions" and value is not None})
        candidates.append(parsed)
    if len(candidates) != 1:
        return {}, ["native_markdown_syntax_ambiguous_or_unparsed"]
    return candidates[0], []


def _native_semantics(controls: list[dict]) -> None:
    """Add provenance-bearing strings and observed ancestry, never state guesses."""
    for control in controls:
        source = control["source"]
        node = source.get("node", {})
        line = source.get("markdown_line", source.get("line"))
        parsed, warnings = _markdown_semantics(line) if line is not None else ({}, [])
        semantics = {key: None for key in _SEMANTIC_ATTRIBUTES}
        evidence = {}
        for key, attribute in _SEMANTIC_ATTRIBUTES.items():
            structured = node.get(key)
            displayed = parsed.get(key)
            if structured is not None and not isinstance(structured, str):
                warnings.append(f"nontext_native_semantic:{key}")
                continue
            if structured is not None and displayed is not None and structured != displayed:
                warnings.append(f"native_semantic_disagreement:{key}")
                continue
            if structured is not None:
                semantics[key] = structured
                evidence[key] = {"kind": "native_structured", "field": key,
                                 "attribute": attribute}
            elif displayed is not None:
                semantics[key] = displayed
                evidence[key] = {"kind": "native_markdown", "attribute": attribute,
                                 "line_number": source.get("markdown_line_number", source.get("line_number"))}
        # Cua's label is the first present title/description/value/identifier
        # (including an empty title). Check the whole fallback order, not just
        # titled rows, before attributing parsed selector facts to a handle.
        presented_label = next((parsed[key] for key in ("title", "description", "value", "identifier")
                                if key in parsed), None)
        if presented_label is not None and "label" in node and node["label"] != presented_label:
            warnings.append("native_markdown_label_disagreement")
            for key in list(evidence):
                if evidence[key]["kind"] == "native_markdown":
                    semantics[key] = None
                    del evidence[key]
        semantics.update(ancestors=[], evidence_sources=evidence, warnings=warnings)
        control["semantics"] = semantics
    by_id = {c["id"]: c for c in controls}
    for control in controls:
        parent = control.get("parent")
        seen = {control["id"]}
        while parent is not None:
            if parent in seen:
                control["semantics"]["warnings"].append("native_ancestry_cycle")
                break
            seen.add(parent)
            ancestor = by_id.get(parent)
            if ancestor is None:
                control["semantics"]["warnings"].append("native_ancestor_unavailable")
                break
            attrs = ancestor["semantics"]
            control["semantics"]["ancestors"].append({
                "id": parent, "role": ancestor["role"], "name": ancestor.get("name"),
                "description": attrs["description"], "help": attrs["help"],
                "identifier": attrs["identifier"], "source_kind": ancestor["source"]["kind"],
            })
            parent = ancestor.get("parent")


def _native_value_evidence(control: dict) -> dict:
    source = control["source"]
    editor = source.get("node", {}).get("editor", {})
    raw = editor.get("raw_value", {})
    if (check_native_editor_contract(source.get("native_editor_contract"))
            and editor.get("contract") == NATIVE_EDITOR_CONTRACT and editor.get("plane") == "editor_buffer"
            and raw.get("status") == "ok" and isinstance(raw.get("value"), str)
            and editor.get("coherence", {}).get("value_stable") is True):
        return {"kind": "native_raw_editor_value", "precision": "exact", "exact_value_proven": True,
                "plane": "editor_buffer", "basis": "same_handle_AXValue_recheck",
                "contract": deepcopy(source["native_editor_contract"]), "atomic_capture": False,
                "committed_document_proven": False, "saved_output_proven": False}
    line = source.get("markdown_line", source.get("line"))
    parsed, _ = _markdown_semantics(line) if line is not None else ({}, [])
    pinned = source.get("native_contract") == CUA_MACOS_0_28_2_CONTRACT
    return {
        "kind": "native_display_value", "precision": "display_only", "exact_value_proven": False,
        "structured_value_trimmed": True if pinned else None,
        "possible_placeholder": True if pinned else None,
        "markdown_value": parsed.get("value"),
        "markdown_is_exact_attribute_read": False,
        "basis": "pinned_cua_display_projection" if pinned else "raw_attribute_provenance_unavailable",
        "source": "platform-macos/src/ax/tree.rs:409-420,471-475; tools/get_window_state.rs:863-869" if pinned else None,
        "contract": deepcopy(source.get("native_contract")),
        "binary_equivalence_proven": False,
    }


def _json_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ObservationError("Observation must be finite JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ObservationError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObservationError(f"{label} must be a nonempty string")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservationError(f"{label} must be an integer >= {minimum}")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ObservationError(f"{label} must be an array of strings")
    return list(value)


def _unwrap(raw: dict) -> tuple[dict, dict]:
    current = _object(raw, "response")
    request = current.get("request", {})
    for _ in range(6):
        if current.get("ok") is False or current.get("isError") is True:
            raise ObservationError("Cua returned an error, not an observation")
        if current.get("refusal") is not None:
            raise ObservationError("Cua returned a refusal, not an observation")
        if "response" in current:
            current = _object(current["response"], "response wrapper")
        elif "structuredContent" in current:
            current = _object(current["structuredContent"], "structuredContent")
        elif "result" in current and not any(k in current for k in ("elements", "refs", "snapshot")):
            current = _object(current["result"], "result")
        else:
            return current, request if isinstance(request, dict) else {}
    raise ObservationError("Too many observation wrappers")


def _target(payload: dict, expected: Mapping, kind: str, request: dict) -> dict:
    if not isinstance(expected, Mapping):
        raise ObservationError("expected_target must be an object")
    fields = ("pid", "window_id") if kind == "native" else ("target_id", "tab_id")
    target = deepcopy(dict(expected))
    for field in fields:
        actual = payload.get(field)
        if kind == "native":
            _integer(actual, field, 1)
        else:
            _string(actual, field)
        if field not in expected or type(actual) is not type(expected[field]) or actual != expected[field]:
            raise ObservationError(f"Target mismatch for {field}")
        target[field] = actual
    arguments = request.get("arguments", {})
    if isinstance(arguments, dict):
        for key in (*fields, "session"):
            if key in arguments and key in expected and arguments[key] != expected[key]:
                raise ObservationError(f"Request context mismatch for {key}")
    # Optional binding facts, when supplied by Cua, must not contradict the caller.
    for key in ("pid", "window_id", "target_id", "tab_id", "session"):
        if key in payload and key in expected and payload[key] != expected[key]:
            raise ObservationError(f"Observed context mismatch for {key}")
    if payload.get("binding_quality") not in (None, "exact"):
        raise ObservationError("Browser binding is not exact")
    return target


def _bounds(raw: Any, coordinate_space: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ObservationError("Bounds must be an object")
    values = {"x": raw.get("x"), "y": raw.get("y"),
              "width": raw.get("width", raw.get("w")),
              "height": raw.get("height", raw.get("h"))}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in values.values()):
        raise ObservationError("Bounds must contain finite numeric geometry")
    if values["width"] < 0 or values["height"] < 0:
        raise ObservationError("Bounds must not have negative size")
    return {**values, "coordinate_space": raw.get("coordinate_space", coordinate_space)}


def _markdown_fields(tail: str) -> tuple[str | None, str | None]:
    """Conservative presentation parsing; original line remains authoritative."""
    parsed, _ = _markdown_semantics("- AXUnknown" + tail)
    # Do not split composite native descriptions into invented labels/values.
    return parsed.get("title", parsed.get("description")), parsed.get("value")


def _native(payload: dict, target: dict, snapshot: str) -> tuple[list, dict, list, dict]:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise ObservationError("Native elements must be an array")
    controls, handles, indexed = [], {}, {}
    warnings = []
    for node in elements:
        node = _object(node, "native element")
        index = _integer(node.get("element_index"), "element_index")
        if index in indexed:
            raise ObservationError("Duplicate native element identity")
        role = _string(node.get("role"), "native role")
        identifier = f"native:{snapshot}:{index}"
        states = deepcopy(node.get("states", {}))
        _object(states, "native states")
        for key in ("enabled", "selected", "checked", "focused", "editable", "value_settable", "selected_text_settable"):
            if key in node:
                states[key] = deepcopy(node[key])
        if role in {"AXCheckBox", "AXRadioButton"}:
            value = node.get("value")
            if "checked" not in states and (value in ("0", "1") or type(value) is int and value in (0, 1)):
                states["checked"] = str(value) == "1"
        parent = node.get("parent_index")
        if parent is not None:
            _integer(parent, "parent_index")
        control = {"id": identifier, "role": role, "name": node.get("label"),
                   "value": deepcopy(node.get("value")), "states": states,
                   "actions": _strings(node.get("actions", []), "native actions"),
                   "parent": f"native:{snapshot}:{parent}" if parent is not None else None,
                   "bounds": _bounds(node.get("frame"), "screen_points"),
                   "source": {"kind": "native", "node": deepcopy(node)}}
        token = node.get("element_token")
        if token is not None:
            _string(token, "element_token")
            # Pinned Cua publishes its snapshot/index pair in each token. Do not
            # mint tokens; reject a contradictory pair supplied in one response.
            if re.fullmatch(r"s[0-9a-fA-F]+:\d+", token) and token != f"{snapshot}:{index}":
                raise ObservationError("Element token contradicts native snapshot/index")
        handle = {"kind": "native", "pid": target["pid"], "window_id": target["window_id"],
                  "snapshot_id": snapshot, "element_index": index}
        if token is not None:
            handle["element_token"] = token
        if "session" in target:
            handle["session"] = target["session"]
        controls.append(control)
        handles[identifier] = handle
        indexed[index] = control
    markdown = payload.get("tree_markdown", "")
    if not isinstance(markdown, str):
        raise ObservationError("tree_markdown must be text")
    hierarchy, stack, seen = [], [], set()
    for number, line in enumerate(markdown.splitlines(), 1):
        match = _NATIVE_LINE.match(line)
        if not match:
            continue
        indent, index_text, role, tail = match.groups()
        depth = len(indent.expandtabs(4))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        index = int(index_text) if index_text is not None else None
        control = indexed.get(index)
        if control is not None and control["role"] != role:
            raise ObservationError("Native markdown/structured role disagreement")
        if control is not None and control["id"] not in seen:
            control["parent"] = parent_id
            control["source"]["markdown_line"] = line
            control["source"]["markdown_line_number"] = number
            seen.add(control["id"])
        elif control is None:
            name, value = _markdown_fields(tail)
            control = {"id": f"native:markdown:{number}", "role": role, "name": name,
                       "value": value, "states": {}, "actions": [], "parent": parent_id,
                       "bounds": None,
                       "source": {"kind": "native_markdown", "line": line,
                                  "line_number": number, "observed_index": index}}
            controls.append(control)
        else:
            warnings.append(f"Repeated markdown identity on line {number}")
        hierarchy.append({"id": f"native:line:{number}", "control_id": control["id"],
                          "parent_control_id": parent_id, "indent": depth, "raw": line})
        stack.append((depth, control["id"]))
    known = {c["id"] for c in controls}
    unresolved = [c["id"] for c in controls if c["parent"] is not None and c["parent"] not in known]
    complete = payload.get("elements_complete")
    if complete is not None and not isinstance(complete, bool):
        raise ObservationError("elements_complete must be boolean or null")
    coverage = {"complete": complete, "scope": "native_window",
                "structured_actionable_only": True, "static_markdown_retained": bool(markdown),
                "raw_element_count": len(elements), "normalized_control_count": len(controls),
                "hierarchy_scope": "rendered_native_tree_with_collapsed_containers",
                "unresolved_parent_ids": unresolved, "parse_warnings": warnings}
    for key in ("truncated", "element_count", "total_element_count", "returned_element_count",
                "filtered_element_count", "degraded", "degraded_reason", "background_input",
                "warnings", "warning", "truncation", "max_elements", "max_depth"):
        if key in payload:
            coverage[key] = deepcopy(payload[key])
    _native_semantics(controls)
    return controls, handles, hierarchy, coverage


def _browser(payload: dict, target: dict, snapshot: str) -> tuple[list, dict, list, dict]:
    controls, handles, known = [], {}, set()
    contract = payload.get("snapshot", {}).get("driver_contract", {})
    repaired = check_contract(contract)
    if contract.get("id") == CONTRACT_ID and not repaired:
        raise ObservationError("Experimental browser contract fingerprint/semantics mismatch")
    for collection in ("refs", "content_refs"):
        rows = payload.get(collection)
        if not isinstance(rows, list):
            raise ObservationError(f"Browser {collection} must be an array")
        for node in rows:
            node = _object(node, "browser node")
            ref = _string(node.get("ref"), "browser ref")
            if ref in known:
                raise ObservationError("Duplicate browser ref")
            known.add(ref)
            if re.fullmatch(r"p\d+:\d+", ref) and not ref.startswith(snapshot + ":"):
                raise ObservationError("Browser ref contradicts snapshot")
            identifier = f"browser:{ref}"
            states = deepcopy(_object(node.get("states", {}), "browser states"))
            # CDP AX exposes ARIA tristates as strings. Preserve the raw source
            # and normalize only unambiguous Boolean spellings, never "mixed".
            for state_key in ("checked", "selected"):
                if states.get(state_key) in ("true", "false"):
                    states[state_key] = states[state_key] == "true"
            if "visibility" in node:
                states["visibility"] = node["visibility"]
            parent = node.get("parent_ref")
            if parent is not None:
                _string(parent, "parent_ref")
            control = {"id": identifier, "role": _string(node.get("role"), "browser role"),
                       "name": node.get("name"), "value": deepcopy(node.get("value")),
                       "states": states, "actions": _strings(node.get("actions", []), "browser actions"),
                       "parent": f"browser:{parent}" if parent is not None else None,
                       "bounds": _bounds(node.get("bounds"), "unknown"),
                       "source": {"kind": "browser", "collection": collection, "node": deepcopy(node)}}
            control["value_evidence"] = {"precision": "display_only", "exact_value_proven": False,
                "plane": "display", "reason": "Cua semantic_v2 clean_semantic_text normalizes whitespace",
                "committed_document_proven": False, "saved_file_proven": False}
            if repaired:
                relation = node.get("parent_relation")
                if relation not in ("root", "resolved") or (relation == "root") != (parent is None):
                    raise ObservationError("Browser parent relation is incomplete or contradictory")
                exact = node.get("exact_value", {})
                if exact.get("precision") == "exact":
                    frame = node.get("frame_identity", {})
                    if (not isinstance(exact.get("value"), str) or exact.get("frame_document_proven") is not True
                            or exact.get("plane") != "dom_control_value"
                            or exact.get("source") not in ("DOMSnapshot.inputValue", "DOMSnapshot.textValue")
                            or any(not isinstance(frame.get(k), str) or not frame[k] for k in ("frame_id", "loader_id"))):
                        raise ObservationError("Invalid exact browser value provenance")
                    control["display_value"] = control["value"]
                    control["value"] = exact["value"]
                    control["value_evidence"] = {"precision": "exact", "exact_value_proven": True,
                        "plane": "dom_control_value", "source": exact["source"], "driver_contract": deepcopy(contract),
                        "atomic_capture": False, "committed_document_proven": False, "saved_file_proven": False}
                if node.get("context_only") is True or node.get("executable") is not True:
                    if node.get("context_only") is True and control["actions"]:
                        raise ObservationError("Context-only node claims executable actions")
                    control["actions"] = []
            controls.append(control)
            handle = {"kind": "browser", "target_id": target["target_id"], "tab_id": target["tab_id"],
                      "snapshot_id": snapshot, "ref": ref}
            if "session" in target:
                handle["session"] = target["session"]
            if not repaired or node.get("executable") is True and node.get("context_only") is False:
                handles[identifier] = handle
    if repaired:
        by_id = {c["id"]: c for c in controls}
        for control in controls:
            seen, current = set(), control
            while current.get("parent") is not None:
                if current["id"] in seen or current["parent"] not in by_id:
                    raise ObservationError("Missing or cyclic exported browser ancestor")
                seen.add(current["id"])
                parent_control = by_id[current["parent"]]
                if current["source"]["node"].get("frame_identity") != parent_control["source"]["node"].get("frame_identity"):
                    raise ObservationError("Cross-frame browser parent reference")
                current = parent_control
    outline = payload.get("outline", "")
    if not isinstance(outline, str):
        raise ObservationError("Browser outline must be text")
    hierarchy, stack = [], []
    for number, line in enumerate(outline.splitlines(), 1):
        match = _OUTLINE_LINE.match(line)
        if not match:
            continue
        indent, role, tail = match.groups()
        depth = len(indent.expandtabs(4))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        identifier = f"outline:{number}"
        # semantic_v2's outline omits refs. Preserve its displayed hierarchy but
        # do not guess which of duplicate/generic refs owns a displayed line.
        hierarchy.append({"id": identifier, "control_id": None,
                          "parent": stack[-1][1] if stack else None,
                          "indent": depth, "role": role, "raw": line})
        stack.append((depth, identifier))
    metadata = _object(payload.get("snapshot"), "browser snapshot")
    complete = metadata.get("complete")
    if repaired and metadata.get("ancestry_complete") is not True:
        raise ObservationError("Experimental browser ancestry completeness not established")
    if complete is not None and not isinstance(complete, bool):
        raise ObservationError("snapshot.complete must be boolean or null")
    if metadata.get("continuation") is not None and complete is True:
        raise ObservationError("Complete browser snapshot has a continuation")
    coverage = {"complete": complete, "scope": metadata.get("scope"),
                "continuation": deepcopy(metadata.get("continuation")),
                "omitted": deepcopy(metadata.get("omitted", {})),
                "node_budget": metadata.get("node_budget"), "selected_nodes": metadata.get("selected_nodes"),
                "total_nodes": metadata.get("total_nodes"), "normalized_control_count": len(controls),
                "structured_parent_links_available": any("parent_ref" in c["source"]["node"] for c in controls),
                "ancestry_complete": metadata.get("ancestry_complete"),
                "outline_retained": bool(outline),
                "outline_ref_binding": "unavailable", "oopif": deepcopy(payload.get("oopif"))}
    return controls, handles, hierarchy, coverage


def normalize_observation(raw: dict, *, kind: str, expected_target: Mapping,
                          observed_at_ns: int, tool_schemas: Any = None,
                          native_contract: Mapping | None = None) -> dict:
    """Normalize every supplied node; never select by task/gold/fixture labels.

    native_contract opts into a pinned source rule, after the caller checks the
    runtime platform/version. It is source-based inference, not a binary audit.
    """
    if kind not in _KINDS:
        raise ObservationError("Unsupported observation kind")
    kind = _KINDS[kind]
    contract = _native_contract(native_contract)
    if contract is not None and kind != "native":
        raise ObservationError("Native source contract cannot apply to a browser observation")
    _integer(observed_at_ns, "observed_at_ns")
    raw_hash = _json_hash(raw)
    payload, request = _unwrap(raw)
    target = _target(payload, expected_target, kind, request)
    if kind == "native":
        background = payload.get("background_input")
        exact = background.get("exact_window", {}) if isinstance(background, dict) else {}
        if payload.get("degraded") is True or (isinstance(exact, dict) and exact.get("status") == "ax_unresolved"):
            raise NativeObservationUnavailable(payload, target, raw_hash)
        snapshot = _string(payload.get("snapshot_id"), "snapshot_id")
        result = _native(payload, target, snapshot)
        text = payload.get("tree_markdown", "")
    else:
        metadata = _object(payload.get("snapshot"), "snapshot")
        if metadata.get("format") != "semantic_v2":
            raise ObservationError("Only browser semantic_v2 is supported")
        snapshot = _string(metadata.get("id"), "snapshot.id")
        result = _browser(payload, target, snapshot)
        text = payload.get("outline", "")
    controls, handles, hierarchy, coverage = result
    observation = {"kind": "native_window_state" if kind == "native" else "browser_semantic_v2",
                   "target": target, "snapshot_id": snapshot, "observed_at_ns": observed_at_ns,
                   "controls": controls, "text": text, "coverage": coverage,
                   "handles": handles, "hierarchy": hierarchy,
                   "provenance": {"adapter": "locua.cua_perception.v1", "raw_sha256": raw_hash,
                                  "observed_at_ns": observed_at_ns, "clock": "unix_time_ns",
                                  "caller_must_retain_raw": True,
                                  "raw_metadata": deepcopy({k: v for k, v in payload.items()
                                      if k not in {"elements", "tree_markdown", "refs", "content_refs", "outline"}})}}
    if contract is not None:
        observation["provenance"]["native_contract"] = contract
        for control in controls:
            control["source"]["native_contract"] = deepcopy(contract)
    if kind == "native":
        editor_contract = payload.get("native_editor_contract")
        if editor_contract is not None and not check_native_editor_contract(editor_contract):
            raise ObservationError("Unsupported native editor producer contract")
        for control in controls:
            if editor_contract is not None and control["source"].get("kind") == "native":
                control["source"]["native_editor_contract"] = deepcopy(editor_contract)
                editor = control["source"].get("node", {}).get("editor", {})
                if editor.get("contract") == NATIVE_EDITOR_CONTRACT and editor.get("plane") == "editor_buffer":
                    control["editor"] = deepcopy(editor)
                    for field in ("value_settable", "focused"):
                        fact = editor.get(field, {})
                        if field == "focused" and editor.get("coherence", {}).get("focus_stable") is not True:
                            control["states"].pop("focused", None)
                            continue
                        if fact.get("status") == "ok" and type(fact.get("value")) is bool:
                            control["states"][field] = fact["value"]
            control["value_evidence"] = _native_value_evidence(control)
            if control["value_evidence"].get("exact_value_proven"):
                control["display_value"] = control.get("value")
                control["value"] = control["source"]["node"]["editor"]["raw_value"]["value"]
    if kind == "native" and tool_schemas is not None:
        for control in controls:
            control["capabilities"] = native_text_capabilities(control, tool_schemas,
                                                               handle=handles.get(control["id"]))
    validate_observation(observation, expected_target=expected_target)
    return observation


def validate_observation(observation: dict, *, expected_target: Mapping,
                         current_snapshot_id: str | None = None,
                         now_ns: int | None = None, max_age_s: float | None = None) -> None:
    """Validate identity/time, not global completeness or action correctness.

    A native incomplete observation can prove a control exists but cannot prove
    absence. Consumers must separately gate actions and completion predicates.
    """
    _object(observation, "observation")
    actual = _object(observation.get("target"), "target")
    if dict(expected_target) != actual:
        raise ObservationError("Observation target does not match expected target")
    snapshot = _string(observation.get("snapshot_id"), "snapshot_id")
    if current_snapshot_id is not None and snapshot != current_snapshot_id:
        raise ObservationError("Observation was superseded by another snapshot")
    stamp = _integer(observation.get("observed_at_ns"), "observed_at_ns")
    if observation.get("provenance", {}).get("observed_at_ns") != stamp:
        raise ObservationError("Observation timestamps disagree")
    if max_age_s is not None:
        if isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float)) or not math.isfinite(max_age_s) or max_age_s < 0:
            raise ObservationError("max_age_s must be finite and nonnegative")
        if now_ns is None:
            now_ns = time.time_ns()
    if now_ns is not None:
        _integer(now_ns, "now_ns")
        if now_ns < stamp:
            raise ObservationError("Observation timestamp lies in the future")
        if max_age_s is not None and now_ns - stamp > max_age_s * 1_000_000_000:
            raise ObservationError("Observation is stale")
    controls = observation.get("controls")
    if not isinstance(controls, list):
        raise ObservationError("controls must be an array")
    identifiers = [_string(c.get("id"), "control id") for c in controls]
    if len(set(identifiers)) != len(identifiers):
        raise ObservationError("Duplicate normalized control identity")
    by_id = {c["id"]: c for c in controls}
    for identifier, handle in observation.get("handles", {}).items():
        if identifier not in identifiers or handle.get("snapshot_id") != snapshot:
            raise ObservationError("Handle does not belong to current observation")
        kind = _KINDS.get(observation.get("kind"))
        fields = ("pid", "window_id") if kind == "native" else ("target_id", "tab_id")
        if any(handle.get(field) != actual.get(field) for field in fields):
            raise ObservationError("Handle targets a different surface")
        if "session" in actual and handle.get("session") != actual["session"]:
            raise ObservationError("Handle belongs to another session")
        source = by_id[identifier].get("source", {})
        node = source.get("node", {})
        if handle.get("kind") != kind or source.get("kind") != kind:
            raise ObservationError("Handle kind disagrees with its observed source")
        if kind == "native":
            index = node.get("element_index")
            if identifier != f"native:{snapshot}:{index}" or handle.get("element_index") != index:
                raise ObservationError("Handle index was swapped between native controls")
            if handle.get("element_token") != node.get("element_token"):
                raise ObservationError("Handle token disagrees with its observed native node")
        elif kind == "browser":
            if identifier != f"browser:{node.get('ref')}" or handle.get("ref") != node.get("ref"):
                raise ObservationError("Handle ref was swapped between browser controls")
        else:
            raise ObservationError("Unsupported normalized observation kind")


def _schemas(inventory: Any) -> dict:
    if isinstance(inventory, dict) and "tools" in inventory:
        inventory = inventory["tools"]
    if isinstance(inventory, list):
        return {item["name"]: item for item in inventory if isinstance(item, dict) and "name" in item}
    if isinstance(inventory, dict):
        return inventory
    raise ObservationError("Tool schemas must be a tools array or name mapping")


def _native_address_bound(control: dict, handle: dict | None) -> bool:
    if not isinstance(handle, dict) or handle.get("kind") != "native":
        return False
    source = control.get("source", {})
    node = source.get("node", {})
    snapshot, index = handle.get("snapshot_id"), node.get("element_index")
    if source.get("kind") != "native" or type(index) is not int or index < 0:
        return False
    if not isinstance(snapshot, str) or not snapshot:
        return False
    if type(handle.get("element_index")) is not int:
        return False
    if any(type(handle.get(k)) is not int or handle[k] <= 0 for k in ("pid", "window_id")):
        return False
    return (control.get("id") == f"native:{snapshot}:{index}"
            and control.get("role") == node.get("role")
            and handle.get("element_index") == index
            and handle.get("element_token") == node.get("element_token"))


def _native_value_writability(control: dict) -> dict:
    """Keep reported state distinct from the explicitly opted-in source rule."""
    states = control.get("states", {})
    source = control.get("source", {})
    node = source.get("node", {})
    result = {"value": None, "basis": "unknown", "contract": None}
    editor = node.get("editor", {})
    fact = editor.get("value_settable", {})
    if (check_native_editor_contract(source.get("native_editor_contract"))
            and editor.get("contract") == NATIVE_EDITOR_CONTRACT and editor.get("plane") == "editor_buffer"
            and fact.get("status") == "ok" and type(fact.get("value")) is bool):
        if states.get("value_settable") is not fact["value"]:
            return {**result, "value": False, "basis": "state_source_disagreement"}
        return {"value": fact["value"], "basis": "same_handle_AXIsAttributeSettable",
                "contract": deepcopy(source["native_editor_contract"])}
    original = node.get("value_settable", node.get("states", {}).get("value_settable"))
    if type(original) is bool:
        if states.get("value_settable") != original:
            return {**result, "value": False, "basis": "state_source_disagreement"}
        return {**result, "value": original, "basis": "explicit_observed_value_settable"}
    if "value_settable" in states and states["value_settable"] is not None:
        return {**result, "value": False, "basis": "unbacked_normalized_writability"}
    contract = source.get("native_contract")
    if contract is None:
        return result
    try:
        contract = _native_contract(contract)
    except ObservationError:
        return {**result, "value": False, "basis": "unsupported_native_contract"}
    # Cua's macOS walker: index iff (actions present OR writable AXValue on a
    # whitelisted role) AND enabled != false. Its serializer omits empty actions.
    # Preserve this as inferred capability evidence; never insert a missing
    # states.value_settable. A field with AX actions provides no such proof.
    actions = node.get("actions", [])
    if (source.get("kind") == "native"
            and node.get("role") in _VALUE_ADDRESSED_TEXT_ROLES
            and type(node.get("element_index")) is int
            and node["element_index"] >= 0
            and isinstance(actions, list) and not actions
            and control.get("actions") == actions
            and node.get("enabled") is not False
            and states.get("enabled") is not False):
        return {"value": True, "basis": "pinned_walker_addressability", "contract": contract,
                "rule": "indexed_native_text_role_without_AX_actions_implies_settable_AXValue",
                "source": "platform-macos/src/ax/tree.rs:431-443; tools/get_window_state.rs:806-921",
                "binary_equivalence_proven": False, "requires_fresh_readback": True}
    return result


def _set_value_eligibility(control: dict, handle: dict | None, schema_supported: bool,
                           addressing: dict) -> bool | None:
    if not schema_supported or not _native_address_bound(control, handle) or control.get("role") not in _TEXT_ROLES:
        return False
    if control.get("states", {}).get("enabled") is False or control.get("states", {}).get("editable") is False:
        return False
    if not (addressing.get("element_token") is True and bool(handle.get("element_token"))
            or addressing.get("snapshot_index") is True):
        return False
    return _native_value_writability(control)["value"]


def native_set_value_eligible(control: dict) -> bool | None:
    """Re-derive eligibility from retained source/handle evidence, not a flag.

    This does not authorize an action or renew its snapshot. The core must still
    validate exact target, intent, freshness, schema and independent readback.
    """
    route = control.get("capabilities", {}).get("set_value", {})
    evidence = route.get("evidence", {})
    expected = _native_value_writability(control)
    if evidence.get("writability") != expected:
        return False
    result = _set_value_eligibility(control, evidence.get("address"), route.get("schema_supported") is True,
                                    route.get("addressing", {}))
    if route.get("eligible") is not result:
        return False
    return result


def native_text_capabilities(control: dict, tool_schemas: Any, *, handle: dict | None = None) -> dict:
    """Describe public routes and unknown editor preconditions, not recipes.

    A supported tool schema does not prove the target accepts an AX write or has
    editor focus. In particular, no shortcut/coordinates are invented here.
    """
    inventory = _schemas(tool_schemas)
    def supports(tool: str, *fields: str) -> bool:
        entry = inventory.get(tool, {})
        schema = entry.get("inputSchema", entry.get("input_schema", entry))
        return isinstance(schema, dict) and set(fields) <= set(schema.get("properties", {}))
    addressed = _native_address_bound(control, handle)
    eligible = control.get("role") in _TEXT_ROLES
    states = control.get("states", {})
    enabled = states.get("enabled") is not False
    set_supported = supports("set_value", "pid", "value") and (
        supports("set_value", "element_token") or supports("set_value", "element_index", "snapshot_id", "window_id"))
    type_supported = supports("type_text", "pid", "window_id", "text")
    click_supported = supports("click", "pid", "window_id", "x", "y")
    key_supported = supports("press_key", "pid", "window_id", "key")
    set_addressing = {"element_token": supports("set_value", "element_token"),
                      "snapshot_index": supports("set_value", "element_index", "snapshot_id", "window_id")}
    # A positive current snapshot demonstrates this reader capability, not
    # success of a future write. Re-derive it from the pinned producer and
    # exact handle rather than trusting a caller-written precision flag.
    value_evidence = _native_value_evidence(control) if addressed and eligible else {}
    read_supported = supports("get_window_state", "pid", "window_id")
    exact_editor_reader = (read_supported and addressed and eligible
        and value_evidence.get("exact_value_proven") is True
        and value_evidence.get("plane") == "editor_buffer"
        and control.get("value_evidence") == value_evidence
        and control.get("value") == control.get("source", {}).get("node", {}).get("editor", {}).get("raw_value", {}).get("value"))
    editor = control.get("source", {}).get("node", {}).get("editor", {})
    focus_fact = editor.get("focused", {})
    focus_reader = (read_supported and addressed and eligible
        and check_native_editor_contract(control.get("source", {}).get("native_editor_contract"))
        and editor.get("contract") == NATIVE_EDITOR_CONTRACT and editor.get("plane") == "editor_buffer"
        and focus_fact.get("status") == "ok" and type(focus_fact.get("value")) is bool
        and editor.get("coherence", {}).get("focus_stable") is True
        and states.get("focused") is focus_fact["value"])
    return {
        "set_value": {"tool": "set_value", "schema_supported": set_supported,
                      "addressed": addressed, "role_is_text_control": eligible,
                      "eligible": _set_value_eligibility(control, handle, set_supported, set_addressing),
                      "addressing": set_addressing,
                      "semantics": "replace_AXValue_driver_may_coerce_numeric_text",
                      "exact_readback_supported": exact_editor_reader,
                      "exact_readback_plane": "editor_buffer" if exact_editor_reader else None,
                      "post_action_readback_proven": False,
                      "evidence": {"writability": _native_value_writability(control), "address": deepcopy(handle)},
                      "requires": ["fresh_exact_handle", "writable_AXValue", "independent_exact_string_readback"]},
        "type": {"tool": "type_text", "schema_supported": type_supported,
                 "addressed": addressed, "role_is_text_control": eligible,
                 "eligible": False if not (type_supported and addressed and enabled and eligible) else None,
                 "addressing": {"element_token": supports("type_text", "element_token"),
                                "snapshot_index": supports("type_text", "element_index", "snapshot_id", "window_id")},
                 "observed_focus": states.get("focused") if type(states.get("focused")) is bool else None,
                 "selected_text_writable": states.get("selected_text_settable") if type(states.get("selected_text_settable")) is bool else None,
                 "read_only_focus_probe_available": focus_reader,
                 "read_only_focus_source": "get_window_state.editor.focused" if focus_reader else None,
                 "semantics": "insert_at_selection_or_caret",
                 "requires": ["proven_editor_focus", "explicit_insert_or_replace_intent", "independent_readback"]},
        "editor_entry": {"schema_supported": click_supported or key_supported,
                         "pixel_click_supported": click_supported,
                         "click_count_supported": supports("click", "count"),
                         "key_supported": key_supported, "eligible": None,
                         "requires": ["observed_control_or_grounded_geometry", "observed_entry_semantics", "verify_editor_state"],
                         "shortcut": None, "coordinates": None},
        "evidence": {"observed_actions": deepcopy(control.get("actions", [])),
                     "source_kind": control.get("source", {}).get("kind"),
                     "schema_is_not_target_capability": True}
    }
