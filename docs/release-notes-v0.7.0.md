# Context Vault v0.7.0 — Core v2 / 检索与记忆内核升级

> **EN** | The retrieval and memory kernel is rewritten: code-aware weighted BM25 over a compact SQLite inverted index (-87% index size at thousand-snapshot scale, P95 39ms), lineage folding so one evolving session yields one hit, temporal memory that automatically supersedes stale decisions (stale-memory intrusion 0.0), a token-budgeted five-slot handoff brief, and a fully reproducible 162-query ablation benchmark (CodingVault-Bench) shipping with the repo.

> **中文** | 本版重写了检索与记忆内核：代码感知加权 BM25 跑在 SQLite 紧凑倒排上（千级快照索引体积 **-87%**、检索 **P95 39ms**），Lineage 折叠让同一演进链只回一个最新版命中，双时态记忆自动覆盖过期决策（**过期记忆入侵率 0.0**），交接简报支持 **token 预算五槽位装配**，并随仓库开源 **162 题可一键复现的消融基准 CodingVault-Bench**。

## ✨ Highlights / 亮点

### 🔍 Deep retrieval core / 深度检索内核
- **EN**: code-aware tokenizer (camelCase/PascalCase/snake_case/kebab-case/paths/error-codes — composite + subword dual indexing so `VaultIndex` and `vault_index` find each other, exact original writing ranks first); block-role-weighted BM25 (global IDF, length normalization, saturating snapshot aggregation kills the v0 tool_result flood); lineage folding; time decay; optional OpenAI-compatible dense rerank (`CV_EMBEDDINGS_URL`, off by default).
- **中文**：代码感知分词（复合词 + 子词双索引，精确原写法优先、跨写法照常召回）；块角色加权 BM25（全局 IDF + 长度归一 + 饱和聚合，终结 v0 时代 tool_result 刷分）；Lineage 折叠 + 时间衰减；可选稠密重排（缺省关闭零外发）。
- **Storage / 存储**: compact SQLite inverted index (`search_index.db`, zlib postings, chain-first-seen block dedup) replaces the ~800MB `index_cache.pkl`. Measured on 2,006 snapshots / 1.8GB: **816MB → 105.8MB (-87.0%), query P50 23.1ms / P95 39.0ms**; first serve/tray restart after upgrade auto-migrates (old pickle removed; it is derived data and rebuilds from snapshots).

### 🧠 Temporal memory / 双时态记忆 (default engine v2)
- **EN**: five kinds (decision / preference / summary / **pitfall** / **constraint**), every item provenance-cited (snapshot#block + real session time) with topic signatures; explicit re-decisions auto-mark earlier memories `superseded` — retrieval returns only currently-valid facts (stale intrusion 0.0 on the bench), `--history` / `include_history` traces the full A→B→C chain; cross-snapshot duplicates collapse into one confidence-scored entry.
- **中文**：五类别提取 + 出处（快照#块 + 会话真实时间）+ 主题签名；同槽位显式推翻自动把旧记忆标记 `superseded`——默认检索不再回过期事实；跨快照重复摘录合并记信心。v0.1 噪音闸门误杀含代码实体决策的缺陷已修复（规则引擎 v0/v0.1 原样保留作消融基线）。

### 📄 Budgeted handoff brief / 预算受限交接简报
- `--target markdown --budget-tokens 4000`: five utility slots (stop state / active decisions / disproven dead-ends / touched-file topology / live-context tail), repeated file reads and duplicate errors folded, compression stats attached. 4K budget on a >100K-token session: generated <100ms with ≥95% touched-file recall.

### 📊 CodingVault-Bench (open, reproducible / 公开可复现消融基准)
- 162 gold queries × 5 dimensions (single-session / multi-session / temporal update / handoff / abstention) over a seeded synthetic corpus (18 evolving chains, 3 fictional projects, zero secrets).
- Ablation (same corpus & queries, 162 gold): cross-writing retrieval R@1 0.69 (vanilla BM25) → **0.89** (v2 code-aware weighted BM25); abstention accuracy 0.46 (v0 set-overlap) → **0.92** (v2); version intrusion 0.86 → **0.0** (lineage folding); memory active recall 0.48 (v0) / 0.50 (v0.1, which false-kills 100% of technical decisions) → **0.88 with stale intrusion 0.0** (v2); handoff file-pass 0.92 (naive truncation) → **1.0** (five-slot, compression 0.42).
- One command: `python benchmarks/run_bench.py` · scale probe: `python benchmarks/bench_scale_local.py --vault <your vault>` · details: `docs/BENCHMARK.md`.

## 🔧 Upgrade notes / 升级说明
- **自动迁移**：升级后首次启动 serve/托盘会自动构建 `search_index.db` 并移除旧 `index_cache.pkl`（纯派生数据，可随时从快照重建）。
- **记忆**：`memory_items` 老库自动补列迁移；要切换到 v2 提取请跑一次 `cv memory extract --force --engine v2`（v0/v0.1 规则引擎仍可用 `--engine rule` 回溯对照）。
- MCP 工具新增 `include_history` 参数（`vault_remember`）；`cv memory search --history` 同语义。
- 纯 Python 3.9+ 标准库，零第三方依赖不变；快照只读铁律不变。

## ✅ Verification receipts / 验证回执
- pytest **124 green**（新增 33 条 v2 验收：冷热等价 / 防刷分 / 折叠 / A→B→C 推翻 / 老库迁移 / dense stub）· CI ubuntu+windows × py3.10/3.12 全绿
- 生产割接实测（2,010 快照 / 1.8GB）：索引 779.8MB → 100.9MB；记忆 2,078 → 2,751 条（active 2,747 / superseded 4，全带出处）
- 割接前完整备份：`cv-backup-20261118-*.zip`（533MB，随 keep=10 轮换）

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.4.2...v0.7.0
