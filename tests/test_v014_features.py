"""v0.14.0 新功能集成测试：
1. Scoped Filtering (project / agent / since) 在热索引、只读索引、冷扫三路严格等价；
2. MCP 双向写入 vault_memorize（冷/热路径、置顶、supersedes 显式推翻旧记忆、参数校验与不可变保证）；
3. MCP / HTTP vault_handoff（按 id / 按 project / 缺省最新 / budget_tokens / include_memories / 越界拦截）；
4. cv memory compile-rules 项目规则编译器（优先级排序、预算裁剪、dry-run、哨兵保护手写内容、.bak-cv 备份、幂等更新与 HTTP 安全校验）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_core  # noqa: E402
import cv_memory  # noqa: E402
import mcp_server  # noqa: E402


def _write_snapshot(
    vault: Path,
    *,
    agent: str,
    cwd: str,
    created_at: str,
    blocks: list[dict],
    session_ref: str = "s1",
    parent_snapshot: str | None = None,
) -> tuple[str, Path]:
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = {
        "format": "context-snapshot/0.1",
        "created_at": created_at,
        "capture": {
            "agent": agent,
            "source_format": f"{agent}-native",
            "completeness": "full",
            "cwd": cwd,
            "session_ref": session_ref,
        },
        "lineage": {"parent_snapshot": parent_snapshot, "fork_of": None},
        "stats": {**cv.zero_stats(), "blocks_total": len(blocks)},
        "conversation": blocks,
    }
    sid = cv.snapshot_id(env)
    p = vault / "snapshots" / f"{sid}.json"
    cv.atomic_write_text(p, json.dumps(env, ensure_ascii=False))
    return sid, p


def _index_vault(vault: Path) -> None:
    conn = cv.db(vault)
    try:
        conn.executescript(cv.SCHEMA + cv.MEMORY_TABLE)
        for p in sorted((vault / "snapshots").glob("cv_*.json")):
            env = json.loads(p.read_text(encoding="utf-8"))
            cv.register_snapshot(conn, env, p.stem, p)
        conn.commit()
    finally:
        conn.close()
    idx = cv.VaultIndex(vault, cv.block_text)
    idx.rebuild(force=True)
    idx._close()


def _http_json(url: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"User-Agent": cv_core.user_agent()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Origin"] = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _seed_scoped_vault(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    vault = tmp_path / "vault"
    now_local = dt.datetime.now().astimezone()
    recent_2d = (now_local - dt.timedelta(days=2)).isoformat(timespec="seconds")
    recent_3d = (now_local - dt.timedelta(days=3)).isoformat(timespec="seconds")
    old_45d = (now_local - dt.timedelta(days=45)).isoformat(timespec="seconds")

    sids = {}
    sids["alpha_claude"], _ = _write_snapshot(
        vault,
        agent="claude-code",
        cwd=r"E:\work\alpha-app",
        created_at=recent_2d,
        session_ref="c1",
        blocks=[
            {"type": "text", "role": "user", "content": "请梳理 vault_token_pipeline 设计与缓存策略"},
            {"type": "text", "role": "assistant", "content": "决定：alpha 项目 vault_token_pipeline 采用单调时钟窗口统计。"},
        ],
    )
    sids["alpha_codex_old"], _ = _write_snapshot(
        vault,
        agent="codex",
        cwd=r"E:\work\alpha-app",
        created_at=old_45d,
        session_ref="c2",
        blocks=[
            {"type": "text", "role": "user", "content": "早期 vault_token_pipeline 原型排查与压测"},
            {"type": "text", "role": "assistant", "content": "已完成早期原型压测。"},
        ],
    )
    sids["beta_zcode"], _ = _write_snapshot(
        vault,
        agent="zcode",
        cwd=r"D:\repo\beta-service",
        created_at=recent_3d,
        session_ref="c3",
        blocks=[
            {"type": "text", "role": "user", "content": "beta 服务 vault_token_pipeline 异步队列落地"},
            {"type": "text", "role": "assistant", "content": "约束：beta 服务禁止阻塞主线程。"},
        ],
    )
    _index_vault(vault)
    return vault, sids


def test_scoped_filtering_three_way_equivalence(tmp_path, monkeypatch):
    """project / agent (含 claude 别名) / since (ISO + 7d) 在热服务、只读索引与冷扫三路完全一致。"""
    vault, sids = _seed_scoped_vault(tmp_path)

    # 额外写入两条项目记忆验证 vault_remember scoped filtering
    cv_memory.memory_add_manual(vault, "alpha 强制启用 WAL 模式", kind="constraint", project="alpha-app", pinned=True)
    old_ts = (dt.datetime.now().astimezone() - dt.timedelta(days=40)).isoformat(timespec="seconds")
    conn = cv.db(vault)
    try:
        conn.execute(
            "INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,event_ts,status,confidence,pinned)"
            " VALUES(?,'manual','decision','alpha 旧方案采用轮询','alpha-app',?,'active',3.0,0)",
            (old_ts, old_ts),
        )
        conn.execute(
            "INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,event_ts,status,confidence,pinned)"
            " VALUES(?,'manual','decision','beta 启用双写队列',?, ?, 'active', 4.0, 0)",
            (dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             r"D:\repo\beta-service",
             dt.datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()

    # 1) 热路径 vs 只读索引 vs 冷文件扫描对比 vault_search & vault_context
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        hot_proj = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "project": "alpha-app"}, vault)
        hot_agent = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "agent": "claude"}, vault)
        hot_since = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "since": "14d"}, vault)
        hot_ctx = mcp_server.dispatch("vault_context", {"query": "vault_token_pipeline", "project": "beta-service"}, vault)
        hot_mem = mcp_server.dispatch("vault_remember", {"query": "模式", "project": r"E:\work\alpha-app", "since": "7d"}, vault)
        # 非法 since 在热路径必须返回 400 -> MCP isError=True
        bad_hot = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "vault_search", "arguments": {"query": "vault_token_pipeline", "since": "not-a-date"}}},
            vault,
        )
        assert bad_hot["result"]["isError"] is True
        # 且非法参数不应污染负缓存 (下一次正常查询仍走热索引)
        still_hot = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "agent": "zcode"}, vault)
        assert "via 热索引" in still_hot and sids["beta_zcode"] in still_hot
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()
    ro_proj = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "project": "alpha-app"}, vault)
    ro_agent = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "agent": "claude"}, vault)
    ro_since = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "since": "14d"}, vault)
    ro_ctx = mcp_server.dispatch("vault_context", {"query": "vault_token_pipeline", "project": "beta-service"}, vault)
    ro_mem = mcp_server.dispatch("vault_remember", {"query": "模式", "project": r"E:\work\alpha-app", "since": "7d"}, vault)

    # 移除 search_index.db 强制冷全扫 ColdRanker
    (vault / "search_index.db").unlink()
    cold_proj = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "project": "alpha-app"}, vault)
    cold_agent = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "agent": "claude"}, vault)
    cold_since = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline", "since": "14d"}, vault)
    cold_ctx = mcp_server.dispatch("vault_context", {"query": "vault_token_pipeline", "project": "beta-service"}, vault)

    for out in (hot_proj, ro_proj, cold_proj):
        assert sids["alpha_claude"] in out and sids["alpha_codex_old"] in out
        assert sids["beta_zcode"] not in out
    for out in (hot_agent, ro_agent, cold_agent):
        assert sids["alpha_claude"] in out
        assert sids["alpha_codex_old"] not in out and sids["beta_zcode"] not in out
    for out in (hot_since, ro_since, cold_since):
        assert sids["alpha_claude"] in out and sids["beta_zcode"] in out
        assert sids["alpha_codex_old"] not in out
    for out in (hot_ctx, ro_ctx, cold_ctx):
        assert sids["beta_zcode"] in out and sids["alpha_claude"] not in out
    for out in (hot_mem, ro_mem):
        assert "alpha 强制启用 WAL 模式" in out
        assert "轮询" not in out and "双写队列" not in out


def test_mcp_vault_memorize_cold_hot_and_supersedes(tmp_path, monkeypatch):
    """vault_memorize 冷/热双路径写入、置顶、supersedes 显式推翻旧记忆及原快照不可变性。"""
    vault, sids = _seed_scoped_vault(tmp_path)
    snap_path = vault / "snapshots" / f"{sids['alpha_claude']}.json"
    snap_sha_before = hashlib.sha256(snap_path.read_bytes()).hexdigest()

    # 1) 冷路径写入旧方案
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()
    r1 = mcp_server.dispatch("vault_memorize", {
        "content": "认证缓存暂定使用内存字典 TTL=60s",
        "kind": "decision",
        "project": "alpha-app",
        "pinned": False,
    }, vault)
    assert "已写入长期记忆" in r1 and "alpha-app" in r1

    old_rows = cv_memory.memory_search_api(vault, "TTL", project="alpha-app")
    assert len(old_rows) == 1
    old_id = old_rows[0]["id"]
    assert old_rows[0]["snapshot"] == "mcp"

    # 2) 热路径用新红线显式 supersedes 旧记忆
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        r2 = mcp_server.dispatch("vault_memorize", {
            "content": "红线：认证缓存统一迁入 Redis 并要求 300s 滑动过期",
            "kind": "constraint",
            "project": "alpha-app",
            "pinned": True,
            "supersedes": old_id,
        }, vault)
        assert "via 热索引" in r2 and f"#{old_id}" in r2 and "已置顶" in r2

        # 默认检索只见新红线、旧记忆已被推翻不污染默认上下文
        rem_active = mcp_server.dispatch("vault_remember", {"query": "认证缓存", "project": "alpha-app"}, vault)
        assert "Redis" in rem_active and "内存字典" not in rem_active

        # include_history=True 可追溯演变链
        rem_hist = mcp_server.dispatch("vault_remember", {
            "query": "认证缓存",
            "project": "alpha-app",
            "include_history": True,
        }, vault)
        assert "Redis" in rem_hist and "内存字典" in rem_hist and "已推翻" in rem_hist

        # 非法 supersedes -> isError=True
        err = mcp_server.handle({
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "vault_memorize", "arguments": {"content": "test", "supersedes": 999999}},
        }, vault)
        assert err["result"]["isError"] is True
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    # 原快照字节校验零修改
    assert hashlib.sha256(snap_path.read_bytes()).hexdigest() == snap_sha_before


def test_mcp_and_http_vault_handoff(tmp_path, monkeypatch):
    """vault_handoff 按 id / 按 project / 附带项目记忆 / 预算控制 / 越权 id 拦截。"""
    vault, sids = _seed_scoped_vault(tmp_path)
    cv_memory.memory_add_manual(
        vault, "架构约束：alpha-app 所有时间窗计算必须统一用本地时区 ISO",
        kind="constraint", project="alpha-app", pinned=True,
    )

    # 冷路径: 按 project 定位最新快照 + 自动带出 alpha-app 记忆
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()
    cold = mcp_server.dispatch("vault_handoff", {
        "project": "alpha-app",
        "budget_tokens": 2500,
        "include_memories": True,
    }, vault)
    assert sids["alpha_claude"] in cold
    assert "项目当前有效记忆" in cold and "本地时区 ISO" in cold

    # 不附带记忆
    no_mem = mcp_server.dispatch("vault_handoff", {
        "id": sids["beta_zcode"],
        "include_memories": False,
    }, vault)
    assert sids["beta_zcode"] in no_mem and "项目当前有效记忆" not in no_mem

    # 热路径 HTTP /handoff 一致性与越界保护
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        hot = mcp_server.dispatch("vault_handoff", {"project": "alpha-app", "budget_tokens": 2500}, vault)
        assert "via 热索引" in hot and sids["alpha_claude"] in hot

        code, body = _http_json(f"http://127.0.0.1:{port}/handoff?id=../secret")
        assert code == 400 and "error" in body
        code404, _ = _http_json(f"http://127.0.0.1:{port}/handoff?id=cv_deadbeef0000")
        assert code404 == 404
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()


def test_compile_project_rules_api_cli_and_http(tmp_path, capsys):
    """compile-rules: 排序/预算截断/干跑/哨兵原地替换保留手写 prose/.bak-cv 备份/二次幂等/HTTP 安全检验。"""
    vault = tmp_path / "vault"
    proj_dir = tmp_path / "my-proj"
    proj_dir.mkdir(parents=True)
    _write_snapshot(
        vault,
        agent="codex",
        cwd=str(proj_dir),
        created_at="2026-11-25T10:00:00+08:00",
        blocks=[{"type": "text", "role": "user", "content": "init"}],
    )
    _index_vault(vault)

    cv_memory.memory_add_manual(vault, "绝不把源码路径写入静态快照", kind="constraint", project="my-proj", pinned=True)
    cv_memory.memory_add_manual(vault, "后端采用 Python stdlib 零依赖", kind="decision", project=str(proj_dir), pinned=False)
    cv_memory.memory_add_manual(vault, "Windows 下 Open 后务必显式关闭 sqlite 连接", kind="pitfall", project="my-proj", pinned=False)
    cv_memory.memory_add_manual(vault, "回复全部使用简洁中文", kind="preference", project="my-proj", pinned=False)

    # 先写入用户手写 AGENTS.md
    agents_file = proj_dir / "AGENTS.md"
    handwritten = "# Team Notes\n\n请勿覆盖这一段手工约定。\n"
    agents_file.write_text(handwritten, encoding="utf-8")

    # 1) dry-run 默认不触盘
    dry = cv_memory.compile_project_rules(vault, "my-proj", target="agents", budget_tokens=2000, apply=False)
    assert dry["applied"] is False and dry["changed"] is True
    assert dry["included"] == 4 and dry["out_path"] == str(agents_file.resolve())
    assert agents_file.read_text(encoding="utf-8") == handwritten
    # 顺序检查: constraint 必须在 decision / pitfall / preference 前面
    bmd = dry["block_markdown"]
    assert bmd.index("绝不把源码路径") < bmd.index("后端采用 Python") < bmd.index("Windows 下 Open") < bmd.index("回复全部使用简洁中文")

    # 2) apply=True 原地附加哨兵块并备份 .bak-cv，同时保留用户手写文本
    res1 = cv_memory.compile_project_rules(vault, "my-proj", target="agents", budget_tokens=2000, apply=True)
    assert res1["applied"] is True and res1["backup_path"] is not None
    assert Path(res1["backup_path"]).read_text(encoding="utf-8") == handwritten
    after1 = agents_file.read_text(encoding="utf-8")
    assert "请勿覆盖这一段手工约定。" in after1
    assert "<!-- context-vault:rules:start" in after1 and "<!-- context-vault:rules:end -->" in after1

    # 3) 新增一条记忆后再次 apply: 只更新哨兵区块内部, 区块外用户手写文本依然完整保留
    cv_memory.memory_add_manual(vault, "红线二：接口必须保持冷热一致", kind="constraint", project="my-proj", pinned=True)
    res2 = cv_memory.compile_project_rules(vault, "my-proj", target="agents", budget_tokens=2000, apply=True)
    assert res2["applied"] is True and res2["included"] == 5
    after2 = agents_file.read_text(encoding="utf-8")
    assert after2.count("<!-- context-vault:rules:start") == 1
    assert "请勿覆盖这一段手工约定。" in after2 and "红线二" in after2

    # 4) 二次无变更运行保持幂等 (applied=False, changed=False)
    res3 = cv_memory.compile_project_rules(vault, "my-proj", target="agents", budget_tokens=2000, apply=True)
    assert res3["changed"] is False and res3["applied"] is False

    # 5) 紧凑预算裁剪必须舍弃低优条目，同时优先保留红线 constraint
    tight = cv_memory.compile_project_rules(vault, "my-proj", target="claude", budget_tokens=220, apply=False)
    assert tight["dropped"] >= 1 and tight["tokens_est"] <= 220

    # 6) CLI compile-rules --json 可用
    cv.main(["--vault", str(vault), "memory", "compile-rules", "--project", "my-proj", "--target", "cursor", "--json"])
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["target"] == "cursor" and cli_out["applied"] is False and cli_out["included"] == 5

    # 7) HTTP 端写非规则文件 (.py) 必须被拒绝 400
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code_bad, body_bad = _http_json(
            f"http://127.0.0.1:{port}/memory/compile_rules",
            method="POST",
            body={"project": "my-proj", "out_path": str(proj_dir / "evil.py"), "apply": True},
        )
        assert code_bad == 400 and "error" in body_bad

        code_ok, body_ok = _http_json(
            f"http://127.0.0.1:{port}/memory/compile_rules?project=my-proj&target=claude&budget=2000",
        )
        assert code_ok == 200 and body_ok["included"] == 5
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()


def test_mcp_401_and_stale_route_fallback_without_poisoning(tmp_path, monkeypatch):
    """401 未鉴权与旧版服务 404 unknown route 必须优雅回退冷路径; 真实 400/业务 404 不污染负缓存."""
    vault, sids = _seed_scoped_vault(tmp_path)
    cv_memory.memory_add_manual(vault, "alpha 强制校验回退路径", kind="constraint", project="alpha-app", pinned=True)

    # 1) 带 --token 的服务而 MCP 未配 CV_SERVE_TOKEN -> 401 时必须自动回退本地
    srv, idx = cv.build_vault_server(vault, 0, token="secret-token-xyz")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CV_SERVE_TOKEN", "")
        monkeypatch.delenv("CV_SERVE_TOKEN", raising=False)
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()

        r_search = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "vault_search", "arguments": {"query": "vault_token_pipeline", "project": "alpha-app"}},
        }, vault)["result"]
        assert r_search["isError"] is False and sids["alpha_claude"] in r_search["content"][0]["text"]
        assert "via 热索引" not in r_search["content"][0]["text"]
        assert mcp_server._svc_skip() is True

        r_rem = mcp_server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "vault_remember", "arguments": {"query": "回退路径", "project": "alpha-app"}},
        }, vault)["result"]
        assert r_rem["isError"] is False and "强制校验回退路径" in r_rem["content"][0]["text"]

        r_ho = mcp_server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "vault_handoff", "arguments": {"project": "alpha-app"}},
        }, vault)["result"]
        assert r_ho["isError"] is False and sids["alpha_claude"] in r_ho["content"][0]["text"]
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    # 2) 模拟旧版 0.13.1 服务: /handoff 返回 404 unknown route, /search 正常 -> 回退本地且不触发负缓存
    from http.server import BaseHTTPRequestHandler

    class _LegacyHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/handoff"):
                body = json.dumps({"error": "unknown route", "routes": ["/healthz", "/search"]}).encode("utf-8")
                self.send_response(404)
            else:
                body = json.dumps({
                    "query": "vault_token_pipeline", "terms": ["vault_token_pipeline"], "total": 1,
                    "hits": [{"sid": sids["alpha_claude"], "agent": "claude-code", "score": 9.9,
                              "cwd": r"E:\work\alpha-app", "created_at": "2026-11-26T10:00:00", "folded": 0}],
                }).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    legacy_srv = cv_core.LocalThreadingHTTPServer(("127.0.0.1", 0), _LegacyHandler)
    legacy_port = legacy_srv.server_address[1]
    threading.Thread(target=legacy_srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{legacy_port}")
        mcp_server._svc_clear_neg()
        ho_legacy = mcp_server.dispatch("vault_handoff", {"project": "alpha-app"}, vault)
        assert sids["alpha_claude"] in ho_legacy and "via 热索引" not in ho_legacy
        assert mcp_server._svc_skip() is False
        sr_legacy = mcp_server.dispatch("vault_search", {"query": "vault_token_pipeline"}, vault)
        assert "via 热索引" in sr_legacy
    finally:
        legacy_srv.shutdown()
        legacy_srv.server_close()

    # 3) 真实服务上的业务 404 (/handoff?id=cv_deadbeef0000) 仍精确报告未找到且不触发负缓存
    srv2, idx2 = cv.build_vault_server(vault, 0)
    port2 = srv2.server_address[1]
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port2}")
        mcp_server._svc_clear_neg()
        miss_ho = mcp_server.dispatch("vault_handoff", {"id": "cv_deadbeef0000"}, vault)
        assert "未找到可用会话快照" in miss_ho and mcp_server._svc_skip() is False
    finally:
        srv2.shutdown()
        srv2.server_close()
        idx2._close()


def test_remember_prints_id_and_parity_and_per_project_dedup(tmp_path, monkeypatch):
    """vault_remember 必须输出可供 vault_memorize(supersedes=#id) 消费的 #id, 并校验 kind/limit 与跨项目隔离。"""
    import re

    vault, _ = _seed_scoped_vault(tmp_path)
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()

    # 跨项目同内容绝不互相抢占归属
    r_a = cv_memory.memory_add_manual(vault, "统一使用 UTC 时间存储", kind="decision", project="proj-a", source="mcp")
    r_b = cv_memory.memory_add_manual(vault, "统一使用 UTC 时间存储", kind="decision", project="proj-b", source="mcp")
    assert r_a["id"] != r_b["id"] and r_b["existed"] is False
    assert len(cv_memory.memory_search_api(vault, "UTC", project="proj-a")) == 1
    assert len(cv_memory.memory_search_api(vault, "UTC", project="proj-b")) == 1

    # 通过 MCP 输出拿到 #id, 直接传入 supersedes
    rem_txt = mcp_server.dispatch("vault_remember", {"query": "UTC", "project": "proj-a"}, vault)
    assert "Agent 录入" in rem_txt
    m = re.search(r"#(\d+)\s+decision", rem_txt)
    assert m is not None, rem_txt
    parsed_id = int(m.group(1))
    assert parsed_id == r_a["id"]

    r_sup = mcp_server.dispatch("vault_memorize", {
        "content": "改用本地时区 ISO-8601 字符串存储",
        "kind": "constraint",
        "project": "proj-a",
        "pinned": True,
        "supersedes": parsed_id,
    }, vault)
    assert f"#{parsed_id}" in r_sup
    rem_after = mcp_server.dispatch("vault_remember", {"query": "存储", "project": "proj-a"}, vault)
    assert "ISO-8601" in rem_after and "UTC" not in rem_after

    # cold/hot 非法 kind 与 limit=0 钳制一致
    cv_memory.memory_add_manual(vault, "第二条决策测试 limit", kind="decision", project="proj-a")
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        hot_bad = mcp_server.handle({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "vault_remember", "arguments": {"query": "存储", "kind": "bad_kind"}},
        }, vault)["result"]
        assert hot_bad["isError"] is True
        hot_lim0 = mcp_server.dispatch("vault_remember", {"query": "", "limit": 0, "project": "proj-a"}, vault)
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    cold_bad = mcp_server.handle({
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "vault_remember", "arguments": {"query": "存储", "kind": "bad_kind"}},
    }, vault)["result"]
    assert cold_bad["isError"] is True
    cold_lim0 = mcp_server.dispatch("vault_remember", {"query": "", "limit": 0, "project": "proj-a"}, vault)
    assert hot_lim0 == cold_lim0 and hot_lim0.count("\n  [#") == 1


