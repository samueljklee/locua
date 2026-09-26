#!/usr/bin/env python3
"""Install a prebuilt wheel offline and probe its deterministic installed surface.

No model/GUI/driver probes. Run from the source root after python -m build --wheel.
Uses an isolated environment and temporary CWD, never source PYTHONPATH.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile


# Executed with the installed interpreter from a temporary CWD. Import refusal
# makes the deterministic entry-point checks fail if they begin loading either
# an engine/driver integration or a provider. Presentation uses a synthetic
# library return value, explicitly not guided execution or model evidence.
DETERMINISTIC_START_PROBE = textwrap.dedent('''\
    import contextlib
    import importlib.abc
    import io
    import json
    from pathlib import Path
    import sys

    forbidden = ("locua.engine", "locua.engine_adapter", "locua.guided",
                 "mlx", "mlx_lm", "torch", "transformers", "huggingface_hub")
    class NoRuntimeImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                raise AssertionError("Deterministic surface initialized runtime: " + fullname)
    sys.meta_path.insert(0, NoRuntimeImports())
    import locua
    from locua import cli, lib

    resources = {}
    for resource in lib.RESOURCES:
        path = lib.skill_directory() / resource
        assert path.is_file() and path.read_bytes(), resource
        resources[resource] = {"path": str(path), "bytes": path.stat().st_size}
    help_text = lib.skill("start")
    assert '<skill_content name="locua-start">' in help_text
    assert "--json" in help_text and "--document" in help_text and "--url" in help_text
    assert "locua start" in lib.short_help("start")

    results = []
    calls = []
    current = None
    def stub_start(**kwargs):
        calls.append(kwargs)
        return current
    lib.start = stub_start
    for status, reason, expected in (("complete", None, 0),
                                      ("blocked", "synthetic_guard_refusal", 6),
                                      ("canceled", "synthetic_cancellation", 130)):
        for machine_readable in (False, True):
            current = {"schema": "locua.result.v1", "operation": "start", "ok": status == "complete",
                       "result": {"status": status, "reason": reason, "artifacts": "/synthetic-output",
                                  "model": "baseline", "synthetic": True}}
            argv = ["start", "--manual", "--url", "http://127.0.0.1:1234/synthetic"]
            if machine_readable:
                argv += ["--json"]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = cli.main(argv)
            assert exit_code == expected, (status, exit_code)
            assert stderr.getvalue() == ""
            assert calls[-1]["model"] is None  # Public library resolves mode-specific omissions.
            assert calls[-1]["browser_click_route"] == "trusted"
            assert calls[-1]["native_save_route"] == "menu"
            assert callable(calls[-1]["ask"]) and callable(calls[-1]["progress"])
            assert calls[-1]["url"] == "http://127.0.0.1:1234/synthetic"
            if machine_readable:
                assert json.loads(stdout.getvalue()) == current
                assert len(stdout.getvalue().splitlines()) == 1
            else:
                lines = ["Locua " + status + "."]
                if reason:
                    lines.append("Reason: " + reason)
                lines.append("Results: /synthetic-output/summary.json")
                assert stdout.getvalue() == "\\n".join(lines) + "\\n"
            results.append({"status": status, "json": machine_readable,
                            "exit_code": exit_code, "stdout": stdout.getvalue()})
    unexpected = [name for name in sys.modules if
                  any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)]
    assert not unexpected, unexpected
    print(json.dumps({"module": locua.__file__, "resources": resources,
                      "runtime_imports": unexpected, "blocked_import_prefixes": forbidden,
                      "presentation_cases": results, "library_start_was_stubbed": True,
                      "inference_calls": 0, "gui_calls": 0}))
''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    # The conformance kit walks distribution roots for a single manifest and
    # intentionally excludes dist/. Keep installed test copies under it.
    parser.add_argument("--environment", type=Path, default=Path("dist/installed-wheel"))
    parser.add_argument("--output", type=Path, default=Path("results/wheel-smoke.json"))
    args = parser.parse_args()
    wheel, environment = args.wheel.resolve(), args.environment.resolve()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LOCUA_CONFIG")}
    if not environment.exists():
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, env=env)
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python, cli = scripts / ("python.exe" if sys.platform == "win32" else "python"), scripts / ("locua.exe" if sys.platform == "win32" else "locua")
    installed = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", str(wheel)],
                               env=env, text=True, capture_output=True)
    if installed.returncode:
        raise RuntimeError(installed.stderr or installed.stdout)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        required = ["locua/SMART_TOOL.md", "locua/docs/installation.md", "locua/docs/usage.md",
                    "locua/guided.py", "locua/conversation.py", "locua/language_planning.py", "locua/document_open.py", "locua/native_save.py",
                    "locua/engine_adapter.py", "locua/engine/probes/offline-macos.sb",
                    "locua/engine/probes/requirements-rlcd.lock.txt", "locua/engine/vendor/qwen/README.md",
                    "locua/engine/vendor/qwen/source.json"]
        absent = [name for name in required if name not in names]
        if absent:
            raise RuntimeError("Wheel misses required runtime resources: " + repr(absent))
    checks = []
    with tempfile.TemporaryDirectory(prefix="locua-wheel-smoke-") as folder:
        cwd = Path(folder).resolve()
        env["LOCUA_CONFIG"] = str(cwd / "missing-config.json")
        def run(name, argv, expected=0, parse=True):
            response = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            item = {"name": name, "exit_code": response.returncode, "expected": expected,
                    "stdout": json.loads(response.stdout) if parse else response.stdout,
                    "stderr": response.stderr, "passed": response.returncode == expected}
            checks.append(item)
            if not item["passed"]:
                raise AssertionError(item)
            return item["stdout"]
        run("installed_help", [str(cli), "--help"], parse=False)
        start_short = run("installed_start_short_help", [str(cli), "start", "-h"], parse=False)
        assert "locua start" in start_short
        start_help = run("installed_start_full_help", [str(cli), "start", "--help"], parse=False)
        assert '<skill_content name="locua-start">' in start_help
        assert all(option in start_help for option in ("--json", "--url", "--document"))
        deterministic = run("installed_start_resources_and_stubbed_presentation_no_runtime", [
            str(python), "-I", "-c", DETERMINISTIC_START_PROBE])
        assert str(environment) in deterministic["module"], "Not importing the installed wheel"
        assert all(str(environment) in resource["path"] for resource in deterministic["resources"].values())
        invalid_start = run("installed_start_invalid_targets_json", [str(cli), "start", "--json",
                            "--url", "http://127.0.0.1:1234/synthetic", "--document", "/synthetic.txt"], expected=2)
        assert invalid_start["ok"] is False and invalid_start["error"]["code"] == "bad_invocation"
        do_help = run("installed_do_help", [str(cli), "do", "--help"], parse=False)
        assert '<skill_content name="locua-do">' in do_help
        language_json = run("installed_language_missing_configuration", [str(cli), "do", "--json", "open calculator",
                            "--out", str(cwd / "missing-language")], expected=6)
        assert language_json["result"]["error"]["code"] == "runtime_configuration_missing"
        missing_start = run("installed_start_missing_configuration_remedy", [str(cli), "start", "--manual", "--json",
                             "--url", "http://127.0.0.1:1234/synthetic", "--out", str(cwd / "missing-start")], expected=6)
        assert missing_start["result"]["error"]["code"] == "runtime_configuration_missing"
        assert "locua setup --help" in missing_start["result"]["remedy"]
        assert missing_start["result"]["config_path"] == str(cwd / "missing-config.json")
        manifest = run("manifest_no_provider", [str(cli), "manifest"])
        assert manifest["result"]["frontmatter"]["name"] == "locua"
        doctor = run("doctor_no_assets", [str(cli), "doctor"])
        assert doctor["result"]["ready"] is False
        run("bad_invocation", [str(cli), "no-such-command"], expected=2)
        identity = run("installed_identity_and_vendor_hashes", [str(python), "-I", "-c",
            "import json,locua;from locua.engine.runtime_paths import verify_source;print(json.dumps({'module':locua.__file__,'source_revision':verify_source()['revision']}))"])
        assert str(environment) in identity["module"], "Not importing the installed wheel"
        snapshot = {"kind":"native_window_state", "target":{"pid":101,"window_id":202},
                    "snapshot_id":"synthetic-1", "observed_at_ns":time.time_ns(), "controls":[
                    {"id":"synthetic-root","name":"Synthetic panel","role":"AXWindow","parent":None,
                     "states":{},"actions":[]}], "handles":{}, "text":"Synthetic data only.",
                    "coverage":{"complete":False}, "provenance":{"synthetic":True}}
        source = cwd / "snapshot.json"; source.write_text(json.dumps(snapshot))
        observation = run("snapshot_inspection_no_driver", [str(cli), "observe", "--snapshot", str(source),
                           "--view", "overview", "--out", str(cwd / "observation")])
        assert observation["result"]["desktop_mutated"] is False
        task = cwd / "task.json"; task.write_text('{}')
        blocked = run("run_missing_runtime_fails", [str(cli), "run", "--task", str(task),
                      "--out", str(cwd / "blocked-run")], expected=6)
        assert blocked["ok"] is False
    report = {"schema":"locua.package_smoke.v1", "wheel":str(wheel),
              "wheel_sha256":hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "environment":str(environment), "source_cwd_used":False, "source_pythonpath_used":False,
              "inference_calls":0,"gui_calls":0,"checks":checks,"passed":all(x["passed"] for x in checks)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"passed":report["passed"],"checks":len(checks),"output":str(args.output),"wheel_sha256":report["wheel_sha256"]}))


if __name__ == "__main__":
    main()
