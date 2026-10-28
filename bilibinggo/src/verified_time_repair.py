"""Targeted corrections from captured public text, with preview and stale-row checks."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from src.lottery_time import BEIJING_TZ, normalize_activity_timing
from src.time_evidence import validate_extracted_times
from src.time_repair import _open, _item, _fingerprint


def preview_verified_dates(database: Path, evidence: list[dict]) -> dict:
    records = []
    with _open(database) as conn:
        for proof in evidence:
            if not proof.get("lottery_time"):
                continue
            checked = validate_extracted_times(proof, proof["text"], reference_ts=proof.get("publication_ts"))
            if not checked.get("lottery_time_verified"):
                raise ValueError("日期与原文不一致：" + proof["id"])
            row = conn.execute("SELECT * FROM activities WHERE dynamic_id=?", (proof["id"],)).fetchone()
            if row is None:
                raise ValueError("活动不存在：" + proof["id"])
            item = _item(row)
            before = {"lottery_time": item.get("lottery_time"), "activity_status": item.get("activity_status")}
            conditions = dict(item.get("conditions") or {})
            conditions.update(lottery_time_text=checked["lottery_time"],
                              lottery_time_source=proof.get("lottery_time_source") or "original_text",
                              lottery_time_precision=checked["lottery_time_precision"],
                              lottery_time_evidence=checked["lottery_time_evidence"],
                              time_reference_ts=proof["publication_ts"], time_extraction_checked=True)
            conditions.pop("lottery_time_inferred", None)
            item.pop("lottery_time_inferred", None)
            item["conditions"] = conditions
            item["lottery_time"] = int(datetime.strptime(checked["lottery_time"], "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ).timestamp())
            normalize_activity_timing(item)
            records.append({"dynamic_id": proof["id"], "fingerprint": _fingerprint(row), "before": before,
                            "after": item, "evidence": checked["lottery_time_evidence"]})
    return {"database": str(database.resolve()), "records": records}


def apply_verified_dates(database: Path, preview: dict, *, backup: Path) -> dict:
    from src.task_lock import execution_lock
    from src.test_isolation import assert_test_path
    from src.secure_files import harden_file_permissions
    assert_test_path(database)
    assert_test_path(backup)
    if str(database.resolve()) != preview["database"] or backup.exists() or backup.resolve() == database.resolve():
        raise ValueError("数据库或备份路径不匹配")
    with execution_lock():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.touch(exist_ok=False)
        if not harden_file_permissions(backup):
            raise RuntimeError("备份权限设置失败")
        with _open(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败")
        with _open(database, writable=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in preview["records"]:
                row = conn.execute("SELECT * FROM activities WHERE dynamic_id=?", (record["dynamic_id"],)).fetchone()
                if row is None or _fingerprint(row) != record["fingerprint"]:
                    raise RuntimeError("预览后数据有变化，未执行修复")
            for record in preview["records"]:
                item = record["after"]
                conn.execute("UPDATE activities SET lottery_time=?, activity_status=?, draw_status=?, draw_tag=?, payload_json=?, updated_at=? WHERE dynamic_id=?",
                             (item["lottery_time"], item["activity_status"], item["draw_status"], item.get("draw_tag", ""),
                              json.dumps(item, ensure_ascii=False), int(time.time()), record["dynamic_id"]))
                conn.execute("DELETE FROM forward_parse_cache WHERE dynamic_id=?", (record["dynamic_id"],))
    return {"backup": str(backup.resolve()), "corrected_ids": [r["dynamic_id"] for r in preview["records"]]}
