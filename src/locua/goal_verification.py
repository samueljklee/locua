"""Reviewed native outcome bindings and positive, fresh readback verification.

Bindings establish *which* observed surface the user reviewed. They do not prove
that an abstract target phrase describes that surface: the caller must present
``review_descriptor`` in its review. No model answer, action acknowledgement,
cached completion, committed document or saved-file claim is accepted here.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
import re
import time

from .engine.prototype.perception import (
    validate_observation, _markdown_semantics, _native_address_bound,
)
from .engine.prototype.native_driver_contract import check_native_editor_contract

VERSION = "locua-native-goal-binding-v1"
MAX_AGE_SECONDS = 30
_SEMANTICS = ("title", "description", "help", "identifier", "value_description")
_TEXT = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
_DISPLAY = _TEXT | {"AXStaticText", "AXHeading"}
_SELECTED = {"AXRadioButton", "AXTab", "AXRow", "AXCell"}
_STATIC_SLOT = "rendered_static_text_slot"
_DISPLAY_VALUE = "display_value"  # Internal read-only preservation predicate.
_NATIVE_VALUE_LABEL = "pinned_native_value_fallback_trim"
# Rust str::trim uses Unicode White_Space, not Python's extra U+001C..001F.
_RUST_WHITESPACE = "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


class BindingError(ValueError):
    """A reviewed outcome cannot be bound or refreshed without guessing."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate(observation, target=None, *, max_age_s=MAX_AGE_SECONDS):
    if not isinstance(observation, dict) or observation.get("kind") != "native_window_state":
        raise BindingError("native_observation_required")
    actual = observation.get("target", {})
    if not isinstance(actual, dict) or any(type(actual.get(k)) is not int or actual[k] <= 0
                                         for k in ("pid", "window_id")):
        raise BindingError("exact_native_target_required")
    validate_observation(observation, expected_target=target if target is not None else actual,
                         now_ns=time.time_ns(), max_age_s=max_age_s)
    return actual


