# Binggo 二次开发版 · B 站抽奖助手（本地部署 + 自动三连）

在本机 Web 控制台里自动发现抽奖动态、判定开奖时间、按节奏参加（关注 / 转发 / 评论 / 预约），
带定时调度器与风控退避。当前这套跑在 Windows 上：面板 `http://127.0.0.1:8787`，SQLite 单文件库，
无外部服务依赖（LLM 走 OpenAI 兼容接口，**可选**，不配也能跑规则路径）。

> **本项目是二次开发版，不是上游官方发布版。**
> 上游与授权说明见 [UPSTREAM.md](UPSTREAM.md)，许可证见 [LICENSE](LICENSE)（MIT）。

## 免责声明

继承上游声明：**仅供个人学习与研究使用**，非官方工具，未获哔哩哔哩授权或认可。使用须遵守法律法规及 B 站平台规则，
**严禁**用于商业用途（收费代抽、营销引流等）、**批量操作多账号**（一机多号、养号、控号、矩阵运营）与任何违法违规行为。
由使用产生的一切后果（账号封禁、功能限制、数据丢失、法律追责）由使用者自行承担，作者与贡献者不承担责任。

本实现的默认取值也是按这个边界设计的：**单账号、单机、每 15 分钟最多 3 条、每轮最多 25 条链接**，并且任何一次账号
动作前先向平台核对真值（已关注/已转发/已评论就跳过），不做绕过风控的加速。

## 相对上游做了什么

| 主题 | 上游 | 本仓库 |
| --- | --- | --- |
| 开奖时间 | 主要依赖 LLM 抽取 | **防幻觉闸门**：正文字面证据 + 本地重算 + 精度一致才写库；支持「晚8点」「10/30」「跨行【开奖日期】」等口语写法（`src/time_evidence.py`） |
| 重复转发 | 按动态 id 判重 | **按转发链根判重**（B 站会把「转发-of-转发」压回链根，链根相同=同一条抽奖换壳），一轮只发一次，动手前再查平台 `is_reposted`（`src/lottery_actions.py`、`web/activity_service.py`） |
| 数据量 | 全量平铺 | **归档分层**（只打标不搬数据，可见层/归档层切换）+ **活动序号**与 **UP uid 绑定**，可 `#1907` 直接定位、`--up <uid>` 按 UP 归类（`src/activity_archive.py`、`src/activity_serials.py`） |
| 脏数据 | 合集/专栏会被当活动 | **入库守卫**：无官方抽奖组件的专栏、以及源 UP 自己的公告，采集阶段就丢弃（`src/lottery_enricher.py`） |
| 风控 | 有基础退避 | 412 熔断（6 小时内 ≥2 次停批量 3 小时）、账号闸只读恢复探测、调度 fatal 自愈、**巡查让车**（补入库逼近三连格点时主动放锁） |
| 运维 | 手动为主 | 一整套 Windows 侧脚本：面板重启、开机自启、队列审计、时间修复工具链、趋势快照 |

## 目录结构

```
bilibili-dynamic-assistant/
├── bilibinggo/               应用本体（FastAPI + SQLModel + Vite 前端）
│   ├── src/                  抓取、解析、参加动作、风控闸门、时间证据闸门
│   ├── web/                  面板后端（app.py）+ 前端源码 frontend/
│   ├── scripts/              命令行入口（run_dashboard.py 等）
│   ├── tests/                pytest（全部离线，不碰账号、不碰生产库）
│   ├── config/               *.example 模板与 sources.yaml；真实凭据不入库
│   ├── data/                 binggo.db 与留痕、备份（不入库）
│   ├── docs/                 功能与接口文档
│   └── README.md             上游 README（保留来源说明）
├── watch_new_sources.py      数据源巡查：比断点、补入库、健康检查、三项体检
├── review_queue_now.py       三连队列审计 + 假队列清理
├── guard_trend.py            守卫/队列趋势快照（deferred/review 增长预警）
├── archive_activities.py     一键归档（打标，不搬数据）
├── activity_by_number.py     按序号/UP 查活动、重复转发体检
├── read_pages_for_times.py · apply_page_read_times.py · repair_stored_evidence_times.py
│                             llm_reread_cached_texts.py · reopen_verified_missing.py · rejoin_unverified_rows.py
│                             时间/状态修复工具链（都先预演，写前整库备份，可回滚）
├── restart_dashboard.py      重启面板；autostart_binggo.py + register_binggo_guard.ps1 开机自启
├── .github/workflows/ci.yml  CI（后端 pytest + 前端 vitest/tsc/vite）
├── CHANGELOG.md              二次开发的变更记录
├── UPSTREAM.md               上游、基线提交与授权说明
└── LICENSE                   MIT
```

