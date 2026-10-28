"""OS-owned, transferable leases shared by the web, scheduler and CLI."""
from contextlib import contextmanager
from functools import wraps
import errno
import os
import threading
import time

from src.app_paths import data_dir
from src.test_isolation import assert_test_path

_local = threading.local()
_mutex = threading.Lock()
_held = set()


class TaskBusyError(RuntimeError):
    pass


class Lease:
    def __init__(self, name, stream, path):
        self.name, self.stream, self.path = name, stream, path

    def close(self):
        if self.stream is None:
            return
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
        self.stream = None
        with _mutex:
            _held.discard(self.path)


def claim(name="execution"):
    path = data_dir() / "task-locks" / (name + ".lock")
    assert_test_path(path)
    path = path.resolve()
    with _mutex:
        if path in _held:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            return None
        _held.add(path)
        return Lease(name, stream, path)


@contextmanager
def activate(lease):
    previous = getattr(_local, "lease", None)
    _local.lease = lease
    try:
        yield
    finally:
        _local.lease = previous


@contextmanager
def execution_lock():
    if getattr(_local, "lease", None) is not None:
        yield
        return
    lease = claim()
    if lease is None:
        raise TaskBusyError("已有任务正在运行（网页、命令行或定时任务），请等待结束")
    try:
        with activate(lease):
            yield
    finally:
        lease.close()


def serialized_task(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with execution_lock():
            return fn(*args, **kwargs)
    return wrapped


@contextmanager
def waiting_lock(name):
    lease = claim(name)
    while lease is None:
        time.sleep(0.02)
        lease = claim(name)
    try:
        yield
    finally:
        lease.close()
