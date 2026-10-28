"""Reject test writes outside an explicitly isolated temporary data root."""
from pathlib import Path
import os
import sys
import tempfile


def assert_test_path(path: Path) -> None:
    root = os.environ.get("BINGGO_TEST_DATA_ROOT")
    testing = bool(root or "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("BINGGO_E2E") == "1")
    if not testing:
        return
    if not root:
        raise RuntimeError("测试未配置隔离数据目录，禁止读写用户数据")
    isolated = Path(root).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if isolated == temporary or not isolated.is_relative_to(temporary):
        raise RuntimeError("测试隔离根必须是临时目录内的专用子目录")
    if not Path(path).resolve().is_relative_to(isolated):
        raise RuntimeError("测试路径越过隔离目录，禁止读写：" + str(path))
