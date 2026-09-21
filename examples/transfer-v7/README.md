# Fresh guided transfer fixtures v7

These are four new disposable tasks for the actual Locua guided CLI: two browser
interfaces and two text files in one native editor. Fixture setup is operator work,
performed before the user starts `locua`. This directory contains human-readable
requirements, not engine plans or action scripts. The root integration owns the
guided command and records its exact invocation; no internal schema entry is needed.

| Case | Human-facing requested result |
|---|---|
| `project` | Project brief: Headline `Marsh transect – phase 03`, Reference `00028-K`; preserve Workspace defaults |
| `release` | Release settings: Display name `Ridge survey / release 05`, Public listing off; preserve Owner and Channel defaults |
| `field-survey` | Replace the complete disposable document with its supplied text, then save the same file |
| `release-note` | Replace a different disposable document with its supplied text, then save the same file |

`tasks.json` contains the user requests, target descriptions and legitimate
supplied literals. Show these to the operator/user when constructing the guided
task. `oracle.json` and `expected/` belong only to the independent evaluator;
never pass them to the planner, model, candidate builder or runtime verifier.
The engine may use the user-requested literals from `tasks.json` normally.

## Browser setup

From the product repository, start a server with a **new** private output directory:

```sh
.venv/bin/python examples/transfer-v7/server.py --port 0 --out artifacts/transfer-v7-receipts-new
```

The first JSON line gives its loopback `url` and receipt directory. The two routes
are `/project` and `/release`; use the printed URL with the chosen route in the
guided Locua browser setup. Both interfaces deliberately keep competing labels.
The project page uses parallel template/project sections and a textarea. Release
settings use a sidebar and main panel with duplicate text/checkbox labels. No new
menu or dialog navigation is tested in this round.

The server knows only initial fixture state and field types. It receives all
current fields on input events, serializes them in event order and writes exact
JSON receipts plus an event journal. It never loads expected answers or writes an
expected result on behalf of a run. Opening a page alone produces no receipt.
Use a fresh server/output directory for any repeated attempt. Stop only the owned
server process after the task; preserve its receipts and run trace.

## Native setup

Copy `initial/field-survey.txt` and `initial/release-note.txt` into a new owned run
directory, keeping their names. Open the copies in TextEdit. The initial files in
this directory must remain unchanged. The corresponding `supplied_text` in
`tasks.json` is the complete desired replacement, including its final newline.

The user begins in the actual Locua CLI, chooses the exact disposable document,
supplies the replacement text and requests same-file save. Record every setup and
review step. If save cannot be expressed or executed, report editor-buffer progress
and a blocked save gate; do not manually save and call it autonomous success.

## Independent output check

After a run, check its receipt or saved native copy, using a new audit filename:

```sh
.venv/bin/python tools/audit_transfer_v7.py --case project --file artifacts/transfer-v7-receipts-new/project.json --out artifacts/project-output-audit-new.json
.venv/bin/python tools/audit_transfer_v7.py --case field-survey --file /path/to/owned-copy/field-survey.txt --out artifacts/field-survey-output-audit-new.json
```

The auditor verifies the fixture freeze first, compares every field with strict
types or every byte, and never repairs output. A passing file check alone proves
neither task execution nor a save mechanism. The independent run audit must also
reconcile model requests, fresh bindings, actions, readbacks, assistance, timing
and cleanup. Preserve first-attempt failures and report retries separately.

`freeze.json` hashes inputs, layouts, expected outputs, rubric and audit/test code
before any tests or model runs. `rubric.json` defines the four-case denominator and
limits. These are guided development transfer tests, not unattended natural-language
or broad reliability evidence.
