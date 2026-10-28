"""Read-only SQLite repair previews; backed-up, fingerprint-checked targeted updates."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from src.lottery_time import has_inferred_time, normalize_activity_timing


def _open(database: Path, *, writable=False):
    from src.test_isolation import assert_test_path
    assert_test_path(database)
    conn = sqlite3.connect(database.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _item(row):
    payload = json.loads(row["payload_json"] or "{}")
    payload.update(dynamic_id=row["dynamic_id"], lottery_time=row["lottery_time"],
                   draw_status=row["draw_status"], activity_status=row["activity_status"])
    return payload


def _fingerprint(row):
    encoded = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _conflict(item):
    conditions = item.get("conditions") or {}
    source = item.get("lottery_time_source") or conditions.get("lottery_time_source")
    evidence = item.get("lottery_time_evidence") or conditions.get("lottery_time_evidence")
    return bool(evidence or source in ("platform", "official", "manual", "original_text"))


def preview_time_repairs(database: Path) -> dict:
    conn = _open(database)
    try:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM activities ORDER BY dynamic_id").fetchall()
        candidates = []
        for row in rows:
            item = _item(row)
            if not has_inferred_time(item):
                continue
            candidates.append({"dynamic_id": row["dynamic_id"], "fingerprint": _fingerprint(row),
                "before": {"lottery_time": row["lottery_time"], "lottery_time_text": (item.get("conditions") or {}).get("lottery_time_text"),
                           "activity_status": row["activity_status"], "draw_status": row["draw_status"]},
                "conflicting_evidence": _conflict(item),
                "proposal": {"lottery_time": None, "participation": "preserve", "review_required": True}})
        return {"version": 1, "database": str(database.resolve()), "mode": "read_only_preview", "activity_count": len(rows),
                "inferred_count": len(candidates), "conflicting_count": sum(item["conflicting_evidence"] for item in candidates),
                "status_counts": dict(Counter(item["before"]["activity_status"] for item in candidates)), "records": candidates}
    finally:
        conn.close()


def apply_time_repairs(database: Path, preview: dict, *, backup: Path) -> dict:
    from src.test_isolation import assert_test_path
    assert_test_path(database)
    assert_test_path(backup)
    if preview.get("version") != 1 or Path(preview["database"]).resolve() != database.resolve():
        raise ValueError("预览版本或数据库路径不匹配")
    if backup.resolve() == database.resolve() or backup.exists():
        raise ValueError("备份路径不能指向原数据库，也不能覆盖已有文件")
    backup.parent.mkdir(parents=True, exist_ok=True)
    from src.secure_files import harden_file_permissions
    backup.touch(exist_ok=False)
    if not harden_file_permissions(backup):
        raise RuntimeError("修复备份权限收紧失败，未复制数据或修改活动")
    source = _open(database)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份完整性检查失败，禁止修复")
    finally:
        destination.close()
        source.close()
    conn = _open(database, writable=True)
    repaired = []
    skipped = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Check every previewed record before the first update. A stale preview never partially applies.
        rows = {}
        for record in preview["records"]:
            row = conn.execute("SELECT * FROM activities WHERE dynamic_id=?", (record["dynamic_id"],)).fetchone()
            if row is None or _fingerprint(row) != record["fingerprint"] or not has_inferred_time(_item(row)):
                raise RuntimeError("预览已过期，请重新生成；本次未写入活动数据")
            rows[record["dynamic_id"]] = row
        now = int(time.time())
        for record in preview["records"]:
            row = rows[record["dynamic_id"]]
            item = _item(row)
            if _conflict(item):
                skipped.append(record["dynamic_id"])
                continue
            conditions = dict(item.get("conditions") or {})
            item["time_repair"] = {"repaired_at": now, "original": record["before"],
                                   "original_fingerprint": record["fingerprint"], "reason": "removed_explicit_inferred_date"}
            item.pop("lottery_time_inferred", None)
            conditions.pop("lottery_time_inferred", None)
            conditions["lottery_time_text"] = ""
            conditions["lottery_time_source"] = "unknown"
            conditions["lottery_time_precision"] = "unknown"
            item.update(lottery_time=None, conditions=conditions)
            normalize_activity_timing(item, now=now)
            conn.execute("UPDATE activities SET lottery_time=NULL, draw_status=?, activity_status=?, draw_tag='', "
                         "status_classified=1, payload_json=?, updated_at=? WHERE dynamic_id=?",
                         (item["draw_status"], item["activity_status"], json.dumps(item, ensure_ascii=False), now, row["dynamic_id"]))
            repaired.append(row["dynamic_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"database": str(database.resolve()), "backup": str(backup.resolve()), "repaired_count": len(repaired),
            "repaired_ids": repaired, "conflict_skipped_ids": skipped, "unmarked_records_changed": 0}