def test_project_scope_segment_boundary_and_handoff_cap(tmp_path):
    """段边界项目匹配防串项目/防误写错误仓库; vault_handoff 突破 12k 截断且无双倍 token 计数."""
    import cv_rank

    assert cv_rank.match_project_scope(r"E:\work\app", "app") is True
    assert cv_rank.match_project_scope(r"E:\work\my-app-v2", "app") is False
    assert cv_rank.match_project_scope(r"E:\work\paper-studio-team", "paper-studio") is False
    assert cv_rank.match_project_scope(r"E:\work\app\sub", r"E:\work\app") is True

    vault = tmp_path / "vault"
    app_dir = tmp_path / "app"
    v2_dir = tmp_path / "my-app-v2"
    team_dir = tmp_path / "paper-studio-team"
    app_dir.mkdir()
    v2_dir.mkdir()
    team_dir.mkdir()

    _write_snapshot(
        vault, agent="codex", cwd=str(app_dir), created_at="2026-11-20T10:00:00+08:00",
        session_ref="s_app", blocks=[{"type": "text", "role": "user", "content": "app init"}],
    )
    _write_snapshot(
        vault, agent="codex", cwd=str(v2_dir), created_at="2026-11-26T10:00:00+08:00",
        session_ref="s_v2", blocks=[{"type": "text", "role": "user", "content": "v2 init"}],
    )
    _write_snapshot(
        vault, agent="codex", cwd=str(team_dir), created_at="2026-11-26T11:00:00+08:00",
        session_ref="s_team", blocks=[{"type": "text", "role": "user", "content": "team init"}],
    )
    _index_vault(vault)

    cv_memory.memory_add_manual(vault, "app 自身硬约束", kind="constraint", project="app", pinned=True)
    cv_memory.memory_add_manual(vault, "v2 不相关约束", kind="constraint", project="my-app-v2", pinned=True)

    # 即便 my-app-v2 更新, compile_project_rules("app") 绝不能写错到 my-app-v2
    res = cv_memory.compile_project_rules(vault, "app", target="agents", apply=True)
    assert Path(res["out_path"]) == (app_dir / "AGENTS.md").resolve()
    assert (app_dir / "AGENTS.md").exists() and not (v2_dir / "AGENTS.md").exists()
    app_txt = (app_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "app 自身硬约束" in app_txt and "v2 不相关约束" not in app_txt

    # 只存在 paper-studio-team 时, 查询 paper-studio 必须报未找到而不是误返 team
    with pytest.raises(cv.SnapshotNotFound):
        cv.build_handoff_api(vault, project="paper-studio")

    # 长交接简报 (>12000 字符) 经 mcp_server.handle 不截断尾部记忆, 且 out_tokens 不再双倍累加
    big_blocks = [{"type": "text", "role": "user", "content": "生成长交接简报测试"}]
    for idx in range(90):
        big_blocks.append({
            "type": "text", "role": "assistant",
            "content": f"决定：第 {idx} 号架构模块需要保留完整英文边界校验说明 " + ("long_english_payload_segment " * 8),
        })
    big_blocks.append({"type": "text", "role": "assistant", "content": "最终停靠: 准备交付"})
    big_sid, _ = _write_snapshot(
        vault, agent="codex", cwd=str(app_dir), created_at="2026-11-26T12:00:00+08:00",
        session_ref="s_big", blocks=big_blocks,
    )
    cv_memory.memory_add_manual(vault, "MCP_HANDOFF_TAIL_MARKER_XYZ 必保记忆", kind="constraint", project="app", pinned=True)
    _index_vault(vault)

    ho = cv.build_handoff_api(vault, sid=big_sid, budget_tokens=8000, include_memories=True)
    assert len(ho["brief"]) > 12000
    assert ho["stats"]["out_tokens"] <= 8000
    # compress_brief 在附加 ## 压缩统计 前估算 out_tokens, 与整段差值仅为统计尾注几十 token
    assert ho["stats"]["out_tokens"] <= cv_core.estimate_text_tokens(ho["brief"]) <= ho["stats"]["out_tokens"] + 160

    rpc_ho = mcp_server.handle({
        "jsonrpc": "2.0", "id": 22, "method": "tools/call",
        "params": {"name": "vault_handoff", "arguments": {"id": big_sid, "budget_tokens": 8000, "include_memories": True}},
    }, vault)["result"]
    assert rpc_ho["isError"] is False
    rpc_txt = rpc_ho["content"][0]["text"]
    assert len(rpc_txt) > 12000 and "项目当前有效记忆" in rpc_txt and "MCP_HANDOFF_TAIL_MARKER_XYZ" in rpc_txt


def test_evolution_chain_since_and_timestamp_validation(tmp_path, monkeypatch):
    """演进链早期祖先命中在叶子处于 since 窗口内时必须保留 (热/只读/冷三路等价), 且严格拦截非法日历与混用时区."""
    import cv_rank

    with pytest.raises(ValueError):
        cv_rank.normalize_since("2026-02-30")
    with pytest.raises(ValueError):
        cv_rank.normalize_since("2026-99-99")
    with pytest.raises(ValueError):
        cv_rank.normalize_since("2026-13")
    with pytest.raises(ValueError):
        cv_rank.normalize_since("2026-11-26T25:00:00")
    with pytest.raises(ValueError):
        cv_rank.normalize_since("2026-11-26T10:00:00+25:00")

    assert cv_rank._ts_ge("2026-11-26T02:30:00.5Z", "2026-11-26T02:30:00+0000") is True
    # aware vs naive 比较不应退回字典序出错
    assert cv_rank._ts_ge("2026-11-26T00:30:00Z", cv_rank.normalize_since("2026-11-26T00:00:00+00:00")) is True
    local_now = dt.datetime.now().astimezone()
    local_plus1h_naive = (local_now + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="seconds")
    utc_now_str = local_now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cv_rank._ts_ge(local_plus1h_naive, utc_now_str) is True

    vault = tmp_path / "vault"
    old_ts = (local_now - dt.timedelta(days=40)).isoformat(timespec="seconds")
    new_ts = (local_now - dt.timedelta(days=1)).isoformat(timespec="seconds")

    snap_old, _ = _write_snapshot(
        vault, agent="codex", cwd=r"E:\work\chain-proj", created_at=old_ts, session_ref="chain1",
        blocks=[{"type": "text", "role": "user", "content": "早期设计 legacy_rbac_token 权限核心方案"}],
    )
    snap_new, _ = _write_snapshot(
        vault, agent="codex", cwd=r"E:\work\chain-proj", created_at=new_ts, session_ref="chain1",
        parent_snapshot=snap_old,
        blocks=[
            {"type": "text", "role": "user", "content": "早期设计 legacy_rbac_token 权限核心方案"},
            {"type": "text", "role": "assistant", "content": "继续补充最新演进审计日志。"},
        ],
    )
    snap_orphan, _ = _write_snapshot(
        vault, agent="codex", cwd=r"E:\work\chain-proj", created_at=old_ts, session_ref="orphan1",
        blocks=[{"type": "text", "role": "user", "content": "废弃孤立会话 legacy_rbac_token 草稿"}],
    )
    _index_vault(vault)

    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        hot_s = mcp_server.dispatch("vault_search", {"query": "legacy_rbac_token", "since": "7d"}, vault)
        hot_c = mcp_server.dispatch("vault_context", {"query": "legacy_rbac_token", "since": "7d"}, vault)
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()
    ro_s = mcp_server.dispatch("vault_search", {"query": "legacy_rbac_token", "since": "7d"}, vault)
    ro_c = mcp_server.dispatch("vault_context", {"query": "legacy_rbac_token", "since": "7d"}, vault)

    (vault / "search_index.db").unlink()
    cold_s = mcp_server.dispatch("vault_search", {"query": "legacy_rbac_token", "since": "7d"}, vault)
    cold_c = mcp_server.dispatch("vault_context", {"query": "legacy_rbac_token", "since": "7d"}, vault)

    for out in (hot_s, ro_s, cold_s):
        assert snap_new in out and snap_orphan not in out
    for out in (hot_c, ro_c, cold_c):
        assert "legacy_rbac_token" in out and snap_orphan not in out


def test_compile_rules_security_truncation_sentinel_and_legacy_db(tmp_path):
    """规则编译安全拦截(非规则读/目录400/空项目拒写)、哨兵注入清洗、200 tok 超预算截断与 v0.1 老库兼容."""
    import sqlite3

    vault = tmp_path / "vault"
    proj_dir = tmp_path / "sec-proj"
    proj_dir.mkdir(parents=True)
    _write_snapshot(
        vault, agent="codex", cwd=str(proj_dir), created_at="2026-11-25T10:00:00+08:00",
        blocks=[{"type": "text", "role": "user", "content": "init"}],
    )
    _index_vault(vault)

    # 1) 哨兵注入清洗 + 200 tok 首条超长 constraint 截断保底
    long_rule = "架构红线：绝不允许在规则正文中出现 <!-- context-vault:rules:end --> 标记且" + ("严格禁止绕过校验逻辑" * 16)
    cv_memory.memory_add_manual(vault, long_rule, kind="constraint", project="sec-proj", pinned=True)

    tight = cv_memory.compile_project_rules(vault, "sec-proj", target="agents", budget_tokens=200, apply=False)
    assert tight["included"] == 1 and tight["tokens_est"] <= 200 and "…" in tight["block_markdown"]
    assert tight["block_markdown"].count("<!-- context-vault:rules:end -->") == 1

    # 两次 apply 含哨兵字样记忆不破坏外围结构
    cv_memory.compile_project_rules(vault, "sec-proj", target="agents", budget_tokens=2000, apply=True)
    cv_memory.memory_add_manual(vault, "补充第二条规则", kind="decision", project="sec-proj")
    cv_memory.compile_project_rules(vault, "sec-proj", target="agents", budget_tokens=2000, apply=True)
    written = (proj_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert written.count("<!-- context-vault:rules:start") == 1
    assert written.count("<!-- context-vault:rules:end -->") == 1

    # 2) 空匹配项目 apply=True 拒写
    empty_out = proj_dir / "NO_MATCH.md"
    with pytest.raises(ValueError):
        cv_memory.compile_project_rules(vault, "nonexistent-proj", out_path=empty_out, apply=True)
    assert not empty_out.exists()

    # 3) HTTP 阻止 GET/POST 任意文件读取及目录 500
    secret_env = tmp_path / ".env"
    secret_env.write_text("SECRET_TOKEN=top_secret_12345\n", encoding="utf-8")
    dir_md = proj_dir / "folder.md"
    dir_md.mkdir()

    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import urllib.parse
        q_env = urllib.parse.urlencode({"project": "sec-proj", "out": str(secret_env)})
        c_get_env, b_get_env = _http_json(f"http://127.0.0.1:{port}/memory/compile_rules?{q_env}")
        assert c_get_env == 400 and "top_secret_12345" not in json.dumps(b_get_env)

        c_post_env, b_post_env = _http_json(
            f"http://127.0.0.1:{port}/memory/compile_rules",
            method="POST",
            body={"project": "sec-proj", "out_path": str(secret_env), "apply": False},
        )
        assert c_post_env == 400 and "top_secret_12345" not in json.dumps(b_post_env)

        q_dir = urllib.parse.urlencode({"project": "sec-proj", "out": str(dir_md)})
        c_get_dir, _ = _http_json(f"http://127.0.0.1:{port}/memory/compile_rules?{q_dir}")
        assert c_get_dir == 400

        c_post_dir, _ = _http_json(
            f"http://127.0.0.1:{port}/memory/compile_rules",
            method="POST",
            body={"project": "sec-proj", "out_path": str(dir_md), "apply": True},
        )
        assert c_post_dir == 400
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    # 4) 只读 v0.1 老库 (缺少 status/pinned 列) 仍可列出项目与编译规则
    import os
    import stat

    legacy_vault = tmp_path / "legacy_v01"
    legacy_vault.mkdir()
    ldb = sqlite3.connect(legacy_vault / "vault.db")
    ldb.executescript("""
    CREATE TABLE memory_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
      kind TEXT, content TEXT, project TEXT, hits INTEGER DEFAULT 0, last_used TEXT);
    INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)
      VALUES('2026-10-01T10:00:00','cv_legacy00000001','decision','老库决定：保持零依赖','legacy-proj');
    """)
    ldb.commit()
    ldb.close()
    os.chmod(legacy_vault / "vault.db", stat.S_IREAD)
    try:
        projs = cv_memory.memory_projects_api(legacy_vault)
        assert projs == [{"project": "legacy-proj", "count": 1}]
        comp = cv_memory.compile_project_rules(legacy_vault, "legacy-proj", apply=False)
        assert comp["included"] == 1 and "保持零依赖" in comp["block_markdown"]
    finally:
        os.chmod(legacy_vault / "vault.db", stat.S_IWRITE | stat.S_IREAD)
