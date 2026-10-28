"""Context Vault 核心路径测试套件.

运行: python -m pytest tests/ -v
被测代码保持零第三方依赖; pytest 仅是开发依赖。
全部用例用 tmp_path 隔离, 不触碰真实 vault/会话目录。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402


# ---------------- 测试样本构造 ----------------

def w(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                    encoding="utf-8")
    return path


def mini_rollout(tmp: Path) -> Path:
    return w(tmp / "rollout-2026-10-01T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
        {"timestamp": "2026-10-01T02:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     "timestamp": "2026-10-01T02:00:00.000Z", "cwd": "D:\\proj",
                     "cli_version": "0.153.4", "source": "exec"}},
        {"timestamp": "2026-10-01T02:00:01.000Z", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "id": "msg_1", "role": "user",
                     "content": [{"type": "input_text", "text": "hello vault"}]}},
        {"timestamp": "2026-10-01T02:00:01.000Z", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "id": "msg_2", "role": "developer",
                     "content": [{"type": "input_text", "text": "<env>injected</env>"}]}},
        {"timestamp": "2026-10-01T02:00:02.000Z", "ordinal": 3, "type": "response_item",
         "payload": {"type": "reasoning", "id": "rs_1",
                     "summary": [{"type": "summary_text", "text": "thinking hard"}],
                     "encrypted_content": "gAAAADROP"}},
        {"timestamp": "2026-10-01T02:00:03.000Z", "ordinal": 4, "type": "response_item",
         "payload": {"type": "function_call", "id": "fc_1", "name": "exec",
                     "arguments": "{\"cmd\":\"ls\"}", "call_id": "call_1"}},
        {"timestamp": "2026-10-01T02:00:04.000Z", "ordinal": 5, "type": "response_item",
         "payload": {"type": "function_call_output", "id": "fco_1", "call_id": "call_1",
                     "output": "file_a file_b"}},
        {"timestamp": "2026-10-01T02:00:05.000Z", "ordinal": 6, "type": "response_item",
         "payload": {"type": "message", "id": "msg_3", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-10-01T02:00:06.000Z", "ordinal": 7, "type": "token_usage_record",
         "payload": {"turn_id": "t1",
                     "turn_token_usage": {"input_tokens": "100", "output_tokens": "20",
                                          "cached_input_tokens": "60",
                                          "cache_write_input_tokens": "10",
                                          "reasoning_output_tokens": "5", "total_tokens": "120"}}},
    ])


def mini_transcript(tmp: Path) -> Path:
    return w(tmp / "ses_TEST0001.jsonl", [
        {"type": "user", "timestamp": "2026-06-01T00:00:00.000Z", "content": "q1"},
        {"type": "tool_use", "timestamp": "2026-06-01T00:00:01.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}},
        {"type": "tool_result", "timestamp": "2026-06-01T00:00:02.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}, "tool_output": {"output": "hit"}},
        {"type": "tool_result", "timestamp": "2026-06-01T00:00:03.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}, "tool_output": {"output": "orphan"}},
    ])


def mini_projects(tmp: Path) -> Path:
    return w(tmp / "11111111-2222-3333-4444-555555555555.jsonl", [
        {"parentUuid": None, "type": "user", "uuid": "u1", "timestamp": "2026-10-01T00:00:00.000Z",
         "sessionId": "11111111-2222-3333-4444-555555555555", "cwd": "D:\\proj",
         "message": {"role": "user", "content": "read the file"}},
        {"parentUuid": "u1", "type": "assistant", "uuid": "a1",
         "timestamp": "2026-10-01T00:00:02.000Z", "cwd": "D:\\proj",
         "message": {"role": "assistant", "model": "glm-5.2",
                     "content": [{"type": "thinking", "thinking": "plan",
                                  "signature": "sig"},
                                 {"type": "tool_use", "id": "call_9", "name": "Read",
                                  "input": {"path": "x.py"}}],
                     "usage": {"input_tokens": 50, "output_tokens": 10,
                               "cache_read_input_tokens": 30,
                               "cache_creation_input_tokens": 5}}},
        {"parentUuid": "a1", "type": "user", "uuid": "u2",
         "timestamp": "2026-10-01T00:00:03.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call_9", "content": "file body"}]}},
    ])


# ---------------- 内容寻址与格式 ----------------

def test_snapshot_id_idempotent_and_sensitive():
    env = cv.build_envelope({"capture": {"agent": "x"}, "conversation": [{"type": "text", "role": "user", "content": "a"}], "stats": cv.zero_stats()})
    s1 = cv.snapshot_id(env)
    assert cv.snapshot_id(json.loads(json.dumps(env))) == s1          # 幂等
    env2 = json.loads(json.dumps(env)); env2["created_at"] = "1999-01-01"
    assert cv.snapshot_id(env2) == s1                                  # created_at 不参与
    env3 = json.loads(json.dumps(env)); env3["lineage"]["fork_of"] = "cv_x"
    assert cv.snapshot_id(env3) == s1                                  # lineage 不参与
    env4 = json.loads(json.dumps(env)); env4["conversation"][0]["content"] = "b"
    assert cv.snapshot_id(env4) != s1                                  # 内容敏感


def test_detect_format_three_way(tmp_path):
    assert cv.detect_format(cv.first_jsonl(mini_rollout(tmp_path)), tmp_path) == "codex-rollout"
    assert cv.detect_format(cv.first_jsonl(mini_transcript(tmp_path / "t")), tmp_path / "t") == "claude-transcript"
    assert cv.detect_format(cv.first_jsonl(mini_projects(tmp_path / "p")), tmp_path / "p") == "claude-projects"


# ---------------- 三源导出 ----------------

def test_export_codex_roundtrip(tmp_path):
    r = cv.export_codex(mini_rollout(tmp_path), drop_developer=False)
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "text", "thinking", "tool_call", "tool_result", "text"]
    assert r["capture"]["cwd"] == "D:\\proj"
    assert r["stats"]["tokens_in"] == 100 and r["stats"]["cache_read"] == 60
    assert r["stats"]["cache_write"] == 10 and r["stats"]["usage_events"] == 1
    # drop_developer 丢弃注入
    r2 = cv.export_codex(mini_rollout(tmp_path), drop_developer=True)
    assert not any(b.get("role") == "developer" for b in r2["conversation"])


def test_export_claude_transcript_fifo_pairing(tmp_path):
    r = cv.export_claude(mini_transcript(tmp_path / "t"), False)
    calls = [b for b in r["conversation"] if b["type"] == "tool_call"]
    results = [b for b in r["conversation"] if b["type"] == "tool_result"]
    assert len(results) == 1                                   # 孤儿 result 被丢弃
    assert results[0]["call_id"] == calls[0]["call_id"]        # FIFO 配对
    assert r["capture"]["completeness"] == "no_assistant_text"


def test_export_claude_code_projects(tmp_path):
    r = cv.export_claude_code(mini_projects(tmp_path / "p"), False)
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "thinking", "tool_call", "tool_result"]
    tc = [b for b in r["conversation"] if b["type"] == "tool_call"][0]
    tr = [b for b in r["conversation"] if b["type"] == "tool_result"][0]
    assert tc["call_id"] == "call_9" and tr["call_id"] == "call_9"
    assert r["capture"]["model"] == "glm-5.2" and r["capture"]["cwd"] == "D:\\proj"
    assert r["stats"]["tokens_in"] == 50 and r["stats"]["cache_read"] == 30
    assert r["capture"]["completeness"] == "full"


# ---------------- 迁移生成 ----------------

def make_env(tmp_path, kind="codex-rollout"):
    exp = {"codex-rollout": (cv.export_codex, mini_rollout),
           "claude-transcript": (cv.export_claude, lambda t: mini_transcript(t / "t")),
           "claude-projects": (cv.export_claude_code, lambda t: mini_projects(t / "p"))}[kind]
    exported = exp[0](exp[1](tmp_path), False)
    return cv.build_envelope(exported)


def test_blocks_to_codex_structure(tmp_path):
    env = make_env(tmp_path)
    lines, dropped, sid, fname = cv.blocks_to_codex(env, False)
    assert sid in fname and fname.startswith("rollout-")        # 文件名含 UUID
    assert all(isinstance(l["ordinal"], int) for l in lines)    # ordinal 必须 int (serde)
    assert lines[0]["type"] == "session_meta"
    kinds = {l["type"] for l in lines}
    assert "event_msg" in kinds and "turn_context" in kinds      # 生命周期 + turn 锚点
    fc = {p["call_id"] for l in lines if l["type"] == "response_item"
          and (p := l["payload"]).get("type") == "function_call"}
    fo = {p["call_id"] for l in lines if l["type"] == "response_item"
          and (p := l["payload"]).get("type") == "function_call_output"}
    assert fc == fo and "call_1" in fc                           # 完美配对
    assert dropped == 1                                          # developer 注入被迁移过滤 (§3.1 裁决)


def test_blocks_to_codex_developer_filtered_and_compaction(tmp_path):
    env = make_env(tmp_path)
    env["conversation"].insert(0, {"type": "compaction", "summary": "earlier stuff",
                                   "replaced_blocks": [0, 0], "ts": "2026-10-01T00:00:00.000Z"})
    env["conversation"].append({"type": "text", "role": "developer", "content": "dev", "ts": ""})
    lines, dropped, sid, fname = cv.blocks_to_codex(env, False)
    roles = [p.get("role") for l in lines if l["type"] == "response_item"
             and (p := l["payload"]).get("type") == "message"]
    assert "developer" not in roles                              # 迁移过滤 developer
    assert dropped == 2                                          # 样本1个 + 追加1个 developer 均被过滤
    user_texts = [p["content"][0]["text"] for l in lines if l["type"] == "response_item"
                  and (p := l["payload"]).get("type") == "message" and p.get("role") == "user"]
    assert "迁移摘要" in user_texts[0]                             # stateless replay: 摘要为首条 user
    assert any("hello vault" in t for t in user_texts[1:])        # 活上下文紧随其后


def test_blocks_to_claude_chain(tmp_path):
    env = make_env(tmp_path, "claude-projects")
    lines, dropped = cv.blocks_to_claude(env, False)
    assert lines[0]["parentUuid"] is None
    assert all(lines[i]["parentUuid"] == lines[i - 1]["uuid"] for i in range(1, len(lines)))
    assert all(str(lines[i]["timestamp"]).endswith("Z") for i in range(len(lines)))
    tu = [c for l in lines if l["type"] == "assistant"
          for c in l["message"]["content"] if c["type"] == "tool_use"]
    tr = [c for l in lines if l["type"] == "user"
          and isinstance(l["message"]["content"], list)
          for c in l["message"]["content"] if c["type"] == "tool_result"]
    assert {t["id"] for t in tu} == {t["tool_use_id"] for t in tr}


# ---------------- sync 增量与 lineage ----------------

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把 cv 的全局路径指向 tmp, 隔离真实会话目录。"""
    home = tmp_path / "home"
    (home / ".codex" / "sessions" / "2026" / "10" / "01").mkdir(parents=True)
    (home / ".claude" / "transcripts").mkdir(parents=True)
    (home / ".claude" / "projects" / "D--proj").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
    import cv as _cv
    # scan_sources 用模块常量, 已随 monkeypatch 生效
    vault = tmp_path / "vault"
    vault.mkdir()
    return home, vault


