#!/usr/bin/env python3
"""CodingVault-Bench 语料 + 金标生成器 (固定 seed, 完全可复现)。

产出 (随仓库提交, 评测零网络零外部依赖):
- benchmarks/coding_vault_bench/corpus/cv_bench_*.json  — 18 条演进会话链 (context-snapshot 形态)
- benchmarks/coding_vault_bench/queries/*.jsonl         — 5 维度 ≥120 条人工级精标金标

反作弊声明 (Zero Data Leakage):
- 本文件是唯一真源: 查询由「链定义参数」推导 (哪个会话改了什么文件/哪次裁决推翻了什么),
  绝不从生成文本中反向抽取; 分词器/提取器代码不引用任何查询词。
- 语料为合成脱敏: 三个虚构项目 (phoenix-api / quartz-ui / cobalt-sync), 路径全部虚构
  (D:\\work\\...), 零真实密钥、零本机私人路径; 生成后由 run_bench.py 做泄漏扫描。

运行: python benchmarks/build_corpus.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20261118
OUT = Path(__file__).resolve().parent / "coding_vault_bench"
NOISE = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
         "incididunt ut labore et dolore magna aliqua ut enim ad minim veniam quis").split()

# ================================================================ 链定义 (金标真源)

# 每条链: 项目 / 虚构 cwd / 主题 / 版本列表; 版本内 events 由模板展开。
# decision_slots: 该链的裁决槽位 — 值随版本演进 (A→B→C), 用于 Temporal 维度与 supersede。
# files: 该链触碰的文件 (handoff 维度金标); pitfalls: 踩坑句 (handoff/记忆金标);
# tokens: 会话特异命令/参数/错误码 (single_session 维度金标)。
CHAINS = [
    # ---------------- phoenix-api (支付网关, 7 链) ----------------
    dict(id="phoenix-relay-retry", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="支付回调 relay 重试策略", versions=6,
         files=["relay/retry_policy.py", "relay/schedule.py", "config/retry.yaml", "relay/metrics.py", "tests/test_retry.py", "docs/retry_runbook.md"],
         tokens=["ERR_502_RELAY_TIMEOUT", "--drain-timeout 90", "retry_budget=0.3"],
         slots=[("retry.yaml", ["fixed_1s", "exponential_jitter", "circuit_breaker"])],
         pitfalls=["retry.yaml 全量重发会在风暴时放大故障, 此路已验证无效"]),
    dict(id="phoenix-webhook-sign", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="webhook 签名校验迁移", versions=5,
         files=["webhook/verify.py", "webhook/routes.py", "webhook/tests_sign.py", "docs/webhook_runbook.md", "config/keys.env"],
         tokens=["SIG_V2_HMAC", "nonce_window=300", "ERR_SIG_MISMATCH"],
         slots=[("verify.py", ["hmac_sha1_legacy", "hmac_sha256_constant_time", "rotating_key_v3"])],
         pitfalls=["字符串直接比较签名存在时序侧信道, 旧实现必须废弃"]),
    dict(id="phoenix-refund-recon", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="退款对账批处理", versions=5,
         files=["refund/reconcile.py", "refund/report.csv", "refund/tests_recon.py", "docs/recon_runbook.md"],
         tokens=["recon_batch=5000", "ERR_AMOUNT_MISMATCH", "--dry-run-refund"],
         slots=[("reconcile.py", ["nightly_full", "hourly_incremental", "streaming_recon"])],
         pitfalls=["夜间全量对账在退款高峰日超窗, 增量方案才可行"]),
    dict(id="phoenix-idempotency", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="幂等键存储选型", versions=6,
         files=["idem/store.py", "idem/ttl.py", "idem/tests_idem.py", "migrations/0001_idem.sql", "docs/idem_notes.md"],
         tokens=["idem_key_ttl=86400", "ERR_DUP_REQUEST", "UNIQUE(idem_key)"],
         slots=[("store.py", ["redis_setnx", "sqlite_unique_index", "postgres_upsert"])],
         pitfalls=["redis SETNX 无 TTL 会让死键永久堆积, 必须带过期"]),
    dict(id="phoenix-ledger-migration", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="账本表双写迁移", versions=7,
         files=["ledger/dual_write.py", "ledger/backfill.sql", "ledger/cutover.md", "ledger/tests_dw.py", "scripts/cutover.sh", "docs/cutover_plan.md"],
         tokens=["backfill_chunk=10000", "ERR_CUTOVER_DIRTY", "--verify-ledger"],
         slots=[("cutover.md", ["dual_write_shadow", "read_switch_10pct", "full_cutover"])],
         pitfalls=["直接切换不回填会让历史行缺失, 必须先 backfill"]),
    dict(id="phoenix-rate-limiter", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="商户级限流器", versions=5,
         files=["ratelimit/token_bucket.py", "ratelimit/rules.json", "ratelimit/tests_rl.py", "config/limits.json", "docs/rl_notes.md"],
         tokens=["bucket_cap=200", "ERR_RATE_LIMITED", "refill_rate=50/s"],
         slots=[("rules.json", ["global_limit", "merchant_tiered", "adaptive_burst"])],
         pitfalls=["全局统一限额会饿死大商户, 分层才公平"]),
    dict(id="phoenix-sdk-client", project="phoenix-api", cwd="D:\\work\\phoenix-api",
         topic="商户 SDK 客户端重试语义", versions=5,
         files=["sdk/client.py", "sdk/errors.py", "sdk/tests_client.py", "docs/sdk_errors.md"],
         tokens=["SDK_MAX_RETRIES=3", "ERR_SDK_TIMEOUT", "backoff_base_ms=250"],
         slots=[("client.py", ["retry_all", "retry_idempotent_only", "retry_safe_methods"])],
         pitfalls=["对非幂等接口盲目重试会造成重复扣款"]),
    # ---------------- quartz-ui (前端组件库, 6 链) ----------------
    dict(id="quartz-virtual-table", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="虚拟滚动表格", versions=6,
         files=["src/table/VirtualTable.tsx", "src/table/rowHeight.ts", "src/table/tests_table.tsx", "stories/Table.stories.tsx"],
         tokens=["rowHeight=36", "ERR_OVERSCAN_RANGE", "overscan=8"],
         slots=[("VirtualTable.tsx", ["fixed_rows", "measured_rows", "hybrid_estimator"])],
         pitfalls=["定高假设在富文本行上崩坏, 必须实测行高"]),
    dict(id="quartz-theme-tokens", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="设计令牌系统", versions=5,
         files=["src/theme/tokens.ts", "src/theme/dark.css", "src/theme/tests_theme.ts", "docs/tokens_guide.md", "scripts/build_tokens.mjs"],
         tokens=["token_prefix=qz-", "ERR_TOKEN_MISSING", "contrast_ratio=4.5"],
         slots=[("tokens.ts", ["hardcoded_hex", "css_variables", "token_pipeline"])],
         pitfalls=["硬编码色值让暗色主题失控, 必须走令牌管线"]),
    dict(id="quartz-form-validation", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="表单校验架构", versions=6,
         files=["src/form/schema.ts", "src/form/validate.ts", "src/form/tests_form.ts", "docs/validation_guide.md"],
         tokens=["ERR_SCHEMA_INVALID", "debounce_ms=200", "zod_schema_v4"],
         slots=[("validate.ts", ["inline_rules", "schema_first", "async_schema"])],
         pitfalls=["内联规则与 schema 双轨校验会互相打架, 单一事实源才可维护"]),
    dict(id="quartz-date-picker", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="日期选择器时区处理", versions=5,
         files=["src/datepicker/Calendar.tsx", "src/datepicker/tz.ts", "src/datepicker/tests_dp.tsx", "docs/tz_guide.md"],
         tokens=["ERR_TZ_OFFSET", "utc_only=true", "locale=zh-CN"],
         slots=[("tz.ts", ["local_zone", "utc_normalize", "zone_aware"])],
         pitfalls=["用本地时区存日期字段会在跨时区会话中漂移"]),
    dict(id="quartz-websocket-feed", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="实时行情推送", versions=6,
         files=["src/feed/socket.ts", "src/feed/buffer.ts", "src/feed/tests_feed.ts", "docs/feed_notes.md"],
         tokens=["heartbeat_ms=15000", "ERR_SOCKET_STALE", "buffer_max=500"],
         slots=[("socket.ts", ["reconnect_blind", "backoff_probe", "sticky_session"])],
         pitfalls=["盲目立即重连会在网关抖动时打爆连接数"]),
    dict(id="quartz-build-pipeline", project="quartz-ui", cwd="D:\\work\\quartz-ui",
         topic="构建产物体积治理", versions=5,
         files=["vite.config.ts", "src/lazyRoutes.ts", "scripts/analyze_bundle.mjs", "docs/build_notes.md"],
         tokens=["chunk_warn=250kb", "ERR_CHUNK_BLOAT", "manualChunks=true"],
         slots=[("vite.config.ts", ["single_bundle", "route_splitting", "lib_externalization"])],
         pitfalls=["单 bundle 让首屏体积失控, 路由拆分是底线"]),
    # ---------------- cobalt-sync (数据同步, 5 链) ----------------
    dict(id="cobalt-conflict-resolve", project="cobalt-sync", cwd="D:\\work\\cobalt-sync",
         topic="同步冲突消解", versions=6,
         files=["sync/merge.py", "sync/vector_clock.py", "sync/tests_merge.py", "docs/merge_notes.md"],
         tokens=["clock_depth=16", "ERR_CONFLICT_LOOP", "lamport_ts"],
         slots=[("merge.py", ["last_write_wins", "vector_clock_merge", "crdt_lww_register"])],
         pitfalls=["last-write-wins 在时钟漂移下会丢更新, 向量钟才可靠"]),
    dict(id="cobalt-checkpoint-store", project="cobalt-sync", cwd="D:\\work\\cobalt-sync",
         topic="断点续传检查点", versions=5,
         files=["sync/checkpoint.py", "sync/resume.log", "sync/tests_ckpt.py", "docs/ckpt_notes.md"],
         tokens=["ckpt_interval=30s", "ERR_CKPT_GAP", "resume_from_seq"],
         slots=[("checkpoint.py", ["memory_only", "sqlite_ckpt", "append_log_ckpt"])],
         pitfalls=["纯内存检查点在进程重启后全部丢失"]),
    dict(id="cobalt-schema-drift", project="cobalt-sync", cwd="D:\\work\\cobalt-sync",
         topic="下游 schema 漂移探测", versions=5,
         files=["sync/schema_probe.py", "sync/drift_report.json", "sync/tests_probe.py", "docs/drift_runbook.md"],
         tokens=["probe_cron=0 */6 * * *", "ERR_DRIFT_DETECTED", "fingerprint_v2"],
         slots=[("schema_probe.py", ["manual_diff", "scheduled_probe", "event_driven_hook"])],
         pitfalls=["手工 diff 只能事后救火, 定时探测才能提前预警"]),
    dict(id="cobalt-batch-export", project="cobalt-sync", cwd="D:\\work\\cobalt-sync",
         topic="批量导出管道", versions=6,
         files=["export/pipeline.py", "export/parquet_writer.py", "export/tests_export.py", "docs/export_runbook.md"],
         tokens=["batch_rows=20000", "ERR_PARQUET_TRUNCATED", "flush_bytes=64mb"],
         slots=[("pipeline.py", ["csv_full_load", "chunked_parquet", "streaming_arrow"])],
         pitfalls=["一次性全量 CSV 在千万行级会 OOM, 分块才是正解"]),
    dict(id="cobalt-connector-auth", project="cobalt-sync", cwd="D:\\work\\cobalt-sync",
         topic="连接器鉴权刷新", versions=5,
         files=["connector/oauth.py", "connector/token_cache.py", "connector/tests_oauth.py", "docs/auth_notes.md"],
         tokens=["refresh_skew=120s", "ERR_TOKEN_EXPIRED", "scope=read:sync"],
         slots=[("oauth.py", ["token_per_request", "cached_refresh", "prefetch_jwty"])],
         pitfalls=["每请求都换 token 会触发上游限流, 必须缓存刷新"]),
]

# 干扰查询主题 (语料中刻意不存在 — Abstention 维度)
ABSTENTION_TOPICS = [
    ("phoenix-api", "kubernetes 部署清单方案"),
    ("phoenix-api", "graphql 网关聚合层"),
    ("quartz-ui", "服务端渲染流式水合"),
    ("quartz-ui", "canvas 白板协同编辑"),
    ("cobalt-sync", "区块链锚点存证"),
    ("cobalt-sync", "federated learning 联邦训练调度"),
]


def _noise(rng, n):
    return " ".join(rng.choice(NOISE) for _ in range(n))


def _iso(day: int, hour: int = 9, minute: int = 0) -> str:
    return f"2026-{3 + day // 28:02d}-{1 + day % 28:02d}T{hour:02d}:{minute:02d}:00"


def _camel(tok: str) -> str:
    """SNAKE_UPPER → camelCase (跨写法检索: 语料写 camel, 查询写 snake —
    代码感知分词经复合词桥接召回; vanilla 分词器命中不了, 构成 V2 区分度)。"""
    parts = tok.lower().split("_")
    return parts[0] + "".join(x.capitalize() for x in parts[1:])


def build_chain(chain: dict, rng) -> tuple:
    """展开一条演进链 → (versions[], gold 记录)。versions[k] = snapshot envelope dict。"""
    sid_of = lambda k: f"cv_bench_{chain['id']}_v{k:02d}"  # noqa: E731
    n_ver = chain["versions"]
    slots = chain["slots"]
    files = chain["files"]
    tokens = chain["tokens"]
    pitfall = chain["pitfalls"][0]
    versions = []
    gold = {
        "chain": chain["id"], "project": chain["project"], "sids": [sid_of(k) for k in range(n_ver)],
        "leaf_sid": sid_of(n_ver - 1), "files": files, "pitfall": pitfall,
        "tokens": tokens, "slots": [], "block_spans": {},
    }
    prev = None
    cum: list = []
    for k in range(n_ver):
        blocks = []
        day = k * 5
        # 用户任务句 (首版提需求, 后续版追进)
        blocks.append({"type": "text", "role": "user",
                       "content": f"{chain['topic']} — 第 {k + 1} 轮: 请继续推进 {chain['project']}",
                       "ts": _iso(day, 9)})
        # assistant 决策: 槽位值演进 (k 版采用 slots 值 v_k; 末版为终值)
        for si, (slot_file, values) in enumerate(slots):
            val = values[min(k, len(values) - 1)]
            old = values[min(k - 1, len(values) - 1)] if k else None
            if k and val != old:
                content = (f"决定：{slot_file} 的方案由 {old} 改为 {val}, "
                           f"旧的 {old} 路线废弃不再使用")
                gold["slots"].append({"slot": slot_file, "final": val,
                                      "superseded": values[:k], "at_version": k})
            elif k == 0:
                content = f"决定：{slot_file} 的方案采用 {val} 作为起点"
            else:
                content = f"结论：{slot_file} 维持 {val} 方案不变"
            blocks.append({"type": "text", "role": "assistant", "content": content,
                           "ts": _iso(day, 9, 10 + si)})
            if k == 2:
                # v0.1 误杀 exerciser: 单对反引号技术实体 + Markdown 结构化裁决 —
                # 规则硬闸门杀掉, v2 保留 (含主语锚点, 直击槽位召回)
                blocks.append({"type": "text", "role": "assistant",
                               "content": f"- **结论**：决定采用 `{slot_file}` 的 {val} 路线",
                               "ts": _iso(day, 9, 12 + si)})
        if k == 3:
            # 跨写法标识符: 语料写 camelCase, 查询写 snake_case (gold.cross 记录一对)
            cam = _camel(tokens[0])
            blocks.append({"type": "text", "role": "assistant",
                           "content": f"决定：配置常量统一为 {cam} 命名 (camelCase 风格)",
                           "ts": _iso(day, 9, 14)})
            gold["cross"] = {"camel": cam, "snake": tokens[0].lower()}
        # 踩坑句: 第 2 版给出根因, 后续版本保留引用 (进行中的工作会持续引用坑)
        if k == 1:
            blocks.append({"type": "text", "role": "assistant",
                           "content": f"踩坑：{pitfall}, 根因已复现, 这是死路不要再走",
                           "ts": _iso(day, 10, 30)})
        elif k > 1:
            blocks.append({"type": "text", "role": "assistant",
                           "content": f"踩坑：{pitfall}", "ts": _iso(day, 10, 30)})
        # thinking 摘要
        blocks.append({"type": "thinking", "role": "assistant",
                       "summary": f"评估 {chain['topic']} 当前方案的边界与回滚成本",
                       "ts": _iso(day, 10, 40)})
        # 工具调用: 触碰文件 (每版触 2 个, 轮换) + 特异命令/错误码
        touched = [files[k % len(files)], files[(k + 1) % len(files)]]
        for fi, f in enumerate(touched):
            blocks.append({"type": "tool_call", "call_id": f"c{k}_{fi}", "name": "edit_file",
                           "arguments": {"path": f, "note": " ".join(tokens[:1])},
                           "ts": _iso(day, 11, fi * 5)})
            blocks.append({"type": "tool_result", "call_id": f"c{k}_{fi}",
                           "content": f"patched {f} ok :: " + _noise(rng, 60), "ts": _iso(day, 11, fi * 5 + 1)})
        # 特异命令 + 错误码 (散布在不同版本, 供 single_session 检索)
        tok = tokens[k % len(tokens)]
        blocks.append({"type": "tool_call", "call_id": f"cmd{k}", "name": "run_cmd",
                       "arguments": {"command": f"{chain['project']} CLI: {tok}"}, "ts": _iso(day, 14)})
        err_text = (f"{tok} failed :: exit 1" if k % 2 else
                    f"probe ok :: {tok} latency 42ms")
        blocks.append({"type": "tool_result", "call_id": f"cmd{k}",
                       "content": err_text + " :: " + _noise(rng, 40), "ts": _iso(day, 14, 1)})
        # 大段日志噪音 (检索压力; 每版 3 条)
        for ni in range(3):
            blocks.append({"type": "tool_result", "call_id": f"log{k}_{ni}",
                           "content": f"frame {ni} :: " + _noise(rng, 120), "ts": _iso(day, 15, ni)})
        cum.extend(blocks)  # 累积语义: 快照 = 会话至今的完整 rollout (与真实 vault 一致)
        gold["block_spans"][sid_of(k)] = {"decision_blocks": [
            i for i, b in enumerate(cum)
            if b["type"] == "text" and b.get("role") == "assistant" and "决定" in b["content"]],
            "files_touched": touched}
        env = {"format_version": "0.1", "created_at": _iso(day, 16),
               "capture": {"agent": "codex", "source_format": "codex-rollout",
                           "cwd": chain["cwd"], "session_ref": f"bench-{chain['id']}",
                           "completeness": "full"},
               "conversation": list(cum),
               "stats": {"tokens_in": 0, "tokens_out": 0, "reasoning_tokens": 0,
                         "cache_read": 0, "cache_write": 0, "total_tokens": 0,
                         "cost_usd": None, "usage_events": 0,
                         "blocks_total": len(blocks), "blocks_dropped": 0},
               "lineage": {"parent_snapshot": prev, "fork_of": None}}
        versions.append((sid_of(k), env))
        prev = sid_of(k)
    return versions, gold


def build_queries(golds: list) -> list:
    """从链定义推导 5 维度金标查询 (≥120 条)。查询由参数推导, 不从文本反向抽取。"""
    queries = []
    rng = random.Random(SEED + 1)
    by_project = {}
    for g in golds:
        by_project.setdefault(g["project"], []).append(g)

    # ① single_session: 每链 2 个特异 token (24 条) + 跨写法标识符 (每链 1 条)。
    # 累积快照语义: 证据块自引入版起存在于其后所有版本 → 有效命中 = 期望叶子的链
    # (run_bench 的 chain-equivalent 判定), expect_sids 统一标叶子。
    for g in golds:
        for tok in g["tokens"][:2]:
            queries.append({
                "id": f"ss-{len(queries) + 1:03d}", "dim": "single_session",
                "query": f"{g['project']} {tok}",
                "expect_sids": [g["leaf_sid"]], "expect_facts": [tok], "abstain": False})
    for g in golds:
        cx = g.get("cross")
        if not cx:
            continue
        queries.append({
            "id": f"ss-{len(queries) + 1:03d}", "dim": "single_session",
            "query": f"{g['project']} {cx['snake']} 命名",
            "expect_sids": [g["leaf_sid"]], "expect_facts": [cx["camel"]], "abstain": False})

    # ② multi_session: 同项目两条不同链的叶子 (24 条 = 12 对 × 2 问法)
    for proj, gs in by_project.items():
        for i in range(len(gs) - 1):
            a, b = gs[i], gs[i + 1]
            queries.append({
                "id": f"ms-{len(queries) + 1:03d}", "dim": "multi_session",
                "query": f"{proj} {a['tokens'][0]} {b['tokens'][0]} 关联排查",
                "expect_sids": [a["leaf_sid"], b["leaf_sid"]], "abstain": False})
    # 补足到 24: 跨项目集成对 (phoenix→quartz 消费方)
    extra_pairs = [(golds[0], golds[7]), (golds[1], golds[8]), (golds[3], golds[9])]
    for a, b in extra_pairs:
        queries.append({
            "id": f"ms-{len(queries) + 1:03d}", "dim": "multi_session",
            "query": f"{a['project']} {a['tokens'][1]} 与 {b['project']} {b['tokens'][0]} 联调定位",
            "expect_sids": [a["leaf_sid"], b["leaf_sid"]], "abstain": False})

    # ③ temporal_update: 每链每槽位问终值 (24 条)
    for g in golds:
        for slot in g["slots"][:2] if g["slots"] else []:
            anchor = slot["slot"].replace(".yaml", "").replace(".py", "").replace(".md", "") \
                .replace(".json", "").replace(".ts", "").replace(".tsx", "")
            queries.append({
                "id": f"tp-{len(queries) + 1:03d}", "dim": "temporal_update",
                "query": f"{g['project']} {anchor} 当前采用什么方案",
                "expect_sids": [g["leaf_sid"]], "expect_facts": [slot["final"]],
                "stale_facts": slot["superseded"], "slot": slot, "abstain": False})
    # 不足 24 则用链的 file-slot 补 (每链 1 槽 × 18 = 18; 补 6 个跨链终值问法)
    for g in golds[:6]:
        slot = g["slots"][0]
        queries.append({
            "id": f"tp-{len(queries) + 1:03d}", "dim": "temporal_update",
            "query": f"{g['project']} {slot['slot']} 现在的最新决策是什么 (含被推翻的历史)",
            "expect_sids": [g["leaf_sid"]], "expect_facts": [slot["final"]],
            "stale_facts": slot["superseded"], "slot": slot, "abstain": False})

    # ④ handoff: 每链做 4K 预算简报, 验收文件+踩坑保留 (24 条 = 18 链 + 6 长链加倍)
    for g in golds:
        queries.append({
            "id": f"ho-{len(queries) + 1:03d}", "dim": "handoff",
            "query": f"{g['chain']} 交接简报",
            "expect_sids": [g["leaf_sid"]], "expect_files": g["files"],
            "expect_pitfalls": [g["pitfall"]], "budget_tokens": 4000, "abstain": False})
    for g in golds[:6]:
        queries.append({
            "id": f"ho-{len(queries) + 1:03d}", "dim": "handoff",
            "query": f"{g['chain']} 全链压缩交接 (预算 2000)",
            "expect_sids": [g["leaf_sid"]], "expect_files": g["files"],
            "expect_pitfalls": [g["pitfall"]], "budget_tokens": 2000, "abstain": False})

    # ⑤ abstention: 语料中不存在的主题 (24 条 = 6 主题 × 4 问法)
    phrasings = ["方案选型现状", "上一轮结论", "踩坑记录", "当前参数配置"]
    for proj, topic in ABSTENTION_TOPICS:
        for ph in phrasings:
            queries.append({
                "id": f"ab-{len(queries) + 1:03d}", "dim": "abstention",
                "query": f"{proj} {topic} {ph}",
                "expect_sids": [], "expect_facts": [], "abstain": True})
    return queries


def main():
    rng = random.Random(SEED)
    corpus_dir = OUT / "corpus"
    queries_dir = OUT / "queries"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    queries_dir.mkdir(parents=True, exist_ok=True)

    all_versions, golds = [], []
    for chain in CHAINS:
        versions, gold = build_chain(chain, rng)
        all_versions.extend(versions)
        golds.append(gold)

    n_blocks = 0
    for sid, env in all_versions:
        (corpus_dir / f"{sid}.json").write_text(
            json.dumps(env, ensure_ascii=False), encoding="utf-8")
        n_blocks += len(env["conversation"])

    queries = build_queries(golds)
    # 按维度分文件落盘
    by_dim = {}
    for q in queries:
        by_dim.setdefault(q["dim"], []).append(q)
    for dim, qs in by_dim.items():
        (queries_dir / f"{dim}.jsonl").write_text(
            "\n".join(json.dumps(q, ensure_ascii=False) for q in qs) + "\n", encoding="utf-8")
    (queries_dir / "gold_chains.json").write_text(
        json.dumps(golds, ensure_ascii=False, indent=1), encoding="utf-8")

    # 泄漏自检 (零密钥 / 零本机路径)
    blob = "\n".join((corpus_dir / f"{sid}.json").read_text(encoding="utf-8")
                     for sid, _ in all_versions)
    bad = [w for w in ("sk-", "api_key", "AKIA", "password=", "C:\\Users", "/home/", "token = ")
           if w.lower() in blob.lower()]
    assert not bad, f"泄漏扫描失败: {bad}"
    print(f"corpus: {len(all_versions)} snapshots / {n_blocks} blocks / "
          f"{sum(len(json.dumps(env)) for _, env in all_versions) // 1024} KB")
    print(f"queries: {len(queries)} gold "
          f"({', '.join(f'{d}={len(v)}' for d, v in sorted(by_dim.items()))})")
    assert len(queries) >= 120, "金标不足 120"


if __name__ == "__main__":
    main()
