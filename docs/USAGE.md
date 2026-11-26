# Context Vault 完整用户手册

本手册面向日常使用者，覆盖 **3 分钟极速上手**、**Web 控制台速查**，以及会话同步、跨工具迁移、交接简报、跨会话记忆、MCP 接入、本地 Token 账本、AML 标准适配与可选语义增强的完整用法。所有命令基于 [tools/cv.py](../tools/cv.py)（零第三方依赖，Python 3.9+）。

---

## 0. 3 分钟快速上手（90% 用户只需要这 3 步）

> **路径 A · Windows 绿色托盘版（免装 Python，14.5 MB 单文件双击即用）**
> 1. 前往 [GitHub Releases (v0.15.1)](https://github.com/nghqqa/context-vault/releases/tag/v0.15.1) 下载 [`ContextVault.exe`](https://github.com/nghqqa/context-vault/releases/download/v0.15.1/ContextVault.exe)（可用同页 `SHA256SUMS.txt` 校验：`certutil -hashfile ContextVault.exe SHA256`）。
> 2. 双击启动：如首次触发 Windows SmartScreen 未知发布者提示，点击「更多信息 → 仍要运行」即可。托盘图标出现在右下角系统托盘，双击图标打开 Web 控制台 `http://127.0.0.1:8791/`。
> 3. **免命令行用满全部功能**：首次空库会在概览顶部显示 **3 步引导卡**——点击即可完成全端会话同步、账本同步，并在「设置与运维」页按需勾选将 `ContextVault.exe --mcp` 接入 Claude Code / Codex / Cursor / Antigravity / DeepSeek Harness（全程无需安装 Python）。
> 4. **数据存放在哪 / 如何卸载**：首次在独立目录运行时自动初始化到 `~/.context-vault/vault`，并在 `~/.context-vault/config.json` 记录正本路径（移动 exe 位置也不丢数据）；右键托盘菜单提供「立即同步」「立即备份」「检查新版本」（Windows 冻结版支持 SHA-256 校验后自动更新重启）「开机自启」。如需彻底卸载：先取消勾选「开机自启」→ 退出托盘 → 删除 exe 及 `~/.context-vault/` 即可。

### 路径 B · 命令行安装（Windows / Linux / macOS，Python 3.9+ 零依赖）

#### 第一步：安装全局命令（可选，零第三方依赖）
```bash
git clone https://github.com/nghqqa/context-vault
cd context-vault
pip install -e .        # 注册全局命令 cv / cv-mcp / cv-aml（不装任何外部依赖）
```
> 若不想安装，直接把下文所有 `cv <命令>` 替换为 `python tools/cv.py <命令>` 即可。全局参数 `--vault <目录>` 需放在子命令之前（默认 `./vault`）。

### 第二步：一键初始化并接入 MCP
```bash
cv setup --mcp                       # 自动接入检测到主程序的客户端（跳过残留目录）
cv setup --mcp --mcp-clients claude,dsh  # 或显式指定要接入的客户端列表
```
这一条命令会自动完成：**建库 → 扫描同步本机全部会话（Claude Code / Codex / ZCode / Antigravity / DeepSeek Harness）→ 同步多端 token 账本 → 提取跨会话长期记忆 → 自动将 MCP 记忆后端写入已检测到主程序的客户端（跳过卸载残留目录）**。

### 第三步：启动本地控制台（自动打开浏览器）
```bash
cv serve --open
```
浏览器自动打开 `http://127.0.0.1:8791/`。之后在任意已接入 MCP 的客户端里直接问你的 Agent：
> *“关于 XX 我们之前定过什么方案？”*

### 按你想做的事情速查

| 我想做什么 | 最短入口 | 详细章节 |
|---|---|---|
| 在浏览器里浏览、检索、管理所有历史会话与配额 | `cv serve --open`（`Ctrl/Cmd+K` 全局联想） | [§2 Web 控制台速查](#2-web-控制台速查) |
| 把 Claude / Codex / ZCode / Antigravity / DSH 会话互转 | 控制台「迁移」页 或 `cv migrate <文件> --target codex` | [§4 跨工具迁移与交接简报](#4-跨工具迁移与交接简报) |
| 在各端间批量同步 `SKILL.md` 技能包 | 控制台「迁移 → Skill 技能迁移」或 `cv skills list` | [§4.5 跨工具 Skill 技能迁移](#45-跨工具-skill-技能迁移) |
| 让 Agent 记住我的决策、踩坑与架构红线 | `cv mcp install --all` / `--client <名>` + 记忆置顶 | [§5 跨会话记忆与 MCP 接入](#5-跨会话记忆与-mcp-接入) |
| 看 5h 额度窗口、冷却倒计时与各模型真实消耗 | 控制台「概览 → 用量与配额窗口」或 `cv ledger quota` | [§6 本地 Token 账本与额度监控](#6-本地-token-账本与额度监控) |
| 给第三方 Agent / 评测框架提供标准 Add/Search 接口 | `cv serve` 内置 `/aml/add` `/aml/search` 或 `cv-aml --serve` | [§7 AML 标准接口与容器部署](#7-aml-标准接口与容器部署) |
| 接入本地 Ollama 做语义向量重排 / LLM 记忆精炼 | `cv semantic setup --ollama` | [§8 可选语义增强层](#8-可选语义增强层ollama--本地-llm) |

---

## 1. 环境准备与两条安全铁律

### 1.1 前置要求
- **核心功能**：Python **3.9+**，**纯标准库零第三方依赖**（`setup` / `sync` / `migrate` / `skills` / `memory` / `ledger` / `serve` / `mcp` / `semantic` / `aml` / `backup` 全部开箱即用）。
- **Antigravity 与 DeepSeek Harness 原生读取**：内置零依赖 SQLite + Protobuf 直读器（支持 WAL 实时同步）；DeepSeek Harness (`~/.dsh/sessions`) 原生兼容 V4/V3/V2 `.jsonl` 及拼接式 `.jsonl.zstd`（通过 stdlib / PyPI / `node:zlib` / `zstd` 五级兜底链无缝编解码）。
- **18 种外部 harness 桥接**（仅 `ingest` 与 `export-harness` 需要）：安装 `session-migrate`（Python ≥ 3.11）：
  ```bash
  uv tool install session-migrate
  ```

### 1.2 两条安全铁律
1. **源会话文件只读**：`sync`、`export`、`ledger sync` 绝不修改各 Agent 写入的原始文件；派生数据全部存放在 `vault.db` / `search_index.db`。
2. **写入真实目录默认 dry-run**：`import`、`migrate`、`prune`、`sync-antigravity` 默认只预览或输出到 `vault/outbox/`，**必须显式追加 `--apply`** 才会写入真实目标目录。

### 1.3 环境体检
```bash
cv doctor        # 检查 Python 版本、本机各 Agent 会话目录、DSH Zstd 后端、MCP 接入摘要与数据库状态
cv init          # 如需显式初始化空 vault（setup / sync 也会按需自动初始化）
```

---

## 2. Web 控制台速查

运行 `cv serve --open` 或双击托盘图标后访问 `http://127.0.0.1:8791/`（仅绑本地回环，带 Host/Origin 跨站防护、自动 gzip 压缩与 SWR 后台预热，首屏并发渲染 < 100ms，浅色/深色/跟随三态主题）：

![Context Vault 概览视图](assets/console-overview-dark.png)

### 快捷键一览
- **`1` / `2` / `3` / `4`**：切换「概览」「快照资产」「迁移」「设置与运维」四大分区
- **`Ctrl + K` / `Cmd + K`**（或点击顶栏 `/ · ⌘K`）：打开**全局快速命令面板**，实时并发联想「跨会话记忆 + 历史快照」，`↑↓` 选择、`Enter` 直达详情抽屉
- **`/`**：快速聚焦顶部全局搜索框，`Enter` 跳转全库搜索
- **`Esc`**：关闭抽屉或命令面板

### 核心视图亮点
1. **概览页**：空库时自动呈现 3 步新手向导；4 格核心资产台账 + 最近入库会话 + 多端资产分布 + 最新记忆摘要 + **「用量与配额窗口」**（5h/24h/7d/30d 切换、4 列 KPI 带、智谱与 Antigravity IDE 实时配额胶囊条、5h 冷却倒计时 ETA、多维排行，并支持一键 **「导出 CSV」「导出 MD」** 报表）。
2. **快照资产页**：
   - **最近列表**：按来源胶囊筛选（全部 / Antigravity / ZCode / Codex / Claude Code / DeepSeek Harness），支持按时间/相关性排序、切换「仅最新版本 / 全部历史版本」与 **「不衰减 (`no_decay`)」** 开关、点击任意行打开右侧详情抽屉（代码块复制、工具调用折叠、会话内搜索高亮、不可变分叉与「迁移此快照 →」直达）。
   - **项目树**：按项目文件夹折叠主对话，来源筛选支持 `ZCode` / `Antigravity` / `Claude Code` / `Codex` / `DeepSeek Harness` 及库内动态发现的其他来源，支持整项或单条一键迁移，以及 Antigravity 免命令行双向清理/补齐。
   - **记忆库**：按「决策 / 偏好 / 踩坑 / 红线 / 压缩摘要」筛选；支持 **「+ 手动记一条」** 直接录入个人红线、**置顶优先**、**删除**、**增量提取** 与 **去重合并**。
3. **迁移工作台**：顶部可切换 **「会话迁移」** 与 **「Skill 技能迁移」**。会话迁移内置 **「单条精调 / 整端批量迁移」** 双粒度切换：单条支持 Codex / Claude Code / Antigravity / DeepSeek Harness (`dsh`) / ZCode / Markdown 交接简报（可选 Token 预算档位与一键复制正文）；**整端批量迁移** 支持按源 Harness（自动聚合 `claude` 系列）/ 项目目录 / 关键词盘点全部顶层主对话，自动跳过同源与 `migrations.jsonl` 已迁会话、单条异常不影响整批，ZCode 写回整批前仅备份 1 次主库（命中超 300 条预览上限时保持「全选整端待迁」即由后端全量处理）；Skill 迁移与「设置与运维」已接入 MCP 后端强一致——仅展示已接入且非零 Skill 的源端和已接入的目标端（自动隐藏 0 Skill 与未接入工具，切源撞同源时自动切换目标），每行可点击 **「预览 / 对比差异」** 查看 unified diff 与文件增删清单，支持 **「目标端管理与历史备份」**（移除或覆盖前自动完整备份至 `vault/backups/`，支持一键恢复），迁往 Antigravity 时可自由切换 **直写全局目录** 或 **打包为可一键开关的独立插件**。
4. **设置与运维页（`4`）**：免命令行管理 **MCP 客户端按需勾选与单项安全移除**（精确探测真实主程序并识别卸载残留目录，默认折叠未检测到/未接入客户端，移除时仅删除 `context-vault` 条目并自动备份 `.bak-cv`）、**本地 Ollama 语义重排与 LLM 记忆精炼/自动标题**、**智谱 Coding Plan Token 配置与多端账本同步**、**一致性 zip 备份管理（含历史清单逐份 CRC+SQLite 校验与下载）/ 中间版本裁剪**，以及全宽 **「审计与运维时间线」**（支持按全部 / 异常失败 / 模型切换 / 正常探测筛选）。

![Context Vault 设置与运维工作台](assets/console-ops-dark.png)

---

## 3. 会话同步、脱敏导出与浏览

### 3.1 增量扫描全部本地会话
```bash
# 扫描本机全部已支持的会话源（Claude / Codex / ZCode / Antigravity / DeepSeek Harness，含 WAL 与 V4 Zstd 崩溃截断恢复）
cv sync

# 额外指定自定义扫描目录（可多次传入）
cv sync --root D:\extra-sessions --root E:\archive

# 周期后台同步模式：活跃会话默认 30 分钟最多存一版（可通过 CV_SNAPSHOT_INTERVAL_MIN 调整）
cv sync --debounce
```

### 3.2 导出单个会话文件（支持敏感信息脱敏）
```bash
cv export ~/.claude/transcripts/ses_xxx.jsonl

# 过滤 developer 注入块
cv export ~/.claude/transcripts/ses_xxx.jsonl --drop-developer

# 导出前自动脱敏常见 API Key (sk-*) / GitHub Token / AWS Key / JWT / PEM 私钥块
# （也可全局设置环境变量 CV_REDACT_SECRETS=1 自动生效）
cv export ~/.claude/transcripts/ses_xxx.jsonl --redact
```

### 3.3 命令行浏览快照
```bash
cv list                                                # 列出快照
cv list --top                                          # 仅列出各项目顶层主对话（过滤历史版本与子代理）
cv list --top --project context-vault --agent codex    # 按项目或来源过滤（支持逗号分隔多来源）
cv list --top --agent antigravity                      # 查看 Antigravity 主对话
cv list --top --include-archived                       # 包含 ZCode 已归档会话
cv show cv_xxxxxxxx                                    # 打印单条快照详情
```

---

## 4. 跨工具迁移与交接简报

### 4.1 原生迁移（支持 `codex` / `claude` / `antigravity` / `dsh` / `zcode` / `markdown`）
推荐流程：**先 `list --top` 找快照 → `diff` 预检损益 → `import` dry-run → `--apply` 落盘**（或在控制台「迁移」页可视化完成）：

```bash
cv list --top                                          # 1. 找到要迁移的快照 ID
cv diff cv_xxxxxxxx                                    # 2. 预检保留项与不可迁移项
cv import cv_xxxxxxxx --target codex                   # 3. dry-run（产物落 vault/outbox/，零副作用）
cv import cv_xxxxxxxx --target codex --apply           # 4. 确认后写入目标真实会话目录
cv import cv_xxxxxxxx --target dsh --apply             # 或写为 DeepSeek Harness V4 .jsonl.zstd 并登记工作区
```

常用选项：
- `--strip-thinking`：不迁移 reasoning/thinking 摘要。
- **一步到位命令 `migrate`**（等价于 `export` → `diff` → `import`）：
  ```bash
  cv migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex
  cv migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex --apply --yes
  ```

### 4.2 整端全量 / 按项目批量导入与 Antigravity 维护
```bash
# 整端全量盘点与批量迁移（按来源 Harness 一键迁出，自动跳过已迁与同源，默认 dry-run）
cv import --agent claude --target codex
cv import --agent zcode --target dsh --apply
cv import --agent claude --target codex --no-skip-migrated --apply  # 强制重跑含已迁会话

# 按项目将顶层主对话批量导入 Antigravity（默认 dry-run）
cv import --project context-vault --target antigravity
cv import --project context-vault --agent zcode --limit 10 --target antigravity --apply

# 清理已导入 Antigravity 的 ZCode 已归档/已删会话或重复副本（建议先关闭 Antigravity）
cv sync-antigravity --project context-vault --dedup
cv sync-antigravity --project context-vault --dedup --apply
```

### 4.3 生成 Token 预算受限的 Markdown 交接简报
将长会话按五槽位效用（停靠态 / 关键裁决 / 已证伪死路 / 触碰文件拓扑 / 活上下文尾部）压缩成结构化简报：
```bash
# 建议预算：2000 | 4000 | 8000（缺省按 48KB 推导）
cv import cv_xxxxxxxx --target markdown --budget-tokens 4000
```

### 4.4 桥接 18 种外部 Harness（需 `session-migrate`）
```bash
cv ingest ./session.json --format opencode --cwd D:\my-project
cv export-harness cv_xxxxxxxx --to opencode
cv export-harness --project context-vault --to cursor --limit 5
```

### 4.5 跨工具 Skill 技能工作台（预览 Diff / 迁移 / 安全移除 / 一键回滚）
支持 `zcode`、`claude`、`codex`、`antigravity`、`cursor`、`dsh` 六端（Web 控制台会自动与「设置与运维 → MCP 接入」保持一致，隐藏未接入与 0 Skill 工具）：
```bash
cv skills list --source zcode                                                    # 盘点本地 Skill 与 System Prompt Token 开销
cv skills inspect academic-figure-prompt --source zcode --target antigravity     # 预览规范化 SKILL.md 与逐行 Diff / 附属文件变更
cv skills migrate --from zcode --to dsh                                          # dry-run 迁移报告（零改动磁盘）
cv skills migrate --from zcode --to antigravity --agy-mode plugin --apply        # 打包为可一键停用的 Antigravity 独立插件
cv skills remove --target antigravity --only archify --apply                     # 移除前自动完整备份到 vault/backups/ 并联动更新 plugin.json
cv skills backups                                                                # 查看 Skill 覆盖/移除前留存的历史备份清单
cv skills rollback skills-20261125-000001-remove --apply                         # 校验 manifest 安全路径后一键恢复原目标目录
```

---

## 5. 跨会话记忆与 MCP 接入

### 5.1 提取、检索与维护记忆
```bash
cv memory extract                                      # 增量提取决策/偏好/踩坑/红线/摘要（默认 v2 双时态引擎）
cv memory extract --snapshot cv_xxxxxxxx               # 仅提取单条快照
cv memory extract --force --engine rule                # 全量重提或切换引擎
cv memory search "token 预算" --limit 8 --project context-vault --since 7d  # 按项目与时间下界检索当前有效记忆
cv memory search "记忆引擎" --history                  # 追溯决策演变链（含已 superseded 历史记录）
cv memory list --limit 20 --project context-vault      # 按项目/类型/时间列出最近记忆（支持 --kind/--since/--history）
cv memory dedup                                        # 合并跨快照重复摘录
cv memory compile-rules --project context-vault --target agents  # 编译为 AGENTS.md/CLAUDE.md/.cursorrules（默认 dry-run；--apply 原地写入并备份 .bak-cv）
```
> 提示：可在 Web 控制台「快照资产 → 记忆库」中直接**手动新增、置顶优先、删除或一键编译项目规则**（立即对 MCP `vault_remember` 生效）。

### 5.2 按需接入与安全移除 MCP 客户端
支持 `claude`、`codex`、`cursor`、`antigravity`、`dsh`（DeepSeek Harness 注入 `$DSH_HOME/cordis.patch.yml`，全局跨 profile 生效）：
```bash
cv mcp status                            # 查看各客户端安装态与卸载残留识别
cv mcp install --all                     # 接入已检测到主程序的客户端（自动跳过仅残留配置目录）
cv mcp install --clients claude,dsh      # 或按需显式指定多个客户端接入
cv mcp uninstall --client cursor         # 安全摘除 context-vault 条目（支持 --clients a,b；自动 .bak-cv 备份）
```

接入后 Agent 获得 8 个纯本地 MCP 工具（检索类工具均支持 `project` / `agent` / `since` 作用域过滤与可选 `budget_tokens` 预算裁剪）：
- `vault_remember(query, limit?, kind?, project?, since?, include_history?, budget_tokens?)`：跨会话长期记忆（含 `#id`、出处与演变链）
- `vault_memorize(content, kind?, project?, pinned?, supersedes?)`：Agent 直接写入长期记忆（支持置顶与显式推翻旧 `#id`）
- `vault_handoff(id?, project?, budget_tokens?, include_memories?)`：生成带预算的五槽位跨工具交接简报（附当前项目有效记忆）
- `vault_context(query, limit?, project?, agent?, since?, no_decay?, budget_tokens?)`：与当前任务相关的历史对话摘录（支持关闭时间衰减）
- `vault_search(query, limit?, project?, agent?, since?, no_decay?, budget_tokens?)`：全库快照搜索（支持 `no_decay` 按原始相关度检索）
- `vault_recent(limit?)`：最近快照列表
- `vault_snapshot(id)`：单快照详情（来源/块分布/活上下文/token）
- `vault_stats()`：多端 token 账本汇总

---

## 6. 本地 Token 账本与额度监控

### 6.1 多端直采、5h 窗口查询与报表导出（纯离线，无需 headroom）
```bash
cv ledger sync                                         # 从 Claude/Codex/ZCode/Antigravity/DeepSeek Harness 日志增量采集
cv ledger sync --sources claude,dsh                    # 仅采集指定端
cv ledger sync --recompute                             # 修改 pricing.json / accounts.json 后重算存量事件（含 DeepSeek V4/Flash 费率）
cv ledger quota                                        # 查看 5h 滚动窗口、恢复 ETA、跑道与排行
cv ledger quota --window 7d --account my-pro --json    # 切换窗口 (5h|24h|7d|30d) 或输出 JSON
cv ledger export --format csv --window 30d --out report.csv      # 导出 CSV 账本报表 (支持 5h|24h|7d|30d|all)
cv ledger export --format markdown --window all --out report.md  # 导出 Markdown 报表（支持 --account 过滤）
```

### 6.2 账号额度池、厂商实时额度与静态面板
```bash
cv ledger limit --list                                 # 查看内置套餐预设与实时额度接入说明
cv ledger limit --name my-zhipu --preset max --tokens-5h 20000000 --source zcode --model-prefix "glm-*"
cv ledger plan-token "<your-coding-plan-token>"        # 配置智谱 Coding Plan 用户 Token（--clear 可清除）
cv ledger import-zcode                                 # 导入 ZCode model-io 审计日志
cv ledger import-relay --url https://api.example.com --mode billing --days 30 --key "$CV_RELAY_KEY"
cv ledger import-headroom                              # 导入旧 headroom 节省记录（可选）
cv ledger stats                                        # 账本汇总（--legacy-headroom 可看旧段落）
cv dashboard --open                                    # 生成并打开自包含 HTML 静态经济面板
```

---

## 7. AML 标准接口与容器部署

Context Vault 完整实现了 AML（Agent Memory Leaderboard）两段式 `Add / Search` 契约，支持多租户隔离（`user_id`）、显式 `budget_tokens` 裁剪、敏感信息脱敏（`redact`）、`request_id` / `Idempotency-Key` 幂等重放与 `409 Conflict` 冲突校验。

### 方式一：本地随 `cv serve` 统一提供（推荐，单进程零锁竞争）
启动 `cv serve` 后，直接挂载：
- `POST http://127.0.0.1:8791/aml/add`
- `POST http://127.0.0.1:8791/aml/search`

不传 `user_id` 时与主库共享同一个 `VaultIndex`，写入后在控制台和 MCP 中毫秒级立即可查；传 `user_id` 时自动按租户目录隔离。

### 方式二：独立 AML HTTP 服务或 Docker 容器
```bash
# 本地独立启动 (默认 127.0.0.1:8792)
cv-aml --serve --vault ./vault --port 8792

# Docker 零依赖部署 (默认监听 0.0.0.0:8000, 带 /health 健康检查与非 root 运行)
docker build -f Dockerfile.aml -t context-vault-aml .
docker run -p 8000:8000 -v $(pwd)/vault:/data/vault context-vault-aml
```

---

## 8. 可选语义增强层（Ollama / 本地 LLM）

默认完全关闭（零网络请求、零额外文件）。当本机装有 [Ollama](https://ollama.com/download) 或任意 OpenAI 兼容服务端时，可解锁**混合语义向量重排（RRF）**、**本地 LLM 隐式记忆精炼**和**自动会话标题清洗**：

```bash
# 1. 拉取本地模型（Ollama 示例）
ollama pull nomic-embed-text
ollama pull qwen2.5:7b

# 2. 一键写入持久化配置到 vault/ai_config.json（此后 serve / MCP / AML 自动生效）
cv semantic setup --ollama

# 或接入自定义 OpenAI 兼容端点
cv semantic setup --embeddings-url http://127.0.0.1:11434/v1/embeddings --embed-model nomic-embed-text \
                  --llm-url http://127.0.0.1:11434/v1/chat/completions --llm-model qwen2.5:7b

# 3. 查看状态（加 --probe 实时探活本机 Ollama 模型）
cv semantic status --probe

# 4. 用本地 LLM 精炼近期快照里的隐式决策/踩坑/红线（按水位幂等）
cv semantic refine --limit 10

# 5. 自动清洗低信息量会话标题（如 "看一下这个项目"；未装 Ollama 时也会走零依赖启发式提炼）
cv semantic titles --limit 30
cv semantic titles --limit 30 --heuristic-only
```

---

## 9. 常驻服务、上游哨兵与日常运维

```bash
# 常驻服务与鉴权（首访 http://127.0.0.1:8791/?token=xxx 自动种 cookie）
cv serve --open --port 8791 --token my-secret-token

# 上游哨兵 / 缓存预热 / 切换守望 / 审计回放
cv probe --profile myrelay --watch 300
cv warm --snapshot cv_xxxxxxxx --profile myrelay --cap 20000
cv watch --interval 2 --hours 8
cv audit --days 7 --kind all             # 支持 all|ok|switch|warm|model_missing|error|abnormal

# 快照分叉与谱系树
cv fork cv_xxxxxxxx --message "尝试方案 B"
cv tree cv_xxxxxxxx

# 一致性备份、清单/校验、中间版本 GC 与数据修复
cv backup --out D:\backups\cv --keep 14
cv backup --list                           # 列出全部本地备份并检查 zip 有效性
cv backup --verify                         # 校验最新备份 ZIP CRC-32 与 vault.db quick_check(1)
cv prune                  # 默认 dry-run 预览
cv prune --apply          # 执行前自动先做一次 backup
cv repair                 # 一次性 lineage 与历史用量回填（幂等）
```

---

## 10. 环境变量参考

| 变量名 | 用途 | 说明 / 示例 |
|---|---|---|
| `CV_SERVE_TOKEN` | `cv serve` 鉴权密钥 | 服务端与 MCP/脚本侧设为同值 |
| `CV_VAULT_SVC` | 显式覆盖常驻服务地址 | 默认由 `vault/serve.json` 自动发现 |
| `CV_REDACT_SECRETS` | 全局入库敏感信息脱敏 | 设为 `1` 时 `export` / AML `add` 自动对常见密钥打码 |
| `CV_EMBEDDINGS_URL` | 可选稠密向量端点 | 优先于 `vault/ai_config.json`；配置后 `/search` 与 AML 自动启用 RRF 重排 |
| `CV_EMBEDDINGS_MODEL` | 向量模型名 | 默认 `nomic-embed-text` |
| `CV_LLM_URL` | 本地/兼容 LLM 对话端点 | 供 `cv semantic refine` / `titles` 使用 |
| `CV_LLM_MODEL` | LLM 模型名 | 默认 `qwen2.5:7b` |
| `CV_OLLAMA_HOST` | Ollama 服务地址 | 默认 `http://127.0.0.1:11434` |
| `CV_AML_TOKEN` | 独立 `cv-aml` 服务鉴权令牌 | 支持 `Authorization: Bearer` 或 `X-API-Key` |
| `CV_SNAPSHOT_INTERVAL_MIN` | `sync --debounce` 最小快照间隔（分钟） | 默认 `30`；设为 `0` 关闭节流 |
| `CV_MEMORY_ENGINE` | 默认记忆提取引擎 | 默认 `v2`；可选 `rule` 或 `module:Class` |
| `CV_DECAY_FLOOR` | 检索时间衰减保底值 (`0.0`–`1.0`) | 默认 `0.65`（精确标识符/错误码查询自动抬高至 `0.80`） |
| `CV_PLAN_TOKEN` | 智谱 Coding Plan 用户 Token | 也可通过 `cv ledger plan-token` 持久化 |
| `CV_RELAY_PROFILES` | 无 headroom 时的 relay 配置 | JSON 数组，见 README 依赖边界说明 |
| `CV_RELAY_KEY` | `probe` / `warm` / `import-relay` API Key | 优先级高于 `--key` |
| `DSH_HOME` | DeepSeek Harness 主目录覆盖 | 默认 `~/.dsh`；同时影响会话、Skill 与 `cordis.patch.yml` |

---

## 11. 常见问题排查

1. **`ingest` / `export-harness` 提示缺少 `smigrate` / `session-migrate`**
   - 运行 `uv tool install session-migrate`（需 Python ≥ 3.11），再用 `cv doctor` 验证。
   - 注：同步本机 Antigravity / Claude / Codex / ZCode / DeepSeek Harness 会话均为**纯 Python 原生实现**，不依赖 `smigrate`。
2. **执行 `import` 或 `migrate` 后目标客户端里看不到新会话（或接入 MCP 后工具未出现）**
   - 检查是否漏加 `--apply`（不加 `--apply` 只写 `vault/outbox/` 而不写真实目录）。
   - 若目标为 **Antigravity**，执行 `import --apply` 或 `sync-antigravity --apply` 前请先完全退出 Antigravity 客户端，避免内存状态覆盖磁盘索引。
   - 若目标为 **DeepSeek Harness**，迁移会话与写入 `$DSH_HOME/cordis.patch.yml` 后**重启 DeepSeek Harness 桌面端**即可自动加载新会话与 `context-vault` MCP；若本机同时无 `zstandard` 与 `node` / `zstd`，`--apply` 写入 V4 `.jsonl.zstd` 时会明确提示安装其一。
3. **`cv serve` 控制台打开返回 401 / 403**
   - 若启用了 `--token` / `CV_SERVE_TOKEN`，首访请带 `?token=...`；另 `serve` 仅允许本机回环 Host/Origin，请通过 `http://127.0.0.1:8791` 访问。
4. **本地端口 `8791` 被系统保留导致无法绑定**
   - `cv serve` 会自动顺延到备用端口并写入 `vault/serve.json`，MCP 与托盘会自动发现实际端口；终端打印行也会显示实际可访问地址。
