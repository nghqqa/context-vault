"""mcp_server 测试: stdio JSON-RPC 分发 + 冷热双路径等价 (铁律: 常驻只是加速, 不是依赖)。

运行: python -m pytest tests/ -v
隔离: SVC_BASE 指向不可达端口强制冷回退; 热路径用随机端口的临时 serve 实例。
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import mcp_server  # noqa: E402


def mk_vault(tmp_path: Path):
    """最小可搜 vault: 1 个含 'hello vault' 的快照。返回 (vault, sid)。"""
    vault = tmp_path / "vault"
    (vault / "snapshots").mkdir(parents=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\proj"},
                             "conversation": [{"type": "text", "role": "user",
                                               "content": "hello vault"}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    return vault, sid


def test_mcp_rpc_dispatch(tmp_path, monkeypatch):
    """initialize / tools/list / tools/call 全走本地冷路径 (SVC_BASE 不可达)。"""
    vault, sid = mk_vault(tmp_path)
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")  # 不可达 → 回退文件扫描

    r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, vault)
    assert r["result"]["serverInfo"]["name"] == "context-vault"

    tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                              vault)["result"]["tools"]
    assert len(tools) == 6 and {t["name"] for t in tools} >= {
        "vault_search", "vault_context", "vault_recent", "vault_stats",
        "vault_snapshot", "vault_remember"}

    call = lambda name, **kw: mcp_server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": name, "arguments": kw}}, vault)["result"]["content"][0]["text"]

    out = call("vault_search", query="hello vault")
    assert sid in out and "命中 1 个快照" in out
    out = call("vault_recent", limit=5)
    assert sid in out
    out = call("vault_stats")
    assert "账本为空" in out  # 空 vault 的诚实回退
    out = call("vault_snapshot", id=sid)
    assert "codex" in out and "hello vault" in out
    # 未知方法报 -32601; 未知工具按 MCP 规范返回 isError 而非炸 server
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}, vault)["error"]["code"] == -32601
    err = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                             "params": {"name": "nope_tool", "arguments": {}}}, vault)["result"]
    assert err["isError"] is True


def test_mcp_hot_cold_equivalence(tmp_path, monkeypatch):
    """铁律验证: 热索引 (via serve) 与冷回退 (文件扫描) 的搜索结果一致。"""
    vault, sid = mk_vault(tmp_path)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        hot = mcp_server.dispatch("vault_search", {"query": "hello vault"}, vault)
        hot_recent = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
    finally:
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    cold = mcp_server.dispatch("vault_search", {"query": "hello vault"}, vault)
    cold_recent = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
    assert "via 热索引" in hot and "via 热索引" not in cold   # 两条路径确实各自命中
    assert sid in hot and sid in cold
    assert "命中 1 个快照" in hot and "命中 1 个快照" in cold
    assert sid in hot_recent and sid in cold_recent
