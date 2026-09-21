"""Thin argparse/I/O adapter; guided start offers text or explicit JSON output."""
import argparse
import json
import sys

from . import __version__, lib
from .config import read_json
from .errors import LocuaError


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def failure(error):
    emit({"schema": "locua.result.v1", "ok": False, "error": error.as_dict()})
    return error.exit_code


def _brief(value, limit=200):
    """One bounded terminal line; never interpret UI text as terminal control."""
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def _literal(value, limit=160):
    # Escaped whitespace remains distinguishable, including empty text and CRLF.
    text = json.dumps(value, ensure_ascii=True, allow_nan=False)
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def incomplete_goal_lines(report):
    """Present existing scoped evidence; never derive results or inspect the UI."""
    if report.get("status") in lib.SUCCESS or not (report.get("request") or report.get("tool_evidence")):
        return []
    def mapping(value):
        return value if isinstance(value, dict) else {}
    evidence = mapping(report.get("tool_evidence"))
    scopes = mapping(evidence.get("scopes"))
    final = mapping(mapping(report.get("verification")).get("verification"))
    proof = final or mapping(evidence.get("verification"))
    receipts = mapping(proof.get("scopes"))
    lines = []
    if str(report.get("reason", "")).startswith("TimeoutError"):
        lines.append("Session time limit reached; the task is incomplete.")
    if not final.get("fresh_refresh"):
        lines.append("No fresh final verification was recorded.")
    goals = [(sid, scope, goal) for sid, scope in scopes.items() if isinstance(scope, dict)
             for goal in scope.get("goals", []) if isinstance(goal, dict)]
    if not goals:
        if report.get("request"):
            lines.append("Requested: " + _brief(report["request"]))
        if "scopes" in evidence and not scopes:
            lines.append("No reviewed outcome was established.")
        return lines
    explanations = {
        "bound_predicate_not_met": "The bound control did not match the requested value.",
        "bound_target_absent": "The previously bound result control was not present in the readback.",
    }
    for sid, scope, goal in goals[:4]:
        kind = goal.get("kind")
        if kind == "calculation":
            # A model-written target label can include its guessed answer. Only
            # the reviewed expression is the requested arithmetic outcome.
            requested = "Evaluate " + _literal(goal.get("expression"))
        elif kind in ("text", "state"):
            requested = _brief(goal.get("target") or goal.get("id"), 100)
            if "value" in goal:
                requested += " = " + _literal(goal["value"])
            if kind == "text":
                requested += " (editor buffer)"
        else:
            requested = _brief(goal.get("target") or goal.get("id"))
        lines.append("Requested outcome: " + requested)
        receipt = mapping(receipts.get(sid))
        rows = [r for r in receipt.get("goals", []) if isinstance(r, dict)
                and r.get("goal_id") == goal.get("id")]
        row = rows[0] if len(rows) == 1 else {}
        readback = mapping(row.get("evidence")) or mapping(mapping(row.get("display_readback")).get("evidence"))
        if "actual" in readback:
            lines.append("Last recorded readback: " + _literal(readback["actual"]) +
                         " (" + _brief(readback.get("plane") or "unspecified evidence plane", 50) + ").")
        else:
            lines.append("Bound result readback: unavailable.")
        if kind == "calculation":
            witness = mapping(scope.get("witness")) or mapping(row.get("arithmetic_input"))
            if "issued_expression_since_clear" in witness:
                lines.append("Last recorded issued input: " + _literal(witness["issued_expression_since_clear"]) + ".")
            if isinstance(witness.get("issued_evaluation"), str):
                lines.append("Evaluation issued for: " + _literal(witness["issued_evaluation"]) + " (issuance only).")
            elif witness:
                lines.append("Evaluation from a known start: not recorded.")
            if witness.get("known_start") is False:
                lines.append("A full reset or known starting expression is unproved.")
        if row.get("matched") is True:
            lines.append("This outcome matched at its recorded check; full request completion remains unproved.")
        else:
            reason = row.get("reason") or "No matching verification receipt was recorded."
            if scope.get("status") in ("blocked_uncertain", "blocked_preservation_unknown"):
                failures = [mapping(event.get("model_result") or event.get("result"))
                            for event in evidence.get("events", []) if isinstance(event, dict)
                            and mapping(event.get("result")).get("scope_id") == sid
                            and mapping(event.get("result")).get("uncertain_action") is True]
                if not row.get("reason") and failures:
                    failure = failures[-1]
                    raw = mapping(failure.get("raw_action_result"))
                    readback = mapping(mapping(raw.get("verification")).get("readback"))
                    reason = failure.get("reason") or readback.get("reason") or raw.get("reason") or reason
                lines.append("An earlier input remains uncertain; another write is disabled for this target.")
            lines.append("Unmet: " + _brief(explanations.get(reason, reason)))
    if len(goals) > 4:
        lines.append(str(len(goals) - 4) + " additional reviewed outcomes are in the result file.")
    failed_preserves = [r for receipt in receipts.values() if isinstance(receipt, dict)
                        for r in receipt.get("preserves", []) if isinstance(r, dict) and r.get("matched") is not True]
    if failed_preserves:
        lines.append("Preservation checks not established: " + str(len(failed_preserves)) + ".")
    return lines


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LocuaError("bad_invocation", message, "Run locua -h or locua <capability> --help.", exit_code=2)


