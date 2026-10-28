# 上游与授权说明

本仓库是 **二次开发版**，不是上游官方发布版。

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/luovicter-collab/bilibinggo （作者 GitHub 显示名 `victor`） |
| 上游描述 | Binggo · 开源 B 站抽奖助手（哔哩哔哩抽奖工具）— 本机自动发现活动，一键/三连参与 |
| 源码基线提交 | `d7678f3cdcd893452d53ac4911e7118cea66fca5` |
| 上游声明的协议 | 上游 README 的徽章与 `## License` 段均写 **MIT**，并链接 `LICENSE` |

## 关于 LICENSE 文件

发布本仓库时（2026-10-28）核查：上游仓库根目录 **没有 `LICENSE` 文件**（README 里的链接指向一个不存在的文件），GitHub 的 license 识别结果也是 `null`。因此本仓库的 `LICENSE` 采用 **MIT 标准全文**，版权行同时保留上游作者的署名，并为我们的改动单独署名——这是对上游「MIT」这一明示意图的忠实落地，不是我们对上游代码重新授权。

若上游日后补上 `LICENSE` 或对授权另有说明，以作者本人的文本为准，本仓库会同步调整。

## 本仓库改了什么

`bilibinggo/` 内被修改的部分 + 根目录全部运维脚本，都是二次开发内容，主要包括：

- 开奖时间的**防幻觉闸门**：正文字面证据 + 本地重算 + 精度一致才入库（`src/time_evidence.py`）。
- **转发链去重**：B 站会把「转发-of-转发」压回链根，链根相同即视为同一条抽奖换壳，一轮只发一次（`src/lottery_actions.py`、`web/activity_service.py`）。
- **归档分层**（打标不搬数据）、**活动序号 + UP uid 绑定**（`src/activity_archive.py`、`src/activity_serials.py`）。
- **入库守卫**：合集/专栏容器与源 UP 自己的公告不入库（`src/lottery_enricher.py`）。
- **风控与频率**：412 退避、账号闸熔断与恢复探测、刷新窗口 `5/9/13/17/21`、巡查让车（`web/auto_config.py`、`web/auto_scheduler.py`、`watch_new_sources.py`）。
- Windows 侧运维：面板重启、开机自启、队列审计、时间修复工具链（根目录各脚本）。

实测与修复过程的完整台账在本地维护（含账号与 UP 昵称等信息，不随仓库发布）。

## 未随本仓库发布的上游文件

上游的 4 个 workflow（`ci.yml`、`pages.yml`、`release-macos.yml`、`release-windows.yml`）原本落在 `bilibinggo/.github/workflows/`。
GitHub 只执行**仓库根目录**的 `.github/workflows/`，放在子目录里既不会运行、又容易让人误以为本仓库有打包发布流水线，
因此从工作副本中移除，改由根目录 `.github/workflows/ci.yml` 承担（后端 pytest + 前端 vitest/tsc/vite）。
需要上游那套 PyInstaller 打包/发布配置时，按上面的基线提交号去上游仓库取。

## 截图说明

`bilibinggo/docs/images/` 与 `bilibinggo/assets/` 里的界面截图**来自上游**，其中的账号名、UID、关注数、活动数都是**上游作者本人的账号数据**，与本仓库维护者无关，也不代表本二次开发版的实际界面（本仓库的改动集中在时间判定、去重、归档、风控与运维脚本）。

