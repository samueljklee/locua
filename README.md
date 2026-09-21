# Locua

An installable library and CLI for local computer-use experiments. The CLI is a
thin adapter over `locua.lib`; the guarded engine remains independent of an agent
framework. Amplifier Smart Tool conformance does not require Amplifier Core.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[amplifier]'
.venv/bin/locua -h
.venv/bin/locua setup --help
```

Before starting, configure the runtime with `locua setup` (see `locua setup --help`)
and check it with `locua doctor`. Activating the Python environment alone does not
configure the driver or model assets. If configuration already exists elsewhere,
pass `--config /path/to/locua.json` or set `LOCUA_CONFIG` to that path.

Describe an outcome without writing an internal control schema:

```sh
locua "Describe the outcome you want"
locua do "Change the Project title to 'Autumn launch'" --url http://127.0.0.1:8765/project
locua start
locua start --manual
```

By default, native language mode uses **Amplifier Core 1.6.1 with its standard loop and
context modules**, a resident pinned local **7B comparator**, and ordinary tool
calling. Install `.[amplifier]` for this path. The model chooses desktop tools;
no fixed phase hook chooses the sequence. Read/launch/reuse/inspection remain
available before goal binding. Task inputs require a readable `run` review and
fresh guards. Model-visible tool results return to the next model call; full evidence remains local.

**RLCD is not used in the Amplifier path.** `--harness legacy` retains the earlier
7B/RLCD workflow; `run` and `start --manual` retain their baseline defaults and
original RLCD. Explicit URL/document adapters remain separate legacy paths.
`--inspection-policy`, save and browser-route flags apply to those legacy adapters,
not to the Amplifier native tools. Original model weights, RLCD and driver are
unchanged. An explicit `--model qwen38` selects the separately pinned local
`mlx-community/Qwen3.8-27B-4bit` experimental comparator in native Amplifier mode.
It uses ordinary nonthinking tool generation, not RLCD, and is never selected as
a fallback. Its roughly 16 GB weight download and larger runtime memory needs
make it a separate hardware choice; this iteration tests it on a 128 GB M4 Max.
The explicit 27B CLI reuses only an exactly matching conversation prefix; mismatches
recompute the full input. A chunk-aligned checkpoint before the changing compaction
notice passed three fixed-input output-parity comparisons. An earlier candidate
changed outputs and was rejected. Real history compaction can still invalidate
the cache; this does not establish acceptable desktop latency.

Explicit provider selection is available in native `do`/language `start`. Local
inference remains the default; hosted models never replace a failed local run.
These are experimental comparison configurations. No tested configuration has
qualified the six-run desktop acceptance gate; none is designated a winner:

| Selection | Behavior |
| --- | --- |
| `--provider local --model comparator` | Existing 7B default, ordinary tool calling |
| `--provider local --model qwen38` | Existing pinned 27B, nonthinking |
| `--provider local --model qwen38 --thinking` | Explicit 27B thinking experiment; 2048 total reasoning and final-output tokens, prefix cache off |
| `--provider openai --model gpt-5.6-sol` | Official Amplifier OpenAI provider, medium reasoning |
| `--provider anthropic --model claude-opus-5` | Official Amplifier Anthropic provider, medium adaptive thinking |

Hosted setup uses the optional pinned provider dependencies:

```sh
python -m pip install '.[amplifier,hosted-comparison]'
locua "In Calculator, calculate 192 * 231 - 100." \
  --provider openai --model gpt-5.6-sol \
  --budget-ledger /path/to/shared-hosted-budget.json
