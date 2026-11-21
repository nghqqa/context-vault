"""MCP 服务面 6 修复回归 (2026-11-19, 三轮 mcp 审查 F1-F6)。

F1 (high) 非 dict JSON 帧不再杀死整个 server — 坏帧忽略/回显 id 报结构化错误, 进程存活;
F2 (high) serve.json 回环校验严格化 — userinfo 绕过/非回环 host/非法 scheme 一律拒绝回退默认;
F3 (medium) 死服务探活 0.5s 级 + 负缓存 (TTL 可配 CV_SERVE_NEG_TTL) — serve.json mtime 变化即失效;
F4 (medium) limit<=0 冷热钳制一致 (search/context 三路: hot / ro 装载 / 冷扫);
F5 (medium) vault_recent limit=0 与服务端 /recent 同口径 (钳 1), hot==cold;
F6 (medium) 只读挂载 vault.db 时 vault_remember 照常返回 (遥测/建表容错), 零表库不劣化。

隔离: 全部临时目录合成 vault + 27xxx/随机高位端口; 绝不触真实 vault 与 8791/18791/8793。
运行: python -m pytest tests/test_mcp_fix.py -v
"""

import io
import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_memory  # noqa: E402
import mcp_server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEAD_SVC = "http://127.0.0.1:1"  # 不可达地址 (现有 test_mcp_server.py 同款冷回退用法)


def mk_vault(tmp_path: Path):
    """最小可搜 vault: 1 个含 'hello vault' 的快照 (与 test_mcp_server.py 口径一致)。"""
    vault = tmp_path / "vault"
    (vault / "snapshots").mkdir(parents=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\proj"},
                             "conversation": [{"type": "text", "role": "user",
                                               "content": "hello vault"}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    return vault, sid


def mk_indexed_vault(tmp_path: Path):
    """带 search_index.db 的 vault: mcp 冷路径会走只读索引装载 (ro) 而非全量冷扫。"""
    vault, sid = mk_vault(tmp_path)
    idx = cv.VaultIndex(vault)  # 非只读: 初次 rebuild(force=True) 即把 search_index.db 落盘
    idx._close()                # 释放 Windows 文件句柄, 供后续只读装载
    return vault, sid


@pytest.fixture(autouse=True)
def _reset_svc_state(monkeypatch):
    """逐测试复位 mcp_server 模块级状态 (负缓存/告警去重), 防跨测试污染。
    修复引入的 4 个新状态用 raising=False: 对未打修复的旧代码也能跑出红 (复现验证)。"""
    monkeypatch.setattr(mcp_server, "SVC_BASE", DEAD_SVC)
    monkeypatch.setattr(mcp_server, "_VAULT", None)
    monkeypatch.setattr(mcp_server, "_svc_neg_until", 0.0, raising=False)
    monkeypatch.setattr(mcp_server, "_svc_neg_base", None, raising=False)
    monkeypatch.setattr(mcp_server, "_svc_neg_stamp", None, raising=False)
    monkeypatch.setattr(mcp_server, "_SERVE_WARN_MTIME", None, raising=False)
    yield


def call_tool(vault, name, **kw):
    """handle(tools/call) → (text, isError)。"""
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                           "params": {"name": name, "arguments": kw}}, vault)
    return r["result"]["content"][0]["text"], r["result"]["isError"]


# ---------------- F1: 非 dict 帧不杀 server ----------------

