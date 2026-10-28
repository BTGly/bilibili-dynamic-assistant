"""旧维护入口改为显式清理预览；详情补全请运行正式刷新任务。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.purge_all_dead_links import main

if __name__ == '__main__':
    raise SystemExit(main())
