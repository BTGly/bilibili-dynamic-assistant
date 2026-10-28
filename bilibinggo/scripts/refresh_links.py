"""Formal chunked refresh task. Preview by default; --execute submits a recorded job."""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--source", choices=[f"DS-{i}" for i in range(1, 8)])
    parser.add_argument("--chunk", type=int, default=20)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--release", help="人工恢复指定检查点")
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()
    if not 1 <= args.chunk <= 100 or (args.limit is not None and args.limit <= 0):
        parser.error("chunk 为 1～100，limit 为正整数")
    from src.app_paths import ensure_user_dirs
    from src.db.schema import init_db
    ensure_user_dirs()
    init_db()
    from src.pipeline.checkpoint_store import preview, release
    from src.task_lock import execution_lock
    from src.db.snapshots import load_ds_check_dict
    from src.activity_store import known_activity_ids
    from src.sources.common import normalize_activity_id, is_valid_dynamic_id
    urls = list(args.urls)
    if args.source:
        snapshot = load_ds_check_dict(args.source)
        if not snapshot:
            parser.error("该数据源没有检查快照，请先更新数据源")
        urls.extend(snapshot.get("activity_links") or [])
    if any(not is_valid_dynamic_id(normalize_activity_id(url) or "") for url in urls):
        parser.error("链接中有无效的动态 ID")
    if args.release:
        if not args.execute or not args.evidence.strip():
            parser.error("恢复需同时提供 --execute 和 --evidence 核查依据")
        with execution_lock():
            release(args.release, args.evidence)
    known = known_activity_ids()
    remaining = len({normalize_activity_id(url) for url in urls} - known)
    print(json.dumps({"mode": "execute" if args.execute else "preview", "new_links": remaining,
                      "batch_size": args.chunk, "limit": args.limit, "checkpoints": preview()},
                     ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    from web.job_runner import JobRunner
    runner = JobRunner()
    runner.recover_on_startup()
    job_id = runner.try_start("refresh_links", {"urls": urls, "batch_size": args.chunk,
                                             "limit": args.limit}, source="cli")
    if job_id is None:
        print("已有任务在运行，请等待结束", file=sys.stderr)
        return 1
    cancelled = False
    while runner.resolve_job_status(job_id).state == "running":
        try:
            time.sleep(0.2)
        except KeyboardInterrupt:
            if not cancelled:
                runner.cancel()
                cancelled = True
                print("已请求取消，等待在途请求收尾并保存结果…", file=sys.stderr)
    final = runner.resolve_job_status(job_id).to_dict()
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final["state"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
