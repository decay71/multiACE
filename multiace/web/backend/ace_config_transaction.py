"""Cooperative MultiACE transactions. PAXX/external writers are NOT covered."""
import os, threading, time, errno, tempfile, stat
from contextlib import contextmanager

class ConfigBusy(OSError):
    """Another cooperating writer owns the configuration; retry after reloading."""

_GUARD = threading.Lock()
_LOCKS = {}
_LOCAL = threading.local()

@contextmanager
def ace_config_lock(path, timeout=5.0):
    if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
        raise ValueError('Configuration lock timeout must be between 0 and 30 seconds')
    key = os.path.realpath(path)
    deadline = time.monotonic() + timeout
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise ConfigBusy('Configuration is busy; reload and retry the save')
    depths = getattr(_LOCAL, 'depths', None)
    if depths is None: depths = _LOCAL.depths = {}
    fd = None
    acquired = False
    try:
        if depths.get(key, 0):
            depths[key] += 1
            try: yield
            finally: depths[key] -= 1
            return
        fd = os.open(key + '.write.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if os.fstat(fd).st_size == 0: os.write(fd, b'0')
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ConfigBusy('Configuration is busy; reload and retry the save') from exc
                time.sleep(min(.02, remaining))
        depths[key] = 1
        try: yield
        finally: del depths[key]
    finally:
        try:
            if fd is not None:
                try:
                    if acquired and os.name == 'nt':
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                finally: os.close(fd)
        finally: lock.release()

# @MULTIACE_FLEXI_SHARED atomic config writes keep owner/mode (fixes root:root ace.cfg halt), all modes
def _copy_owner(temp, ref):
    """Give `temp` the uid/gid of `ref` (needs root; an unprivileged
    writer already owns what it creates)."""
    chown = getattr(os, 'chown', None)
    geteuid = getattr(os, 'geteuid', None)
    if geteuid is not None and geteuid() != 0: return
    if chown is None: return
    try: chown(temp, ref.st_uid, ref.st_gid)
    except PermissionError: pass

def atomic_text_replace(path, text):
    """Unique sibling, fsync and replace. Caller owns the configuration lock."""
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=parent)
    try:
        # Readable by Klipper and owned like the folder. The web backend may
        # run as root: a root-owned 0600 ace.cfg cannot be read by Klipper
        # (user lava) and halts it with 'Unable to open config file' (live
        # 2026-11-17). The target's own mode is kept, plus read for all, so a
        # file that was already broken that way is repaired by the next save.
        ref = os.stat(path) if os.path.exists(path) else None
        os.chmod(temp, (stat.S_IMODE(ref.st_mode) if ref is not None else 0o644) | 0o444)
        _copy_owner(temp, os.stat(parent))
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(text.encode('utf-8')); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if fd is not None: os.close(fd)
        if os.path.exists(temp): os.unlink(temp)
