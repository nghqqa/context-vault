# Context Vault

[![CI](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml/badge.svg)](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml)

> **给个人开发者的本地优先上下文资产库**：会话可迁移、token 账本可查证、上游变动早知道。

你的 AI 编程会话上下文是你最值钱的工作资产——但它锁在每家 agent 的私有格式里，换模型/换中转站/换工具就清零重来。Context Vault 把它变成**你自己机器上的中立资产**。

## 为什么是你、为什么是现在

- 79.7% 的开发者每周使用两种以上 AI 编程工具（Stack Overflow 2026）——上下文散落在各家。
- 国内 relay 用户月度级面对：模型下架、分组权限变动、倍率调价（本项目动机链里，一次真实的模型静默禁用造成了近一天的故障）。
- 市面上的迁移工具（txcript、session-migrate、Codex 官方 `/import`）都是**一次性搬运工**；Context Vault 是**资产库**——存储、谱系、账本、审计，一个位置。

## 三个核心能力

| 能力 | 命令 | 一句话 |
|---|---|---|
| **会话快照资产库** | `cv export` / `cv list` / `cv show` | 把 Codex / Claude Code 会话导出为供应商中立的 [context-snapshot](docs/snapshot-format.md) 格式，内容寻址、幂等去重、SQLite 索引 |
| **跨 agent 迁移** | `cv migrate` / `cv diff` / `cv import` | Claude Code ↔ Codex 双向，迁移前诚实预览（丢什么、留什么、活上下文多大），dry-run 默认、`--apply` 才落盘 |
| **token 账本 + 上游哨兵** | `cv ledger` / `cv probe` | headroom 账本一键导入（本机实测已累计节省 **$5,787**）；定时探测 relay `/v1/models`，模型消失即刻告警留痕 |

## 快速开始

```bash
# 零依赖，Python 3.9+，单文件
python tools/cv.py init
python tools/cv.py doctor                  # 环境体检

# 自动入库: 扫描全部本地会话源, mtime 增量 + 自动 lineage (会话树)
python tools/cv.py sync                  # 首次回填 173 个会话实测 1.6s, 二次 0.0s

# 导出会话为资产（只读源文件，幂等）
python tools/cv.py export ~/.codex/sessions/2026/10/20/rollout-xxxx.jsonl
python tools/cv.py export ~/.claude/transcripts/ses_xxxx.jsonl

# 一键迁移（导出 → 预览 → 导入，默认 dry-run）
python tools/cv.py migrate ~/.claude/transcripts/ses_xxxx.jsonl --target codex
python tools/cv.py import cv_xxxx --target codex --apply   # 确认后写入真实会话目录

# token 经济账本
python tools/cv.py ledger import-headroom  # 导入 headroom savings_events + lifetime
python tools/cv.py ledger stats

# 上游哨兵（模型消失 = 立即告警 + 数据库留痕）
python tools/cv.py probe                   # 单次
python tools/cv.py probe --watch 300       # 每 5 分钟守护

# 缓存预热 (切换上游前预发历史前缀, M2)
python tools/cv.py warm --snapshot cv_xxxx --profile myrelay
python tools/cv.py warm --snapshot cv_xxxx --profile myrelay --prefix-file AGENTS.md  # 精确前缀

# token 经济面板 (自包含 HTML, 零外部依赖)
python tools/cv.py dashboard --open

# 上游切换守望 (Switcher 换 profile 即弹"携带会话"提醒, 无侵入)
python tools/cv.py watch

# 常驻服务: 热索引 21x 加速 + 交互式控制台 (127.0.0.1:8791)
python tools/cv.py serve     # 浏览器打开 http://127.0.0.1:8791
```

### Web 控制台（`cv serve` 自带，127.0.0.1:8791）

- **概览**：累计节省卡片 / 月度走势 / 按模型账本（可筛选钻取的数据源）
- **快照资产**：全库搜索 + 点击展开会话详情（块分布 / 活上下文 / token）
- **迁移工作台**：选快照 → 诚实预览（不可迁移清单 + 活上下文估算）→ 生成 outbox 或一键写入目标 agent 会话目录
- **哨兵时间线**：探测 / 切换 / 模型消失全历史，一键立即探测

纯本地回环、单文件、零外部依赖。Pro 托盘版（ContextVault.exe）把控制台与哨兵/守望/热索引打包成 Windows 常驻程序。

# 接入任意其他 harness 会话 (经 session-migrate, 17+: opencode/cursor/qwen/kimi/grok/antigravity/...)
# 依赖: uv tool install session-migrate
python tools/cv.py ingest <会话文件> [--format opencode]

### 作为 MCP server 接入（任意 MCP 客户端 = 记忆后端）

```bash
claude mcp add context-vault -s user -- python /path/to/tools/mcp_server.py --vault /path/to/vault
# Codex / Cursor 等任何 MCP 客户端同理配置 stdio 命令
```

