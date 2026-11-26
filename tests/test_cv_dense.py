"""审核修复验收测试: 精确原写词项 / V1≠V2 区分度 / MCP 只读索引冷路径 /
cv_dense 接线 (stub 缓存 + 故障回退) / cmd_memory 老库迁移。

运行: python -m pytest tests/test_cv_semantic.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_semantic  # noqa: E402
import mcp_server  # noqa: E402
from cv_index import ColdRanker, VaultIndex, tokenize  # noqa: E402


def _write(vault: Path, sid: str, blocks: list) -> None:
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\proj",
                                         "session_ref": "s1"},
                             "conversation": blocks, "stats": cv.zero_stats()})
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                     encoding="utf-8")


# ---------------- 精确原写词项 (VaultIndex 精确优先, 跨写法仍召回) ----------------

def test_exact_original_writing_prefers_exact_doc(tmp_path):
    _write(tmp_path, "cv_exactcamel00001",
           [{"type": "text", "role": "assistant",
             "content": "配置入口是 VaultIndex 类"}])
    _write(tmp_path, "cv_exactsnake00001",
           [{"type": "text", "role": "assistant",
             "content": "配置入口是 vault_index 模块"}])
    idx = VaultIndex(tmp_path)
    hits = idx.search("VaultIndex", 5)["hits"]
    assert hits, "两文档都应命中 (跨写法召回)"
    assert hits[0]["sid"] == "cv_exactcamel00001", "精确原写文档必须排第一"
    hits2 = idx.search("vault_index", 5)["hits"]
    assert hits2[0]["sid"] == "cv_exactsnake00001", "反向同理"
    # 双向召回都包含两文档
    assert {h["sid"] for h in hits} == {h["sid"] for h in hits2} == \
        {"cv_exactcamel00001", "cv_exactsnake00001"}


def test_v1_vanilla_distinct_from_v2_code_aware(tmp_path):
    """V1 (vanilla) 命中不了跨写法查询, V2 (code-aware) 可以 — 消融区分度修复验证。"""
    _write(tmp_path, "cv_camelcase0000001",
           [{"type": "text", "role": "assistant",
             "content": "配置常量统一为 RetryBudgetMs 命名"}])
    v1 = VaultIndex(tmp_path, tokenizer=lambda t: tokenize(t, code_aware=False),
                    weights=cv.UNIFORM, code_aware=False, config_id="vanilla")
    v2 = VaultIndex(tmp_path, config_id="v2")
    q = "retry_budget_ms"
    assert not v1.search(q, 5)["hits"], "vanilla 分词必须命中不了 snake→camel 跨写法"
    assert v2.search(q, 5)["hits"], "代码感知分词经复合词桥接必须命中"


def test_config_fingerprint_prevents_reuse(tmp_path):
    """同目录落盘的另一配置索引必须判失效重建 (消融防串档闸)。"""
    _write(tmp_path, "cv_cfgfp00000000001",
           [{"type": "text", "role": "user", "content": "hello 配置指纹"}])
    a = VaultIndex(tmp_path, config_id="cfg-a")
    assert a.stats["cache_load"] == 0
    b = VaultIndex(tmp_path, config_id="cfg-a")
    assert b.stats["cache_load"] == 1, "同配置应正常复用"
    c = VaultIndex(tmp_path, config_id="cfg-b")
    assert c.stats["cache_load"] == 0 and c.stats["full"] == 1, "异配置必须判失效全量重建"


# ---------------- MCP 只读索引冷路径 (无 HTTP 服务, 毫秒级) ----------------

def test_mcp_cold_path_uses_search_index_db(tmp_path, monkeypatch):
    _write(tmp_path, "cv_roidx0000000001",
           [{"type": "text", "role": "user", "content": "phoenix 迁移方案 关键决策"}])
    VaultIndex(tmp_path)  # 落盘 search_index.db
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    # 守卫: 若冷路径误回退全量扫描 (ColdRanker), 立即炸出 — 证明走的是只读索引
    def _boom(*a, **k):
        raise AssertionError("应走 search_index.db 只读路径, 而非 ColdRanker 全量扫描")
    monkeypatch.setattr(cv, "ColdRanker", _boom)
    out = mcp_server.dispatch("vault_search", {"query": "phoenix 迁移"}, tmp_path)
    assert "cv_roidx0000000001" in out and "via 本地索引" in out
    out2 = mcp_server.dispatch("vault_context", {"query": "phoenix 迁移"}, tmp_path)
    assert "cv_roidx0000000001" in out2


def test_mcp_cold_path_falls_back_without_index(tmp_path, monkeypatch):
    _write(tmp_path, "cv_noidx00000000001",
           [{"type": "text", "role": "user", "content": "cobalt 检查点 方案"}])
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    out = mcp_server.dispatch("vault_search", {"query": "检查点"}, tmp_path)
    assert "cv_noidx00000000001" in out and "via 本地索引" not in out


def test_readonly_never_writes_or_deletes(tmp_path):
    _write(tmp_path, "cv_roguard000000001",
           [{"type": "text", "role": "user", "content": "只读守卫 测试"}])
    VaultIndex(tmp_path)
    db = tmp_path / "search_index.db"
    mtime0 = db.stat().st_mtime
    ro = VaultIndex(tmp_path, readonly=True)
    assert ro.search("只读守卫", 5)["hits"]
    ro.rebuild()  # 只读模式必须拒绝重建
    assert db.stat().st_mtime == mtime0, "readonly 绝不落盘"
    # 只读模式下损坏检测不删除文件
    ro._store is not None and ro._close()
    db.write_bytes(b"garbage not sqlite \x00")
    broken = VaultIndex(tmp_path, readonly=True)
    assert getattr(broken, "_broken", False)
    assert db.exists(), "只读模式绝不删除索引文件"


# ---------------- cv_dense: stub 缓存命中 + 故障优雅回退 ----------------

class _EmbedStub(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        texts = body.get("input", [])
        _EmbedStub.calls += 1
        vecs = [[float((len(t) * 7) % 13 + 1.0), float((len(t) * 3) % 5 + 1.0)] for t in texts]
        out = json.dumps({"data": [{"embedding": v} for v in vecs]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_dense_hybrid_cache_and_fallback(tmp_path, monkeypatch):
    _write(tmp_path, "cv_dense00000000001",
           [{"type": "text", "role": "user", "content": "phoenix 幂等键方案 关键决策"}])
    idx = VaultIndex(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        provider = cv_semantic.DenseProvider(url=f"http://127.0.0.1:{port}/v1/embeddings",
                                          model="stub-embed")
        cache = cv_semantic.EmbeddingCache(tmp_path)
        r1 = cv_semantic.hybrid_search(idx, tmp_path, "幂等键", 5,
                                    provider=provider, cache=cache)
        assert r1.get("hybrid") is True and r1["hits"]
        calls_after_first = _EmbedStub.calls
        r2 = cv_semantic.hybrid_search(idx, tmp_path, "幂等键", 5,
                                    provider=provider, cache=cache)
        assert r2["hits"][0]["sid"] == r1["hits"][0]["sid"]
        assert _EmbedStub.calls == calls_after_first + 1, \
            "快照向量必须命中缓存 (第二次仅新增 1 次查询向量请求)"
        st = cache.stats()
        assert st["entries"] >= 1
    finally:
        srv.shutdown()
        srv.server_close()

    # 故障回退: 端点不可达 → 优雅回退纯结构化结果, 绝不抛异常
    bad = cv_semantic.DenseProvider(url="http://127.0.0.1:1/v1/embeddings")
    r3 = cv_semantic.hybrid_search(idx, tmp_path, "幂等键", 5, provider=bad)
    assert r3.get("hybrid") is False and r3["hits"], "故障必须回退纯 BM25 结果"


def test_dense_hybrid_fallback_respects_limit_and_closes_cache(tmp_path):
    """回退路径 (未配置 URL / 端点故障) 必须严格按 limit 截断, 且内部创建的 EmbeddingCache
    必须在返回前关闭句柄 (Windows 上立刻可删 embeddings.db)。"""
    for i in range(6):
        _write(tmp_path, f"cv_lim{i:02d}0000000001",
               [{"type": "text", "role": "user", "content": f"共同锚词 fallbacklimit7 快照 {i}"}])
    idx = VaultIndex(tmp_path)
    try:
        unconfigured = cv_semantic.DenseProvider(url="")
        r_unconf = cv_semantic.hybrid_search(idx, tmp_path, "fallbacklimit7", 2, provider=unconfigured)
        assert r_unconf["hybrid"] is False
        assert len(r_unconf["hits"]) == 2, f"未配置回退不得越过 limit=2, 实得 {len(r_unconf['hits'])}"

        bad = cv_semantic.DenseProvider(url="http://127.0.0.1:1/v1/embeddings")
        r_err = cv_semantic.hybrid_search(idx, tmp_path, "fallbacklimit7", 2, provider=bad)
        assert r_err["hybrid"] is False
        assert len(r_err["hits"]) == 2, f"故障回退不得越过 limit=2, 实得 {len(r_err['hits'])}"
        emb_db = tmp_path / "embeddings.db"
        if emb_db.exists():
            emb_db.unlink()  # 若句柄泄漏, Windows 下此处必抛 PermissionError
    finally:
        idx._close()


# ---------------- cmd_memory 老库迁移 (P0-4 回归) ----------------

def test_cmd_memory_on_legacy_schema(tmp_path, capsys, monkeypatch):
    """v0.1 老库 (memory_items 无 topic_key 列) 上 cv memory search/list 不再崩。"""
    vault = tmp_path / "vault"
    (vault / "snapshots").mkdir(parents=True)
    conn = sqlite3.connect(vault / "vault.db")
    conn.executescript("""
CREATE TABLE memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
  kind TEXT, content TEXT, project TEXT, hits INTEGER DEFAULT 0, last_used TEXT);
INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)
  VALUES('t','cv_legacy00000001','decision','决定：采用确定性导出 id 方案','proj');
""")
    conn.commit()
    conn.close()
    from types import SimpleNamespace
    ns = SimpleNamespace(vault=str(vault), memory_cmd="search", query="确定性导出",
                         limit=5, history=False, engine=None)
    cv.cmd_memory(ns)  # 老库上必须正常工作 (曾炸 no such column: topic_key)
    out = capsys.readouterr().out
    assert "确定性导出" in out
    ns.history = True
    cv.cmd_memory(ns)
    assert "确定性导出" in capsys.readouterr().out
    ns.memory_cmd = "list"
    cv.cmd_memory(ns)
    assert "decision" in capsys.readouterr().out
