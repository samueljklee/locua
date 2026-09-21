"""Compact, lossless-discoverable views of a retained UI observation.

No desktop/model IO, task predicates, new handles, or action authorization lives
here. Full observations and action guards remain with the caller. Listing rows
are only a representation; detail fragments must be joined before interpreting
an oversized attribute. Native visibility absent from a capture stays unknown.
"""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
import hashlib
import json
import time

from .engine.prototype.perception import validate_observation
from .engine.prototype.regions import catalog_regions, inspect_region

VERSION = "progressive-ui-v1"
DEFAULT_BYTES = 10000
COLUMNS = ["id", "role", "name", "value", "states", "actions", "parent",
           "region_ref", "membership", "evidence_ref"]
ACTION_COLUMNS = ["id", "kind", "requires_value"]
_SEMANTIC_FIELDS = ("name", "title", "description", "help", "identifier")
_READOUT_ROLES = {"AXStaticText", "AXHeading", "StaticText", "statictext", "text",
                  "heading", "paragraph"}
_NAV_ROLES = {"AXLink", "AXTab", "AXTabButton", "AXMenuBarItem", "AXDisclosureTriangle",
              "link", "tab", "menuitem", "treeitem"}
_EVIDENCE_FIELDS = ("precision", "exact_value_proven", "plane")


class ProgressiveUIError(ValueError):
    pass


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProgressiveUIError("Views require finite JSON evidence") from exc


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _bytes(value):
    # Match the caller's conservative, ASCII-escaped response size accounting.
    return len(json.dumps(value, sort_keys=True, allow_nan=False).encode())


def _cursor(source, query, offset):
    body = {"version": VERSION, "source": source, "query": _hash(query), "offset": offset}
    body["checksum"] = _hash(body)
    return base64.urlsafe_b64encode(_json(body).encode()).decode()


def _offset(cursor, source, query, count):
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError()
        body = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if set(body) != {"version", "source", "query", "offset", "checksum"}:
            raise ValueError()
        checksum = body.pop("checksum")
        if checksum != _hash(body) or body["version"] != VERSION or body["source"] != source or body["query"] != _hash(query):
            raise ValueError()
        offset = body["offset"]
        if type(offset) is not int or not 0 < offset < count:
            raise ValueError()
        return offset
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ProgressiveUIError("Continuation is corrupt, stale, or belongs to another query") from exc


def _compact(value, field, control_id):
    """Defer large whole attributes explicitly, never return truncated literals."""
    if _bytes(value) <= 512:
        return deepcopy(value)
    return {"deferred": True, "control_id": control_id, "detail_field": field,
            "json_bytes": len(_json(value).encode()), "sha256": _hash(value),
            "exact_value_in_this_view": False}


def _fragments(field, value):
    encoded = _json(value)
    parts = [encoded[i:i+512] for i in range(0, len(encoded), 512)]
    return [{"kind": "json_fragment", "field": field, "part": i, "parts": len(parts),
             "encoding": "canonical_json_ascii", "text": part, "sha256": _hash(value),
             "json_chars": len(encoded), "complete_value_in_this_item": False}
            for i, part in enumerate(parts)]


