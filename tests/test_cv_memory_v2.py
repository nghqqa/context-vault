"""双时态记忆引擎 v2 验收测试 (W2): 五类别提取 / v0.1 误杀修复 / A→B→C 推翻链 /
跨快照去重记信心 / 老库 schema 增量迁移 / active-only 检索与 include_history 追溯。

运行: python -m pytest tests/test_cv_memory_v2.py -v
铁律: 全部构造在 tmp_path 临时 vault, 绝不触碰生产 vault/。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_memory  # noqa: E402
import mcp_server  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "snapshots").mkdir(parents=True)
    return v


def _write_snap(vault: Path, sid: str, blocks: list, cwd: str = "D:\\proj"):
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": cwd, "session_ref": "s1"},
                             "conversation": blocks, "stats": cv.zero_stats()})
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                     encoding="utf-8")


def _extract(vault: Path, force: bool = True) -> dict:
    return cv.extract_memories_incremental(vault, engine_name="v2", force=force)


# ---------------- 五类别 + 出处 ----------------

def test_temporal_engine_kinds_and_provenance(tmp_path):
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_kinds0000000001", [
        {"type": "compaction", "summary": "早前确定了缓存 TTL=300", "replaced_blocks": [0, 0],
         "ts": "2026-01-01T00:00:00"},
        {"type": "text", "role": "assistant",
         "content": "决定：snapshot_id 交换采用确定性导出方案", "ts": "2026-01-02T08:30:00"},
        {"type": "text", "role": "user", "content": "记住：以后所有提交都必须带签名",
         "ts": "2026-01-02T09:00:00"},
        {"type": "text", "role": "assistant",
         "content": "踩坑：cv_index.pkl 在 Windows 句柄未关时 unlink 必失败，根因是共享写打开",
         "ts": "2026-01-03T10:00:00"},
        {"type": "text", "role": "assistant",
         "content": "架构铁律：snapshots 目录绝不可写入，所有派生数据只住索引层",
         "ts": "2026-01-03T11:00:00"},
    ])
    r = _extract(vault)
    assert r["engine"] == "v2"
    conn = sqlite3.connect(vault / "vault.db")
    conn.row_factory = sqlite3.Row
    rows = {r2["kind"]: r2 for r2 in conn.execute("SELECT * FROM memory_items")}
    conn.close()
    assert {"summary", "decision", "preference", "pitfall", "constraint"} <= set(rows)
    dec = rows["decision"]
    assert dec["block_idx"] == 1 and dec["event_ts"] == "2026-01-02T08:30:00"
    assert dec["topic_key"] and "proj" in dec["topic_key"]
    assert dec["status"] == "active" and dec["confidence"] == 1.0
    # event_ts 必须取块内真实时间, 不是提取时刻
    assert rows["pitfall"]["event_ts"] == "2026-01-03T10:00:00"


# ---------------- v0.1 误杀修复 (真实缺陷回归) ----------------

def test_noise_gate_v2_keeps_technical_decisions(tmp_path):
    """v0.1 硬闸门误杀案例: 单对反引号技术实体 + Markdown 结构化裁决行 — v2 必须保留。"""
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_noise0000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定采用 `FTS5` 虚表做全文索引", "ts": "2026-02-01T00:00:00"},
        {"type": "text", "role": "assistant",
         "content": "- **结论**：我们决定采用单实例自托管方案", "ts": "2026-02-01T01:00:00"},
    ])
    r = _extract(vault)
    assert r["added"] >= 2, "两个被 v0.1 误杀的技术裁决必须由 v2 保留"
    # rule 引擎 (v0.1 语义) 在同样语料上确实误杀 — 消融对照的证据面
    env = json.loads((vault / "snapshots" / "cv_noise0000000001.json").read_text(encoding="utf-8"))
    env["_sid"] = "cv_noise0000000001"
    rule_kinds = [it["kind"] for it in cv_memory.extract_memories(env)]
    assert "decision" not in rule_kinds, "v0.1 规则引擎应复现误杀 (对照基线不许被修好)"
    # ≥2 对反引号的代码罗列仍然被 v2 杀掉
    assert cv_memory._memory_line_noise("用 `a` 加 `b` 再 `c` 拼接") is True
    assert cv_memory._memory_line_noise("决定采用 `FTS5` 索引") is False


# ---------------- A→B→C 推翻链 (W2 核心验收) ----------------

def test_supersede_abc_chain(tmp_path):
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_supA00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：deploy.yaml 的调度策略采用 daily 批处理", "ts": "2026-01-01T00:00:00"}])
    _write_snap(vault, "cv_supB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：deploy.yaml 的调度策略改为 weekly 批处理", "ts": "2026-02-01T00:00:00"}])
    _write_snap(vault, "cv_supC00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：deploy.yaml 的调度策略改为 monthly 批处理", "ts": "2026-03-01T00:00:00"}])
    r = _extract(vault)
    assert r["superseded"] >= 2

    conn = sqlite3.connect(vault / "vault.db")
    conn.row_factory = sqlite3.Row
    rows = {r2["content"]: r2 for r2 in conn.execute(
        "SELECT * FROM memory_items WHERE kind='decision'")}
    conn.close()
    assert len(rows) == 3
    a = next(v for v in rows.values() if "daily" in v["content"])
    b = next(v for v in rows.values() if "weekly" in v["content"])
    c = next(v for v in rows.values() if "monthly" in v["content"])
    assert a["status"] == "superseded" and a["superseded_by"] == b["id"]
    assert b["status"] == "superseded" and b["superseded_by"] == c["id"]
    assert c["status"] == "active"

    # 默认检索: 只回 C, 过期记忆不污染
    hits = cv.memory_search_api(vault, "deploy 调度", 10)
    contents = [h["content"] for h in hits]
    assert any("monthly" in t for t in contents)
    assert not any("daily" in t or "weekly" in t for t in contents), \
        "默认查询绝不允许召回已推翻记忆 (v0 高噪缺陷)"

    # include_history: 演变链完整可查
    hist = cv.memory_search_api(vault, "deploy 调度", 10, include_history=True)
    st = {h["content"]: h["status"] for h in hist}
    assert st and all(len(st) == 3 for _ in [0]) and len(hist) == 3
    superseded = [h for h in hist if h["status"] == "superseded"]
    assert {h["superseded_by"] for h in superseded} == {c["id"], b["id"]}


# ---------------- 跨快照去重 (re-affirm 记信心) ----------------

def test_dedup_reaffirm_confidence(tmp_path):
    vault = _vault(tmp_path)
    blk = [{"type": "text", "role": "assistant",
            "content": "决定：迁移报告必须逐条列出不可迁移项", "ts": "2026-04-01T00:00:00"}]
    _write_snap(vault, "cv_dupA00000000001", blk)
    _write_snap(vault, "cv_dupB00000000001", blk)  # 同内容: 演进链上的重复摘录
    r = _extract(vault)
    conn = sqlite3.connect(vault / "vault.db")
    n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE content LIKE '%迁移报告%'").fetchone()[0]
    conf = conn.execute("SELECT confidence FROM memory_items WHERE content LIKE '%迁移报告%'").fetchone()[0]
    conn.close()
    assert n == 1, "同内容跨快照只保留一条 (v0 重复刷屏缺陷)"
    assert conf > 1.0, "重复出现应提升置信而非新增行"


# ---------------- 老库 schema 增量迁移 ----------------

def test_schema_migration_from_v01_db(tmp_path):
    vault = _vault(tmp_path)
    conn = sqlite3.connect(vault / "vault.db")
    conn.executescript("""