def _numeric(value):
    """A complete unambiguous dot-decimal; no locale/group-separator guessing."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    # LRM/RLM affect display direction, not the numeric characters. Preserve
    # the original string in evidence; do not discard arbitrary format chars,
    # punctuation, grouping or surrounding prose.
    value = value.strip().replace("\u2212", "-").replace("\u200e", "").replace("\u200f", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?", value):
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number.adjusted()) > 300:
            return None
        return Fraction(number)
    except (ValueError, InvalidOperation):
        return None


def _display(control):
    if control.get("role") not in _DISPLAY:
        return None
    value = control.get("value")
    # Empty is a real value, not an invitation to substitute the label.
    return value if isinstance(value, str) else control.get("name")


def _semantic(control):
    semantics = control.get("semantics", {})
    if not isinstance(semantics, dict):
        raise BindingError("invalid_control_semantics")
    return {"role": control.get("role"), "name": control.get("name"),
            "semantics": {k: semantics.get(k) for k in _SEMANTICS}}


def _ancestors(control, observation):
    by_id = {c["id"]: c for c in observation["controls"]}
    parent, seen, result = control.get("parent"), {control["id"]}, []
    while parent is not None:
        if parent in seen or parent not in by_id:
            raise BindingError("native_ancestry_unresolved")
        seen.add(parent)
        ancestor = by_id[parent]
        result.append(_semantic(ancestor))
        parent = ancestor.get("parent")
    return result


def _bounds(control):
    bounds = control.get("bounds")
    if not isinstance(bounds, dict):
        return None
    fields = ("x", "y", "width", "height")
    if any(type(bounds.get(k)) not in (int, float) or not math.isfinite(bounds[k]) for k in fields):
        return None
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        return None
    return {k: bounds[k] for k in fields}


def _native_value_label_proven(control, observation):
    """Reprove the pinned producer's value fallback, never a name heuristic.

    tree.rs trims the display value; get_window_state.rs chooses title, then
    description, then value for the label. An exact indexed rendered prefix
    proves title absence, and a metadata-only suffix proves description absence.
    Match the *whole literal value span*, not a quoted-string regex: this also
    supports embedded quotes and LF/CRLF without interpreting buffer text as
    tree syntax. Row-shaped continuations and unsupported line separators stay
    unproved. Metadata is not parsed (identifiers may contain spaces), and raw
    buffer text stays untouched.
    """
    source = control.get("source", {})
    node = source.get("node", {})
    contract = source.get("native_editor_contract")
    if (control.get("role") not in _TEXT or source.get("kind") != "native"
            or not check_native_editor_contract(contract)
            or observation.get("provenance", {}).get("raw_metadata", {}).get("native_editor_contract") != contract
            or node.get("role") != control.get("role")):
        return False
    proof = control.get("value_evidence", {})
    editor = node.get("editor", {})
    value = control.get("value")
    if (proof.get("precision") != "exact" or proof.get("exact_value_proven") is not True
            or proof.get("plane") != "editor_buffer" or proof.get("contract") != contract
            or editor.get("contract") != contract["id"] or editor.get("plane") != "editor_buffer"
            or editor.get("coherence", {}).get("value_stable") is not True
            or not isinstance(value, str)):
        return False
    for key in ("raw_value", "raw_value_recheck"):
        fact = editor.get(key, {})
        if fact.get("status") != "ok" or fact.get("value") != value:
            return False
    displayed = value.strip(_RUST_WHITESPACE)
    if (not displayed
            or any(c in displayed for c in '\x00\x0b\x0c\x1c\x1d\x1e\x1f\u0085\u2028\u2029')
            or re.search(r'\r(?!\n)', displayed)
            # The normalizer reads unescaped presentation lines. Never let a
            # buffer continuation manufacture a control or alter its ancestry.
            or any(re.match(r'^\s*-\s+(?:\[\d+\]\s+)?AX\w+\b', part)
                   for part in displayed.splitlines()[1:])
            or any(node.get(k) is not None for k in ("title", "description"))
            or any(control.get("semantics", {}).get(k) is not None for k in ("title", "description"))
            or node.get("label") != displayed or node.get("value") != displayed
            or control.get("name") != displayed or control.get("display_value") != displayed):
        return False
    index = node.get("element_index")
    snapshot = observation.get("snapshot_id")
    handle = observation.get("handles", {}).get(control.get("id"), {})
    if (type(index) is not int or index < 0 or control.get("id") != f"native:{snapshot}:{index}"
            or node.get("element_token") != f"{snapshot}:{index}"
            or handle.get("element_token") != node["element_token"]
            or handle.get("element_index") != index or handle.get("snapshot_id") != snapshot
            or handle.get("kind") != "native"
            or any(handle.get(k) != observation["target"].get(k) for k in ("pid", "window_id"))):
        return False
    line, number = source.get("markdown_line"), source.get("markdown_line_number")
    if not isinstance(line, str) or type(number) is not int or number < 1:
        return False
    text = observation.get("text", "")
    lines = text.splitlines()
    if number > len(lines) or lines[number-1] != line:
        return False
    prefix = f'- [{index}] {control["role"]} = "{displayed}"'
    # Source line numbers are physical splitlines() positions. Preserve every
    # original separator while locating the start, then compare known data
    # across the exact span; do not concatenate or trim continuation lines.
    offset = sum(len(part) for part in text.splitlines(keepends=True)[:number-1])
    rendered = text[offset:].lstrip(" ")
    if not rendered.startswith(prefix):
        return False
    suffix = rendered[len(prefix):].partition('\n')[0]
    if any(c in suffix for c in '\r\x0b\x0c\x1c\x1d\x1e\x1f\u0085\u2028\u2029'):
        return False
    if suffix and not (suffix.startswith(" [") and suffix.endswith("]")):
        return False
    # Exactly one row for this source index; don't accept a selected duplicate.
    indexed = [x for x in lines if re.match(rf"^\s*-\s+\[{index}\]\s+", x)]
    return indexed == [line]


def _identity(control, observation, policy):
    if policy.get("mode") == _STATIC_SLOT:
        return _static_slot_identity(control, observation)
    item = _semantic(control)
    # Drop only fields proved to be derived from this control's observed value
    # at binding time, never ancestor labels or all names indiscriminately.
    display = control.get("value") if policy.get("display_value_only") else _display(control)
    for key in policy["value_derived_fields"]:
        current = item["name"] if key == "name" else item["semantics"].get(key)
        if key == "name" and policy.get("name_derivation") == _NATIVE_VALUE_LABEL:
            if not _native_value_label_proven(control, observation):
                raise BindingError("native_value_label_fallback_unproved")
        elif current != display:
            raise BindingError("value_derived_identity_changed")
        if key == "name": item["name"] = None
        else: item["semantics"][key] = None
    return {**item, "ancestors": _ancestors(control, observation),
            "bounds": _bounds(control) if policy["require_bounds"] else None}


def _static_slot_identity(control, observation):
    """Read-only position in the pinned producer's rendered AX tree.

    This is a structural display slot, NOT identity of an AX object. Layout
    containers collapse in this producer. We require a unique identified parent
    and retain all observed sibling roles/order. Other static-text landmarks,
    or a proved native sibling constellation for a lone readout, anchor the slot.
    Unknown truncation outside this local list remains unknown; no global
    completeness, write handle or exact raw-attribute claim is created.
    """
    source = control.get("source", {})
    if (control.get("role") != "AXStaticText" or source.get("kind") != "native_markdown"
            or source.get("observed_index") is not None or control.get("actions")
            or control["id"] in observation.get("handles", {}) or control.get("name") is not None
            or any(_semantic(control)["semantics"].values())):
        raise BindingError("structural_slot_requires_unaddressed_static_text")
    match = re.fullmatch(r'\s*- AXStaticText = "([^"\r\n]*)"\s*', source.get("line", ""))
    if not match or match[1] != control.get("value"):
        raise BindingError("structural_slot_markdown_value_unproved")
    coverage = observation.get("coverage", {})
    if (coverage.get("hierarchy_scope") != "rendered_native_tree_with_collapsed_containers"
            or coverage.get("parse_warnings") or coverage.get("unresolved_parent_ids")
            or coverage.get("truncated") or coverage.get("degraded") or coverage.get("truncation")
            or "AX tree truncated" in observation.get("text", "")):
        raise BindingError("structural_slot_capture_unresolved")
    contract = observation.get("provenance", {}).get("raw_metadata", {}).get("native_editor_contract")
    if not check_native_editor_contract(contract):
        raise BindingError("structural_slot_producer_unpinned")
    by_id = {c["id"]: c for c in observation["controls"]}
    parent = by_id.get(control.get("parent"))
    if (not parent or parent.get("source", {}).get("kind") != "native"
            or not parent.get("semantics", {}).get("identifier")
            or parent.get("source", {}).get("native_editor_contract") != contract):
        raise BindingError("structural_slot_identified_parent_required")
    parent_identity = {**_semantic(parent), "ancestors": _ancestors(parent, observation)}
    parent_peers = [c for c in observation["controls"]
                   if {**_semantic(c), "ancestors": _ancestors(c, observation)} == parent_identity]
    if len(parent_peers) != 1:
        raise BindingError("structural_slot_parent_ambiguous")
    rows = [r for r in observation.get("hierarchy", []) if r.get("parent_control_id") == parent["id"]]
    ids = [r.get("control_id") for r in rows]
    expected_ids = [c["id"] for c in observation["controls"] if c.get("parent") == parent["id"]]
    if len(ids) != len(set(ids)) or set(ids) != set(expected_ids) or control["id"] not in ids:
        raise BindingError("structural_slot_hierarchy_disagrees")
    siblings = [by_id[cid] for cid in ids]
    own_row = rows[ids.index(control["id"])]
    if own_row.get("raw") != source["line"]:
        raise BindingError("structural_slot_source_line_disagrees")
    landmarks = []
    for offset, sibling in enumerate(siblings):
        if sibling["id"] == control["id"] or sibling.get("role") != "AXStaticText":
            continue
        value = _display(sibling)
        if not isinstance(value, str) or not value or value == control.get("value"):
            raise BindingError("structural_slot_landmark_ambiguous")
        landmarks.append({"offset": offset, **_semantic(sibling), "value": value})
    anchors = {}
    if not landmarks:
        anchors = _single_readout_anchors(parent, siblings, rows, observation, contract)
    return {"role": "AXStaticText", "name": None, "semantics": {},
            "ancestors": _ancestors(control, observation), "bounds": None,
            "mode": _STATIC_SLOT, "parent_identity": parent_identity,
            "sibling_index": ids.index(control["id"]), "sibling_roles": [s.get("role") for s in siblings],
            "static_landmarks": landmarks, **anchors}


def _identified_native_anchor(control, row, observation, contract):
    """Reprove an indexed AX identifier and frame; a label is not an anchor.

    Identifiers may originate in the pinned rendered row because this driver
    omits them from structured elements. Reuse its ambiguity-aware parser and
    require the row, handle, source role and frame to agree. This observes an
    existing control; it creates no authority to press that control.
    """
    source = control.get("source", {})
    node = source.get("node", {})
    identifier = control.get("semantics", {}).get("identifier")
    if not isinstance(identifier, str) or not identifier:
        return None
    if (source.get("kind") != "native" or source.get("native_editor_contract") != contract
            or control.get("semantics", {}).get("warnings")
            or not _native_address_bound(control, observation.get("handles", {}).get(control["id"]))):
        raise BindingError("structural_anchor_source_unproved")
    line, number = source.get("markdown_line"), source.get("markdown_line_number")
    lines = observation.get("text", "").splitlines()
    if (not isinstance(line, str) or type(number) is not int or not 1 <= number <= len(lines)
            or lines[number-1] != line or row.get("raw") != line):
        raise BindingError("structural_anchor_row_unproved")
    parsed, warnings = _markdown_semantics(line)
    if (warnings or parsed.get("identifier") != identifier
            or node.get("identifier") not in (None, identifier)):
        raise BindingError("structural_anchor_identifier_unproved")
    bounds = _bounds(control)
    frame = node.get("frame", {})
    if (bounds is None or not isinstance(frame, dict)
            or any(frame.get(raw) != bounds[key] for raw, key in
                   (("x", "x"), ("y", "y"), ("w", "width"), ("h", "height")))):
        raise BindingError("structural_anchor_frame_unproved")
    return {"role": control["role"], "identifier": identifier, "bounds": bounds}


def _single_readout_anchors(parent, siblings, rows, observation, contract):
    """A lone unnamed readout needs independent, non-value identity evidence.

    Require at least two distinct, located, addressed sibling AX identifiers,
    plus an addressed parent. Retain every qualifying anchor and its rendered
    offset. Fresh comparison permits only coherent whole-window translation:
    relative frames, parent size, sibling roles/order and anchor identifiers
    must remain unchanged. Enabled/focus/value/label changes do not become
    identity. Added readouts or reflow require a newly reviewed binding.
    """
    if sum(c.get("role") == "AXStaticText" for c in siblings) != 1:
        raise BindingError("single_readout_scope_ambiguous")
    parent_rows = [r for r in observation.get("hierarchy", []) if r.get("control_id") == parent["id"]]
    if len(parent_rows) != 1:
        raise BindingError("structural_anchor_parent_row_ambiguous")
    parent_anchor = _identified_native_anchor(parent, parent_rows[0], observation, contract)
    if parent_anchor is None:
        raise BindingError("structural_anchor_parent_unproved")
    frame = parent_anchor["bounds"]
    anchors, identities, locations = [], set(), set()
    for offset, (control, row) in enumerate(zip(siblings, rows)):
        if control.get("role") == "AXStaticText":
            continue
        anchor = _identified_native_anchor(control, row, observation, contract)
        if anchor is None:
            continue
        key = (anchor["role"], anchor["identifier"])
        if key in identities:
            raise BindingError("structural_anchor_identifier_ambiguous")
        identities.add(key)
        b = anchor["bounds"]
        relative = {"x": b["x"]-frame["x"], "y": b["y"]-frame["y"],
                    "width": b["width"], "height": b["height"]}
        if (relative["x"] < 0 or relative["y"] < 0
                or relative["x"]+relative["width"] > frame["width"]
                or relative["y"]+relative["height"] > frame["height"]):
            raise BindingError("structural_anchor_outside_parent")
        locations.add(tuple(relative[k] for k in ("x", "y", "width", "height")))
        anchors.append({"offset": offset, "role": anchor["role"],
                        "identifier": anchor["identifier"], "relative_bounds": relative})
    if len(anchors) < 2 or len(locations) < 2:
        raise BindingError("structural_slot_anchor_evidence_insufficient")
    return {"anchor_basis": "single_readout_identified_native_siblings_v1",
            "parent_size": {"width": frame["width"], "height": frame["height"]},
            "native_anchors": anchors}


def _identity_policy(control, observation, kind):
    semantics = _semantic(control)
    derived = []
    display = control.get("value") if kind == _DISPLAY_VALUE else _display(control)
    # Text editors can legitimately expose AXValue as their accessibility name.
    # A readable arithmetic surface may similarly name itself with its result.
    if kind in ("text", "calculation", _DISPLAY_VALUE) and isinstance(display, str):
        if kind in ("text", _DISPLAY_VALUE) or _numeric(display) is not None:
            if semantics["name"] == display: derived.append("name")
            for key in ("title", "description", "value_description"):
                if semantics["semantics"][key] == display: derived.append(key)
    name_derivation = None
    if kind == "text" and _native_value_label_proven(control, observation):
        if "name" not in derived: derived.append("name")
        name_derivation = _NATIVE_VALUE_LABEL
    identifier = semantics["semantics"].get("identifier")
    ancestors = _ancestors(control, observation)
    strong_ancestor = any(a.get("name") or a["semantics"].get("identifier") for a in ancestors)
    if derived:
        # Geometry is pinned even with an identifier when available. Without an
        # identifier, require positive geometry and a named/identified ancestor.
        if not identifier and not (_bounds(control) is not None and strong_ancestor):
            raise BindingError("value_derived_target_has_no_stable_identity")
    elif not (identifier or semantics.get("name") or any(semantics["semantics"].values())):
        raise BindingError("target_has_no_stable_identity")
    policy = {"value_derived_fields": derived, "require_bounds": bool(derived and _bounds(control))}
    if name_derivation is not None: policy["name_derivation"] = name_derivation
    if kind == _DISPLAY_VALUE:
        policy.update(read_only=True, display_value_only=True)
    return policy


def _property(outcome, control, *, allow_unknown_state=False):
    kind, role = outcome.get("kind"), control.get("role")
    plane = outcome.get("evidence_plane")
    if plane not in ("display", "editor_buffer"):
        raise BindingError("unsupported_evidence_plane")
    if kind == "state":
        if plane != "display" or type(outcome.get("value")) is not bool:
            raise BindingError("state_requires_display_boolean")
        if 'property' in outcome:
            prop = outcome['property']
            if prop not in ('checked', 'selected'):
                raise BindingError('state_property_not_supported')
        elif role == "AXCheckBox": prop = "checked"
        elif role in _SELECTED: prop = "selected"
        else: raise BindingError("state_role_semantics_unproved: specify the intended state property (selected or checked). "
            "An initially unknown property can be reviewed with an explicit one-time press; "
            "a goal-toggle effect still requires known fresh state, and verification requires an observed boolean.")
        if type(control.get("states", {}).get(prop)) is not bool:
            if not (allow_unknown_state and control.get('states', {}).get(prop) is None):
                raise BindingError("state_property_unknown")
        return prop, "display"
    if kind == "text":
        if role not in _TEXT or not isinstance(outcome.get("value"), str):
            raise BindingError("text_requires_native_editor")
        proof = control.get("value_evidence", {})
        if (proof.get("precision") != "exact" or proof.get("exact_value_proven") is not True
                or proof.get("plane") != "editor_buffer" or not isinstance(control.get("value"), str)):
            raise BindingError("exact_editor_value_unknown")
        return "value", "editor_buffer"
    if kind == _DISPLAY_VALUE:
        # A native popup or readout may have a displayed value without being
        # an editor. Do not fall back to a label, convert nonstrings, or promote
        # this trimmed/possibly-placeholder projection into exact AXValue,
        # editor-buffer, document-content, saved-output or input authority.
        if plane != "display" or not isinstance(outcome.get("value"), str):
            raise BindingError("display_preservation_requires_display_string")
        if role in _TEXT:
            raise BindingError("editor_preservation_requires_exact_buffer: inspect the editor's exact value evidence")
        if not isinstance(control.get("value"), str):
            raise BindingError("display_value_unknown: selected control has no observed string value; inspect a readable value control")
        return "value", "display"
    if kind == "calculation":
        if plane != "display" or role not in _DISPLAY or not isinstance(_display(control), str):
            raise BindingError("calculation_requires_readable_display")
        from .goal_planner import verify_arithmetic
        verify_arithmetic(outcome.get("expression"))
        return "numeric_display", "display"
    raise BindingError("outcome_kind_not_supported")


def _peers(binding, observation):
    matches = []
    for c in observation["controls"]:
        try:
            if _identity(c, observation, binding["identity_policy"]) == binding["core_identity"]:
                matches.append(c)
        except BindingError:
            continue
    return matches


def _check_binding(binding):
    if not isinstance(binding, dict) or binding.get("version") != VERSION:
        raise BindingError("unsupported_binding")
    body = {k: v for k, v in binding.items() if k != "id"}
    if binding.get("id") != "goal-binding:" + _hash(body):
        raise BindingError("binding_changed_after_review")


def bind(outcome, control, observation):
    """Bind a model-selected candidate before review; raises BindingError.

    This is not semantic interpretation or a review substitute. Uniqueness is
    among captured controls, never an assertion of complete app enumeration.
    """
    return _bind(outcome, control, observation)


def bind_for_review(outcome, control, observation):
    """Identify a control in retained evidence, without asserting current state.

    The original timestamp and complete observation digest remain bound. This
    path grants no action or verification authority: matches_binding() and
    verify() still require a fresh observation and unchanged unique identity.
    """
    return _bind(outcome, control, observation, retained_review=True)


def _bind(outcome, control, observation, *, retained_review=False):
    target = _validate(observation, max_age_s=None if retained_review else MAX_AGE_SECONDS)
    if not isinstance(outcome, dict) or not isinstance(outcome.get("id"), str) or not outcome["id"]:
        raise BindingError("outcome_identity_required")
    peers = [c for c in observation["controls"] if c.get("id") == control.get("id")]
    if len(peers) != 1 or peers[0] != control:
        raise BindingError("control_not_in_observation")
    # Review may name a future predicate without pretending it is observed now.
    # Only explicit properties permit this; final verification remains strict.
    prop, plane = _property(outcome, control,
        allow_unknown_state=retained_review and 'property' in outcome)
    try:
        policy = _identity_policy(control, observation, outcome["kind"])
    except BindingError:
        # Only arithmetic display readback may use this weaker positional
        # fallback. It must never authorize native text editing or presses.
        if outcome["kind"] != "calculation":
            raise
        _static_slot_identity(control, observation)
        policy = {"mode": _STATIC_SLOT, "read_only": True}
    identity = _identity(control, observation, policy)
    binding = {"version": VERSION, "outcome_id": outcome["id"], "outcome_sha256": _hash(outcome),
        "target": deepcopy(target), "property": prop, "evidence_plane": plane,
        "requested_evidence_plane": outcome["evidence_plane"], "target_phrase": outcome.get("target"),
        "core_identity": identity, "identity": deepcopy(identity), "identity_policy": policy,
        "initial_snapshot_id": observation["snapshot_id"], "bound_at_ns": observation["observed_at_ns"],
        "review_required": True, "review_descriptor": {"role": control.get("role"), "name": control.get("name"),
            "value_at_binding": deepcopy(control.get("value")), "semantics": deepcopy(identity["semantics"]),
            "ancestors": deepcopy(identity["ancestors"]), "bounds": deepcopy(control.get("bounds")),
            "property": prop, "evidence_plane": plane,
            "identity_scope": policy.get("mode", "semantic_control"),
            "read_only": policy.get("read_only", False)},
        "coverage_complete_at_binding": observation.get("coverage", {}).get("complete"),
        "uniqueness_scope": "captured_controls"}
    if prop in ('checked', 'selected'):
        state = control.get('states', {}).get(prop)
        binding['review_descriptor'].update(state_at_binding=deepcopy(state),
            state_observed_at_binding=type(state) is bool)
    if outcome["kind"] == _DISPLAY_VALUE:
        binding["review_descriptor"].update(
            value_precision="display_only", exact_raw_axvalue_proven=False,
            no_write_authority=True,
            limit="Observed display string only; may be trimmed or a placeholder. Exact editor buffer, committed content and saved output are not proved.")
    if policy.get("mode") == _STATIC_SLOT:
        binding["review_descriptor"].update(
            sibling_index=identity["sibling_index"], sibling_roles=deepcopy(identity["sibling_roles"]),
            static_landmarks=deepcopy(identity["static_landmarks"]),
            limit="Rendered display slot only; persistent AX object identity and global coverage unproved")
        if identity.get("anchor_basis"):
            binding["review_descriptor"].update(
                anchor_basis=identity["anchor_basis"], native_anchors=deepcopy(identity["native_anchors"]),
                parent_size=deepcopy(identity["parent_size"]),
                layout_change_requires_new_binding=True,
                no_write_authority=True)
    if len(_peers(binding, observation)) != 1:
        raise BindingError("stable_target_identity_ambiguous")
    if retained_review:
        binding["review_capture"] = {
            "observation_sha256": _hash(observation), "retained_evidence_only": True,
            "current_state_proven": False, "action_authority_granted": False}
    binding["id"] = "goal-binding:" + _hash(binding)
    return binding


def check_review_predicate(binding, outcome, observation):
    """Check a preservation value at its exact historical review capture only.

    Deliberately returns matched_at_capture, never the fresh verify() result
    shape. The caller must establish preservation again before any input.
    """
    _check_binding(binding)
    _validate(observation, binding["target"], max_age_s=None)
    capture = binding.get("review_capture", {})
    if (capture.get("retained_evidence_only") is not True
            or capture.get("observation_sha256") != _hash(observation)
            or observation["snapshot_id"] != binding["initial_snapshot_id"]
            or observation["observed_at_ns"] != binding["bound_at_ns"]):
        raise BindingError("review_capture_changed")
    if _hash(outcome) != binding["outcome_sha256"] or outcome.get("id") != binding["outcome_id"]:
        raise BindingError("reviewed_outcome_changed")
    peers = _peers(binding, observation)
    if len(peers) != 1:
        raise BindingError("bound_target_absent_or_ambiguous")
    control = peers[0]
    prop, plane = _property(outcome, control)
    if prop != binding["property"] or plane != binding["evidence_plane"]:
        raise BindingError("bound_evidence_semantics_changed")
    if prop == "value":
        matched = control["value"] == outcome["value"]
    elif prop in ("checked", "selected"):
        matched = control["states"][prop] is outcome["value"]
    else:
        raise BindingError("review_preservation_requires_text_or_state")
    return {"matched_at_capture": matched, "snapshot_id": observation["snapshot_id"],
            "observed_at_ns": observation["observed_at_ns"], "retained_evidence_only": True,
            "current_state_proven": False, "action_authority_granted": False}


def _refresh(binding, observation):
    _check_binding(binding)
    _validate(observation, binding["target"])
    if observation["observed_at_ns"] < binding["bound_at_ns"]:
        raise BindingError("observation_predates_reviewed_binding")
    peers = _peers(binding, observation)
    if len(peers) != 1:
        raise BindingError("bound_target_absent" if not peers else "bound_target_ambiguous")
    return peers[0]


def matches_binding(binding, observation, control_id):
    """Identity-only authority check; never filters on the requested value."""
    try:
        if binding.get("identity_policy", {}).get("read_only") is True:
            return False
        return _refresh(binding, observation)["id"] == control_id
    except (ValueError, TypeError, KeyError):
        return False


def matches_retained_identity(binding, observation, control_id):
    """Resolve a historical choice to a reviewed identity, never authorize input.

    Review/thinking latency may exceed the fresh-evidence limit. Callers must
    still use the ordinary fresh permission and verification path before input.
    No timestamp is changed and a different/ambiguous identity is not accepted.
    """
    try:
        _check_binding(binding)
        if binding.get('identity_policy', {}).get('read_only') is True:
            return False
        _validate(observation, binding['target'], max_age_s=None)
        if observation['observed_at_ns'] < binding['bound_at_ns']:
            return False
        peers = _peers(binding, observation)
        return len(peers) == 1 and peers[0]['id'] == control_id
    except (ValueError, TypeError, KeyError):
        return False


def matches_readback_identity(binding, observation, control_id):
    """Identity-only check for arithmetic display recovery, never input authority."""
    try:
        if binding.get('property')!='numeric_display' or binding.get('evidence_plane')!='display':
            return False
        return _refresh(binding,observation)['id']==control_id
    except (ValueError,TypeError,KeyError):
        return False


def verify(binding, outcome, observation, *, number_format=None):
    """Re-evaluate now. A previous success cannot replace missing evidence.

    This function permits the binding snapshot itself as one current read. A
    caller requiring two independent reads must enforce distinct snapshot IDs.
    """
    try:
        control = _refresh(binding, observation)
        if _hash(outcome) != binding["outcome_sha256"] or outcome.get("id") != binding["outcome_id"]:
            raise BindingError("reviewed_outcome_changed")
        prop, plane = _property(outcome, control)
        if prop != binding["property"] or plane != binding["evidence_plane"]:
            raise BindingError("bound_evidence_semantics_changed")
        if prop == "numeric_display":
            from .goal_planner import verify_arithmetic
            expected = verify_arithmetic(outcome["expression"])
            actual = _display(control)
            interpretation = 'complete_dot_decimal_optional_LRM_RLM'
            if number_format is None:
                number = _numeric(actual)
            else:
                from .number_format import parse_display_number
                interpretation = parse_display_number(actual, number_format,
                    expected_target=observation['target'])
                if interpretation.get('status') != 'parsed_under_observed_locale':
                    raise BindingError('numeric_display_uninterpretable: '+interpretation.get('reason','unknown_locale'))
                number = Fraction(interpretation['numerator'], interpretation['denominator'])
            if number is None:
                raise BindingError("numeric_display_uninterpretable")
            matched = number == Fraction(expected["numerator"], expected["denominator"])
        elif prop == "value":
            actual = control["value"]; matched = actual == outcome["value"]
        else:
            actual = control["states"][prop]; matched = actual is outcome["value"]
        evidence = {"binding_id": binding["id"], "target": deepcopy(observation["target"]),
            "snapshot_id": observation["snapshot_id"], "observed_at_ns": observation["observed_at_ns"],
            "control_id": control["id"], "property": prop, "actual": deepcopy(actual), "plane": plane,
            "coverage_complete": observation.get("coverage", {}).get("complete"),
            "identity_scope": binding["identity_policy"].get("mode", "semantic_control"),
            "persistent_ax_object_proven": False,
            "numeric_interpretation": interpretation if prop == "numeric_display" else None,
            "committed_document_proven": False, "saved_output_proven": False,
            "binding_review_required": True}
        if outcome["kind"] == _DISPLAY_VALUE:
            evidence.update(value_precision="display_only", exact_raw_axvalue_proven=False,
                editor_buffer_proven=False, no_write_authority=True,
                value_projection_limits=deepcopy(control.get("value_evidence", {})))
        return {"matched": matched, "status": "matched" if matched else "mismatch",
                "reason": "bound_predicate_established" if matched else "bound_predicate_not_met", "evidence": evidence}
    except (ValueError, TypeError, KeyError) as error:
        return {"matched": False, "status": "unavailable", "reason": str(error), "evidence": None}
