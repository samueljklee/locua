"""Explicit, portable ownership-checked Locua Driver lifecycle on macOS.

No build/developer checkout dependencies. Only start() launches the distinct app
and its normal permission onboarding. doctor/status never request grants. stop
requires a fresh matching PID, executable, socket inode and durable launch ledger.
Ad-hoc signing does not guarantee grant persistence across future rebuilds.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
BUNDLE_ID = 'local.locua.driver'
ENV = {'CUA_DRIVER_RS_TELEMETRY_ENABLED':'false','CUA_DRIVER_RS_UPDATE_CHECK':'false'}
class SetupError(RuntimeError):
    pass


def _run(args, *, timeout=15, cwd=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env={**os.environ, **ENV}, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise SetupError(f'{args[0]} exited {result.returncode}: {(result.stderr or result.stdout)[-3000:]}')
    return result

def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def _write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def _pid_executable(pid):
    if type(pid) is not int or pid <= 0:
        raise SetupError('Invalid daemon PID')
    proc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    count = proc.proc_pidpath(pid, buffer, len(buffer))
    if count > 0:
        return Path(os.fsdecode(buffer.value)).resolve()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise SetupError('Cannot prove daemon process identity or absence') from exc
    raise SetupError('Process exists but its executable identity is unavailable')

class Runtime:
    """Bind lifecycle operations to three explicit absolute configuration paths."""
    def __init__(self, config):
        if sys.platform != 'darwin':
            raise SetupError('Locua Driver app lifecycle currently supports macOS only')
        for key in ('driver_app', 'driver_binary', 'driver_socket'):
            if not isinstance(config.get(key), str) or not config[key] or not Path(config[key]).is_absolute():
                raise SetupError(key + ' must be an explicit absolute local path')
        self.app = Path(config['driver_app'])
        self.executable = Path(config['driver_binary'])
        self.socket = Path(config['driver_socket'])
        if self.executable != self.app / 'Contents/MacOS/locua-driver':
            raise SetupError('driver_binary must be the configured Locua app executable')
        self.manifest = self.app.parent / 'manifest.json'
        self.ledger = self.app.parent / 'owner.json'
        self.pending = self.app.parent / 'launch-pending.json'
    def _manifest(self):
        try:
            value = json.loads(self.manifest.read_text())
        except (OSError, ValueError) as exc:
            raise SetupError('Configure a packaged Locua Driver app and its adjacent manifest.json') from exc
        if value.get('schema') != 'locua.runtime.v1' or value.get('bundle_id') != BUNDLE_ID or value.get('app') != str(self.app) or (value.get('executable') != str(self.executable)) or (value.get('socket') != str(self.socket)):
            raise SetupError('Runtime manifest does not describe this exact Locua package')
        return value

    def _matching_processes(self):
        result = subprocess.run(['/usr/bin/pgrep', '-x', 'locua-driver'], text=True, capture_output=True, timeout=3)
        if result.returncode not in (0, 1):
            raise SetupError('Cannot establish whether the exact runtime is already running')
        pids = [int(line) for line in result.stdout.splitlines()]
        if len(pids) > 64:
            raise SetupError('Too many same-name processes to establish exact runtime ownership')
        return [pid for pid in pids if _pid_executable(pid) == self.executable.resolve()]

    def _adopt_pending(self, metadata):
        if not self.pending.exists():
            return
        pending = json.loads(self.pending.read_text())
        if pending.get('schema') != 'locua.runtime_launch.v1' or pending.get('app') != str(self.app) or pending.get('socket') != str(self.socket):
            raise SetupError('Pending launch identity differs from configured runtime')
        diagnostics = {key: pending[key] for key in ('diagnostic_paths', 'diagnostic_file_identities') if key in pending}
        _write_json(self.ledger, {'schema': 'locua.runtime_owner.v1', 'app': str(self.app), 'pid': metadata['pid'], 'socket': str(self.socket), 'socket_identity': self._socket_identity(), 'launched_at_ns': pending['launched_at_ns'], **diagnostics})
        self.pending.unlink()

    def _create_launch_diagnostics(self, launched_at):
        """Create private files before LaunchServices opens them; never reuse a log."""
        prefix = f'locua-launch-{launched_at}-{uuid.uuid4().hex}'
        paths, identities = {}, {}
        for channel in ('stdout', 'stderr'):
            path = self.ledger.parent / f'{prefix}.{channel}.log'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                paths[channel] = str(path)
                identities[channel] = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}
            finally:
                os.close(fd)
        return {'diagnostic_paths': paths, 'diagnostic_file_identities': identities}

    def _diagnostics(self, metadata=None, socket_identity=None):
        """Report log locations only. Logs never establish grants or ownership."""
        result = {'diagnostic_paths': {}, 'diagnostic_status': 'not_recorded',
                  'diagnostic_permission_authority': False}
        source = self.pending if metadata is None else self.ledger
        if not source.exists():
            return result
        try:
            record = json.loads(source.read_text())
            schema = 'locua.runtime_launch.v1' if metadata is None else 'locua.runtime_owner.v1'
            if (record.get('schema') != schema or record.get('app') != str(self.app)
                    or record.get('socket') != str(self.socket)
                    or (metadata is not None and (record.get('pid') != metadata['pid']
                        or record.get('socket_identity') != socket_identity))):
                return {**result, 'diagnostic_status': 'ownership_record_mismatch'}
            if 'diagnostic_paths' not in record:
                return result  # Compatible with launch/owner records from before logging.
            paths, identities = record['diagnostic_paths'], record.get('diagnostic_file_identities')
            if not isinstance(paths, dict) or set(paths) != {'stdout', 'stderr'} or not isinstance(identities, dict):
                raise ValueError('Malformed diagnostic record')
            prefixes = []
            for channel in ('stdout', 'stderr'):
                if not isinstance(paths[channel], str):
                    raise ValueError('Malformed diagnostic path')
                path = Path(paths[channel])
                match = re.fullmatch(r'locua-launch-(\d+)-([0-9a-f]{32})\.' + channel + r'\.log', path.name)
                if (not path.is_absolute() or path.parent != self.ledger.parent or not match
                        or int(match.group(1)) != record.get('launched_at_ns')):
                    raise ValueError('Diagnostic path is outside this launch')
                prefixes.append(match.group(2))
                info = path.lstat()
                identity = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or identities.get(channel) != identity):
                    raise ValueError('Diagnostic file identity or privacy changed')
            if len(set(prefixes)) != 1:
                raise ValueError('Diagnostic files belong to different launches')
            return {**result, 'diagnostic_paths': dict(paths), 'diagnostic_status': 'available'}
        except (OSError, ValueError, TypeError, KeyError):
            return {**result, 'diagnostic_status': 'unavailable'}

    def _socket_identity(self):
        info = self.socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError('Locua endpoint is not a socket owned by this user')
        return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}

    def _request(self, method, *, args=None, name=None):
        before = self._socket_identity()
        request = {'method': method}
        if args is not None:
            request['args'] = args
        if name is not None:
            request['name'] = name
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(2)
            channel.connect(str(self.socket))
            channel.sendall(json.dumps(request).encode() + b'\n')
            chunks = bytearray()
            deadline = time.monotonic() + 2
            while b'\n' not in chunks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SetupError('Locua diagnostic response deadline expired')
                channel.settimeout(remaining)
                data = channel.recv(65536)
                if not data:
                    raise SetupError('Locua endpoint closed without a response')
                chunks.extend(data)
                if len(chunks) > 1024 * 1024:
                    raise SetupError('Locua diagnostic response exceeded its bound')
        if self.socket.exists() and self._socket_identity() != before:
            raise SetupError('Locua endpoint changed during the request')
        value = json.loads(bytes(chunks).split(b'\n', 1)[0])
        if not isinstance(value, dict):
            raise SetupError('Locua endpoint returned a malformed response')
        return value

    def _owned_metadata(self):
        answer = self._request('metadata')
        metadata = answer.get('result')
        if answer.get('ok') is not True or not isinstance(metadata, dict):
            raise SetupError('Locua endpoint did not provide daemon metadata')
        if _pid_executable(metadata.get('pid')) != self.executable.resolve():
            raise SetupError('Endpoint PID is not executing this exact Locua bundle')
        if metadata.get('embedded') is not False:
            raise SetupError('Expected an independently launched Locua app, not an embedded host')
        return metadata

    def doctor(self):
        m = self._manifest()
        if self.executable.is_symlink() or self.app.is_symlink():
            raise SetupError('Runtime app/executable must not be a symlink')
        if _sha(self.executable) != m['signed_executable_sha256']:
            raise SetupError('Packaged executable hash differs from manifest')
        with (self.app / 'Contents/Info.plist').open('rb') as stream:
            plist = plistlib.load(stream)
        if plist.get('CFBundleIdentifier') != BUNDLE_ID or plist.get('CFBundleExecutable') != 'locua-driver':
            raise SetupError('App bundle identity differs from Locua manifest')
        _run(['/usr/bin/codesign', '--verify', '--strict', self.app])
        return {'status': 'package_ready', 'app': str(self.app), 'bundle_id': BUNDLE_ID, 'signature': m['signing'], 'permission_status': 'unknown_until_own_daemon_reports', 'permission_prompted': False, 'socket': str(self.socket), 'browser_source_fingerprint_sha256': m['browser_source_fingerprint_sha256'], 'native_source_fingerprint_sha256': m.get('native_source_fingerprint_sha256'), 'signed_executable_sha256': m['signed_executable_sha256']}

    def status(self):
        report = self.doctor()
        if not self.socket.exists():
            return {**report, 'runtime': 'launch_pending' if self.pending.exists() else 'stopped', 'permissions': None,
                    **self._diagnostics()}
        metadata = self._owned_metadata()
        self._adopt_pending(metadata)
        permission_reply = self._request('call', name='check_permissions', args={'prompt': False})
        socket_identity = self._socket_identity()
        return {**report, 'runtime': 'running', 'daemon': metadata, 'socket_identity': socket_identity,
                'permissions': permission_reply, **self._diagnostics(metadata, socket_identity)}

    def start(self):
        self.doctor()
        if self.socket.exists():
            return {**self.status(), 'started': False}
        if self.pending.exists():
            pending = json.loads(self.pending.read_text())
            if pending.get('schema') != 'locua.runtime_launch.v1' or pending.get('app') != str(self.app) or pending.get('socket') != str(self.socket):
                raise SetupError('Unrecognized pending launch; refusing another instance')
            if time.time_ns() - pending['launched_at_ns'] < 30000000000 or self._matching_processes():
                raise SetupError('Own launch is still pending; finish normal permission onboarding and use status')
            self.pending.unlink()
        if self._matching_processes():
            raise SetupError('Exact runtime app already exists without a verified endpoint; refusing duplicate launch')
        launched_at = time.time_ns()
        diagnostics = self._create_launch_diagnostics(launched_at)
        with self.pending.open('x') as stream:
            os.chmod(self.pending, 384)
            json.dump({'schema': 'locua.runtime_launch.v1', 'app': str(self.app), 'socket': str(self.socket), 'launched_at_ns': launched_at, **diagnostics}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _run(['/usr/bin/open', '-g', '-a', self.app,
              '--stdout', diagnostics['diagnostic_paths']['stdout'],
              '--stderr', diagnostics['diagnostic_paths']['stderr'],
              '--args', 'serve', '--socket', self.socket, '--permission-mode', 'standard', '--no-overlay'], timeout=10)
        deadline = time.monotonic() + 12
        last_error = None
        while time.monotonic() < deadline:
            try:
                metadata = self._owned_metadata()
                self._adopt_pending(metadata)
                return {**self.status(), 'started': True, 'onboarding_may_be_pending': True}
            except (OSError, ValueError, SetupError) as exc:
                last_error = str(exc)
                time.sleep(0.2)
        raise SetupError(f'Own daemon readiness is unproved; launch intent retained, do not relaunch: {last_error}')

    def stop(self):
        self.doctor()
        if not self.socket.exists():
            if self.pending.exists() or self._matching_processes():
                raise SetupError('No own endpoint, but launch/process may exist; shutdown cannot be verified')
            return {'status': 'already_stopped', 'socket': str(self.socket)}
        self._adopt_pending(self._owned_metadata())
        try:
            owner = json.loads(self.ledger.read_text())
        except (OSError, ValueError) as exc:
            raise SetupError('No launch ownership record; refusing to stop an unowned runtime') from exc
        metadata = self._owned_metadata()
        if owner.get('schema') != 'locua.runtime_owner.v1' or owner.get('app') != str(self.app) or owner.get('socket') != str(self.socket) or (owner.get('pid') != metadata['pid']) or (owner.get('socket_identity') != self._socket_identity()):
            raise SetupError('Fresh runtime identity disagrees with launch ownership; refusing shutdown')
        reply = self._request('shutdown_if_pid', args={'expected_pid': metadata['pid']})
        if reply.get('ok') is not True:
            raise SetupError('Owned daemon refused PID-bound shutdown')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if not self.socket.exists() and _pid_executable(metadata['pid']) is None:
                self.ledger.unlink(missing_ok=True)
                return {'status': 'stopped', 'pid': metadata['pid'], 'socket_absent': True}
            time.sleep(0.1)
        raise SetupError('Shutdown requested, but exact process/socket termination is not yet verified')

def doctor(config):
    return Runtime(config).doctor()

def status(config):
    return Runtime(config).status()

def start(config):
    return Runtime(config).start()

def stop(config):
    return Runtime(config).stop()
