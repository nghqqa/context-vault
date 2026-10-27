# context-snapshot 格式规范 v0.1

> 状态：**定稿**（2026-10-27，四项开放问题已裁决，见 §8）· 上游文档：[BLUEPRINT.md](BLUEPRINT.md) §4
> 设计输入：本机真实会话文件实测（2026-10-27，样本见附录 C）+ txcript 六块模型 + stackness.dev 迁移丢失清单。
> 本格式是 Context Vault 的核心资产格式。设计目标：**与转换器（session-migrate/txcript）互操作，而非竞争**——做"资产层的中立存储单元"，转换器是它的 I/O。

---

## 1. 目标与非目标

### 目标
1. **供应商中立**：一个快照可以从任意 agent（Codex/Claude 系/Cursor/…）导出，语义上可重放进任意其他 agent。
2. **只存可移植内容**：加密推理、服务端状态等明确排除，并为排除项留下标记（见 §5 completeness）。
3. **内容寻址 + 幂等**：同一会话重复导出得到同一 `snapshot_id`，天然去重。
4. **账本内嵌**：每个快照自带 token 用量与成本钩子（字段与 Codex `token_usage_record` 实测对齐）。
5. **谱系（lineage）**：快照可分叉、可追溯，支持会话树。

### 非目标（诚实边界，写进规范防自欺）
- **不做无损往返**：跨 agent 迁移必然丢内容（实证：加密 reasoning、子代理转录、检查点快照）。本格式记录"丢了什么"，不假装没丢。
- **不做服务端状态搬运**：Responses API `previous_response_id`、服务端 KV cache 等一律改写为 stateless 重放语义。
- **不做格式转换**：agent 私有格式 ↔ 本格式的转换由适配器（含嵌入 session-migrate/txcript）完成，本规范只定义适配器的输出契约。

---

## 2. 信封（Envelope）

```jsonc
{
  "format_version": "0.1",
  "snapshot_id": "cv_<sha256[:16]>",       // 内容寻址：对 §2.1 规范化 JSON 求 sha256，取前 16 位十六进制
  "created_at": "2026-10-27T21:30:00+08:00", // 快照创建时刻（≠会话发生时刻）
  "capture": {                              // 捕获来源（取代 v0.1 草案时期的 "source"）
    "agent": "codex|claude-code|zcode|cursor|…",
    "agent_version": "0.158.0-alpha.2.1",   // 可选；格式漂移排查用（Claude Code 官方声明格式随版本变化）
    "source_format": "codex-rollout|claude-transcript|txcript|…",  // 适配器读的原始格式
    "provider": "relay:999555999|bigmodel|openai|…",  // 可选，未知为 null
    "model": "gpt-5.6-sol",                 // 可选
    "session_ref": "01a0e333-cb17-7fd0-8248-1da41d1c1014", // 供应商侧原始会话标识
    "cwd": "D:\\goai",                      // 可选；会话工作目录
    "originator": "Codex Desktop",          // 可选；区分用户主会话与内部机制会话 (实测存在审批子代理 rollout)
    "completeness": "full"                  // 见 §5
  },
  "conversation": [ /* 块数组，见 §3 */ ],
  "memory_hints": [                          // 可选；给记忆引擎的提取提示（关键决策/结论摘要）
    "…"
  ],                                         // 裁决 2026-10-27: v0.1 只留字段, M3 由记忆引擎回填
  "stats": { /* 见 §6 */ },
  "lineage": { "parent_snapshot": null, "fork_of": null }
}
```

### 2.1 规范化与内容寻址
- 计算 `snapshot_id` 时剔除 `created_at` 与 `lineage`（否则父链会让幂等失效）；`conversation`、`capture`、`stats`、`memory_hints`、`format_version` 参与 hash。
- 序列化规则：UTF-8、`ensure_ascii=false`、键按本规范出现顺序、无多余空白、`\n` 分隔符统一 LF。
- `capture` 允许附加扩展键（如适配器的 `notes`），消费方必须忽略未识别键。

---

## 3. 会话块（conversation blocks）

会话是**块数组**（非消息数组）。块模型与 txcript 六块对齐，另加两类有真实数据支撑的扩展块。

### 3.1 `text` —— 文本消息
```jsonc
{ "type": "text", "role": "user|assistant|developer|system",
  "content": "…", "ts": "2026-09-27T14:10:24.770Z" }
```
- **developer 注入（裁决 2026-10-27）：导出默认保留，迁移默认过滤。**
  vault 是完整审计资产（developer 块进快照，`cv export --drop-developer` 可丢）；
  迁移产物是可移植子集（import 端过滤 developer——它是源 agent 的环境提示，对目标 agent 无意义）。
  资产完整性与迁移瘦身是两个关注点，分开处理。

### 3.2 `thinking` —— 推理摘要（只留可移植部分）
```jsonc
{ "type": "thinking", "summary": "Reviewing repository instructions…", "ts": "…" }
```
- **只保留明文 summary**。Codex `encrypted_content`、Claude 的 thinking signature 一律丢弃（跨家不可移植，实证见 stackness 评测），不留占位字段。
- 多段 summary 合并为单块，段落用 `\n\n` 连接。