```

For Anthropic, explicitly use `--provider anthropic --model claude-opus-5`.
Put the corresponding literal `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` assignment
in private `~/.amplifier/keys.env` (mode `0600`). Locua reads that file without
executing it; keep credentials out of commands, task text and artifacts. Hosted
runs need the configured driver and permissions, but no local model cache.

Hosted disclosure is limited to one explicitly named installed application.
Use its literal application name in the request. For a document, include its
exact existing window title in single or double quotes; unmatched or ambiguous
scope stops. The request and scoped UI content are sent to the selected provider.
Unrelated windows, recent-document entries and raw driver metadata stay outside
the model view; full evidence stays in private local traces. This narrow boundary
is not support for arbitrary application aliases or cross-application tasks.
`--task-observations` applies the same boundary to a local comparison run.

Both hosted configurations use ordinary tool calling, not RLCD. They retain
human review and fresh action guards, disable retries/fallback, and bound each
completion to 120 seconds, 24,576 input tokens and 2,048 total output tokens.
One persistent ledger enforces a shared **$15 cap**, reserving spend before every
actual SDK dispatch; uncertain requests keep their reservation. By default it is
`~/Library/Application Support/locua/hosted-budget.json` on macOS. Use the same
`--budget-ledger` path for all comparison runs; do not replace it to reset spend.
Reports distinguish conservative token-price estimates from invoices. Hosted
options do not apply to `--harness legacy`, `--url`, `--document`, manual mode or
reviewed `run`. A connection smoke is not desktop acceptance or a model ranking. The
current Anthropic configuration failed its connection gate with an explicit
service refusal before any tool call; no fallback or prompt change was applied.

A real 7B tool-result-next-call smoke passed after bridge and diagnostic repairs;
its first failed development attempt is retained. Desktop acceptance and model
reliability are separate. `verified_reviewed_scope` means fresh reviewed outcome
predicates passed after a person checked coverage of the original request. It
does not independently prove language fidelity, committed documents or saving.
Private traces include requests, native model output, tool calls/results, tokens,
human review time and execution time. No online inference fallback exists.

The `tools-v6.5` native preview includes compact region/readout overviews, role-based
lists and on-demand control details, with complete page continuations. Retained
exploration and unresolved needs survive context compaction but never authorize
input. Tool contracts reject incompatible fields; informational caveats do not
create a saving requirement. Exact-window recovery remains guarded, and a
per-user lock refuses overlapping desktop sessions before model loading.

The current installed CLI regression gate is **not passed**. Public window
references now accompany retained results. A sole unnamed readout can be bound
using independently identified native sibling anchors; changes to that structure
remain grounds for refusal. Review can use retained evidence, with fresh checks
before input. These repairs do not choose the model's next action.

Two installed `tools-v6.5` OpenAI checks completed an exact TextEdit buffer
replacement after separate fixture resets: 8 model calls each, 50.1 and 46.0
seconds excluding review. Each required one faithful manager-approved review and
passed independent exact buffer readback. Neither issued Save; automatic saving
and disk contents were not certified. The tested request was:

```sh
locua "In TextEdit, replace the entire text in 'Locua-v10-transfer-draft.txt' with 'Status: reviewed locally.'" --provider openai --model gpt-5.6-sol --budget-ledger /path/to/existing-shared-budget.json
```

The named disposable document was already open and readable. A rerun with its
buffer already equal to the desired value would not prove a new edit.

The pinned local 27B remained incomplete on the current Calculator regression:
572.0 seconds excluding review, All Clear plus digits `19`, then timeout. Its
next control and reviewed scope remained available. The hosted frozen variation
also failed: it entered `81-29/4` twice instead of preserving `(81-29)/4`, displayed
73.75, then cleared. Verification rejected completion. No configuration is
promoted on the strength of the editor result or earlier differently versioned
Calculator passes. No permission or user window action is pending.

See manager `docs/research/model-comparison-v7.md` for versioned CLI runs, repeat
checks, exact configuration, costs, limits and the next checkpoint. Earlier
`cli-milestone-v6.md` results remain historical evidence. Original 1.5B/7B RLCD
paths are intact. A separate one-state, three-order selector comparison found
7B reviewed-edit matches in 3/3 orders with both original RLCD and ordinary
one-token selection (mean 6.73 versus 6.96 seconds). This is neither held-out
accuracy nor an end-to-end RLCD speedup.

The prior v9 fixed-phase acceptance failed; retain its results in the manager's
`docs/research/implementation-v9.md`. Do not interpret successful application
launch as successful task execution.

The earlier v8 language gate also failed. Its separate evaluator-authored browser
engine success (two edits plus 22 checked preserves in 22.839s) remains assisted
execution evidence, not natural-language autonomy. Excel cell identity, committed
content, generic native saving and faithful language planning remain open.

Optional `locua start --manual` composes supported text/checkbox edits from observed
fields and exact values; it does not infer the request. `--document FILE` plus
`--native-save-route textedit_shortcut` uses the separate TextEdit adapter only in
manual mode. `--browser-click-route dom_event` explicitly selects synthetic browser
clicks; there is no route fallback. `--json` prints machine-readable results,
`--out NEW_DIRECTORY` selects private evidence, and timing excludes input/review
waits in language mode. `locua do --help` and `locua start --help` explain limits.

For the pinned browser driver, semantic reference typing and explicitly selected
DOM clicks can reach controls marked near the viewport. The review identifies
when the driver may scroll to reach a control. This is a semantic capability,
not proof of physical visibility; hidden, unknown and occluded targets remain
unavailable. No coordinates or layout-specific resizing are added to the loop.

Document saving is a named TextEdit/plain-text capability: it opens an existing
UTF-8 `.txt` file in the background, binds a new observed window and exact initial
buffer, lets local RLCD select the edit, then performs the explicitly selected
Save operation and checks exact post-action buffer/file bytes. The shortcut route
uses Cua's guarded foreground key operation; its standard Command-S contract is
application-specific, not inferred from the accessibility tree. No route/model
fallback is performed. The native `menu` route is retained but its foreground gate
refused in this environment. Refused Save plus autosaved bytes is partial, never
full explicit-save completion. Windows remain open for inspection.

This is guided task composition, not automatic language understanding. Human
choices provide target, desired value and approval; local RLCD still selects
inspection/actions from the full guarded candidate set. The fresh v7 browser
transfer tasks exposed action-selection failures; do not treat the CLI UX as proof
of general reliability. The observed-language entry point remains experimental and failed its current acceptance gate. See the manager checkpoint for measured successes and retained failures.

Configure explicit local runtime/model/driver paths with `locua setup`, inspect
readiness with `locua doctor`, then preview a reviewed task:

```sh
locua run --task reviewed-task.json --out ./preview-001
```

Task mutation through `run` requires `--execute`; `do`/`start` require typing
`run` after review. `run` and manual mode default to the original1.5B backend;
language preview defaults explicitly to7B. Native Amplifier mode uses ordinary
tool calling; only the retained legacy/manual paths use original RLCD for actions.
No online inference fallback is supported. Generic reliability, automatic native
Excel editing, and Windows/Linux runtime support remain unproved.

`locua doctor --probe` reports owned-runtime `diagnostic_paths` when private
stdout/stderr logs were recorded for its launch. These logs can explain a pending
permission gate; `diagnostic_permission_authority` is always false. Readiness still
requires the current daemon's permission report and a successful scoped operation.
Older launches may report no recorded logs; do not restart an active task just to
obtain them.

The package owns executable source; caches, interpreters and driver binaries are
explicit external assets. No private developer paths are encoded in configuration.
See `locua --help` and each capability's `--help` for arguments and remedies.

Two disposable browser examples are available in this source checkout. Start
their loopback server in one terminal, then preview a reviewed plan in another:

```sh
python3 examples/browser-lab/server.py --out /path/to/local-fixture-receipts
locua run --task examples/browser-lab/contact-plan.json --out /path/to/new-preview
```

With the explicit local runtime and driver ready, the same reviewed plan can be
run using `--execute`; use `--model comparator` only when deliberately selecting
the larger experiment. `badge-plan.json` additionally exercises a menu/dialog.
These are fixture tasks, not proof arbitrary websites work. Their localhost URLs
assume the server's default port 8765. Keep the server running while testing and
stop it with Ctrl-C afterward. These checkout examples are separate from wheel
runtime assets; the installed tool does not depend on them.

For a single-command developer check from this checkout, use the acceptance
runner. It starts a private loopback server on an available port, runs one reviewed
plan through the public library, independently checks the saved fixture receipt,
and stops its server and browser session:

```sh
.venv/bin/python tools/browser_preview.py --case contact \
  --config /path/to/locua.json --out /path/to/new-contact-run \
  --model comparator --browser-click-route dom_event --execute
