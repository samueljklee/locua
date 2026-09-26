# Capability skills

Non-help results are one JSON object on stdout, except interactive `start` and `do`, which
prints a concise result by default and offers `--json`. Progress and review are
on stderr. Exit 0
means the requested capability's documented result succeeded; invalid arguments
exit 2, missing dependencies 3, unsupported operations 4, runtime faults 5,
blocked/incomplete execution 6, cancellation 130. Help and manifest never need
models. Library callers receive dictionaries or `LocuaError` with a remedy.

## manifest

Read the installed canonical manifest. Arguments: only optional `--config`
(ignored for this operation). Example: `locua manifest`. Result contains
`frontmatter` and Markdown `body` in the shared JSON envelope. No model, driver,
configuration or network is needed. Missing packaged manifest is an installation
failure; reinstall the complete package.

## doctor

Inspect local configuration and runtime readiness without loading a model.
Arguments: `--config PATH` selects configuration; `--probe` additionally invokes
the bounded engine/component status checks. Example: `locua doctor --probe`.
The result lists paths, missing prerequisites and separate permission status.
A probe also uses the configured interpreter to compare installed package versions
to the bundled lock through importlib.metadata, under a five-second bound. It
does not import MLX or load models; wrong/missing environment packages have named
remedies. A completed diagnostic exits 0 even when `ready` is false; readiness is data,
not a claim an automation task succeeded. A failed diagnostic exits nonzero.
No permission prompts, driver activation, downloads or inference are implicit.

The owned runtime may include `diagnostic_paths.stdout` and
`diagnostic_paths.stderr`, plus `diagnostic_status`. These are private, launch-bound
files; the tool reports their paths without printing their contents. An older launch
can report `not_recorded`; missing or mismatched files are unavailable.
`diagnostic_permission_authority:false` means log text never establishes grants,
ownership or readiness. Use the current owned daemon's permission report; do not
restart a running task merely to obtain logs.

## setup

Write local configuration. Arguments: `--config PATH` selects its destination;
`--runtime-python PATH`, `--model-cache PATH`, `--driver-binary PATH`,
`--driver-socket PATH`, and `--driver-app PATH` specify explicit local assets.
`--replace` permits replacing an existing configuration. `--activate-driver`
explicitly asks the configured engine to activate its own driver application;
that optional operation can fail on missing assets or permissions and never
loads a model. Without it, setup only writes a private config file.

Example: `locua setup --runtime-python /path/to/runtime/bin/python --model-cache /path/to/cache`.
The result names `config_path` and resolved settings. Null fields remain
unconfigured and doctor reports them; writing config does not establish readiness.
The library equivalent is `locua.lib.setup(values, path=path)`.

New driver launches keep Cua's default agent cursor overlay enabled. It appears
during driver activity and fades after inactivity; model inference alone does
not move it. A driver already launched with the overlay disabled needs to be
restarted after any active Locua task finishes. The overlay is visual feedback,
not proof that a requested action succeeded.

## targets

List desktop windows and exact target identities without changing focus or
dispatching actions. Arguments: `--pid N` optionally limits results to one
process; `--config PATH` chooses the configured driver. Example: `locua targets`.
The result contains observed window metadata with PID/window ID; it can include
private application/window titles and stays local. Listing is not authorization
to control every returned window. Use an explicit target scope for subsequent
observation or execution. Missing driver/permissions fail with a named remedy.
Library equivalent: `locua.lib.targets(pid=None, config=config)`.

## observe

Inspect supplied observation data or acquire one explicitly scoped desktop view.
Arguments: exactly one of `--snapshot FILE` (normalized JSON, no live acquisition)
or `--target FILE` (exact driver target JSON). Live acquisition uses `--kind browser`
or `--kind native`. `--view overview|inspect|search|full` selects the view;
`--region-id ID` is required for inspect and optionally scopes search;
`--query TEXT` is required for search. `--limit N` chooses 1..256 items per page
(default 32; inspect 64), and `--cursor TOKEN` continues the same view. These
pagination options do not apply to full. Unused view options are rejected.
`--config PATH` selects runtime configuration; `--out PATH` selects a trace/output
directory when the engine writes artifacts. Example:
`locua observe --snapshot observation.json --view overview`.

Continue using the returned `observation_file` with `--snapshot`, the returned
`coverage.continuation` token, and the same view, region, query and page size.
Live recapture produces a different snapshot and makes the old cursor stale;
it cannot silently continue an older observation.

Results report observation identity and coverage. Partial pages do not prove
absence or uniqueness. Duplicate controls and omissions stay explicit. This
capability performs no task actions or model inference. Missing driver, unknown
target, unavailable permissions, stale identity and unsupported views fail with
named remedies. Supply actual data to `locua.lib.observe(snapshot=data)` when
composing library calls, rather than passing a file reference.

