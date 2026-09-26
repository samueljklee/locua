# Model and profile overrides

For ordinary native `locua "outcome"`, `locua do`, and language-mode `locua start`,
the 0.2.0 defaults are `--provider local --model qwen38 --tool-profile step-v2
--instruction-profile continuity-v1`, with thinking off. These are preview defaults,
not a reliability ranking. Explicit incompatible options fail before execution.

```sh
# Same default tools/instructions with the smaller local model:
locua "Open Calendar and switch to Day view." --model comparator

# Reproduce the former native 7B baseline, with all three factors explicit:
locua "Open Calendar and switch to Day view." --model comparator \
  --tool-profile baseline --instruction-profile baseline

# Spell out the current default for a reproducible comparison:
locua "Open Calendar and switch to Day view." --model qwen38 \
  --tool-profile step-v2 --instruction-profile continuity-v1
```

The aliases are `qwen38` (pinned Qwen3.8-27B 4bit), `comparator` (Qwen2.5-7B),
and `baseline` (original Qwen2.5-1.5B). A model choice does not implicitly select RLCD.
Native Amplifier mode uses ordinary tool calling for all three.

`step-v2` exposes typed references containing observed captions and unique identity,
progressive discovery, and compact retained execution facts. Retained UI is stale
until refreshed; captions grant no action authority. The model still chooses targets
and actions. Existing review, fresh checks and independent verification remain.

Historical profiles stay available for regression comparisons, not as competing
recommendations in the main instructions:

| Tool profile | Purpose |
| --- | --- |
| baseline | Original tool interface and responses |
| fresh-region-v1 | Fresh unfiltered structural-region page after input |
| execution-state-v1 | Original responses with compact retained execution facts |
| continuity-v1 | Compact observed references and retained review scope |
| semantic-v1 / semantic-v2 | Additional observed context; v2 deduplicates old values |
| step-v1 | Typed references and focused execution reminder |
| step-v2 | Current preview; adds observed captions to typed references |

Instruction overrides remain `baseline`, `concise-v1`, `concise-examples-v1`,
`concise-help-v1`, `principles-v1`, `principles-help-v1`, `continuity-v1`, and
`continuity-arguments-v1`. The last is a failed small-model candidate retained for
reproduction. No variant's existence proves it works better.
`step-v1` and `step-v2` require `baseline` or `continuity-v1` instructions; to compare
older instruction families, explicitly select a compatible older tool profile too.
`continuity-arguments-v1` requires continuity or semantic tools. Compact profiles are local-only and
incompatible with `--task-observations`.

Legacy adapters resolve omitted options separately, before any execution:

| Entry | Omitted model / profiles | Decoding |
| --- | --- | --- |
| Native local language | qwen38 / step-v2 / continuity-v1 | Ordinary tool calls |
| Native local `--task-observations` | qwen38 / baseline / baseline | Ordinary tool calls |
| Explicit hosted native | Model ID required / baseline / baseline | Ordinary tool calls |
| `--harness legacy`, `--url`, or `--document` | comparator / baseline / baseline | Ordinary interpretation, original RLCD selection |
| `start --manual`, reviewed `run`, `eval` | baseline | Original RLCD selection where applicable |

These legacy paths retain their limited adapter contracts. The default 27B has no
legacy/RLCD adapter; explicitly requesting it there is an error, never a fallback.
`--url` uses the separate browser adapter, rather than changing the native loop.
`--thinking` is an explicit bounded local qwen38 experiment with prefix cache off.

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
Hosted selection never replaces local defaults or retained RLCD options.