class _View:
    def __init__(self, observation, actions):
        if not isinstance(observation, dict):
            raise ProgressiveUIError("An existing observation is required")
        now_ns = time.time_ns()
        validate_observation(observation, expected_target=observation.get("target", {}), now_ns=now_ns)
        self.age_seconds = (now_ns-observation["observed_at_ns"])/1e9
        if len(observation["controls"]) > 10000:
            raise ProgressiveUIError("At most 10000 controls; no implicit truncation")
        self.observation = observation
        self.catalog = catalog_regions(observation)
        self.controls = {c["id"]: c for c in observation["controls"]}
        self.children = {cid: [] for cid in self.controls}
        for c in observation["controls"]:
            if c.get("parent") in self.children:
                self.children[c["parent"]].append(c["id"])
        if not isinstance(actions, (list, tuple)):
            raise ProgressiveUIError("Actions must be the caller's captured action array")
        self.actions = {cid: [] for cid in self.controls}
        seen = set()
        for action in actions:
            if (not isinstance(action, dict) or not isinstance(action.get("id"), str)
                    or not action["id"] or action["id"] in seen
                    or action.get("control_id") not in self.controls
                    or action.get("snapshot_id") != observation["snapshot_id"]
                    or action.get("target") != observation["target"]
                    or not isinstance(action.get("kind"), str)):
                raise ProgressiveUIError("Actions require unique captured IDs and exact target/snapshot/control binding")
            seen.add(action["id"])
            self.actions[action["control_id"]].append(deepcopy(action))
        self.source = _hash({"observation": observation, "actions": actions})

    def scope(self, region_id):
        if region_id is None:
            return self.observation["controls"], set(self.controls), [], 0
        if not isinstance(region_id, str) or not region_id:
            raise ProgressiveUIError("region_id must be a captured region ID")
        detail = inspect_region(self.observation, region_id, self.catalog)
        text = ([{"kind": "unbound_text", "text": s} for s in detail["model_context_text"]["unbound_lines"]]
                + [{"kind": "context_only_text", "text": s} for s in detail["model_context_text"]["context_only_outline_lines"]])
        return (detail["observation"]["controls"], set(detail["primary_control_ids"]), text,
                len(detail["provenance"]["outside_control_ids"]))

    def row(self, control, membership):
        cid = control["id"]
        values = [cid, control["role"], _compact(control.get("name"), "name", cid),
                  _compact(control.get("value"), "value", cid),
                  _compact(control.get("states", {}), "states", cid),
                  [[a["id"], a["kind"], a.get("requires_value")] for a in self.actions[cid]],
                  control.get("parent"), self.catalog["memberships"][cid], membership,
                  {k: deepcopy(control.get("value_evidence", {})[k]) for k in _EVIDENCE_FIELDS
                   if k in control.get("value_evidence", {})}]
        return values

    def page(self, operation, query, items, *, cursor, limit, max_bytes, extra=None, outside=0):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ProgressiveUIError("limit must be 1..256")
        if type(max_bytes) is not int or not 2048 <= max_bytes <= 12288:
            raise ProgressiveUIError("max_bytes must be 2048..12288")
        start = _offset(cursor, self.source, query, len(items))
        complete = self.observation.get("coverage", {}).get("complete")
        if type(complete) not in (bool, type(None)):
            raise ProgressiveUIError("Source completeness must be boolean or unknown")

        def build(end):
            page_items = deepcopy(items[start:end])
            lookups = {}
            if operation == "list":
                regions, evidence = [], []
                for row in page_items:
                    if not isinstance(row, list):
                        continue
                    if row[7] not in regions:
                        regions.append(row[7])
                    if row[9] not in evidence:
                        evidence.append(row[9])
                    row[7], row[9] = regions.index(row[7]), evidence.index(row[9])
                lookups = {"regions": regions, "evidence_profiles": evidence,
                           "reference_columns": {"region_ref": "regions", "evidence_ref": "evidence_profiles"}}
            return {"version": VERSION, "status": "ok", "operation": operation,
                    "snapshot_id": self.observation["snapshot_id"], "target": deepcopy(self.observation["target"]),
                    "observed_at_ns": self.observation["observed_at_ns"],
                    "capture_age_seconds": self.age_seconds,
                    "retained_observation_only": True, "fresh_capture_required_for_action": True,
                    "items": page_items,
                    "coverage": {"scope": deepcopy(query), "source_complete": complete,
                                 "matched_total": len(items), "returned_count": end-start,
                                 "previous_page_count": start, "remaining_count": len(items)-end,
                                 "enumeration_complete": start == 0 and end == len(items),
                                 "outside_scope_count": outside,
                                 "continuation": _cursor(self.source, query, end) if end < len(items) else None,
                                 "negative_evidence_proven": False, "uniqueness_proven": False},
                    "provenance": {"source_observation_sha256": self.catalog["source_observation_sha256"],
                                   "view_source_sha256": self.source, "creates_handles": False,
                                   "authorizes_actions": False, "full_observation_required_for_guards": True},
                    "representation": {"max_bytes": max_bytes, "items_clipped": False,
                                       "detail_operation": "control", "full_raw_observation_retained_by_caller": True},
                    **deepcopy(extra or {}), **lookups}

        end = min(len(items), start+limit)
        result = build(end)
        if _bytes(result) <= max_bytes:
            return result
        lo, hi, best = start+1, end-1, None
        while lo <= hi:
            mid = (lo+hi)//2
            candidate = build(mid)
            if _bytes(candidate) <= max_bytes:
                best = candidate; lo = mid+1
            else:
                hi = mid-1
        if best is not None:
            return best
        raise ProgressiveUIError("One complete discovery item or its envelope exceeds max_bytes; use control detail or a narrower explicit role/region query. No item was clipped or skipped.")


