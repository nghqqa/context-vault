**简体中文** | [**English**](README.md)

# Context Vault

[官网](https://nghqqa.github.io/context-vault/) · [![CI](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml/badge.svg)](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nghqqa/context-vault)](https://github.com/nghqqa/context-vault/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> **给个人开发者的本地优先上下文资产库**：快照你拥有、迁移说实话、账本可审计、记忆带出处。

你的 AI 编程会话是你最值钱的工作资产——但它锁在每家 agent 的私有格式里，换模型/换中转站/换工具就清零重来。Context Vault 把它变成**你自己机器上的中立资产**：agent 来来去去，vault 一直在。

- 79.7% 的开发者每周使用两种以上 AI 编程工具（Stack Overflow 2026）——上下文散落各家。
- 市面上的迁移工具全是**一次性搬运工**；没有一个是**资产库**（持久存储、内容寻址、谱系、账本、审计）。
- 锁定问题不会自己消失：厂商从中受益。

## ✨ 核心能力

- **快照资产库**——把 Codex / Claude Code / 19 种桥接 harness 的会话导出为供应商中立的 [context-snapshot](docs/snapshot-format.md) 格式：内容寻址、不可变、幂等、自动 lineage（会话树与 fork）。
- **交接简报档**——`--target markdown` 生成 ≤48KB 可携交接简报（停在哪/最近动作/活上下文尾部），任何 agent 都能吃。
- **vault 备份**——`cv backup`：一致性快照 + 轮换 zip；常驻控制台可选 `CV_SERVE_TOKEN` 鉴权。
- **诚实迁移**——Codex ⇄ Claude Code 双向（双端真机验证），默认 dry-run，每次迁移产出"什么保住了/什么丢了"的完整报告。
- **token 账本**——从你自己会话文件内嵌的 usage 记录聚合出按模型成本、缓存命中率、5 小时窗口视图，不需要任何代理。
- **跨会话记忆**——从历史自动提取决策/偏好/摘要，`MemoryEngine` 接口可插拔；每条记忆标注来源快照。
- **MCP 记忆后端**——6 个工具让任意 MCP 客户端（Claude Code / Codex / Cursor）变成"记得住的 agent"，一条命令全客户端接入。
- **上游哨兵**——定时探测 relay `/v1/models`，模型静默消失即刻告警并留审计痕。

## 🚀 快速开始

```bash
python tools/cv.py doctor          # 环境体检（零依赖，Python 3.9+）
python tools/cv.py sync            # 自动入库: 扫描全部本地会话源 (增量 + lineage)
python tools/cv.py serve           # Web 控制台 http://127.0.0.1:8791
python tools/cv.py mcp install --all   # claude / codex / cursor 一键接入 MCP
```

迁移（默认 dry-run，不加 `--apply` 绝不碰真实会话目录）：

```bash
python tools/cv.py migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex
python tools/cv.py import cv_xxxx --target codex --apply
```

更多：`cv ingest <会话文件>`（桥接读 19 种格式，经 session-migrate）、`cv export-harness cv_xxxx --to opencode`（桥接写 18 个目标）、`cv memory extract`（记忆提取）、`cv probe --watch 300`（哨兵）、`cv backup`（一致性备份轮换）、`cv dashboard --open`（自包含面板）。

## 📦 迁移保真度表（What survives）

诚实优先——这是完整清单，不是精选集：

| 数据 | 状态 | 说明 |
|---|---|---|
| 对话与配对的工具调用/结果 | ✅ 迁移 | 未配对的结果被丢弃并**逐条计数**进报告 |
| 思考摘要（reasoning/thinking） | ⚠️ 部分 | 签名与加密内容不可移植——按[规范 §3.2](docs/snapshot-format.md) 丢弃 |
| 压缩边界（compaction） | ✅ 迁移 | 转为摘要块；stateless-replay 语义 |
| developer 注入块（env 等） | ❌ 过滤 | 由目标 agent 运行时重新注入 |
| token 用量记录 | ✅ 迁移 | 进账本，不进迁移会话 |
| 子代理转录 · 图片 | ❌ 暂不支持 | |
| env / hooks / MCP 配置 / 权限 | ❌ 留在源端 | 搬不走，也不该搬 |

## 🧠 记忆后端 —— API 一览

| MCP 工具 | 返回 |
|---|---|
| `vault_remember(query)` | 长期记忆：历史决策/项目结论/用户偏好（每条标注来源快照） |
| `vault_context(query)` | 与当前任务相关的历史摘录 |
| `vault_search(query)` | 全库文本搜索 |
| `vault_recent(limit)` | 最近快照列表 |
| `vault_snapshot(id)` | 单快照详情（块分布/活上下文/token） |
| `vault_stats()` | token 账本汇总 |

新任务开始时问你的 agent 一句："关于 X 我们之前定过什么？"——它查的是你自己的历史，本地。

## 🗄️ 工作原理

```
vault/
├── snapshots/cv_*.json   # 不可变, 内容寻址 (sha256 → id); lineage 在信封内
├── vault.db              # 索引: 搜索词项/账本/记忆/审计/同步水位
├── outbox/               # dry-run 迁移产物 (绝不碰真实目录)
└── backups/              # cv backup: 一致性 zip, 自动轮换
```

两条铁律不打折：**快照不可变**（任何派生数据住索引层，绝不回写快照）；**源会话文件只读**（vault 永不修改 agent 写下的东西）。`cv serve` 只绑回环地址，校验 Host/Origin、写入强制 JSON content-type——本地进程畅通，恶意网页 403。

## 真实数据验证（2026-10-27/29，本机）

- 195MB Codex rollout → **1.5 秒**导出 → 12MB 快照；39 个压缩边界、2.24 亿 token 用量、838 对工具调用全部保真。
- **双向迁移真机验证全部通过**：Codex→Claude 在 2.1.181 里 resume 续写；Claude→Codex 在 0.153.4 里恢复 **373,160 tokens 全量上下文**。
- 逆向并规避 codex 两代格式陷阱（`ordinal` 数字/字符串互不兼容、resume 走 SQLite thread-store、"rollout is empty" 误导性报错）。
- 测试 **22 用例**，含冷热双路径等价测试——常驻索引与文件扫描回退必须结果一致（"常驻只是加速，不是依赖"被写进了测试）。

## 设计原则

1. **只读源、永不改写流量**——资产层与通路层（代理/网关）严格分离；代理删了重装，Vault 一个字节不丢。
2. **诚实优于完整**——快照带 `completeness` 标记；迁移 diff 列出不可迁移项；账本估算标注为估算。
3. **stateless replay**——服务端状态不可搬运；活上下文 = 最后一个压缩边界之后的内容，摘要注入为新会话首条消息。

## 依赖与数据源（架构边界）

**核心功能零外部依赖**（纯 Python 标准库，CLI 单文件）。token 账本直接提取自你 会话文件自带的 usage 记录（实测本机 749 快照 / 数亿 tokens / 缓存命中率 68% 量级——全部零外部依赖）。headroom 的节省**金额**是增强项，未安装时显示接入引导。

| 功能 | headroom 提供 | 无 headroom 的替代 |
|---|---|---|
| 省钱账本导入 | `~/.headroom/savings_events.jsonl` + `proxy_savings.json` | 暂无（路线图：直连 relay usage API） |
| 上游探测/预热的 relay 发现 | `switcher-profiles.json` | 环境变量 `CV_RELAY_PROFILES='[{"id":"x","openai_api_url":"https://…","default_model":"…"}]'` |
| 探测/预热的认证 | `headroom-auth-token.ps1` 自动取 token | 环境变量 `CV_RELAY_KEY` |
| 切换守望 | 监听 Switcher 写的 `upstream-profile.json` | 任何程序写该文件均触发；或 `cv watch --file` 监听任意配置 |

架构铁律：**headroom 卸载/停用，Vault 资产一个字节不丢**，仅上表增强项降级。账本历史一经导入即永久归属 vault。

## 🆚 与同类的关系

| | Context Vault | 搬运工（session-migrate · txcript · `/import`） | 记忆层（Mem0 · Letta） | 代理（headroom · LiteLLM） |
|---|---|---|---|---|
| 层次 | **资产库** | 转换器 | 记忆 | 通路 |
| 不可变内容寻址存储 + lineage | ✅ | — | — | — |
| 双向迁移 | ✅ 原生 2 + 桥接 18 | ✅ | — | — |
| 从会话文件出账本 | ✅ | — | — | 仅自家流量 |
| 带出处的记忆 | ✅（规则引擎 v0，可插拔） | — | ✅ | — |
| 开放格式规范 | ✅ [CC-BY](docs/snapshot-format.md)（含 txcript 映射） | txcript `Simple` | — | — |

搬运工是上游适配器而非竞品——`cv ingest` / `cv export-harness` 的 19 格式桥接正是驱动 session-migrate 实现的。Vault 拥有它们不做的部分：存储、谱系、账本、审计。

## harness 支持矩阵

| 通道 | 覆盖 |
|---|---|
| 原生读+写（真机验证） | Codex · Claude Code · ZCode 转录 |
| 桥接读（`cv ingest`，19 格式） | opencode · cursor · qwen · kimi · grok · antigravity · copilot · devin · hermes · kilo · mastracode · muse · omp · pi · vibe · openhands … |
| 桥接写（`cv export-harness`，18 目标） | 同上清单 |
| 账本数据源 | headroom · relay billing API · ZCode 用量审计 |
| 无法支持 | 加密存储的 harness（如 Windsurf——本地会话加密，无格式可适配） |

## 路线图

- **M0 ✅** 格式 spec v0.1 · 双源导出 · 账本导入 · 可用性探测
- **M1 ✅** SQLite 资产库 · 双向迁移（双端真机验证）· diff 预览
- **M2 ✅** 常驻服务 + Web 控制台 · MCP 后端 · 缓存预热 · 哨兵/守望 · 19 格式桥接接入
- **M3 ✅** 记忆层（引擎可插拔）· fork/谱系 · 审计时间线
- **下一步** 发布与反馈 · 记忆出处 span · 可选加密

## FAQ

**和直接翻 `~/.claude/projects` 有什么区别？** 你可以——一次性、单一格式、单机。Vault 给你的是跨 harness、跨月份的统一可查询存储：会话演进的 lineage、上游变动的审计痕、以及全部历史之上的记忆提取。

**有数据离开我的机器吗？** 没有。无遥测、无账号、核心功能不需要任何 API key。`serve` 只绑 127.0.0.1；`probe` 只访问你配置的 relay 端点。

**免费吗？** 核心 MIT，永久。另有闭源 Windows 托盘伴侣（自动入库/每日备份/常驻控制台）；开源核心离开它完整可用。

## 贡献

Issue 与 PR 欢迎。格式规范的讨论是最高价值的贡献——[snapshot-format.md](docs/snapshot-format.md) 才是产品。

## 许可

代码 **MIT** · 格式规范 **CC-BY**（[snapshot-format.md](docs/snapshot-format.md)）。开源格式换生态位；伴侣便利养活开发。
