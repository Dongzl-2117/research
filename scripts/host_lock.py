"""宿主机任务锁：macOS / Linux 的 Python 标准库；锁文件永不删除。"""

import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def host_lock(repo: Path, *, exclusive: bool = False):
    path = repo / "data/status/tasks.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
