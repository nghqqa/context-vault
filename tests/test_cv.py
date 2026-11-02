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
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")  # 隔离真实 zcode 库
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
    assert own["sessions"] == 1
    assert own["tokens_in"] == 100 and own["cache_read"] == 60
    assert own["cache_hit_rate"] == 0.6
    assert own["by_agent"][0]["agent"] == "codex"
    assert own["by_agent"][0]["sessions"] == 1


def _register_env(vault: Path, capture: dict, stats: dict, created_at: str) -> str:
    """注册一个手工 stats 的快照 (账本口径测试用), 返回 sid。"""
    import uuid as _u
    env = cv.build_envelope({"capture": capture, "conversation":
                             [{"type": "text", "role": "user", "content": "x"}],
                             "stats": {**cv.zero_stats(), **stats}})
    env["created_at"] = created_at
    sid = "cv_t" + _u.uuid4().hex[:12]
    out = vault / "snapshots" / f"{sid}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(env), encoding="utf-8")
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    cv.register_snapshot(conn, env, sid, out)
    conn.commit()
    return sid


def test_owned_stats_cache_hit_normalized(isolated):
    """回归 (2026-11-02 审查 #2): Claude 的 tokens_in 只是未缓存输入, 缓存读/写单独计数,
    旧公式 cache_read/tokens_in 在 10+90 时得出 900%。按来源语义归一分母后:
    claude-projects 分母 = 未缓存+读+写; codex 的 input 已含缓存部分, 分母 = input。"""
    _, vault = isolated
    _register_env(vault, {"agent": "claude-code", "source_format": "claude-projects"},
                  {"tokens_in": 10, "cache_read": 90, "cache_write": 0,
                   "total_tokens": 110, "usage_events": 1}, "2026-11-01T00:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == 0.9                      # 90/100, 不再是 9.0
    # 混源: 加一个 codex 叶子 (input=100 已含 cached 60, 写 10) → 加权 150/(100 + 100) = 0.75
    _register_env(vault, {"agent": "codex", "source_format": "codex-rollout"},
                  {"tokens_in": 100, "cache_read": 60, "cache_write": 10,
                   "total_tokens": 120, "usage_events": 1}, "2026-11-01T01:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == round(150 / 200, 3)
    assert own["cache_hit_rate"] <= 1.0


def test_owned_stats_dedup_cumulative(isolated, monkeypatch):
    """回归 (2026-11-02 审查 #1): sync 对同一会话保存多个累计快照, owned_stats 曾对
    全版本求和 → 同一批 usage 逐版本重复累计 (100 变 200)。现在只累计 lineage 叶子。"""
    home, vault = isolated
    src = mini_rollout(home / ".codex" / "sessions" / "2026" / "10" / "01")
    import os as _os
    from types import SimpleNamespace
    A = lambda: SimpleNamespace(vault=str(vault), root=None)
    cv.cmd_sync(A())                                    # v1: input=100, cache_read=60
    w(src, [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
      + [{"timestamp": "2026-10-01T03:00:00.000Z", "ordinal": 8, "type": "response_item",
          "payload": {"type": "message", "id": "msg_9", "role": "user",
                      "content": [{"type": "input_text", "text": "follow up"}]}}])
    _os.utime(src)
    cv.cmd_sync(A())                                    # v2 累计含 v1 全部 usage, parent=v1
    conn = cv.db(vault)
    leaf_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]
    own = cv.owned_stats(vault)
    assert own["snapshots"] == 2                        # 版本总数: 诚实包含被替代的历史版本
    assert own["sessions"] == 1                         # 独立会话 = 1 条演进链
    assert own["tokens_in"] == 100 and own["cache_read"] == 60   # 只数叶子, 不再 200/120
    assert own["by_agent"][0]["snapshots"] == 1
    # fork 是用户主动创建的独立资产副本: 计自己的叶子 (会话 +1, 用量按副本重复)
    cv.cmd_fork(SimpleNamespace(vault=str(vault), snapshot_id=leaf_sid, message=None))
    own2 = cv.owned_stats(vault)
    assert own2["snapshots"] == 3 and own2["sessions"] == 2 and own2["tokens_in"] == 200


# ---------------- M3 记忆层 ----------------

def test_memory_extract_and_search(isolated, monkeypatch, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path, "codex-rollout")
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


# ---------------- M3 收尾: fork / audit / 引擎接口 ----------------

def test_fork_lineage(isolated, monkeypatch, capsys, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path)
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


# ---------------- zcode sqlite 适配器 ----------------

def test_export_zcode_session(isolated, tmp_path):
    import sqlite3 as _s
    db = _s.connect(":memory:")
    db.row_factory = _s.Row
    db.executescript("""CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_updated INTEGER,
        time_deleted INTEGER, time_created INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, sequence INTEGER);""")
    db.execute("INSERT INTO session VALUES('sess_t','D:\proj','测试会话',1793000000000,NULL,1793000000000)")
    db.execute("INSERT INTO message VALUES('m1','sess_t',?,1000,1)",
               (json.dumps({"role": "user"}),))
    db.execute("INSERT INTO message VALUES('m2','sess_t',?,2000,2)",
               (json.dumps({"role": "assistant", "modelId": "GLM-5.3"}),))
    db.execute("INSERT INTO part VALUES('p1','m1','sess_t',?,1)",
               (json.dumps({"type": "text", "text": "帮我看看"}),))
    db.execute("INSERT INTO part VALUES('p2','m1','sess_t',?,2)",
               (json.dumps({"type": "text", "text": "[系统提醒]", "synthetic": True}),))
    db.execute("INSERT INTO part VALUES('p3','m2','sess_t',?,1)",
               (json.dumps({"type": "reasoning", "text": "先查库",
                            "metadata": {"anthropic": {"signature": "sig"}}}),))
    db.execute("INSERT INTO part VALUES('p4','m2','sess_t',?,2)",
               (json.dumps({"type": "tool", "callID": "call_1", "tool": "Bash",
                            "state": {"status": "completed", "input": {"command": "ls"},
                                      "output": "a b c"}}),))
    r = cv.export_zcode_session(db, db.execute("SELECT * FROM session").fetchone())
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "thinking", "tool_call", "tool_result"]  # synthetic 被过滤
    assert r["capture"]["agent"] == "zcode" and r["capture"]["model"] == "GLM-5.3"
    assert r["capture"]["cwd"] == "D:\proj"
    tc = r["conversation"][2]; tr = r["conversation"][3]
    assert tc["call_id"] == "call_1" and tr["call_id"] == "call_1"
    assert tr["content"] == "a b c"
    assert "signature" not in json.dumps(r)  # spec §3.2: 签名不可移植即丢弃


# ---------------- serve 本地访问防护 + 账本回归 (2026-10-29 修复) ----------------

import http.client  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _req(port, method, path, body=None, headers=None):
    """直接对 127.0.0.1:port 发请求, 返回 (status, body_json); 头部完全可控。"""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    raw = r.read().decode("utf-8", errors="replace")
    c.close()
    return r.status, (json.loads(raw) if raw.startswith("{") else {})


def test_serve_local_guard_and_stats(isolated, tmp_path):
    """Host/Origin 回环白名单 + POST 强制 application/json; /stats 曾因漏导入 500。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _idx = cv.build_vault_server(vault, 0)  # 0 = 随机空闲端口
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 本地进程 (MCP 热索引 / 托盘 / 控制台同源页面) 正常访问
        code, h = _req(port, "GET", "/healthz")
        assert code == 200 and h["status"] == "ok"
        code, s = _req(port, "GET", "/stats")  # 回归: ledger_summary 漏导入曾 500
        assert code == 200 and "owned" in s and "stats" in s
        # DNS rebinding: Host 非回环 → 403
        code, _ = _req(port, "GET", "/healthz", headers={"Host": "evil.example:1"})
        assert code == 403
        # 恶意网页跨站: Origin 非回环 → 403
        code, _ = _req(port, "GET", "/healthz", headers={"Origin": "http://evil.example"})
        assert code == 403
        # 跨站简单请求 POST (text/plain 不触发预检) → 403, 不再触达 do_import
        code, _ = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": "x", "apply": True}),
                       headers={"Content-Type": "text/plain"})
        assert code == 403
        # 合法 POST: application/json + 回环 Origin → 200 (真实快照 dry-run 迁移)
        env = make_env(tmp_path)
        sid = cv.snapshot_id(env)
        (vault / "snapshots").mkdir(exist_ok=True)
        cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": sid, "target": "codex", "apply": False}),
                       headers={"Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{port}"})
        assert code == 200 and r["lines"] >= 1 and r["applied"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_ledger_relay_billing_single_entry(isolated, monkeypatch, capsys):
    """回归: billing 模式重复代码块曾致每次导入双计入 relay_usage。"""
    _, vault = isolated
    monkeypatch.delenv("CV_RELAY_KEY", raising=False)
    monkeypatch.setattr(cv, "fetch_relay_usage", lambda url, key, s, e: {"total_usage": 12.34})
    cv.cmd_ledger_import_relay(SimpleNamespace(
        vault=str(vault), mode="billing", url="https://relay.example",
        days=7, start=None, end=None, key="k", token=None, user_id=None, log_path=None))
    conn = cv.db(vault)
    rows = conn.execute("SELECT cost_usd FROM ledger_events WHERE kind='relay_usage'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 12.34


# ---------------- backup: 一致性备份与轮换 ----------------

def test_backup_content_and_rotation(isolated, tmp_path):
    import sqlite3 as _s
    import zipfile
    _, vault = isolated
    env = make_env(tmp_path)
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind) VALUES('t','t','x')")
    conn.commit()

    bdir = vault / "backups"
    cv.cmd_backup(SimpleNamespace(vault=str(vault), out=str(bdir), keep=2))
    zips = sorted(bdir.glob("cv-backup-*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as z:
        names = z.namelist()
        assert "vault.db" in names and f"snapshots/{sid}.json" in names
        z.extract("vault.db", bdir / "restore-test")
    rc = _s.connect(bdir / "restore-test" / "vault.db")  # 备份库可开且数据完整
    assert rc.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 1
    rc.close()
    assert not list(bdir.glob(".backup-tmp-*"))          # 无临时残留

    # 轮换: 伪造两份旧备份, keep=1 时应只剩最新一份
    for name in ("cv-backup-20200101-000000.zip", "cv-backup-20250101-000000.zip"):
        (bdir / name).write_bytes(b"old")
    cv.cmd_backup(SimpleNamespace(vault=str(vault), out=str(bdir), keep=1))
    left = [p.name for p in bdir.glob("cv-backup-*.zip")]
    assert len(left) == 1                                        # keep=1: 只剩最新一份
    assert not (bdir / "cv-backup-20200101-000000.zip").exists()  # 两份伪造旧备份被轮换掉
    assert not (bdir / "cv-backup-20250101-000000.zip").exists()


# ---------------- Markdown 交接简报 (可携降级档) ----------------

def test_brief_generation(isolated, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path)
    env["conversation"].append({"type": "text", "role": "assistant",
                                "content": "已完成 RBAC 方案初稿, 待评审", "ts": ""})
    env["conversation"].append({"type": "tool_call", "call_id": "c2", "name": "edit",
                                "arguments": {"path": "src/auth.py"}, "ts": ""})
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    r = cv.do_import(vault, sid, "markdown", apply=False)
    brief = Path(r["dest"]).read_text(encoding="utf-8")
    assert len(brief) <= 48 * 1024 + 200                         # 48KB 上限
    for section in ("它停在哪儿", "最近动作", "活上下文尾部", "RBAC 方案初稿", "edit", "src/auth.py"):
        assert section in brief
    assert "precise" not in brief and r["applied"] is False      # 简报不写真实会话目录


def test_brief_stop_point_prefers_last_assistant_output(isolated, tmp_path):
    """回归 (2026-11-02 审查 #5): 原生 Codex/Claude exporter 的普通 assistant 消息是
    text 块 (role=assistant), 停点检索曾只认 agent_message → 明明有进展文本,
    简报却写"会话中无 assistant 文本输出"。"""
    _, vault = isolated
    env = cv.build_envelope({"capture": {"agent": "claude-code", "cwd": "D:\\proj",
                                         "completeness": "full"},
                             "conversation": [
                                 {"type": "text", "role": "user", "content": "继续修 bug", "ts": ""},
                                 {"type": "text", "role": "assistant",
                                  "content": "已定位空指针, 修复已提交", "ts": ""}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    r = cv.do_import(vault, sid, "markdown", apply=False)
    brief = Path(r["dest"]).read_text(encoding="utf-8")
    stop = brief.split("## 它停在哪儿", 1)[1].split("## 最近动作", 1)[0]
    assert "已定位空指针, 修复已提交" in stop                      # 停点 = 最后一条 assistant 文本
    assert "会话中无 assistant 文本输出" not in stop
    # agent_message 仍是合法停点 (多 agent 场景)
    env["conversation"].append({"type": "agent_message", "author": "a",
                                "recipient": "b", "content": "移交给 b 处理", "ts": ""})
    sid2 = cv.snapshot_id(env)
    cv.atomic_write_text(vault / "snapshots" / f"{sid2}.json", json.dumps(env, ensure_ascii=False))
    stop2 = Path(cv.do_import(vault, sid2, "markdown", apply=False)["dest"]).read_text("utf-8")
    stop2 = stop2.split("## 它停在哪儿", 1)[1].split("## 最近动作", 1)[0]
    assert "移交给 b 处理" in stop2


def test_do_import_catchable_errors(isolated):
    """回归 (2026-11-02 审查 #8): 领域层抛可捕获异常 (SnapshotNotFound/BadMigrateTarget),
    不再是 SystemExit (BaseException) — HTTP 层才能映射 404/400, CLI 在 main() 统一转退出。"""
    _, vault = isolated
    with pytest.raises(cv.SnapshotNotFound):
        cv.do_import(vault, "cv_not0000000000000000000000000000000", "codex", False)
    with pytest.raises(cv.SnapshotNotFound):
        cv.load_snapshot(vault, "cv_not0000000000000000000000000000000")
    with pytest.raises(cv.BadMigrateTarget):
        cv.do_import(vault, "cv_x", "nosuchtarget", False)
    try:
        cv.do_import(vault, "cv_not0000000000000000000000000000000", "codex", False)
    except Exception as e:  # SystemExit 不是 Exception 子类 — 这正是当年 HTTP 断连的根因
        assert not isinstance(e, SystemExit)


def test_serve_token_auth(isolated, tmp_path):
    """鉴权档: 无凭据 401; ?token= 首访 200+Set-Cookie; cookie 复访 200; 错 token 401。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/healthz")
        assert code == 401                                       # 无凭据
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/healthz?token=sekret")
        r = c.getresponse(); body = r.read(); setc = r.getheader("Set-Cookie"); c.close()
        assert r.status == 200 and "vault_token=sekret" in (setc or "")
        code, _ = _req(port, "GET", "/healthz", headers={"Cookie": "vault_token=sekret"})
        assert code == 200                                       # cookie 复访
        code, _ = _req(port, "GET", "/healthz", headers={"X-Vault-Token": "sekret"})
        assert code == 200                                       # 头形态 (MCP/脚本)
        code, _ = _req(port, "GET", "/healthz?token=wrong")
        assert code == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_token_root_seeds_cookie(isolated):
    """回归 (2026-11-02 审查 #3): 首访 /?token= 的 HTML 响应曾不发 Set-Cookie,
    控制台页面加载后的所有 fetch 全部 401。根路径必须与 /healthz 同样种 cookie。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/?token=sekret")
        r = c.getresponse(); body = r.read(); setc = r.getheader("Set-Cookie"); c.close()
        assert r.status == 200 and b"<!doctype html>" in body[:40].lower()
        assert "vault_token=sekret" in (setc or "")
        # 带首访种下的 cookie 复访根路径与数据接口: 不再 401
        code, _ = _req(port, "GET", "/", headers={"Cookie": "vault_token=sekret"})
        assert code == 200
        code, h = _req(port, "GET", "/healthz", headers={"Cookie": "vault_token=sekret"})
        assert code == 200 and h["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_unknown_sid_stable_json(isolated, tmp_path):
    """回归 (2026-11-02 审查 #8): 不存在的迁移 sid 曾让 load_snapshot 抛 SystemExit
    (BaseException), 逃过 HTTP 的 except Exception, 客户端收到断连而非 JSON 错误。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        post = lambda sid: _req(port, "POST", "/migrate",
                                body=json.dumps({"id": sid, "target": "codex", "apply": False}),
                                headers={"Content-Type": "application/json"})
        # 形态非法 (含路径穿越) → 400 稳定 JSON
        code, r = post("../../etc/passwd")
        assert code == 400 and "invalid id" in r["error"]
        # 形态合法但不存在 → 404 稳定 JSON, 不再断连
        code, r = post("cv_0000000000000000")
        assert code == 404 and "not found" in r["error"]
        # GET /snapshot 与 /preview 同样受 id 形态校验保护
        code, _ = _req(port, "GET", "/snapshot?id=../vault.db%00")
        assert code == 400
        code, _ = _req(port, "GET", "/preview?id=cv_zzz")
        assert code == 400
        code, _ = _req(port, "GET", "/snapshot?id=cv_0000000000000000")
        assert code == 404
        # 未知迁移目标 → 400 (BadMigrateTarget), 不落通用 500
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": "cv_0000000000000000", "target": "nosuch",
                                        "apply": False}),
                       headers={"Content-Type": "application/json"})
        assert code == 404  # 先撞存在性 404; 换真实快照验证 target 分支
        env = make_env(tmp_path)
        sid = cv.snapshot_id(env)
        (vault / "snapshots").mkdir(exist_ok=True)
        cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": sid, "target": "nosuch", "apply": False}),
                       headers={"Content-Type": "application/json"})
        assert code == 400 and "未知迁移目标" in r["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_log_redacts_token(isolated):
    """回归 (2026-11-02 审查 #3 附带): ?token= 随 requestline 落 serve_http.log,
    鉴权档下必须脱敏为 token=***, 明文不得进日志。"""
    import re as _re
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret-value")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/healthz?token=sekret-value")
        assert code == 200
    finally:
        srv.shutdown()
        srv.server_close()
    log = (vault / "serve_http.log").read_text(encoding="utf-8")
    assert "token=***" in log
    assert "sekret-value" not in log


