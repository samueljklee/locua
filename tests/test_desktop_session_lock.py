"""Real advisory-lock subprocess checks in disposable private directories."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

from locua.desktop_session_lock import acquire_desktop_session,current_desktop_session_token
from locua.errors import LocuaError

SCRIPT='''
import json,os,sys
from locua.desktop_session_lock import acquire_desktop_session
from locua.errors import LocuaError
sys.stdin.readline()
try:lease=acquire_desktop_session(path=sys.argv[1],purpose="test child")
except LocuaError as error:
 print(json.dumps({"status":"busy","code":error.code,"details":error.details}),flush=True);sys.exit(0)
print(json.dumps({"status":"acquired","pid":os.getpid()}),flush=True)
command=sys.stdin.readline().strip()
if command=="crash":os._exit(17)
lease.close()
'''

class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'private/desktop.lock';self.children=[]
        self.addCleanup(self.cleanup_children)
    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.stdin.write('release\n');child.stdin.flush();child.wait(timeout=5)
            child.stdin.close();child.stdout.close();child.stderr.close()
    def child(self):
        process=subprocess.Popen([sys.executable,'-c',SCRIPT,str(self.path)],stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(process);return process
    def start(self,child):child.stdin.write('go\n');child.stdin.flush()
    def read(self,child):
        import select
        ready,_,_=select.select([child.stdout],[],[],5)
        self.assertTrue(ready,'Lock child did not return promptly')
        line=child.stdout.readline()
        if not line:self.fail('Lock child ended without a status; exit='+str(child.poll()))
        return json.loads(line)
    def test_independent_same_process_owner_refused_nested_explicit_token_allowed(self):
        with acquire_desktop_session(path=self.path,purpose='outer') as outer:
            self.assertEqual(current_desktop_session_token(),outer.token)
            with self.assertRaises(LocuaError) as error:acquire_desktop_session(path=self.path,purpose='independent')
            self.assertEqual(error.exception.code,'desktop_control_busy')
            inner=acquire_desktop_session(path=self.path,purpose='nested',token=current_desktop_session_token())
            inner.close();self.assertEqual(current_desktop_session_token(),outer.token)
        self.assertIsNone(current_desktop_session_token())
        acquire_desktop_session(path=self.path,purpose='later').close()
    def test_standalone_owner_does_not_set_outer_context_or_bless_another_owner(self):
        lease=acquire_desktop_session(path=self.path);self.addCleanup(lease.close)
        self.assertIsNone(current_desktop_session_token())
        with self.assertRaises(LocuaError):acquire_desktop_session(path=self.path)
    def test_parent_release_waits_for_nested_reference(self):
        outer=acquire_desktop_session(path=self.path);inner=acquire_desktop_session(path=self.path,token=outer.token)
        outer.close()
        with self.assertRaises(LocuaError):acquire_desktop_session(path=self.path)
        inner.close();acquire_desktop_session(path=self.path).close()
    def test_cross_process_race_has_exactly_one_winner_and_descriptive_nonblocking_loser(self):
        first=self.child();second=self.child();self.start(first);self.start(second)
        outcomes=[self.read(first),self.read(second)]
        self.assertEqual(sorted(o['status'] for o in outcomes),['acquired','busy'])
        loser=next(o for o in outcomes if o['status']=='busy')
        self.assertEqual(loser['code'],'desktop_control_busy');self.assertIn('holder',loser['details'])
        winner=(first,second)[next(i for i,o in enumerate(outcomes) if o['status']=='acquired')]
        winner.stdin.write('release\n');winner.stdin.flush();winner.wait(timeout=5)
        acquire_desktop_session(path=self.path,purpose='after release').close()
    def test_process_crash_releases_kernel_lock_without_unlink_or_pid_killing(self):
        child=self.child();self.start(child);self.assertEqual(self.read(child)['status'],'acquired')
        inode=self.path.stat().st_ino;child.stdin.write('crash\n');child.stdin.flush()
        self.assertEqual(child.wait(timeout=5),17)
        with acquire_desktop_session(path=self.path,purpose='after crash'):
            self.assertEqual(self.path.stat().st_ino,inode)
    def test_stale_foreign_token_and_symlink_refused(self):
        with self.assertRaises(LocuaError) as error:acquire_desktop_session(path=self.path,token='foreign')
        self.assertEqual(error.exception.code,'desktop_session_expired')
        real=self.path.parent/'other';real.write_text('unchanged');self.path.symlink_to(real)
        with self.assertRaises(OSError):acquire_desktop_session(path=self.path)
        self.assertEqual(real.read_text(),'unchanged')
    @unittest.skipUnless(hasattr(os,'fork'),'requires fork')
    def test_fork_child_does_not_inherit_authority_or_unlock_parent(self):
        lease=acquire_desktop_session(path=self.path);lease.__enter__();readfd,writefd=os.pipe()
        pid=os.fork()
        if pid==0:
            os.close(readfd)
            try:
                try:acquire_desktop_session(path=self.path);result=b'unexpected'
                except LocuaError as error:result=error.code.encode()
                lease.close();os.write(writefd,result)
            finally:os._exit(0)
        os.close(writefd)
        try:self.assertEqual(os.read(readfd,100),b'desktop_control_busy')
        finally:os.close(readfd);os.waitpid(pid,0)
        child=self.child();self.start(child);self.assertEqual(self.read(child)['status'],'busy')
        lease.close();acquire_desktop_session(path=self.path).close()