def test_f1_bad_frames_do_not_kill_server(tmp_path):
    """四种合法 JSON 但非对象的坏帧 + params=null 帧: 进程全程存活, 后续 ping 正常。"""
    vault, _ = mk_vault(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tools" / "mcp_server.py"), "--vault", str(vault)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    q: queue.Queue = queue.Queue()

    def reader():  # readline 逐行读, 不用缓冲迭代 (管道迭代会凑满缓冲才吐)
        for raw in iter(proc.stdout.readline, b""):
            q.put(raw)
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(line: str):
        proc.stdin.write((line + "\n").encode("utf-8"))
        proc.stdin.flush()

    def recv(timeout=15.0):
        raw = q.get(timeout=timeout)
        assert raw is not None, "server 提前退出 (stdout 关闭)"
        return json.loads(raw.decode("utf-8"))

    try:
        send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        assert recv()["result"]["serverInfo"]["name"] == "context-vault"
        # 四种坏帧曾直接 AttributeError 杀死整个 server (exit 1) — 修复后逐帧忽略
        for bad in ("[1,2,3]", "null", '"str"', "42"):
            send(bad)
        send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        assert recv()["id"] == 2          # 坏帧被消费且忽略: 否则到不了 ping / 进程已死
        assert proc.poll() is None
        # dict 帧但 params=null: params.get 在 tools/call 内层 try 里抛 AttributeError →
        # 按 MCP 规范降级为 isError 结果 (工具层异常语义不变), id 照常回显
        send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": None}))
        r = recv()
        assert r["id"] == 3 and r["result"]["isError"] is True
        send(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}))
        assert recv()["id"] == 4          # 出错后进程仍存活可用
        assert proc.poll() is None
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None):  # 崩溃时把 stderr 带出来方便定位
            sys.stderr.write(proc.stderr.read().decode("utf-8", "replace"))
        proc.stdout.close()
        proc.stderr.close()


def test_f1_handle_ignores_non_dict_frames(tmp_path):
    """handle 直调: 非 dict 帧 (含退化输入) 一律返回 None, 不抛异常。"""
    vault, _ = mk_vault(tmp_path)
    for bad in ([1, 2, 3], None, "str", 42, 3.14, [None]):
        assert mcp_server.handle(bad, vault) is None


def test_f1_main_loop_guard_degrades_frame(tmp_path, monkeypatch, capsys):
    """主循环帧级兜底: handle 抛异常的单帧降级为 -32603 (id 尽力回显), 无 id 帧静默
    忽略, main() 正常返回 = 进程不退。"""
    vault, _ = mk_vault(tmp_path)
    frames = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "boom"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # 无 id
    ]
    monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--vault", str(vault)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(frames) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    real_handle = mcp_server.handle

    def flaky(msg, v):  # 只让 boom 帧在 handle 里炸, 其余走真实现
        if isinstance(msg, dict) and msg.get("method") == "boom":
            raise RuntimeError("模拟帧级异常")
        return real_handle(msg, v)

    monkeypatch.setattr(mcp_server, "handle", flaky)
    mcp_server.main()  # 不抛异常即主循环扛住了坏帧
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert lines[0]["id"] == 1 and "result" in lines[0]
    assert lines[1]["id"] == 2 and lines[1]["error"]["code"] == -32603
    assert len(lines) == 2  # 无 id 的通知帧异常 → 静默忽略, 不写响应


# ---------------- F2: serve.json 严格回环校验 ----------------

def test_f2_serve_json_strict_loopback(tmp_path, capsys):
    """三类恶意串拒绝回退默认且告警一行 (同文件只一次); 正常回环条目照常工作;
    文件缺失/坏 JSON 静默回退 (原有行为不变)。"""
    vault, _ = mk_vault(tmp_path)
    mcp_server.SVC_BASE = mcp_server._DEFAULT_SVC  # 显式回默认: 才会走 serve.json 发现
    mcp_server._VAULT = vault
    sj = vault / "serve.json"
    evils = [
        "http://127.0.0.1:80@evil.example/",   # userinfo 绕过: 真实 host 是 evil.example
        "http://10.0.0.9:27999/",              # 非回环 host
        "https://127.0.0.1:27999/",            # 非法 scheme
    ]
    for i, evil in enumerate(evils):
        sj.write_text(json.dumps({"url": evil}), encoding="utf-8")
        os.utime(sj, (1700000000 + i, 1700000000 + i))  # 显式 mtime: 逐条独立告警一次
        assert mcp_server._resolve_base() == mcp_server._DEFAULT_SVC
        assert mcp_server._resolve_base() == mcp_server._DEFAULT_SVC  # 同一文件二调静默
        err = capsys.readouterr().err
        assert err.count("不合格") == 1, f"恶意串 {evil!r} 应恰好告警一行, got: {err!r}"
    # 正常条目: 127.0.0.1 / localhost / [::1] 照常被发现
    for i, ok in enumerate(("http://127.0.0.1:27991", "http://localhost:27992",
                            "http://[::1]:27993")):
        sj.write_text(json.dumps({"url": ok}), encoding="utf-8")
        os.utime(sj, (1700000010 + i, 1700000010 + i))
        assert mcp_server._resolve_base() == ok
        assert capsys.readouterr().err == ""
    # 文件缺失 / 坏 JSON / 非对象: 静默回退默认 (等效现有处理路径)
    sj.unlink()
    assert mcp_server._resolve_base() == mcp_server._DEFAULT_SVC
    assert capsys.readouterr().err == ""
    sj.write_text("{not json", encoding="utf-8")
    assert mcp_server._resolve_base() == mcp_server._DEFAULT_SVC
    assert capsys.readouterr().err == ""


