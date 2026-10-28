# 第三批：凭据、数据库与评论提交保护

> 最新调整：按用户要求，已取消逐条文案确认和模型动作字段门槛，改为固定动作与已保存文案。参见 [参与自动化调整](participation-automation-adjustment.md)。下文的强制预览描述为当时方案，已被此调整取代。

## 计划与范围

按“先阻止误删及测试越界，再收紧凭据权限，最后整理导出和提交预览”的顺序执行。修改源码并运行隔离测试；真实活动库不做清理、不提交真实参与请求。现有凭据文件只调整访问权限，不输出其内容。

## 导出

- `scripts/export_migrate_bundle.py` 默认 `--mode diagnostic`。可分享 ZIP 只有数据库汇总：表计数及疑似敏感内容的位置、数量。不包含原始 SQLite、配置、日志、旧备份或任意额外文件。
- 敏感检查扫描各表的字段与嵌套 JSON，识别敏感键、常见凭据形式及当前已知凭据值；不输出命中值，不修改源库。它是线索检查，不能证明数据库不存在任何未知形式的秘密，因此分享包始终排除原始库。
- 网页文本诊断包保留排查日志，但再次替换已知凭据值，并附数据库敏感检查摘要。
- `--mode private` 才生成含 Cookie/API Key 的私人迁移备份，manifest 标明不可分享。旧的两个私人打包入口明确选择此模式，保持迁移能力。私人 ZIP 和 SQLite 临时快照先收紧文件权限，再写入内容；现有输出文件不覆盖。

```powershell
.\.venv\Scripts\python.exe scripts/export_migrate_bundle.py --mode diagnostic --no-appdata --out dist/Binggo-diagnostics.zip
.\.venv\Scripts\python.exe scripts/export_migrate_bundle.py --mode private --no-appdata --out dist/private/Binggo-userdata-migrate.zip
```

## Windows 权限

使用当前用户 SID 创建受保护 DACL，只保留该用户 FullControl，移除继承；写后重新读取 ACL 验证。实现通过 Windows PowerShell 的 .NET FileSecurity API 操作，遵循 [微软 ACL 文档](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/set-acl?view=powershell-5.1) 的安全描述符规则。

凭据写入使用唯一空临时文件，先收紧权限，再写入并原子替换。权限处理失败时明确报错；设置页显示已收紧、未收紧、验证失败或文件不存在。启动也会尝试收紧已有凭据。POSIX 使用仅用户可读写权限。权限不等同于加密，也不能阻止管理员接管文件。

## 活动保护与清理

- `code=0` 但缺失详情不代表永久失效；账号风控和登录异常代码不能当作目标已删除。
- 补全、重抓、重试流程遇到不可访问活动时保留记录、标记 accessibility，并隔离参与队列。缺少活动详情经过有限重试后同样标记不可访问。刷新保留人工核查和修复审计。
- 直接 `purge_dynamic_ids` 不再删除；旧维护入口改为显式清理预览，详情补全使用正式刷新任务。
- 清理默认只读预览。`--apply` 读取已有预览，先备份并检查完整性，再核对所有目标指纹；任何目标变化或恢复访问时整体退出。只删除活动行，参与历史保留。

```powershell
.\.venv\Scripts\python.exe scripts/purge_all_dead_links.py --preview docs/reports/cleanup-preview.json
.\.venv\Scripts\python.exe scripts/purge_all_dead_links.py --preview docs/reports/cleanup-preview.json --apply --backup data/backups/before-cleanup.db
```

## 测试隔离

pytest 导入业务模块之前强制设置独立临时 HOME。数据库引擎、目录创建、日志、凭据写入及修复/清理工具验证隔离根：缺少根、指向真实目录或路径逃出临时根立即中止。模块级日志路径也随各测试隔离。

E2E 在首次导入前设置专用临时根，先验证路径再初始化。Playwright 不复用已运行服务器，且只连接固定隔离端口，防止测试附着真实应用。

## 评论提交

- 旧随机评论配置统一按自定义处理，界面取消随机借用入口。常规参与不再读取他人评论生成文案。
- 网页单条和批次参与先展示自己的完整转发/评论文案、活动链接和已知规则，要求核对原文。界面明确不会自动保证关键词、@好友等要求已满足。
- 预览签名绑定账号、目标、文案、规则及十分钟有效期。API 拒绝缺少有效预览的提交；执行前再次核对，提交使用预览中的原文案。用户取消时不创建参与任务。
- 需要评论的自动目标，只有该账号核查过相同文案与规则才能执行；文案或规则变化会再次进入待核查。批准不解除其他失败隔离。
- 命令行默认只输出预览；实际提交须同时给出 `--execute --confirm-rules`。这两个参数表示操作者已核对活动原文。

```powershell
.\.venv\Scripts\python.exe scripts/participate.py 动态ID --text "自己的文案"
.\.venv\Scripts\python.exe scripts/participate.py 动态ID --text "自己的文案" --execute --confirm-rules
```

## 验证记录

自动测试覆盖分享包排除凭据及旧备份、私人备份一致性、Windows ACL 实际验证与失败显示、测试越界阻断、只读清理预览与过期保护、不可访问活动刷新后仍保留、预览篡改/规则变化拒绝、API 固定确认文案和前端取消不提交。最终结果与本机权限检查见 `reports/release-safety-result-2026-10-09.json`。

2026-10-09 本机验证：现有 cookies.txt、llm.env 均收紧并通过 ACL 复查。旧文件已有当前用户所有权，因此只修改 DACL，不重复申请修改所有者权限。566 条活动及数据库内容摘要前后完全相同；未清理真实活动、未发出参与请求。可分享诊断 ZIP 已生成，数据库扫描标记 44 个疑似敏感单元格，仅导出位置和数量，源数据未修改。
