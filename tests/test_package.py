"""Package/library/CLI boundaries. Fake adapter only; no desktop or model calls."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest
import venv
from unittest.mock import patch

from locua import __version__, config, lib
from locua.cli import main
from locua.errors import LocuaError


class PackageTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                status = main(args)
            except SystemExit as error:
                status = error.code
        return status, out.getvalue(), err.getvalue()

    def test_manifest_is_one_installed_source(self):
        m = lib.manifest()
        self.assertEqual(m["frontmatter"]["version"], __version__)
        self.assertEqual(m["frontmatter"]["name"], "locua")
        self.assertEqual(m["frontmatter"]["platforms"], ["macos"])
        self.assertTrue(m["body"])
        for resource in lib.RESOURCES:
            self.assertTrue((lib.skill_directory() / resource).is_file(), resource)

    def test_help_all_capabilities_needs_no_adapter_or_model(self):
        with patch("locua.lib.importlib.import_module", side_effect=AssertionError("must not load engine")):
            for capability in [None, *lib.CAPABILITIES]:
                for flag in ("-h", "--help"):
                    status, output, error = self.invoke(([capability] if capability else []) + [flag])
                    self.assertEqual(status, 0)
                    self.assertFalse(error)
                    self.assertIn("locua", output)
                    if flag == "--help":
                        self.assertIn("<skill_content", output)
                        self.assertIn("Skill directory:", output)
                        self.assertNotIn("\nusage:", output)

    def test_manifest_stdout_is_one_json_no_diagnostics(self):
        status, output, error = self.invoke(["manifest"])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output)["ok"])
        self.assertFalse(error)

    def test_bad_cli_and_duplicate_json_are_explicit_failures(self):
        status, output, _ = self.invoke(["does-not-exist"])
        self.assertEqual(status, 2)
        self.assertIn("remedy", json.loads(output)["error"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.json"; path.write_text('{"id":1,"id":2}')
            status, output, _ = self.invoke(["run", "--task", str(path)])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output)["error"]["code"], "invalid_json_file")

    def test_setup_atomic_no_implicit_launch_or_download(self):
        with tempfile.TemporaryDirectory() as folder, patch("locua.lib.importlib.import_module", side_effect=AssertionError("no runtime")):
            path = Path(folder).resolve() / "settings/config.json"
            result = lib.setup({"model_cache": "cache"}, path=path)
            self.assertEqual(result["result"]["config"]["model_cache"], str(path.parent / "cache"))
            self.assertFalse(result["result"]["models_downloaded"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(LocuaError):
                lib.setup({}, path=path)
            lib.setup({"model_cache": "replacement"}, path=path, replace=True)
            self.assertEqual(config.load(path)[0]["model_cache"], str(path.parent / "replacement"))

    def test_config_selection_explicit_then_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder).resolve() / "a.json"; second = Path(folder).resolve() / "b.json"
            with patch.dict(os.environ, {"LOCUA_CONFIG": str(second)}):
                self.assertEqual(config.selected_path(first), first)
                self.assertEqual(config.selected_path(), second)
        for bad in ({"version": True}, {"model_backend": "cloud"}, {"lab_engine_path": "/hidden"}, {"runtime_python": []}):
            with self.subTest(bad=bad), self.assertRaises(LocuaError):
                config.validate(bad)

    def test_venv_python_symlink_survives_config_roundtrip_and_invocation(self):
        if sys.platform == "win32":
            self.skipTest("This regression exercises POSIX venv symlink identity")
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();environment=root/"runtime"
            venv.EnvBuilder(with_pip=False,symlinks=True).create(environment)
            interpreter=environment/"bin/python"
            self.assertTrue(interpreter.is_symlink())
            self.assertNotEqual(interpreter,interpreter.resolve())
            config.write({"runtime_python":str(interpreter)},root/"config.json")
            loaded,_=config.load(root/"config.json")
            self.assertEqual(loaded["runtime_python"],str(interpreter))
            result=subprocess.run([loaded["runtime_python"],"-c","import sys;print(sys.prefix)"],check=True,text=True,capture_output=True)
            self.assertEqual(Path(result.stdout.strip()),environment)

    def test_doctor_does_not_call_paths_ready_without_runtime_probe(self):
        with tempfile.TemporaryDirectory() as folder, patch("locua.lib.importlib.import_module", side_effect=AssertionError("no runtime")):
            result = lib.doctor({field: folder for field in config.PATH_FIELDS})
            self.assertTrue(result["ok"])
            self.assertFalse(result["result"]["ready"])
            self.assertFalse(result["result"]["permissions_verified"])
            self.assertFalse(result["result"]["model_loaded"])

    def test_missing_engine_has_named_remedy_no_lab_fallback(self):
        with patch("locua.lib.importlib.import_module", side_effect=ImportError("missing")):
            with self.assertRaises(LocuaError) as caught:
                lib.run(task={"id": "reviewed"})
        self.assertEqual(caught.exception.code, "engine_unavailable")
        self.assertIn("no hidden lab fallback", caught.exception.remedy)

    def test_run_preserves_literal_data_preview_default_and_adapter_boundary(self):
        calls = []
        def execute(**kwargs):
            calls.append(kwargs)
            kwargs["payload"]["task"]["value"] = "mutated copy"
            kwargs["progress"]("local progress")
            return {"status": "proposed"}
        data = {"value": " 00042-A\nO'Neil & Sons – Lab "}; before = deepcopy(data)
        with patch("locua.lib.importlib.import_module", return_value=types.SimpleNamespace(execute=execute)):
            result = lib.run(task=data)
        self.assertEqual(data, before)
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0]["operation"], "run")
        self.assertFalse(calls[0]["payload"]["execute"])
        self.assertEqual(calls[0]["payload"]["model"], "baseline")
        self.assertEqual(set(calls[0]), {"operation", "payload", "config", "progress"})

    def test_data_input_and_scope_validation_precedes_adapter(self):
        with patch("locua.lib.importlib.import_module", side_effect=AssertionError("not called")):
            for kwargs in ({}, {"request": "do stuff"}, {"task": {}, "request": "both"}, {"task": {}, "model": "online"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(LocuaError): lib.run(**kwargs)
            with self.assertRaises(LocuaError): lib.observe(snapshot={}, target={})
            with self.assertRaises(LocuaError): lib.observe(snapshot={}, view="inspect")

    def test_cli_progress_separate_and_blocked_results_nonzero(self):
        def execute(**kwargs):
            kwargs["progress"]("checking local target")
            return {"status": "blocked", "reason": "ambiguous"}
        with tempfile.TemporaryDirectory() as folder, patch("locua.lib.importlib.import_module", return_value=types.SimpleNamespace(execute=execute)):
            path = Path(folder)/"task.json"; path.write_text('{"id":"task"}')
            status, output, error = self.invoke(["run", "--task", str(path)])
        self.assertEqual(status, 6)
        self.assertFalse(json.loads(output)["ok"])
        self.assertEqual(error, "checking local target\n")

    def test_explicit_comparator_and_execution_are_forwarded(self):
        calls=[]
        def execute(**kwargs):
            calls.append(kwargs);return {"status":"complete"}
        with patch("locua.lib.importlib.import_module", return_value=types.SimpleNamespace(execute=execute)):
            lib.run(request="Set name", scope={"kind":"simulation"}, model="comparator", execute=True)
        self.assertEqual(calls[0]["payload"]["model"], "comparator")
        self.assertTrue(calls[0]["payload"]["execute"])

    def test_targets_is_read_only_explicit_pid_payload(self):
        calls=[]
        def execute(**kwargs):
            calls.append(kwargs);return {"status":"observed","windows":[]}
        with patch("locua.lib.importlib.import_module", return_value=types.SimpleNamespace(execute=execute)):
            self.assertTrue(lib.targets(pid=42)["ok"])
            self.assertTrue(lib.targets()["ok"])
            with self.assertRaises(LocuaError):lib.targets(pid=True)
        self.assertEqual([c["payload"] for c in calls],[{"pid":42},{"pid":None}])
        self.assertTrue(all(c["operation"]=="targets" for c in calls))


if __name__ == "__main__":
    unittest.main()
