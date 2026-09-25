# Locua implementation

This is the product repository under the Locua manager workspace. The latest user
request selects a preview baseline; historical no-promotion checkpoints in the
manager describe earlier experiments and do not override that request.

## Current baseline and scope

- Version 0.2.0 native language CLI/library defaults: local `qwen38` (27B),
  `step-v2` tools, `continuity-v1` instructions, Amplifier ordinary nonthinking
  tool calling. No RLCD in this path. Central defaults: `preview_defaults.py`.
- The user's Calendar success is user-reported; CPU/package checks do not add
  autonomous task evidence. No configuration has met the full 8/10 per-workflow,
  no unintended changes, typical task under two minutes qualification.
- Current live demo scope is Calendar/browser or disposable Office file/actions.
  Do not restart TextEdit testing. See `src/locua/docs/gaps.md` for next checks.
- Preserve explicit model/profile overrides, original 1.5B/7B/RLCD and legacy
  adapters. Old profile names mean their original behavior, not the new default.
  No silent fallback, new weights, hosted inference or changed decoder.
- Keep old experiment implementations needed for comparisons and `step-v2`
  inheritance. Cleanup removes obsolete recommendations, not evidence or guards.

## Engineering rules

- `src/locua/lib.py` is the public tool; `cli.py` handles parsing and I/O only.
  Public CLI and library defaults must agree. Deterministic commands must not
  initialize runtime/model/desktop services.
- Keep one canonical `src/locua/SMART_TOOL.md`, bundled with every referenced
  resource. Its version must match package metadata and `locua.__version__`.
- The guarded engine is bundled under `src/locua/engine/`. No implicit sibling
  checkout or developer-home dependency. External assets use explicit config;
  preserve venv interpreter lexical paths and HF_HOME (the parent of `hub/`).
- Preserve full requests and constraints, progressive discovery and competitors.
  No app-specific recipes, expected-answer filtering, or stale input authority.
  App launch and schema validity are not verified task completion. Buffer,
  committed content and saved file evidence are distinct.
- Keep Amplifier's standard tool loop; do not rebuild fixed phases as hooks.
- Serialize desktop/GPU sessions. Acquire the existing desktop lease before
  live work or replacing the installed package; do not kill another owner.
  Do not request OS permissions without fresh evidence of the exact blocker.
- Keep mocked, isolated, operator-assisted, user-reported and independently
  verified autonomous evidence distinct. Inspect retained failures before live
  retries. Stop repeated equivalent failure and repair the demonstrated cause.
- State, logs and config stay private in per-user directories. Never commit
  credentials, model weights, personal UI traces or generated artifacts.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python tools/package_smoke.py dist/locua-0.2.0-py3-none-any.whl
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python ../amplifier-smart-tools/conformance/run.py .
```

The separately checked-out Smart Tool conformance kit is a development tool,
not a runtime dependency. Keep live inference and desktop input out of packaging
checks. Retained-evidence replay tests explicitly skip when their private artifact
directory is absent; they do not weaken assertions when evidence is present.
Historical private evidence remains in ignored artifacts and the manager;
Git history retains previous source versions.