class Help(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, *, render, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)
        self.render = render

    def __call__(self, parser, namespace, values, option_string=None):
        print(self.render())
        parser.exit(0)


def add_help(parser, capability=None):
    parser.add_argument("-h", action=Help, render=lambda: lib.short_help(capability))
    parser.add_argument("--help", action=Help, render=lambda: lib.skill(capability))
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Explicit local configuration path.")


def parser():
    root = Parser(prog="locua", add_help=False)
    add_help(root)
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in lib.CAPABILITIES:
        item = sub.add_parser(name, add_help=False, help=lib.CAPABILITIES[name][1])
        add_help(item, name)
        if name == "doctor":
            item.add_argument("--probe", action="store_true", help="Request bounded runtime permission/status checks; no model load.")
        elif name == "targets":
            item.add_argument("--pid", type=int, help="Optional exact process ID; omitted lists observed windows.")
            item.add_argument("--out")
        elif name == "setup":
            for field in ("runtime-python", "model-cache", "driver-binary", "driver-socket", "driver-app"):
                item.add_argument("--" + field, default=argparse.SUPPRESS)
            item.add_argument("--replace", action="store_true")
            item.add_argument("--activate-driver", action="store_true")
        elif name == "observe":
            source = item.add_mutually_exclusive_group(required=True)
            source.add_argument("--snapshot", help="Normalized observation JSON, read without acquiring desktop state.")
            source.add_argument("--target", help="Exact target JSON, acquired through the configured driver.")
            item.add_argument("--kind", choices=("browser", "native"))
            item.add_argument("--view", choices=("overview", "inspect", "search", "full"), default="overview")
            item.add_argument("--region-id")
            item.add_argument("--query")
            item.add_argument("--cursor", help="Continuation from the same snapshot, view, query and page size.")
            item.add_argument("--limit", type=int, help="Page size 1..256; defaults to 32 (inspect 64).")
            item.add_argument("--out")
        elif name == "run":
            source = item.add_mutually_exclusive_group(required=True)
            source.add_argument("--task", help="Reviewed locua-task-plan-v1 outcome plan JSON.")
            source.add_argument("--request", help="Natural-language request for a local planning proposal.")
            item.add_argument("--scope", help="Explicit caller scope JSON for natural-language requests.")
            item.add_argument("--data", help="Literal supplied task data JSON.")
            item.add_argument("--model", choices=("baseline", "comparator"), default="baseline")
            item.add_argument("--execute", action="store_true")
            item.add_argument("--inspection-policy", choices=("model_led", "reviewed_target_first"), default="model_led")
            item.add_argument("--browser-click-route", choices=("trusted", "dom_event"), default="trusted",
                              help="Explicit click route; dom_event uses synthetic background events. No automatic fallback.")
            item.add_argument("--out")
        elif name in ("start", "do"):
            item.add_argument("request", nargs="*", help=argparse.SUPPRESS)
            if name == "start":
                item.add_argument("--manual", action="store_true", help="Use the optional observed-field debug workflow.")
            item.add_argument("--json", action="store_true", help="Print the full machine-readable result instead of the concise terminal result.")
            target = item.add_mutually_exclusive_group()
            target.add_argument("--url", help="Open and inspect this browser URL in an owned isolated window.")
            target.add_argument("--document", help="Inspect an existing UTF-8 .txt document in TextEdit; verified saving is supported only by the separate manual adapter.")
            item.add_argument("--native-save-route", choices=("menu", "textedit_shortcut"), default="menu",
                              help="Explicit native save capability: observed menu or guarded TextEdit Command-S. No fallback.")
            item.add_argument("--provider", choices=("local", "openai", "anthropic"), default="local",
                              help="Explicit inference provider; hosted providers never replace local defaults.")
            item.add_argument("--model", default=None,
                              help="Local baseline/comparator/qwen38, or an explicitly supported hosted model ID.")
            item.add_argument("--thinking", action="store_true", help="Explicit bounded thinking experiment for local qwen38.")
            item.add_argument("--task-observations", action="store_true", help="Apply the hosted task-only observation boundary to a local comparison run.")
            item.add_argument("--tool-profile", choices=("baseline", "fresh-region-v1", "execution-state-v1"), default="baseline",
                              help="Explicit generic tool-response experiment; baseline behavior remains the default.")
            item.add_argument("--instruction-profile", choices=("baseline", "concise-v1", "concise-examples-v1", "concise-help-v1"), default="baseline",
                              help="Explicit instruction experiment; no model, tool-schema or action-guard change.")
            item.add_argument("--budget-ledger", help="Shared private hosted-spend ledger; no credentials in this file.")
            item.add_argument("--budget-cap-usd", type=float, default=15,
                              help="Declared hosted ledger cap (0 < cap <= 15); must match an existing ledger. Never resets spending.")
            item.add_argument("--harness", choices=("amplifier", "legacy"), default="amplifier",
                              help="Native language loop: Amplifier ordinary tool calling, or retained legacy RLCD workflow.")
            item.add_argument("--inspection-policy", choices=("model_led", "reviewed_target_first"), default="reviewed_target_first")
            item.add_argument("--browser-click-route", choices=("trusted", "dom_event"), default="trusted")
            item.add_argument("--out")
        elif name == "eval":
            item.add_argument("--cases", required=True)
            item.add_argument("--model", choices=("baseline", "comparator"), default="baseline")
            item.add_argument("--out")
    return root