```

Use `--case badge` for the menu/dialog example. `comparator` explicitly selects
the experimental local 7B model with the original RLCD selector; `dom_event`
explicitly requests synthetic browser events because the tested macOS background
trusted-click route was refused. There is no automatic route or model fallback.
Progress appears on stderr; `summary.json` contains timing, receipt verification,
and cleanup results. On the development Mac, both reviewed browser tasks passed:
contact in **14.3 seconds** (2 decisions, 1 edit) and badge/menu/dialog in
**41.2 seconds** (7 decisions, 3 actions), including model startup and cleanup.
Independent receipt checks verified all 5 contact fields and all 3 badge fields.
One earlier contact attempt failed during browser binding before inference; these
development runs are connection evidence, not a general accuracy estimate.

Initial read-only browser binding retries only the driver's structured
`browser_requires_setup` refusal, at most twice, keeping the same PID/window/session
and all ownership checks. Actions and navigation are never retried by this mechanism.
The successful contact run bound on its first attempt; it does not establish the
retry's live effectiveness.

A person still installs/configures the runtime, grants permissions, and reviews
task scope, targets, data and navigation outcomes. Natural-language planning
produces a proposal and is not reliable unattended task interpretation. On the
fresh v7 four-task screen, no task met every completion requirement: two browser
tasks stopped on model selection, and two native tasks verified editor changes
but failed explicit Save. One native file matched exactly; the other had extra
bytes and failed disk verification. These retained failures are separate from
later development checks of region context and the explicit shortcut route.
Live Excel cell transactions remain unproved. Browser and native editing share
the guarded decision loop, with different driver capabilities.

A separate six-case language screen produced zero usable, contract-faithful plans
for either backend. The comparator emitted valid JSON in all six, but its two
accepted plans still mishandled ambiguity or saved-output requirements. Guided
choices are the current input path; they do not solve language interpretation.

Decision inputs are retained in each run's `events.jsonl`. To evaluate the same
inputs independently without desktop access:

```sh
.venv/bin/python tools/export_decisions.py --events /path/to/run/events.jsonl \
  --out /path/to/new-decisions
locua eval --cases /path/to/new-decisions/cases.json --model baseline \
  --config /path/to/locua.json --out /path/to/new-replay
```

The export preserves candidates and inspection history. It does not infer correct
answers from previous choices, so this replay reports validity and latency;
accuracy requires separately reviewed expected answers.

Run package unit tests with `python -m unittest discover -s tests -v`.
After installing into an isolated environment, run the official conformance kit:

```sh
PATH="$PWD/.venv/bin:$PATH" python /path/to/amplifier-smart-tools/conformance/run.py "$PWD"
```

The conformance runner needs its declared PyYAML/Pydantic dependencies; its
documented `uv run` command resolves them. Use the official specification and
runner at revision `70432044f26e2094b5894516adab68aa14f88592` (verified against
upstream main during initial packaging). Passing conformance proves the package
surface, not model reliability or desktop task completion. No repository URL or
package registry release has been published yet.
