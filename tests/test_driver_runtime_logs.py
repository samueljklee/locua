"""Private launch diagnostics only; all launch/process/socket APIs mocked."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from locua.engine import driver_runtime as module


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name).resolve()
        app=self.folder/'LocuaDriver.app'
        with patch.object(module.sys,'platform','darwin'):
            self.runtime=module.Runtime({'driver_app':str(app),'driver_binary':str(app/'Contents/MacOS/locua-driver'),
                                        'driver_socket':str(self.folder/'driver.sock')})
        self.identity={'device':1,'inode':2,'uid':os.getuid()}
    def tearDown(self):self.temp.cleanup()
    def pending(self):
        record={'schema':'locua.runtime_launch.v1','app':str(self.runtime.app),'socket':str(self.runtime.socket),
                'launched_at_ns':123,**self.runtime._create_launch_diagnostics(123)}
        module._write_json(self.runtime.pending,record)
        return record
    def adopt(self):
        record=self.pending()
        with patch.object(self.runtime,'_socket_identity',return_value=self.identity):
            self.runtime._adopt_pending({'pid':73})
        return record

    def test_launch_has_exclusive_private_files_and_options_before_args(self):
        def launch(argv,**kwargs):
            pending=json.loads(self.runtime.pending.read_text())
            self.assertEqual(self.runtime.pending.stat().st_mode & 0o777,0o600)
            self.assertNotIn('-n',argv)
            for channel in ('stdout','stderr'):
                i=argv.index('--'+channel)
                self.assertLess(i,argv.index('--args'))
                self.assertEqual(argv[i+1],pending['diagnostic_paths'][channel])
                path=Path(argv[i+1])
                self.assertEqual(path.parent,self.runtime.ledger.parent)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(path.read_bytes(),b'')
            self.assertEqual(argv[argv.index('--permission-mode')+1],'standard')
            raise module.SetupError('mock launch failure')
        with patch.object(self.runtime,'doctor'),patch.object(self.runtime,'_matching_processes',return_value=[]), \
             patch.object(module,'_run',side_effect=launch):
            with self.assertRaisesRegex(module.SetupError,'mock launch failure'):self.runtime.start()
        self.assertEqual(self.runtime._diagnostics()['diagnostic_status'],'available')

    def test_pending_paths_and_identities_propagate_to_owner(self):
        original=self.adopt();owner=json.loads(self.runtime.ledger.read_text())
        self.assertFalse(self.runtime.pending.exists())
        for key in ('diagnostic_paths','diagnostic_file_identities'):
            self.assertEqual(owner[key],original[key])
        self.assertEqual(self.runtime._diagnostics({'pid':73},self.identity)['diagnostic_paths'],original['diagnostic_paths'])
        self.assertFalse(self.runtime._diagnostics({'pid':73},self.identity)['diagnostic_permission_authority'])

    def test_status_exposes_paths_without_promoting_permissions(self):
        original=self.adopt();self.runtime.socket.write_text('mock')
        pending_reply={'ok':False,'error':'permissions_pending','exit_code':75}
        with patch.object(self.runtime,'doctor',return_value={'status':'package_ready'}), \
             patch.object(self.runtime,'_owned_metadata',return_value={'pid':73}), \
             patch.object(self.runtime,'_socket_identity',return_value=self.identity), \
             patch.object(self.runtime,'_request',return_value=pending_reply) as request:
            status=self.runtime.status()
        request.assert_called_once_with('call',name='check_permissions',args={'prompt':False})
        self.assertEqual(status['permissions'],pending_reply)
        self.assertEqual(status['diagnostic_paths'],original['diagnostic_paths'])

    def test_stale_ledger_cannot_attach_logs_to_another_process_or_socket(self):
        self.adopt()
        for metadata,identity in (({'pid':74},self.identity),({'pid':73},{**self.identity,'inode':3})):
            result=self.runtime._diagnostics(metadata,identity)
            self.assertEqual(result['diagnostic_paths'],{})
            self.assertEqual(result['diagnostic_status'],'ownership_record_mismatch')
        self.assertEqual(self.runtime._diagnostics()['diagnostic_paths'],{})

    def test_changed_files_symlinks_and_nonprivate_files_are_not_exposed(self):
        for mutation in ('replace','symlink','mode'):
            with self.subTest(mutation=mutation):
                if self.runtime.pending.exists():self.runtime.pending.unlink()
                record=self.pending();path=Path(record['diagnostic_paths']['stdout'])
                if mutation=='mode':path.chmod(0o644)
                else:
                    moved=path.with_suffix('.previous');path.rename(moved)
                    if mutation=='replace':path.write_text('replacement');path.chmod(0o600)
                    else:path.symlink_to(moved)
                result=self.runtime._diagnostics()
                self.assertEqual(result['diagnostic_paths'],{})
                self.assertEqual(result['diagnostic_status'],'unavailable')

    def test_exclusive_creation_cannot_overwrite_existing_log(self):
        with patch.object(module.uuid,'uuid4') as uid:
            uid.return_value.hex='a'*32
            first=self.runtime._create_launch_diagnostics(123)
            path=Path(first['diagnostic_paths']['stdout']);path.write_text('kept')
            with self.assertRaises(FileExistsError):self.runtime._create_launch_diagnostics(123)
            self.assertEqual(path.read_text(),'kept')

    def test_legacy_pending_and_owner_without_diagnostics_still_work(self):
        self.runtime.pending.write_text(json.dumps({'schema':'locua.runtime_launch.v1','app':str(self.runtime.app),
            'socket':str(self.runtime.socket),'launched_at_ns':1}))
        self.assertEqual(self.runtime._diagnostics()['diagnostic_status'],'not_recorded')
        with patch.object(self.runtime,'_socket_identity',return_value=self.identity):self.runtime._adopt_pending({'pid':73})
        self.assertEqual(self.runtime._diagnostics({'pid':73},self.identity)['diagnostic_status'],'not_recorded')


if __name__=='__main__':unittest.main()