def _mk_snap(vault: Path, sid: str, first_user: str, blocks: int = 3):
    """写一个 VaultIndex 可解析的最小快照文件, 返回其 mtime 前的文件对象。"""
    p = vault / "snapshots" / f"{sid}.json"
    p.parent.mkdir(exist_ok=True)
    env = {"capture": {"agent": "zcode", "completeness": "full", "cwd": "D:\proj"},
           "created_at": "2026-11-01T00:00:00",
           "stats": {"blocks_total": blocks, "tokens_in": 100 * blocks},
           "conversation": ([{"type": "user", "role": "user", "content": first_user}] +
                            [{"type": "assistant", "role": "assistant", "content": f"回复{i}"}
                             for i in range(blocks - 1)])}
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    return p


def test_index_incremental_cache_and_removal(isolated):
    """索引三态: 追加走增量 (不全量), 重启加载缓存后增量追赶一致, 删除触发全量。"""
    _, vault = isolated
    bt = lambda b: b.get("content") or ""
    _mk_snap(vault, "cv_aaaa", "第一个会话 关键词甲")
    _mk_snap(vault, "cv_bbbb", "第二个会话 关键词乙")
    idx = cv.VaultIndex(vault, bt)
    assert len(idx.meta) == 2 and idx.stats == {"full": 1, "incr": 0, "cache_load": 0}

    # 增量: 纯新增 -> incr 计数, 不动全量
    _mk_snap(vault, "cv_cccc", "第三个会话 关键词甲")
    idx.rebuild()
    assert len(idx.meta) == 3 and idx.stats["incr"] == 1 and idx.stats["full"] == 1
    assert "cv_cccc" in idx.meta
    assert idx.search("关键词甲", 5)["hits"][0]["sid"] in ("cv_aaaa", "cv_cccc")

    # 缓存: 新实例直接加载, 追加的 cv_dddd 走增量追赶, 搜索/上下文跨加载一致
    _mk_snap(vault, "cv_dddd", "第四个会话 关键词乙")
    idx2 = cv.VaultIndex(vault, bt)
    assert idx2.stats["cache_load"] == 1 and idx2.stats["full"] == 0
    assert len(idx2.meta) == 4
    assert idx2.search("关键词乙", 5)["hits"][0]["sid"] in ("cv_bbbb", "cv_dddd")
    assert idx2.context("关键词甲", 3)["results"]
    assert idx2.load_env("cv_aaaa") is not None

    # 删除: 差分发现移除 -> 全量重建, 计数与搜索立刻一致
    (vault / "snapshots" / "cv_aaaa.json").unlink()
    idx2.rebuild()
    assert len(idx2.meta) == 3 and "cv_aaaa" not in idx2.meta
    assert idx2.stats["full"] == 1 and idx2.stats["cache_load"] == 1  # 全量恰好 +1
    assert (vault / "index_cache.pkl").exists()  # 落盘缓存已生成