## run

Use local models to propose or execute one scoped task. Arguments: exactly one of
`--task FILE` (reviewed `locua-task-plan-v1` outcome plan JSON) or `--request TEXT`
(natural-language proposal). Natural-language input requires `--scope FILE` with
explicit scope JSON. `--data FILE` supplies literal task data. `--model baseline`
is the original backend; `--model comparator` explicitly selects the larger local
experiment. `--execute` permits guarded actions for a reviewed plan supplied
through `--task`; default is preview. Natural-language `--request` returns a
reviewable plan and refuses `--execute` until that plan has been reviewed and
passed via `--task`.
`--browser-click-route trusted|dom_event` explicitly chooses the click route
(default `trusted`). The current macOS standalone driver refuses trusted
background clicks; use `--browser-click-route dom_event` for synthetic DOM clicks
when the target supports them. No automatic route fallback occurs. This does not
prove compatibility with sites that require trusted user input.
`--config PATH` selects runtime assets and `--out PATH` names a new local artifact
directory. Example: `locua run --task reviewed-task.json --out ./preview-001`.

The result includes status and local evidence/artifact paths. Completion requires
the declared predicates and preserves evidence-plane limitations. A planner
proposal may require review or clarification; JSON validity is not intent fidelity.
No task recipe or expected evaluator output supplies actions. All inference stays
local. Missing assets, unsupported native editing, ambiguity, stale targets,
out-of-scope choices, token limits, uncertain effects and exhausted budgets stop
with nonzero exit status. Do not automatically replay an uncertain action.
Use `locua.lib.run(task=data, execute=False, ...)` for composition.
The library accepts the same `browser_click_route` option. Specify roles when a
label and its control share the same name; ambiguity is a refusal. An unnamed
native editor can be described by its exact role plus a named window ancestor,
provided that freshly observed combination identifies exactly one control.

## eval

Evaluate recorded decisions or explicit simulated tasks with local models.
Arguments: `--cases FILE` contains case data; `--model baseline|comparator` chooses
the explicit local backend; `--config PATH` selects runtime assets; `--out PATH`
names a new evidence directory. Example:
`locua eval --cases simulation-cases.json --out ./evaluation-001`.

The result retains failures, abstentions and unsupported cases. Exit 0 means the
evaluation finished, not that every case passed: inspect `completed`, `total`,
and each case's status. It is simulated evidence, not proof of desktop reliability.
Cases must use the documented engine schema; invalid case envelopes fail rather
than being repaired or silently excluded. By default a simulation fixture contains `id`, `controls`,
and a reviewed `reference_plan`; the engine's simulator may also consume explicit
`effects`. No evaluator gold is passed to the selector.
This model-backed operation needs local runtime/cache assets but no desktop
permission. It does not dispatch GUI actions or expose gold to the selector.
Library equivalent: `locua.lib.evaluate(cases=data, ...)`.

For recorded one-step decisions, pass this explicit bundle instead:

```json
{
  "kind": "decisions",
  "cases": [{
    "id": "recorded-001",
    "goal": "Choose the next permitted action for the recorded state.",
    "observation_summary": "The local form contains a blank Name field.",
    "candidates": [
      {"id": "fill-name", "description": "Enter Ada into Name"},
      {"id": "save", "description": "Press Save"}
    ],
    "expected_id": "fill-name"
  }]
}
```

Supply 1..100 uniquely identified cases. Each model input contains only the
unchanged goal, observation summary, candidate catalog and optional `history`
(default `[]`). History is an ordered list of at most 64 exact
`{"action": "...", "outcome": "..."}` entries. Preserve captured inspection
history when replaying a real decision; silently dropping it changes the input.
All case structures, byte bounds and catalogs are validated before loading a
single resident selected model; the worker's token limit still applies without
truncation. Candidate order is preserved and at most 254 candidates are supported.
`expected_id` is optional, is never sent to the model, and must name a candidate
or be null to expect abstention. Omitting it leaves that case unscored.

The result separates `valid_outputs` from `scoring.correct` and
`scoring.incorrect`. A valid wrong choice is a scored failure, not a protocol
error. Worker errors or invalid output stop the batch. Cancellation retains
partial evidence, discards scoring for a response received after cancellation,
and reports `attempted`, `unrun`, `total` and `scoring.ungraded_expected`.
Each local case artifact retains the raw response and call wall time; the summary
records model startup and end-to-end time. `cases.json` contains gold separately
from `model-inputs.json`. These files are private local artifacts (mode 0600).
Recorded decisions are offline selection evidence, not live task completion or
automatic evidence that a Cua-derived observation/candidate mapping is correct.

## start