def overview(observation, *, actions=(), cursor=None, limit=64, max_bytes=DEFAULT_BYTES):
    """Readouts first, then every region relationship and navigation candidate.

    Navigation is a structural role/container hint, never permission or proof of
    an effect. Unknown native visibility is explicitly retained, not called true.
    """
    view = _View(observation, actions)
    items, named_readouts = [], []
    for c in observation["controls"]:
        states = c.get("states", {})
        visibility = states.get("visibility", c.get("source", {}).get("node", {}).get("visibility"))
        if (c["role"] in _READOUT_ROLES and (c.get("name") or c.get("value") is not None)
                and states.get("visible") is not False and states.get("hidden") is not True
                and visibility not in ("hidden", "offscreen", "off_screen", "outside_viewport")):
            destination = named_readouts if c.get("name") else items
            destination.append({"kind": "named_readout" if c.get("name") else "unnamed_readout",
                          "id": c["id"], "role": c["role"],
                          "name": _compact(c.get("name"), "name", c["id"]),
                          "value": _compact(c["value"], "value", c["id"]), "parent": c.get("parent"),
                          "region_id": view.catalog["memberships"][c["id"]],
                          "visibility": visibility, "visible": states.get("visible"),
                          "value_evidence": {k: deepcopy(c.get("value_evidence", {})[k]) for k in _EVIDENCE_FIELDS
                                             if k in c.get("value_evidence", {})}})
    regions = {r["id"]: r for r in view.catalog["regions"]}
    for r in regions.values():
        items.append({"kind": "region", "region_id": r["id"], "label": r["label"],
                      "region_kind": r["kind"], "root_control_id": r["root_control_id"],
                      "parent_region_id": r["parent_region_id"], "child_region_ids": r["child_region_ids"],
                      "counts": deepcopy(r["counts"])})
    items.extend(named_readouts)
    for c in observation["controls"]:
        r = regions[view.catalog["memberships"][c["id"]]]
        structural = c["role"] in _NAV_ROLES
        container = r["kind"] in ("navigation", "toolbar") and bool(view.actions[c["id"]])
        if structural or container:
            items.append({"kind": "navigation_candidate", "id": c["id"], "role": c["role"],
                          "name": _compact(c.get("name"), "name", c["id"]),
                          "region_id": r["id"], "parent": c.get("parent"),
                          "states": _compact(c.get("states", {}), "states", c["id"]),
                          "actions": [[a["id"], a["kind"], a.get("requires_value")] for a in view.actions[c["id"]]],
                          "basis": "observed_role" if structural else "observed_navigation_or_toolbar_region"})
    return view.page("overview", {"kind": "overview"}, items, cursor=cursor, limit=limit, max_bytes=max_bytes,
                     extra={"counts": {"controls": len(view.controls), "regions": len(regions),
                                       "overview_items": dict(Counter(i["kind"] for i in items))},
                            "action_columns": ACTION_COLUMNS, "other_controls_discoverable": "list",
                            "visibility_unknown_is_not_visible_proof": True})


def listing(observation, *, region_id=None, role=None, query=None, actions=(), cursor=None,
            limit=128, max_bytes=DEFAULT_BYTES):
    """Compact whole rows; the same regional scope applies to every filter."""
    view = _View(observation, actions)
    if role is not None and (not isinstance(role, str) or not role):
        raise ProgressiveUIError("role must be an exact observed nonempty role string")
    if query is not None and (not isinstance(query, str) or not query or len(query) > 1024):
        raise ProgressiveUIError("query must be a nonempty semantic-label substring, at most1024 characters")
    controls, primary, texts, outside = view.scope(region_id)
    rows = []
    for c in controls:
        if role is not None and c["role"] != role:
            continue
        labels = {"name": c.get("name"), **{k: c.get("semantics", {}).get(k) for k in _SEMANTIC_FIELDS if k != "name"}}
        if query is not None and not any(isinstance(s, str) and query.casefold() in s.casefold() for s in labels.values()):
            continue
        rows.append(view.row(c, "primary" if c["id"] in primary else "context"))
    # Shared text remains discoverable in its own explicit item, not invented
    # controls. A role/label query makes no claim about this unbound text.
    items = list(rows)
    if role is None and query is None:
        for index, text in enumerate(texts):
            if _bytes(text) <= 768:
                items.append(deepcopy(text))
            else:
                items.extend({**part, "source_kind": text["kind"]}
                             for part in _fragments(f"shared_text:{index}", text["text"]))
    scope = {"kind": "controls", "region_id": region_id, "role": role, "query": query}
    return view.page("list", scope, items, cursor=cursor, limit=limit, max_bytes=max_bytes, outside=outside,
                     extra={"columns": COLUMNS, "action_columns": ACTION_COLUMNS,
                            "relationships": "parent IDs define captured children; control detail lists child IDs",
                            "scope_counts": {"primary": len(primary), "surrounding_context": len(controls)-len(primary),
                                             "matching_controls": len(rows), "unbound_text_items": len(texts)},
                            "query_searches_values": False, "filtered_text_remains_available_in_unfiltered_list": True,
                            "context_controls_are_not_primary_action_candidates": True})


