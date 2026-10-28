"""Verified owner-only permissions; secret writes fail before exposing credentials."""

from __future__ import annotations

import os
import stat
import sys
import json
import subprocess
import tempfile
from pathlib import Path

_RESULTS: dict[str, dict] = {}


def _windows_acl(path: Path, *, apply: bool) -> bool:
    script = r'''
$ErrorActionPreference = 'Stop'
$target = $env:BINGGO_PERMISSION_TARGET
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ($env:BINGGO_PERMISSION_APPLY -eq '1') {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
    [System.IO.File]::SetAccessControl($target, $acl)
}
$check = [System.IO.File]::GetAccessControl($target)
$rules = @($check.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$valid = $check.AreAccessRulesProtected -and $rules.Count -eq 1 -and $rules[0].IdentityReference.Value -eq $sid.Value -and $rules[0].AccessControlType -eq 'Allow' -and (($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl)
@{protected=[bool]$valid} | ConvertTo-Json -Compress
'''
    env = dict(os.environ, BINGGO_PERMISSION_TARGET=str(path.resolve()), BINGGO_PERMISSION_APPLY="1" if apply else "0")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError("Windows ACL 操作失败，请检查文件所有权及文件系统权限")
    return json.loads(result.stdout)["protected"] is True


def permission_status(path: Path) -> dict:
    target = Path(path)
    if not target.is_file():
        return {"state": "missing", "protected": False}
    try:
        protected = _windows_acl(target, apply=False) if sys.platform == "win32" else not (target.stat().st_mode & 0o077)
        return {"state": "protected" if protected else "unprotected", "protected": protected,
                "last_result": _RESULTS.get(str(target.resolve()))}
    except Exception:
        return {"state": "failed", "protected": False, "reason": "无法验证文件权限"}


def harden_file_permissions(path: Path) -> bool:
    """尽力将 path 收紧为仅当前用户可读写。成功 True；跳过/失败 False。"""
    from src.test_isolation import assert_test_path
    assert_test_path(path)
    try:
        from src.app_logging import get_logger

        logger = get_logger("secure_files")
    except Exception:
        logger = None  # type: ignore[assignment]

    try:
        target = Path(path)
        if not target.is_file():
            return False
        if sys.platform == "win32":
            protected = _windows_acl(target, apply=True)
            if not protected:
                raise RuntimeError("Windows ACL 写入后验证未通过")
            _RESULTS[str(target.resolve())] = {"success": True}
            return True
        mode = target.stat().st_mode
        new_mode = mode & ~(stat.S_IRWXG | stat.S_IRWXO)
        new_mode |= stat.S_IRUSR | stat.S_IWUSR
        os.chmod(target, new_mode)
        return True
    except Exception as exc:
        _RESULTS[str(Path(path).resolve())] = {"success": False, "reason": str(exc)}
        if logger is not None:
            logger.warning("无法收紧权限 path=%s: %s", path, exc)
        return False


def write_text_secret(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """先保护空临时文件，再写入凭据并原子替换；权限失败明确报错。"""
    target = Path(path)
    from src.test_isolation import assert_test_path
    assert_test_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp_path = Path(temporary)
    try:
        if not harden_file_permissions(tmp_path):
            raise RuntimeError("密钥临时文件权限收紧失败，未写入凭据")
        tmp_path.write_text(text, encoding=encoding)
        tmp_path.replace(target)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    if not harden_file_permissions(target):
        raise RuntimeError("密钥已保存，但文件权限验证失败，请检查权限状态")