Describe an ordinary-language outcome; no control IDs, schema or prepared action
sequence is required. `locua "outcome"`, `locua do "outcome"`, and `locua start
"outcome"` share the same library entry. With no request, Locua asks for one.

```sh
locua "Open Calendar and switch to Day view."
```

The native preview defaults to local **qwen38 (Qwen3.8-27B)**, **step-v2** tools,
**continuity-v1** instructions and Amplifier ordinary nonthinking tool calling.
**RLCD is not used in this path.** The same selections can be made explicitly:

```sh
locua "Open Calendar and switch to Day view." --model qwen38 \
  --tool-profile step-v2 --instruction-profile continuity-v1
```

Locua discovers the app and UI, inspects relevant regions, asks about genuine
ambiguity, and presents a readable review. Requested app opening/activation can
precede review. Check the entire request and its preservation constraints, then
type `run` to authorize task input; another response cancels. Fresh guards check
targets before input. The model may revise its next step after observations.
No model statement or successful launch alone proves the requested result.

Progress and review go to stderr. Final output distinguishes verified outcomes
from blocked or incomplete tasks. `--json` returns the full structured result;
`--out NEW_DIRECTORY` chooses private artifacts and `--config FILE` selects runtime
configuration. Default macOS runs are under `~/Library/Application Support/locua/runs/`.
Reports include model/tool traces, calls, tokens and elapsed time excluding human
review. Keep logs private: they can contain desktop content.

`--model comparator` uses local 7B; `--model baseline` uses local 1.5B. Both retain
the default tools/instructions in native mode, and both remain unqualified.
`--tool-profile` and `--instruction-profile` preserve older comparison options.
See `docs/profiles.md` for the exact compatibility matrix, hosted selection,
`--thinking`, and original RLCD paths. No automatic model or provider fallback.

`--harness legacy`, explicit `--url URL`, and `--document FILE` select retained
limited adapters with their original 7B/RLCD selection defaults. They do not use
the new native loop or its default profiles. Browser route, inspection policy and
native save route flags configure those adapters only. The native loop does not
provide general saved-file verification; buffer text is not proof of persistence.
Required or forbidden backing-file changes are currently blocked before editing.

The synthetic cursor marks the last inspected window/control evidence, not the
model's internal attention. Retained state is stale until refreshed. Current
macOS display topology is captured at driver startup; dynamic monitor changes
remain unvalidated. See `docs/gaps.md` for the hidden-window recovery issue and
other limits. The selected default is a usable preview configuration, not a
qualified completion-rate or latency guarantee.

Library equivalent: `locua.lib.do(request, ask=callback, progress=callback)`.
Deterministic help/configuration never loads a model or starts desktop services.

### Optional manual/debug workflow

`locua start "outcome"` routes to the reviewed language workflow described under `do`. With no request, `locua start` asks for the desired outcome. `locua start --manual` lists already-open native windows for guided field editing. `locua start -h` gives a
short terminal guide; `--help` returns this fuller Smart Tool guide.

```sh
locua start --manual --url http://127.0.0.1:8765/project --model comparator
locua start --manual --document /absolute/path/notes.txt --model comparator --native-save-route textedit_shortcut
locua start --manual
```

Choose supported text fields or checkboxes by their observed names and context.
Enter an exact value, or `@/path/to/value.txt` for UTF-8 multiline text. `@@` enters
one literal initial `@`. Finish field selection with Enter. Review the before/after
values, preserved fields, selected model and verification scope; type `run` to
approve. Any other answer cancels before edits. The last command lists native
windows to choose; document saving requires the explicit `--document` path.

Manual mode defaults to original 1.5B RLCD; native language mode uses the 27B preview described below. `comparator` explicitly uses experimental
7B with original RLCD. `dom_event` explicitly selects synthetic browser clicks;
there is no automatic fallback. Progress/review print to stderr; stdout contains
a concise result (use `--json` for full structured output). `--out NEW_DIRECTORY`
controls local artifacts. Total elapsed time includes review. A canceled/blocked
workflow returns nonzero and records its reason.

The native window list comes from the operating system and displays the driver's
on-screen/desktop status, keeping off-screen entries available for explicit choice.
It does not guarantee that a listed window is still open or exposes readable or
writable controls. If the driver cannot
bind that window's accessibility surface, Locua explains the limitation and offers
`retry` for the same window, `choose` for a fresh window selection, or Enter to stop.
Discovery allows at most three reads, all before plan review or inference. Bringing
the window onto the current desktop and letting it settle may help, but recovery
is not guaranteed. Locua does not automatically change focus or use pixel input.
Each attempt and its cleanup remain in the run artifacts. A failed refresh clears
the previous capture so its controls cannot be used for dispatch.

