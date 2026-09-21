"""The tool's public library. CLI parsing and presentation are separate adapters."""
from copy import deepcopy
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys

from . import __version__
from . import config as configuration
from .errors import LocuaError

CAPABILITIES = {
    "manifest": ("deterministic", "Read the installed Smart Tool manifest."),
    "doctor": ("deterministic", "Inspect explicit runtime paths and report readiness without loading models."),
    "setup": ("deterministic", "Write local configuration; optionally request explicit driver activation."),
    "targets": ("deterministic", "List observed desktop windows with their exact target identities."),
    "observe": ("deterministic", "Read a scoped desktop snapshot or inspect supplied snapshot data."),
    "start": ("model-backed", "Start a language task; --manual selects the optional field picker."),
    "do": ("model-backed", "Describe an outcome, review local interpretation, and execute guarded changes."),
    "run": ("model-backed", "Propose or execute a scoped task with local models and fresh guards."),
    "eval": ("model-backed", "Evaluate recorded decisions or supplied simulations with local models."),
}
SUCCESS = {"complete", "verified_reviewed_scope", "proposed", "observed", "evaluated", "ready", "diagnostic", "configured", "plan_ready_for_review"}
RESOURCES = ["SMART_TOOL.md", "docs/installation.md", "docs/usage.md", "lib.py", "config.py"]


def skill_directory():
    return Path(__file__).resolve().parent


def manifest():
    """Read structured frontmatter and prose from the one installed manifest.

    The frontmatter is JSON, a YAML-compatible serialization. No secondary copy
    or manifest parser dependency is required.
    """
    text = (skill_directory() / "SMART_TOOL.md").read_text(encoding="utf-8")
    frontmatter, body = text.split("---", 2)[1:]
    fields = configuration.strict_json(frontmatter)
    return {"frontmatter": fields, "body": body.strip()}


def describe():
    """Return capability costs and the public callable boundary."""
    return [{"name": name, "kind": kind, "description": description}
            for name, (kind, description) in CAPABILITIES.items()]


def short_help(capability=None):
    if capability is None:
        return "locua \"describe the outcome\" [options]\nlocua do [REQUEST] [options]\nlocua <capability> [options]\n\n" + "\n".join(
            f"  {name:<9} [{kind}] {description}" for name, (kind, description) in CAPABILITIES.items()) + "\n\nUse locua --help or locua <capability> --help for full guidance."
    if capability == "start":
        return ("locua start [REQUEST] [--url URL | --document FILE] [options]\n"
                "Describe an outcome, review its interpretation, then type run.\n"
                "Use --manual for optional observed-field selection.\n\n"
                "  --config FILE   Use an existing runtime configuration\n"
                "  --provider NAME local (default), openai, or anthropic; no fallback\n"
                "  --model ID      Local comparator (language default), baseline, qwen38; hosted ID must be explicit\n"
                "  --thinking      Explicit bounded local qwen38 thinking experiment\n"
                "  --tool-profile baseline|fresh-region-v1|execution-state-v1  Explicit response-view experiment\n"
                "  --instruction-profile baseline|concise-v1|concise-examples-v1|concise-help-v1  Explicit prompt experiment\n"
                "  --budget-ledger PATH  Shared hosted API spend cap\n"
                "  --budget-cap-usd N  Ledger cap, at most 15; existing ledger must match\n"
                "  --json          Print machine-readable results\n\n"
                "First use: locua setup --help; check configuration: locua doctor.\n"
                "Use --help for the full Smart Tool guide and current limitations.")
    if capability == "do":
        return ('locua do [REQUEST] [--url URL | --document FILE] [options]\n'
                'Describe the outcome, answer real ambiguities, review the plan, then type run.\n'
                'Preview default: local 7B comparator; Amplifier ordinary tool calling.\n'
                '  --provider local|openai|anthropic (local default; no fallback)\n'
                '  --model ID (local baseline|comparator|qwen38; explicit hosted ID required)\n'
                '  --thinking (explicit local qwen38 bounded-thinking experiment)\n'
                '  --tool-profile baseline|fresh-region-v1|execution-state-v1 (explicit response-view experiment)\n'
                '  --instruction-profile baseline|concise-v1|concise-examples-v1|concise-help-v1 (explicit prompt experiment)\n'
                '  --budget-ledger PATH (shared hosted API spend cap)\n'
                '  --budget-cap-usd N (ledger cap <= 15; never resets spending)\n'
                '  --harness amplifier|legacy (native tasks only)\n'
                '  --json  --out DIRECTORY  --config FILE\n'
                'Original RLCD remains in legacy, manual and reviewed-run paths. See --help for limits.')
    return "locua " + capability + " [options]\n" + CAPABILITIES[capability][1] + "\nUse --help for arguments, examples and failure remedies."


