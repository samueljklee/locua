"""Recorded decisions and public pagination: fake model, no GPU or desktop."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from locua import engine_adapter as adapter, lib
from locua.cli import main
from locua.errors import LocuaError

CONFIG = {"runtime_python": "/explicit/python", "model_cache": "/explicit/cache"}


def case(identity="one", *, expected="z"):
    return {"id": identity, "goal": "Use the supplied exact text.", "observation_summary": "Recorded local fixture state.",
            "candidates": [{"id": "z", "description": "Enter  00017-A\n"}, {"id": "a", "description": "Leave unchanged"}],
            "expected_id": expected}


def response(*, selected="z", request_id, candidates, **unused):
    return {"request_id": request_id, "selected_id": selected, "abstained": selected is None,
            "coverage": {"candidate_ids": [c["id"] for c in candidates], "omitted_candidates": 0},
            "dispatched": False, "timing": {"inference_ms": 2.5}, "raw": {"fixture": True}}


class RecordedEvaluationTests(unittest.TestCase):
    def run_cases(self, cases, service, folder, *, progress=lambda _: None):
        return adapter._evaluate({"cases": {"kind": "decisions", "cases": cases}, "model": "comparator",
                                  "out": str(Path(folder) / "eval")}, CONFIG, progress)

    def test_one_resident_preserves_order_literals_and_no_gold_in_model_input(self):
        service = Mock(); service.info.return_value = {"model_key": "comparator"}
        service.choose.side_effect = response
        cases = [case("first"), case("second", expected="a")]; before = deepcopy(cases)
        messages = []
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service) as constructor:
            result = self.run_cases(cases, service, folder, progress=messages.append)
            self.assertEqual(result["status"], "evaluated")
            self.assertEqual(result["scoring"], {"expected_total": 2, "graded": 2, "correct": 1, "incorrect": 1, "ungraded_expected": 0})
            self.assertEqual((result["valid_outputs"], result["invalid_outputs"], result["unrun"]), (2, 0, 0))
            self.assertEqual(result["results"][0]["response"]["timing"]["inference_ms"], 2.5)
            self.assertGreaterEqual(result["end_to_end_wall_s"], result["startup_wall_s"])
            for path in (Path(folder) / "eval").glob("*.json"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            saved = json.loads((Path(folder) / "eval/model-inputs.json").read_text())
            self.assertTrue(all(set(x) == {"goal", "observation_summary", "candidates", "history"} for x in saved))
        constructor.assert_called_once_with(model="comparator"); service.close.assert_called_once()
        self.assertEqual(cases, before)
        for call in service.choose.call_args_list:
            self.assertNotIn("expected_id", call.kwargs)
            self.assertNotIn("id", call.kwargs)
            self.assertEqual(call.kwargs["candidates"], cases[0]["candidates"])
            self.assertEqual(call.kwargs["history"], [])
        self.assertTrue(any("2/2" in text for text in messages))

    def test_missing_gold_unscored_and_null_explicitly_means_abstain(self):
        service = Mock(); service.info.return_value = {}
        service.choose.side_effect = lambda **kw: response(selected=None, **kw)
        unscored = case("unscored"); del unscored["expected_id"]
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service):
            result = self.run_cases([case(expected=None), unscored], service, folder)
        self.assertEqual(result["scoring"], {"expected_total": 1, "graded": 1, "correct": 1, "incorrect": 0, "ungraded_expected": 0})
        self.assertTrue(result["results"][0]["output_valid"])
        self.assertIsNone(result["results"][1]["score"])

    def test_captured_history_is_preserved_exactly_and_gold_stays_outside(self):
        item=case();item['history']=[{'action':'Inspect region Ω','outcome':'Read-only; no effect.  \n'}]
        before=deepcopy(item);service=Mock();service.info.return_value={};service.choose.side_effect=response
        with tempfile.TemporaryDirectory() as folder, patch('locua.engine.prototype.decision.ModelService',return_value=service):
            result=self.run_cases([item],service,folder)
            saved=json.loads((Path(folder)/'eval/model-inputs.json').read_text())
        self.assertEqual(saved[0]['history'],before['history'])
        self.assertEqual(service.choose.call_args.kwargs['history'],before['history'])
        self.assertNotIn('expected_id',service.choose.call_args.kwargs)
        self.assertEqual(item,before);self.assertEqual(result['scoring']['correct'],1)

    def test_all_inputs_validate_before_model_load_and_artifact_creation(self):
        invalid = []
        for key, value in (("goal", " "), ("observation_summary", None), ("id", "\n"), ("expected_id", "missing"),
                           ("candidates", [{"id": "x", "description": "x", "expected": True}]),
                           ("candidates", [{"id": "x", "description": "x"}] * 2),
                           ("candidates", [{"id": str(i), "description": "x"} for i in range(255)])):
            item = case("bad"); item[key] = value; invalid.append(item)
        for history in (None, [{'action':'a'}], [{'action':'a','outcome':False}], [{'action':'a','outcome':'b'}]*65):
            unknown = case('bad');unknown['history']=history;invalid.append(unknown)
        unknown = case('bad');unknown['unknown_field']=[];invalid.append(unknown)
        invalid.append(case("first"))
        with patch("locua.engine.prototype.decision.ModelService") as constructor, patch.object(adapter, "artifact_directory") as output:
            for item in invalid:
                with self.subTest(item=item), self.assertRaises(ValueError):
                    self.run_cases([case("first"), item], None, "/ignored")
        constructor.assert_not_called(); output.assert_not_called()

    def test_unknown_eval_kind_is_not_silently_simulation(self):
        with patch("locua.engine.prototype.decision.ModelService") as constructor:
            with self.assertRaisesRegex(ValueError, "kind"):
                adapter._evaluate({"cases": {"kind": "decision", "cases": [case()]}, "model": "baseline"}, CONFIG, lambda _: None)
        constructor.assert_not_called()

    def test_invalid_selection_coverage_and_dispatch_claim_stop_without_scoring(self):
        for field, value in (("selected_id", "outside"), ("coverage", {"candidate_ids": ["a", "z"], "omitted_candidates": 0}),
                             ("dispatched", True), ("request_id", "foreign"), ("abstained", True)):
            service = Mock(); service.info.return_value = {}
            def bad(**kwargs):
                item = response(**kwargs); item[field] = value; return item
            service.choose.side_effect = bad
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service):
                result = self.run_cases([case(), case("two")], service, folder)
            self.assertEqual(result["status"], "blocked"); self.assertEqual(result["invalid_outputs"], 1)
            self.assertEqual(result["unrun_case_ids"], ["two"])
            self.assertEqual(result["scoring"]["graded"], 0)
            self.assertEqual(result["scoring"]["ungraded_expected"], 2)
            service.choose.assert_called_once(); service.close.assert_called_once()

    def test_worker_error_retains_attempted_row_and_unrun_denominator(self):
        service = Mock(); service.info.return_value = {}; service.choose.side_effect = TimeoutError("worker stopped")
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service):
            result = self.run_cases([case(), case("two")], service, folder)
            row = json.loads((Path(folder) / "eval/case-001.json").read_text())
        self.assertEqual((result["attempted"], result["unrun"], result["total"]), (1, 1, 2))
        self.assertEqual(row["status"], "error"); self.assertIsNone(row["output_valid"])
        self.assertEqual(result["scoring"]["graded"], 0); service.close.assert_called_once()

    def test_interrupt_during_choose_discards_score_and_stops_next_case(self):
        handlers = []
        def register(sig, handler):
            handlers.append(handler); return "prior-handler"
        service = Mock(); service.info.return_value = {}
        def choose(**kw):
            handlers[0](None, None); return response(**kw)
        service.choose.side_effect = choose
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service), patch.object(adapter.signal, "signal", side_effect=register):
            result = self.run_cases([case(), case("two")], service, folder)
        self.assertEqual(result["status"], "canceled"); self.assertEqual(result["unrun"], 1)
        self.assertEqual(result["valid_outputs"], 1); self.assertEqual(result["scoring"]["graded"], 0)
        self.assertTrue(result["results"][0]["response_discarded_after_cancellation"])
        self.assertEqual(handlers[-1], "prior-handler"); service.close.assert_called_once()

    def test_startup_error_unrun_all_and_cleanup_failure_is_not_success(self):
        service = Mock(); service.info.side_effect = RuntimeError("info failed"); service.close.side_effect = RuntimeError("cleanup failed")
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService", return_value=service):
            result = self.run_cases([case()], service, folder)
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertEqual(result["status_before_cleanup_failure"], "blocked")
        self.assertEqual(result["unrun"], 1); self.assertEqual(result["attempted"], 0)
        service.choose.assert_not_called(); service.close.assert_called_once()


class ObservationPaginationTests(unittest.TestCase):
    def test_cli_library_and_adapter_forward_exact_pagination_and_region_search(self):
        view = {"coverage": {"continuation": "next"}}
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.observation_tools.search", return_value=view) as search, patch.object(adapter, "owner", side_effect=AssertionError("no GUI")):
            source = Path(folder) / "source.json"; source.write_text('{"snapshot_id":"saved"}')
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                status = main(["observe", "--snapshot", str(source), "--view", "search", "--query", "Twin",
                               "--region-id", "r1", "--cursor", "source-bound-token", "--limit", "7", "--out", str(Path(folder) / "out")])
        self.assertEqual(status, 0); self.assertEqual(json.loads(output.getvalue())["result"]["view"], view)
        search.assert_called_once_with({"snapshot_id": "saved"}, "Twin", region_id="r1", cursor="source-bound-token", limit=7)

    def test_inappropriate_view_options_fail_before_adapter(self):
        with patch("locua.lib.importlib.import_module", side_effect=AssertionError("must not load adapter")):
            for options in ({"view": "full", "cursor": "x"}, {"view": "full", "limit": 2}, {"view": "overview", "query": "x"},
                            {"view": "overview", "region_id": "x"}, {"limit": True}, {"limit": 257}, {"limit": 0},
                            {"cursor": ""}, {"kind": "native"}):
                with self.subTest(options=options), self.assertRaises(LocuaError):
                    lib.observe(snapshot={}, **options)

    def test_overview_inspect_pagination_plumbing_and_stale_errors_propagate(self):
        for kind in ("overview", "inspect"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.observation_tools." + kind, side_effect=ValueError("stale cursor")) as view:
                with self.assertRaisesRegex(LocuaError, "stale cursor"):
                    lib.observe(snapshot={"snapshot_id": "saved"}, view=kind, region_id="r1" if kind == "inspect" else None,
                                cursor="old", limit=4, out=str(Path(folder) / "out"))
            self.assertEqual(view.call_args.kwargs, {"cursor": "old", "limit": 4})


if __name__ == "__main__":
    unittest.main()