### 3.3 `tool_call` —— 工具调用
```jsonc
{ "type": "tool_call", "call_id": "call_JNGy…", "name": "exec",
  "namespace": "collaboration",           // 可选；Codex 多代理命名空间实测存在
  "arguments": { /* 对象或字符串 */ }, "ts": "…" }
```

### 3.4 `tool_result` —— 工具结果
```jsonc
{ "type": "tool_result", "call_id": "call_JNGy…", "content": "…",
  "is_error": false, "ts": "…" }
```
- `call_id` 与 3.3 配对；多块输出（Codex 实测 `output[]` 多元素）合并为单一 `content` 字符串，`\n---\n` 分隔。
- **截断阈值（裁决 2026-10-27）：单块 256KB**。超出时前后各保留 128KB 并打 `truncated` 标记；
  完整原文 M2 起入对象库（ref 与 §3.5 图片同规则）。

### 3.5 `image` —— 图片
```jsonc
{ "type": "image", "mime": "image/png", "ref": "cv_<hash16>", "ts": "…" }
```
- 图片字节不内嵌 JSON，存入 vault 对象库（内容寻址同款规则），块内只留 `ref`。
- **对象库规范（裁决 2026-10-27：规范先定，实现随 M2 SQLite refcount 落地）**：
  布局为 `vault/blobs/<hash2>/<hash>` 平铺内容寻址（前 2 位分桶）；SQLite 维护
  `blobs(hash PK, size, mime, refcount)` 表，快照注册时 +1、删除时 -1，refcount 归零即孤儿，
  由 `cv gc` 显式回收（永不自动删，防误删资产）。截断的 tool_result 全文（§3.4）共用此库。

### 3.6 `artifact` —— 工件（保留位）
本机样本暂未出现；为 txcript artifact 块预留同构字段 `{ "type":"artifact", "kind":"…", "ref":"cv_…" }`。

### 3.7 `agent_message` —— 多代理通信（扩展块）
```jsonc
{ "type": "agent_message", "author": "/root/design_review", "recipient": "/root",
  "content": "…", "ts": "…" }
```
Codex `response_item:agent_message` 实测存在。子代理转录能否整体导入由适配器决定；至少顶级会话中的代理间消息不丢。

### 3.8 `compaction` —— 压缩边界（扩展块）
```jsonc
{ "type": "compaction", "summary": "…", "replaced_blocks": [0, 336], "ts": "…" }
```
- Codex `compacted` 事件实测：`replacement_history` 可达数十块。**只记录边界与摘要，不展开替代历史**（体积与中立性）。`replaced_blocks` 是被压缩掉的块区间（含端点索引）。
- 重放语义：从最后一个 `compaction` 块之后的内容才是"活着"的上下文；之前的块仅供审计与检索。

---

## 4. 与 txcript 六块模型的映射（互操作承诺）

| txcript 块 | context-snapshot | 差异说明 |
|---|---|---|
| `text` | `text` | 我们显式携带 `role`；txcript 无角色语义时映射为 `role:"unknown"` |
| `thinking` | `thinking` | 双方同策略：只留可移植明文；签名/加密体丢弃 |
| `tool_call` | `tool_call` | 我们多一个可选 `namespace`（多代理），映射时置 null |
| `tool_result` | `tool_result` | 键名对齐 `call_id` |
| `image` | `image` | txcript 内嵌或引用均可映射到内容寻址 `ref` |
| `artifact` | `artifact` | 同构保留位 |
| — | `agent_message` | txcript 无此块；导出 txcript 时并入 `text`（`role:"agent"`） |
| — | `compaction` | txcript 无此块；txcript 流中若遇压缩标记按 3.8 生成 |

**承诺**：任何 txcript 流可无损导入 context-snapshot（六块全覆盖）；反向导出时 `agent_message`/`compaction` 有定义好的降级路径。这是 M1 公开 spec 时的核心互操作叙事。

---

## 5. `capture.completeness` —— 诚实标记

| 值 | 含义 |
|---|---|
| `full` | 全角色全内容 |
| `no_assistant_text` | 转录不含 assistant 文本（本机 Claude 系转录实测：仅 user/tool_use/tool_result） |
| `no_reasoning` | 源格式无推理内容 |
| `truncated` | 导出时按大小/条数截断（`stats.blocks_dropped` 记数量） |

消费方（迁移器/记忆引擎）必须先读此字段再决定可用性。**宁可标记缺失，不可假装完整。**

---

## 6. `stats` —— 账本钩子（与 Codex `token_usage_record` 实测对齐）