def skill(capability=None):
    """Render Agent Skill guidance from library-owned metadata and bundled docs."""
    if capability is not None and capability not in CAPABILITIES:
        raise LocuaError("unknown_capability", capability, "Run locua -h.", exit_code=2)
    name = "locua" + ("-" + capability if capability else "")
    lines = [f'<skill_content name="{name}">', f"Skill directory: {skill_directory()}",
             "Relative paths in this skill are relative to the skill directory.", ""]
    try:
        urls = metadata.metadata("locua").get_all("Project-URL") or []
        repository = next((u.split(", ", 1)[1] for u in urls if u.lower().startswith("repository, ")), None)
        if repository:
            lines.insert(2, "Repository: " + repository)
    except metadata.PackageNotFoundError:
        pass
    if capability is None:
        lines += ["# locua", "", manifest()["body"], "", "## Capabilities", "",
                  "Each capability has its own skill: locua <capability> --help."]
        lines += [f"- {n} [{kind}]: {desc}" for n, (kind, desc) in CAPABILITIES.items()]
    else:
        usage = (skill_directory() / "docs/usage.md").read_text(encoding="utf-8")
        marker = "## " + capability + "\n"
        section = usage.split(marker, 1)[1].split("\n## ", 1)[0]
        lines += ["# locua " + capability, "", "[" + CAPABILITIES[capability][0] + "]", "", section.strip()]
    lines += ["", "<skill_resources>"] + ["  <file>" + r + "</file>" for r in RESOURCES] + ["</skill_resources>", "</skill_content>"]
    return "\n".join(lines)


def _config(value):
    if isinstance(value, dict):
        return configuration.validate(value)
    return configuration.load(value)[0]


def _envelope(operation, result):
    if not isinstance(result, dict):
        raise LocuaError("invalid_engine_result", "Engine did not return an object.", "Use matching package/runtime versions.", exit_code=5)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    ok = result.get("ok", result.get("status") in SUCCESS)
    if type(ok) is not bool:
        raise LocuaError("invalid_engine_result", "Engine result ok must be boolean.", "Use matching package/runtime versions.", exit_code=5)
    return {"schema": "locua.result.v1", "operation": operation, "ok": ok, "result": result}


def _engine(operation, payload, config, progress=None):
    from .engine.prototype.perception import NativeObservationUnavailable
    try:
        adapter = importlib.import_module("locua.engine_adapter")
    except ImportError as error:
        raise LocuaError("engine_unavailable", "This installation does not contain the Locua engine adapter.",
                         "Install a complete Locua wheel or the current source checkout; no hidden lab fallback is used.") from error
    try:
        result = adapter.execute(operation=operation, payload=deepcopy(payload), config=_config(config), progress=progress or (lambda message: None))
        return _envelope(operation, result)
    except LocuaError:
        raise
    except NativeObservationUnavailable as error:
        raise LocuaError(error.code, str(error), error.remedy,
                         exit_code=4, details=error.details) from error
    except Exception as error:
        raise LocuaError("runtime_error", str(error), "Run locua doctor with the same configuration and inspect the named prerequisite or local trace.", exit_code=5) from error


def doctor(config=None, *, probe=False, progress=None):
    """Inspect paths deterministically. probe requests the separate runtime doctor.

    A completed diagnostic is a valid result even when ready is false. It never
    grants permissions, downloads models or initializes inference.
    """
    value = _config(config)
    checks = []
    for field in configuration.PATH_FIELDS:
        raw = value[field]
        exists = Path(raw).exists() if raw else False
        checks.append({"name": field, "configured": raw is not None, "exists": exists,
                       "path": raw, "remedy": "Set " + field + " with locua setup." if not exists else None})
    missing = [x["name"] for x in checks if not x["exists"] and x["name"] not in ("driver_app", "driver_socket")]
    configured = not missing and sys.platform == "darwin"
    report = {"status": "diagnostic", "version": __version__, "platform": sys.platform,
              "supported_inference_platform": sys.platform == "darwin", "configuration_ready": configured, "ready": False,
              "checks": checks, "missing": missing, "permissions_verified": False,
              "model_loaded": False, "desktop_observed": False, "online_fallback": False}
    if probe:
        runtime = _engine("doctor", {}, value, progress)
        report["runtime"] = runtime["result"]
        report["permissions_verified"] = runtime["result"].get("permissions_verified") is True
        report["ready"] = configured and runtime["ok"] and runtime["result"].get("ready", False)
    return _envelope("doctor", report)


