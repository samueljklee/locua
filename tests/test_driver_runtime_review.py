"""Pure ownership/lifecycle regressions. All process/socket/launch calls mocked."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from locua.engine import driver_runtime as module


class DriverRuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name).resolve()
        app=self.folder/"LocuaDriver.app"
        with patch.object(module.sys,"platform","darwin"):
            self.runtime=module.Runtime({"driver_app":str(app),"driver_binary":str(app/"Contents/MacOS/locua-driver"),"driver_socket":str(self.folder/"locua.sock")})
        self.identity={"device":1,"inode":2,"uid":3}
    def tearDown(self):self.temp.cleanup()
    def pending(self,age=0):
        self.runtime.pending.write_text(json.dumps({"schema":"locua.runtime_launch.v1","app":str(self.runtime.app),
            "socket":str(self.runtime.socket),"launched_at_ns":time.time_ns()-int(age*1e9)}))
    def ledger(self):
        self.runtime.ledger.write_text(json.dumps({"schema":"locua.runtime_owner.v1","app":str(self.runtime.app),
            "socket":str(self.runtime.socket),"pid":73,"socket_identity":self.identity,"launched_at_ns":1}))
    def test_recent_pending_never_relaunches(self):
        self.pending()
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_matching_processes",return_value=[]),patch.object(module,"_run") as run:
            with self.assertRaisesRegex(module.SetupError,"still pending"):self.runtime.start()
        run.assert_not_called();self.assertTrue(self.runtime.pending.exists())
    def test_older_pending_with_owned_process_never_relaunches(self):
        self.pending(age=60)
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_matching_processes",return_value=[73]),patch.object(module,"_run") as run:
            with self.assertRaisesRegex(module.SetupError,"still pending"):self.runtime.start()
        run.assert_not_called()
    def test_launch_failure_retains_durable_pending_without_duplicate_option(self):
        def failure(argv,**kwargs):
            self.assertTrue(self.runtime.pending.exists());self.assertNotIn("-n",argv)
            self.assertEqual(json.loads(self.runtime.pending.read_text())["schema"],"locua.runtime_launch.v1")
            raise module.SetupError("launch refused")
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_matching_processes",return_value=[]),patch.object(module,"_run",side_effect=failure):
            with self.assertRaisesRegex(module.SetupError,"launch refused"):self.runtime.start()
        self.assertTrue(self.runtime.pending.exists())
    def test_stop_refuses_socket_identity_change(self):
        self.runtime.socket.write_text("mock endpoint");self.ledger()
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_owned_metadata",return_value={"pid":73}),patch.object(
            self.runtime,"_socket_identity",return_value={**self.identity,"inode":999}),patch.object(self.runtime,"_request") as request:
            with self.assertRaisesRegex(module.SetupError,"identity disagrees"):self.runtime.stop()
        request.assert_not_called()
    def test_owned_shutdown_is_pid_bound_and_requires_absence(self):
        self.runtime.socket.write_text("mock endpoint");self.ledger()
        def shutdown(method,**kwargs):
            self.assertEqual((method,kwargs),("shutdown_if_pid",{"args":{"expected_pid":73}}))
            self.runtime.socket.unlink();return {"ok":True}
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_owned_metadata",return_value={"pid":73}),patch.object(
            self.runtime,"_socket_identity",return_value=self.identity),patch.object(self.runtime,"_request",side_effect=shutdown),patch.object(module,"_pid_executable",return_value=None):
            result=self.runtime.stop()
        self.assertEqual(result["status"],"stopped");self.assertFalse(self.runtime.ledger.exists())
    def test_absent_endpoint_with_pending_launch_is_not_already_stopped(self):
        self.pending()
        with patch.object(self.runtime,"doctor"),patch.object(self.runtime,"_matching_processes",return_value=[]):
            with self.assertRaisesRegex(module.SetupError,"shutdown cannot be verified"):self.runtime.stop()


if __name__=="__main__":unittest.main()
