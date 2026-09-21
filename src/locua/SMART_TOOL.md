---
{
  "smart_tool_format": 1,
  "name": "locua",
  "version": "0.1.0",
  "description": "Proposes and executes explicitly scoped computer-use tasks with fresh observation guards. Local models are the default; hosted comparison providers require explicit selection. Use for automation experiments where action evidence and safe stopping matter.",
  "use_cases": [
    "Inspect a desktop target without sending its contents to an online model",
    "Preview a reviewed automation task before permitting desktop actions",
    "Compare local action selection on recorded decisions or explicit simulated evaluation cases"
  ],
  "platforms": ["macos"],
  "requires": [
    {"name": "local-model-runtime", "purpose": "Model-backed run and eval need a configured local interpreter, pinned dependencies and cached weights. Manifest, help, configuration and snapshot inspection work without it.", "install": "docs/installation.md", "optional": true},
    {"name": "cua-driver", "purpose": "Live observation and execution need the configured driver and target-specific OS permissions. Supplied snapshot inspection and simulated evaluation do not need live desktop access.", "install": "docs/installation.md", "optional": true}
  ]
}
---
The library is the tool: import `locua.lib` for typed results and compose calls
in Python. The CLI exposes the same functions. Commands print JSON to stdout; interactive `do` and guided `start`
prints a concise result by default and supports `--json`. Progress and review go
to stderr. No model or desktop service starts during import or help.

This is an experimental local automation tool. `do` explicitly uses the local 7B
comparator as its preview default; `run` and manual `start` retain the 1.5B baseline.
Native language mode uses Amplifier's standard loop with ordinary local tool
calling; RLCD is not used in that path. Original RLCD remains unchanged in
`--harness legacy`, explicit URL/document adapters, manual mode and reviewed `run`.
Explicit `--model qwen38` selects the local Qwen3.8-27B-4bit experimental
comparator, only in native Amplifier mode. It uses ordinary nonthinking tool
generation and separately verified model files; there is no automatic fallback.
Add `--thinking` only with local `--model qwen38` for the bounded thinking
experiment: 2048 total reasoning and final-output tokens, with prefix cache off.
A valid plan or schema is not proof of
faithful intent, correct actions, or task completion.

Start with `locua setup --help`, configure explicit local paths, then run
`locua doctor`. Use `locua "describe the outcome"` for local goal interpretation
and plan review, or `locua start --manual` for explicit field selection.
Use `locua do --help` for current interpretation limits. Use `locua run --task task.json --out ./run-preview` to inspect a
proposal. `run` requires `--execute` for desktop mutation; interactive `do` and `start` require
typing `run` before task inputs. The native `do` path can discover, open and activate
an application before review. Failed/unknown desktop effects do not
authorize automatic retries. Use a new output directory for each run and inspect
its task state after an interruption.

Native `do` gives the complete request to the selected model through Amplifier;
the default model remains local.
Discovery, launch/reuse, region inspection, clarification, reviewed task inputs
and fresh verification are tools available throughout the session. Outcomes do
not all need binding before exploration. Explicit `--url`/`--document` retain the
separate observed-field language workflow. Inspection/save/browser-route options
apply to those legacy adapters, not to the new native tool loop.
`start --manual` and reviewed `run --task`
remain separate entry points. Native opening, calculation, text and boolean-state
capabilities are experimental. Unsupported navigation, new-document identity,
preservation or saved-output proof stops rather than weakening the request.
Malformed ordinary tool calls are retained and refused, without silently repaired
arguments. A verified reviewed scope means fresh predicates passed after a human
reviewed request coverage; it is not independent proof of language fidelity.

Hosted comparison is experimental; no desktop acceptance or model ranking is
claimed. The current Anthropic configuration returned a service refusal before
any tool call in its connection gate; no fallback was used.
Native `do`/language `start` accepts explicit
`--provider openai --model gpt-5.6-sol` or
`--provider anthropic --model claude-opus-5`. Install
`.[amplifier,hosted-comparison]`; put literal `OPENAI_API_KEY` and/or
`ANTHROPIC_API_KEY` assignments in private `~/.amplifier/keys.env` (mode `0600`).
The file is read without execution, and keys must not appear in task text or
artifacts. These options use existing official Amplifier providers and ordinary
tool calling, not RLCD; they do not apply to legacy, URL/document, manual or
reviewed `run` paths. Hosted reasoning is fixed at medium; `--thinking` is local-only.

Hosted requests must name one installed application literally. For a document,
quote its exact existing window title. Ambiguous/unmatched scope stops. The
request and scoped UI content leave the machine; unrelated windows, recent-file
entries and raw driver metadata are excluded from the model view. Full private
traces remain local. `--task-observations` applies the same restricted view to a
local comparison. This does not provide arbitrary aliases or multi-app scope.

Hosted calls have retries/fallback disabled, a 120-second completion bound,
24,576 input-token limit and 2,048 total output-token limit. A persistent shared
**$15 ledger** reserves cost before each actual SDK dispatch, retaining uncertain
charges. The macOS default is
`~/Library/Application Support/locua/hosted-budget.json`; use the same explicit
`--budget-ledger PATH` across comparison runs. Cost reports are conservative
estimates, not invoices. Do not delete or change ledgers to reset the cap.
Local defaults and every retained RLCD option remain unchanged.

`--tool-profile fresh-region-v1` is an explicit native Amplifier experiment:
after input, it returns a compact overview and an unfiltered page of fresh
controls from the uniquely corresponding structural region. All other regions
and page continuations remain discoverable. Ambiguous region correspondence
falls back to discovery; every input still requires the unchanged fresh guards.
`--tool-profile baseline` remains the default. The library `do`/`start` accepts
the equivalent `tool_profile` option. No completion or performance improvement
is implied by selecting this experimental view.

`--tool-profile execution-state-v1` is a separate local-only experiment. It
retains a bounded factual record of approved goals, issued actions and failed
checks through Amplifier context compaction. Historical observations are marked
as potentially stale and never authorize input. It uses baseline tool responses,
does not choose actions, and does not enable the fresh-region experiment.
It currently rejects `--task-observations` because its retained facts do not use
that disclosure projection; it cannot be used with hosted inference.

`--instruction-profile` selects a separate, unproven prompt experiment in the
native Amplifier loop. `baseline` preserves the original policy and remains the
default. `concise-v1` changes only the system operating policy;
`concise-examples-v1` adds two development examples to that policy;
`concise-help-v1` instead changes top-level tool help alongside that policy.
All variants retain the same argument schemas, observations, action guards,
verification, provider/model settings and decoding. The library `do`/`start`
accepts the equivalent `instruction_profile` option. No variant is a demonstrated
reliability improvement merely because it is installed. Hosted experiments still
require explicit provider/model selection and their authorized spending limits.
`--budget-cap-usd N` declares the cap of that hosted ledger (at most $15). An
existing ledger must match; the option never clears charges or reservations.

No online model fallback is permitted. Runtime inference currently targets macOS
and MLX; Windows and Linux desktop/inference support is unverified. Raw editor,
committed document, saved-file and visible display evidence remain distinct.
Native Excel cell binding is not implied by saved-file reads or OCR labels.

Installation and prerequisites: `docs/installation.md`. Each capability's arguments,
result, example and failure behavior: `locua <capability> --help`. Detailed usage
is shipped in `docs/usage.md`. Application/window discovery precedes target binding;
task inputs are limited to the reviewed scope. UI text cannot add authority. Trace files stay local and may contain
private desktop text; choose an appropriate output directory.