def setup(values=None, *, path=None, replace=False, activate_driver=False, progress=None):
    """Write explicit config. Installs/downloads are never an import side effect."""
    data, output = configuration.write(values or {}, path, replace=replace)
    result = {"status": "configured", "config_path": str(output), "config": data,
              "models_downloaded": False, "permissions_requested": None if activate_driver else False,
              "normal_permission_onboarding_possible": activate_driver}
    if activate_driver:
        runtime = _engine("setup", {"activate_driver": True}, data, progress)
        result["runtime"] = runtime["result"]
        result["ok"] = runtime["ok"]
    return _envelope("setup", result)


def observe(*, snapshot=None, target=None, kind=None, view="overview", region_id=None, query=None,
            cursor=None, limit=None, out=None, config=None, progress=None):
    """Inspect supplied data or acquire one explicitly scoped driver observation."""
    if (snapshot is None) == (target is None):
        raise LocuaError("observation_scope_required", "Supply exactly one snapshot or target object.", "Use --snapshot FILE or --target FILE --kind browser/native.", exit_code=2)
    if not isinstance(snapshot if snapshot is not None else target, dict):
        raise LocuaError("invalid_observation_input", "Observation input must be a JSON object.", "Supply a normalized snapshot or exact target object.", exit_code=2)
    if view not in ("overview", "inspect", "search", "full") or (view == "inspect" and not region_id) or (view == "search" and not query):
        raise LocuaError("invalid_view", "Inspection needs a region ID; search needs a query.", "Run locua observe --help.", exit_code=2)
    if ((region_id is not None and (view not in ("inspect", "search") or not isinstance(region_id, str) or not region_id))
            or (query is not None and (view != "search" or not isinstance(query, str) or not query))
            or (cursor is not None and (view == "full" or not isinstance(cursor, str) or not cursor))
            or (limit is not None and (view == "full" or type(limit) is not int or not 1 <= limit <= 256))
            or (snapshot is not None and kind is not None)):
        raise LocuaError("invalid_view_option", "View options must apply to the selected view; limit is 1..256 and full has no pagination.",
                         "Use cursor with the same saved snapshot, view, query, region and page size; kind is for live targets only.", exit_code=2)
    return _engine("observe", {"snapshot": snapshot, "target": target, "kind": kind, "view": view,
                    "region_id": region_id, "query": query, "cursor": cursor, "limit": limit,
                    "out": str(out) if out else None}, config, progress)


def targets(*, pid=None, out=None, config=None, progress=None):
    """List driver windows without changing focus or dispatching task actions."""
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise LocuaError("invalid_pid", "PID must be a positive integer.", "Omit --pid to list observed windows, or pass an exact PID.", exit_code=2)
    payload = {"pid": pid}
    if out is not None:
        payload['out'] = str(out)
    return _engine("targets", payload, config, progress)


def run(*, task=None, request=None, scope=None, supplied_data=None, model="baseline", execute=False,
        browser_click_route="trusted", inspection_policy="model_led", out=None, config=None, progress=None):
    """Propose by default. Supplied outcome plan or local natural-language proposal.

    A local planner's output is not proof of faithful interpretation. The engine
    retains explicit grounding, authorization, effect and completion gates.
    """
    if (task is None) == (request is None) or (task is not None and not isinstance(task, dict)):
        raise LocuaError("task_required", "Supply exactly one task/plan object or request string.", "Use --task FILE or --request TEXT --scope FILE.", exit_code=2)
    if request is not None and (not isinstance(request, str) or not request.strip() or not isinstance(scope, dict)):
        raise LocuaError("scope_required", "Natural-language input needs a nonempty request and explicit scope object.", "Pass --scope FILE; UI content cannot add scope.", exit_code=2)
    if model not in ("baseline", "comparator") or type(execute) is not bool:
        raise LocuaError("invalid_run_option", "Model must be baseline or comparator and execute must be boolean.", "The original baseline is default; comparator is explicit.", exit_code=2)
    if supplied_data is not None and not isinstance(supplied_data, dict):
        raise LocuaError("invalid_supplied_data", "Supplied data must be a JSON object.", "Pass literal task data, not commands or file references.", exit_code=2)
    if browser_click_route not in ("trusted", "dom_event"):
        raise LocuaError("invalid_browser_click_route", "Browser click route must be trusted or dom_event.", "dom_event explicitly selects synthetic background clicks; no automatic fallback occurs.", exit_code=2)
    if inspection_policy not in ("model_led", "reviewed_target_first"):
        raise LocuaError("invalid_inspection_policy", "Unknown inspection policy.", "Choose model_led or reviewed_target_first.", exit_code=2)
    return _engine("run", {"task": task, "request": request, "scope": scope, "supplied_data": supplied_data,
                    "model": model, "execute": execute, "browser_click_route": browser_click_route,
                    "inspection_policy": inspection_policy,
                    "out": str(out) if out else None}, config, progress)


