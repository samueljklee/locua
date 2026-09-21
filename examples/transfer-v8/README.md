# Transfer v8

Four predeclared live cases and eight ordinary-language cases. Definitions and gold
were sealed before inspection or language policy implementation. Fixture source
and workbook are sealed separately before inference. No source imports `gold.json`
into the server, model or engine. `verify.py` is an independent post-run oracle.

Start the disposable server from the product repository:

```sh
python3 examples/transfer-v8/server.py --port 0 --out artifacts/transfer-v8-receipts-001
```

It prints exact URLs for `/dispatch` and `/access`. It serves only those routes;
requests, gold and receipts are not HTTP resources. Launching it and opening a
disposable workbook are operator setup, not agent actions. Each comparison needs
a fresh page and new receipt directory; never reuse edited state as a fresh test.

The ordinary requests are in `cases.json` and `language-cases.json`. Users start
through the public `locua` journey and review observed targets. Log clarification
answers, target selection, plan edits, approval and route choice separately from
model interpretation. Hidden navigation may honestly be unsupported; an assisted
outcome-plan experiment does not replace a blocked ordinary-language journey.

The Dispatch page has 20 reference controls before the Evening targets. Actual
snapshot ordering must prove the target lies beyond page one before a pagination
claim. All duplicate controls remain available. The Access page keeps its dialog
open after Save so its final fields remain observable. Its receipt is written
only for an explicit Save event; every input is also journaled.

```sh
python3 examples/transfer-v8/verify.py browser --case dispatch-board --receipts artifacts/transfer-v8-receipts-001
python3 examples/transfer-v8/verify.py browser --case access-dialog --receipts artifacts/transfer-v8-receipts-001
python3 examples/transfer-v8/verify.py workbook /absolute/path/to/disposable/sample-register.xlsx
```

For Settings, record the initial exact toggle state and restore it after the
attempt. Already-on completion is not mutation coverage. For Excel, copy the
frozen workbook before opening it; change only Samples!F7 through an authorized
native route, then explicitly save. The request describes the sample row/column,
not coordinates. Unsupported editor binding or save counts as blocked, not a
missing test. The workbook oracle proves saved content, not delivery of Save.

Report four first attempts, including blocked and unrun cases. Keep models and
policies separate. Existing VS Code/Edge user captures are regressions, not new
held-out cases. No local model runs or GUI actions are performed by fixture tests.