def test_sync_incremental_lineage(isolated, monkeypatch):
    home, vault = isolated
    src = mini_rollout(home / ".codex" / "sessions" / "2026" / "10" / "01")
    import time as _t
    import os as _os
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)

    from types import SimpleNamespace
    A = lambda: SimpleNamespace(vault=str(vault), root=None)
    cv.cmd_sync(A())
    n1 = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n1 == 1
    first_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]

    cv.cmd_sync(A())                                             # 二次全跳过
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1

    w(src, [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
      + [{"timestamp": "2026-10-01T03:00:00.000Z", "ordinal": 8, "type": "response_item",
          "payload": {"type": "message", "id": "msg_9", "role": "user",
                      "content": [{"type": "input_text", "text": "follow up"}]}}])
    _os.utime(src)
    cv.cmd_sync(A())
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    new_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]
    assert new_sid != first_sid
    env = json.loads((vault / "snapshots" / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert env["lineage"]["parent_snapshot"] == first_sid        # lineage 自动成链


# ---------------- 热索引 ----------------

def test_tokenize_and_index(tmp_path):
    toks = cv.tokenize("MergePilot 优化 dataset1 时序")
    assert "mergepilot" in toks and "dataset1" in toks
    assert any(len(t) == 2 and all("\u4e00" <= c <= "\u9fff" for c in t) for t in toks)  # CJK bigram

    def bt(b):
        return b.get("content", "") or ""
    idx = cv.VaultIndex(tmp_path, bt)
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    env = make_env(tmp_path)
    (tmp_path / "snapshots" / "cv_test1.json").write_text(
        json.dumps(env, ensure_ascii=False), encoding="utf-8")
    idx.rebuild(force=True)
    r = idx.search("hello vault", 5)
    assert r["hits"], "索引可检索"


# ---------------- 账本 ----------------

def test_ledger_headroom_import(isolated, monkeypatch):
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir()
    w(head / "savings_events.jsonl", [
        {"v": 1, "ts": "2026-08-19T10:30:27Z", "before": 100, "after": 40, "saved": 60,
         "cost_usd": 0.03, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"}])
    (head / "proxy_savings.json").write_text(json.dumps({
        "schema_version": "6", "generated_at": "2026-10-28T00:00:00Z",
        "lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5,
                     "cache_read_tokens": 9000, "cache_savings_usd": 0.7,
                     "total_input_tokens": 100000}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)

    from types import SimpleNamespace
    cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    assert n == 2                                                 # 1 明细 + 1 汇总
    row = conn.execute("SELECT tokens_saved, cost_usd FROM ledger_events "
                       "WHERE kind='compression'").fetchone()
    assert row["tokens_saved"] == 60 and abs(row["cost_usd"] - 0.03) < 1e-9


# ---------------- 自有账本 (协作者零依赖体验) ----------------

def test_owned_stats_zero_external(isolated):
    _, vault = isolated
    # 无 headroom 时自有账本仍完整: 快照 stats 聚合
    exp = cv.export_codex(mini_rollout(Path(str(vault)) and (isolated[0] / ".codex" / "sessions" / "2026" / "10" / "01")), False)
    env = cv.build_envelope(exp)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    import uuid as _u
    sid = "cv_t" + _u.uuid4().hex[:12]
    out = vault / "snapshots" / f"{sid}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(env), encoding="utf-8")
    cv.register_snapshot(conn, env, sid, out)
    conn.commit()
    own = cv.owned_stats(vault)
    assert own["snapshots"] == 1
    assert own["tokens_in"] == 100 and own["cache_read"] == 60
    assert own["cache_hit_rate"] == 0.6
    assert own["by_agent"][0]["agent"] == "codex"


# ---------------- M3 记忆层 ----------------

def test_memory_extract_and_search(isolated, monkeypatch):
    _, vault = isolated
    env = make_env(tmp_path_maker(), "codex-rollout")
    env["conversation"].append({"type": "text", "role": "assistant",
                                "content": "结论：我们决定采用单实例自托管方案", "ts": ""})
    env["conversation"].insert(0, {"type": "compaction", "summary": "早前讨论了缓存策略并确定 TTL=300",
                                   "replaced_blocks": [0, 0], "ts": ""})
    env["conversation"].append({"type": "text", "role": "user",
                                "content": "记住：以后所有提交都必须带签名", "ts": ""})
    sid = "cv_memtest1"
    (vault / "snapshots").mkdir(exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    import cv as _cv
    items = _cv.extract_memories({**env, "_sid": sid, "capture": {"cwd": "D:\proj"}})
    kinds = {i["kind"] for i in items}
    assert kinds == {"summary", "decision", "preference"}
    dec = [i for i in items if i["kind"] == "decision"]
    assert any("单实例自托管" in i["content"] for i in dec)
    assert all(i["project"] == "proj" for i in items)

    conn = _cv.db(vault)
    conn.executescript(_cv.SCHEMA + _cv.MEMORY_TABLE)
    for it in items:
        conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)"
                     " VALUES(?,?,?,?,?)", ("t", sid, it["kind"], it["content"], it["project"]))
    conn.commit()
    hits = _cv.memory_search_api(vault, "自托管 方案", 5)
    assert hits and any("单实例" in h["content"] for h in hits)
    assert all(h["snapshot"] == sid for h in hits)


def tmp_path_maker():
    return Path(__file__).parent  # make_env 只用 tmp 做中转, 任意可写目录均可


# ---------------- M3 收尾: fork / audit / 引擎接口 ----------------

def test_fork_lineage(isolated, monkeypatch, capsys):
    _, vault = isolated
    env = make_env(Path(__file__).parent)
    sid = "cv_forksrc1"
    (vault / "snapshots").mkdir(exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    from types import SimpleNamespace
    cv.cmd_fork(SimpleNamespace(vault=str(vault), snapshot_id=sid, message="test fork"))
    new_sid = [f.stem for f in (vault / "snapshots").glob("cv_*.json") if f.stem != sid][0]
    forked = json.loads((vault / "snapshots" / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert forked["lineage"]["fork_of"] == sid
    assert forked["capture"]["fork_note"] == "test fork"
    # 原快照不可变: 内容未被改动
    orig = json.loads((vault / "snapshots" / f"{sid}.json").read_text(encoding="utf-8"))
    assert orig["lineage"]["fork_of"] is None


def test_audit_summary(isolated):
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    import sqlite3 as _s
    for st, miss in (("ok", None), ("ok", None), ("switch", None),
                     ("http_error", '["gpt-x"]')):
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (f"2026-10-28T0{st.__len__()}:" + "00:00+08:00", "p1", "u", st, 200, 10, "[]", miss))
    conn.commit()
    r = cv.audit_summary_and_timeline(vault, "", "all")
    assert r["summary"]["ok_probes"] == 2
    assert r["summary"]["switches"] == 1
    assert r["summary"]["model_missing_events"] == 1
    assert r["timeline"][0]["day"] == "2026-10-28"


def test_memory_engine_protocol():
    eng = cv.load_memory_engine("rule")
    assert eng.name == "rule"
    assert hasattr(eng, "extract") and hasattr(eng, "search")
    # 动态加载路径: 用本模块内伪造引擎验证 importlib 机制
    import sys as _s
    _s.path.insert(0, "tools")
    fake = "tests_fake_engine:FakeEngine"
    import types as _t
    mod = _t.ModuleType("tests_fake_engine")
    class FakeEngine:
        name = "fake"
        def extract(self, env): return [{"kind": "decision", "content": "x",
                                         "project": "p", "snapshot_id": "s"}]
        def search(self, vault, q, lim): return []
    mod.FakeEngine = FakeEngine
    _s.modules["tests_fake_engine"] = mod
    e2 = cv.load_memory_engine(fake)
    assert e2.name == "fake" and e2.extract({})[0]["content"] == "x"
