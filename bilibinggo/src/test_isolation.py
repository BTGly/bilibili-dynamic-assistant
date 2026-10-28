"""Reject test writes outside an explicitly isolated temporary data root."""
from pathlib import Path
import os
import sys
import tempfile


def under_test_isolation() -> bool:
    """当前是不是跑在测试/E2E 语境下（决定了"防误操作"的闸门要不要生效）。"""
    root = os.environ.get("BINGGO_TEST_DATA_ROOT")
    return bool(root or "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("BINGGO_E2E") == "1")


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 里面。Windows 上不能直接信 resolve()+is_relative_to：
    `resolve()` 对"文件还没创建"和"已存在"的两条路径可能给出不同形态（8.3 短名、大小写），
    于是明明在隔离目录内的锁文件会被误判成越界（10-29 03:4x 全量跑时稳定复现）。
    所以解析前后各比一次，并且大小写无关。"""
    for a, b in ((child.resolve(), parent.resolve()), (child, parent)):
        left = os.path.normcase(str(a)).rstrip("\\/")
        right = os.path.normcase(str(b)).rstrip("\\/")
        if left == right or left.startswith(right + os.sep) or left.startswith(right + "/"):
            return True
    return False


def assert_test_path(path: Path) -> None:
    if not under_test_isolation():
        return
    root = os.environ.get("BINGGO_TEST_DATA_ROOT")
    if not root:
        raise RuntimeError("测试未配置隔离数据目录，禁止读写用户数据")
    isolated = Path(root).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if isolated == temporary or not _is_within(isolated, temporary):
        raise RuntimeError("测试隔离根必须是临时目录内的专用子目录")
    if not _is_within(Path(path), isolated):
        raise RuntimeError("测试路径越过隔离目录，禁止读写：" + str(path))
