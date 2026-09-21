# Excel cell adapter (experimental, live acceptance pending)

`locua.engine.excel.ExcelAdapter` is an explicit application adapter, separate
from generic Cua controls. It uses Excel's installed Apple-event dictionary to
address an already-running PID, executable, workbook name/full name, worksheet
and one contiguous A1 range (at most 64 cells). There are no invoice labels,
fixed cell addresses, screen coordinates, focus shortcuts or script interpolation.
It does not launch Excel, enable macros, run VBA or change global settings.

`engine/native/excel_events.swift` compiles to a local helper. Its exact PID
address comes from `NSAppleEventDescriptor(processIdentifier:)`. It verifies the
running process's bundle identifier/executable before and after each event and
rechecks workbook full name and range address. `AEDeterminePermissionToAutomateTarget`
is always called with `askUserIfNeeded:false`; every event also uses
`neverInteract` with a deadline. Missing Automation permission returns explicitly.
The helper has a separate execution identity from Cua; Cua Accessibility/Screen
Recording do not establish permission for it. No borrowed grants, identity
impersonation or automatic permission onboarding are implemented here.

```python
from locua.engine.excel import ExcelAdapter, AppleEventExecutor
transport = AppleEventExecutor(explicit_helper_path, pinned_binary_sha256)
adapter = ExcelAdapter({
    'pid': exact_excel_pid,
    'executable': exact_excel_executable,
    'workbook_name': exact_workbook_name,
    'workbook_full_name': exact_excel_reported_full_name,
    'sheet': exact_sheet_name,
}, transport)
permission = adapter.doctor()          # no prompt; no cell reads
snapshot = adapter.observe(explicit_a1_range)
address = adapter.resolve_label(snapshot, explicit_label, 'unique_nonblank_right')
result = adapter.set_cell(snapshot, address, {'type':'string','value':literal},
                          authorize_write=True)
```

Callers may supply an explicit observed address instead of resolving a label.
`right`, `below`, and `unique_nonblank_right` are explicit structural choices
within the full requested range. Unknown, unstable, duplicate or competing cells
refuse unique binding. Named-table resolution is not implemented in this first
transport; callers must not imply a table model from a rectangular range.

Source start/end timestamps must be present, ordered and bounded; missing times
do not acquire fresh evidence. Snapshot receipts are local to one adapter, expire
after 30 seconds and are
consumed before a write. The helper freshly compares the captured value/formula
before issuing one mutation. Workbook read-only, worksheet protection, merged
cells and array membership must explicitly permit a single-cell write. Supported
requested values are string, finite binary64 number, boolean and explicit A1
formula. Leading `=` strings require a separate text-only route and are refused;
blank clearing, dates, errors and unsupported descriptor types are not guessed.
Excel may interpret/normalize an input; readback must match the requested typed
value. A timeout/error after dispatch is unknown effect and never auto-retried.

Evidence is deliberately separate:

- `value2`, `formula`, and `has_formula` are direct typed object-model facts.
  A matching repeated readback supports `committed_document` for that cell.
- No object-model read/write proves native editor focus, UI edit-buffer state,
  selected cell, or an AX handle. Those fields remain unproved.
- `save(..., authorize_save_workbook=True)` acknowledges a scoped workbook save
  request only. It requires authorization for *all pending changes* in that file.
  Neither an acknowledgement nor Excel's `saved` flag proves file bytes.
- `verify_saved_addresses(path, sheet, expected)` independently reads OOXML and
  proves stored literals/formula serialization by file hash. Formula cached
  results are never treated as freshly calculated output.

Source contract: `/Applications/Microsoft Excel.app/Contents/Resources/Excel.sdef`
workbook `X141`, full name `1773`, read only `1830`, saved `1842`; worksheet
`XwSH`; range `X117`, value2 `DPV2`, formula `1562`, has formula `1573`, merged
`1588`, array membership `1572`; get address `sTBL1515`. Save `coresave` comes
from its included `/System/Library/ScriptingDefinitions/CocoaStandard.sdef`.
Apple's SDK `NSAppleEventDescriptor.h` defines typed PID descriptors and bounded
Apple-event sending. The dictionary's existence is not runtime acceptance.

Validation: helper compiled on this Mac; eight pure descriptor/range checks sent
zero Apple events/permission queries. Fourteen Python tests use synthetic peers and
saved XML, covering target/receipt binding, competitors, stale state, exact empty
and whitespace text, unknown effects, merged/array refusal and saved-file planes.
No live helper/Excel invocation has been performed by the implementation worker.
Next acceptance requires an explicit no-prompt doctor result, disposable workbook
read/write/readback, and independent saved-file verification under the actual
helper permission identity. The current LocuaDriver app binary was not changed.
