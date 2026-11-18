#!/usr/bin/env python3
"""CodingVault-Bench 一键消融实验 (Ablation Study) — 30 秒复现。

同一语料 + 同一金标上自动跑完变体矩阵, 输出 Markdown 表格 + JSON 原始结果:
- benchmarks/results/bench_results.json
- benchmarks/results/BENCH_TABLE.md

变体矩阵 (检索演进 × 记忆演进 × 交接演进):
  Panel A 检索 (dims: single/multi/temporal/abstention):
    Baseline0  v0 词集交集 (升级前实现, benchmarks/legacy_v0.py 原样语义)
    V1         Vanilla BM25 (纯词切分, 权重全 1, 无折叠, 无衰减)
    V2         V1 + 代码感知分词
    V3         V2 + 块角色加权 + Lineage 折叠
    Ours       V3 + 时间衰减 (Full Core v2)
    +Dense     Ours + 稠密向量 RRF (仅 CV_EMBEDDINGS_URL 配置时; 缺省跳过零外发)
  Panel B 记忆 (dim: temporal_update):
    v0   宽松规则 (无噪音闸门, 无覆盖)
    v0.1 硬噪音闸门 (高降噪, 但误杀含代码实体的决策 — 已知缺陷对照)
    v2   双时态引擎 (默认): 出处 + topic_key + superseded_by + 去重记信心
  Panel C 交接 (dim: handoff):
    naive 截断 (前 4K token) vs compress_brief 五槽位效用装配 (W3)

反作弊纪律: 查询集来自 benchmarks/build_corpus.py 的链定义参数 (人工金标),
不从文本反向抽取; 各变体共用同一查询/语料; 全部数字由真实运行落盘。

运行: python benchmarks/run_bench.py [--quick]
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(HERE))

import cv  # noqa: E402
import cv_memory  # noqa: E402
from cv_index import (  # noqa: E402
    ColdRanker,
    UNIFORM,
    VaultIndex,
    tokenize,
)
import legacy_v0  # noqa: E402

BENCH = HERE / "coding_vault_bench"
SEARCH_DIMS = ("single_session", "multi_session", "temporal_update", "abstention")


def load_queries() -> dict:
    out = {}
    for f in sorted((BENCH / "queries").glob("*.jsonl")):
        out[f.stem] = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def load_corpus(tmp: Path) -> Path:
    dst = tmp / "snapshots"
    dst.mkdir(parents=True)
    for f in (BENCH / "corpus").glob("cv_bench_*.json"):
        shutil.copy2(f, dst / f.name)
    return tmp


def _mk_vault_with_memories(tmp: Path, engine_name: str, loose: bool = False) -> dict:
    """把语料注册进 vault.db 并按指定引擎提取记忆 (Panel B 各代际)。"""
    vault = tmp
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    for f in sorted((vault / "snapshots").glob("cv_bench_*.json")):
        sid = f.stem
        env = json.loads(f.read_text(encoding="utf-8"))
        cv.register_snapshot(conn, env, sid, f)
    conn.commit()
    conn.close()
    if loose:
        # v0 宽松规则: 逐快照提取, 无噪音闸门, 无覆盖 — 直写表 (不走 v2 通道)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA + cv_memory.MEMORY_TABLE)
        for f in sorted((vault / "snapshots").glob("cv_bench_*.json")):
            sid = f.stem
            env = json.loads(f.read_text(encoding="utf-8"))
            env["_sid"] = sid
            conn.execute("DELETE FROM memory_items WHERE snapshot_id=?", (sid,))
            for it in _extract_loose(env):
                conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,"
                             "status,confidence) VALUES(?,?,?,?,?,'active',1.0)",
                             (cv.now_iso(), it["snapshot_id"], it["kind"], it["content"],
                              it["project"]))
        conn.commit()
        conn.close()
        return {"engine": "v0-loose"}
    return cv_memory.extract_memories_incremental(vault, engine_name=engine_name, force=True)


def _extract_loose(env: dict) -> list:
    """v0 宽松规则 = v0.1 代码去掉噪音闸门 (保留标记窗口语义)。"""
    items = []
    for it in legacy_v0.extract_memories_v0_loose(env):
        items.append(it)
    return items


# ---------------- Panel A: 检索变体 ----------------

def build_search_variant(tmp: Path, name: str):
    """返回 (search_fn, index_size_mb, build_sec)。search_fn(query, limit) -> hits[{'sid',...}]"""
    vault = tmp
    if name == "Baseline0":
        envs = []
        for f in sorted((vault / "snapshots").glob("cv_bench_*.json")):
            env = json.loads(f.read_text(encoding="utf-8"))
            env["_sid"] = f.stem
            envs.append(env)
        envs.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        t0 = time.perf_counter()

        def search(query, limit):
            terms = legacy_v0.tokenize(query)
            if not terms:
                return []
            scored = []
            for env in envs:
                hits = legacy_v0.score_blocks(env, terms)
                if hits:
                    scored.append((sum(h[0] for h in hits), env.get("_sid")))
            scored.sort(key=lambda r: (-r[0], r[1]))
            return [{"sid": s, "score": sc} for sc, s in scored[:limit]]
        return search, 0.0, time.perf_counter() - t0, None

    params = {
        "V1": dict(tokenizer=lambda t: tokenize(t, code_aware=False), weights=UNIFORM,
                   code_aware=False, config_id="vanilla"),
        "V2": dict(tokenizer=tokenize, weights=UNIFORM, config_id="code-uniform"),
        "V3": dict(tokenizer=tokenize, config_id="code-weighted"),
        "Ours": dict(tokenizer=tokenize),
    }[name]
    # 每变体独立落盘: 上一配置的 search_index.db 必须清掉 (cfg 指纹是第二道闸)
    (vault / "search_index.db").unlink(missing_ok=True)
    _t0[0] = time.perf_counter()
    idx = VaultIndex(vault, **params)
    if idx.stats["cache_load"]:
        raise RuntimeError(f"{name}: 不应复用落盘索引 (cfg 指纹/清库双重失效)")
    build_sec = time.perf_counter() - _t0[0]
    size_mb = (vault / "search_index.db").stat().st_size / 1048576
    fold = name in ("V3", "Ours")
    decay = name == "Ours"
    if name == "Ours+Dense":
        from cv_dense import DenseProvider, EmbeddingCache, hybrid_search
        from pathlib import Path as _P

        def search(query, limit):
            return hybrid_search(idx, _P(vault), query, limit,
                                 provider=DenseProvider(),
                                 cache=EmbeddingCache(_P(vault)))["hits"]
        return search, size_mb, build_sec, idx

    def search(query, limit):
        return idx.search(query, limit, decay=decay, fold=fold)["hits"]
    return search, size_mb, build_sec, idx


_t0 = [0.0]


# ---------------- 指标 ----------------

def rank_metrics(hits: list, expect_sids: list, chain_leaf: dict | None = None,
                 k_list=(1, 5)) -> dict:
    """链等价判定 (累积快照语义): 命中链上任一版本且其叶子 ∈ 期望 = 有效。
    chain_leaf: sid → 该链叶子 (期望叶子本身恒有效)。"""
    exp = set(expect_sids)
    sids = [h["sid"] for h in hits]

    def ok(sid):
        if sid in exp:
            return True
        leaf = (chain_leaf or {}).get(sid)
        return leaf is not None and leaf in exp

    rr = 0.0
    for i, sid in enumerate(sids[:10]):
        if ok(sid):
            rr = 1.0 / (i + 1)
            break
    rec = {f"R@{k}": (1.0 if any(ok(s) for s in sids[:k]) else 0.0) for k in k_list}
    return {"rr10": rr, **rec}


def run_search_panel(tmp: Path, queries: dict, variants: list) -> list:
    rows = []
    dim_qs = {d: queries.get(d, []) for d in SEARCH_DIMS}
    # abstention 校准阈值分位 (每变体自校准 — 用 single_session 正例 top1 分数的 P25)
    names = list(variants)
    from cv_dense import DenseProvider
    if DenseProvider().available:
        names.append("Ours+Dense")  # CV_EMBEDDINGS_URL 配置时自动加跑, 缺省零外发
    idx_ref = None
    for name in names:
        if idx_ref is not None and hasattr(idx_ref, "_close"):
            idx_ref._close()  # 释放上一变体 sqlite 句柄 (Windows: 不关则下个变体 unlink 撞 32)
        _t0[0] = time.perf_counter()
        search, size_mb, build_sec, idx_ref = build_search_variant(tmp, name)
        lats, r1_all, r5_all, rr_all, n_pos = [], [], [], [], 0
        stale_hits, stale_n = 0, 0
        chain_leaf = {}
        gold_chains = json.loads((BENCH / "queries" / "gold_chains.json").read_text(encoding="utf-8"))
        for g in gold_chains:
            for sid in g["sids"]:
                chain_leaf[sid] = g["leaf_sid"]
        pos_scores = []
        results = {}
        for dim in SEARCH_DIMS:
            for q in dim_qs[dim]:
                t0 = time.perf_counter()
                hits = search(q["query"], 5)
                lats.append((time.perf_counter() - t0) * 1000)
                sids = [h["sid"] for h in hits]
                if q.get("abstain"):
                    continue
                m = rank_metrics(hits, q.get("expect_sids", []), chain_leaf)
                r1_all.append(m["R@1"])
                r5_all.append(m["R@5"])
                rr_all.append(m["rr10"])
                n_pos += 1
                pos_scores.append(hits[0]["score"] if hits else 0.0)
                results[q["id"]] = sids
                if dim == "temporal_update":
                    exp_leaf = set(q.get("expect_sids", []))
                    stale_n += 1
                    # 过期入侵: top5 中出现同链非叶子版本 (历史版本刷屏) 即入侵
                    for s in sids:
                        leaf = chain_leaf.get(s)
                        if leaf and leaf in exp_leaf and s != leaf:
                            stale_hits += 1
                            break
        # abstention: 拒答判定 = top1 分数 < 校准阈值 (正例 top1 的 P25)
        tau = statistics.quantiles(pos_scores, n=4)[0] if len(pos_scores) >= 4 else 0.0
        ab_ok, ab_n = 0, 0
        for q in dim_qs["abstention"]:
            hits = search(q["query"], 5)
            ab_n += 1
            top1 = hits[0]["score"] if hits else 0.0
            if not hits or top1 < tau:
                ab_ok += 1
        lats.sort()
        rows.append({
            "variant": name, "panel": "search",
            "recall_at_1": round(sum(r1_all) / max(len(r1_all), 1), 4),
            "recall_at_5": round(sum(r5_all) / max(len(r5_all), 1), 4),
            "mrr_at_10": round(sum(rr_all) / max(n_pos, 1), 4),
            "stale_version_intrusion": round(stale_hits / max(stale_n, 1), 4),
            "abstention_accuracy": round(ab_ok / max(ab_n, 1), 4),
            "index_size_mb": round(size_mb, 2),
            "latency_p50_ms": round(lats[len(lats) // 2], 1) if lats else None,
            "latency_p95_ms": round(lats[int(len(lats) * 0.95) - 1], 1) if lats else None,
            "build_sec": round(build_sec, 2),
            "per_query": results,
        })
    return rows


def run_memory_panel(tmp: Path, queries: dict) -> list:
    rows = []
    tp = queries.get("temporal_update", [])
    gold_chains = json.loads((BENCH / "queries" / "gold_chains.json").read_text(encoding="utf-8"))
    # v0.1 误杀率: v2 提取的含技术实体裁决句中, 规则硬闸门漏掉的比例
    v2_dec, rule_dec = set(), set()
    snaps = sorted((tmp / "snapshots").glob("cv_bench_*.json"))
    for f in snaps:
        env = json.loads(f.read_text(encoding="utf-8"))
        env["_sid"] = f.stem
        for it in cv_memory.extract_memories_v2(env):
            if it["kind"] in ("decision", "constraint") and ("`" in it["content"] or it["content"].startswith("-")):
                v2_dec.add(it["content"][:80])
        for it in cv_memory.extract_memories(env):
            if it["kind"] in ("decision", "constraint"):
                rule_dec.add(it["content"][:80])
    killed = len(v2_dec - rule_dec)
    kill_rate = round(killed / max(len(v2_dec), 1), 4)
    for name, engine, loose in (("v0-loose", None, True), ("v0.1-gated", "rule", False),
                                ("v2-temporal", "v2", False)):
        (tmp / "vault.db").unlink(missing_ok=True)  # shutil.rmtree 对单文件静默失败
        for stale in (tmp / "vault.db-wal", tmp / "vault.db-shm"):
            stale.unlink(missing_ok=True)
        res = _mk_vault_with_memories(tmp, engine, loose=loose)
        n_mem = cv_memory.memory_counts_api(tmp)
        act_ok, act_n, stale_intr, stale_n = 0, 0, 0, 0
        for q in tp:
            hits = cv_memory.memory_search_api(tmp, q["query"], 5)
            texts = [h["content"] for h in hits]
            facts = q.get("expect_facts", [])
            if facts:
                act_n += 1
                if any(f in t for t in texts for f in facts):
                    act_ok += 1
            # 过期入侵: 结果把旧值当作当前方案断言 (推翻语句本身提及旧值不算 —
            # "由 A 改为 B / 旧的 A 路线废弃" 描述的是历史)
            stale = q.get("stale_facts", [])
            if stale:
                stale_n += 1
                for t in texts:
                    for old in stale:
                        i = t.find(old)
                        if i < 0:
                            continue
                        ctx = t[max(0, i - 12):i]
                        if "由" not in ctx and "旧的" not in ctx:
                            stale_intr += 1
                            break
                    else:
                        continue
                    break
        # v0.1 误杀率: v2 提取的裁决句被 rule 引擎漏掉的比例 (kill_sets 只需算一次)
        rows.append({
            "variant": name, "panel": "memory",
            "temporal_active_recall": round(act_ok / max(act_n, 1), 4),
            "stale_memory_intrusion": round(stale_intr / max(stale_n, 1), 4),
            "memories_total": n_mem.get("total", 0),
            "memories_active": n_mem.get("active", 0),
            "v01_kill_rate_technical_decisions": kill_rate if name == "v0.1-gated" else None,
            "engine_report": res,
        })
    return rows


def run_handoff_panel(tmp: Path, queries: dict) -> list:
    rows = []
    ho = queries.get("handoff", [])
    leaf_envs = {}
    for f in sorted((tmp / "snapshots").glob("cv_bench_*.json")):
        leaf_envs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    for mode in ("naive_truncate_4k", "ours_compress_4k"):
        file_hits, file_n, pit_hits, pit_n = 0, 0, 0, 0
        ratios = []
        for q in ho:
            sid = q["expect_sids"][0]
            env = dict(leaf_envs[sid])
            env["_sid"] = sid
            budget = q.get("budget_tokens", 4000)
            if mode == "naive_truncate_4k":
                # 朴素基线: 顺序拼接会话正文, 截到预算 token 数
                pieces, used = [], 0
                for b in env["conversation"]:
                    t = cv.block_text(b)
                    cost = cv.estimate_text_tokens(t)
                    pieces.append(t)
                    used += cost
                    if used >= budget:
                        break
                text = "\n".join(pieces)
                kept_files = sum(1 for p in q["expect_files"] if p in text)
                kept_pit = sum(1 for p in q.get("expect_pitfalls", []) if p[:40] in text)
                ratios.append(budget / max(cv.estimate_text_tokens(
                    "\n".join(cv.block_text(b) for b in env["conversation"])), 1))
            else:
                text, stats = cv.compress_brief(env, budget_tokens=budget)
                kept_files = stats["files_kept"]
                kept_pit = sum(1 for p in q.get("expect_pitfalls", [])
                               if any(p[:40] in ln for ln in text.splitlines()))
                ratios.append(stats["ratio"])
            file_n += 1
            file_hits += 1 if kept_files >= max(1, int(0.95 * len(q["expect_files"]))) else 0
            pit_n += 1
            pit_hits += 1 if kept_pit >= len(q.get("expect_pitfalls", [])) else 0
        rows.append({
            "variant": mode, "panel": "handoff",
            "file_recall_pass_rate": round(file_hits / max(file_n, 1), 4),
            "pitfall_recall_pass_rate": round(pit_hits / max(pit_n, 1), 4),
            "compression_ratio": round(statistics.mean(ratios), 4) if ratios else None,
        })
    return rows


def render_markdown(search_rows, memory_rows, handoff_rows) -> str:
    lines = ["# CodingVault-Bench 消融实验 (自动生成 — run_bench.py)", ""]
    lines += ["## Panel A — 检索演进", "",
              "| 变体 | Recall@1 | Recall@5 | MRR@10 | 版本入侵率↓ | 拒答准确率 | 索引 MB | P50 ms | P95 ms |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in search_rows:
        lines.append(f"| {r['variant']} | {r['recall_at_1']} | {r['recall_at_5']} | "
                     f"{r['mrr_at_10']} | {r['stale_version_intrusion']} | "
                     f"{r['abstention_accuracy']} | {r['index_size_mb']} | "
                     f"{r['latency_p50_ms']} | {r['latency_p95_ms']} |")
    lines += ["", "## Panel B — 记忆演进", "",
              "| 引擎 | Temporal Active Recall | Stale Memory Intrusion↓ | 记忆总量 | 活跃记忆 | v0.1 误杀率 |",
              "|---|---|---|---|---|---|"]
    for r in memory_rows:
        kr = r.get("v01_kill_rate_technical_decisions")
        lines.append(f"| {r['variant']} | {r['temporal_active_recall']} | "
                     f"{r['stale_memory_intrusion']} | {r['memories_total']} | "
                     f"{r['memories_active']} | {kr if kr is not None else '—'} |")
    lines += ["", "## Panel C — 交接压缩 (4K/2K 预算)", "",
              "| 变体 | 文件保留达标率 (≥95%) | 踩坑保留率 | 压缩比 |", "|---|---|---|---|"]
    for r in handoff_rows:
        lines.append(f"| {r['variant']} | {r['file_recall_pass_rate']} | "
                     f"{r['pitfall_recall_pass_rate']} | {r['compression_ratio']} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过朴素基线的全文拼接 (提速)")
    args = ap.parse_args()

    queries = load_queries()
    n_q = sum(len(v) for v in queries.values())
    print(f"queries: {n_q} gold across {len(queries)} dims")
    tmp = Path(tempfile.mkdtemp(prefix="cv_bench_"))
    try:
        load_corpus(tmp)
        _t0[0] = time.perf_counter()
        search_rows = run_search_panel(tmp, queries,
                                       ["Baseline0", "V1", "V2", "V3", "Ours"])
        memory_rows = run_memory_panel(tmp, queries)
        handoff_rows = run_handoff_panel(tmp, queries) if not args.quick else []
        out_dir = HERE / "results"
        out_dir.mkdir(exist_ok=True)
        payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "n_queries": n_q, "search": search_rows,
                   "memory": memory_rows, "handoff": handoff_rows}
        (out_dir / "bench_results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        md = render_markdown(search_rows, memory_rows, handoff_rows)
        (out_dir / "BENCH_TABLE.md").write_text(md, encoding="utf-8")
        print(md)
        print(f"results → {out_dir / 'bench_results.json'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
