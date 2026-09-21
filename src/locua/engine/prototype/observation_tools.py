"""Read-only snapshot views with explicit pagination and uncertainty.

These views never acquire desktop state, create handles, prove absence in an
incomplete capture, or authorize actions. Guards retain the original observation.
"""
from copy import deepcopy
import base64
import hashlib
import json

from .regions import catalog_regions, inspect_region

VERSION = "snapshot-observation-tools-v1"
_SEARCH_FIELDS = ("name", "title", "description", "help", "identifier")


class ObservationToolError(ValueError):
    pass


def _digest(value):
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError) as error:
        raise ObservationToolError("Snapshot views require finite JSON data") from error
    return hashlib.sha256(data).hexdigest()


def _source(observation):
    if not isinstance(observation, dict) or not observation.get("snapshot_id") or not isinstance(observation.get("target"), dict):
        raise ObservationToolError("An identified snapshot and target are required")
    if not isinstance(observation.get("controls"), list) or len(observation["controls"]) > 10000:
        raise ObservationToolError("Snapshot requires at most 10000 controls; no implicit truncation")
    catalog = catalog_regions(observation)
    return catalog, _digest(observation)


def _cursor(source, query, offset, limit):
    body = {"version": VERSION, "source": source, "query": _digest(query),
            "offset": offset, "limit": limit}
    body["checksum"] = _digest(body)
    return base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode()


def _offset(cursor, source, query, limit, count):
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError()
        data = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or set(data) != {"version", "source", "query", "offset", "limit", "checksum"}:
            raise ValueError()
        checksum = data.pop("checksum")
        if checksum != _digest(data) or data["version"] != VERSION or data["source"] != source or data["query"] != _digest(query) or data["limit"] != limit:
            raise ValueError()
        offset = data["offset"]
        if type(offset) is not int or offset <= 0 or offset >= count or offset % limit:
            raise ValueError()
        return offset
    except (ValueError, TypeError, UnicodeError, KeyError) as error:
        raise ObservationToolError("Continuation is corrupt, stale, or belongs to another query/page size") from error


def _page(observation, items, *, operation, query, source, limit, cursor,
          outside_scope=0, metadata=None):
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ObservationToolError("Page size must be 1..256; no unbounded view")
    start = _offset(cursor, source, query, limit, len(items))
    end = min(start + limit, len(items))
    more = _cursor(source, query, end, limit) if end < len(items) else None
    source_complete = observation.get("coverage", {}).get("complete")
    if source_complete not in (True, False, None) or type(source_complete) not in (bool, type(None)):
        raise ObservationToolError("Source completeness must be true, false, or unknown")
    all_on_page = start == 0 and end == len(items)
    return {"version": VERSION, "operation": operation, "items": deepcopy(items[start:end]),
            "coverage": {"scope": deepcopy(query), "source_complete": source_complete,
                         "enumeration_complete": all_on_page,
                         "complete": all_on_page and source_complete is True,
                         "matched_total": len(items), "returned_count": end-start,
                         "omitted_count": len(items)-(end-start), "previous_page_count": start,
                         "remaining_count": len(items)-end, "outside_scope_count": outside_scope,
                         "continuation": more, "negative_evidence_proven": False,
                         "uniqueness_proven": False},
            "metadata": deepcopy(metadata or {}),
            "provenance": {"target": deepcopy(observation["target"]),
                           "snapshot_id": observation["snapshot_id"],
                           "source_observation_sha256": source,
                           "observation_only": True, "authorizes_actions": False,
                           "creates_handles": False, "full_observation_required_for_guards": True}}


def overview(observation, *, limit=32, cursor=None):
    """Enumerate every structural region; no task or expected value influences it."""
    catalog, source = _source(observation)
    by_id = {c["id"]: c for c in observation["controls"]}
    items = []
    for region in catalog["regions"]:
        landmarks = []
        for cid in region["control_ids"]:
            c = by_id[cid]
            if isinstance(c.get("name"), str) and c["name"]:
                entry = {"role": c["role"], "name": c["name"]}
                if entry not in landmarks:
                    landmarks.append(entry)
        items.append({"kind": "region", "region_id": region["id"], "label": region["label"],
                      "region_kind": region["kind"], "parent_region_id": region["parent_region_id"],
                      "counts": deepcopy(region["counts"]), "landmarks": landmarks[:8],
                      "landmarks_omitted": max(0, len(landmarks)-8)})
    return _page(observation, items, operation="overview", query={"kind": "all_regions"},
                 source=source, limit=limit, cursor=cursor,
                 metadata={"all_regions_discoverable": True, "catalog_coverage": catalog["coverage"]})