# ---------------- F3: 死服务 0.5s 探活 + 负缓存 ----------------

def _blackhole_port():
    """只 bind+listen 不 accept 的黑洞端口: TCP 握手由 backlog 完成, HTTP 请求悬挂
    → 精确命中 0.5s 探活超时 (connection refused 在 Windows 是毫秒级, 测不出超时降档)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    return s, s.getsockname()[1]


def test_f3_dead_service_timeout_and_negative_cache(tmp_path):
    """死端口首调 0.5s 级回退 (非 2s 级); 二调走负缓存不再等待;
    touch serve.json 使负缓存失效 → 重新探活。"""
    vault, sid = mk_vault(tmp_path)
    hole, dead_port = _blackhole_port()
    sj = vault / "serve.json"
    sj.write_text(json.dumps({"url": f"http://127.0.0.1:{dead_port}"}), encoding="utf-8")
    mcp_server.SVC_BASE = mcp_server._DEFAULT_SVC  # 走 serve.json 发现
    try:
        t0 = time.monotonic()
        out1 = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
        dt1 = time.monotonic() - t0
        assert sid in out1 and "via 热索引" not in out1  # 死端口 → 结果仍由冷路径正确给出
        assert dt1 < 2.0                                 # 0.5s 级超时, 不再是 2s 级探活
        assert mcp_server._svc_skip() is True            # 负缓存已记下死目标
        t1 = time.monotonic()
        out2 = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
        dt2 = time.monotonic() - t1
        assert sid in out2
        assert dt2 < max(0.3, dt1 / 2)                   # 二调不再吃探活超时
        # touch serve.json (mtime 变化) → 负缓存失效 → 第三调重新探活 (重新吃 ~0.5s)
        st = sj.stat()
        os.utime(sj, (int(st.st_atime), int(st.st_mtime) + 5))
        assert mcp_server._svc_skip() is False
        t2 = time.monotonic()
        out3 = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
        dt3 = time.monotonic() - t2
        assert sid in out3
        assert dt3 >= 0.4                                # 重新探测: 又命中一次 0.5s 超时
        assert mcp_server._svc_skip() is True
    finally:
        hole.close()


def test_f3_recovery_rediscovered_via_serve_json(tmp_path):
    """服务恢复 (重写 serve.json, mtime 变化) → 负缓存失效, 重新走热路径。"""
    vault, sid = mk_vault(tmp_path)
    hole, dead_port = _blackhole_port()
    (vault / "serve.json").write_text(json.dumps({"url": f"http://127.0.0.1:{dead_port}"}),
                                      encoding="utf-8")
    mcp_server.SVC_BASE = mcp_server._DEFAULT_SVC
    try:
        mcp_server.dispatch("vault_recent", {"limit": 5}, vault)  # 首调: 探活失败 → 负缓存
        assert mcp_server._svc_skip() is True
        srv, _ = cv.build_vault_server(vault, 0)  # 服务"恢复": 随机高位端口并重写 serve.json
        port = srv.server_address[1]
        assert port != dead_port and 8791 != port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            assert mcp_server._svc_skip() is False       # mtime 变了 → 重新探测
            out = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
            assert "via 热索引" in out and sid in out     # 恢复后重新被热路径发现
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        hole.close()


def test_f3_negative_cache_keyed_by_base(tmp_path):
    """负缓存按 (地址, mtime) 记忆: 换了显式 SVC_BASE 目标即失效, 不误伤其他目标的热路径。"""
    vault, sid = mk_vault(tmp_path)
    mcp_server.SVC_BASE = DEAD_SVC
    mcp_server.dispatch("vault_recent", {"limit": 5}, vault)  # 死目标 → 负缓存
    assert mcp_server._svc_skip() is True
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    conn.close()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mcp_server.SVC_BASE = f"http://127.0.0.1:{port}"  # 显式换目标
        assert mcp_server._svc_skip() is False
        out = mcp_server.dispatch("vault_search", {"query": "hello vault"}, vault)
        assert "via 热索引" in out and sid in out
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------- F4: limit<=0 冷热语义一致 (search/context 三路) ----------------

def test_f4_limit_zero_three_paths_search(tmp_path, monkeypatch):
    """limit=0/-3: hot / ro 装载 / 冷扫 三路都钳 1 — 不再把真实命中报成"未命中"或报零行。"""
    vault_h, sid = mk_indexed_vault(tmp_path / "h")   # 热路径 vault (带索引 + serve)
    vault_r, _ = mk_indexed_vault(tmp_path / "r")     # 只读索引装载 vault (无 serve)
    vault_c, _ = mk_vault(tmp_path / "c")             # 纯冷扫 vault (无索引文件)
    srv, _ = cv.build_vault_server(vault_h, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    norm = lambda s: s.replace(" (via 热索引)", "").replace(" (via 本地索引)", "")
    try:
        mcp_server.SVC_BASE = f"http://127.0.0.1:{port}"
        hot = [mcp_server.dispatch("vault_search",
                                   {"query": "hello vault", "limit": lim}, vault_h)
               for lim in (0, -3)]
    finally:
        srv.shutdown()
        srv.server_close()
    mcp_server.SVC_BASE = DEAD_SVC
    ro = [mcp_server.dispatch("vault_search", {"query": "hello vault", "limit": lim}, vault_r)
          for lim in (0, -3)]
    cold = [mcp_server.dispatch("vault_search", {"query": "hello vault", "limit": lim}, vault_c)
            for lim in (0, -3)]
    for out in hot + ro + cold:
        assert "未命中" not in out                     # 修复前 ro/冷扫把真实命中报成未命中
        assert "命中 1 个快照" in out                  # limit<=0 → 1: 真实命中数照报
        assert sid in out
        assert sum(1 for l in out.splitlines() if l.startswith("  [")) == 1  # 恰好 1 行命中
    assert norm(hot[0]) == norm(ro[0]) == norm(cold[0])   # 三路文本一致 (via 标记除外)
    assert norm(hot[1]) == norm(ro[1]) == norm(cold[1])


def test_f4_limit_zero_three_paths_context(tmp_path):
    """vault_context limit=0/-3 三路一致: 都给出快照块摘录, 不误报"无相关上下文"。"""
    vault_h, sid = mk_indexed_vault(tmp_path / "h")
    vault_r, _ = mk_indexed_vault(tmp_path / "r")
    vault_c, _ = mk_vault(tmp_path / "c")
    srv, _ = cv.build_vault_server(vault_h, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mcp_server.SVC_BASE = f"http://127.0.0.1:{port}"
        hot = mcp_server.dispatch("vault_context", {"query": "hello vault", "limit": 0}, vault_h)
    finally:
        srv.shutdown()
        srv.server_close()
    mcp_server.SVC_BASE = DEAD_SVC
    ro = mcp_server.dispatch("vault_context", {"query": "hello vault", "limit": -3}, vault_r)
    cold = mcp_server.dispatch("vault_context", {"query": "hello vault", "limit": 0}, vault_c)
    for out in (hot, ro, cold):
        assert "无相关上下文" not in out
        assert f"--- 快照 {sid} 块#" in out            # 命中块照常摘录
        assert "hello vault" in out


# ---------------- F5: vault_recent limit=0 hot==cold ----------------

def test_f5_recent_limit_zero_same_rows(tmp_path):
    """limit=0/-3 冷热都按 /recent 服务端口径钳 1 (修复前冷 5 行 / 热 1 行分裂);
    limit=2 行为不变。"""
    vault, sid = mk_vault(tmp_path)
    env2 = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\proj2"},
                              "conversation": [{"type": "text", "role": "user",
                                                "content": "another session entirely"}],
                              "stats": cv.zero_stats()})
    sid2 = cv.snapshot_id(env2)
    cv.atomic_write_text(vault / "snapshots" / f"{sid2}.json",
                         json.dumps(env2, ensure_ascii=False))
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    conn.close()

    def rowcount(s):  # 热行 "  [cv_..]", 冷行 "  cv_.."
        return sum(1 for l in s.splitlines() if l.startswith("  [") or l.startswith("  cv_"))

    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mcp_server.SVC_BASE = f"http://127.0.0.1:{port}"
        hot = {lim: mcp_server.dispatch("vault_recent", {"limit": lim}, vault)
               for lim in (0, -3, 2)}
    finally:
        srv.shutdown()
        srv.server_close()
    mcp_server.SVC_BASE = DEAD_SVC
    cold = {lim: mcp_server.dispatch("vault_recent", {"limit": lim}, vault)
            for lim in (0, -3, 2)}
    for lim in (0, -3):
        assert rowcount(hot[lim]) == rowcount(cold[lim]) == 1   # 0/负数 → 1, hot==cold
        assert "最近 1 个" in hot[lim] and "最近 1 个" in cold[lim]
        # 钳 1 后两快照只列其一 (同秒 created_at 并列序不约定); 但必须恰好是其中之一
        assert any(s in hot[lim] for s in (sid, sid2))
        assert any(s in cold[lim] for s in (sid, sid2))
    assert rowcount(hot[2]) == rowcount(cold[2]) == 2           # 正常 limit 不受影响
    assert sid in hot[2] and sid in cold[2] and sid2 in hot[2] and sid2 in cold[2]


# ---------------- F6: 只读挂载 vault_remember 容错 ----------------

def _seed_memory(vault: Path, content: str):
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.executescript(cv_memory.MEMORY_TABLE)
    conn.execute("INSERT INTO memory_items(created_at, snapshot_id, kind, content, project)"
                 " VALUES(?, ?, 'decision', ?, 'proj')",
                 ("2026-11-19T10:00:00", "cv_seed00000000", content))
    conn.commit()
    conn.close()


def test_f6_remember_readonly_mount(tmp_path):
    """只读挂载 vault.db: vault_remember 照常返回检索结果, 无 isError (修复前 UPDATE/
    建表抛 OperationalError → isError); 纯读的 vault_search 同库不受影响。"""
    vault, sid = mk_vault(tmp_path)
    _seed_memory(vault, "决策: 采用 SQLite 倒排索引方案")
    dbf = vault / "vault.db"
    os.chmod(dbf, stat.S_IREAD)  # Windows 只读属性 → SQLite 写入抛 attempt to write a readonly database
    mcp_server.SVC_BASE = DEAD_SVC
    try:
        text, is_err = call_tool(vault, "vault_remember", query="SQLite 倒排")
        assert is_err is False
        assert "SQLite 倒排索引方案" in text          # 检索结果照常返回
        s_text, s_err = call_tool(vault, "vault_search", query="hello vault")
        assert s_err is False and sid in s_text       # 同只读库, 纯读工具不受影响
    finally:
        os.chmod(dbf, stat.S_IWRITE | stat.S_IREAD)   # 还原可写, 便于 tmp 清理


def test_f6_remember_zero_table_writable(tmp_path):
    """零表 vault.db 可写: 行为不劣化 — 建表后诚实返回"没有记忆" (修复前后同口径)。"""
    vault, _ = mk_vault(tmp_path)
    import sqlite3
    dbf = vault / "vault.db"
    sqlite3.connect(dbf).close()  # 零表空库
    mcp_server.SVC_BASE = DEAD_SVC
    text, is_err = call_tool(vault, "vault_remember", query="随便什么")
    assert is_err is False
    assert "没有关于" in text
    conn = cv.db(vault)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "memory_items" in names                     # 可写: 建表逻辑照常 (与修复前一致)


def test_f6_remember_zero_table_readonly(tmp_path):
    """只读挂载 + 零表 (从未有可写调用): 检索 SELECT 失败兜底为空列表, 不再 isError
    (修复前 executescript 建表即崩 → isError)。"""
    vault, _ = mk_vault(tmp_path)
    import sqlite3
    dbf = vault / "vault.db"
    sqlite3.connect(dbf).close()  # 零表空库
    os.chmod(dbf, stat.S_IREAD)   # 挂载只读在先: 全程无机会建表
    mcp_server.SVC_BASE = DEAD_SVC
    try:
        text, is_err = call_tool(vault, "vault_remember", query="随便什么")
        assert is_err is False
        assert "没有关于" in text                      # 与空库同口径的诚实输出
    finally:
        os.chmod(dbf, stat.S_IWRITE | stat.S_IREAD)


def test_f6_remember_writable_mount_still_scores(tmp_path):
    """可写挂载: 遥测 UPDATE 照常生效 (hits/last_used 递增), 修复未改变正常路径。"""
    vault, _ = mk_vault(tmp_path)
    _seed_memory(vault, "决策: 采用 SQLite 倒排索引方案")
    mcp_server.SVC_BASE = DEAD_SVC
    text, is_err = call_tool(vault, "vault_remember", query="SQLite 倒排")
    assert is_err is False and "SQLite 倒排索引方案" in text
    conn = cv.db(vault)
    row = conn.execute("SELECT hits, last_used FROM memory_items").fetchone()
    conn.close()
    assert row["hits"] >= 1 and row["last_used"]      # 遥测仍在写


def test_f7_snapshot_corrupt_json_and_ledger_summary_robustness(tmp_path):
    """损坏/缺字段快照返回可读错误不抛异常; ledger_summary 遇到坏 raw JSON 仍安全返回且关闭连接;
    SERVER_VERSION 与 cv.VERSION 对齐。"""
    import sqlite3
    assert mcp_server.SERVER_VERSION == cv.VERSION
    vault, _ = mk_vault(tmp_path)
    bad_sid = "cv_bad00000000001"
    (vault / "snapshots" / f"{bad_sid}.json").write_text("{corrupt json", encoding="utf-8")
    text1, is_err1 = call_tool(vault, "vault_snapshot", id=bad_sid)
    assert is_err1 is False and "损坏" in text1

    inc_sid = "cv_1c000000000001"
    (vault / "snapshots" / f"{inc_sid}.json").write_text(json.dumps({"only": 1}), encoding="utf-8")
    text2, is_err2 = call_tool(vault, "vault_snapshot", id=inc_sid)
    assert is_err2 is False and "不完整" in text2

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.execute("INSERT INTO ledger_events(source,tokens_saved,cost_usd,raw) VALUES(?,?,?,?)",
                 ("headroom-lifetime", 100, 1.5, "{bad raw"))
    conn.commit()
    conn.close()
    summary = mcp_server.ledger_summary(vault)
    assert summary["lifetime_tokens_saved"] == 100 and summary["lifetime_compression_saved_usd"] == 1.5
    # 若 ledger_summary 泄漏连接, Windows 下删除 vault.db 会抛 PermissionError
    (vault / "vault.db").unlink()