def unpack_row(page, row):
    """Decode one compact row for a caller's read-only ledger; creates no handle."""
    if page.get("operation") != "list" or page.get("columns") != COLUMNS or not isinstance(row, list) or len(row) != len(COLUMNS):
        raise ProgressiveUIError("Expected a complete progressive list row")
    result = dict(zip(COLUMNS, deepcopy(row)))
    for key, table, output in (("region_ref", "regions", "region_id"),
                               ("evidence_ref", "evidence_profiles", "value_evidence")):
        index = result.pop(key)
        if type(index) is not int or not isinstance(page.get(table), list) or not 0 <= index < len(page[table]):
            raise ProgressiveUIError("Invalid page-local semantic reference")
        result[output] = deepcopy(page[table][index])
    if not isinstance(result["actions"], list) or any(not isinstance(a, list) or len(a) != len(ACTION_COLUMNS) for a in result["actions"]):
        raise ProgressiveUIError("Expected complete action descriptor rows")
    result["actions"] = [dict(zip(ACTION_COLUMNS, a)) for a in result["actions"]]
    return result


def exposed_controls(page):
    """Return only IDs/fields actually represented on this page, for a ledger.

    This is not a reconstructed full observation or action authorization.
    Oversized deferred fields/fragments remain explicitly incomplete.
    """
    if page.get("version") != VERSION:
        raise ProgressiveUIError("Expected a progressive UI view")
    if page.get("operation") == "list":
        return [unpack_row(page, row) for row in page.get("items", []) if isinstance(row, list)]
    if page.get("operation") == "overview":
        return [deepcopy(row) for row in page.get("items", [])
                if row.get("kind") in ("unnamed_readout", "named_readout", "navigation_candidate")]
    if page.get("operation") == "control":
        result = {"id": page["control_id"], "partial_detail": True}
        result.update({row["field"]: deepcopy(row["value"]) for row in page.get("items", [])
                       if row.get("kind") == "attribute"})
        return [result]
    raise ProgressiveUIError("Unknown progressive UI operation")


def detail(observation, control_id, *, actions=(), cursor=None, max_bytes=DEFAULT_BYTES):
    """Exact semantics and evidence attributes, losslessly paged if oversized.

    `json_fragment` entries are ordered fragments of one canonical JSON value.
    Join all parts and JSON-decode before interpreting it; a partial fragment is
    not exact evidence. Hashes bind the complete value, not an editable handle.
    """
    view = _View(observation, actions)
    if not isinstance(control_id, str) or not control_id:
        raise ProgressiveUIError("control_id must be a captured control ID")
    if control_id not in view.controls:
        raise ProgressiveUIError("Control is absent from this captured observation")
    c = view.controls[control_id]
    fields = {k: deepcopy(c.get(k)) for k in ("id", "role", "name", "value", "states", "parent",
                                               "bounds", "semantics", "value_evidence", "editor", "capabilities")}
    fields.update(children=view.children[control_id], region_id=view.catalog["memberships"][control_id],
                  actions=view.actions[control_id], observed_actions=deepcopy(c.get("actions", [])))
    items = []
    for field, value in fields.items():
        encoded = _json(value)
        if len(encoded) <= 768:
            items.append({"kind": "attribute", "field": field, "value": value})
        else:
            items.extend(_fragments(field, value))
    return view.page("control", {"kind": "control", "control_id": control_id}, items,
                     cursor=cursor, limit=256, max_bytes=max_bytes,
                     extra={"control_id": control_id, "fragment_reassembly_required": True,
                            "source_implementation_metadata": "retained in caller's full raw observation; not action authority"})
