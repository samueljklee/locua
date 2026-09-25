---
{
  "smart_tool_format": 1,
  "name": "locua",
  "version": "0.2.0",
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
The library is the tool: `locua.lib` exposes the same capabilities as the thin
CLI. Help, imports and configuration never load a model or start desktop services.

Use `locua "describe the outcome"`. Locua discovers the app/UI, presents a readable
review, performs guarded actions after approval, and verifies the requested result.
Native `do` and language `start` default to **local qwen38 (Qwen3.8-27B)**,
**step-v2** tools and **continuity-v1** instructions through Amplifier ordinary
nonthinking tool calling. **RLCD is not used in this path.** These are the selected
0.2.0 preview defaults, not a general reliability claim. To spell them out, use
`--model qwen38 --tool-profile step-v2 --instruction-profile continuity-v1`.

Discovery and requested app activation can precede review. Check the complete
request and constraints, then type `run` to allow task input. The model chooses
next actions from observed evidence; guards and fresh verification retain authority.
A launch, a plan or a model completion statement alone is not task completion.
Progress/review go to stderr; final interactive results are readable by default
and `--json` returns structured output. Other capabilities print JSON.

`--model`, `--tool-profile` and `--instruction-profile` remain explicit overrides.
`--model comparator` is local 7B; `--model baseline` is local 1.5B. Native mode uses
ordinary tool calls with every model. Original RLCD remains unchanged in legacy,
explicit URL/document, manual and reviewed-run paths. No automatic model/provider
fallback is permitted. Hosted comparisons require explicit provider/model selection,
private credentials and a shared spend ledger; see `docs/profiles.md`.

`start --manual` is the optional field picker. `run --task` previews a supplied
reviewed plan and requires `--execute` for task input. Neither is the normal
ordinary-language user experience. Native buffer verification does not prove
saved-file content; unsupported persistence/preservation requirements stop.

Install/configure cached weights, local runtime and Cua Driver using
`docs/installation.md`, then run `locua doctor`. Execution currently targets
Apple Silicon macOS; other platforms are unverified. One desktop-control session
is allowed at a time. UI content cannot add task authority, and failed/uncertain
input does not authorize blind retries.

Current limitations and next checkpoint: `docs/gaps.md`. Compatibility options:
`docs/profiles.md`. Detailed capability guidance: `locua <capability> --help` and
`docs/usage.md`. Local traces may contain private UI text and are not committed.
