"""预览或修复 SQLite 中明确标记为推断的开奖日期；默认只读预览。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import uuid
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.app_paths import user_home
from src.time_repair import preview_time_repairs, apply_time_repairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=user_home() / "data" / "binggo.db")
    parser.add_argument("--preview", type=Path, required=True, help="预览 JSON 路径；--apply 时读取此文件")
    parser.add_argument("--apply", action="store_true", help="备份后只修复预览中仍匹配且带明确推断标记的记录")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.apply:
        preview = json.loads(args.preview.read_text(encoding="utf-8"))
        backup = args.backup or args.database.parent / "backups" / ("before-time-repair-" + uuid.uuid4().hex + ".db")
        result = apply_time_repairs(args.database, preview, backup=backup)
    else:
        result = preview_time_repairs(args.database)
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        args.preview.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("records", "repaired_ids")}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
