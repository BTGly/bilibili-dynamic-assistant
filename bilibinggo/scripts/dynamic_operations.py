"""按明确的动态链接预览或执行操作；不推断活动规则、不标记抽奖资格。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bilibili_auth import require_login
from src.bilibili_client import BilibiliClient
from src.log_redact import redact_obj
from src.lottery_actions import execute_full_participation
from src.sources.common import is_valid_dynamic_id

DEFAULT_ACTIONS = ("follow", "like", "repost", "comment")


def parse_target(value: str) -> str:
    value = value.strip()
    if is_valid_dynamic_id(value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("请使用 HTTPS 动态链接或 18–19 位动态 ID")
    if parsed.netloc == "www.bilibili.com":
        match = re.fullmatch(r"/opus/(\d{18,19})/?", parsed.path)
    elif parsed.netloc == "t.bilibili.com":
        match = re.fullmatch(r"/(\d{19})/?", parsed.path)
    else:
        match = None
    if match is None:
        raise argparse.ArgumentTypeError("只支持 www.bilibili.com/opus/ 和 t.bilibili.com/ 动态链接；短链接请先打开获取完整链接")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=parse_target, help="完整动态链接或动态 ID")
    parser.add_argument("--actions", nargs="+", choices=DEFAULT_ACTIONS, default=list(DEFAULT_ACTIONS))
    parser.add_argument("--text", help="明确指定转发/评论文字；最多 233 字符，不自动生成")
    parser.add_argument("--execute", action="store_true", help="实际执行；不传此参数只读取状态并输出预览")
    args = parser.parse_args(argv)
    if len(set(args.actions)) != len(args.actions):
        parser.error("--actions 不允许重复")
    text = (args.text or "").strip()
    if set(args.actions) & {"repost", "comment"} and not text:
        parser.error("转发或评论必须明确提供 --text")
    if len(text) > 233:
        parser.error("--text 不能超过 233 字符；工具不会截断文字")
    try:
        require_login()
        with BilibiliClient() as client:
            actions, context = execute_full_participation(
                client,
                dynamic_id=args.target,
                action_text=text,
                dry_run=not args.execute,
                action_names=tuple(args.actions),
                stop_on_failure=True,
                strict_context=True,
                journal=args.execute,
            )
        complete = len(actions) == len(args.actions) and all(action.ok for action in actions)
        payload = {
            "mode": "execute" if args.execute else "preview",
            "ok": complete,
            "dynamic_id": args.target,
            "sender_uid": context.sender_uid,
            "text": text,
            "requested_actions": args.actions,
            "actions": [asdict(action) for action in actions],
            "remaining_actions": args.actions[len(actions):],
            "note": "操作结果不代表已满足抽奖规则或获得抽奖资格；请按活动规则核对。",
        }
        print(json.dumps(redact_obj(payload), ensure_ascii=False, indent=2))
        return 0 if complete else 1
    except Exception as exc:
        print(json.dumps(redact_obj({"ok": False, "error": str(exc)}), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
