"""只读预览不可访问活动；只有 --apply 才按预览备份并删除。"""
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
from src.activity_cleanup import preview_cleanup, apply_cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=user_home() / 'data/binggo.db')
    parser.add_argument('--preview', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='明确执行预览中的删除，不重新联网判定')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if args.apply:
        preview = json.loads(args.preview.read_text(encoding='utf-8'))
        backup = args.backup or args.database.parent / 'backups' / ('before-cleanup-' + uuid.uuid4().hex + '.db')
        report = apply_cleanup(args.database, preview, backup)
    else:
        report = preview_cleanup(args.database)
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        args.preview.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