def test_index_regression_guards(isolated):
    """复测三高危的回归门: LRU 溢出、损坏缓存回退、缺键快照跳过。"""
    import collections
    _, vault = isolated
    bt = lambda b: b.get("content") or ""
    _mk_snap(vault, "cv_good1", "正常快照 回归门甲")
    # H3: 缺 capture/stats 的合法 JSON 必须被跳过, 不炸重建/启动
    bad = vault / "snapshots" / "cv_bad1.json"
    bad.write_text(json.dumps({"created_at": "2026-11-01T00:00:00"}), encoding="utf-8")
    idx = cv.VaultIndex(vault, bt)
    assert "cv_good1" in idx.meta and "cv_bad1" not in idx.meta
    assert idx.search("回归门", 5)["hits"], "好快照应可被检索"
    # H1: LRU 溢出走 OrderedDict 逐出, 不抛 TypeError; 缓存上限被尊重
    for i in range(70):
        idx._env_cache[f"cv_pad{i}"] = {"_sid": f"cv_pad{i}"}
    assert idx.load_env("cv_good1") is not None
    assert isinstance(idx._env_cache, collections.OrderedDict)
    assert len(idx._env_cache) <= cv.VaultIndex.ENV_CACHE_MAX
    # H2: 损坏的 index_cache.pkl 必须静默回退全量, 不让 UnpicklingError 逃出启动
    (vault / "index_cache.pkl").write_bytes(b"definitely not a pickle \x80\x04\x95")
    idx2 = cv.VaultIndex(vault, bt)
    assert idx2.stats["cache_load"] == 0 and idx2.stats["full"] == 1
    assert len(idx2.meta) == 1  # bad1 被跳过, good1 在


