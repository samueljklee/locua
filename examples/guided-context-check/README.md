# Guided context repair check

Exactly one disposable development case: Studio booking board. This separately
validates a generic context-label repair with the actual guided CLI. It is not
held-out evaluation and is not added to the frozen v7 denominator. No model or
browser has run against this case at fixture freeze time.

The page contains two Room inputs and two Send reminder checkboxes in named
Soundcheck and Recording groups, plus an unrelated Coordination note. Only the
Recording Room and checkbox should change. Full human instructions are in
`task.txt`; select fields from Locua's observed list, never from predetermined
field numbers, internal handles, schema keys or an action recipe.

From the product repository, the manager can run:

```sh
python3 examples/guided-context-check/server.py --port 8767 --out artifacts/guided-context-check-001/receipts
locua start --url http://127.0.0.1:8767/ --model comparator --browser-click-route dom_event --out artifacts/guided-context-check-001/run
python3 examples/guided-context-check/verify.py --receipts artifacts/guided-context-check-001/receipts
```

Use the configured local runtime/model arguments normally required by this
installation. The comparator and DOM-event route are explicit development
choices. The server prints its exact URL and binds only 127.0.0.1. A new receipt
directory is mandatory. Stop only this owned server after the browser/session
cleanup. The page posts its complete current input state automatically; it has
no Save button and opening it creates no receipt.

`initial.json` is fixture setup. `task.txt` contains legitimate requested data.
`oracle.json` and `verify.py` are evaluator-only and must not enter model input;
the server never imports them or serves files. The independent verifier checks
all five final values, strict types, receipt/journal agreement, and the three
preserved values in every recorded event. A receipt pass does not prove model
correctness, actual driver dispatch or native saving; retain Locua's run trace
and independently report false completion, out-of-scope proposals, refused or
uncertain actions. Do not repair output by posting/writing expected values.

`freeze.json` pins the seven input/source files before inference. The offline
tests only exercise rendering, recording and the verifier in temporary folders;
they start no server, app or model:

```sh
python3 examples/guided-context-check/test_fixture.py -v
```