## 快速开始

```bash
# 1) 后端依赖（Python 3.12 实测）
cd bilibinggo
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt   # Linux/macOS 用 .venv/bin/pip

# 2) 凭据：只从模板复制，真实文件已被 .gitignore 挡住
cp config/cookies.txt.example config/cookies.txt      # B 站 Cookie（SESSDATA 等）
cp config/llm.env.example config/llm.env              # 可选：不配则只走规则路径

# 3) 前端必须构建一次（dist 不入库）
cd web/frontend && npm install && npm run build       # tsc --noEmit + vite build → web/static/dist

# 4) 起面板
cd ../.. && python scripts/run_dashboard.py           # 打开 http://127.0.0.1:8787
```

面板里先确认账号信息与登录态，再开启自动调度。改了后端代码**必须重启**（没有热重载）：
`python restart_dashboard.py --force`。

## 运行节奏（`bilibinggo/web/auto_config.py`）

| 事项 | 频率 |
| --- | --- |
| 三连参与 | 每 10 分钟一格（:00/:10/:20/:30/:40/:50），每格 3 条（18 条/小时），先开奖的先参加；时间未知的排队尾，其中「只写了参与截止」的按截止日近→远排在彻底没信息的前面；同一转发链、同一动态一轮只一次 |
| 数据源刷新 | 每天 5 次：05:00 / 09:00 / 13:00 / 17:00 / 21:00（每 4 小时一轮），9 个源 + 监控用户 + 状态刷新；重启不补跑当天已完成的窗口 |
| 巡查补漏 | 每 3 小时 :07 跑 `watch_new_sources.py`：比断点，发现新容器就补入库（每轮上限 25 条链接、batch 6 / workers 2），并做幽灵已参加、归队审计、重复转发三项体检 |
| 让车规则 | 补入库与面板共用一把执行锁（`@serialized_task`）。离三连格点不足 3 分钟不再开新批次；已在跑的批次在条目/批次边界收手放锁，断点不推进，下一轮按 `pending − 已入库` 继续 |
| 风控退避 | 412 触发后记事件，6 小时内 ≥2 次则熔断补入库 3 小时只做恢复复查；账号闸恢复靠只读探测 |

## 开发

```bash
cd bilibinggo
.venv/Scripts/python.exe -m pytest -q                 # 全量测试（当前 608 passed / 2 skipped）
cd web/frontend && npm run build                       # 前端构建
```

CI（`.github/workflows/ci.yml`，push / PR 触发）跑两件事：`bilibinggo` 下的 pytest（先现场构建前端，
因为面板的静态托管测试需要 `web/static/dist`），以及前端的 `vitest + tsc --noEmit + vite build`。
Playwright e2e 没进 CI——依赖浏览器下载且易受网络抖动影响，本地 `npm run test:e2e` 可跑。

约定：**任何写库操作都要先预演**，`--apply` 前用 sqlite backup API 整库备份、逐行 before 快照落 `data/_*_log.json`，可回滚；
测试通过 `BINGGO_HOME` + `BINGGO_TEST_DATA_ROOT` 指向临时目录，禁止碰生产数据（`src/test_isolation.py` 强制）。

## 安全与隐私

- 凭据与数据全部留在本机：`config/cookies.txt`、`config/llm.env`、`data/` 均在 `.gitignore` 内，仓库只带 `*.example`。
- 面板无鉴权且只绑 `127.0.0.1:8787`，**不要暴露到公网**；需要远程访问请自己加 SSH/隧道 + 反向鉴权。
- 参与动作有平台核对与本地留痕三张表（`participations` / `participation_actions` / `participation_guards`），中断可按进度续跑，不会重复发。

## 已知限制

- B 站对 `web-dynamic/v1/detail` 有 WAF（412），批量读会触发，系统靠退避 + 熔断 + 恢复探测自保。
- 纯图片公告（开奖时间写在图里）读不到；`major.draw.id` 类抽奖卡有 404 情况，靠正文兜底。
- 源发现只比每源**最新一期**容器，中间漏发的期次不回补（跨期重复由链根/链接身份去重兜底）。

## License

MIT — 见 [LICENSE](LICENSE)。上游版权归属、基线提交号与 `LICENSE` 缺失的处理见 [UPSTREAM.md](UPSTREAM.md)。
二次开发的变更逐条记在 [CHANGELOG.md](CHANGELOG.md)。
感谢上游 [luovicter-collab/bilibinggo](https://github.com/luovicter-collab/bilibinggo) 的作者与贡献者。