def start(*, url=None, document=None, request=None, model=None, manual=False, harness="amplifier", provider="local", thinking=False, budget_ledger=None, budget_cap_usd=15, task_observations=False, tool_profile="baseline", instruction_profile="baseline", browser_click_route="trusted", native_save_route="menu", inspection_policy="reviewed_target_first", out=None,
          config=None, ask=None, progress=None):
    """Guided task entry; clients supply ask(prompt)->answer and progress callbacks.

    Explicit reviewed choices replace internal-schema authoring. This capability
    does not claim automatic natural-language interpretation.
    """
    if not manual:
        return do(request=request, url=url, document=document, model=model or ('comparator' if provider=='local' else None),
                  harness=harness, provider=provider, thinking=thinking, budget_ledger=budget_ledger, task_observations=task_observations, tool_profile=tool_profile,
                  instruction_profile=instruction_profile,
                  budget_cap_usd=budget_cap_usd,
                  browser_click_route=browser_click_route, native_save_route=native_save_route,
                  inspection_policy=inspection_policy, out=out, config=config, ask=ask, progress=progress)
    if provider!='local' or thinking or budget_ledger is not None or budget_cap_usd!=15 or task_observations or tool_profile!='baseline' or instruction_profile!='baseline':
        raise LocuaError('invalid_run_option','Provider/thinking options apply only to the Amplifier language loop.','Omit --manual.',exit_code=2)
    if request is not None:
        raise LocuaError('bad_invocation', 'Manual mode takes field choices, not a language request.', 'Omit manual mode to interpret your request.', exit_code=2)
    if model not in (None,'baseline','comparator'):
        raise LocuaError('invalid_run_option','This model is available only in the Amplifier native language path.','Omit --manual and use --harness amplifier.',exit_code=2)
    if (url is not None and document is not None) or not callable(ask):
        raise LocuaError("guided_input_required", "Choose URL or document and supply a review interaction callback.",
                         "Use locua start --url URL, --document FILE, or select a native window interactively.", exit_code=2)
    from .guided import start as guided_start
    return _envelope("start", guided_start(url=url, document=document, model=model or 'baseline',
        browser_click_route=browser_click_route, native_save_route=native_save_route, inspection_policy=inspection_policy, out=out, config=config, ask=ask,
        progress=progress or (lambda message: None)))