提供 5 个工具：`vault_search`（全库搜索）、`vault_context`（记忆检索——新会话开始时拉取相关历史上下文）、`vault_recent`、`vault_snapshot`、`vault_stats`（账本）。零第三方依赖。

一键接入全部客户端（claude CLI / Codex config.toml / Cursor mcp.json，写入前自动备份）：

```bash
python tools/cv.py mcp install --all
python tools/cv.py mcp status
```

## 真实数据验证（2026-10-27，本机）

- 195MB Codex rollout → **1.5 秒**导出 → 12MB 快照；39 个压缩边界、2.24 亿 token 用量、838 对工具调用全部保真
- **双向迁移真机验证全部通过**：
  - Codex → Claude：`claude --resume` (2.1.181) 加载迁移会话，准确概括原会话并续写（162→176 行）
  - Claude → Codex：`codex exec resume` (0.153.4) 恢复迁移会话，**373,160 tokens 上下文全量加载**，准确概括原工作内容
- 逆向工程发现并规避了 codex 两代格式的兼容性陷阱（`ordinal` 字段 0.153 数字 / 0.158 字符串互不兼容、resume 走 SQLite thread-store、静默跳过坏行报误导性 "rollout is empty"）
- 账本首批真实数据：压缩节省 $1,947 + 缓存节省 $3,840（35,383 次请求、5.4B tokens 流量）
- probe 实测发现：09-23 被禁的 `gpt-6-astra` 已悄悄重新上架——**模型下架又上架，只有留痕才知道发生过什么**

## 设计原则

1. **只读源、永不改写流量**——资产层与通路层（代理/网关）严格分离，删掉代理重装，Vault 一个字节不丢。
2. **诚实优于完整**——快照带 `completeness` 标记（如 Claude 转录不含 assistant 文本就明说）；迁移 diff 列出不可迁移项清单，不假装无损。
3. **stateless replay 语义**——服务端状态不可搬运，压缩边界之后才是活上下文，摘要注入为新会话首条消息。

## 依赖与数据源（架构边界）

**Context Vault 的核心功能零外部依赖**（纯 Python 标准库，不需要 headroom）：

- 会话快照资产库（export / list / diff / fork）
- 双向迁移（Claude Code ↔ Codex）
- MCP 记忆后端 + Web 控制台 + 热索引服务
- 快照自带的 token 账本钩子（直接提取自 Codex `token_usage_record` 等源文件）

**以下功能是 headroom（通路层代理）的增强，没有它也能用替代配置：**

| 功能 | headroom 提供 | 无 headroom 的替代 |
|---|---|---|
| 省钱账本导入 | `~/.headroom/savings_events.jsonl` + `proxy_savings.json` | 暂无（路线图：直连 relay usage API） |
| 上游探测/预热 的 relay 发现 | `switcher-profiles.json` | 环境变量 `CV_RELAY_PROFILES='[{"id":"x","openai_api_url":"https://…","default_model":"…"}]'` |
| 探测/预热 的认证 | `headroom-auth-token.ps1` 自动取 token | 环境变量 `CV_RELAY_KEY` |
| 切换守望 | 监听 Switcher 写的 `upstream-profile.json` | 任何程序写该文件均触发；或用 `cv watch --file` 监听任意配置 |

架构铁律：**headroom 卸载/停用，Vault 资产一个字节不丢**，仅上表增强项降级。账本历史一经导入即永久归属 vault。

## 与现有工具的关系

- **txcript / session-migrate / Codex `/import`**：转换器，一次性搬运。Context Vault 的快照格式与 txcript 六块模型有[互操作映射](docs/snapshot-format.md#4-与-txcript-六块模型的映射互操作承诺)；需要更多 harness（Cursor/opencode 等）时，它们是 Vault 的上游适配器。
- **headroom / LiteLLM 等代理**：通路层依赖，账本数据源。Vault 拥有数据、代理可整体替换。
- **Glean / Contextual AI**：企业云端 context 平台。Context Vault 是它的个人/本地反面。

## 路线图

- **M0 ✅** 格式 spec v0.1 定稿 · 双源导出 · 账本导入 · 可用性探测
- **M1 ✅** SQLite 资产库 · 双向迁移（双端真机验证）· diff 预览 · 一键迁移
- **M2** Vault 常驻服务 + **MCP server**（Claude Code/Codex/Cursor 天然接入）· 切换携带会话 · **缓存预热**（切换上游前预发历史前缀，省延迟与重算——市面无同类）· 账本面板
- **M3** 记忆引擎嵌入（Supermemory/Graphiti，存储归 Vault）· OTel 遥测出口 · 审计时间线回放

快照格式规范：[docs/snapshot-format.md](docs/snapshot-format.md)（context-snapshot v0.1，含 txcript 六块模型互操作映射）。

## 许可

代码 MIT · 格式规范 CC-BY（[snapshot-format.md](docs/snapshot-format.md)）。
开源格式换生态位；托盘哨兵、预热切换、面板等增值能力走 Pro。