# ---------------- 全面测试回归 (dwfrun-bdea7af6 发现) ----------------

def _mk_chain_rollout(tmp: Path, sid: str) -> Path:
    """带 token_usage_record 的最小 codex rollout (账本/同步回归用)。"""
    return w(tmp / f"rollout-2026-11-01T00-00-00-{sid}.jsonl", [
        {"timestamp": "2026-11-01T00:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": sid, "session_id": sid, "timestamp": "2026-11-01T00:00:00.000Z",
                     "cwd": "D:\\proj", "cli_version": "0.153.4", "source": "exec"}},
        {"timestamp": "2026-11-01T00:00:01.000Z", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "id": "m1", "role": "user",
                     "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-11-01T00:00:02.000Z", "ordinal": 2, "type": "token_usage_record",
         "payload": {"turn_id": "t1", "turn_token_usage": {"input_tokens": "100", "output_tokens": "20",
                                                           "cached_input_tokens": "60",
                                                           "cache_write_input_tokens": "10",
                                                           "reasoning_output_tokens": "5",
                                                           "total_tokens": "120"}}},
    ])


def test_export_idempotent_preserves_chain(isolated):
    """H1 回归: sync 已成链后 cv export 幂等跳过, 不得用 parent=NULL 的 envelope 覆盖链
    (旧代码让同一会话 usage 双计, 重新引入 09f90a6 修掉的缺陷)。"""
    from types import SimpleNamespace
    import os as _os
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "11111111-1111-1111-1111-111111111111")
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "follow up"}]}})
    w(src, rows)
    sync()                                             # 建链 v1→v2
    own0 = cv.owned_stats(vault)
    assert own0["sessions"] == 1 and own0["tokens_in"] == 100
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    own1 = cv.owned_stats(vault)
    assert own1["sessions"] == 1 and own1["tokens_in"] == 100   # 不再 2/200
    conn = cv.db(vault)
    linked = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE parent_snapshot IS NOT NULL").fetchone()["n"]
    assert linked == 1                                 # 链完好


