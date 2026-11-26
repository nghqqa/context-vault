"""检索引擎 v2 验收测试 (W1): 代码感知分词 / 块角色加权 BM25 防刷分 / Lineage 折叠 /
SQLite 持久层与增量追赶 / 冷热等价 / RRF 与时间衰减。

运行: python -m pytest tests/test_cv_v2.py -v
铁律: 全部构造在 tmp_path 临时 vault, 绝不触碰生产 vault/。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
from cv_index import (  # noqa: E402
    ColdRanker,
    IDENT_BOOST,
    UNIFORM,
    chain_root,
    decay_factor,
    fold_chains,
    rrf_fuse,
    tokenize,
    ident_set,
)


# ---------------- 构造工具 ----------------

def _env(blocks: list, cwd: str = "D:\\proj", created_at: str | None = None) -> dict:
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": cwd, "session_ref": "s1"},
                             "conversation": blocks, "stats": cv.zero_stats()})
    if created_at:
        env["created_at"] = created_at
    return env


def _write(vault: Path, env: dict, sid: str) -> str:
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                     encoding="utf-8")
    return sid


def _load_envs(vault: Path) -> list:
    out = []
    for f in sorted((vault / "snapshots").glob("cv_*.json")):
        env = json.loads(f.read_text(encoding="utf-8"))
        env["_sid"] = f.stem
        out.append(env)
    return out


NOISE = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "


# ---------------- 代码感知分词 ----------------

def test_tokenizer_code_aware_split_and_composite():
    toks = tokenize("use VaultIndex not vault_index inside fix_apply.mjs see CVE-2026-12345")
    assert "vaultindex" in toks and "vault" in toks and "index" in toks
    assert "fix" in toks and "apply" in toks
    assert "cve" in toks and "2026" in toks
    # 双向召回: 驼峰查询命中 snake 文档, 反之亦然 (复合词归一)
    a = set(tokenize("VaultIndex"))
    b = set(tokenize("vault_index"))
    assert a & b >= {"vaultindex", "vault", "index"}


def test_tokenizer_v0_compat_and_cjk():
    toks = tokenize("MergePilot 优化 dataset1 时序")
    assert "mergepilot" in toks and "dataset1" in toks  # 复合保留
    assert "merge" in toks and "pilot" in toks          # camel 子词
    assert any(len(t) == 2 and all("\u4e00" <= c <= "\u9fff" for c in t) for t in toks)  # CJK bigram
    # code_aware=False: 纯词切分 (vanilla 消融对照)
    assert tokenize("VaultIndex", code_aware=False) == ["vaultindex"]


def test_ident_set_precision():
    idents = ident_set("call fix_apply.mjs and cv_index.py rows=42")
    assert "fixapplymjs" in idents and "fix" in idents and "apply" in idents
    assert "cvindexpy" in idents and "index" in idents  # 复合形态 + 子词都入集合
    assert "42" not in idents  # 纯数字不入标识符集合


# ---------------- BM25 防刷分 (W1 核心痛点) ----------------

def _flood_vault(vault: Path):
    """决策快照 (2 行关键拍板) vs tool_result 刷屏快照 (200 条重复命中日志)。"""
    dec = _env([
        {"type": "text", "role": "user", "content": "检索引擎怎么解决刷分问题?"},
        {"type": "text", "role": "assistant",
         "content": "决定：采用 BM25 饱和聚合方案, tool_result 降权, 检索刷分由角色权重压制。"},
    ])
    _write(vault, dec, "cv_dec000000000001")
    flood_blocks = [{"type": "text", "role": "user", "content": "run tests"}]
    for i in range(200):
        flood_blocks.append({"type": "tool_result", "call_id": f"c{i}",
                             "content": f"[log {i}] 检索刷分 benchmark output {NOISE}"})
    flood = _env(flood_blocks)
    _write(vault, flood, "cv_flood00000000001")


def test_flood_guard_decision_beats_tool_spam(tmp_path):
    _flood_vault(tmp_path)
    now = time.time()
    hot = cv.VaultIndex(tmp_path).search("检索刷分 方案", 5, now=now)
    cold = ColdRanker(_load_envs(tmp_path), "检索刷分 方案", now=now).search(5)
    assert hot["hits"] and cold["hits"]
    assert hot["hits"][0]["sid"] == "cv_dec000000000001", \
        "决策快照必须压过 200 条 tool_result 刷屏 (v0 求和聚合下会被刷分淹没)"
    assert cold["hits"][0]["sid"] == "cv_dec000000000001"


def test_ident_boost_keeps_tool_evidence_recallable(tmp_path):
    """精确函数名查询: 只出现在 tool_result 的标识符必须仍能召回该快照 (精确加分不误伤)。"""
    blocks = [{"type": "text", "role": "user", "content": "look into resolve_cache_bug"}]
    for i in range(30):
        blocks.append({"type": "tool_result", "call_id": f"d{i}",
                       "content": f"trace {i}: inside resolve_cache_bug path {NOISE}"})
    _write(tmp_path, _env(blocks), "cv_tool000000000001")
    _write(tmp_path, _env([{"type": "text", "role": "assistant",
                            "content": "决定：无关会话, 讨论其他主题"}]), "cv_other00000000001")
    hot = cv.VaultIndex(tmp_path).search("resolve_cache_bug", 5)
    sids = [h["sid"] for h in hot["hits"]]
    assert "cv_tool000000000001" in sids, "tool 块标识符精确命中必须可召回"


# ---------------- Lineage 折叠 ----------------

def test_lineage_fold_chain_to_leaf(tmp_path):
    prev = None
    for i, sid in enumerate(("cv_chain000000000a", "cv_chain000000000b", "cv_chain000000000c")):
        env = _env([{"type": "text", "role": "user", "content": "phoenix 迁移方案 关键决策"}])
        if prev:
            env["lineage"]["parent_snapshot"] = prev
        _write(tmp_path, env, sid)
        prev = sid
    now = time.time()
    hot = cv.VaultIndex(tmp_path).search("phoenix 迁移", 10, now=now)
    assert hot["total"] == 1, f"同一演进链 3 个快照命中必须折叠为 1 (got {hot['total']})"
    assert hot["hits"][0]["sid"] == "cv_chain000000000c"      # 代表 = 链叶子 (最新版)
    assert hot["hits"][0]["folded"] == 2                      # 折叠了 2 个历史版本
    cold = ColdRanker(_load_envs(tmp_path), "phoenix 迁移", now=now).search(10)
    assert cold["total"] == 1 and cold["hits"][0]["sid"] == "cv_chain000000000c"
    # context 同链去重
    ctx = cv.VaultIndex(tmp_path).context("phoenix 迁移", 10, now=now)
    assert len(ctx["results"]) == 1


def test_fold_chains_pure_function():
    meta = {"a": {"created_at": "2026-01-01T00:00:00"},
            "b": {"created_at": "2026-02-01T00:00:00"},
            "c": {"created_at": "2026-03-01T00:00:00"},
            "d": {"created_at": "2026-04-01T00:00:00"}}
    scored = {"a": 3.0, "b": 2.0, "c": 5.0}       # d 无命中
    out = fold_chains(scored, ["a", "b", "c", "d"], {"b": "a", "c": "b", "d": "c"}, meta)
    assert len(out) == 1
    sc, rep, folded = out[0]
    assert rep == "d" and folded == 3             # 链叶子 d 是代表; 4 成员链折叠 3 个版本位
    assert sc == 5.0                              # 链记分 = 最高分成员


def test_chain_root_function():
    parents = {"b": "a", "c": "b"}
    memo = {}
    all_sids = {"a", "b", "c"}
    assert chain_root("c", parents, all_sids, memo) == "a"
    assert chain_root("b", parents, all_sids, memo) == "a"
    assert chain_root("a", parents, all_sids, memo) == "a"


# ---------------- 持久层 / 增量 / 损坏回退 ----------------

def test_index_persistence_incremental_and_removal(tmp_path):
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "第一 会话 甲"}]), "cv_aaaa0000000001")
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "第二 会话 乙"}]), "cv_bbbb0000000001")
    idx = cv.VaultIndex(tmp_path)
    assert len(idx.meta) == 2 and idx.stats == {"full": 1, "incr": 0, "cache_load": 0}

    # 增量: 纯新增 → incr 计数
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "第三 会话 甲"}]), "cv_cccc0000000001")
    idx.rebuild()
    assert idx.stats["incr"] == 1 and idx.stats["full"] == 1
    assert "cv_cccc0000000001" in idx.meta

    # 缓存: 新实例直接装载 SQLite 索引, 追加走增量
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "第四 会话 乙"}]), "cv_dddd0000000001")
    idx2 = cv.VaultIndex(tmp_path)
    assert idx2.stats["cache_load"] == 1 and idx2.stats["full"] == 0
    idx2.rebuild()
    assert idx2.stats["incr"] == 1 and len(idx2.meta) == 4
    assert idx2.search("第四 会话", 5)["hits"]
    assert idx2.context("第三 会话", 3)["results"]

    # 删除: 差分 → 全量重建, 与搜索立刻一致; v2 库文件落盘, 800MB pickle 不再产生
    (tmp_path / "snapshots" / "cv_aaaa0000000001.json").unlink()
    idx2.rebuild()
    assert len(idx2.meta) == 3 and "cv_aaaa0000000001" not in idx2.meta
    assert idx2.stats["full"] == 1
    assert (tmp_path / "search_index.db").exists()
    assert not (tmp_path / "index_cache.pkl").exists()


def test_corrupt_db_falls_back_to_full_rebuild(tmp_path):
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "回归 正常 快照"}]), "cv_good0000000001")
    idx = cv.VaultIndex(tmp_path)
    assert "cv_good0000000001" in idx.meta
    idx._close()
    (tmp_path / "search_index.db").write_bytes(b"definitely not a sqlite db \x00garbage")
    idx2 = cv.VaultIndex(tmp_path)
    assert idx2.stats["cache_load"] == 0 and idx2.stats["full"] == 1
    assert idx2.search("回归", 5)["hits"]


def test_legacy_pkl_removed_after_v2_build(tmp_path):
    (tmp_path / "index_cache.pkl").write_bytes(b"stale v0 cache")
    _write(tmp_path, _env([{"type": "text", "role": "user", "content": "迁移 到 v2"}]), "cv_v2000000000001")
    cv.VaultIndex(tmp_path)
    assert not (tmp_path / "index_cache.pkl").exists(), "v0 pickle 必须在 v2 建库成功后移除"


# ---------------- 冷热等价 ----------------

def test_hot_cold_parity_search_and_context(tmp_path):
    _flood_vault(tmp_path)
    prev = None
    for sid in ("cv_chain000000000a", "cv_chain000000000b"):
        env = _env([{"type": "text", "role": "user", "content": "phoenix 迁移方案 关键决策"}])
        if prev:
            env["lineage"]["parent_snapshot"] = prev
        _write(tmp_path, env, sid)
        prev = sid
    _write(tmp_path, _env([{"type": "text", "role": "assistant",
                            "content": "决定：数据库迁移采用双写方案"}], created_at="2026-05-01T00:00:00"),
           "cv_db00000000000001")
    now = time.time()
    for q in ("检索刷分 方案", "phoenix 迁移", "数据库迁移 双写", "不存在的词组xyz"):
        hot = cv.VaultIndex(tmp_path).search(q, 5, now=now)
        cold = ColdRanker(_load_envs(tmp_path), q, now=now).search(5)
        assert [h["sid"] for h in hot["hits"]] == [h["sid"] for h in cold["hits"]], q
        assert [round(h["score"], 6) for h in hot["hits"]] == \
               [round(h["score"], 6) for h in cold["hits"]], q
        hotc = cv.VaultIndex(tmp_path).context(q, 3, now=now)
        coldc = ColdRanker(_load_envs(tmp_path), q, now=now).context(3)
        assert [(r["sid"], r["block"]) for r in hotc["results"]] == \
               [(r["sid"], r["block"]) for r in coldc["results"]], q


# ---------------- 衰减 / RRF / 权重消融开关 ----------------

def test_decay_factor_monotonic():
    now = time.mktime(time.strptime("2026-11-18T00:00:00", "%Y-%m-%dT%H:%M:%S"))
    fresh = decay_factor("2026-11-17T00:00:00", now, 90.0)
    old = decay_factor("2026-01-01T00:00:00", now, 90.0)
    assert 0 < old < fresh <= 1.0
    assert decay_factor("garbage-ts", now, 90.0) == 1.0
    assert decay_factor("2026-01-01T00:00:00", now, 0) == 1.0


def test_rrf_fuse_basic():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a"]], k=60)
    keys = [k for k, _ in fused]
    assert keys[0] in ("a", "b")          # 两路都靠前的必领先
    assert set(keys) == {"a", "b", "c"}


def test_uniform_weights_ablation_switch(tmp_path):
    _flood_vault(tmp_path)
    idx = cv.VaultIndex(tmp_path, weights=UNIFORM, ident_boost=1.0)
    assert idx.search("检索刷分 方案", 5)["hits"]  # vanilla 模式可运行 (消融对照)


def test_time_decay_orders_recent_first(tmp_path):
    _write(tmp_path, _env([{"type": "text", "role": "assistant",
                            "content": "决定：时序检索方案 老快照"}],
                           created_at="2026-01-01T00:00:00"), "cv_old0000000000001")
    _write(tmp_path, _env([{"type": "text", "role": "assistant",
                            "content": "决定：时序检索方案 新快照"}],
                           created_at="2026-11-17T00:00:00"), "cv_new0000000000001")
    now = time.mktime(time.strptime("2026-11-18T00:00:00", "%Y-%m-%dT%H:%M:%S"))
    r = cv.VaultIndex(tmp_path).search("时序检索方案", 5, now=now)
    assert r["hits"][0]["sid"] == "cv_new0000000000001", "同分并列时时间衰减应让新快照领先"


# ---------------- W3: 预算受限交接简报 (compress_brief) ----------------

def _big_env(tmp_path):
    """>100K tokens 的真实形态大快照: 长决策 + 多次重复读文件 + 重复报错 + 大段回显。"""
    rng = __import__("random").Random(42)
    blocks = [{"type": "text", "role": "user",
               "content": "重构检索引擎: 解决 tool_result 刷分与 800MB 索引膨胀"}]
    touched = [f"src/module_{i}.py" for i in range(20)]
    for i, path in enumerate(touched):
        blocks.append({"type": "text", "role": "assistant",
                       "content": f"决定：模块 {path} 采用饱和聚合方案, 关键拍板第{i}项", "ts": ""})
        for rep in range(4):  # 同文件多次读取 → 必须折叠为末次态
            blocks.append({"type": "tool_call", "call_id": f"r{i}_{rep}", "name": "read_file",
                           "arguments": {"path": path}, "ts": ""})
            blocks.append({"type": "tool_result", "call_id": f"r{i}_{rep}",
                           "content": " ".join(rng.choice("lorem ipsum dolor sit amet consectetur "
                                                          "adipiscing elit sed do".split())
                                               for _ in range(1200)), "ts": ""})
    for rep in range(10):  # 重复报错 → 去重 ×10
        blocks.append({"type": "tool_call", "call_id": f"e{rep}", "name": "run_cmd",
                       "arguments": {"command": "pytest"}, "ts": ""})
        blocks.append({"type": "tool_result", "call_id": f"e{rep}", "is_error": True,
                       "content": "AssertionError: flood guard failed at line 42" + " padding " * 30,
                       "ts": ""})
    blocks.append({"type": "text", "role": "assistant",
                   "content": "踩坑：直接 unlink 打开中的 db 句柄在 Windows 必失败, 已验证无效的死路",
                   "ts": ""})
    blocks.append({"type": "text", "role": "assistant",
                   "content": "最终停靠: 全部 20 模块改造完成, 测试全绿, 待提交", "ts": ""})
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": r"D:\proj", "session_ref": "s1"},
                             "conversation": blocks, "stats": {**cv.zero_stats(),
                                                               "blocks_total": len(blocks)}})
    (tmp_path / "snapshots").mkdir(parents=True, exist_ok=True)
    sid = "cv_big0000000000001"
    (tmp_path / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                        encoding="utf-8")
    return env, sid, touched


def test_brief_budget_4000_recalls_files_and_pitfalls(tmp_path):
    env, sid, touched = _big_env(tmp_path)
    env["_sid"] = sid
    raw_chars = sum(len(b.get("content") or b.get("summary") or "") for b in env["conversation"])
    assert raw_chars > 400_000  # 大快照原始内容 >400K chars ≈ >100K tokens (block_text 有 500 字符检索截断)

    import time as _t
    t0 = _t.perf_counter()
    brief, stats = cv.compress_brief(env, budget_tokens=4000)
    elapsed_ms = (_t.perf_counter() - t0) * 1000
    assert elapsed_ms < 100, f"4K 预算简报生成必须 <100ms (实测 {elapsed_ms:.1f}ms)"
    assert stats["budget_tokens"] == 4000 and stats["orig_tokens"] > stats["out_tokens"]
    assert stats["ratio"] < 0.2

    # 五槽位都在
    for sec in ("它停在哪儿", "关键架构裁决", "已证伪死路与踩坑", "最近动作", "触碰文件与工具", "活上下文尾部"):
        assert sec in brief, sec
    # 停靠态必保
    assert "最终停靠" in brief
    # 触碰文件召回 ≥95% (20 个文件至少 19 个)
    kept = sum(1 for p in touched if p in brief)
    assert kept >= 19, f"文件召回 {kept}/20"
    # 重复报错去重 ×10 只留一条计数行
    assert brief.count("AssertionError: flood guard failed") == 1
    assert "×10" in brief
    # 踩坑保留 (防接力 agent 重踩)
    assert "踩坑" in brief and "死路" in brief
    # 压缩统计元数据
    assert "## 压缩统计" in brief


def test_blocks_to_brief_backward_compat(tmp_path):
    """budget_tokens=None: 与 v0 签名/行为兼容 (do_import markdown 不带预算 = 宽预算)。"""
    env, sid, touched = _big_env(tmp_path)
    env["_sid"] = sid
    brief = cv.blocks_to_brief(env)
    assert len(brief) <= 48 * 1024 + 200
    assert "它停在哪儿" in brief and "src/module_0.py" in brief


def test_brief_budget_priority_ordering(tmp_path):
    """极小预算 (800 tok): 裁决/死路优先于活上下文尾部 — 效用最大化语义。"""
    env, sid, touched = _big_env(tmp_path)
    env["_sid"] = sid
    brief, stats = cv.compress_brief(env, budget_tokens=800)
    assert "关键架构裁决" in brief and "踩坑" in brief
    assert "最终停靠" in brief  # 停点必保


# ---------------- 链内块去重 (累积演进链体积治理) ----------------

def test_chain_block_dedup_and_parity(tmp_path):
    """累积链 v2 ⊇ v1: 重复块只索引首个出现; 检索折叠到叶子; 冷热语料集一致。"""
    b1 = [{"type": "text", "role": "user", "content": "phoenix 重试策略 定稿"}]
    _write(tmp_path, _env(b1), "cv_dedup000000000a")
    b2 = b1 + [{"type": "text", "role": "user", "content": "phoenix 重试策略 追加轮次"}]
    env2 = _env(b2)
    env2["lineage"]["parent_snapshot"] = "cv_dedup000000000a"
    _write(tmp_path, env2, "cv_dedup000000000b")
    now = time.time()
    idx = cv.VaultIndex(tmp_path)
    assert len(idx.meta) == 2
    assert len(idx._doc_sid) == 2, "重复块必须只索引首个出现 (2 唯一块)"
    hot = idx.search("重试策略", 10, now=now)
    cold = ColdRanker(_load_envs(tmp_path), "重试策略", now=now).search(10)
    assert [h["sid"] for h in hot["hits"]] == [h["sid"] for h in cold["hits"]]
    assert hot["hits"][0]["sid"] == "cv_dedup000000000b"   # 折叠代表 = 叶子
    assert hot["hits"][0]["folded"] == 1
    ctx_h = idx.context("重试策略", 5, now=now)
    ctx_c = ColdRanker(_load_envs(tmp_path), "重试策略", now=now).context(5)
    assert [(r["sid"], r["block"]) for r in ctx_h["results"]] == \
           [(r["sid"], r["block"]) for r in ctx_c["results"]]


def test_incremental_dedup_against_existing_chain(tmp_path):
    """增量: 新版本追加时对已索引祖先去重 (blockhash 账本)。"""
    b1 = [{"type": "text", "role": "user", "content": "cobalt 同步检查点 基线"}]
    _write(tmp_path, _env(b1), "cv_incr0000000000a")
    idx = cv.VaultIndex(tmp_path)
    assert len(idx._doc_sid) == 1
    b2 = b1 + [{"type": "text", "role": "user", "content": "cobalt 同步检查点 第二轮"}]
    env2 = _env(b2)
    env2["lineage"]["parent_snapshot"] = "cv_incr0000000000a"
    _write(tmp_path, env2, "cv_incr0000000000b")
    idx.rebuild()
    assert idx.stats["incr"] == 1
    assert len(idx._doc_sid) == 2, "增量必须对祖先已索引块去重"
    r = idx.search("检查点 第二轮", 5)
    assert r["hits"] and r["hits"][0]["sid"] == "cv_incr0000000000b"