Near-viewport browser controls may use the pinned driver's semantic reference
typing or explicitly selected DOM-click route, which attempts to reveal the
target before input. The review identifies that access method; it does not prove
physical visibility or occlusion. Hidden, unknown, occluded and unsupported
targets stay unavailable; native and trusted-click visibility rules are unchanged.

This is guided composition, not automatic natural-language interpretation. Hidden
navigation and arbitrary actions are not authored in this mode. All readable
supported fields other than the selected changes become explicit preservation
constraints; unsupported/ambiguous fields are reported. The engine still gets its
complete observation/candidate tools and checks fresh identity before each action.
Runtime assets and permissions require prior setup. Browser discovery creates then
closes an isolated window; execution opens a fresh one and rechecks all constraints.

`--document` is explicitly limited to an existing UTF-8 `.txt` file opened in the
background through the exact system TextEdit application. The terminal retains
normal keyboard input while composing/reviewing. A new window with a complete
matching initial buffer is required; already-open or ambiguous documents stop.
The local RLCD model chooses the editor action. A separate deterministic save
adapter then checks the exact document/window/editor again and verifies saved bytes.

`--native-save-route textedit_shortcut` explicitly uses TextEdit's standard Command-S
contract through Cua's exact-window foreground `press_key` guard. The driver attempts
to restore the prior foreground; independent restoration proof is not exported.
Default `menu` uses an observed Save menu path, whose foreground gate refused on
this development runtime. There is no fallback between routes. A refused Save never
passes the explicit-save requirement merely because autosave produced the bytes.
Shortcut success means an accepted scoped key post plus exact post-action buffer
and file readback; exclusive causal attribution and app-handler acknowledgment are
not claimed. Shortcut mapping: https://support.apple.com/en-us/102650 .

The adapter does not support Save As, new unnamed documents, rich text or arbitrary
native applications. Document windows remain open for the user; driver sessions close.
Use `do` for the language workflow, or `run --request` for a separate experimental language proposal; no schema-valid
proposal is automatically treated as reviewed authority.

## do

Describe an ordinary-language outcome; no control IDs, schema or prepared action
sequence is required. `locua "outcome"`, `locua do "outcome"`, and `locua start
"outcome"` share the same library entry. With no request, Locua asks for one.

```sh
locua "Open Calendar and switch to Day view."
```

The native preview defaults to local **qwen38 (Qwen3.8-27B)**, **step-v2** tools,
**continuity-v1** instructions and Amplifier ordinary nonthinking tool calling.
**RLCD is not used in this path.** The same selections can be made explicitly:

```sh
locua "Open Calendar and switch to Day view." --model qwen38 \
  --tool-profile step-v2 --instruction-profile continuity-v1
```

Locua discovers the app and UI, inspects relevant regions, asks about genuine
ambiguity, and presents a readable review. Requested app opening/activation can
precede review. Check the entire request and its preservation constraints, then
type `run` to authorize task input; another response cancels. Fresh guards check
targets before input. The model may revise its next step after observations.
No model statement or successful launch alone proves the requested result.

Progress and review go to stderr. Final output distinguishes verified outcomes
from blocked or incomplete tasks. `--json` returns the full structured result;
`--out NEW_DIRECTORY` chooses private artifacts and `--config FILE` selects runtime
configuration. Default macOS runs are under `~/Library/Application Support/locua/runs/`.
Reports include model/tool traces, calls, tokens and elapsed time excluding human
review. Keep logs private: they can contain desktop content.

`--model comparator` uses local 7B; `--model baseline` uses local 1.5B. Both retain
the default tools/instructions in native mode, and both remain unqualified.
`--tool-profile` and `--instruction-profile` preserve older comparison options.
See `docs/profiles.md` for the exact compatibility matrix, hosted selection,
`--thinking`, and original RLCD paths. No automatic model or provider fallback.

`--harness legacy`, explicit `--url URL`, and `--document FILE` select retained
limited adapters with their original 7B/RLCD selection defaults. They do not use
the new native loop or its default profiles. Browser route, inspection policy and
native save route flags configure those adapters only. The native loop does not
provide general saved-file verification; buffer text is not proof of persistence.
Required or forbidden backing-file changes are currently blocked before editing.

The synthetic cursor marks the last inspected window/control evidence, not the
model's internal attention. Retained state is stale until refreshed. Current
macOS display topology is captured at driver startup; dynamic monitor changes
remain unvalidated. See `docs/gaps.md` for the hidden-window recovery issue and
other limits. The selected default is a usable preview configuration, not a
qualified completion-rate or latency guarantee.

Library equivalent: `locua.lib.do(request, ask=callback, progress=callback)`.
Deterministic help/configuration never loads a model or starts desktop services.
