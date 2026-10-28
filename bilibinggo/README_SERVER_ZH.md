# B 站动态助手：Linux 开发版

本版基于 https://github.com/luovicter-collab/bilibinggo ，源码基线提交为 `d7678f3cdcd893452d53ac4911e7118cea66fca5`。保留上游 README 和来源说明。本包是本次修改版，不是上游官方发布版。

## 本版做了什么

- 复用上游网页面板、活动发现、扫码登录、参与记录和定时调度。
- 新增 `scripts/dynamic_operations.py`：按明确的动态链接或 ID 操作，默认只预览；实际执行需要 `--execute`。
- 新入口默认依次关注、点赞、转发、评论，可通过 `--actions` 选择；不会执行收藏、投币、视频操作。
- 新入口要求转发和评论文字明确提供，不自动补写，也不截断超长文案。
- 新入口缺少 `modules.module_author.mid`、评论所需的 `basic.comment_id_str` 或 `basic.comment_type` 时停止，避免推断操作对象。
- 修复转发错误文案包含“已”或“重复”就误报成功的问题；错误响应必须核实账号转发状态。
- 关注、点赞、转发、评论的写请求不自动重试，避免超时后重复提交。
- 新入口失败即停止，输出已完成动作和剩余动作。原面板保留上游完整参与流程，包括动态收藏；选择四项操作的入口目前在命令行中。

## 服务器要求与本地验证

需要 Linux、Bash、Python 3.12 或更高版本、Python venv 和 pip，以及正常的 HTTPS 网络访问。包内已经包含构建好的前端，服务器不需要安装 Node.js 来运行。

用户提供的阿里云配置是 2 核 CPU、2 GiB 内存、40 GiB 系统盘。当前选择接口操作方案，不需要运行浏览器或 MySQL。测试环境中服务刚启动时，进程 RSS 约 69 MiB，包含测试依赖的 Python 虚拟环境约 121 MiB。该数字来自本地空库、未登录状态，不是阿里云持续运行测量，也不是峰值保证；活动库、日志和任务运行会增加占用。这套配置适合先做单账号部署验证。

2026-10-08 验证结果：

- 129 项针对性 Python 测试通过，覆盖动作选择、预览不写入、失败停止、转发误报、字段缺失、原参与流程、网页接口、静态页面、限速和日志脱敏。
- 前端类型检查和生产构建通过。
- Shell 语法检查、Git diff 空白检查通过。
- 实际启动服务后，首页 `/`、`/api/jobs/current`、`/api/runtime` 均返回 HTTP 200。
- 测试依赖出现一项上游 TestClient 弃用提示，不影响以上测试结果。
- 没有登录用户账号，也没有实际向 B 站发出关注、点赞、转发或评论请求；B 站端到端验证仍需在用户服务器完成。

## 先确认服务器环境

不要根据监控页面推断系统发行版。请在服务器终端运行下面两行，并提供输出；如果已有 Python，也请保留实际版本信息。

```bash
cat /etc/os-release
python3 --version
```

不同发行版的 Python 安装步骤不同，所以此处不提供未经核实的系统包安装命令。

## 安装并启动

解压本包，进入 `bilibinggo` 目录。在已有 Python 3.12+、venv 和 pip 的情况下运行：

```bash
bash deploy/linux/setup.sh
bash deploy/linux/start.sh
```

程序前台运行，终端关闭会影响该运行方式。长期运行所需的进程管理与开机启动，待确认服务器系统和现有管理方式后配置。程序重启后请在面板核对调度器状态，不能假定自动参与会自动恢复。

默认监听 `127.0.0.1:8787`。网页面板持有账号登录态，使用 SSH 隧道访问；不需要把该端口开放到公网。请在你自己的电脑终端运行下方模板，把 SSH 用户和服务器 IP 替换为真实值：

```bash
ssh -N -L 8787:127.0.0.1:8787 <SSH登录用户>@<服务器公网IP>
```

随后在电脑浏览器打开 http://127.0.0.1:8787 ，通过原面板扫码登录。SSH 身份、密钥或密码由用户在本机处理，不发送到聊天中。

启动脚本通过上游真实支持的 `BINGGO_HOME` 指定数据目录，默认是项目目录下的 `var/`。登录信息、数据库和日志保存在该目录。需要外部目录时，在启动前明确设置这个环境变量。移动项目或切换到命令行时应继续使用同一个数据目录。

## 按动态链接预览与执行

扫码登录后，在项目根目录运行。下面的动态 ID 来自用户提供的链接；示例文字仅演示语法，不代表该活动要求这段评论，也不说明该活动仍在进行中。

```bash
export BINGGO_HOME="$PWD/var"
.venv/bin/python scripts/dynamic_operations.py 'https://www.bilibili.com/opus/1248895125820538904' --text '这里替换为你确认要发送的评论'
```

这是预览，读取当前状态并显示将要执行的动作，不提交写请求。确认目标和文案后，明确执行：

```bash
.venv/bin/python scripts/dynamic_operations.py 'https://www.bilibili.com/opus/1248895125820538904' --text '这里替换为你确认要发送的评论' --execute
```

只选择关注与点赞：

```bash
.venv/bin/python scripts/dynamic_operations.py 'https://www.bilibili.com/opus/1248895125820538904' --actions follow like --execute
```

结果为 JSON，包含 `mode`、`ok`、`dynamic_id`、`sender_uid`、`text`、`requested_actions`、`actions`、`remaining_actions`、`note`。预览中的 `ok` 仅表示预览正常生成；执行中的 `ok` 表示所选动作均被当前实现判为完成。退出码 0 为完成，1 为运行失败，2 为参数错误。

新命令不自动将活动标记为“已参加”。操作完成不能替代活动规则核验，也不能证明抽奖资格。该命令不会自动判断文字抽奖是否过期、是否要求特定评论、是否需视频三连；这些条件应按活动原文确认。原面板的活动管理和自动调度仍按上游活动库流程工作。

支持完整 `https://www.bilibili.com/opus/…`、`https://t.bilibili.com/…` 链接和上游校验允许的动态 ID。`b23.tv` 短链接请先在浏览器打开，再复制实际动态链接；视频链接在本版新入口中会被拒绝。

## 验证与当前边界

默认使用项目已有的查询能力检查重复操作。上游账号动态查询最多读取三页，评论查询仅检查一页，较旧的转发或被折叠的评论可能无法被找到；当前不能保证覆盖所有历史参与记录。写请求超时时结果可能不确定，请先核查 B 站实际状态，再决定是否重试。

原面板的“参与”以及定时调度属于上游现有功能。新加入的动作选择、严格字段检查、预览、失败停止属于新命令行入口；本版没有把这些选项接到原面板按钮上。后续可以在实测接口后增加统一的面板设置。

需要运行本次验证时，另装开发依赖，再运行以下命令：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/test_dynamic_operations.py tests/test_participation_success.py tests/test_participation_validation.py tests/test_participation_guards.py tests/test_participate_reserve.py tests/test_participate_triple.py tests/test_participate_text.py tests/test_favorite_optional.py tests/test_dashboard_server.py tests/test_frontend_static.py tests/test_web_api.py tests/test_api_errors.py tests/test_bilibili_rate_limit.py tests/test_log_redact.py tests/test_secure_files.py -q
```
