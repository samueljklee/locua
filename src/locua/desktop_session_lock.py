"""Per-user desktop exclusion across Locua processes; no PID killing or polling.

The stable lock file is never unlinked. The kernel releases its flock when the
last owning descriptor closes, including process crashes. Metadata is diagnostic
only; it is never used to decide that a lock may be stolen. macOS/Linux only.
"""
from contextvars import ContextVar
import errno
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import time

from .errors import LocuaError

_CURRENT=ContextVar('locua_desktop_session_token',default=None)
_GUARD=threading.RLock()
_HELD={}


def current_desktop_session_token():
    return _CURRENT.get()


def default_lock_path():
    # Deliberately independent of LOCUA_CONFIG and selected model/config files.
    if sys.platform=='darwin':root=Path.home()/'Library/Application Support/locua'
    else:root=Path.home()/'.local/state/locua'
    return root/'locks/desktop-control.lock'


def _busy(path,holder):
    return LocuaError('desktop_control_busy','Another Locua session owns desktop control.',
        'Wait for that session to finish or cancel it normally, then retry. Locua will not interrupt or kill its owner.',
        details={'lock_path':str(path),'holder':holder,'lock_authority':'OS advisory lock; metadata is diagnostic only'})


def _after_fork_child():
    global _GUARD
    # Never LOCK_UN an inherited description: that would release the parent.
    for entry in _HELD.values():
        try:os.close(entry['fd'])
        except OSError:pass
    _HELD.clear();_GUARD=threading.RLock();_CURRENT.set(None)


if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_child=_after_fork_child)


class DesktopSessionLease:
    def __init__(self,path,token,pid):
        self.path=path;self.token=token;self.pid=pid;self.closed=False;self._context=None

    def __enter__(self):
        if self.closed or self.pid!=os.getpid():raise RuntimeError('Desktop lease is closed or belongs to parent process')
        if self._context is not None:raise RuntimeError('Desktop lease context already entered')
        self._context=_CURRENT.set(self.token)
        return self

    def close(self):
        if self.closed:return
        if self.pid!=os.getpid():
            self.closed=True;self._context=None;return
        if self._context is not None:
            _CURRENT.reset(self._context);self._context=None
        self.closed=True
        with _GUARD:
            entry=_HELD.get(self.path)
            if entry is None or entry['token']!=self.token:return
            entry['references']-=1
            if entry['references']==0:
                _HELD.pop(self.path)
                # Closing the final descriptor releases flock; never unlink.
                os.close(entry['fd'])

    def __exit__(self,*_):self.close()


def acquire_desktop_session(*,purpose='desktop operation',token=None,path=None):
    """Acquire immediately or raise desktop_control_busy, without waiting.

    ``path`` is for isolated tests; production uses the single per-user default.
    Nested acquisition must pass the explicit current token. Merely running in
    the same process is insufficient. Only entering the outer lease context sets
    the ContextVar; a standalone Cua owner does not bless independent owners.
    """
    if not isinstance(purpose,str) or not purpose or len(purpose)>160:raise ValueError('Short nonempty lock purpose required')
    if token is not None and (not isinstance(token,str) or not token):raise ValueError('Invalid desktop session token')
    try:import fcntl
    except ImportError as error:
        raise LocuaError('desktop_lock_unsupported','OS advisory desktop lock is unavailable.',
                         'This desktop integration currently requires macOS or Linux.') from error
    selected=Path(path).expanduser().absolute() if path is not None else default_lock_path()
    selected.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    # Reject symlinked lock paths, non-private ownership and replacement routes.
    if selected.parent.is_symlink():raise ValueError('Desktop lock directory may not be a symlink')
    parent=selected.parent.stat()
    if parent.st_uid!=os.getuid() or parent.st_mode&0o022:raise ValueError('Desktop lock directory must be owned by this user and not writable by others')
    with _GUARD:
        held=_HELD.get(selected)
        if held is not None:
            if token!=held['token']:raise _busy(selected,held['metadata'])
            held['references']+=1
            return DesktopSessionLease(selected,token,os.getpid())
        # A supplied stale/foreign token never creates a new session silently.
        if token is not None:raise LocuaError('desktop_session_expired','The enclosing desktop session is no longer active.',
                                             'Start a new outer desktop session; do not reuse its old token.')
        fd=os.open(selected,os.O_RDWR|os.O_CREAT|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            os.set_inheritable(fd,False);info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1:
                raise ValueError('Desktop lock file must be a private owned regular file')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EAGAIN,errno.EACCES):raise
                try:holder=json.loads(os.pread(fd,4096,0))
                except (ValueError,OSError):holder={'metadata':'unavailable'}
                raise _busy(selected,holder) from error
            identity=secrets.token_hex(24)
            metadata={'pid':os.getpid(),'purpose':purpose,'acquired_at_ns':time.time_ns()}
            data=(json.dumps(metadata,separators=(',',':'))+'\n').encode()
            os.ftruncate(fd,0);os.pwrite(fd,data,0);os.fsync(fd)
            _HELD[selected]={'fd':fd,'token':identity,'references':1,'metadata':metadata}
            return DesktopSessionLease(selected,identity,os.getpid())
        except BaseException:
            os.close(fd);raise
