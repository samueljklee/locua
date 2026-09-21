"""Read-only OOXML saved-cell evidence with structural, layout-independent lookup.

This adapter does not edit a workbook, calculate formulas, inspect a live Excel
editor, or turn a file address into a native UI action handle.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import io
from pathlib import Path
import posixpath
import re
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_A1 = re.compile(r"\$?([A-Z]{1,3})\$?([1-9][0-9]*)\Z")


class SpreadsheetObservationError(ValueError):
    pass


def _position(address):
    match = _A1.fullmatch(address)
    if not match:
        raise SpreadsheetObservationError("Unsupported cell address")
    column = 0
    for char in match[1]: column = column * 26 + ord(char) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise SpreadsheetObservationError("Cell outside worksheet limits")
    return row, column


def _address(row, column):
    if not 1 <= row <= 1048576 or not 1 <= column <= 16384:
        raise SpreadsheetObservationError("Adjacent cell outside worksheet limits")
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26); letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


def _range(reference):
    parts = reference.split(":")
    if len(parts) not in (1, 2): raise SpreadsheetObservationError("Unsupported cell range")
    start, end = _position(parts[0]), _position(parts[-1])
    if end[0] < start[0] or end[1] < start[1]: raise SpreadsheetObservationError("Inverted cell range")
    return start, end


def _rich_text(node):
    # Preserve text exactly, including whitespace and line breaks; no rendering
    # or normalization. Phonetic annotation runs are not the cell's text value.
    return "".join((child.text or "") for child in node.findall(S + "t") + node.findall(S + "r/" + S + "t"))


def _typed_value(value):
    if not isinstance(value, dict) or set(value) != {"type", "value"}:
        raise SpreadsheetObservationError("Typed literal value is required")
    kind, scalar = value["type"], value["value"]
    valid = (kind in ("string", "error", "iso_date", "number") and type(scalar) is str
             or kind == "boolean" and type(scalar) is bool or kind == "blank" and scalar is None)
    if not valid: raise SpreadsheetObservationError("Invalid typed literal")
    if kind == "number":
        try:
            finite = Decimal(scalar).is_finite()
        except InvalidOperation:
            finite = False
        if not finite: raise SpreadsheetObservationError("Invalid serialized number")
    return value


def _scalar(node, strings):
    kind = node.get("t", "n")
    value = node.find(S + "v")
    text = value.text if value is not None else None
    if kind == "inlineStr":
        inline = node.find(S + "is")
        return {"type": "string", "value": _rich_text(inline) if inline is not None else ""}
    if kind == "s":
        if text is None or not text.isdigit() or int(text) >= len(strings):
            raise SpreadsheetObservationError("Invalid shared-string reference")
        return {"type": "string", "value": strings[int(text)]}
    if text is None:
        return {"type": "blank", "value": None}
    if kind == "b":
        if text not in ("0", "1"): raise SpreadsheetObservationError("Invalid Boolean cell")
        return {"type": "boolean", "value": text == "1"}
    if kind in ("str", "e", "d", "n"):
        return {"type": {"str": "string", "e": "error", "d": "iso_date", "n": "number"}[kind], "value": text}
    raise SpreadsheetObservationError("Unsupported serialized cell type: " + kind)


def snapshot_workbook(path, *, max_uncompressed_bytes=50_000_000):
    path = Path(path).resolve()
    before = path.stat(); data = path.read_bytes(); after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise SpreadsheetObservationError("Workbook changed while reading")
    if len(data) > max_uncompressed_bytes:
        raise SpreadsheetObservationError("Workbook archive exceeds bounded size")
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(e.file_size for e in entries) > max_uncompressed_bytes:
            raise SpreadsheetObservationError("Workbook package exceeds bounded size")
        names = {e.filename for e in entries}
        if len(names) != len(entries): raise SpreadsheetObservationError("Duplicate package part")

        def xml(name):
            raw = archive.read(name)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise SpreadsheetObservationError("XML declarations unsupported")
            return ET.fromstring(raw)

        def relationships(part):
            folder, file = posixpath.split(part)
            name = posixpath.join(folder, "_rels", file + ".rels")
            if name not in names: return {}
            result = {}
            for rel in xml(name).findall(P + "Relationship"):
                if rel.get("TargetMode") == "External": continue
                target = rel.get("Target", "")
                resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target))
                if resolved.startswith("../") or ":" in resolved or resolved not in names:
                    raise SpreadsheetObservationError("Invalid package relationship")
                key = rel.get("Id")
                if not key or key in result: raise SpreadsheetObservationError("Invalid relationship identity")
                result[key] = {"part": resolved, "type": rel.get("Type", "")}
            return result

        workbook = xml("xl/workbook.xml"); rels = relationships("xl/workbook.xml")
        if workbook.tag != S + "workbook": raise SpreadsheetObservationError("Unsupported workbook namespace")
        string_parts = [v["part"] for v in rels.values() if v["type"].endswith("/sharedStrings")]
        if len(string_parts) > 1: raise SpreadsheetObservationError("Ambiguous shared strings")
        shared = [_rich_text(item) for item in xml(string_parts[0]).findall(S + "si")] if string_parts else []
        sheets = []
        for sheet in workbook.findall(S + "sheets/" + S + "sheet"):
            name, relationship = sheet.get("name"), rels.get(sheet.get(R + "id"))
            if not name or not relationship or not relationship["type"].endswith("/worksheet"):
                raise SpreadsheetObservationError("Unsupported worksheet relationship")
            part = relationship["part"]; tree = xml(part); cells = {}
            for node in tree.findall(S + "sheetData/" + S + "row/" + S + "c"):
                reference = node.get("r", ""); row, column = _position(reference)
                address = _address(row, column)
                if address in cells: raise SpreadsheetObservationError("Duplicate cell identity")
                formula = node.find(S + "f")
                scalar = _scalar(node, shared)
                _typed_value(scalar)
                cells[address] = {"address": address, "row": row, "column": column,
                                  "stored": scalar if formula is None else None,
                                  "formula": None if formula is None else {"text": formula.text or "", "attributes": dict(formula.attrib)},
                                  "cached": scalar if formula is not None else None,
                                  "cache_freshness": "unknown" if formula is not None else None,
                                  "style_index": node.get("s"), "native_handle": None}
                if len(cells) > 100000: raise SpreadsheetObservationError("Worksheet exceeds bounded cell count")
            table_rels = relationships(part); tables = []
            for link in tree.findall(S + "tableParts/" + S + "tablePart"):
                relation = table_rels.get(link.get(R + "id"))
                if not relation or not relation["type"].endswith("/table"):
                    raise SpreadsheetObservationError("Unsupported table relationship")
                table = xml(relation["part"]); start, end = _range(table.get("ref", ""))
                if end[0] - start[0] > 100000: raise SpreadsheetObservationError("Table exceeds bounded row count")
                columns = [column.get("name") for column in table.findall(S + "tableColumns/" + S + "tableColumn")]
                if len(columns) != end[1] - start[1] + 1 or any(not name for name in columns) or len(set(columns)) != len(columns):
                    raise SpreadsheetObservationError("Ambiguous table columns")
                header = int(table.get("headerRowCount", "1")); totals = int(table.get("totalsRowCount", "0"))
                if header not in (0, 1) or totals not in (0, 1): raise SpreadsheetObservationError("Unsupported table row metadata")
                tables.append({"name": table.get("name"), "display_name": table.get("displayName"),
                               "range": table.get("ref"), "columns": columns,
                               "first_data_row": start[0] + header, "last_data_row": end[0] - totals,
                               "first_column": start[1]})
            merged = [item.get("ref", "") for item in tree.findall(S + "mergeCells/" + S + "mergeCell")]
            for reference in merged: _range(reference)
            sheets.append({"name": name, "part": part, "cells": cells, "tables": tables, "merged_ranges": merged})
        if len({s["name"] for s in sheets}) != len(sheets): raise SpreadsheetObservationError("Duplicate sheet names")
    return {"schema": "locua.saved_workbook.v1", "plane": "saved_file", "precision": "serialized_cells",
            "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "observed_at_ns": time.time_ns(),
            "size_bytes": len(data), "sheets": sheets, "calculation_performed": False,
            "native_cell_identity_proven": False, "editor_buffer_proven": False,
            "committed_document_proven": False, "creates_action_handles": False}


def _cell(sheet, row, column):
    address = _address(row, column)
    for reference in sheet["merged_ranges"]:
        start, end = _range(reference)
        if start[0] <= row <= end[0] and start[1] <= column <= end[1] and start != end:
            raise SpreadsheetObservationError("Merged target requires an explicit supported structural rule")
    return deepcopy(sheet["cells"].get(address, {"address": address, "row": row, "column": column,
                    "stored": {"type": "blank", "value": None}, "formula": None, "cached": None,
                    "cache_freshness": None, "native_handle": None}))


def resolve_cell(snapshot, selector):
    """Resolve one saved-file cell by table/key or unique adjacent label."""
    if snapshot.get("schema") != "locua.saved_workbook.v1" or not isinstance(selector, dict):
        raise SpreadsheetObservationError("Invalid workbook observation or selector")
    sheets = [s for s in snapshot["sheets"] if s["name"] == selector.get("sheet")]
    if len(sheets) != 1: raise SpreadsheetObservationError("Exact unique sheet is required")
    sheet = sheets[0]
    if set(selector) == {"sheet", "table", "column", "row_key"}:
        if any(not isinstance(selector[k], str) or not selector[k] for k in ("table", "column")):
            raise SpreadsheetObservationError("Exact nonempty table and column names are required")
        tables = [t for t in sheet["tables"] if selector["table"] in (t["name"], t["display_name"])]
        if len(tables) != 1: raise SpreadsheetObservationError("Table is missing or ambiguous")
        table = tables[0]; key = selector["row_key"]
        if not isinstance(key, dict) or set(key) != {"column", "equals"} or key["column"] not in table["columns"] or selector["column"] not in table["columns"]:
            raise SpreadsheetObservationError("Table selector has unknown columns")
        expected = _typed_value(key["equals"])
        key_column = table["first_column"] + table["columns"].index(key["column"])
        rows = [row for row in range(table["first_data_row"], table["last_data_row"] + 1)
                if _cell(sheet, row, key_column)["stored"] == expected]
        if len(rows) != 1: raise SpreadsheetObservationError("Table row key is missing or ambiguous")
        cell = _cell(sheet, rows[0], table["first_column"] + table["columns"].index(selector["column"]))
    elif set(selector) == {"sheet", "label", "relation"} and selector["relation"] in ("right", "below", "unique_nonblank_right"):
        if not isinstance(selector["label"], str) or not selector["label"]:
            raise SpreadsheetObservationError("Exact nonempty label is required")
        labels = [c for c in sheet["cells"].values() if c["stored"] == {"type": "string", "value": selector["label"]}]
        if len(labels) != 1: raise SpreadsheetObservationError("Label is missing or ambiguous")
        source = labels[0]; _cell(sheet, source["row"], source["column"])
        if selector["relation"] == "unique_nonblank_right":
            # This explicit relationship examines every saved cell to the right
            # on the same row. It does not pick the nearest, use expected values,
            # infer meaning from labels, or silently cross to another row.
            candidates = [c for c in sheet["cells"].values()
                          if c["row"] == source["row"] and c["column"] > source["column"]
                          and (c["formula"] is not None
                               or c["stored"] is not None and c["stored"]["type"] != "blank"
                               and not (c["stored"]["type"] == "string" and c["stored"]["value"] == ""))]
            if len(candidates) != 1:
                raise SpreadsheetObservationError("Rightward nonblank cell is missing or ambiguous")
            cell = _cell(sheet, source["row"], candidates[0]["column"])
        else:
            cell = _cell(sheet, source["row"] + (selector["relation"] == "below"),
                         source["column"] + (selector["relation"] == "right"))
    else:
        raise SpreadsheetObservationError("Unsupported structural cell selector")
    return {"plane": "saved_file", "workbook_sha256": snapshot["sha256"], "sheet": sheet["name"],
            "selector": deepcopy(selector), "cell": cell, "native_handle": None,
            "editable_native_target_proven": False}


def verify_saved_cells(path, expectations):
    """Verify literal/formula serialization; cached results are never fresh proof."""
    snapshot = snapshot_workbook(path); results = []
    for expectation in expectations:
        if not isinstance(expectation, dict) or set(expectation) != {"selector", "expected"}:
            raise SpreadsheetObservationError("Invalid saved-cell expectation")
        resolved = resolve_cell(snapshot, expectation["selector"]); cell = resolved["cell"]
        expected = expectation["expected"]
        if isinstance(expected, dict) and set(expected) == {"formula"} and isinstance(expected["formula"], str):
            formula = cell["formula"]
            status = "pass" if formula is not None and formula["text"] == expected["formula"] else "fail"
            reason = "formula_serialization_only"
        elif isinstance(expected, dict) and set(expected) == {"type", "value"}:
            _typed_value(expected)
            status = "unknown" if cell["formula"] is not None else "pass" if cell["stored"] == expected else "fail"
            reason = "formula_cache_not_fresh_proof" if cell["formula"] is not None else "literal_serialization"
        else:
            raise SpreadsheetObservationError("Expected typed literal or formula required")
        results.append({"status": status, "reason": reason, "observation": resolved, "expected": deepcopy(expected)})
    return {"schema": "locua.saved_cells_verification.v1", "plane": "saved_file", "sha256": snapshot["sha256"],
            "status": "pass" if results and all(r["status"] == "pass" for r in results) else "fail" if any(r["status"] == "fail" for r in results) else "unknown",
            "results": results, "calculation_performed": False, "native_editor_proven": False}