def main(argv=None):
    args = None
    def progress(message):
        print(str(message), file=sys.stderr, flush=True)
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if not argv:
            argv = ["do"]
        elif argv[0] not in lib.CAPABILITIES and not argv[0].startswith('-') and (len(argv[0].split()) > 1 or (len(argv) > 1 and not argv[1].startswith('-'))):
            argv.insert(0, 'do')
        args = parser().parse_args(argv)
        config = getattr(args, "config", None)
        if args.command == "manifest":
            result = {"schema": "locua.result.v1", "operation": "manifest", "ok": True, "result": lib.manifest()}
        elif args.command == "doctor":
            result = lib.doctor(config, probe=args.probe, progress=progress)
        elif args.command == "setup":
            fields = ("runtime_python", "model_cache", "driver_binary", "driver_socket", "driver_app")
            values = {field: getattr(args, field) for field in fields if hasattr(args, field)}
            result = lib.setup(values, path=config, replace=args.replace, activate_driver=args.activate_driver, progress=progress)
        elif args.command == "targets":
            result = lib.targets(pid=args.pid, out=args.out, config=config, progress=progress)
        elif args.command == "observe":
            result = lib.observe(snapshot=read_json(args.snapshot) if args.snapshot else None,
                target=read_json(args.target) if args.target else None, kind=args.kind, view=args.view,
                region_id=args.region_id, query=args.query, cursor=args.cursor, limit=args.limit,
                out=args.out, config=config, progress=progress)
        elif args.command == "run":
            result = lib.run(task=read_json(args.task) if args.task else None, request=args.request,
                scope=read_json(args.scope) if args.scope else None, supplied_data=read_json(args.data) if args.data else None,
                model=args.model, execute=args.execute, browser_click_route=args.browser_click_route,
                inspection_policy=args.inspection_policy,
                out=args.out, config=config, progress=progress)
        elif args.command in ("start", "do"):
            def ask(prompt):
                print(prompt, file=sys.stderr, end="", flush=True)
                answer = sys.stdin.readline()
                if not answer:
                    raise LocuaError("guided_input_ended", "Review input ended before completion.",
                                     "Run locua start in a terminal and review the proposed changes.", exit_code=2)
                return answer.removesuffix("\n").removesuffix("\r")
            if getattr(args, 'manual', False) and args.request:
                raise LocuaError('bad_invocation', '--manual takes field choices, not a language request.', 'Omit --manual to interpret your request.', exit_code=2)
            operation = lib.start if args.command == 'start' else lib.do
            extra = {'manual': args.manual} if args.command == 'start' else {}
            result = operation(url=args.url, document=args.document, model=args.model or (('baseline' if getattr(args, 'manual', False) else 'comparator') if args.provider == 'local' else None),
                request=" ".join(args.request) if args.request else None,
                harness=args.harness, provider=args.provider, thinking=args.thinking, budget_ledger=args.budget_ledger, task_observations=args.task_observations, tool_profile=args.tool_profile,
                instruction_profile=args.instruction_profile,
                budget_cap_usd=args.budget_cap_usd,
                inspection_policy=args.inspection_policy,
                native_save_route=args.native_save_route, browser_click_route=args.browser_click_route, out=args.out, config=config,
                ask=ask, progress=progress, **extra)
        else:
            result = lib.evaluate(read_json(args.cases), model=args.model, out=args.out, config=config, progress=progress)
        if args.command in ("start", "do") and not args.json:
            value = result["result"]
            print("Locua " + value["status"] + ".")
            if value.get("reason"):
                print("Reason: " + value["reason"])
            for line in incomplete_goal_lines(value):
                print(line)
            if value.get("config_path"):
                print("Configuration: " + value["config_path"])
            if value.get("remedy"):
                print("Next step: " + value["remedy"])
            print("Results: " + value["artifacts"] + "/summary.json")
        else:
            emit(result)
        if result["ok"]:
            return 0
        return 130 if result.get("result", {}).get("status") == "canceled" else 6
    except LocuaError as error:
        if args is not None and args.command in ("start", "do") and not args.json:
            print("Locua stopped: " + error.message, file=sys.stderr)
            print("Next step: " + error.remedy, file=sys.stderr)
            return error.exit_code
        return failure(error)
    except KeyboardInterrupt:
        return failure(LocuaError("canceled", "Canceled by the caller.", "Inspect local task state before any retry.", exit_code=130))
    except (OSError, ValueError, TypeError) as error:
        return failure(LocuaError("invalid_input_or_io", str(error), "Check input JSON, paths, and write permissions; run locua doctor.", exit_code=2))


if __name__ == "__main__":
    raise SystemExit(main())