```jsonc
"stats": {
  "tokens_in": 27832,            // Σ input_tokens
  "tokens_out": 364,             // Σ output_tokens
  "reasoning_tokens": 136,       // Σ reasoning_output_tokens（可分离观测）
  "cache_read": 21248,           // Σ cached_input_tokens
  "cache_write": 0,              // Σ cache_write_input_tokens
  "total_tokens": 28196,         // Σ total_tokens（缺省 = 各项之和，不重复计 reasoning）
  "cost_usd": null,              // 可选；来源：账本（vault）估算或供应商回执，null = 未定价
  "usage_events": 1,             // 参与聚合的用量记录条数（溯源）
  "blocks_total": 338, "blocks_dropped": 0
}
```
- Codex 源直接从 `token_usage_record.thread_token_usage`（终值）或逐条聚合；**逐 turn 明细不入快照**，入 Vault 账本表（快照只存会话级汇总，账本持有明细，`snapshot_id` 外键关联）。
- Claude 系转录无用量数据：`usage_events: 0`，各项为 0，`completeness` 不受影响（用量缺失 ≠ 内容缺失）。

---

## 7. Lineage

- `parent_snapshot`：同一会话的前一快照（增量导出链）。
- `fork_of`：从某快照分叉（用户在历史节点开新分支）。
- 会话树 = 内容寻址 + 这两个指针，无需额外结构。

---

## 8. 裁决记录（2026-10-27 定稿）

| # | 问题 | 裁决 | 落点 |
|---|---|---|---|
| 1 | developer 注入内容取舍 | **导出默认保留（审计完整性），迁移默认过滤（可移植性）**；`--drop-developer` 可在导出时丢弃 | §3.1 |
| 2 | tool_result 截断阈值 | **256KB**（前后各留 128KB + truncated 标记），全文 M2 起入对象库 | §3.4 |
| 3 | memory_hints 生成方 | **M3 记忆引擎回填**（Supermemory/Graphiti 的自然输入端）；v0.1 只留字段 | §2 |
| 4 | 附件对象库 | **规范先定**（§3.5：平铺内容寻址 + blobs 表 refcount + `cv gc` 显式回收），**实现缓到 M2** | §3.5 |

---

## 附录 A · 源格式映射速查

**Codex rollout（`~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl`）**

> 实战警示（2026-10-27 逆向验证，双向迁移真机通过）：Codex 自家两代格式互不兼容——
> 0.153.4（CLI/VS Code 扩展）的 `ordinal`/时间戳/usage 全部是 **JSON 数字**；
> 0.158+（桌面端）写成**字符串**。codex-rs 的 serde 解析失败时会**静默跳过整行**，
> 表层错误是误导性的 "rollout is empty"。resume 走 SQLite thread-store
> （`thread_history_1.sqlite`），rollout 是回放源；文件名必须含 session UUID。
> 适配器以目标端实际版本的字段类型为准，且验证必须用知情测试（问上下文内容）。

| rollout 行 | 快照产物 |
|---|---|
| `session_meta` | `capture.{agent_version, session_ref, cwd, originator}`；`agent:"codex"` |
| `turn_context` | 不入块（cwd/sandbox 等进 capture 或丢弃） |
| `response_item:message` (role=user/assistant) | `text` |
| `response_item:message` (role=developer) | `text(role=developer)`（§8.1 开关） |
| `response_item:reasoning` | `thinking`（summary 拼接；**encrypted_content 丢弃**） |
| `response_item:function_call` / `custom_tool_call` | `tool_call`（namespace 取自 payload.namespace） |
| `response_item:function_call_output` / `custom_tool_call_output` | `tool_result` |
| `response_item:agent_message` | `agent_message` |
| `token_usage_record` | 聚合入 `stats` |
| `compacted` | `compaction` 块 |
| `world_state` / `inter_agent_communication_metadata` / `event_msg` 生命周期事件 | v0.1 丢弃（thread 重建层，快照以 response_item 为主干） |

**Claude 系 transcript（`~/.claude/transcripts/ses_*.jsonl`，实测 3 种行类型）**
| transcript 行 | 快照产物 |
|---|---|
| `user` | `text(role=user)` |
| `tool_use` | `tool_call`（call_id 由适配器合成，源无 ID） |
| `tool_result` | `tool_result` |
| （无 assistant 行） | `capture.completeness="no_assistant_text"` |

## 附录 B · 变更记录
- v0.1（2026-10-27）：初稿。基于 BLUEPRINT §4 草案修订：`source`→`capture`（含 completeness）；消息流改块模型；新增 `agent_message`/`compaction` 块；stats 与 Codex `token_usage_record` 字段对齐；新增 txcript 映射表与互操作承诺。
- v0.1 定稿（2026-10-27）：§8 四项裁决落入正文——developer 导出保留/迁移过滤（§3.1）、截断阈值 256KB（§3.4）、memory_hints 归 M3（§2）、对象库规范先定实现缓 M2（§3.5）；附录 A 补 Codex 两代格式不兼容实战警示（ordinal 类型陷阱）。

## 附录 C · 实测样本（2026-10-27，本机）
- Codex：`~/.codex/sessions/2026/09/27/rollout-…-01a0e333….jsonl`（195MB；session_meta×1, event_msg×9294, response_item×5733, token_usage_record×1593, compacted×39, turn_context×778, world_state×145, inter_agent_communication_metadata×28）
- Claude 系：`~/.claude/transcripts/ses_143a9cf12ffe8C1xzY5m7EfnOS.jsonl`（1.5MB；user×23, tool_use×108, tool_result×108）；全库 160 文件仅此 3 种行类型