def test_sync_content_unchanged_no_self_parent(isolated):
    """M2 回归: 仅 mtime 变化 (内容未变) 的二次 sync 不得产生 parent==id 自环
    (自环会被叶子过滤把会话从账本清零)。"""
    from types import SimpleNamespace
    import os as _os
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "22222222-2222-2222-2222-222222222222")
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    _os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 5))
    sync()
    conn = cv.db(vault)
    rows = conn.execute("SELECT id, parent_snapshot FROM snapshots").fetchall()
    assert all(r["parent_snapshot"] != r["id"] for r in rows)     # 无自环
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100       # 会话未从账本消失


def test_headroom_detail_import_idempotent(isolated, monkeypatch):
    """H2 回归: 重跑 import-headroom, compression 明细与 lifetime 一样幂等, 不再翻倍。"""
    from types import SimpleNamespace
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir(parents=True, exist_ok=True)
    w(head / "savings_events.jsonl", [
        {"v": 1, "ts": "2026-11-01T10:30:27Z", "before": 100, "after": 40, "saved": 60,
         "cost_usd": 0.03, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"},
        {"v": 1, "ts": "2026-11-01T11:00:00Z", "before": 50, "after": 10, "saved": 40,
         "cost_usd": 0.02, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"}])
    (head / "proxy_savings.json").write_text(json.dumps(
        {"schema_version": "6", "generated_at": "2026-11-01T00:00:00Z",
         "lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)
    imp = lambda: cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))  # noqa: E731
    imp()
    imp()
    imp()                                              # 连跑三次
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(tokens_saved),0) t FROM ledger_events"
                     " WHERE kind='compression'").fetchone()
    assert n["n"] == 2 and n["t"] == 100               # 旧代码 6/300


def test_ledger_stats_minimal_lifetime(isolated, monkeypatch):
    """ledger-7 防御: lifetime 缺 cache_read_tokens/total_input_tokens 键 (写入端存 null)
    时 cmd_ledger_stats 不再 int(None) TypeError。"""
    from types import SimpleNamespace
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir(parents=True, exist_ok=True)
    (head / "proxy_savings.json").write_text(json.dumps(
        {"lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)
    cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault)))       # 旧代码在此崩溃


def test_fork_of_fork_not_swallowed(isolated, capsys):
    """M4 回归: fork 的 fork 必须产生 fork_of=F1 的新实体, 不被祖先的"已存在"吞掉。"""
    from types import SimpleNamespace
    _, vault = isolated
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                             "conversation": [{"type": "text", "role": "user", "content": "base"}],
                             "stats": cv.zero_stats()})
    a_sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "snapshots" / f"{a_sid}.json").write_text(json.dumps(env), encoding="utf-8")
    fk = lambda sid: cv.cmd_fork(  # noqa: E731
        SimpleNamespace(vault=str(vault), snapshot_id=sid, message=None))
    fk(a_sid)
    f1_sid = capsys.readouterr().out.split("→ ")[1].split("\n")[0].strip()
    n0 = len(list((vault / "snapshots").glob("cv_*.json")))
    fk(f1_sid)                                         # 旧代码在此报"已存在"指向 A
    f2 = [p for p in (vault / "snapshots").glob("cv_*.json")
          if json.loads(p.read_text(encoding="utf-8")).get("lineage", {}).get("fork_of") == f1_sid]
    assert len(list((vault / "snapshots").glob("cv_*.json"))) == n0 + 1
    assert len(f2) == 1