CREATE TABLE memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
  kind TEXT, content TEXT, project TEXT, hits INTEGER DEFAULT 0, last_used TEXT);
INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)
  VALUES('t','cv_old00000000001','decision','决定：老库旧记忆','oldproj');
""")
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(vault / "vault.db")
    cv.ensure_memory_schema(conn2)
    conn2.close()
    # 老行经 DEFAULT 回填 active, 新列齐备; 检索照常工作
    hits = cv.memory_search_api(vault, "老库旧记忆", 5)
    assert hits and hits[0]["status"] == "active"
    assert hits[0]["snapshot"] == "cv_old00000000001"
    assert hits[0]["block_idx"] is None  # v0.1 时代未记录出处 — 诚实为 NULL, 不伪造


# ---------------- MCP vault_remember 双时态契约 ----------------

def test_mcp_remember_active_only_and_history(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_mcpA00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：sync.yaml 的水位策略采用 mtime 方案", "ts": "2026-05-01T00:00:00"}])
    _write_snap(vault, "cv_mcpB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：sync.yaml 的水位策略改为 ordinal 方案", "ts": "2026-06-01T00:00:00"}])
    _extract(vault)
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")  # 强制冷路径
    default_out = mcp_server.dispatch("vault_remember", {"query": "水位策略"}, vault)
    assert "mtime" not in default_out and "ordinal" in default_out
    hist_out = mcp_server.dispatch("vault_remember",
                                   {"query": "水位策略", "include_history": True}, vault)
    assert "mtime" in hist_out and "已推翻" in hist_out
