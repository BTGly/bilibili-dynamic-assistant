"""Explicit activity cleanup: read-only preview, backup, then fingerprint-checked deletion."""
from pathlib import Path
import json
import sqlite3
from src.time_repair import _open, _fingerprint


def preview_cleanup(database: Path) -> dict:
    conn = _open(database)
    try:
        conn.execute("BEGIN")
        records = []
        for row in conn.execute("SELECT * FROM activities ORDER BY dynamic_id"):
            item = json.loads(row["payload_json"] or "{}")
            access = item.get("accessibility") or {}
            if access.get("state") == "inaccessible":
                records.append({"dynamic_id": row["dynamic_id"], "fingerprint": _fingerprint(row),
                                "reason": access.get("reason"), "activity_status": row["activity_status"]})
        return {"version": 1, "database": str(database.resolve()), "mode": "cleanup_preview", "records": records}
    finally:
        conn.close()


def apply_cleanup(database: Path, preview: dict, backup: Path) -> dict:
    from src.test_isolation import assert_test_path
    assert_test_path(database)
    assert_test_path(backup)
    if preview.get("version") != 1 or preview.get("mode") != "cleanup_preview" or Path(preview["database"]).resolve() != database.resolve():
        raise ValueError("清理预览不匹配")
    if backup.exists() or backup.resolve() == database.resolve():
        raise ValueError("不能覆盖原库或已有备份")
    backup.parent.mkdir(parents=True, exist_ok=True)
    from src.secure_files import harden_file_permissions
    backup.touch(exist_ok=False)
    if not harden_file_permissions(backup):
        raise RuntimeError("清理备份权限收紧失败，未复制数据或删除活动")
    conn = _open(database, writable=True)
    try:
        from contextlib import closing
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败")
        conn.execute("BEGIN IMMEDIATE")
        for record in preview["records"]:
            row = conn.execute("SELECT * FROM activities WHERE dynamic_id=?", (record["dynamic_id"],)).fetchone()
            if row is None or _fingerprint(row) != record["fingerprint"]:
                raise RuntimeError("清理预览已过期；本次未删除数据")
            if (json.loads(row["payload_json"] or "{}").get("accessibility") or {}).get("state") != "inaccessible":
                raise RuntimeError("活动已恢复访问；本次未删除数据")
        for record in preview["records"]:
            conn.execute("DELETE FROM activities WHERE dynamic_id=?", (record["dynamic_id"],))
        conn.commit()
        return {"deleted": len(preview["records"]), "backup": str(backup.resolve()), "participation_history": "preserved"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