def do(request=None, *, url=None, document=None, model="comparator", browser_click_route="trusted",
       native_save_route="menu", inspection_policy="reviewed_target_first", harness="amplifier", provider="local", thinking=False, budget_ledger=None, budget_cap_usd=15, task_observations=False, tool_profile="baseline", instruction_profile="baseline", out=None,
       config=None, ask=None, progress=None):
    """Reviewed language workflow; no user-authored internal schema required."""
    if not callable(ask) or (url is not None and document is not None):
        raise LocuaError("review_interaction_required", "Provide an interaction callback and at most one explicit target.",
                         "Run locua do in a terminal; review is required before task edits.", exit_code=2)
    if provider not in ('local','openai','anthropic') or type(thinking) is not bool:
        raise LocuaError('invalid_run_option','Unknown provider or invalid thinking option.','Run locua do --help.',exit_code=2)
    if provider!='local':
        if not isinstance(model,str) or not model.strip():
            raise LocuaError('explicit_hosted_model_required','Hosted inference requires an explicit model ID.','Use --provider and --model together; there is no fallback.',exit_code=2)
        if harness!='amplifier' or url is not None or document is not None or thinking:
            raise LocuaError('invalid_run_option','Hosted providers use the Amplifier native language path and their declared reasoning configuration.','Omit --thinking, --url, --document and --harness legacy.',exit_code=2)
    if type(task_observations) is not bool or (task_observations and (harness!='amplifier' or url is not None or document is not None)):
        raise LocuaError('invalid_run_option','Task observations apply only to the native Amplifier loop.','Use native language mode.',exit_code=2)
    if tool_profile not in ('baseline','fresh-region-v1','execution-state-v1') or (tool_profile!='baseline' and (harness!='amplifier' or url is not None or document is not None)):
        raise LocuaError('invalid_run_option','Tool profiles apply only to the native Amplifier loop.','Use --harness amplifier without --url or --document.',exit_code=2)
    from .instruction_policy import PROFILES
    if instruction_profile not in PROFILES or (instruction_profile!='baseline' and (harness!='amplifier' or url is not None or document is not None)):
        raise LocuaError('invalid_run_option','Instruction profiles apply only to the native Amplifier loop.','Use --harness amplifier without --url or --document.',exit_code=2)
    if tool_profile=='execution-state-v1' and (provider!='local' or task_observations):
        raise LocuaError('invalid_run_option','Execution state profile currently requires unscoped local observations.',
                         'Use --provider local without --task-observations, or select --tool-profile baseline.',exit_code=2)
    if thinking and (provider!='local' or model!='qwen38' or harness!='amplifier' or url is not None or document is not None):
        raise LocuaError('invalid_run_option','Bounded thinking is an explicit local qwen38 Amplifier experiment.','Use --provider local --model qwen38 --thinking.',exit_code=2)
    if budget_ledger is not None and provider=='local':
        raise LocuaError('invalid_run_option','API spend ledgers apply only to hosted providers.','Omit --budget-ledger for local inference.',exit_code=2)
    if type(budget_cap_usd) not in (int,float) or not 0<budget_cap_usd<=15 or (provider=='local' and budget_cap_usd!=15):
        raise LocuaError('invalid_run_option','Hosted ledger cap must be positive and at most 15, and match any existing ledger.','Use --budget-cap-usd only with an explicitly authorized hosted ledger; it cannot reset spending.',exit_code=2)
    if (harness not in ("amplifier", "legacy") or (provider=='local' and model not in ("baseline", "comparator", "qwen38")) or browser_click_route not in ("trusted", "dom_event")
            or inspection_policy not in ("model_led", "reviewed_target_first")
            or native_save_route not in ("menu", "textedit_shortcut")):
        raise LocuaError("invalid_run_option", "Unknown model, route or inspection policy.", "Run locua do --help.", exit_code=2)
    if model=='qwen38' and (harness!='amplifier' or url is not None or document is not None):
        raise LocuaError('invalid_run_option','qwen38 is an explicit experimental Amplifier native tool model; it has no RLCD or legacy adapter.','Use native language mode with --harness amplifier --model qwen38.',exit_code=2)
    # Explicit URL/document callers retain the v8 reviewed adapter entry. The
    # ordinary desktop path starts from the complete goal, not an open window.
    if url is None and document is None and harness == "amplifier":
        from .amplifier_session import run as language_run
    elif url is None and document is None and inspection_policy != "model_led":
        from .goal_loop import run as language_run
    else:
        from .conversation import run as language_run
    inference_options = ({'provider':provider,'thinking':thinking,'budget_ledger':budget_ledger,'task_observations':task_observations}
                         if provider!='local' or thinking or task_observations else {})
    if tool_profile!='baseline':inference_options['tool_profile']=tool_profile
    if instruction_profile!='baseline':inference_options['instruction_profile']=instruction_profile
    if provider!='local':inference_options['budget_cap_usd']=budget_cap_usd
    return _envelope("do", language_run(request, url=url, document=document, model=model, **inference_options,
                     browser_click_route=browser_click_route, native_save_route=native_save_route,
                     inspection_policy=inspection_policy, out=out, config=config,
                     ask=ask, progress=progress or (lambda message: None)))


def evaluate(cases, *, model="baseline", out=None, config=None, progress=None):
    """Run explicit local evaluation cases; cases are data, never caller file refs."""
    if not isinstance(cases, (dict, list)) or model not in ("baseline", "comparator"):
        raise LocuaError("invalid_evaluation", "Supply a case object/list and an explicit supported model.", "Run locua eval --help.", exit_code=2)
    return _engine("eval", {"cases": cases, "model": model, "out": str(out) if out else None}, config, progress)
