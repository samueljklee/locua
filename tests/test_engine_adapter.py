"""Owned-process cleanup and adapter data contracts with no model or GUI."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from locua import engine_adapter as adapter
from locua import config, lib
from locua.errors import LocuaError

CONFIG = {"runtime_python": "/explicit/python", "model_cache": "/explicit/cache"}


def plan():
    return {"version":"locua-task-plan-v1","request":"Set Note to hello", "scope":{"kind":"browser","url":"http://localhost/test"},
            "outcomes":[{"id":"note","subject":{"name":"Note"},"property":"value","value":"hello",
                         "requires":[],"evidence_plane":"display","source_text":"Set Note to hello"}], "constraints":[],"unknowns":[]}


class AdapterTests(unittest.TestCase):
    def test_browser_click_route_is_explicit_and_reaches_driver(self):
        from locua.cli import parser
        self.assertEqual(parser().parse_args(['run', '--task', 'plan.json']).browser_click_route, 'trusted')
        self.assertEqual(parser().parse_args(['run', '--task', 'plan.json', '--browser-click-route', 'dom_event']).browser_click_route, 'dom_event')
        with patch.object(lib, '_engine', return_value={}) as engine:
            lib.run(task=plan(), browser_click_route='dom_event')
            self.assertEqual(engine.call_args.args[1]['browser_click_route'], 'dom_event')
            lib.run(task=plan())
            self.assertEqual(engine.call_args.args[1]['browser_click_route'], 'trusted')
            with self.assertRaises(LocuaError): lib.run(task=plan(), browser_click_route='auto')
        for route in ('trusted', 'dom_event'):
            connection=Mock(); connection.close.return_value=[]
            with tempfile.TemporaryDirectory() as folder, patch.object(adapter, 'owner', return_value=connection), patch(
                    'locua.engine.prototype.cli.prepare_owned_browser', return_value={'target_id':'owned','tab_id':'tab'}), patch(
                    'locua.engine.prototype.cua.CuaAdapter') as driver:
                driver.return_value.observe.side_effect=ValueError('stop before model and actions')
                report=adapter._run({'task':plan(),'model':'baseline','execute':True,'browser_click_route':route,
                                     'out':str(Path(folder)/'run')},CONFIG,lambda _:None)
                self.assertEqual(driver.call_args.kwargs['browser_click_route'],route)
                self.assertEqual(report['browser_click_route'],route)

    def test_public_doctor_propagates_permission_verification_separately_from_readiness(self):
        for grants, dependencies_ready, expected_verified, expected_ready in (
                ((True, True), True, True, True),
                ((False, True), True, True, False),
                ((True, True), False, True, False),
                (None, True, False, False)):
            raw = {"status": "package_ready", "runtime": "running", "permissions":
                   {"ok": True, "result": {"structuredContent": {"accessibility": grants[0], "screen_recording": grants[1]}}}
                   if grants is not None else {"ok": False, "error": "permissions pending", "exit_code": 75}}
            with self.subTest(grants=grants, dependencies_ready=dependencies_ready), tempfile.TemporaryDirectory() as folder, patch(
                    "locua.engine.driver_runtime.status", return_value=raw) as status, patch(
                    "locua.engine.driver_runtime.start") as start, patch.object(
                    adapter, "runtime_dependency_status", return_value={"ready": dependencies_ready}), patch.object(
                    lib.sys, "platform", "darwin"), patch("locua.engine.prototype.decision.ModelService") as model:
                result = lib.doctor({field: folder for field in config.PATH_FIELDS}, probe=True)["result"]
            self.assertEqual(result["permissions_verified"], expected_verified)
            self.assertEqual(result["permissions_verified"], result["runtime"]["permissions_verified"])
            self.assertEqual(result["ready"], expected_ready)
            self.assertFalse(result["model_loaded"]); self.assertFalse(result["desktop_observed"])
            status.assert_called_once(); start.assert_not_called(); model.assert_not_called()

    def test_dependency_probe_uses_configured_interpreter_without_loading_mlx(self):
        def response(argv,**kwargs):
            self.assertEqual(argv[0],"/chosen/venv/bin/python")
            self.assertEqual(argv[1:3],["-I","-c"])
            self.assertNotIn("import mlx",argv[3]);self.assertEqual(kwargs["timeout"],5)
            versions={name:None for name in json.loads(argv[4])}
            return subprocess.CompletedProcess(argv,0,json.dumps({"versions":versions,"prefix":"/chosen/venv"}),"")
        with patch.object(adapter.subprocess,"run",side_effect=response):
            result=adapter.runtime_dependency_status({"runtime_python":"/chosen/venv/bin/python"})
        self.assertFalse(result["ready"]);self.assertTrue(result["mismatches"])
        self.assertFalse(result["model_imported"])

    def test_dependency_probe_timeout_is_actionable_not_ready(self):
        with patch.object(adapter.subprocess,"run",side_effect=subprocess.TimeoutExpired("python",5)):
            result=adapter.runtime_dependency_status(CONFIG)
        self.assertFalse(result["ready"]);self.assertIn("runtime_python",result["remedy"])

    def test_driver_grants_are_distinct_from_direct_capture_and_model_readiness(self):
        raw={"status":"package_ready","runtime":"running","permissions":{"ok":True,"result":{"structuredContent":{
            "accessibility":True,"screen_recording":True,"screen_recording_capturable":None,"direct_capture_status":"not_checked"}}}}
        result=adapter.driver_readiness(raw,activation=True)
        self.assertTrue(result["ready"]);self.assertTrue(result["ok"])
        self.assertFalse(result["direct_capture_proven"]);self.assertFalse(result["model_load_proven"])
        raw["permissions"]["result"]["structuredContent"]["screen_recording"]=False
        result=adapter.driver_readiness(raw,activation=True)
        self.assertFalse(result["ready"]);self.assertFalse(result["ok"])
        self.assertEqual(result["status"],"awaiting_permissions")

    def test_pending_permission_gate_does_not_invent_false_grants(self):
        raw={"status":"package_ready","runtime":"running","permissions":{"ok":False,"error":"permissions pending","exit_code":75}}
        result=adapter.driver_readiness(raw)
        self.assertTrue(result["ok"]);self.assertFalse(result["ready"])
        self.assertIsNone(result["permissions"]);self.assertFalse(result["permissions_verified"])
        self.assertFalse(adapter.driver_readiness({"status":"package_ready","runtime":"stopped"})["ready"])

    def test_environment_restored_on_failure_and_no_implicit_values(self):
        with patch.dict(os.environ, {"LOCUA_RUNTIME_PYTHON":"prior-python"}, clear=True), patch.object(adapter.sys,"platform","darwin"):
            with self.assertRaisesRegex(RuntimeError,"injected"):
                with adapter.runtime_environment(CONFIG):
                    self.assertEqual(os.environ["LOCUA_MODEL_CACHE"],CONFIG["model_cache"])
                    raise RuntimeError("injected")
            self.assertEqual(os.environ["LOCUA_RUNTIME_PYTHON"],"prior-python")
            self.assertNotIn("LOCUA_MODEL_CACHE",os.environ)
            with self.assertRaises(LocuaError):
                with adapter.runtime_environment({}):pass

    def test_read_only_close_failures_are_not_success(self):
        connection=Mock();connection.call.return_value=({}, {"windows":[]});connection.close.return_value=["transport still running"]
        with tempfile.TemporaryDirectory() as folder, patch.object(adapter,"artifact_directory",return_value=Path(folder)), patch.object(adapter,"owner",return_value=connection):
            with self.assertRaisesRegex(LocuaError,"cleanup"):
                adapter._targets({"pid":17},{"driver_binary":"/synthetic/driver","driver_socket":"/synthetic/socket"},lambda _:None)
        connection.close.assert_called_once()
        connection.call.assert_called_once_with("list_windows",{"pid":17})

    def test_browser_setup_failure_closes_only_owned_resources(self):
        connection=Mock();connection.close.return_value=[];connection.call.return_value=({}, {"windows":[]})
        def partial(owner,session,url,cancel,state):
            state.update(session_started=True,owned_browser_pid=51)
            raise RuntimeError("partial browser setup")
        with tempfile.TemporaryDirectory() as folder, patch.object(adapter,"owner",return_value=connection), patch(
            "locua.engine.prototype.cli.prepare_owned_browser",side_effect=partial), patch("locua.engine.prototype.decision.ModelService") as model:
            result=adapter._run({"task":plan(),"model":"baseline","execute":False,"out":str(Path(folder)/"run")},CONFIG,lambda _:None)
        self.assertEqual(result["status"],"blocked")
        self.assertIn("partial browser setup",result["reason"])
        connection.end_session.assert_called_once();connection.close.assert_called_once();model.assert_not_called()
        connection.call.assert_called_once_with("list_windows",{"pid":51},cleanup=True)

    def test_browser_setup_cancel_is_canceled_and_cleanup_failure_retained(self):
        from locua.engine.prototype.cli import Cancelled
        connection=Mock();connection.close.return_value=["closed incompletely"]
        with tempfile.TemporaryDirectory() as folder, patch.object(adapter,"owner",return_value=connection), patch(
            "locua.engine.prototype.cli.prepare_owned_browser",side_effect=Cancelled("stop")):
            result=adapter._run({"task":plan(),"model":"baseline","execute":False,"out":str(Path(folder)/"run")},CONFIG,lambda _:None)
        self.assertEqual(result["status_before_cleanup_failure"],"canceled")
        self.assertEqual(result["status"],"cleanup_failed")
        connection.close.assert_called_once()

    def test_supplied_data_is_forwarded_to_review_only_planner(self):
        data={"literal":" 0017\n"};expected=deepcopy(data)
        p=plan();p["unknowns"]=["Which target?"]
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.architecture.generate_plan",return_value=(p,{"mock":True})) as planner, patch.object(adapter,"owner") as owner:
            result=adapter._run({"request":"Use supplied data","scope":{"kind":"simulation"},"supplied_data":data,
                                 "model":"baseline","execute":False,"out":str(Path(folder)/"run")},CONFIG,lambda _:None)
        self.assertEqual(result["status"],"needs_clarification");owner.assert_not_called()
        self.assertEqual(planner.call_args.kwargs["supplied_data"],expected);self.assertEqual(data,expected)

    def test_natural_language_execute_never_reaches_planner_or_owner(self):
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.architecture.generate_plan") as planner, patch.object(adapter,"owner") as owner:
            result=adapter._run({"request":"Edit this","scope":{"kind":"native"},"model":"baseline","execute":True,
                                 "out":str(Path(folder)/"run")},CONFIG,lambda _:None)
        self.assertEqual(result["status"],"blocked");self.assertIn("plan review",result["reason"])
        planner.assert_not_called();owner.assert_not_called()
        self.assertIsNone(result['driver']);self.assertIsNone(result['selector_decoding'])
        self.assertEqual(result['planner_decoding'],'compact_greedy');self.assertFalse(result['desktop_mutated'])

    def test_failed_planner_never_claims_cua_or_rlcd_execution(self):
        with tempfile.TemporaryDirectory() as folder, patch('locua.engine.prototype.architecture.generate_plan',side_effect=ValueError('invalid proposal')), patch.object(adapter,'owner') as owner:
            result=adapter._run({'request':'Edit this','scope':{'kind':'native'},'model':'comparator','execute':False,
                                 'out':str(Path(folder)/'run')},CONFIG,lambda _:None)
        self.assertEqual(result['status'],'blocked');self.assertIsNone(result['driver'])
        self.assertIsNone(result['selector_decoding']);self.assertEqual(result['planner_decoding'],'compact_greedy')
        self.assertFalse(result['desktop_mutated']);owner.assert_not_called()

    def test_evaluation_preserves_data_cancellation_and_full_denominator(self):
        cases=[{"id":str(i),"controls":[],"reference_plan":plan(),"supplied_data":{"value":" 42 "}} for i in range(2)]
        selector=Mock()
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService",return_value=selector), patch.object(adapter,"_run",return_value={"status":"canceled"}) as run:
            result=adapter._evaluate({"cases":cases,"model":"baseline","out":str(Path(folder)/"eval")},CONFIG,lambda _:None)
        self.assertEqual(result["status"],"canceled");self.assertEqual(result["total"],2)
        self.assertEqual((result["attempted"],result["unrun"]),(1,1));selector.close.assert_called_once()
        self.assertEqual(run.call_args.args[0]["supplied_data"],cases[0]["supplied_data"])

    def test_evaluation_exception_and_model_cleanup_remain_failures(self):
        selector=Mock();selector.close.side_effect=RuntimeError("worker did not exit")
        with tempfile.TemporaryDirectory() as folder, patch("locua.engine.prototype.decision.ModelService",return_value=selector), patch.object(adapter,"_run",side_effect=ValueError("bad case")):
            result=adapter._evaluate({"cases":[{"id":"one","controls":[],"reference_plan":plan()}],
                                     "model":"baseline","out":str(Path(folder)/"eval")},CONFIG,lambda _:None)
        self.assertEqual(result["status"],"cleanup_failed");self.assertEqual(result["status_before_cleanup_failure"],"blocked")
        self.assertIn("bad case",result["reason"]);self.assertEqual(result["unrun"],1)


if __name__=="__main__":unittest.main()
