"""参与活动：互动/转发按必需规则执行操作，预约执行关注 + 预约。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bilibili_client import BilibiliClient
from src.fetch_activity_info import ENRICHED_OUTPUT_PATH
from src.participation import participate_activity
from src.sources.common import load_previous_output

SUPPORTED_TYPES = {"互动抽奖", "转发抽奖", "预约抽奖"}


def _lookup_lottery_type(dynamic_id: str) -> str:
    payload = load_previous_output(ENRICHED_OUTPUT_PATH)
    if not payload:
        raise RuntimeError(f"未找到活动库: {ENRICHED_OUTPUT_PATH}")
    for item in payload.get("activities") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("dynamic_id") or "") != dynamic_id:
            continue
        lottery_type = str(item.get("lottery_type") or "")
        if lottery_type in SUPPORTED_TYPES:
            return lottery_type
    raise RuntimeError(
        f"未找到活动 {dynamic_id} 的类型信息，请先运行一键更新或维护脚本补全活动库"
    )


def main() -> int:
    from src.app_paths import ensure_user_dirs
    ensure_user_dirs()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="参与抽奖活动")
    parser.add_argument("dynamic_id", help="动态 ID")
    parser.add_argument("--text", help="自己的转发/评论文案")
    parser.add_argument("--execute", action="store_true", help="实际参与；默认只展示预览")
    parser.add_argument("--confirm-rules", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        lottery_type = _lookup_lottery_type(args.dynamic_id)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    from src.comment_preview import create_preview
    preview = create_preview("participate", [args.dynamic_id], text=args.text)
    print(json.dumps({"mode": "preview", **preview["body"]}, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    chosen = preview["body"]["items"][0]["text"]

    with BilibiliClient() as client:
        result = participate_activity(
            client,
            dynamic_id=args.dynamic_id,
            lottery_type=lottery_type,
            dry_run=False,
            persist=True,
            action_text=chosen,
        )

    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.status == "joined" else 1


if __name__ == "__main__":
    raise SystemExit(main())
