# Context Vault 完整用户手册

本手册面向日常使用者，覆盖安装体检、会话同步、跨工具迁移、交接简报、跨会话记忆、MCP 接入、本地 Token 账本与运维维护。所有命令与参数均基于 [tools/cv.py](file:///e:/context-vault/tools/cv.py)。

> 约定：以下示例统一使用 `python tools/cv.py <子命令>`。全局参数 `--vault <目录>` 可放在子命令之前（默认 `./vault`），例如 `python tools/cv.py --vault D:\my-vault list`。

---

## 1. 环境准备与快速体检

### 1.1 前置要求
* **核心功能**：Python **3.9+**，**零外部依赖**（纯标准库即可运行 `sync` / `migrate` / `memory` / `ledger` / `serve` / `mcp` / `backup`）。
* **18 种外部 harness 桥接**（仅 `ingest` 与 `export-harness` 需要）：需安装 `session-migrate`（Python ≥ 3.11）：
  ```bash
  uv tool install session-migrate
  ```

### 1.2 初始化与环境体检
```bash
# 检查 Python 环境、本地各 agent 会话目录、smigrate 与数据库状态
python tools/cv.py doctor

# 如需在自定义目录显式初始化空 vault（sync 等命令也会按需初始化）
python tools/cv.py --vault ./vault init
```

---

## 2. 两条安全铁律

1. **源会话文件只读**：`sync`、`export`、`ledger sync` 绝不修改各 agent 写入的原始文件。
2. **写入真实目录默认 dry-run**：`import`、`migrate`、`prune`、`sync-antigravity` 默认只预览或输出到 `vault/outbox/`，**必须显式追加 `--apply`** 才会写入真实目标目录。

---

## 3. 场景一：会话自动入库与浏览

### 3.1 增量扫描全部本地会话
```bash
# 扫描本机全部已支持的会话源（按 mtime 水位增量入库并自动建立 lineage）
python tools/cv.py sync

# 额外指定自定义扫描目录（可多次传入）
python tools/cv.py sync --root D:\extra-sessions --root E:\archive

# 周期后台同步模式：活跃会话默认 30 分钟最多存一版（可通过 CV_SNAPSHOT_INTERVAL_MIN 调整）
python tools/cv.py sync --debounce
```

### 3.2 导出单个指定会话文件
```bash
python tools/cv.py export ~/.claude/transcripts/ses_xxx.jsonl

# 过滤掉 developer 注入块（如环境上下文）
python tools/cv.py export ~/.claude/transcripts/ses_xxx.jsonl --drop-developer
```

### 3.3 查看快照列表与详情
```bash
# 列出 vault 内快照
python tools/cv.py list

# 仅列出各项目顶层主对话（过滤历史版本与子代理）
python tools/cv.py list --top

# 按项目路径/名称或来源 agent 过滤
python tools/cv.py list --top --project context-vault --agent codex
python tools/cv.py list --top --agent zcode,claude-code

# 包含子代理 / 包含 ZCode 已归档会话
python tools/cv.py list --top --include-subagents
python tools/cv.py list --top --include-archived

# 查看单条快照详情
python tools/cv.py show cv_xxxxxxxx
```

---

## 4. 场景二：跨工具迁移与交接简报

### 4.1 原生迁移（支持 `codex` / `claude` / `antigravity` / `zcode` / `markdown`）

推荐流程：**先 `list --top` 找快照 → `diff` 预检损益 → `import` dry-run → `--apply` 落盘**。

```bash
# 1. 找到要迁移的快照 ID
python tools/cv.py list --top

# 2. 迁移前预览（查看可保留项与不可迁移项）
python tools/cv.py diff cv_xxxxxxxx

# 3. dry-run 试跑（产物写入 vault/outbox/，不碰真实目录）
python tools/cv.py import cv_xxxxxxxx --target codex

# 4. 确认无误后写入目标 agent 的真实会话目录
python tools/cv.py import cv_xxxxxxxx --target codex --apply
```

常用迁移选项：
* `--strip-thinking`：不迁移 reasoning/thinking 摘要。
* **一步到位命令 `migrate`**（等价于 `export` → `diff` → `import`）：
  ```bash
  # 先预览
  python tools/cv.py migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex
  # 确认写入真实目录（--yes 跳过交互确认）
  python tools/cv.py migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex --apply --yes
  ```

### 4.2 按项目批量导入到目标端（如迁入 Antigravity）
```bash
# 将某项目的顶层主对话批量导入 Antigravity（默认 dry-run）
python tools/cv.py import --project context-vault --target antigravity

# 严格限定只迁移某来源 agent、限制数量并实际写入
python tools/cv.py import --project context-vault --agent zcode --limit 10 --target antigravity --apply
```

> **提示**：如果需要清理已经导入 Antigravity 的 ZCode 已归档/已删除会话或重复副本，可使用 `sync-antigravity`（建议操作前先关闭 Antigravity）：
> ```bash
> # 预览清理范围
> python tools/cv.py sync-antigravity --project context-vault --dedup
> # 实际执行清理
> python tools/cv.py sync-antigravity --project context-vault --dedup --apply
> ```

### 4.3 生成 Token 预算受限的 Markdown 交接简报
适合把长会话压缩成结构化简报，直接贴进新会话或发给协作者：
```bash
# 建议预算：2000 | 4000 | 8000（缺省按 48KB 推导）
python tools/cv.py import cv_xxxxxxxx --target markdown --budget-tokens 4000
```

### 4.4 桥接 18 种外部 Harness（需 `session-migrate`）
```bash
# 从外部格式接入 vault（支持 opencode/cursor/qwen/kimi/grok 等）
python tools/cv.py ingest ./session.json
# 若源格式存在歧义（如 opencode vs kilo），显式指定 --format 与 --cwd
python tools/cv.py ingest ./session.json --format opencode --cwd D:\my-project

# 从 vault 快照导出为任意目标 harness 格式
python tools/cv.py export-harness cv_xxxxxxxx --to opencode
# 也支持按项目批量导出
python tools/cv.py export-harness --project context-vault --to cursor --limit 5
```

---

## 5. 场景三：跨会话记忆与 MCP 接入

### 5.1 提取与检索记忆
```bash
# 增量提取决策/偏好/摘要/踩坑记忆（默认使用 v2 双时态引擎）
python tools/cv.py memory extract

# 仅处理单个快照 / 忽略水位全量重提 / 切换引擎（v2 | rule | module:Class）
python tools/cv.py memory extract --snapshot cv_xxxxxxxx
python tools/cv.py memory extract --force --engine rule

# 检索当前有效记忆（被推翻的旧决策默认不返回）
python tools/cv.py memory search "token 预算" --limit 8

# 追溯决策演变链（包含已 superseded 的历史记忆）
python tools/cv.py memory search "记忆引擎" --history

# 列出最近记忆 / 跨快照去重合并
python tools/cv.py memory list --limit 20 --history
python tools/cv.py memory dedup
```

### 5.2 一键接入 MCP 客户端
支持 `claude`、`codex`、`cursor`、`antigravity`：
```bash
# 查看各客户端当前 MCP 接入状态
python tools/cv.py mcp status

# 一键接入全部已安装的客户端
python tools/cv.py mcp install --all

# 或仅接入指定客户端
python tools/cv.py mcp install --client antigravity
```

接入后，agent 会获得以下 6 个本地工具：
* `vault_remember`：检索长期记忆（决策/结论/偏好，附带来源快照）
* `vault_context`：按当前任务检索相关历史摘录
* `vault_search`：全库文本检索
* `vault_recent`：列出最近快照
* `vault_snapshot`：查看单快照详情
* `vault_stats`：查看 token 账本汇总

---

## 6. 场景四：本地 Token 账本与额度监控

### 6.1 多端直采入账（纯本地离线，无需 headroom）
```bash
# 从 Claude / Codex / ZCode / Antigravity 本地日志增量采集到 usage_events
python tools/cv.py ledger sync

# 仅采集指定端（逗号分隔）
python tools/cv.py ledger sync --sources claude,codex

# 修改 pricing.json 或 accounts.json 后，强制重算存量事件费用与归属账号
python tools/cv.py ledger sync --recompute
```

### 6.2 查看 5 小时滚动窗口、恢复 ETA 与跑道
```bash
# 查看 5h 窗口（可选 5h | 24h | 7d | 30d）
python tools/cv.py ledger quota
python tools/cv.py ledger quota --window 7d

# 按账号过滤或输出 JSON 供脚本消费
python tools/cv.py ledger quota --account my-pro --json
```

### 6.3 配置账号额度池与厂商实时套餐额度
```bash
# 查看内置套餐预设与实时额度接入说明
python tools/cv.py ledger limit --list

# 写入自定义额度池到 accounts.json
python tools/cv.py ledger limit --name my-zhipu --preset max --tokens-5h 20000000 --source zcode --model-prefix "glm-*"

# 配置智谱 Coding Plan 用户 Token（从浏览器 F12 请求头 Authorization 获取，非普通 API Key）
python tools/cv.py ledger plan-token "<your-coding-plan-token>"
# 清除已存 Token
python tools/cv.py ledger plan-token --clear
```

### 6.4 导入 Relay / ZCode / Headroom 账本与可视化
```bash
# 从 ZCode model-io 审计日志入账
python tools/cv.py ledger import-zcode

# 从 OpenAI 兼容 relay 拉取 billing
python tools/cv.py ledger import-relay --url https://api.example.com --mode billing --days 30 --key "$CV_RELAY_KEY"

# 从 new-api 系 web 日志拉取（需系统访问令牌）
python tools/cv.py ledger import-relay --url https://api.example.com --mode web --token "<token>" --user-id 1

# 导入旧 headroom 节省记录
python tools/cv.py ledger import-headroom

# 打印账本汇总（加 --legacy-headroom 可显示旧 headroom 段落）
python tools/cv.py ledger stats

# 生成自包含 HTML 经济面板并立即在浏览器打开
python tools/cv.py dashboard --open
```

---

## 7. 场景五：常驻服务、上游哨兵与运维维护

### 7.1 常驻服务与 Web 控制台
```bash
# 启动热索引与 Web 控制台（默认仅绑定 127.0.0.1:8791）
python tools/cv.py serve

# 自定义端口并启用 Token 鉴权（也可通过 CV_SERVE_TOKEN 环境变量）
python tools/cv.py serve --port 8791 --token my-secret-token
```
启用鉴权后：浏览器首次访问使用 `http://127.0.0.1:8791/?token=my-secret-token` 自动登录；脚本/MCP 侧在请求头传 `X-Vault-Token`（或设置同值 `CV_SERVE_TOKEN`）。

### 7.2 上游探测、缓存预热、切换守望与审计
```bash
# 探测 relay /v1/models 可用性（--watch 300 表示每 300 秒循环探测）
python tools/cv.py probe --profile myrelay --watch 300

# 切换上游前预发历史前缀进行缓存预热
python tools/cv.py warm --snapshot cv_xxxxxxxx --profile myrelay --cap 20000

# 监听 Switcher 上游配置文件变化并提醒携带活跃会话
python tools/cv.py watch --interval 2 --hours 8

# 回放审计时间线（kind: all | ok | switch | warm | model_missing）
python tools/cv.py audit --days 7 --kind all
```

### 7.3 快照分叉与谱系树
```bash
# 在指定快照节点开分支
python tools/cv.py fork cv_xxxxxxxx --message "尝试方案 B"

# 查看演进链与 fork 子树
python tools/cv.py tree cv_xxxxxxxx
```

### 7.4 备份、版本裁剪与修复
```bash
# 一致性备份（sqlite 在线快照 + snapshots 打包，默认保留最近 10 份在 vault/backups）
python tools/cv.py backup
python tools/cv.py backup --out D:\backups\cv --keep 14

# 中间版本 GC：每会话每天保留最后一版 + 叶子；fork/孤本整链保留（默认 dry-run）
python tools/cv.py prune
python tools/cv.py prune --apply   # 执行前会自动先做一次 backup

# 一次性数据修复：lineage 索引回填 + zcode 历史用量回填（幂等）
python tools/cv.py repair
```

---

## 8. 环境变量参考

| 变量名 | 用途 | 说明 / 示例 |
|---|---|---|
| `CV_SERVE_TOKEN` | `cv serve` 鉴权密钥 | 服务端与 MCP/脚本侧设为同值 |
| `CV_VAULT_SVC` | 显式覆盖常驻服务地址 | 默认由 `vault/serve.json` 自动发现 |
| `CV_RELAY_PROFILES` | 无 headroom 时的 relay 配置 | JSON 数组，如 `'[{"id":"myrelay","openai_api_url":"https://host","default_model":"m"}]'` |
| `CV_RELAY_KEY` | `probe` / `warm` / `import-relay` API Key | 优先级高于 `--key` |
| `CV_RELAY_WEB_TOKEN` | `import-relay --mode web` 访问令牌 | 也可直接用 `--token` 传入 |
| `CV_SNAPSHOT_INTERVAL_MIN` | `sync --debounce` 最小快照间隔（分钟） | 默认 `30`；设为 `0` 关闭节流 |
| `CV_MEMORY_ENGINE` | 默认记忆提取引擎 | 默认 `v2`；可选 `rule` 或 `module:Class` |
| `CV_PLAN_TOKEN` | 智谱 Coding Plan Token | 也可通过 `cv ledger plan-token` 持久化配置 |
| `CV_EMBEDDINGS_URL` | 可选稠密向量重排服务地址 | 配置后在检索启用 `hybrid=1` 时生效 |

---

## 9. 常见问题排查

1. **`ingest` / `export-harness` 提示缺少 `smigrate` / `session-migrate`**
   * 运行 `uv tool install session-migrate`（需 Python ≥ 3.11），再用 `python tools/cv.py doctor` 验证。
2. **执行 `import` 或 `migrate` 后目标客户端里看不到新会话**
   * 检查是否忘记加 `--apply`（不加 `--apply` 只写 `vault/outbox/` 而不写真实目录）。
   * 若导入目标为 **Antigravity**，执行 `import --apply` 或 `sync-antigravity --apply` 时请先关闭 Antigravity 客户端，避免内存状态覆盖索引。
3. **`probe` / `warm` / `import-relay` 提示无 API key**
   * 确认已设置 `CV_RELAY_KEY` 环境变量、显式传入 `--key`，或已配置 `headroom-auth-token.ps1`。
4. **`cv serve` 控制台打开返回 401 / 403**
   * 检查是否启用了 `--token` / `CV_SERVE_TOKEN`（首访需带 `?token=...`）；另外 `serve` 仅允许本机回环 Host/Origin 访问，请使用 `http://127.0.0.1:8791` 访问。
