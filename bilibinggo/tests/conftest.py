from __future__ import annotations

import sys
import os
import tempfile

_TEST_HOME = tempfile.TemporaryDirectory(prefix="binggo-tests-", ignore_cleanup_errors=True)
os.environ["BINGGO_HOME"] = _TEST_HOME.name
os.environ["BINGGO_TEST_DATA_ROOT"] = _TEST_HOME.name


def _release_and_cleanup() -> None:
    # SQLite/WAL 句柄未释放时 TemporaryDirectory 的清理会被 ignore_cleanup_errors 静默吞掉，
    # 于是 %TEMP% 里每次跑测试留一个 binggo-tests-* 目录。先丢掉引擎再删，并给 Windows
    # 的文件解锁留一点重试余量。
    try:
        from src.db.engine import reset_engine_for_tests

        reset_engine_for_tests()
    except Exception:
        pass
    import shutil
    import time

    for _ in range(3):
        try:
            shutil.rmtree(_TEST_HOME.name, ignore_errors=False)
            break
        except OSError:
            time.sleep(0.3)
    try:
        _TEST_HOME.cleanup()
    except OSError:
        pass


def _discard_stale_homes(keep: str) -> None:
    # 清掉更早会话残留的隔离目录；只认我们自己的前缀，且只碰 1 天前的。
    import shutil
    import time
    from pathlib import Path

    cutoff = time.time() - 86400
    for stale in Path(tempfile.gettempdir()).glob("binggo-tests-*"):
        try:
            if str(stale.resolve()) == keep or stale.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue


def pytest_sessionfinish(session, exitstatus) -> None:
    # 必须用会话钩子而不是 atexit：解释器退出时 import 已不可用，清理会静默失败。
    _release_and_cleanup()


_discard_stale_homes(_TEST_HOME.name)
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离用户数据目录与 SQLite 引擎，供依赖 DB 的测试使用。"""
    monkeypatch.setenv("BINGGO_HOME", str(tmp_path))
    monkeypatch.setenv("BINGGO_TEST_DATA_ROOT", str(tmp_path))
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "output").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("src.app_paths.USER_HOME", tmp_path)
    monkeypatch.setattr("src.app_paths.DATA_DIR", data_dir)
    monkeypatch.setattr("src.app_paths.CONFIG_DIR", config_dir)
    monkeypatch.setattr("src.app_paths.user_home", lambda: tmp_path)
    # 已建空库，跳过 ensure_user_dirs 的种子 bootstrap，避免污染隔离用例
    monkeypatch.setattr("src.app_paths._SEEDED", True)
    monkeypatch.setattr("src.app_paths._BOOTSTRAPPED", True)

    # 常见 Store 模块级 DATA_DIR 副本
    for mod in (
        "src.app_logging",
        "src.state_store",
        "src.activity_store",
        "src.user_data",
        "src.source_outputs",
        "src.watch_sync",
        "web.activity_service",
    ):
        try:
            monkeypatch.setattr(f"{mod}.DATA_DIR", data_dir, raising=False)
        except Exception:
            pass

    from src.db.engine import db_path, reset_engine_for_tests
    from src.db.schema import init_db

    reset_engine_for_tests()
    assert db_path().resolve().is_relative_to(tmp_path.resolve()), "测试数据库未隔离，禁止初始化"
    init_db()
    # 硬隔离：引擎路径必须落在 tmp_path，防止污染开发机真实 binggo.db
    resolved_db = db_path().resolve()
    resolved_home = tmp_path.resolve()
    assert resolved_db.is_relative_to(resolved_home), resolved_db
    # 额外保险：绝不能落到仓库真实 data/binggo.db
    real_db = (ROOT / "data" / "binggo.db").resolve()
    assert resolved_db != real_db, "isolated_home 仍指向仓库真实数据库"
    yield tmp_path
    reset_engine_for_tests()
    assert db_path().resolve().is_relative_to(tmp_path.resolve()), db_path()