def test_export_codex_compaction_replaced_range():
    """migrate-1 回归: replaced_blocks = 上次压缩边界到当前最后一个会话块 (含端点)。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = w(d / "rollout-c.jsonl", [
        {"timestamp": "t", "ordinal": 0, "type": "session_meta",
         "payload": {"session_id": "s", "cwd": "D:\\p"}},
        {"timestamp": "t", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "q1"}]}},
        {"timestamp": "t", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "a1"}]}},
        {"timestamp": "t", "ordinal": 3, "type": "compacted", "payload": {"message": "sum1"}},
        {"timestamp": "t", "ordinal": 4, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "q2"}]}},
        {"timestamp": "t", "ordinal": 5, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "a2"}]}},
        {"timestamp": "t", "ordinal": 6, "type": "compacted", "payload": {"message": "sum2"}}])
    rb = [b["replaced_blocks"] for b in cv.export_codex(p, False)["conversation"]
          if b["type"] == "compaction"]
    assert rb == [[0, 1], [3, 4]]                      # 旧代码 [[0, 0], [0, 2]]


def test_blocks_to_codex_last_agent_message():
    """migrate-2 回归: agent_message 结尾的会话, task_complete.last_agent_message 不再为空。"""
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "C:\\w", "completeness": "full"},
                             "conversation": [{"type": "agent_message", "author": "a", "recipient": "b",
                                               "content": "final agent message", "ts": ""}],
                             "stats": cv.zero_stats()})
    lines, _, _, _ = cv.blocks_to_codex(env, False)
    tc = [l for l in lines if l.get("type") == "event_msg"
          and l.get("payload", {}).get("type") == "task_complete"][0]
    assert tc["payload"]["last_agent_message"] == "final agent message"


def test_owned_stats_ingest_denominator(isolated):
    """ledger-3 回归: via-smigrate 产物是 Claude 语义 usage, 命中率分母按 Claude 归一。"""
    _, vault = isolated
    _register_env(vault, {"agent": "cursor", "source_format": "via-smigrate:cursor"},
                  {"tokens_in": 10, "cache_read": 90, "cache_write": 0,
                   "total_tokens": 110, "usage_events": 1}, "2026-11-01T00:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == 0.9                # 旧代码 9.0 (900%)


def test_guard_rejection_drains_body(isolated):
    """M1 回归: 守卫拒绝的 POST 已先读净 body — 同一 keep-alive 连接的下一条请求完好。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/migrate", body=json.dumps({"id": "cv_bad", "target": "codex"}),
                  headers={"Content-Type": "text/plain"})          # 跨站简单请求 → 403
        r1 = c.getresponse()
        r1.read()
        assert r1.status == 403
        c.request("GET", "/healthz")                               # 同一 keep-alive 连接
        r2 = c.getresponse()
        body2 = r2.read()
        assert r2.status == 200                                    # 旧代码收到 501/400 垃圾
        assert json.loads(body2)["status"] == "ok"
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_errors_are_400_not_500(isolated):
    """serve-2 回归: 数值参数坏 / JSON body 坏 → 400 稳定 JSON, 不落 500 兜底回显异常文本。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/recent?limit=abc")
        assert code == 400 and "limit" in r["error"]
        code, r = _req(port, "POST", "/migrate", body="{not json",
                       headers={"Content-Type": "application/json"})
        assert code == 400 and "error" in r
    finally:
        srv.shutdown()
        srv.server_close()


def test_stats_dbless_no_poison(isolated, monkeypatch):
    """M5 回归: 无 vault.db 时 /stats 200 且不留零表空库; 事后冷路径 vault_stats 仍诚实。"""
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                             "conversation": [{"type": "text", "role": "user", "content": "hi"}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/stats")
        assert code == 200                                          # 旧代码 500 + 留零表空库
    finally:
        srv.shutdown()
        srv.server_close()
    import mcp_server  # noqa: E402
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    out = mcp_server.dispatch("vault_stats", {}, vault)
    # 修复后 /stats 会把库建齐 (不再留零表毒化), 冷路径两种诚实空态都合法:
    # 库不存在 → "账本为空"回退; 库为空表 → 全 0 数字。唯独不能再 isError
    assert "isError" not in out and "no such table" not in out
    assert "账本为空" in out or "快照数: 0" in out


def test_cold_search_skips_malformed_snapshot(isolated, monkeypatch):
    """M6 回归: 畸形快照文件不废掉冷路径 search/recent (与热索引一致地跳过)。"""
    import mcp_server  # noqa: E402
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    good = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                              "conversation": [{"type": "text", "role": "user",
                                                "content": "phantom 迁移方案"}],
                              "stats": cv.zero_stats()})
    sid = cv.snapshot_id(good)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(good), encoding="utf-8")
    (vault / "snapshots" / "cv_malformed000001.json").write_text(
        json.dumps({"conversation": [{"type": "text", "role": "user", "content": "phantom 迁移"}]}),
        encoding="utf-8")
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    out = mcp_server.dispatch("vault_search", {"query": "phantom"}, vault)
    assert sid in out and "isError" not in out
    out2 = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
    assert sid in out2


def test_search_tie_hot_cold_same_order(isolated, monkeypatch):
    """M7 回归: 得分并列时冷热返回同一快照 (limit 截断边界命中集一致, 不随哈希种子漂移)。"""
    import mcp_server  # noqa: E402
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    for fname, cat, text in (("cv_aaaa111111111111.json", "2026-01-01T00:00:00", "alpha here"),
                             ("cv_bbbb222222222222.json", "2026-02-02T00:00:00", "beta here")):
        env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                                 "conversation": [{"type": "text", "role": "user", "content": text}],
                                 "stats": cv.zero_stats()})
        env["created_at"] = cat
        (vault / "snapshots" / fname).write_text(json.dumps(env), encoding="utf-8")
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, hot = _req(port, "GET", "/search?q=alpha%20beta&limit=1")
        assert code == 200
        hot_sid = hot["hits"][0]["sid"]
    finally:
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    cold = mcp_server.dispatch("vault_search", {"query": "alpha beta", "limit": 1}, vault)
    assert hot_sid in cold


def test_zcode_subsecond_update_syncs(isolated, monkeypatch, tmp_path):
    """ledger-8 回归: zcode 水位严格前进 — 秒内的新消息增量不再被 <1s 容差吞掉。"""
    import sqlite3 as _s
    from types import SimpleNamespace
    _, vault = isolated
    zdb_p = tmp_path / "zcode.sqlite"
    db = _s.connect(zdb_p)
    db.executescript("""CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_updated INTEGER,
        time_deleted INTEGER, time_created INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, sequence INTEGER);""")
    db.execute("INSERT INTO session VALUES('s1','D:\\p',NULL,1000,NULL,1000)")
    db.execute("INSERT INTO message VALUES('m1','s1',?,900,1)", (json.dumps({"role": "user"}),))
    db.execute("INSERT INTO part VALUES('p1','m1','s1',?,1)",
               (json.dumps({"type": "text", "text": "第一句"}),))
    db.commit()
    monkeypatch.setattr(cv, "ZCODE_DB", zdb_p)
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    db.execute("INSERT INTO part VALUES('p2','m1','s1',?,2)",
               (json.dumps({"type": "text", "text": "秒回"}),))
    db.execute("UPDATE session SET time_updated=1500 WHERE id='s1'")   # 仅 +500ms
    db.commit()
    sync()                                             # 旧代码: 0.5s < 1s 容差 → "秒回"永久丢失
    snaps = sorted((vault / "snapshots").glob("cv_*.json"))
    assert any("秒回" in p.read_text(encoding="utf-8") for p in snaps)
