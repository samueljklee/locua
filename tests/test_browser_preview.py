"""Acceptance-runner ownership and oracle checks, no actual server/GUI/model."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import browser_preview as preview


class Child:
    def __init__(self):
        self.pid, self.returncode, self.stdout = 4242, None, io.BytesIO()
        self.terminated, self.killed = 0, 0
    def poll(self): return self.returncode
    def terminate(self): self.terminated += 1; self.returncode = -15
    def wait(self, timeout): return self.returncode
    def kill(self): self.killed += 1; self.returncode = -9


class BrowserPreviewTests(unittest.TestCase):
    def run_fake(self, folder, engine_call, *, case="contact", ready_error=None):
        folder = Path(folder)
        config = folder / "config.json"; config.write_text("{}")
        process = Child()
        def ready(child, receipts):
            self.assertIs(child, process)
            if ready_error: raise ready_error
            return {"url": "http://127.0.0.1:54321", "receipts": str(receipts)}
        original_verify = preview.verify_receipt
        with patch.object(preview.subprocess, "Popen", return_value=process) as popen, patch.object(
                preview, "read_server_ready", side_effect=ready), patch.object(preview.lib, "run", side_effect=engine_call) as engine, patch.object(
                preview, "verify_receipt", side_effect=lambda path, expected: original_verify(path, expected, wait_s=0)), redirect_stderr(io.StringIO()):
            report = preview.run_preview(case=case, config=config, out=folder / "acceptance", execute=True,
                                         model="comparator", browser_click_route="dom_event")
        return report, process, engine, popen

    def test_complete_exact_receipt_is_independent_and_only_url_changes(self):
        source = preview.FIXTURES / "contact-plan.json"; original = source.read_bytes()
        template = json.loads(original)
        def run(**kwargs):
            task = deepcopy(kwargs["task"])
            self.assertEqual(task["scope"]["url"], "http://127.0.0.1:54321/contact")
            task["scope"]["url"] = template["scope"]["url"]
            self.assertEqual(task, template)
            self.assertEqual(set(kwargs), {"task", "config", "model", "browser_click_route", "execute", "out", "progress"})
            self.assertEqual(kwargs["browser_click_route"], "dom_event")
            self.assertTrue(kwargs["execute"])
            receipt = Path(kwargs["out"]).parent / "receipts/contact.json"
            preview.private_json(receipt, preview.EXPECTED["contact"])
            kwargs["progress"]("fake local run")
            return {"ok": True, "result": {"status": "complete"}}
        with tempfile.TemporaryDirectory() as folder:
            report, process, engine, popen = self.run_fake(folder, run)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["receipt_verification"]["status"], "pass")
            self.assertFalse(report["oracle_supplied_to_model"])
            self.assertTrue(report["server_cleanup"]["verified_stopped"])
            self.assertGreaterEqual(report["timing"]["full_end_to_end_wall_s"], report["timing"]["locua_run_including_model_and_cleanup_wall_s"])
            for path in (Path(folder) / "acceptance").glob("*.json*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:4], ["--port", "0"])
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(source.read_bytes(), original)
        engine.assert_called_once(); self.assertEqual(process.terminated, 1); self.assertEqual(process.killed, 0)

    def test_complete_claim_without_saved_receipt_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            report, process, engine, _ = self.run_fake(folder, lambda **_: {"ok": True, "result": {"status": "complete"}})
        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["engine_complete"])
        self.assertEqual(report["receipt_verification"]["reason"], "saved_receipt_absent")
        self.assertEqual(process.terminated, 1); engine.assert_called_once()

    def test_exact_receipt_does_not_override_blocked_engine(self):
        def blocked(**kwargs):
            preview.private_json(Path(kwargs["out"]).parent / "receipts/badge.json", preview.EXPECTED["badge"])
            return {"ok": False, "result": {"status": "blocked", "reason": "postcondition unknown"}}
        with tempfile.TemporaryDirectory() as folder:
            report, process, engine, _ = self.run_fake(folder, blocked, case="badge")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["receipt_verification"]["status"], "pass")
        self.assertFalse(report["engine_complete"])
        self.assertEqual(process.terminated, 1); engine.assert_called_once()

    def test_startup_and_engine_exceptions_stop_only_owned_child(self):
        for phase in ("startup", "engine"):
            def broken(**_): raise RuntimeError("injected model boundary failure")
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                report, process, engine, _ = self.run_fake(folder, broken,
                    ready_error=TimeoutError("injected startup bound") if phase == "startup" else None)
                saved = json.loads((Path(folder) / "acceptance/summary.json").read_text())
            self.assertEqual(report["status"], "fail")
            self.assertEqual(saved["status"], "fail")
            self.assertTrue(report["server_cleanup"]["verified_stopped"])
            self.assertEqual(process.terminated, 1)
            self.assertEqual(engine.call_count, 0 if phase == "startup" else 1)
            if phase == "engine":
                self.assertEqual(report["receipt_verification"]["reason"], "saved_receipt_absent")

    def test_startup_line_requires_owned_loopback_url_and_receipt_path(self):
        child = Child(); child.stdout = Mock(); child.stdout.fileno.return_value = 99
        with tempfile.TemporaryDirectory() as folder:
            receipts = Path(folder)
            for url in ("http://127.0.0.1:54321", "http://example.test:54321", "http://127.0.0.1:54321/contact"):
                line = (json.dumps({"url": url, "receipts": str(receipts)})+"\n").encode()
                with self.subTest(url=url), patch.object(preview.select, "select", return_value=([99], [], [])), patch.object(preview.os, "read", return_value=line):
                    if url == "http://127.0.0.1:54321":
                        self.assertEqual(preview.read_server_ready(child, receipts)["url"], url)
                    else:
                        with self.assertRaisesRegex(ValueError, "owned loopback"):
                            preview.read_server_ready(child, receipts)

    def test_full_receipt_comparison_rejects_extra_missing_and_wrong_type(self):
        expected = preview.EXPECTED["contact"]
        for mode in ("extra", "missing", "type", "value"):
            actual = deepcopy(expected)
            if mode == "extra": actual["other"] = "unexpected"
            elif mode == "missing": del actual["shipping.cost"]
            elif mode == "type": actual["billing.news"] = 0  # 0 must not equal False here.
            else: actual["billing.cost"] = "46"
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "receipt.json"; path.write_text(json.dumps(actual))
                result = preview.verify_receipt(path, expected, wait_s=0)
            self.assertEqual(result["status"], "fail")

    def test_owned_server_timeout_uses_bounded_kill_and_reaps(self):
        child = Child()
        child.wait = Mock(side_effect=[subprocess.TimeoutExpired("fixture", 5), -9])
        result = preview.stop_server(child)
        self.assertTrue(result["verified_stopped"]); self.assertTrue(result["kill_required"])
        self.assertEqual((child.terminated, child.killed), (1, 1))
        self.assertTrue(child.stdout.closed)
        self.assertEqual(child.wait.call_count, 2)

    def test_interrupt_during_oracle_still_cleans_owned_server(self):
        process = Child()
        with tempfile.TemporaryDirectory() as folder, patch.object(preview.subprocess, "Popen", return_value=process), patch.object(
                preview, "read_server_ready", side_effect=lambda child, receipts: {"url": "http://127.0.0.1:54321", "receipts": str(receipts)}), patch.object(
                preview.lib, "run", return_value={"ok": True, "result": {"status": "complete"}}), patch.object(
                preview, "verify_receipt", side_effect=KeyboardInterrupt), redirect_stderr(io.StringIO()):
            config = Path(folder) / "config.json"; config.write_text("{}")
            report = preview.run_preview(case="contact", config=config, out=Path(folder) / "run", execute=True)
        self.assertEqual(report["status"], "canceled")
        self.assertTrue(report["server_cleanup"]["verified_stopped"])
        self.assertEqual(process.terminated, 1)

    def test_no_execution_or_existing_output_never_starts_server(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(preview.subprocess, "Popen") as popen:
            config = Path(folder) / "config.json"; config.write_text("{}")
            with self.assertRaisesRegex(ValueError, "explicit --execute"):
                preview.run_preview(case="contact", config=config, out=Path(folder) / "new")
            with self.assertRaises(FileExistsError):
                preview.run_preview(case="contact", config=config, out=folder, execute=True)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