def inspect(observation, region_id, *, limit=64, cursor=None):
    """Enumerate complete region members plus all competitors/ancestors/shared text."""
    catalog, source = _source(observation)
    detail = inspect_region(observation, region_id, catalog)
    primary = set(detail["primary_control_ids"])
    items = [{"kind": "control", "membership": "primary" if c["id"] in primary else "context",
              "control": c, "handle": detail["observation"]["handles"].get(c["id"])}
             for c in detail["observation"]["controls"]]
    items.extend({"kind": "unbound_text", "text": line}
                 for line in detail["model_context_text"]["unbound_lines"])
    items.extend({"kind": "context_only_text", "text": line}
                 for line in detail["model_context_text"]["context_only_outline_lines"])
    return _page(observation, items, operation="inspect",
                 query={"kind": "region_detail", "region_id": detail["region"]["id"]},
                 source=source, limit=limit, cursor=cursor,
                 outside_scope=len(detail["provenance"]["outside_control_ids"]),
                 metadata={"region": detail["region"],
                           "primary_control_count": len(primary),
                           "context_control_count": len(detail["context_control_ids"]),
                           "competing_control_ids": detail["provenance"]["competing_control_ids"],
                           "context_controls_are_not_action_candidates": True,
                           "inspection_provenance": detail["provenance"]})


def search(observation, text, *, role=None, match="contains", region_id=None,
           limit=32, cursor=None):
    """Search observed labels/semantics only; never filter by desired field values.

    Search narrows inspection, not action authority or proof of unique identity.
    All matches, including identical labels, remain visible through continuation.
    """
    catalog, source = _source(observation)
    if not isinstance(text, str) or not text or len(text) > 1024 or match not in ("contains", "exact"):
        raise ObservationToolError("Require a bounded nonempty label query and exact/contains match")
    if role is not None and (not isinstance(role, str) or not role):
        raise ObservationToolError("Role filter must be an exact nonempty role")
    controls = observation["controls"]
    primary = {c["id"] for c in controls}
    if region_id is not None:
        detail = inspect_region(observation, region_id, catalog)
        controls, primary = detail["observation"]["controls"], set(detail["primary_control_ids"])
    items = []
    for control in controls:
        if role is not None and control["role"] != role:
            continue
        fields = {"name": control.get("name"), **{k: control.get("semantics", {}).get(k) for k in _SEARCH_FIELDS if k != "name"}}
        matches = [k for k, v in fields.items() if isinstance(v, str) and
                   (v == text if match == "exact" else text.casefold() in v.casefold())]
        if matches:
            items.append({"kind": "control", "control": control,
                          "handle": observation.get("handles", {}).get(control["id"]),
                          "region_id": catalog["memberships"][control["id"]],
                          "membership": "primary" if control["id"] in primary else "context",
                          "matched_fields": matches})
    return _page(observation, items, operation="search",
                 query={"kind": "labels", "text": text, "role": role, "match": match, "region_id": region_id},
                 source=source, limit=limit, cursor=cursor,
                 outside_scope=len(observation["controls"])-len(controls),
                 metadata={"searched_controls": len(controls), "nonmatching_control_count": len(controls)-len(items),
                           "search_fields": list(_SEARCH_FIELDS), "values_searched": False,
                           "empty_result_proves_absence": False})


def capabilities(observation):
    """Describe this captured evidence; never probe permission, mutate, or infer cells."""
    catalog, source = _source(observation)
    controls = observation["controls"]
    return {"version": VERSION, "snapshot_id": observation["snapshot_id"],
            "source_observation_sha256": source,
            "overview": True, "inspect": True, "search": True,
            "region_count": len(catalog["regions"]),
            "hierarchy_basis": catalog["coverage"]["hierarchy_basis"],
            "source_complete": observation.get("coverage", {}).get("complete"),
            "addressed_control_count": sum(c["id"] in observation.get("handles", {}) for c in controls),
            "native_raw_editor_observation": "separate_helper_permission_and_binding_required",
            "native_cell_editor_binding": {"supported": False, "reason": "no_proven_cell_to_native_editor_handle_contract"},
            "saved_file_cells": "separate_read_only_observer_not_native_handles",
            "ocr": "annotation_only_no_inferred_editability",
            "creates_handles": False, "authorizes_actions": False}
